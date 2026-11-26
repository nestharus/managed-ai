"""Structural graph integrity linting over verified HeadIndex v3 mmap or node maps."""

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from managed_ai_information.core.formats import GRAPH_LINT_FORMAT
from managed_ai_information.store.records.head_index_bin import (
    MAP_FLAG_HAS_CONTENT_LINKS,
    MAP_FLAG_HAS_DUP_RELS,
    MAP_FLAG_OPEN_QUESTION,
    HeadIndexView,
)

_RECORD_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,120}$")
_PROVENANCE_RELATION_LABELS = frozenset(
    {
        "corresponds to dated origin",
        "grounded in original edition",
        "declared by original source",
        "revises native navigation from",
    }
)
_DEFAULT_PROVENANCE_SELF_LOOPS = frozenset({"revises native navigation from"})
_ANSWER_RELATION_LABELS = frozenset({"answers", "settles", "answered-by", "settled-by"})
_OPEN_QUESTION_STATES = frozenset({"", "open", "qualified_unissued"})


@dataclass(slots=True)
class _MmapLintCtx:
    view: HeadIndexView
    cold_ids: set[str]
    rel_labels: tuple[str, ...] | list[str]
    has_vocab: bool
    is_prov: list[bool]
    is_self_ok: list[bool]
    is_decl: list[bool]
    is_ans_in: list[bool]
    is_ans_out: list[bool]
    findings: dict[str, list[dict[str, Any]]]
    question_groups: dict[str, list[str]]


def _empty_findings() -> dict[str, list[dict[str, Any]]]:
    return {
        "dangling_record_targets": [],
        "non_id_content_refs": [],
        "non_provenance_self_loops": [],
        "duplicate_edges": [],
        "content_relation_drift": [],
        "answered_open_questions": [],
        "uncontrolled_predicates": [],
    }


def _extract_vocabulary(system: Any) -> tuple[set[str], frozenset[str] | set[str]]:
    vocab = getattr(system.domain_selection, "relation_vocabulary", {}) or {}
    declared = set(vocab.get("predicates", ()))
    self_loops = _DEFAULT_PROVENANCE_SELF_LOOPS | set(
        vocab.get("provenance_self_loop_relations", ())
    )
    return declared, self_loops


def _format_lint_report(
    system: Any,
    *,
    indexed: bool,
    records_examined: int,
    external_non_record_targets: int,
    has_vocab: bool,
    findings: dict[str, list[dict[str, Any]]],
    question_groups: dict[str, list[str]],
) -> dict[str, Any]:
    findings["duplicate_open_questions"] = [
        {"question": qnorm[:200], "ids": sorted(ids)}
        for qnorm, ids in sorted(question_groups.items())
        if len(ids) > 1
    ]
    return {
        "format": GRAPH_LINT_FORMAT,
        "instance_id": system.instance_id,
        "indexed": indexed,
        "records_examined": records_examined,
        "external_non_record_targets": external_non_record_targets,
        "relation_vocabulary_declared": has_vocab,
        "summary": {name: len(rows) for name, rows in findings.items()},
        **findings,
    }


def _is_external_non_record_target(label: str, target: str) -> bool:
    return (
        label in _PROVENANCE_RELATION_LABELS
        or "/" in target
        or "." in target
        or ":" in target
        or _RECORD_ID_RE.fullmatch(target) is None
    )


def _lint_node_relations(
    ident: str,
    graph: dict[str, Any],
    all_ids: set[str],
    self_loop_whitelist: set[str] | frozenset[str],
    declared_predicates: set[str],
    findings: dict[str, list[dict[str, Any]]],
) -> tuple[set[str], int]:
    rel_list = graph.get("relations", ())
    rel_targets = {target for _, target in rel_list}
    external_targets = 0
    for dup_label, dup_target in graph.get("duplicate_relations", ()):
        findings["duplicate_edges"].append(
            {"id": ident, "relation": dup_label, "target": dup_target}
        )
    for label, target in rel_list:
        if target == ident:
            if label not in self_loop_whitelist:
                findings["non_provenance_self_loops"].append({"id": ident, "relation": label})
        elif target not in all_ids:
            if _is_external_non_record_target(label, target):
                external_targets += 1
            else:
                findings["dangling_record_targets"].append(
                    {"id": ident, "relation": label, "target": target}
                )
        elif (
            declared_predicates
            and label not in declared_predicates
            and label not in _PROVENANCE_RELATION_LABELS
        ):
            findings["uncontrolled_predicates"].append(
                {"id": ident, "relation": label, "target": target}
            )
    return rel_targets, external_targets


def _lint_node_content_links(
    ident: str,
    graph: dict[str, Any],
    all_ids: set[str],
    rel_targets: set[str],
    findings: dict[str, list[dict[str, Any]]],
) -> None:
    content_links = graph.get("content_links")
    if not content_links:
        return
    for field_name, values in sorted(content_links.items()):
        for val in values:
            if not _RECORD_ID_RE.fullmatch(val):
                findings["non_id_content_refs"].append(
                    {"id": ident, "field": field_name, "value": val[:160]}
                )
            elif val in all_ids and val != ident and val not in rel_targets:
                findings["content_relation_drift"].append(
                    {"id": ident, "field": field_name, "target": val}
                )


