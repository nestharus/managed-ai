"""Individual guard checks for turn working-copy retirement (MAINTENANCE.md)."""

import os
import posixpath
from collections.abc import Callable
from pathlib import Path
from typing import Any

from managed_ai.core.exchange.carrier_receipts import transport_outcome
from managed_ai.core.exchange.publication_receipts import (
    publication_inventory,
    publication_matches,
    publication_name,
    validate_publication_record,
)
from managed_ai.core.formats import TURN_SELECTION_FORMAT, validate_optional_format
from managed_ai.core.storage import (
    MAX_BYTES,
    encode,
    is_ordinary_dir_stat,
    is_ordinary_file_stat,
    read_bytes,
    sha,
)
from managed_ai.store.protocol.identities import exact_fields, require, strict_json
from managed_ai.store.turns.preparation_journal import feedback_id

TURNS_RELATIVE = ".system/turns"

_TURN_REQUEST_ALLOWED_FIELDS = {
    "task",
    "consumer",
    "records",
    "sources",
    "techniques",
    "targets",
    "returns",
    "artifacts",
    "attention",
    "record_limit",
    "attention_limit",
    "attention_max_bytes",
    "attention_groups",
    "attention_items",
    "project_sources",
    "discovery",
    "source_pins",
    "compositions",
    "feedback_uses",
}


def _read_strict_file(path: Path, limit: int = MAX_BYTES) -> dict[str, Any]:
    return strict_json(read_bytes(path, limit))


def _lstat_or_none(path: Path) -> os.stat_result | None:
    try:
        return path.lstat()
    except FileNotFoundError:
        return None


def _check_packet_and_return(
    system: Any,
    turn: str,
    refuse: Callable[..., None],
) -> tuple[Path, dict[str, Any] | None, dict[str, Any] | None, dict[str, Any] | None]:
    """Guard (a): Packet exists, validates cleanly, and has a strict validated return.json."""
    packet_dir = system.ex.directory(turn)
    staged_dir = system.ex.prepared_directory(turn)
    if _lstat_or_none(staged_dir) is not None:
        refuse("staged_preparation_present", str(staged_dir.name))

    binding = None
    request = None
    returned = None
    pkt_info = _lstat_or_none(packet_dir)
    if pkt_info is None or not is_ordinary_dir_stat(pkt_info):
        refuse("packet_missing")
        return packet_dir, binding, request, returned

    try:
        binding = _read_strict_file(packet_dir / "binding.json")
        request = _read_strict_file(packet_dir / "request.json")
        loaded_dir, loaded_req = system.ex.load(turn)
        require(
            loaded_dir == packet_dir and loaded_req == request,
            "loaded packet differs from strict request",
        )
    except (OSError, ValueError, KeyError, TypeError) as exc:
        refuse("packet_invalid", str(exc))
        return packet_dir, None, None, None

    try:
        outcome = transport_outcome(system.ex, packet_dir, request)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        refuse("transport_outcome_unreadable", str(exc))
        outcome = None

    if outcome is not None:
        refuse("terminal_without_return", outcome.get("reason"))

    return_path = packet_dir / "return.json"
    ret_info = _lstat_or_none(return_path)
    if ret_info is None or not is_ordinary_file_stat(ret_info):
        refuse("missing_return")
    else:
        try:
            returned = _read_strict_file(return_path)
            system.ex.validate_return(packet_dir, returned)
            require(
                returned.get("packet") == turn
                and returned.get("request_sha256") == binding["request_sha256"],
                "return binding differs from packet request",
            )
        except (OSError, ValueError, KeyError, TypeError) as exc:
            refuse("invalid_return", str(exc))
            returned = None

    return packet_dir, binding, request, returned


