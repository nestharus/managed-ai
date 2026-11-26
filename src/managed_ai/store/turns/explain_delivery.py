"""Explain a frozen context from its captured evidence, without reselecting work."""

from typing import Any

from managed_ai.core.exchange import MAX_PACKET_BYTES
from managed_ai.core.formats import CONTEXT_EXPLANATION_FORMAT as FORMAT
from managed_ai.core.formats import INCLUSION_ORIGINS_FORMAT as ORIGINS
from managed_ai.core.formats import (
    PACKET_BINDING_FORMAT,
    TURN_EXPOSURE_FORMAT,
    TURN_VIEW_FORMAT,
    validate_optional_format,
)
from managed_ai.core.storage import encode, read_bytes, read_json, sha, strict_json_loads
from managed_ai.store.turns.view_chunks import (
    hydrate_outside_view_records,
    outside_view_record_count,
    validate_outside_view_chunks_metadata,
)

ROLES = ("methods", "materials", "targets", "returns", "artifacts")


def captured_view(
    exchange: Any, turn: str, request: dict[str, Any], *, hydrate_outside: bool = True
) -> dict[str, Any]:
    """Read only the full view referenced by this already validated packet."""
    prefix = ".system/turns/" + turn + "/"
    with exchange.objects.pack_session():
        exposure = next(
            (r for r in request["materials"] if r["path"] == prefix + "exposure.json"), None
        )
        if exposure is not None:
            binding = strict_json_loads(exchange.objects.read(exposure["sha256"]), "exposure.json")
            validate_optional_format(binding, TURN_EXPOSURE_FORMAT, "exposure.json")
            view = strict_json_loads(
                exchange.objects.read(binding["full_view_object"]), "view.json"
            )
            validate_optional_format(view, TURN_VIEW_FORMAT, "view.json")
            if hydrate_outside:
                return hydrate_outside_view_records(exchange.objects, view)
            validate_outside_view_chunks_metadata(view)
            return view

        original = next(
            (r for r in request["materials"] if r["path"] == prefix + "view.json"), None
        )
        if original is None:
            raise ValueError("no captured system view for this turn")

        view = strict_json_loads(exchange.objects.read(original["sha256"]), "view.json")
        validate_optional_format(view, TURN_VIEW_FORMAT, "view.json")
        if hydrate_outside:
            return hydrate_outside_view_records(exchange.objects, view)
        validate_outside_view_chunks_metadata(view)
        return view


def _parse_captured_origins(
    view: dict[str, Any],
    included: set[tuple[str, str]],
) -> dict[tuple[str, str], list[Any]]:
    origins = view.get("inclusion_origins")
    recorded: dict[tuple[str, str], list[Any]] = {}
    if origins is None:
        return recorded

    if not isinstance(origins, dict) or origins.get("format") != ORIGINS:
        raise ValueError("unsupported captured inclusion-origin format")

    for row in origins["units"]:
        key = (row["role"], row["path"])
        if key not in included or key in recorded or not isinstance(row["reasons"], list):
            raise ValueError("captured inclusion origins do not match the packet units")
        recorded[key] = row["reasons"]

    return recorded


def _verify_packet_entries(
    exchange: Any,
    entries: list[tuple[str, dict[str, Any]]],
    record_paths: dict[str, str],
) -> tuple[dict[str, dict[str, Any]], dict[str, int], dict[str, dict[str, int]]]:
    records: dict[str, dict[str, Any]] = {}
    object_sizes: dict[str, int] = {}
    by_role: dict[str, dict[str, int]] = {}

    for role, row in entries:
        key = row["sha256"]
        body: bytes | None = None
        if key is None:
            if row["bytes"] != 0:
                raise ValueError("absent captured input has a nonzero byte count")
        elif key not in object_sizes:
            body = exchange.objects.read(key)
            object_sizes[key] = len(body)

        if key is not None and object_sizes[key] != row["bytes"]:
            raise ValueError("captured input byte count differs: " + row["path"])

        if role == "materials" and row["path"] in record_paths:
            if body is None:
                body = exchange.objects.read(key)
            record = strict_json_loads(body, row["path"])
            if record.get("id") != record_paths[row["path"]]:
                raise ValueError("captured selected-record identity differs")
            records[record["id"]] = record

        totals = by_role.setdefault(role, {"entries": 0, "bytes": 0})
        totals["entries"] += 1
        totals["bytes"] += row["bytes"]

    return records, object_sizes, by_role