def _lint_open_question(
    ident: str,
    graph: dict[str, Any],
    incoming: dict[str, list[tuple[str, str]]],
    question_groups: dict[str, list[str]],
    answered_open_questions: list[dict[str, Any]],
) -> None:
    qnorm = graph.get("question_norm")
    if qnorm:
        question_groups.setdefault(qnorm, []).append(ident)
    answer_edges: list[dict[str, str]] = [
        {"from": src, "relation": label}
        for label, src in incoming.get(ident, ())
        if label in _ANSWER_RELATION_LABELS
    ]
    out_answers = sorted(
        (label, dst)
        for label, dst in graph.get("relations", ())
        if label in ("answered-by", "settled-by")
    )
    answer_edges.extend({"to": dst, "relation": label} for label, dst in out_answers)
    if answer_edges:
        answered_open_questions.append(
            {
                "id": ident,
                "selection_state": graph.get("selection_state", ""),
                "answer_edges": answer_edges,
            }
        )


def _lint_mmap_external_edge(
    ctx: _MmapLintCtx, slot: int, ident: str | None, rid: int, t_off: int, t_len: int
) -> tuple[str | None, int]:
    """Classify an edge whose target is absent from the hot mmap slot table."""
    target = ctx.view._str(t_off, t_len)
    if target in ctx.cold_ids:
        if ctx.has_vocab and not ctx.is_decl[rid] and not ctx.is_prov[rid]:
            ident = ident or ctx.view.slot_id(slot)
            ctx.findings["uncontrolled_predicates"].append(
                {"id": ident, "relation": ctx.rel_labels[rid], "target": target}
            )
        return ident, 0

    if ctx.is_prov[rid] or "/" in target or "." in target or ":" in target:
        return ident, 1
    if _RECORD_ID_RE.fullmatch(target):
        ident = ident or ctx.view.slot_id(slot)
        ctx.findings["dangling_record_targets"].append(
            {"id": ident, "relation": ctx.rel_labels[rid], "target": target}
        )
        return ident, 0
    return ident, 1


def _lint_mmap_slot_edges(
    ctx: _MmapLintCtx,
    slot: int,
    ident: str | None,
    fwd_raw: list[tuple[int, int, int, int]],
    rel_targets: set[str] | None,
) -> tuple[str | None, int]:
    """Scan all forward edges of one mmap slot, populating anomaly lists on cold paths."""
    ext_targets = 0
    view = ctx.view
    for rid, t_slot, t_off, t_len in fwd_raw:
        if rel_targets is not None:
            rel_targets.add(view._str(t_off, t_len))
        if t_slot == slot:
            if not ctx.is_self_ok[rid]:
                ident = ident or view.slot_id(slot)
                ctx.findings["non_provenance_self_loops"].append(
                    {"id": ident, "relation": ctx.rel_labels[rid]}
                )
            continue
        if t_slot >= 0:
            if ctx.has_vocab and not ctx.is_decl[rid] and not ctx.is_prov[rid]:
                ident = ident or view.slot_id(slot)
                ctx.findings["uncontrolled_predicates"].append(
                    {
                        "id": ident,
                        "relation": ctx.rel_labels[rid],
                        "target": view._str(t_off, t_len),
                    }
                )
            continue
        ident, inc = _lint_mmap_external_edge(ctx, slot, ident, rid, t_off, t_len)
        ext_targets += inc
    return ident, ext_targets


def _lint_mmap_content_links(
    ctx: _MmapLintCtx, ident: str, aux: dict[str, Any], rel_targets: set[str]
) -> None:
    """Validate content-link references for one mmap slot that carries content links."""
    for field_name, values in sorted(aux["content_links"].items()):
        for val in values:
            if not _RECORD_ID_RE.fullmatch(val):
                ctx.findings["non_id_content_refs"].append(
                    {"id": ident, "field": field_name, "value": val[:160]}
                )
            elif (
                val != ident
                and val not in rel_targets
                and (val in ctx.cold_ids or ctx.view.slot_for_id(val) >= 0)
            ):
                ctx.findings["content_relation_drift"].append(
                    {"id": ident, "field": field_name, "target": val}
                )