def _check_publications(
    turn: str,
    turn_dir: Path,
    packet_dir: Path,
    binding: dict[str, Any] | None,
    request: dict[str, Any] | None,
    returned: dict[str, Any] | None,
    refuse: Callable[..., None],
) -> tuple[dict[str, Any] | None, list[str], list[str]]:
    """Guard (b): Clean preparation state and 100% of declared targets returned and published."""
    if _lstat_or_none(turn_dir / "attention-draft") is not None:
        refuse("uncommitted_attention_draft")

    prep_path = turn_dir / "preparation.json"
    prep_state = None
    if _lstat_or_none(prep_path) is not None:
        try:
            prep_state = _read_strict_file(prep_path)
            phase = prep_state.get("phase")
            if phase != "sealed":
                refuse("incomplete_preparation_phase", phase)
            elif binding is not None and (
                prep_state.get("request_sha256") != binding["request_sha256"]
                or prep_state.get("context_sha256") != binding["context_sha256"]
            ):
                refuse("preparation_binding_mismatch")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            refuse("unreadable_preparation_marker", str(exc))

    declared_targets: list[str] = []
    complete_targets: list[str] = []
    if request is not None and returned is not None:
        declared_targets = [row["path"] for row in request.get("targets", [])]
        proposal_paths = {row["path"] for row in returned.get("proposals", [])}
        unreturned = [t for t in declared_targets if t not in proposal_paths]
        if unreturned:
            refuse("unreturned_targets", unreturned)

        publications, reconciliations, unreadable = publication_inventory(packet_dir)
        if unreadable:
            refuse(
                "unreadable_publication_records",
                {name: str(err) for name, err in sorted(unreadable.items())},
            )
        if reconciliations:
            refuse("reconciled_publication_unestablished", sorted(reconciliations))

        complete_set: set[str] = set()
        bad_pubs: list[str] = []
        for slot_name, (pub_record, _) in sorted(publications.items()):
            target = pub_record.get("path")
            try:
                if not isinstance(target, str) or slot_name != publication_name(target):
                    raise ValueError(f"non-canonical publication slot: {slot_name}")
                require(
                    publication_matches(turn, request, pub_record, returned),
                    f"publication binding mismatch: {slot_name}",
                )
                validate_publication_record(pub_record)
            except (ValueError, KeyError, TypeError) as exc:
                bad_pubs.append(f"{slot_name}: {exc}")
                continue

            if pub_record.get("phase") != "complete":
                bad_pubs.append(f"{target}: phase={pub_record.get('phase')}")
                continue
            complete_set.add(target)

        if bad_pubs:
            refuse("incomplete_or_invalid_publications", bad_pubs)

        unpublished = [t for t in declared_targets if t not in complete_set]
        if unpublished:
            refuse("unpublished_targets", unpublished)
        complete_targets = [t for t in declared_targets if t in complete_set]

    return prep_state, declared_targets, complete_targets


def _check_live_references(
    turn: str,
    refuse: Callable[..., None],
    *,
    record_refs: dict[str, list[dict[str, Any]]],
    record_scan_errors: list[str],
    packet_refs: dict[str, list[dict[str, str]]],
    packet_scan_errors: list[str],
    attention_refs: dict[str, list[dict[str, str]]],
    attention_error: str | None,
    interp_refs: dict[str, list[dict[str, str]]],
    interp_errors: list[str],
    uncommitted_refs: dict[str, list[dict[str, str]]],
    uncommitted_errors: list[str],
) -> None:
    """Guard (c): No live structural reference from records, packets, Attention, or interps."""
    if turn in record_refs:
        refuse("referenced_by_records", record_refs[turn])
    if record_scan_errors:
        refuse("record_scan_uncertain", record_scan_errors)
    if turn in packet_refs:
        refuse("referenced_by_other_packets", packet_refs[turn])
    if packet_scan_errors:
        refuse("cross_packet_scan_uncertain", packet_scan_errors)
    if turn in attention_refs:
        refuse("referenced_by_attention", attention_refs[turn])
    if attention_error is not None:
        refuse("attention_scan_uncertain", attention_error)
    if turn in interp_refs:
        refuse("referenced_by_interpretations", interp_refs[turn])
    if interp_errors:
        refuse("interpretation_scan_uncertain", interp_errors)
    if turn in uncommitted_refs:
        refuse("referenced_by_uncommitted_turns", uncommitted_refs[turn])
    if uncommitted_errors:
        refuse("uncommitted_turn_scan_uncertain", uncommitted_errors)


