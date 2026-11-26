"""Domain-neutral read-only graph query and integrity linter backed by verified HeadIndex."""
from __future__ import annotations

from collections import deque
import re

from managed_ai.core.formats import (
    GRAPH_LINKS_FORMAT,
    GRAPH_LINT_FORMAT,
    GRAPH_NEIGHBORS_FORMAT,
    GRAPH_PATH_FORMAT,
    HEAD_INDEX_FORMAT,
)
from managed_ai.store.protocol.identities import require
from managed_ai.store.protocol.inputs import integer, strings, text
from managed_ai.store.records.head_index import extract_graph_hints

_RECORD_ID_RE = re.compile(r'^[A-Za-z0-9_-]{1,120}$')
_PROVENANCE_RELATION_LABELS = frozenset({
    'corresponds to dated origin',
    'grounded in original edition',
    'declared by original source',
    'revises native navigation from',
})
_DEFAULT_PROVENANCE_SELF_LOOPS = frozenset({'revises native navigation from'})
_ANSWER_RELATION_LABELS = frozenset({'answers', 'settles', 'answered-by', 'settled-by'})
_OPEN_QUESTION_STATES = frozenset({'', 'open', 'qualified_unissued'})


def _load_graph_nodes(system) -> tuple[dict[str, dict], set[str], bool]:
    """Load verified graph metadata from HeadIndex v2 or fall back to scan_records()."""
    store = system.record_store
    cold_ids = set(store.cold_archive.ids())
    loaded = store.indexed_summaries_with_format()
    if loaded is not None:
        summaries, index_format = loaded
        if index_format == HEAD_INDEX_FORMAT:
            return summaries, set(summaries.keys()) | cold_ids, True

    nodes = {}
    for rec in store.scan_records():
        ident = rec['id']
        nodes[ident] = {
            'id': ident,
            'kind': rec['kind'],
            'revision': rec['revision'],
            'sha256': rec['sha256'],
            'active': rec['active'],
            'graph': extract_graph_hints(rec),
        }
    return nodes, set(nodes.keys()) | cold_ids, False


def _node_pin(node: dict) -> dict:
    return {
        'id': node['id'],
        'kind': node['kind'],
        'revision': node['revision'],
        'sha256': node['sha256'],
        'active': node['active'],
    }


def _node_relations(node: dict) -> list:
    return node['graph'].get('relations', ())


def _build_outgoing(nodes: dict[str, dict]) -> dict[str, list[tuple[str, str]]]:
    outgoing: dict[str, list[tuple[str, str]]] = {}
    for src, node in nodes.items():
        rels = [(label, target) for label, target in _node_relations(node)]
        if len(rels) > 1:
            rels.sort()
        outgoing[src] = rels
    return outgoing


def _build_incoming(
    nodes: dict[str, dict],
    *,
    targets: set[str] | None = None,
) -> dict[str, list[tuple[str, str]]]:
    incoming: dict[str, list[tuple[str, str]]] = {}
    for src, node in nodes.items():
        for label, target in _node_relations(node):
            if targets is None or target in targets:
                incoming.setdefault(target, []).append((label, src))

    for edges in incoming.values():
        if len(edges) > 1:
            edges.sort()
    return incoming


def _collect_neighbor_steps(
    cur: str,
    direction: str,
    nodes: dict[str, dict],
    outgoing_cache: dict[str, list[tuple[str, str]]],
    incoming: dict[str, list[tuple[str, str]]],
) -> list[tuple[str, str, str, str]]:
    step_edges: list[tuple[str, str, str, str]] = []

    if direction in ('out', 'both'):
        cached = outgoing_cache.get(cur)
        if cached is None:
            node = nodes.get(cur)
            cached = [(label, target) for label, target in _node_relations(node)] if node is not None else []
            if len(cached) > 1:
                cached.sort()
            outgoing_cache[cur] = cached
        for label, target in cached:
            step_edges.append((cur, target, label, 'out'))

    if direction in ('in', 'both'):
        for label, src in incoming.get(cur, ()):
            step_edges.append((src, cur, label, 'in'))

    return step_edges


