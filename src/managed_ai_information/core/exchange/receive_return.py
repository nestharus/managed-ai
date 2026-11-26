"""Dispatch a packet, receive its attributed return, and validate the retained receipt."""

import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..storage import ObjectStore, encode, now, read_json, sha, write_json
from ..storage import read_bytes as _bytes
from .carrier_receipts import retain_transport, transport_outcome, transport_view
from .proposal_files import (
    _proposal_bytes,
    _proposal_directory,
    _run_nominations,
    proposal_mappings,
)

if TYPE_CHECKING:
    from .transport.packet_transport import PacketTransport


class PacketReceiving:
    """Dispatch and attributed-return receiving for exchange packets."""

    if TYPE_CHECKING:
        objects: ObjectStore
        transport: PacketTransport

        def load(self, packet: str) -> tuple[Path, dict[str, Any]]: ...
        def put(self, data: bytes) -> str: ...

    def dispatch(
        self,
        packet: str,
        argv: list[str],
        *,
        proposals: list[str] | None = None,
        producer: str | None = None,
    ) -> dict[str, Any]:
        directory, request = self.load(packet)
        if (directory / "return.json").exists():
            raise ValueError("packet already has a received return")
        if (directory / "transport-outcome.json").exists():
            transport_outcome(self, directory, request)
            raise ValueError("terminal transport outcome recorded; prepare a fresh occasion")

        nominations = None
        if proposals:
            mappings = proposal_mappings(request, proposals)
            if producer is not None:
                if not producer.strip():
                    raise ValueError("producer attribution is required")
                nominations = _run_nominations(producer, mappings)

        return self.transport.dispatch(packet, directory, request, argv, nominations=nominations)

    def _resolve_carrier_return(
        self,
        packet: str,
        directory: Path,
        request: dict[str, Any],
        producer: str | None,
        mappings: dict[str, str],
        wait: float,
        run_nominations: bool,
    ) -> tuple[
        bytes | None, dict[str, Any] | None, str | None, dict[str, str], Any, dict[str, Any]
    ]:
        marker = self.transport.bound_marker(packet, directory, request)
        bound = False
        origin = None
        saved = None

        if "run_nominations" in marker:
            saved = marker["run_nominations"]
            if saved["version"] != 1 or not saved["producer"].strip():
                raise ValueError("invalid original run nominations")
            if producer is not None and producer != saved["producer"]:
                raise ValueError("receive producer differs from original bounded run")
            producer = saved["producer"]
            if mappings and _run_nominations(producer, mappings) != saved:
                raise ValueError("receive nominations differ from original bounded run")
            bound = True
        elif run_nominations:
            raise ValueError(
                "original run nominations unavailable; use the original explicit receive"
            )

        data, view = self.transport.collect_marked(marker, wait)
        pending: dict[str, Any] = {"packet": packet, "received": False, "carrier": view}
        if bound:
            pending["run_nominations"] = True

        if data is None:
            return None, view, producer, mappings, origin, pending

        if bound and saved is not None:
            proposals = [
                target + "=" + os.fsdecode(bytes.fromhex(raw)) for target, raw in saved["files"]
            ]
            mappings = proposal_mappings(request, proposals)
            origin = saved["origin"]

        return data, view, producer, mappings, origin, pending

    def _collect_proposals(
        self,
        request: dict[str, Any],
        mappings: dict[str, str],
        origin: Any,
        *,
        from_carrier: bool,
        pending: dict[str, Any] | None,
    ) -> tuple[list[dict[str, Any]] | None, dict[str, Any] | None]:
        expected = {r["path"]: r for r in request["targets"]}
        changes: list[dict[str, Any]] = []

        with _proposal_directory(origin) as source_directory:
            for target, source in mappings.items():
                try:
                    body = _proposal_bytes(source, source_directory)
                except (OSError, ValueError) as exc:
                    if not from_carrier:
                        raise
                    display = source.encode("utf-8", "backslashreplace").decode("utf-8")
                    error: dict[str, Any] = {
                        "target": target,
                        "file": display,
                        "error": str(exc).encode("utf-8", "backslashreplace").decode("utf-8"),
                    }
                    if display != source:
                        error["file_bytes_hex"] = os.fsencode(source).hex()
                    if pending is None:
                        raise RuntimeError(
                            "carrier proposal collection requires pending state"
                        ) from exc
                    return None, dict(pending, proposal_error=error)

                proposal: dict[str, Any] = {
                    "path": target,
                    "sha256": self.put(body),
                    "bytes": len(body),
                    "base_sha256": expected[target]["sha256"],
                }
                if from_carrier:
                    proposal["source"] = "caller-nominated-file"
                changes.append(proposal)

        return changes, None

    def receive(
        self,
        packet: str,
        reply: Path | None,
        producer: str | None,
        proposals: list[str],
        *,
        wait: float = 0,
        run_nominations: bool = False,
    ) -> dict[str, Any]:
        directory, request = self.load(packet)
        if (directory / "return.json").exists():
            raise ValueError("return already recorded; preserve it and prepare another occasion")
        if (directory / "transport-outcome.json").exists():
            transport_outcome(self, directory, request)
            raise ValueError("terminal transport outcome recorded; prepare a fresh occasion")
        if run_nominations and (reply is not None or proposals or producer is not None):
            raise ValueError("original run nominations carry their own files, carrier and producer")
        if not run_nominations and (not producer or not producer.strip()):
            raise ValueError(
                "producer attribution is required; it is caller-asserted, not authenticated"
            )

        mappings = proposal_mappings(request, proposals)
        origin = None
        pending: dict[str, Any] | None = None
        view: dict[str, Any] | None = None
        transport: dict[str, Any]

        if reply is None:
            data, view, producer, mappings, origin, pending = self._resolve_carrier_return(
                packet, directory, request, producer, mappings, wait, run_nominations
            )
            if data is None:
                return pending
        else:
            if self.transport.has_attempt(directory):
                raise ValueError("a dispatched packet must receive its actual carrier return")
            data = _bytes(reply)
            transport = {
                "kind": "caller-import",
                "source_path": str(reply.resolve()),
                "scope": "caller-asserted producing occurrence; not independently authenticated",
            }

        changes, proposal_error = self._collect_proposals(
            request,
            mappings,
            origin,
            from_carrier=(reply is None),
            pending=pending,
        )
        if proposal_error is not None:
            return proposal_error
        if changes is None:
            raise RuntimeError("proposal collection returned neither changes nor error")

        reply_sha256 = self.put(data)
        if reply is None:
            if view is None:
                raise RuntimeError("carrier return requires a transport view")
            transport = retain_transport(self, view)

        record: dict[str, Any] = {
            "version": 1,
            "packet": packet,
            "consumer": request["consumer"],
            "request_sha256": sha(encode(request)),
            "producer": producer,
            "received_at": now(),
            "reply_sha256": reply_sha256,
            "reply_bytes": len(data),
            "proposals": changes,
            "transport": transport,
        }
        record["return_sha256"] = sha(encode(record))
        write_json(directory / "return.json", record)
        return record

    def validate_return(
        self,
        directory: Path,
        record: dict[str, Any],
        *,
        request_sha256: str | None = None,
        _verified_objects: dict[str, int] | set[str] | None = None,
        _objects_fd: int | None = None,
        _memo_lock: Any = None,
    ) -> None:
        if (directory / "transport-outcome.json").exists():
            raise ValueError("packet contains both a return and a terminal outcome")
        if record["return_sha256"] != sha(
            encode({k: v for k, v in record.items() if k != "return_sha256"})
        ):
            raise ValueError("return record changed")

        if request_sha256 is None:
            request = read_json(directory / "request.json")
            request_sha256 = sha(encode(request))
        if record["request_sha256"] != request_sha256:
            raise ValueError("return belongs to another request")

        def verify_object(key: str, expected_bytes: int) -> None:
            if isinstance(_verified_objects, dict):
                if _memo_lock is not None:
                    with _memo_lock:
                        known = _verified_objects.get(key)
                else:
                    known = _verified_objects.get(key)
                if known is not None:
                    if known != expected_bytes:
                        raise ValueError("content object changed (length changed)")
                    return

            self.objects.read(key, expected_bytes=expected_bytes, dir_fd=_objects_fd)
            if isinstance(_verified_objects, dict):
                if _memo_lock is not None:
                    with _memo_lock:
                        _verified_objects[key] = expected_bytes
                else:
                    _verified_objects[key] = expected_bytes

        if type(record["reply_bytes"]) is not int or record["reply_bytes"] < 0:
            raise ValueError("invalid reply length")
        verify_object(record["reply_sha256"], record["reply_bytes"])

        for row in record["proposals"]:
            if type(row["bytes"]) is not int or row["bytes"] < 0:
                raise ValueError("invalid proposal length")
            verify_object(row["sha256"], row["bytes"])

        view = transport_view(self, record["transport"])
        if view is not None:
            stdout = view["streams"]["stdout"]
            if (
                stdout["sha256"] != record["reply_sha256"]
                or stdout["byte_count"] != record["reply_bytes"]
            ):
                raise ValueError("transport stdout does not match the reply")