def _verify_captured_recovery(
    system: Any,
    turn: str,
    binding: dict[str, Any],
    request: dict[str, Any],
    returned: dict[str, Any],
    prep_state: dict[str, Any] | None,
    refuse: Callable[..., None],
) -> None:
    try:
        view_doc = system.view(turn)
        require(isinstance(view_doc, dict), "captured view must be an object")

        recovered = system.recover(turn)
        require(
            recovered.get("request_sha256") == binding["request_sha256"]
            and recovered.get("context_sha256") == binding["context_sha256"],
            "recovered context binding differs",
        )

        explained = system.explain_context(turn)
        require(
            explained.get("request_sha256") == binding["request_sha256"]
            and explained.get("context_sha256") == binding["context_sha256"],
            "explained context binding differs",
        )

        system.ex.objects.read(
            returned["reply_sha256"], retained=True, expected_bytes=returned["reply_bytes"]
        )
        for proposal in returned.get("proposals", []):
            system.ex.objects.read(
                proposal["sha256"], retained=True, expected_bytes=proposal["bytes"]
            )

        discovery_prefix = f"{TURNS_RELATIVE}/{turn}/discovery-summary.json"
        disc_entry = next(
            (r for r in request.get("materials", []) if r.get("path") == discovery_prefix), None
        )
        if disc_entry is not None and disc_entry.get("sha256"):
            disc_summary = strict_json(system.ex.objects.read(disc_entry["sha256"], retained=True))
            if disc_summary.get("report_object"):
                system.ex.objects.read(disc_summary["report_object"], retained=True)

        if isinstance(prep_state, dict) and prep_state.get("feedback_object"):
            system.ex.objects.read(prep_state["feedback_object"], retained=True)

        att_state = system.attention._load()
        fb_id = feedback_id(turn)
        if fb_id in att_state.get("feedbacks", {}):
            system.attention.inspect(fb_id)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        refuse("captured_recovery_failed", str(exc))


def _expected_captured_files(
    system: Any,
    turn: str,
    request: dict[str, Any],
    refuse: Callable[..., None],
) -> tuple[dict[str, str], str | None]:
    turn_prefix = f"{TURNS_RELATIVE}/{turn}/"
    expected_captured: dict[str, str] = {}
    brief_row = request.get("brief")
    rows = (
        ([brief_row] if isinstance(brief_row, dict) else [])
        + list(request.get("methods", []))
        + list(request.get("materials", []))
    )

    for row in rows:
        if not isinstance(row, dict):
            continue
        row_path = row.get("path")
        row_sha = row.get("sha256")
        if (
            isinstance(row_path, str)
            and row_path.startswith(turn_prefix)
            and isinstance(row_sha, str)
        ):
            expected_captured[row_path[len(turn_prefix) :]] = row_sha

    full_view_object = None
    exp_sha = expected_captured.get("exposure.json")
    if exp_sha:
        try:
            exp_doc = strict_json(system.ex.objects.read(exp_sha, retained=True))
            full_view_object = exp_doc.get("full_view_object")
            if isinstance(full_view_object, str):
                expected_captured["view.json"] = full_view_object
        except (OSError, ValueError, KeyError, TypeError) as exc:
            refuse("captured_recovery_failed", f"exposure.json verification failed: {exc}")

    return expected_captured, full_view_object


