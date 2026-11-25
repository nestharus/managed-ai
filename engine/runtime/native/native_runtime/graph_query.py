"""Domain-neutral read-only graph query and integrity linter backed by verified HeadIndex."""
from __future__ import annotations

from collections import deque
import re

from native_runtime.head_index import extract_graph_hints
from native_runtime.identities import require
from native_runtime.inputs import integer, strings, text


GRAPH_NEIGHBORS_FORMAT = 'managed-ai-graph-neighbors/1'
GRAPH_LINKS_FORMAT = 'managed-ai-graph-links/1'
GRAPH_PATH_FORMAT = 'managed-ai-graph-path/1'
GRAPH_LINT_FORMAT = 'managed-ai-graph-lint/1'

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
    """Load verified graph metadata from HeadIndex (if enriched) or fall back to scan_records()."""
    store = system.record_store
    cold_ids = set(store.cold_archive.ids())
    summaries = store.indexed_summaries()
    if summaries is not None and all(isinstance(e.get('graph'), dict) for e in summaries.values()):
        nodes = {}
        for ident, entry in summaries.items():
            g = entry['graph']
            nodes[ident] = {
                'id': ident,
                'kind': entry['kind'],
                'revision': entry['revision'],
                'sha256': entry['sha256'],
                'active': entry['active'],
                'relations': g.get('relations', []),
                'duplicate_relations': g.get('duplicate_relations', []),
                'content_links': g.get('content_links', {}),
                'title': g.get('title', ''),
                'selection_state': g.get('selection_state', ''),
                'question_norm': g.get('question_norm'),
            }
        return nodes, set(nodes.keys()) | cold_ids, True

    nodes = {}
    for rec in store.scan_records():
        g = extract_graph_hints(rec)
        ident = rec['id']
        nodes[ident] = {
            'id': ident,
            'kind': rec['kind'],
            'revision': rec['revision'],
            'sha256': rec['sha256'],
            'active': rec['active'],
            'relations': g['relations'],
            'duplicate_relations': g.get('duplicate_relations', []),
            'content_links': g['content_links'],
            'title': g['title'],
            'selection_state': g['selection_state'],
            'question_norm': g.get('question_norm'),
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


def _build_adjacency(nodes: dict[str, dict]):
    outgoing: dict[str, list[tuple[str, str]]] = {ident: [] for ident in nodes}
    incoming: dict[str, list[tuple[str, str]]] = {}
    for src, node in nodes.items():
        for label, target in node['relations']:
            outgoing[src].append((label, target))
            incoming.setdefault(target, []).append((label, src))
    for edges in outgoing.values():
        edges.sort()
    for edges in incoming.values():
        edges.sort()
    return outgoing, incoming


def neighbors(system, record_id: str, *, direction: str = 'both', hops: int = 1,
              relations: list[str] | tuple[str, ...] = (), active_only: bool = False,
              records_only: bool = False) -> dict:
    """Return bounded BFS neighborhood around one record ID."""
    text(record_id, 'record_id')
    require(direction in ('in', 'out', 'both'), 'direction must be in, out or both')
    integer(hops, 'hops', 1)
    require(hops <= 4, 'hops must be between 1 and 4')
    rel_filter = set(strings(list(relations), 'relations')) if relations else None

    nodes, all_ids, indexed = _load_graph_nodes(system)
    require(record_id in nodes, 'record not found in hot graph: ' + record_id)
    outgoing, incoming = _build_adjacency(nodes)

    visited = {record_id: 0}
    queue = deque([(record_id, 0)])
    discovered_nodes: list[dict] = []
    traversed_edges: list[dict] = []
    seen_edges: set[tuple[str, str, str, str]] = set()

    while queue:
        cur, depth = queue.popleft()
        if depth >= hops:
            continue
        step_edges: list[tuple[str, str, str, str]] = []
        if direction in ('out', 'both'):
            for label, target in outgoing.get(cur, ()):
                step_edges.append((cur, target, label, 'out'))
        if direction in ('in', 'both'):
            for label, src in incoming.get(cur, ()):
                step_edges.append((src, cur, label, 'in'))

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
                    'from': src, 'to': dst, 'relation': label,
                    'direction': edge_dir, 'depth': depth + 1,
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


def links(system, *, target: str, relations: list[str] | tuple[str, ...] = (),
          active_only: bool = False) -> dict:
    """Indexed reverse-edge lookup returning all inbound edges pointing to target."""
    text(target, 'target')
    rel_filter = set(strings(list(relations), 'relations')) if relations else None

    nodes, _, indexed = _load_graph_nodes(system)
    _, incoming = _build_adjacency(nodes)
    rows: list[dict] = []
    for label, src in incoming.get(target, ()):
        if rel_filter is not None and label not in rel_filter:
            continue
        src_node = nodes[src]
        if active_only and not src_node['active']:
            continue
        rows.append({
            'from': _node_pin(src_node),
            'relation': label,
            'to': target,
        })
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


def shortest_path(system, src: str, dst: str, *, max_hops: int = 6,
                  active_only: bool = False, relations: list[str] | tuple[str, ...] = ()) -> dict:
    """Deterministic shortest directed record-to-record path via BFS."""
    text(src, 'src')
    text(dst, 'dst')
    integer(max_hops, 'max_hops', 1)
    require(max_hops <= 6, 'max_hops must be between 1 and 6')
    rel_filter = set(strings(list(relations), 'relations')) if relations else None

    nodes, _, indexed = _load_graph_nodes(system)
    require(src in nodes, 'source record not found: ' + src)
    require(dst in nodes, 'destination record not found: ' + dst)
    outgoing, _ = _build_adjacency(nodes)

    if src == dst:
        return {
            'format': GRAPH_PATH_FORMAT,
            'instance_id': system.instance_id,
            'source': src,
            'target': dst,
            'max_hops': max_hops,
            'active_only': active_only,
            'indexed': indexed,
            'found': True,
            'hops': 0,
            'nodes': [_node_pin(nodes[src])],
            'edges': [],
        }

    parent: dict[str, tuple[str, str]] = {}
    depth_map: dict[str, int] = {src: 0}
    queue = deque([src])
    found = False
    while queue:
        cur = queue.popleft()
        cur_depth = depth_map[cur]
        if cur_depth >= max_hops:
            continue
        for label, nxt in outgoing.get(cur, ()):
            if rel_filter is not None and label not in rel_filter:
                continue
            nxt_node = nodes.get(nxt)
            if nxt_node is None:
                continue
            if active_only and nxt != dst and not nxt_node['active']:
                continue
            if active_only and nxt == dst and not nxt_node['active']:
                continue
            if nxt in depth_map:
                continue
            depth_map[nxt] = cur_depth + 1
            parent[nxt] = (cur, label)
            if nxt == dst:
                found = True
                queue.clear()
                break
            queue.append(nxt)

    if not found:
        return {
            'format': GRAPH_PATH_FORMAT,
            'instance_id': system.instance_id,
            'source': src,
            'target': dst,
            'max_hops': max_hops,
            'active_only': active_only,
            'indexed': indexed,
            'found': False,
            'hops': None,
            'nodes': [],
            'edges': [],
        }

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
    return {
        'format': GRAPH_PATH_FORMAT,
        'instance_id': system.instance_id,
        'source': src,
        'target': dst,
        'max_hops': max_hops,
        'active_only': active_only,
        'indexed': indexed,
        'found': True,
        'hops': len(path_edges),
        'nodes': [_node_pin(nodes[i]) for i in path_ids],
        'edges': path_edges,
    }


def lint(system) -> dict:
    """Detect structural graph anomalies using domain-neutral rules and optional domain relation_vocabulary."""
    nodes, all_ids, indexed = _load_graph_nodes(system)
    outgoing, incoming = _build_adjacency(nodes)

    vocab = getattr(system.domain_selection, 'relation_vocabulary', {}) or {}
    declared_predicates = set(vocab.get('predicates', ()))
    self_loop_whitelist = _DEFAULT_PROVENANCE_SELF_LOOPS | set(vocab.get('provenance_self_loop_relations', ()))

    dangling_record_targets: list[dict] = []
    non_id_content_refs: list[dict] = []
    non_provenance_self_loops: list[dict] = []
    duplicate_edges: list[dict] = []
    content_relation_drift: list[dict] = []
    answered_open_questions: list[dict] = []
    uncontrolled_predicates: list[dict] = []
    question_groups: dict[str, list[str]] = {}
    external_non_record_targets = 0

    for ident in sorted(nodes):
        node = nodes[ident]
        rel_list = node['relations']
        rel_targets = {target for _, target in rel_list}

        # 1. Exact duplicate relation rows, self-loops, dangling record-like targets, controlled vocabulary
        for dup_label, dup_target in node.get('duplicate_relations', ()):
            duplicate_edges.append({'id': ident, 'relation': dup_label, 'target': dup_target})
        for label, target in rel_list:
            if target == ident:
                if label not in self_loop_whitelist:
                    non_provenance_self_loops.append({'id': ident, 'relation': label})
            elif target not in all_ids:
                if label in _PROVENANCE_RELATION_LABELS or '/' in target or '.' in target or ':' in target:
                    external_non_record_targets += 1
                elif _RECORD_ID_RE.fullmatch(target):
                    dangling_record_targets.append({'id': ident, 'relation': label, 'target': target})
                else:
                    external_non_record_targets += 1
            else:
                # Record-to-record edge: check optional controlled predicate vocabulary
                if declared_predicates and label not in declared_predicates and label not in _PROVENANCE_RELATION_LABELS:
                    uncontrolled_predicates.append({'id': ident, 'relation': label, 'target': target})

        # 2. Content FK fields: non-ID prose detection + content <-> relations drift
        for field_name, values in sorted(node['content_links'].items()):
            for val in values:
                if not _RECORD_ID_RE.fullmatch(val):
                    non_id_content_refs.append({'id': ident, 'field': field_name, 'value': val[:160]})
                elif val in all_ids and val != ident and val not in rel_targets:
                    content_relation_drift.append({'id': ident, 'field': field_name, 'target': val})

        # 3. Open question duplicate clusters & answered-yet-open checks
        if node['kind'] == 'question' and node['active']:
            state = node['selection_state']
            if state in _OPEN_QUESTION_STATES:
                qnorm = node.get('question_norm')
                if qnorm:
                    question_groups.setdefault(qnorm, []).append(ident)
                answer_edges = [
                    {'from': src, 'relation': label}
                    for label, src in incoming.get(ident, ())
                    if label in _ANSWER_RELATION_LABELS
                ]
                answer_edges.extend(
                    {'to': dst, 'relation': label}
                    for label, dst in outgoing.get(ident, ())
                    if label in ('answered-by', 'settled-by')
                )
                if answer_edges:
                    answered_open_questions.append({
                        'id': ident, 'selection_state': state, 'answer_edges': answer_edges,
                    })

    duplicate_open_questions = [
        {'question': qnorm[:200], 'ids': sorted(ids)}
        for qnorm, ids in sorted(question_groups.items())
        if len(ids) > 1
    ]

    return {
        'format': GRAPH_LINT_FORMAT,
        'instance_id': system.instance_id,
        'indexed': indexed,
        'records_examined': len(nodes),
        'external_non_record_targets': external_non_record_targets,
        'relation_vocabulary_declared': bool(declared_predicates),
        'summary': {
            'dangling_record_targets': len(dangling_record_targets),
            'non_id_content_refs': len(non_id_content_refs),
            'non_provenance_self_loops': len(non_provenance_self_loops),
            'duplicate_edges': len(duplicate_edges),
            'content_relation_drift': len(content_relation_drift),
            'duplicate_open_questions': len(duplicate_open_questions),
            'answered_open_questions': len(answered_open_questions),
            'uncontrolled_predicates': len(uncontrolled_predicates),
        },
        'dangling_record_targets': dangling_record_targets,
        'non_id_content_refs': non_id_content_refs,
        'non_provenance_self_loops': non_provenance_self_loops,
        'duplicate_edges': duplicate_edges,
        'content_relation_drift': content_relation_drift,
        'duplicate_open_questions': duplicate_open_questions,
        'answered_open_questions': answered_open_questions,
        'uncontrolled_predicates': uncontrolled_predicates,
    }