def _lint_mmap_open_question(
    ctx: _MmapLintCtx,
    slot: int,
    ident: str,
    aux: dict[str, Any],
    fwd_raw: list[tuple[int, int, int, int]],
) -> None:
    """Check duplicate-question grouping and answer edges for one open-question mmap slot."""
    tup = ctx.view.slot_tuple(slot)
    qnorm = aux["question_norm"]
    if qnorm:
        ctx.question_groups.setdefault(qnorm, []).append(ident)

    answer_edges: list[dict[str, str]] = [
        {"from": ctx.view.slot_id(src_slot), "relation": ctx.rel_labels[rid]}
        for rid, src_slot in ctx.view.rev_edges_raw(slot)
        if ctx.is_ans_in[rid]
    ]
    out_answers = sorted(
        (ctx.rel_labels[rid], ctx.view._str(t_off, t_len))
        for rid, _t_slot, t_off, t_len in fwd_raw
        if ctx.is_ans_out[rid]
    )
    answer_edges.extend({"to": dst, "relation": label} for label, dst in out_answers)
    if answer_edges:
        ctx.findings["answered_open_questions"].append(
            {
                "id": ident,
                "selection_state": ctx.view._str(tup[4], tup[5]),
                "answer_edges": answer_edges,
            }
        )


def _lint_mmap(system: Any, view: HeadIndexView, cold_ids: set[str]) -> dict[str, Any]:
    """Zero-allocation structural lint directly over an exact HeadIndex v3 mmap view."""
    declared_predicates, self_loop_whitelist = _extract_vocabulary(system)
    rel_labels = view.rel_labels()
    ctx = _MmapLintCtx(
        view=view,
        cold_ids=cold_ids,
        rel_labels=rel_labels,
        has_vocab=bool(declared_predicates),
        is_prov=[label in _PROVENANCE_RELATION_LABELS for label in rel_labels],
        is_self_ok=[label in self_loop_whitelist for label in rel_labels],
        is_decl=[label in declared_predicates for label in rel_labels],
        is_ans_in=[label in _ANSWER_RELATION_LABELS for label in rel_labels],
        is_ans_out=[label in ("answered-by", "settled-by") for label in rel_labels],
        findings=_empty_findings(),
        question_groups={},
    )
    external_non_record_targets = 0

    for slot in range(view.record_count):
        mflags = view.slot_map_flags(slot)
        has_clinks = bool(mflags & MAP_FLAG_HAS_CONTENT_LINKS)
        ident: str | None = None
        aux: dict[str, Any] | None = None
        fwd_raw = view.fwd_edges_raw(slot)
        rel_targets: set[str] | None = set() if has_clinks else None

        if mflags & MAP_FLAG_HAS_DUP_RELS:
            ident = view.slot_id(slot)
            aux = view.slot_graph_aux(slot)
            for dup_label, dup_target in aux["duplicate_relations"] or ():
                ctx.findings["duplicate_edges"].append(
                    {"id": ident, "relation": dup_label, "target": dup_target}
                )
        if fwd_raw:
            ident, ext_inc = _lint_mmap_slot_edges(ctx, slot, ident, fwd_raw, rel_targets)
            external_non_record_targets += ext_inc
        if has_clinks and rel_targets is not None:
            ident = ident or view.slot_id(slot)
            aux = aux or view.slot_graph_aux(slot)
            _lint_mmap_content_links(ctx, ident, aux, rel_targets)
        if mflags & MAP_FLAG_OPEN_QUESTION:
            ident = ident or view.slot_id(slot)
            aux = aux or view.slot_graph_aux(slot)
            _lint_mmap_open_question(ctx, slot, ident, aux, fwd_raw)

    return _format_lint_report(
        system,
        indexed=True,
        records_examined=view.record_count,
        external_non_record_targets=external_non_record_targets,
        has_vocab=ctx.has_vocab,
        findings=ctx.findings,
        question_groups=ctx.question_groups,
    )


def lint_nodes(
    system: Any,
    nodes: dict[str, dict[str, Any]],
    all_ids: set[str],
    *,
    indexed: bool,
    incoming_builder: Callable[..., dict[str, list[tuple[str, str]]]],
) -> dict[str, Any]:
    """Run structural graph lint over a materialized node summary dictionary."""
    open_question_ids = {
        ident
        for ident, node in nodes.items()
        if node["kind"] == "question"
        and node["active"]
        and node["graph"].get("selection_state", "") in _OPEN_QUESTION_STATES
    }
    incoming = incoming_builder(nodes, targets=open_question_ids) if open_question_ids else {}
    declared_predicates, self_loop_whitelist = _extract_vocabulary(system)
    findings = _empty_findings()
    question_groups: dict[str, list[str]] = {}
    external_non_record_targets = 0

    for ident in sorted(nodes):
        graph = nodes[ident]["graph"]
        rel_targets, ext_count = _lint_node_relations(
            ident, graph, all_ids, self_loop_whitelist, declared_predicates, findings
        )
        external_non_record_targets += ext_count
        _lint_node_content_links(ident, graph, all_ids, rel_targets, findings)
        if ident in open_question_ids:
            _lint_open_question(
                ident, graph, incoming, question_groups, findings["answered_open_questions"]
            )

    return _format_lint_report(
        system,
        indexed=indexed,
        records_examined=len(nodes),
        external_non_record_targets=external_non_record_targets,
        has_vocab=bool(declared_predicates),
        findings=findings,
        question_groups=question_groups,
    )
