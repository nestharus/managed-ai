"""Inspect packet, transport, and publication state through read-only observations."""
from __future__ import annotations

import os
import threading
from typing import Any

from ..storage import open_directory, read_json
from .carrier_receipts import transport_outcome
from .publication import observe_basis, observe_target
from .publication_receipts import (
    bound_publication_proposal,
    publication_inventory,
    publication_matches,
    publication_name,
    validate_publication_record,
    validate_publication_reconciliation,
)


def inspect_publications(exchange, directory, request, result, observe) -> None:
    try:
        publications, reconciliations, unreadable = publication_inventory(directory)
    except OSError as exc:
        result["publication_inventory_error"] = str(exc)
        return

    targets = {row["path"]: row for row in request["targets"]}
    received = result["return"]
    attached: set[str] = set()

    for name, (record, digest) in publications.items():
        observation: dict[str, Any] = {"record_file": name, "record": record}
        try:
            target = record.get("path")
            if not isinstance(target, str) or target not in targets:
                raise ValueError("publication path is not a declared target")

            try:
                current = observe(target).copy()
            except (OSError, ValueError) as exc:
                observation["observation_error"] = str(exc)
            else:
                current["matches_base"] = current["sha256"] == targets[target]["sha256"]
                observation["current_target"] = current
                proposal = bound_publication_proposal(result["packet"], request, received, target)
                if proposal is not None:
                    current["matches_proposal"] = current["sha256"] == proposal["sha256"]

            if not publication_matches(result["packet"], request, record, received):
                raise ValueError("publication receipt binding changed")

            validate_publication_record(record)
            canonical = publication_name(target, "reconcile")
            matching = [
                slot
                for slot, (account, _) in reconciliations.items()
                if account.get("intent_sha256") == digest
            ]
            slot = canonical if canonical in reconciliations else matching[0] if len(matching) == 1 else None
            if slot is not None:
                account = reconciliations[slot][0]
                observation["reconciliation"] = validate_publication_reconciliation(
                    request,
                    received,
                    record,
                    digest,
                    account,
                )
                attached.add(slot)
                if slot != canonical:
                    observation["reconciliation_file"] = slot
        except (OSError, ValueError, KeyError, TypeError) as exc:
            observation.setdefault("observation_error", str(exc))

        result["publications"].append(observation)

    unattached = [
        {"record_file": name, "record": record}
        for name, (record, _) in reconciliations.items()
        if name not in attached
    ]
    for name, error in unreadable.items():
        row = {"record_file": name, "observation_error": str(error)}
        (result["publications"] if name.lower().startswith("publish-") else unattached).append(row)

    if unattached:
        result["unattached_reconciliations"] = unattached


class PacketInspection:
    """Read-only inspection and listing of retained exchange packets."""

    def packet_summary(self, directory, consumer, publications, *, returned, outcome):
        """Summarize validated observations of one packet under the caller's guard."""
        attempted = self.transport.has_attempt(directory)
        if outcome is not None:
            transport = "terminal-without-return"
        elif returned:
            transport = "returned"
        else:
            transport = "dispatch-attempted" if attempted else "prepared"

        row: dict[str, Any] = {
            "packet": directory.name,
            "consumer": consumer,
            "transport": transport,
            "publications": [{"path": p["path"], "phase": p["phase"]} for p in publications],
            "semantic_fulfillment": "not-decided-by-exchange",
        }
        if outcome is not None:
            row["transport_outcome"] = {k: outcome[k] for k in ("reason", "account", "outcome_sha256")}
        return row

    def list(self, consumer: str | None = None) -> list[dict[str, Any]]:
        """Expose retained occasions without inventing a semantic completion flag."""
        verified_objects: dict[str, int] = {}
        validated_returns: dict[str, dict[str, Any]] = {}
        request_shas: dict[str, str] = {}
        memo_lock = threading.Lock()
        objects_fd = None

        if hasattr(os, "O_NOFOLLOW") and hasattr(os, "O_DIRECTORY"):
            try:
                objects_fd = open_directory(self.root / "objects")
            except OSError:
                objects_fd = None

        def inspect_one(directory):
            try:
                _, request = self._load_directory(
                    directory,
                    _verified_objects=verified_objects,
                    _validated_returns=validated_returns,
                    _objects_fd=objects_fd,
                    _memo_lock=memo_lock,
                    _request_shas=request_shas,
                )
                if consumer is not None and request["consumer"] != consumer:
                    return None

                has_return = (directory / "return.json").exists()
                if has_return:
                    with memo_lock:
                        cached = directory.name in validated_returns
                        req_sha = request_shas.get(directory.name)
                    if not cached:
                        record = read_json(directory / "return.json")
                        self.validate_return(
                            directory,
                            record,
                            request_sha256=req_sha,
                            _verified_objects=verified_objects,
                            _objects_fd=objects_fd,
                            _memo_lock=memo_lock,
                        )
                        with memo_lock:
                            validated_returns[directory.name] = record

                outcome = transport_outcome(self, directory, request)
                publications = [read_json(p) for p in directory.glob("publish-*.json")]
                return self.packet_summary(
                    directory,
                    request["consumer"],
                    publications,
                    returned=has_return,
                    outcome=outcome,
                )
            except (ValueError, KeyError, OSError, TypeError) as exc:
                return {"packet": directory.name, "transport": "record-unresolved", "error": str(exc)}

        try:
            with self.objects.pack_session():
                packet_dirs = [
                    directory
                    for directory in sorted((self.root / "packets").iterdir())
                    if not directory.name.startswith(".") and directory.is_dir()
                ]
                if len(packet_dirs) >= 4:
                    from concurrent.futures import ThreadPoolExecutor

                    with ThreadPoolExecutor(max_workers=min(8, len(packet_dirs))) as pool:
                        observed = list(pool.map(inspect_one, packet_dirs))
                else:
                    observed = [inspect_one(directory) for directory in packet_dirs]
        finally:
            if objects_fd is not None:
                os.close(objects_fd)

        return [row for row in observed if row is not None]

    def inspect(self, packet: str) -> dict[str, Any]:
        with self.objects.pack_session():
            directory, request = self.load(packet)
            result: dict[str, Any] = {"packet": packet, "consumer": request["consumer"], "basis": None, "return": None}
            observations: dict[str, Any] = {}

            def observe(path: str):
                if path not in observations:
                    try:
                        observations[path] = observe_target(self, path)
                    except (OSError, ValueError) as exc:
                        observations[path] = exc

                observation = observations[path]
                if isinstance(observation, Exception):
                    raise observation
                return observation

            try:
                result["basis"] = observe_basis(self, packet, directory, request, observe=observe)
            except (OSError, ValueError, KeyError, TypeError) as exc:
                result["basis_error"] = str(exc)

            result["publications"] = []
            if (directory / "return.json").exists():
                record = read_json(directory / "return.json")
                self.validate_return(directory, record)
                result["return"] = record
                data = self.get(record["reply_sha256"])
                try:
                    result["reply_text"] = data.decode("utf-8")
                except UnicodeDecodeError:
                    result["reply_text"] = None

            inspect_publications(self, directory, request, result, observe)
            result["inspection_scope"] = (
                "Read-only observations in one cooperative-owner invocation; not an atomic external-writer "
                "snapshot, historical completion proof, reconciliation or permission to replay."
            )

            outcome = transport_outcome(self, directory, request)
            if outcome is not None:
                result["transport_outcome"] = outcome

            return result
