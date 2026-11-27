"""Peer-specific tools exposing launch context, verified peer reads, and return staging."""

import base64
from pathlib import Path
from typing import Any

from managed_ai_information.companions.peer.peer_protocol import (
    OWNER_STATUSES,
    RETURN_SCHEMA,
    atomic_write_json,
    canonical,
    detached,
    digest,
    fields,
    identifier,
    identity,
    integer,
    need,
    path_conflicts,
    scoped_file,
)
from managed_ai_information.companions.peer.peer_protocol import (
    WORKBENCH_STATE_SCHEMA as STATE_SCHEMA,
)
from managed_ai_information.companions.peer.peer_protocol import (
    canonical_absolute as absolute,
)
from managed_ai_information.companions.peer.peer_protocol import (
    loads_strict as decode_json,
)
from managed_ai_information.companions.peer.peer_protocol import (
    target_path as relative,
)

COUNT_KEYS = ("model_calls", "tool_calls", "peer_read_calls", "peer_read_bytes")

__all__ = ["COUNT_KEYS", "PeerTools", "write_json"]


def write_json(path: Path | str, value: Any) -> None:
    atomic_write_json(path, value, exclusive=True)


class PeerTools:
    """Peer-specific tools exposing launch context, verified peer reads, and return staging."""

    def __init__(
        self,
        config: dict[str, Any],
        request: dict[str, Any],
        workbench: Any,
        directory: Path,
    ) -> None:
        self.config = config
        self.request = request
        self.workbench = workbench
        self.directory = directory
        self.staged: dict[str, Any] | None = None
        self.reads: list[dict[str, Any]] = []
        self.max_file = config.get("max_file_bytes", 2097152)

    def context(self, arguments: dict[str, Any]) -> dict[str, Any]:
        fields(arguments, ())
        return {"request": detached(self.request)}

    @staticmethod
    def _verify_captured_manifest(
        owner: str,
        revision: int,
        response: dict[str, Any],
        manifest: Any,
    ) -> tuple[dict[str, dict[str, Any]], dict[str, bytes]]:
        need(
            response.get("schema") == RETURN_SCHEMA
            and response.get("owner") == owner
            and response.get("revision") == revision
            and isinstance(response.get("files"), dict),
            "Captured response identity mismatch",
        )
        need(isinstance(manifest, list), "Captured manifest must be an array")

        entries: dict[str, dict[str, Any]] = {}
        for item in manifest:
            fields(item, ("path", "bytes", "sha256", "owner", "revision"))
            name = item["path"]
            relative(name)
            need(
                name not in entries
                and item["owner"] == owner
                and item["revision"] == revision
                and integer(item["bytes"]),
                "Captured manifest identity mismatch",
            )
            entries[name] = item

        need(
            set(entries) == set(response["files"]) and not path_conflicts(entries),
            "Captured response/manifest membership mismatch",
        )

        decoded: dict[str, bytes] = {}
        for name, value in response["files"].items():
            need(isinstance(value, str), "Captured returned file must be base64")
            body = base64.b64decode(value, validate=True)
            need(
                identity(body) == {k: entries[name][k] for k in ("bytes", "sha256")},
                "Captured returned bytes disagree with manifest: " + name,
            )
            decoded[name] = body

        return entries, decoded

    def read(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Verify and read selected artifact files from an accepted launch-time peer checkpoint."""
        record: dict[str, Any] = {
            "attempt": len(self.reads) + 1,
            "arguments": detached(arguments),
            "successful": False,
            "io_bytes": 0,
            "selected_body_bytes": 0,
        }

        def observed(path: Path) -> bytes:
            data = path.read_bytes()
            record["io_bytes"] += len(data)
            return data

        try:
            fields(arguments, ("owner", "paths"))
            owner, paths = arguments["owner"], arguments["paths"]
            need(owner in self.request["peers"], "Peer is outside this launch-time snapshot")
            need(
                isinstance(paths, list)
                and bool(paths)
                and all(isinstance(p, str) for p in paths)
                and len(paths) == len(set(paths)),
                "paths must be a nonempty unique string array",
            )
            for path_str in paths:
                relative(path_str)

            peer = self.request["peers"][owner]
            revision = peer["revision"]
            need(revision > 0, "Peer has no captured contribution in this request")

            response_path = absolute(peer["return_path"], "peer.return_path", file=True)
            manifest_path = absolute(peer["manifest_path"], "peer.manifest_path", file=True)
            artifacts_dir = absolute(peer["artifacts_dir"], "peer.artifacts_dir", directory=True)

            response_body = observed(response_path)
            manifest_body = observed(manifest_path)
            entries, decoded = self._verify_captured_manifest(
                owner,
                revision,
                decode_json(response_body),
                decode_json(manifest_body),
            )

            result: dict[str, Any] = {
                "owner": owner,
                "revision": revision,
                "response_identity": identity(response_body),
                "manifest_identity": identity(manifest_body),
                "files": {},
                "scope": (
                    "captured structurally accepted peer contribution; no semantic acceptance or "
                    "latest-currentness claim"
                ),
            }
            for name in paths:
                need(name in entries, "Selected path is absent from the captured manifest: " + name)
                need(
                    entries[name]["bytes"] <= self.max_file,
                    "Selected peer body exceeds per-file bound: " + name,
                )
                path = scoped_file(artifacts_dir, name)
                need(
                    path.stat().st_size <= self.max_file,
                    "Selected captured file exceeds per-file bound: " + name,
                )
                body = observed(path)
                need(
                    body == decoded[name],
                    "Captured file body differs from accepted returned bytes: " + name,
                )
                record["selected_body_bytes"] += len(body)

                item = identity(body)
                try:
                    item.update(encoding="utf-8", body=body.decode("utf-8"))
                except UnicodeDecodeError:
                    item.update(encoding="base64", base64=base64.b64encode(body).decode("ascii"))
                result["files"][name] = item

            record.update(
                successful=True,
                owner=owner,
                revision=revision,
                response_identity=result["response_identity"],
                manifest_identity=result["manifest_identity"],
                selected_files={name: identity(decoded[name]) for name in paths},
            )
            return result
        except Exception as exc:
            record["error"] = {"type": type(exc).__name__, "message": str(exc)}
            raise
        finally:
            self.reads.append(record)
            write_json(self.directory / f"peer-read-{record['attempt']:04d}.json", record)

    def stage(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Validate and stage exact workspace artifact bytes and peer messages for host return."""
        fields(arguments, ("status", "state", "obligations", "ack", "send", "files"))
        status = arguments["status"]
        need(status in OWNER_STATUSES, "Invalid staged owner status")
        need(isinstance(arguments["obligations"], list), "Staged obligations must be an array")
        need(
            status != "complete" or not arguments["obligations"],
            "Complete owner cannot retain finite obligations",
        )

        ack = arguments["ack"]
        need(
            isinstance(ack, list)
            and all(isinstance(x, str) for x in ack)
            and len(ack) == len(set(ack)),
            "ack must be a unique string array",
        )
        need(
            set(ack) <= {msg["id"] for msg in self.request["inbox"]},
            "Cannot acknowledge a message absent from this request",
        )

        sends = arguments["send"]
        need(isinstance(sends, list), "send must be an array")
        owners = set(self.request["peers"]) | {self.request["owner"]}
        seen: dict[str, bytes] = {}
        for message in sends:
            fields(message, ("id", "to", "payload"))
            need(
                identifier(message["id"]) and message["to"] in owners,
                "Invalid outgoing message identity/destination",
            )
            body = canonical({"to": message["to"], "payload": message["payload"]})
            need(
                message["id"] not in seen or seen[message["id"]] == body,
                "Changed outgoing message under reused local identity",
            )
            seen[message["id"]] = body

        names = arguments["files"]
        need(
            isinstance(names, list)
            and all(isinstance(x, str) for x in names)
            and len(names) == len(set(names)),
            "files must be a unique string array",
        )
        for name in names:
            relative(name)
        need(not path_conflicts(names), "Staged file/directory conflict")

        captured: dict[str, bytes] = {}
        pins: dict[str, dict[str, Any]] = {}
        total = 0
        for name in names:
            path = scoped_file(self.workbench.workspace, name)
            need(
                path.stat().st_size <= self.max_file, "Staged file exceeds per-file bound: " + name
            )
            body = path.read_bytes()
            need(len(body) <= self.max_file, "Staged file exceeds per-file bound: " + name)
            total += len(body)
            need(
                total <= self.request["limits_remaining"]["max_artifact_bytes"],
                "Staged files exceed current harness artifact allowance",
            )
            captured[name] = body
            pins[name] = identity(body)

        self.staged = {"arguments": detached(arguments), "captured": captured, "pins": pins}
        return {
            "staged": True,
            "status": status,
            "finite_obligations": len(arguments["obligations"]),
            "acknowledgments": len(ack),
            "messages": len(sends),
            "files": pins,
            "scope": (
                "candidate only; messages/acknowledgments take effect only through host acceptance "
                "of a successful return"
            ),
        }

    def finish(self, result: dict[str, Any], config_sha: str) -> dict[str, Any]:
        need(result["status"] == "complete", "Model turn did not complete: " + result["status"])
        need(self.staged is not None, "Completed model turn has no staged peer return")
        staged = self.staged
        if staged is None:
            raise RuntimeError("Completed model turn has no staged peer return")

        for name, body in staged["captured"].items():
            need(
                scoped_file(self.workbench.workspace, name).read_bytes() == body,
                "Selected workspace file changed after staging: " + name,
            )

        previous = self.request["state"]
        accounting = (
            dict.fromkeys(COUNT_KEYS, 0) if previous is None else dict(previous["accounting"])
        )
        accounting["model_calls"] += result["model_calls"]
        accounting["tool_calls"] += result["tool_calls"]
        accounting["peer_read_calls"] += sum(entry["successful"] for entry in self.reads)
        accounting["peer_read_bytes"] += sum(
            entry["selected_body_bytes"] for entry in self.reads if entry["successful"]
        )

        state = {
            "schema": STATE_SCHEMA,
            "owner": self.request["owner"],
            "revision": self.request["revision"],
            "worker_config_sha256": config_sha,
            "model_state": staged["arguments"]["state"],
            "continuing_duties": detached(self.config.get("obligations", [])),
            "completed_turns": 1 if previous is None else previous["completed_turns"] + 1,
            "accounting": accounting,
            "last_turn": {
                "evidence_dir": str(self.directory),
                "request_sha256": digest(canonical(self.request)),
                "model_status": result["status"],
                "scope": "local completed model turn; host admission is subsequent",
            },
        }
        return {
            "schema": RETURN_SCHEMA,
            "owner": self.request["owner"],
            "revision": self.request["revision"],
            "state": state,
            "status": staged["arguments"]["status"],
            "obligations": staged["arguments"]["obligations"],
            "ack": staged["arguments"]["ack"],
            "send": staged["arguments"]["send"],
            "files": {
                name: base64.b64encode(body).decode("ascii")
                for name, body in staged["captured"].items()
            },
        }