def neighbors(
    system,
    record_id: str,
    *,
    direction: str = 'both',
    hops: int = 1,
    relations: list[str] | tuple[str, ...] = (),
    active_only: bool = False,
    records_only: bool = False,
) -> dict:
    """Return bounded BFS neighborhood around one record ID."""
    text(record_id, 'record_id')
    require(direction in ('in', 'out', 'both'), 'direction must be in, out or both')
    integer(hops, 'hops', 1)
    require(hops <= 4, 'hops must be between 1 and 4')
    rel_filter = set(strings(list(relations), 'relations')) if relations else None

    nodes, all_ids, indexed = _load_graph_nodes(system)
    require(record_id in nodes, 'record not found in hot graph: ' + record_id)

    outgoing_cache: dict[str, list[tuple[str, str]]] = {}
    incoming = _build_incoming(nodes) if direction in ('in', 'both') else {}

    visited = {record_id: 0}
    queue = deque([(record_id, 0)])
    discovered_nodes: list[dict] = []
    traversed_edges: list[dict] = []
    seen_edges: set[tuple[str, str, str, str]] = set()

    while queue:
        cur, depth = queue.popleft()
        if depth >= hops:
            continue

        step_edges = _collect_neighbor_steps(cur, direction, nodes, outgoing_cache, incoming)
        for src, dst, label, edge_dir in step_edges:
            if rel_filter is not None and label not in rel_filter:
                continue
            other = dst if edge_dir == 'out' else src
            other_node = nodes.get(other)
            if records_only and other not in all_ids:
                continue
            if active_only and (other_node is None or not other_node['active']):
                continue

            ekey = (src, dst, label, edge_dir)
            if ekey not in seen_edges:
                seen_edges.add(ekey)
                traversed_edges.append({
                    'from': src,
                    'to': dst,
                    'relation': label,
                    'direction': edge_dir,
                    'depth': depth + 1,
                })

            if other_node is not None and other not in visited:
                visited[other] = depth + 1
                discovered_nodes.append(dict(_node_pin(other_node), depth=depth + 1))
                queue.append((other, depth + 1))

    discovered_nodes.sort(key=lambda r: (r['depth'], r['id']))
    return {
        'format': GRAPH_NEIGHBORS_FORMAT,
        'instance_id': system.instance_id,
        'origin': _node_pin(nodes[record_id]),
        'direction': direction,
        'hops': hops,
        'active_only': active_only,
        'records_only': records_only,
        'indexed': indexed,
        'neighbor_count': len(discovered_nodes),
        'edge_count': len(traversed_edges),
        'neighbors': discovered_nodes,
        'edges': traversed_edges,
    }


def links(
    system,
    *,
    target: str,
    relations: list[str] | tuple[str, ...] = (),
    active_only: bool = False,
) -> dict:
    """Indexed reverse-edge lookup returning all inbound edges pointing to target."""
    text(target, 'target')
    rel_filter = set(strings(list(relations), 'relations')) if relations else None

    nodes, _, indexed = _load_graph_nodes(system)
    matched_edges: list[tuple[str, str]] = []

    for src, node in nodes.items():
        if active_only and not node['active']:
            continue
        for label, dst in _node_relations(node):
            if dst != target:
                continue
            if rel_filter is not None and label not in rel_filter:
                continue
            matched_edges.append((label, src))

    matched_edges.sort()
    rows = [
        {'from': _node_pin(nodes[src]), 'relation': label, 'to': target}
        for label, src in matched_edges
    ]
    return {
        'format': GRAPH_LINKS_FORMAT,
        'instance_id': system.instance_id,
        'target': target,
        'target_head': _node_pin(nodes[target]) if target in nodes else None,
        'active_only': active_only,
        'indexed': indexed,
        'matched': len(rows),
        'links': rows,
    }


def _path_response(
    system,
    src: str,
    dst: str,
    *,
    max_hops: int,
    active_only: bool,
    indexed: bool,
    found: bool,
    hops: int | None,
    path_nodes: list[dict],
    path_edges: list[dict],
) -> dict:
    return {
        'format': GRAPH_PATH_FORMAT,
        'instance_id': system.instance_id,
        'source': src,
        'target': dst,
        'max_hops': max_hops,
        'active_only': active_only,
        'indexed': indexed,
        'found': found,
        'hops': hops,
        'nodes': path_nodes,
        'edges': path_edges,
    }