def _verify_metadata_working_files(
    system: Any,
    turn: str,
    turn_dir: Path,
    binding: dict[str, Any],
    request: dict[str, Any],
    prep_state: dict[str, Any] | None,
    declared_targets: list[str],
    expected_captured: dict[str, str],
    full_view_object: str | None,
    file_hashes: dict[str, str],
    allowed_files: set[str],
    modified_files: list[str],
    uncaptured_entries: list[str],
) -> None:
    if "preparation.json" in file_hashes:
        allowed_files.add("preparation.json")
        if not (
            isinstance(prep_state, dict)
            and prep_state.get("phase") == "sealed"
            and prep_state.get("feedback") == feedback_id(turn)
            and prep_state.get("request_sha256") == binding["request_sha256"]
            and prep_state.get("context_sha256") == binding["context_sha256"]
        ):
            modified_files.append("preparation.json")

    work_request_doc = None
    if "request.json" in file_hashes:
        allowed_files.add("request.json")
        try:
            work_request_doc = _read_strict_file(turn_dir / "request.json")
            require(
                isinstance(work_request_doc, dict)
                and set(work_request_doc) <= _TURN_REQUEST_ALLOWED_FIELDS
                and work_request_doc.get("consumer") == request.get("consumer")
                and work_request_doc.get("targets") == declared_targets
                and work_request_doc.get("returns")
                == [r["origin"]["packet"] for r in request.get("returns", [])]
                and work_request_doc.get("artifacts")
                == [
                    f"{r['origin']['packet']}:{r['origin']['proposal_path']}"
                    for r in request.get("artifacts", [])
                ],
                "working request.json disagrees with packet request",
            )
            brief_sha = expected_captured.get("brief.md")
            if brief_sha and isinstance(work_request_doc.get("task"), str):
                brief_text = system.ex.objects.read(brief_sha, retained=True).decode("utf-8")
                expected_prefix = "# Current receiving task\n\n" + work_request_doc["task"] + "\n"
                require(
                    brief_text.startswith(expected_prefix),
                    "working request.json task disagrees with captured brief.md",
                )
        except OSError, ValueError, KeyError, TypeError, UnicodeDecodeError:
            modified_files.append("request.json")
            work_request_doc = None

    if "selection.json" in file_hashes:
        allowed_files.add("selection.json")
        try:
            sel_doc = _read_strict_file(turn_dir / "selection.json")
            exact_fields(
                sel_doc, {"request", "view", "attention_feedback"}, {"format"}, "selection.json"
            )
            validate_optional_format(sel_doc, TURN_SELECTION_FORMAT, "selection.json")
            require(
                sel_doc["attention_feedback"] == feedback_id(turn),
                "selection.json feedback differs",
            )
            require(
                isinstance(full_view_object, str)
                and sha(encode(sel_doc["view"])) == full_view_object,
                "selection.json view differs from captured full_view_object",
            )
            if work_request_doc is not None:
                require(
                    encode(sel_doc["request"]) == encode(work_request_doc),
                    "selection.json request differs from working request.json",
                )
        except OSError, ValueError, KeyError, TypeError:
            modified_files.append("selection.json")
    elif "request.json" in file_hashes:
        modified_files.append("request.json")

    if "attention-spec.json" in file_hashes:
        if isinstance(work_request_doc, dict) and "attention" in work_request_doc:
            allowed_files.add("attention-spec.json")
            expected_att_sha = sha(encode(work_request_doc["attention"]))
            if file_hashes["attention-spec.json"] != expected_att_sha:
                modified_files.append("attention-spec.json")
            else:
                try:
                    system.ex.objects.read(expected_att_sha, retained=True)
                except OSError, ValueError:
                    modified_files.append("attention-spec.json")
        else:
            uncaptured_entries.append("attention-spec.json")


def _check_working_copy_integrity(
    system: Any,
    turn: str,
    turn_dir: Path,
    binding: dict[str, Any],
    request: dict[str, Any],
    returned: dict[str, Any],
    prep_state: dict[str, Any] | None,
    declared_targets: list[str],
    dir_issues: list[str],
    file_hashes: dict[str, str],
    dir_paths: list[str],
    refuse: Callable[..., None],
) -> None:
    """Guard (d): Verify 100% captured recoverability and working-copy byte correspondence."""
    with system.ex.objects.pack_session():
        _verify_captured_recovery(system, turn, binding, request, returned, prep_state, refuse)
        if dir_issues:
            return

        expected_captured, full_view_object = _expected_captured_files(
            system, turn, request, refuse
        )
        modified_files: list[str] = []
        uncaptured_entries: list[str] = []
        allowed_files: set[str] = set()

        for rel, expected_sha in sorted(expected_captured.items()):
            if rel in file_hashes:
                allowed_files.add(rel)
                if file_hashes[rel] != expected_sha:
                    modified_files.append(rel)

        _verify_metadata_working_files(
            system,
            turn,
            turn_dir,
            binding,
            request,
            prep_state,
            declared_targets,
            expected_captured,
            full_view_object,
            file_hashes,
            allowed_files,
            modified_files,
            uncaptured_entries,
        )

    for rel in sorted(file_hashes):
        if rel not in allowed_files:
            uncaptured_entries.append(rel)

    allowed_dirs: set[str] = set()
    for rel in allowed_files:
        parent = posixpath.dirname(rel)
        while parent:
            allowed_dirs.add(parent)
            parent = posixpath.dirname(parent)

    for rel_dir in sorted(dir_paths):
        if rel_dir not in allowed_dirs:
            uncaptured_entries.append(rel_dir + "/")

    if modified_files:
        refuse("working_dir_modified_after_capture", sorted(set(modified_files)))
    if uncaptured_entries:
        refuse("uncaptured_working_files", sorted(set(uncaptured_entries)))
