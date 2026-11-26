"""Authored relation extraction and bounded BFS anchor traversal for Discovery."""
from __future__ import annotations

from collections import deque

from managed_ai.store.records.head_index import extract_content_record_links


def _append_content_link_edges(discovery, item: dict, result: list[dict], excluded: set[str]) -> None:
    record = item.get('record', {})
    if not (discovery.req.get('include_content_links') and isinstance(record.get('content'), dict)):
        return

    ref = item['ref']
    content_links = extract_content_record_links(record['content'])
    syn_index = len(record.get('relations', []))

    for field_name, vals in sorted(content_links.items()):
        label = 'content:' + field_name
        if label in excluded or (discovery.req['relation_labels'] and label not in discovery.req['relation_labels']):
            continue

        for val in vals:
            target = 'record:' + val
            if target not in discovery.entries:
                continue

            key = (ref, syn_index)
            syn_index += 1
            if key not in discovery.relation_rows:
                if len(discovery.relation_rows) >= discovery.req['scan_limit']:
                    discovery.saturated.add('relation-scan-budget')
                    discovery.frontier.append({
                        'ref': ref,
                        'reason': 'relation-scan-budget',
                        'next_relation_index': syn_index - 1,
                    })
                    return
                discovery.relation_rows.add(key)

            edge = {
                'from': ref,
                'to': target,
                'relation': label,
                'declared_in': {
                    'ref': ref,
                    'revision': item['source']['revision'],
                    'record_sha256': item['source']['record_sha256'],
                },
                'properties': {'channel': 'content-link', 'field': field_name},
            }
            result.append(edge)
            if key not in discovery.edge_keys:
                discovery.edge_keys.add(key)
                discovery.edges.append(edge)


def extract_record_edges(discovery, item: dict) -> list[dict]:
    """Extract declared relation edges (and optional content links) from one inspected record."""
    ref = item['ref']
    if ref in discovery.edge_cache:
        return discovery.edge_cache[ref]

    result: list[dict] = []
    excluded = set(discovery.req.get('exclude_relation_labels', ()))
    records_only = bool(discovery.req.get('records_only', False))
    record = item.get('record', {})

    for index, relation in enumerate(record.get('relations', [])):
        key = (ref, index)
        if key not in discovery.relation_rows:
            if len(discovery.relation_rows) >= discovery.req['scan_limit']:
                discovery.saturated.add('relation-scan-budget')
                discovery.frontier.append({
                    'ref': ref,
                    'reason': 'relation-scan-budget',
                    'next_relation_index': index,
                })
                break
            discovery.relation_rows.add(key)

        if not isinstance(relation, dict) or not isinstance(relation.get('relation'), str):
            discovery._unresolve(str(relation), 'malformed-declared-relation', ref)
            continue

        label = relation['relation']
        if label in excluded:
            continue
        if discovery.req['relation_labels'] and label not in discovery.req['relation_labels']:
            continue

        raw_target = relation.get('target')
        if records_only:
            if not isinstance(raw_target, str) or not raw_target:
                continue
            candidate_ref = (
                raw_target
                if raw_target.startswith('record:')
                else ('record:' + raw_target if ':' not in raw_target else None)
            )
            if candidate_ref is None or candidate_ref not in discovery.entries:
                continue
            target = candidate_ref
        else:
            target = discovery._resolve(raw_target, ref)
            if target is None:
                continue

        edge = {
            'from': ref,
            'to': target,
            'relation': label,
            'declared_in': {
                'ref': ref,
                'revision': item['source']['revision'],
                'record_sha256': item['source']['record_sha256'],
            },
            'properties': {k: v for k, v in relation.items() if k not in ('relation', 'target')},
        }
        result.append(edge)
        if key not in discovery.edge_keys:
            discovery.edge_keys.add(key)
            discovery.edges.append(edge)

    _append_content_link_edges(discovery, item, result, excluded)
    discovery.edge_cache[ref] = result
    return result


def build_incoming_edges(discovery) -> dict[str, list[dict]]:
    if discovery.incoming is None:
        discovery.incoming = {}
        for ref in discovery.by_scope['records']:
            item = discovery._inspect(ref)
            if item:
                for edge in extract_record_edges(discovery, item):
                    discovery.incoming.setdefault(edge['to'], []).append(edge)
    return discovery.incoming


def traverse_anchors(discovery) -> None:
    """Breadth-first relation traversal starting from requested discovery anchors."""
    queue: deque[tuple[str, int]] = deque()
    visited: set[str] = set()

    for anchor in discovery.req['anchors']:
        ref = discovery._resolve(anchor)
        if ref is None:
            continue
        item = discovery._inspect(ref)
        if item is None:
            discovery._unresolve(anchor, 'anchor-not-examined-or-inactive')
            continue
        discovery._candidate(item, {'kind': 'anchor', 'requested': anchor})
        if ref not in visited:
            queue.append((ref, 0))
            visited.add(ref)

    while queue:
        ref, depth = queue.popleft()
        item = discovery.loaded[ref]
        if depth >= discovery.req['hops']:
            discovery.frontier.append({
                'ref': ref,
                'reason': 'hop-bound',
                'depth': depth,
                'scope': 'Not expanded; this does not assert that further relations exist.',
            })
            continue

        edges = []
        if discovery.req['direction'] in ('outgoing', 'both') and item['scope'] == 'records':
            edges.extend((e, e['to'], 'outgoing') for e in extract_record_edges(discovery, item))
        if discovery.req['direction'] in ('incoming', 'both'):
            edges.extend((e, e['from'], 'incoming') for e in build_incoming_edges(discovery).get(ref, []))

        for edge, target, direction in edges:
            other = discovery._inspect(target)
            if other is None:
                discovery._unresolve(target, 'endpoint-not-examined-or-inactive', edge['from'])
                continue
            discovery._candidate(
                other,
                {
                    'kind': 'declared-relation',
                    'edge': edge,
                    'direction': direction,
                    'depth': depth + 1,
                },
            )
            if target not in visited:
                visited.add(target)
                queue.append((target, depth + 1))