def _bfs_shortest_path(
    nodes: dict[str, dict],
    src: str,
    dst: str,
    *,
    max_hops: int,
    active_only: bool,
    rel_filter: set[str] | None,
) -> tuple[bool, dict[str, tuple[str, str]]]:
    parent: dict[str, tuple[str, str]] = {}
    depth_map: dict[str, int] = {src: 0}
    queue = deque([src])

    while queue:
        cur = queue.popleft()
        cur_depth = depth_map[cur]
        if cur_depth >= max_hops:
            continue

        cur_edges = [(label, nxt) for label, nxt in _node_relations(nodes[cur])]
        if len(cur_edges) > 1:
            cur_edges.sort()

        for label, nxt in cur_edges:
            if rel_filter is not None and label not in rel_filter:
                continue
            nxt_node = nodes.get(nxt)
            if nxt_node is None or (active_only and not nxt_node['active']) or nxt in depth_map:
                continue

            depth_map[nxt] = cur_depth + 1
            parent[nxt] = (cur, label)
            if nxt == dst:
                return True, parent
            queue.append(nxt)

    return False, parent


def shortest_path(
    system,
    src: str,
    dst: str,
    *,
    max_hops: int = 6,
    active_only: bool = False,
    relations: list[str] | tuple[str, ...] = (),
) -> dict:
    """Deterministic shortest directed record-to-record path via BFS."""
    text(src, 'src')
    text(dst, 'dst')
    integer(max_hops, 'max_hops', 1)
    require(max_hops <= 6, 'max_hops must be between 1 and 6')
    rel_filter = set(strings(list(relations), 'relations')) if relations else None

    nodes, _, indexed = _load_graph_nodes(system)
    require(src in nodes, 'source record not found: ' + src)
    require(dst in nodes, 'destination record not found: ' + dst)

    if active_only and (not nodes[src]['active'] or not nodes[dst]['active']):
        return _path_response(
            system, src, dst, max_hops=max_hops, active_only=active_only,
            indexed=indexed, found=False, hops=None, path_nodes=[], path_edges=[],
        )

    if src == dst:
        return _path_response(
            system, src, dst, max_hops=max_hops, active_only=active_only,
            indexed=indexed, found=True, hops=0, path_nodes=[_node_pin(nodes[src])], path_edges=[],
        )

    found, parent = _bfs_shortest_path(
        nodes, src, dst, max_hops=max_hops, active_only=active_only, rel_filter=rel_filter,
    )
    if not found:
        return _path_response(
            system, src, dst, max_hops=max_hops, active_only=active_only,
            indexed=indexed, found=False, hops=None, path_nodes=[], path_edges=[],
        )

    path_ids: list[str] = []
    path_edges: list[dict] = []
    cur = dst
    while cur != src:
        path_ids.append(cur)
        prev, label = parent[cur]
        path_edges.append({'from': prev, 'to': cur, 'relation': label})
        cur = prev

    path_ids.append(src)
    path_ids.reverse()
    path_edges.reverse()
    return _path_response(
        system, src, dst, max_hops=max_hops, active_only=active_only,
        indexed=indexed, found=True, hops=len(path_edges),
        path_nodes=[_node_pin(nodes[i]) for i in path_ids], path_edges=path_edges,
    )


def _lint_node_relations(
    ident: str,
    graph: dict,
    all_ids: set[str],
    self_loop_whitelist: set[str],
    declared_predicates: set[str],
    findings: dict[str, list[dict]],
) -> tuple[set[str], int]:
    rel_list = graph.get('relations', ())
    rel_targets = {target for _, target in rel_list}
    external_targets = 0

    for dup_label, dup_target in graph.get('duplicate_relations', ()):
        findings['duplicate_edges'].append({'id': ident, 'relation': dup_label, 'target': dup_target})

    for label, target in rel_list:
        if target == ident:
            if label not in self_loop_whitelist:
                findings['non_provenance_self_loops'].append({'id': ident, 'relation': label})
        elif target not in all_ids:
            if label in _PROVENANCE_RELATION_LABELS or '/' in target or '.' in target or ':' in target:
                external_targets += 1
            elif _RECORD_ID_RE.fullmatch(target):
                findings['dangling_record_targets'].append({'id': ident, 'relation': label, 'target': target})
            else:
                external_targets += 1
        elif declared_predicates and label not in declared_predicates and label not in _PROVENANCE_RELATION_LABELS:
            findings['uncontrolled_predicates'].append({'id': ident, 'relation': label, 'target': target})

    return rel_targets, external_targets