def _build_explanation_units(
    turn: str,
    entries: list[tuple[str, dict[str, Any]]],
    view: dict[str, Any],
    recorded: dict[tuple[str, str], list[Any]],
    record_paths: dict[str, str],
    records: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    basis_paths: dict[str, list[dict[str, Any]]] = {}
    for ident, record in records.items():
        for basis in record["basis"]:
            basis_paths.setdefault(basis["path"], []).append(
                {
                    "record": ident,
                    "revision": record["revision"],
                    "expected": {k: basis.get(k) for k in ("sha256", "bytes")},
                }
            )

    prefix = ".system/turns/" + turn + "/"
    technique_paths = {
        prefix + "knowledge/" + sha(entry["id"].encode()) + ".txt": entry
        for entry in view["techniques"]
    }

    units: list[dict[str, Any]] = []
    for role, row in entries:
        reasons = recorded.get((role, row["path"]))
        correspondences: list[dict[str, Any]] = []
        unit: dict[str, Any] = {
            "role": role,
            "path": row["path"],
            "sha256": row["sha256"],
            "bytes": row["bytes"],
            "origin_status": "recorded" if reasons is not None else "not-recorded",
            "inclusion_reasons": reasons or [],
            "captured_correspondences": correspondences,
        }
        if "origin" in row:
            unit["prior_occurrence"] = row["origin"]

        if role == "materials":
            if row["path"] in record_paths:
                ident = record_paths[row["path"]]
                record = records[ident]
                correspondences.append(
                    {
                        "kind": "selected-record",
                        "record": ident,
                        "revision": record["revision"],
                        "governing_in_captured_view": ident in view["governing_records"],
                    }
                )

            for correspondence in basis_paths.get(row["path"], []):
                captured = {k: row[k] for k in ("sha256", "bytes")}
                correspondences.append(
                    {
                        "kind": "selected-record-basis-path",
                        **correspondence,
                        "captured": captured,
                        "same_bytes": correspondence["expected"] == captured,
                        "scope": (
                            "Path correspondence and exact byte comparison, "
                            "not basis validity or semantic need."
                        ),
                    }
                )

            entry = technique_paths.get(row["path"])
            if (
                entry is not None
                and row["sha256"] == entry["sha256"]
                and row["bytes"] == entry["bytes"]
            ):
                correspondences.append({"kind": "selected-technique", "source": entry["id"]})

        units.append(unit)

    return units


def explain_context(exchange: Any, turn: str) -> dict[str, Any]:
    with exchange.objects.pack_session():
        directory, request = exchange.load(turn)
        view = captured_view(exchange, turn, request, hydrate_outside=False)
        binding = read_json(directory / "binding.json")
        validate_optional_format(binding, PACKET_BINDING_FORMAT, "binding.json")

        carrier = read_bytes(directory / "context.md", MAX_PACKET_BYTES)
        if (
            sha(carrier) != binding["context_sha256"]
            or sha(encode(request)) != binding["request_sha256"]
        ):
            raise ValueError("frozen request/context changed")

        entries = [("brief", request["brief"])]
        entries.extend((role, row) for role in ROLES for row in request.get(role, []))
        included = {(role, row["path"]) for role, row in entries}
        recorded = _parse_captured_origins(view, included)

        # Recover record/basis correspondence strictly from selected immutable objects.
        record_paths = {"records/" + ident + ".json": ident for ident in view["selected_records"]}
        records, object_sizes, by_role = _verify_packet_entries(exchange, entries, record_paths)
        units = _build_explanation_units(turn, entries, view, recorded, record_paths, records)

    nonempty = sum(row["sha256"] is not None for _, row in entries)
    return {
        "format": FORMAT,
        "packet": turn,
        "consumer": request["consumer"],
        "request_sha256": binding["request_sha256"],
        "context_sha256": binding["context_sha256"],
        "totals": {
            "selected_body_bytes": sum(row["bytes"] for _, row in entries),
            "rendered_context_bytes": len(carrier),
            "entries": len(entries),
            "by_role": by_role,
            "distinct_selected_objects": len(object_sizes),
            "distinct_selected_object_bytes": sum(object_sizes.values()),
            "repeated_object_entries": nonempty - len(object_sizes),
        },
        "units": units,
        "captured_selection": {
            k: view.get(k)
            for k in (
                "governing_records",
                "omitted_records",
                "outside_record_basis",
                "unavailable_record_basis",
                "attention_selection",
                "attention_delivered",
                "attention_omitted",
                "attention_outside_view",
            )
        },
        "outside_counts": {
            "outside_view_records": outside_view_record_count(view),
            "outside_techniques": len(view.get("outside_techniques", [])),
            "outside_project_sources": len(view.get("outside_project_sources", [])),
        },
        "provenance_scope": (
            "Recorded reasons come from actual capture additions. Older or unrecorded origins "
            "remain unknown; a captured correspondence does not establish which rule caused "
            "inclusion. No live preparation request, selection file, current head or current "
            "source body is used."
        ),
        "adjustment": (
            "Inspect recover TURN --text for complete bodies and view TURN for the complete "
            "captured inventory. A different selection requires a fresh receiving turn. An "
            "unnecessary ordinary record can be omitted and a needed source selected directly "
            "when adequate; governing records and applicable duty basis remain. Domain entries "
            "belong to the selected domain contract, not a per-turn size override. returns "
            "selects an earlier reply; artifacts selects a retained proposed body. Neither "
            "transfers authority."
        ),
        "scope": (
            "Exact captured delivery, not reading, source applicability or alignment. Body "
            "totals count each actual role entry; shared reasons do not multiply an entry. "
            "Distinct-object totals describe only selected retained bodies, not total capture "
            "storage or verification cost. Integrity checks read retained objects and the "
            "carrier; no elapsed-time, token or semantic-efficacy measurement is supplied."
        ),
    }
