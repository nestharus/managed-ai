"""Scope preparation, overlap nomination indexing, and native context construction."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from managed_ai.host.continuing_work.contract import DEFAULT_HOST_CONFIG
from managed_ai.host.continuing_work.occurrence_files import (
    atomic_bytes,
    digest,
    ident,
    nonempty,
    now,
)

HERE = Path(__file__).resolve().parent


def operation_guide_path() -> Path:
    """Locate the continuing-work OPERATION.md method guide."""
    local = HERE / "OPERATION.md"
    if local.is_file():
        return local

    repo_doc = HERE.parents[3] / "docs/engine/host/continuing_work/OPERATION.md"
    if repo_doc.is_file():
        return repo_doc

    raise FileNotFoundError(f"Host operation guide is absent: {repo_doc}")


def prepare_questions(
    supplied_questions: Any,
    selected_sources: Any,
    task: str,
) -> list[dict[str, Any]]:
    """Validate and normalize authored Attention questions before any host/native effects."""
    if not isinstance(selected_sources, list) or any(
        not isinstance(row, dict) for row in selected_sources
    ):
        raise ValueError("sources must be a list of selected resource objects")

    selected_ids = [ident(row["id"]) for row in selected_sources]
    if len(set(selected_ids)) != len(selected_ids):
        raise ValueError("Duplicate selected resource")

    if not isinstance(supplied_questions, list):
        raise ValueError("questions must be a list")

    selected_set = set(selected_ids)
    questions: list[dict[str, Any]] = []
    for idx, item in enumerate(supplied_questions):
        label = f"questions[{idx}]"
        if not isinstance(item, dict):
            raise ValueError(label + " must be an object")

        unknown = set(item) - {"question", "use", "sources"}
        if unknown:
            raise ValueError(
                label
                + " has unsupported fields: "
                + ", ".join(sorted(unknown))
                + "; use sources for selected resource IDs",
            )

        refs = item.get("sources", [])
        if not isinstance(refs, list) or any(not isinstance(ref, str) for ref in refs):
            raise ValueError(label + ".sources must be a list of selected resource IDs")
        if len(set(refs)) != len(refs):
            raise ValueError(label + ".sources contains duplicates")

        missing = set(refs) - selected_set
        if missing:
            raise ValueError(
                label + ".sources names unselected resource IDs: " + ", ".join(sorted(missing)),
            )

        questions.append(
            {
                "question": nonempty(item.get("question"), label + ".question"),
                "use": nonempty(item.get("use", task), label + ".use"),
                "sources": refs,
            },
        )

    return questions


def prepare_attention_selection(spec: dict[str, Any]) -> dict[str, list[str]]:
    """Validate explicit caller-selected Attention item and group lists."""
    limits = (
        ("attention_items", DEFAULT_HOST_CONFIG.max_attention_items),
        ("attention_groups", DEFAULT_HOST_CONFIG.max_attention_groups),
    )
    attention: dict[str, list[str]] = {}

    for key, limit in limits:
        value = spec.get(key, [])
        if (
            not isinstance(value, list)
            or len(value) > limit
            or any(not isinstance(entry, str) for entry in value)
        ):
            raise ValueError(key + " must be a bounded list of strings")
        if len(value) != len(set(value)):
            raise ValueError(key + " contains duplicates")
        attention[key] = value

    return attention


def overlap_rows(
    previous_scopes: list[dict[str, Any]],
    sources: list[dict[str, Any]],
    reads: list[str],
    writes: list[str],
) -> list[dict[str, Any]]:
    """Nominate declared footprints and observed source/output identity without semantic ranking."""
    rows: list[dict[str, Any]] = []
    read_set = set(reads)
    write_set = set(writes)
    current_by_hash: dict[str, list[str]] = {}

    for source in sources:
        current_by_hash.setdefault(source["sha256"], []).append(source["id"])

    for previous in previous_scopes:
        prev_reads = set(previous["reads"])
        prev_writes = set(previous["writes"])
        conflict = sorted((write_set & (prev_reads | prev_writes)) | (prev_writes & read_set))
        shared = sorted(read_set & prev_reads)

        prior_by_hash: dict[str, list[str]] = {}
        for source in previous["sources"]:
            prior_by_hash.setdefault(source["sha256"], []).append(source["id"])

        same = [
            {
                "sha256": key,
                "current_source_ids": current_by_hash[key],
                "prior_source_ids": prior_by_hash[key],
            }
            for key in sorted(current_by_hash.keys() & prior_by_hash.keys())
        ]
        output_uses = [
            {
                "sha256": out["sha256"],
                "current_source_ids": current_by_hash[out["sha256"]],
                "received_output": out["path"],
            }
            for out in previous.get("result", {}).get("outputs", [])
            if out["sha256"] in current_by_hash
        ]
        if not (conflict or shared or same or output_uses):
            continue

        scope_state = (
            "partially-received"
            if previous.get("result", {}).get("partial")
            else "returned"
            if "result" in previous
            else "superseded-without-output"
            if "superseded" in previous
            else "prepared-or-unreconciled"
        )
        rows.append(
            {
                "scope": previous["id"],
                "prepared_at_utc": previous["prepared_at_utc"],
                "read_write": conflict,
                "shared_reads": shared,
                "shared_source_bodies": same,
                "received_output_uses": output_uses,
                "scope_state": scope_state,
                "successor": previous.get("superseded", {}).get("successor"),
                "return_sha256": previous.get("result", {}).get("return", {}).get("return_sha256"),
            },
        )

    rows.sort(
        key=lambda row: (
            bool(row["received_output_uses"]),
            bool(row["read_write"]),
            row["scope_state"] == "prepared-or-unreconciled",
            row["prepared_at_utc"],
            row["scope"],
        ),
        reverse=True,
    )
    return rows


def write_overlap_index(
    host: Any,
    sid: str,
    sources: list[dict[str, Any]],
    resource_changes: list[dict[str, Any]],
    reads: list[str],
    writes: list[str],
    overlap_limit: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """Index prior source changes and write the complete overlap nomination table once."""
    changed = list(resource_changes)
    changed_ids = {entry["resource"] for entry in changed}
    previous_scopes = host.scopes()

    # Pre-sort and pre-index previous scopes once to avoid O(S * P log P) inner sorts.
    newest_first = sorted(previous_scopes, key=lambda item: item["prepared_at_utc"], reverse=True)
    prior_by_path: dict[str, list[dict[str, Any]]] = {}
    for old_scope in newest_first:
        for row in old_scope["sources"]:
            prior_by_path.setdefault(row["path"], []).append(row)

    for source in sources:
        if source["id"] in changed_ids:
            continue
        prior = [
            row
            for row in prior_by_path.get(source["path"], ())
            if row["sha256"] != source["sha256"]
        ]
        if prior:
            changed.append(
                {
                    "resource": source["id"],
                    "before": prior[0],
                    "current": source,
                    "nomination_basis": (
                        "same selected current source path under possibly different resource IDs"
                    ),
                },
            )
            changed_ids.add(source["id"])

    for change in changed:
        target_res = change["resource"]
        target_path = change["current"]["path"]
        target_sha = change["current"]["sha256"]
        change["affected_prior_consumers"] = [
            scope["id"]
            for scope in previous_scopes
            if any(
                (row["id"] == target_res or row["path"] == target_path)
                and row["sha256"] != target_sha
                for row in scope["sources"]
            )
        ]

    all_overlaps = overlap_rows(previous_scopes, sources, reads, writes)
    affected = {name for change in changed for name in change["affected_prior_consumers"]}
    all_overlaps.sort(
        key=lambda row: (bool(row["received_output_uses"]), row["scope"] in affected),
        reverse=True,
    )
    overlaps = all_overlaps[:overlap_limit]

    index = host.root / "overlap-index" / f"{sid}.json"
    host._write(
        index,
        {
            "scope": sid,
            "previous_scope_count": len(previous_scopes),
            "rows": all_overlaps,
            "meaning": (
                "Author-declared footprints plus exact same-source/received-output identity. "
                "Shared reading need not conflict. Disjoint identities do not establish "
                "semantic independence."
            ),
        },
    )
    overlap_pin = host.pin(index)
    coverage = {
        "previous_scopes": len(previous_scopes),
        "matching_scopes": len(all_overlaps),
        "delivered": len(overlaps),
        "omitted": len(all_overlaps) - len(overlaps),
        "limit": overlap_limit,
        "omitted_scope_sample": [row["scope"] for row in all_overlaps[overlap_limit : overlap_limit + 20]],
        "complete_index": overlap_pin,
        "meaning": (
            "Nominations, not inferred conflicts, automatic scheduling or semantic completeness. "
            "Acquire the complete index when omitted or new relationships matter."
        ),
    }
    return changed, overlaps, coverage


def _validate_prepare_spec(host: Any, spec: dict[str, Any]) -> dict[str, Any]:
    """Validate all prepare spec inputs before touching files or invoking the native engine."""
    sid = ident(spec["id"])
    saved = host.root / "scopes" / f"{sid}.json"
    if saved.exists():
        raise ValueError("A scope is one immutable occurrence; select a fresh identity")

    task = nonempty(spec["task"], "task")
    prediction = nonempty(spec["prediction"], "prediction")
    selected_sources = spec.get("sources", [])
    questions = prepare_questions(spec.get("questions", []), selected_sources, task)
    for ordinal, question in enumerate(questions, 1):
        question["id"] = ident(f"{sid}-q{ordinal}")

    resource_paths = [(item, Path(item["path"])) for item in selected_sources]
    attention = prepare_attention_selection(spec)

    overlap_limit = spec.get("overlap_limit", DEFAULT_HOST_CONFIG.default_overlap_limit)
    if type(overlap_limit) is not int or not 1 <= overlap_limit <= DEFAULT_HOST_CONFIG.max_overlap_limit:
        raise ValueError("overlap_limit must be an integer from 1 through 200")

    outnames = [ident(name) for name in spec["outputs"]]
    if not outnames or len(set(outnames)) != len(outnames):
        raise ValueError("Select distinct new output names")

    targets = ["outputs/" + sid + "/" + name for name in outnames]
    reads = list(dict.fromkeys([*spec.get("reads", []), *(row["id"] for row in selected_sources)]))
    writes = list(dict.fromkeys(spec.get("writes", [])))

    duty_ids = spec["duty_ids"]
    requested_duties = set(duty_ids)
    if not requested_duties:
        raise ValueError("Bind actual current parent duties")

    repair_ids = [ident(value) for value in spec.get("repairs", [])]
    return {
        "sid": sid,
        "saved": saved,
        "task": task,
        "prediction": prediction,
        "questions": questions,
        "resource_paths": resource_paths,
        "attention": attention,
        "overlap_limit": overlap_limit,
        "targets": targets,
        "reads": reads,
        "writes": writes,
        "duty_ids": duty_ids,
        "requested_duties": requested_duties,
        "repair_ids": repair_ids,
    }


def _refresh_scope_sources(
    host: Any,
    resource_paths: list[tuple[dict[str, Any], Path]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    sources: list[dict[str, Any]] = []
    resource_changes: list[dict[str, Any]] = []

    for item, path in resource_paths:
        row, old = host.refresh_resource(item["id"], path)
        row = dict(row, role=item.get("role", "source"))
        sources.append(row)
        if old:
            resource_changes.append({"resource": row["id"], "before": old, "current": row})

    return sources, resource_changes


def _track_scope_attention(
    host: Any,
    sid: str,
    task: str,
    questions: list[dict[str, Any]],
    sources: list[dict[str, Any]],
    changed: list[dict[str, Any]],
    note: str,
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for question in questions:
        items.append(
            {
                "id": question["id"],
                "question": question["question"],
                "use": question["use"],
                "basis": [row["work_path"] for row in sources if row["id"] in question["sources"]],
                "groups": [sid],
            },
        )

    if changed:
        items.append(
            {
                "id": ident(sid + "-changed-basis"),
                "question": (
                    "How do these changed sources alter current dependent work or "
                    "previously adequate partials?"
                ),
                "use": task,
                "basis": [note, *[row["current"]["work_path"] for row in changed]],
                "groups": [sid],
            },
        )

    if items:
        host.call("track", "--spec", host.temp(sid + "-attention.json", {"items": items}))

    return items


def _ensure_method_guide(host: Any) -> tuple[Path, str]:
    guide_path = operation_guide_path()
    method_raw = guide_path.read_bytes()
    method_sha = digest(method_raw)
    method_path = "sources/host/method-" + method_sha + ".md"
    method_file = host.work / method_path

    if method_file.exists() and method_file.read_bytes() != method_raw:
        raise ValueError("Retained host method identity differs")
    if not method_file.exists():
        atomic_bytes(method_file, method_raw)

    return guide_path, method_path


def prepare_scope(host: Any, spec: dict[str, Any]) -> dict[str, Any]:
    """Prepare an immutable producing scope with overlap nominations, Attention tracking, and context."""
    parsed = _validate_prepare_spec(host, spec)
    sid = parsed["sid"]
    saved = parsed["saved"]
    targets = parsed["targets"]

    host.current(host.config["commission"], "Current commission")
    goal = host.goal()
    known = {row["id"]: row for row in goal["content"]["obligations"]}
    if parsed["requested_duties"] - known.keys():
        raise ValueError("Bind actual current parent duties")

    sources, resource_changes = _refresh_scope_sources(host, parsed["resource_paths"])
    changed, overlaps, coverage = write_overlap_index(
        host,
        sid,
        sources,
        resource_changes,
        parsed["reads"],
        parsed["writes"],
        parsed["overlap_limit"],
    )

    if any((host.work / target).exists() for target in targets):
        raise ValueError("Fresh generation targets must be absent")
    for target in targets:
        (host.work / target).parent.mkdir(parents=True, exist_ok=True)

    repairs = host.repairs.generation_repairs(parsed["repair_ids"], sources)
    source_note = {
        "scope": sid,
        "prediction": parsed["prediction"],
        "duty_ids": parsed["duty_ids"],
        "changed_sources": changed,
        "overlap_nominations": overlaps,
        "overlap_coverage": coverage,
        "authored_relations": spec.get("relations", []),
        "selected_treatments": spec.get("treatments", []),
        "unknowns": spec.get("unknowns", ["Unnominated semantic dependencies may remain."]),
        "repairs": repairs,
        "receiving_question": spec.get(
            "receiving_question",
            "Did the actual contribution produce the predicted useful effect under current sources?",
        ),
    }
    note = "sources/host/" + sid + "-prospect.json"
    host._write(host.work / note, source_note)

    items = _track_scope_attention(
        host,
        sid,
        parsed["task"],
        parsed["questions"],
        sources,
        changed,
        note,
    )
    guide_path, method_path = _ensure_method_guide(host)

    attention = parsed["attention"]
    request: dict[str, Any] = {
        "task": parsed["task"],
        "consumer": spec.get("consumer", sid),
        "sources": ["sources/host/COMMISSION.md", method_path, note, *[row["work_path"] for row in sources]],
        "targets": targets,
        "attention_items": list(
            dict.fromkeys(
                ["continuing-commission", *[row["id"] for row in items], *attention["attention_items"]],
            ),
        ),
        "attention_groups": attention["attention_groups"],
        "attention_limit": spec.get("attention_limit", max(8, len(items) + 1)),
    }
    if "attention_max_bytes" in spec:
        request["attention_max_bytes"] = spec["attention_max_bytes"]
    for key in ("techniques", "discovery", "compositions"):
        if key in spec:
            request[key] = spec[key]

    result = host.call("context", sid, "--request", host.temp(sid + "-request.json", request))
    recover = host.call("recover", sid)
    context_bytes = recover["context"].encode("utf-8")
    context_sha = digest(context_bytes)
    context_file = host.root / "contexts" / f"{sid}.md"
    atomic_bytes(context_file, context_bytes)

    scope = {
        "format": "g60-continuing-work-scope/1",
        "id": sid,
        "prepared_at_utc": now(),
        "host_producer": host.pin((HERE / "continuing_work.py").resolve()),
        "host_dependencies": host.producer_dependencies(),
        "host_method": host.pin(guide_path),
        "spec": spec,
        "parent_goal": {k: goal[k] for k in ("revision", "sha256")},
        "reads": parsed["reads"],
        "writes": parsed["writes"],
        "sources": sources,
        "targets": targets,
        "repairs": repairs,
        "nominations": source_note,
        "context": str(context_file),
        "native_context": result,
    }
    host._write(saved, scope)
    retained = host.pin(saved)
    host.event("prepared", {"id": sid, "scope": retained, "context_sha256": context_sha})

    return {
        "id": sid,
        "context": str(context_file),
        "selected_source_bytes": sum(row["bytes"] for row in sources),
        "changed_sources": len(changed),
        "overlap_nominations": len(overlaps),
        "targets": targets,
        "parent_owed": [
            row["id"] for row in known.values() if row["disposition"] not in ("fulfilled", "released")
        ],
    }