def _lint_node_content_links(
    ident: str,
    graph: dict,
    all_ids: set[str],
    rel_targets: set[str],
    findings: dict[str, list[dict]],
) -> None:
    content_links = graph.get('content_links')
    if not content_links:
        return

    for field_name, values in sorted(content_links.items()):
        for val in values:
            if not _RECORD_ID_RE.fullmatch(val):
                findings['non_id_content_refs'].append({'id': ident, 'field': field_name, 'value': val[:160]})
            elif val in all_ids and val != ident and val not in rel_targets:
                findings['content_relation_drift'].append({'id': ident, 'field': field_name, 'target': val})


def _lint_open_question(
    ident: str,
    graph: dict,
    incoming: dict[str, list[tuple[str, str]]],
    question_groups: dict[str, list[str]],
    answered_open_questions: list[dict],
) -> None:
    state = graph.get('selection_state', '')
    qnorm = graph.get('question_norm')
    if qnorm:
        question_groups.setdefault(qnorm, []).append(ident)

    answer_edges = [
        {'from': src, 'relation': label}
        for label, src in incoming.get(ident, ())
        if label in _ANSWER_RELATION_LABELS
    ]
    out_answers = sorted(
        (label, dst) for label, dst in graph.get('relations', ())
        if label in ('answered-by', 'settled-by')
    )
    answer_edges.extend({'to': dst, 'relation': label} for label, dst in out_answers)

    if answer_edges:
        answered_open_questions.append({
            'id': ident,
            'selection_state': state,
            'answer_edges': answer_edges,
        })


def lint(system) -> dict:
    """Detect structural graph anomalies using domain-neutral rules and optional domain relation_vocabulary."""
    nodes, all_ids, indexed = _load_graph_nodes(system)
    open_question_ids = {
        ident for ident, node in nodes.items()
        if node['kind'] == 'question'
        and node['active']
        and node['graph'].get('selection_state', '') in _OPEN_QUESTION_STATES
    }
    incoming = _build_incoming(nodes, targets=open_question_ids) if open_question_ids else {}

    vocab = getattr(system.domain_selection, 'relation_vocabulary', {}) or {}
    declared_predicates = set(vocab.get('predicates', ()))
    self_loop_whitelist = _DEFAULT_PROVENANCE_SELF_LOOPS | set(vocab.get('provenance_self_loop_relations', ()))

    findings: dict[str, list[dict]] = {
        'dangling_record_targets': [],
        'non_id_content_refs': [],
        'non_provenance_self_loops': [],
        'duplicate_edges': [],
        'content_relation_drift': [],
        'answered_open_questions': [],
        'uncontrolled_predicates': [],
    }
    question_groups: dict[str, list[str]] = {}
    external_non_record_targets = 0

    for ident in sorted(nodes):
        graph = nodes[ident]['graph']
        rel_targets, ext_count = _lint_node_relations(
            ident, graph, all_ids, self_loop_whitelist, declared_predicates, findings,
        )
        external_non_record_targets += ext_count
        _lint_node_content_links(ident, graph, all_ids, rel_targets, findings)

        if ident in open_question_ids:
            _lint_open_question(ident, graph, incoming, question_groups, findings['answered_open_questions'])

    duplicate_open_questions = [
        {'question': qnorm[:200], 'ids': sorted(ids)}
        for qnorm, ids in sorted(question_groups.items())
        if len(ids) > 1
    ]
    findings['duplicate_open_questions'] = duplicate_open_questions

    return {
        'format': GRAPH_LINT_FORMAT,
        'instance_id': system.instance_id,
        'indexed': indexed,
        'records_examined': len(nodes),
        'external_non_record_targets': external_non_record_targets,
        'relation_vocabulary_declared': bool(declared_predicates),
        'summary': {name: len(rows) for name, rows in findings.items()},
        **findings,
    }
