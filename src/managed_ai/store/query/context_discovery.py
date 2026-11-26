"""Bounded, source-qualified discovery for the cooperative local agent system.

The host supplies useful questions, relation meanings and applicability. This
module searches declared local scopes and follows authored record relations; it
does not infer authority, semantic completeness or a universal dependency graph.
No persistent index is maintained. The returned selectors are candidates for a
later context, not a claim that a receiving participant has read their bodies.
"""
from __future__ import annotations

from contextlib import nullcontext
import copy
import errno
import hashlib
import json
import os
from pathlib import Path
import stat
import sys

from managed_ai.core.formats import DISCOVERY_FORMAT as FORMAT
from managed_ai.core.ownership import packet_id
from managed_ai.core.storage import _OPEN_RO_NOFOLLOW, is_ordinary_file_stat
from managed_ai.platform import strict_json_loads
from managed_ai.store.protocol.inputs import parse_input_bytes
from managed_ai.store.query.discovery_traversal import (
    build_incoming_edges,
    extract_record_edges,
    traverse_anchors,
)
from managed_ai.store.records.records import RecordStore

PROJECT_CONTEXT_BYTES = 2 * 1024 * 1024
SCOPES = ('records', 'project', 'techniques')
PREFIX_SCOPE = {'record': 'records', 'project': 'project', 'technique': 'techniques'}
SELECTOR = {'records': 'records', 'project': 'project_sources', 'techniques': 'techniques'}
FIELDS = {
    'query', 'content', 'scopes', 'anchors', 'relation_labels', 'direction',
    'hops', 'limit', 'scan_limit', 'max_bytes', 'include_inactive',
    'scan_order', 'seed', 'records_only', 'exclude_relation_labels',
    'include_content_links',
}
SCAN_ORDERS = ('inventory', 'scope-round-robin', 'seeded')
SCOPE = (
    'Lexical matches and authored relation routes nominate inspection. '
    'Machine examination, a returned candidate, body delivery, semantic '
    'applicability, actual use and authority are different outcomes. '
    'No returned match establishes no adequate source outside this search.'
)

# Minimum valid native record JSON byte count (mandatory fields + 64-hex sha256 exceed 200B).
_MIN_RECORD_JSON_BYTES = 128
_MAX_ZERO_PROGRESS_RECORD_BYTE_SKIPS = 64


def digest(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def canonical(value) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
        allow_nan=False,
    ).encode('utf-8')


def integer(value, name: str, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f'{name} must be an integer from {minimum} to {maximum}')

    return value


def strings(value, name: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(v, str) or not v.strip() for v in value):
        raise ValueError(name + ' must be a list of nonempty strings')

    if len(set(value)) != len(value):
        raise ValueError(name + ' contains duplicates')

    return value


def request(value, deployed: bool) -> dict:
    if not isinstance(value, dict):
        raise ValueError('discovery request must be an object')

    if set(value) - FIELDS:
        raise ValueError('unknown discovery fields: ' + ', '.join(sorted(set(value) - FIELDS)))

    query = value.get('query', '')

    if not isinstance(query, str):
        raise ValueError('discovery query must be text')

    for flag in ('content', 'include_inactive', 'records_only', 'include_content_links'):
        if flag in value and type(value[flag]) is not bool:
            raise ValueError(flag + ' must be Boolean')

    scopes = strings(value.get('scopes', list(SCOPES if deployed else ('records', 'techniques'))), 'scopes')

    if not scopes or set(scopes) - set(SCOPES):
        raise ValueError('scopes must select records, project or techniques')

    if 'project' in scopes and not deployed:
        raise ValueError('project discovery requires an explicitly selected deployment')

    direction = value.get('direction', 'outgoing')

    if direction not in ('outgoing', 'incoming', 'both'):
        raise ValueError('direction must be outgoing, incoming or both')

    scan_order = value.get('scan_order', 'inventory')

    if scan_order not in SCAN_ORDERS:
        raise ValueError('scan_order must be inventory, scope-round-robin or seeded')

    if scan_order == 'seeded':
        seed = integer(value.get('seed'), 'seed', 0, 2**64 - 1)
    else:
        if 'seed' in value:
            raise ValueError('seed is only accepted with scan_order=seeded')
        seed = None

    normalized = {
        'query': query,
        'content': value.get('content', False),
        'scopes': scopes,
        'anchors': strings(value.get('anchors', []), 'anchors'),
        'relation_labels': strings(value.get('relation_labels', []), 'relation_labels'),
        'direction': direction,
        'hops': integer(value.get('hops', 1), 'hops', 0, 8),
        'limit': integer(value.get('limit', 25), 'limit', 1, 200),
        'scan_limit': integer(value.get('scan_limit', 1000), 'scan_limit', 1, 100000),
        'max_bytes': integer(value.get('max_bytes', 1048576), 'max_bytes', 1, 33554432),
        'include_inactive': value.get('include_inactive', False),
        'scan_order': scan_order,
    }

    if scan_order == 'seeded':
        normalized['seed'] = seed

    if value.get('records_only'):
        normalized['records_only'] = True

    exclude_labels = strings(value.get('exclude_relation_labels', []), 'exclude_relation_labels')

    if exclude_labels:
        normalized['exclude_relation_labels'] = exclude_labels

    if value.get('include_content_links'):
        normalized['include_content_links'] = True

    return normalized


class Discovery:
    def __init__(self, system):
        self.records = system.record_store
        self.techniques = system.techniques
        self.projects = system.projects

    def verify_selection(self, pins):
        """Resolve exact candidate pins before a later context uses their selectors."""

        if not isinstance(pins, list) or len(pins) > 200:
            raise ValueError('source pins must be a list of at most 200 {ref, source} objects')

        refs, scopes = [], set()

        for pin in pins:
            if not isinstance(pin, dict) or set(pin) != {'ref', 'source'}:
                raise ValueError('each source pin must contain exactly ref and source')
            ref = pin['ref']
            if not isinstance(ref, str) or not ref or not isinstance(pin['source'], dict):
                raise ValueError('a source pin requires a reference and source identity object')
            scope = PREFIX_SCOPE.get(ref.partition(':')[0])
            if scope is None or ':' not in ref:
                raise ValueError('source pin must use a declared reference namespace')
            if ref in refs:
                raise ValueError('duplicate source pin: ' + ref)
            canonical(pin)
            refs.append(ref)
            scopes.add(scope)

        if not pins:
            return {'verified': [], 'context_request': {}}

        current = self.search({
            'anchors': refs,
            'scopes': [s for s in SCOPES if s in scopes],
            'hops': 0,
            'limit': 200,
            'scan_limit': 100000,
            'max_bytes': 33554432,
            'include_inactive': True,
        })
        candidates = {c['ref']: c for c in current['candidates']}

        for pin in pins:
            candidate = candidates.get(pin['ref'])
            if candidate is None:
                raise ValueError('selected discovery source is missing or exceeds verification bounds: ' + pin['ref'])
            if candidate['source'] != pin['source']:
                raise ValueError('selected discovery source identity changed: ' + pin['ref'])
            if candidate.get('selector') is None or not candidate.get('body_identity', {}).get('verified'):
                raise ValueError('selected discovery source is unavailable as verified context text: ' + pin['ref'])

        verified = strict_json_loads(canonical(pins), 'source pins', require_object=False)

        return {'verified': verified, 'context_request': current['context_request']}

    def _init_search_state(self, supplied: dict) -> None:
        self.req = request(supplied, self.projects.current is not None)
        self.entries = {}
        self.by_scope = {scope: [] for scope in SCOPES}
        self.catalogue = None
        self.examined, self.loaded, self.bodies = set(), {}, {}
        self.body_info, self.body_failures = {}, {}
        self.body_objects, self.reused_body_references = {}, 0
        self.examined_bytes = 0
        self.skipped, self.unresolved, self.frontier = [], [], []
        self._skips, self._unresolved = set(), set()
        self.relation_rows, self.edges, self.edge_keys = set(), [], set()
        self.edge_cache = {}
        self.candidates = {}
        self.incoming = None
        self.lexical_nomination = None
        self.saturated = set()
        self._records_fd = None
        self._records_dir_str = None
        self._project_selection_copy = (
            copy.deepcopy(self.projects.current.get('selection'))
            if self.projects.current is not None else None
        )
        self._catalogue_snapshot_copy = None

    def _build_coverage(self, all_candidates: list[dict], shown: list[dict], omitted: list[str]) -> dict:
        unexamined = {
            scope: [ref for ref in self.by_scope[scope] if ref not in self.examined]

            for scope in self.req['scopes']
        }
        project = self.projects.current
        search_mode = (
            'anchor-neighborhood-and-lexical'
            if self.req['anchors'] and self.req['query'].strip()
            else 'anchor-neighborhood'
            if self.req['anchors']
            else 'lexical'
            if self.req['query'].strip()
            else 'inventory'
        )

        return {
            'requested_scopes': self.req['scopes'],
            'unavailable_default_scopes': [] if project else ['project'],
            'inventory_counts': {s: len(self.by_scope[s]) for s in self.req['scopes']},
            'inventory': {s: self.by_scope[s] for s in self.req['scopes']},
            'examined_entries': len(self.examined),
            'examined_entries_by_scope': {
                s: len(self.by_scope[s]) - len(unexamined[s]) for s in self.req['scopes']
            },
            'lexical_nomination': self.lexical_nomination,
            'examined_relation_rows': len(self.relation_rows),
            'examined_body_bytes': self.examined_bytes,
            'examined_body_reference_bytes': sum(row['bytes'] for row in self.body_info.values()),
            'examined_body_sources': len(self.body_info) - self.reused_body_references,
            'reused_body_references': self.reused_body_references,
            'body_accounting': (
                'examined_body_bytes charges each acquired bound source object once '
                'within this query. examined_body_reference_bytes counts its bytes '
                'for each verified reference. Reuse requires the same catalogue '
                'body path, expected digest and byte count; no cross-query cache.'
            ),
            'unexamined': unexamined,
            'unexamined_count': sum(map(len, unexamined.values())),
            'skipped': self.skipped,
            'unresolved_references': self.unresolved,
            'traversal_frontier': self.frontier,
            'omitted_candidate_refs': omitted,
            'matched_candidates': len(all_candidates),
            'shown_candidates': len(shown),
            'limits': {k: self.req[k] for k in ('hops', 'limit', 'scan_limit', 'max_bytes')},
            'saturated': sorted(self.saturated),
            'search_mode': search_mode,
            'project_selection': copy.deepcopy(self._project_selection_copy),
            'project_manifest_sha256': None if project is None else project.get('selection', {}).get('manifest_sha256'),
            'catalogue_snapshot': copy.deepcopy(self._catalogue_snapshot_copy),
            'catalogue_metadata_sha256': None if self.catalogue is None else digest(canonical(self.catalogue)),
            'record_membership_sha256': digest(canonical(self.by_scope['records'])),
            'binding_scope': (
                'Catalogue metadata digest uses sorted compact UTF-8 JSON. Record membership '
                'binds visible IDs only; revisions of unexamined records were not read. '
                'Project identity comes from the selected verified deployment manifest. '
                'Complete examined body identities are reported separately.'
            ),
            'scope': SCOPE,
        }

    def _build_ranked_recovery(self) -> dict | None:
        engine = Path(__file__).resolve().parents[2]
        cli_path = engine / 'library' / 'cli.py'
        lib_dir = engine / 'library' / 'library'

        if not (cli_path.is_file() and (lib_dir / 'catalogue.json').is_file()):
            return None

        query = self.req['query']
        recovery = {
            'operation': 'ranked-published-knowledge',
            'scope': (
                'Optional separate, read-only BM25 search of the installed knowledge library. '
                'Local records and project files stay in the app search. Inspect the returned '
                'source owner before using a nomination.'
            ),
        }

        if len(query.encode('utf-8')) <= 2048 and '\x00' not in query:
            recovery['argv'] = [
                sys.executable, '-B', str(cli_path), '--library', str(lib_dir),
                'discover', '--backend', 'bm25',
                '--query=' + query, '--limit', str(min(self.req['limit'], 100)),
                '--scan-limit', str(min(self.req['scan_limit'], 2048)),
                '--max-bytes', str(self.req['max_bytes']),
            ]
        else:
            recovery['input_needed'] = 'Supply a ranked query of at most 2048 UTF-8 bytes without NUL.'

        return recovery

    def search(self, supplied):
        """Return a bounded discovery report without writing or invoking effects."""
        self._init_search_state(supplied)

        use_fast_records = (
            'records' in self.req['scopes']
            and type(self.records).size is RecordStore.size
            and type(self.records).read is RecordStore.read
            and (self.records.work / 'records').is_dir()
        )
        fd_ctx = nullcontext(None)

        if use_fast_records:
            if hasattr(os, 'O_NOFOLLOW') and hasattr(os, 'O_DIRECTORY'):
                fd_ctx = self.records.ex.open_workspace_dir('records')
            else:
                _, self._records_dir_str = self.records._validated_records_dir_win()

        with fd_ctx as records_fd:
            self._records_fd = records_fd

            try:
                self._inventory()
                if self.req['anchors']:
                    self._traverse()
                if self.req['query'].strip() or not self.req['anchors']:
                    self._lexical()

                all_candidates = list(self.candidates.values())
                shown = all_candidates[:self.req['limit']]
                selectors = {key: [] for key in SELECTOR.values()}

                for candidate in shown:
                    self._selector(candidate)

                    for key, values in (candidate.get('selector') or {}).items():
                        selectors[key].extend(v for v in values if v not in selectors[key])
            finally:
                self._records_fd = None
                self._records_dir_str = None

        omitted = [c['ref'] for c in all_candidates[self.req['limit']:]]

        if omitted:
            self.saturated.add('result-limit')

        coverage = self._build_coverage(all_candidates, shown, omitted)
        result = {
            'format': FORMAT,
            'request': copy.deepcopy(self.req),
            'candidates': shown,
            'context_request': {k: v for k, v in selectors.items() if v},
            'context_request_scope': (
                'Candidate selectors only: not automatically applicable, read by a '
                'participant, or authorized for an effect. Retain this discovery '
                'report and compare each expected source identity when preparing '
                'a later context. A changed source needs an explicit current use.'
            ),
            'source_bindings': {c['ref']: copy.deepcopy(c['source']) for c in shown},
            'examined_bodies': {k: dict(v) for k, v in self.body_info.items()},
            'coverage': coverage,
            'ordering': (
                'Requested anchors and bounded breadth-first relations, then lexical '
                + self.req['scan_order'] + ' order; not a relevance ranking.'
            ),
        }

        if not shown and coverage['search_mode'] == 'lexical' and 'techniques' in self.req['scopes']:
            recovery = self._build_ranked_recovery()
            if recovery is not None:
                result['recovery'] = recovery

        return result

    def _inventory(self):
        if 'records' in self.req['scopes']:
            for ident in self.records.ids():
                ref = 'record:' + ident
                self.entries[ref] = {'scope': 'records', 'id': ident}
                self.by_scope['records'].append(ref)

        if 'project' in self.req['scopes']:
            for row in sorted(self.projects.current['project_files'], key=lambda r: r['path']):
                name = row['path']
                if (
                    not isinstance(name, str)
                    or not name
                    or '\0' in name
                    or '\\' in name
                    or name.startswith('/')
                    or (len(name) >= 2 and name[1] == ':')
                    or any(part == '..' or (len(part) >= 2 and part[1] == ':') for part in name.split('/'))
                ):
                    raise ValueError('invalid accepted-project member path')
                ref = 'project:' + name
                if ref in self.entries:
                    raise ValueError('duplicate accepted-project member')
                self.entries[ref] = {'scope': 'project', 'id': name, 'row': row}
                self.by_scope['project'].append(ref)

        if 'techniques' in self.req['scopes']:
            self.catalogue = self.techniques.index()
            self._catalogue_snapshot_copy = copy.deepcopy(self.catalogue.get('snapshot'))
            entries = self.catalogue.get('entries', [])
            if not isinstance(entries, list):
                raise ValueError('catalogue entries must be a list')

            for row in sorted(entries, key=lambda r: r['id']):
                ref = 'technique:' + row['id']
                if ref in self.entries:
                    raise ValueError('duplicate catalogue entry')
                self.entries[ref] = {'scope': 'techniques', 'id': row['id'], 'row': row}
                self.by_scope['techniques'].append(ref)

    def _skip(self, ref, reason):
        key = (ref, reason)

        if key not in self._skips:
            self._skips.add(key)
            self.skipped.append({'ref': ref, 'reason': reason})

    def _unresolve(self, target, reason, declared_in=None):
        key = (target, reason, declared_in)

        if key not in self._unresolved:
            self._unresolved.add(key)
            self.unresolved.append({'target': target, 'reason': reason, 'declared_in': declared_in})

    def _resolve(self, target, declared_in=None):
        if not isinstance(target, str) or not target:
            self._unresolve(str(target), 'nonempty-reference-required', declared_in)
            return None

        if ':' not in target and 'record:' + target in self.entries:
            target = 'record:' + target

        prefix = target.partition(':')[0]
        scope = PREFIX_SCOPE.get(prefix)

        if scope is None:
            self._unresolve(target, 'unknown-reference-or-undeclared-bare-record', declared_in)
            return None

        if scope not in self.req['scopes']:
            self._unresolve(target, 'outside-requested-scopes', declared_in)
            return None

        if target not in self.entries:
            self._unresolve(target, 'absent-from-selected-inventory', declared_in)
            return None
        return target

    def _budget_bytes(self, ref, count) -> bool:
        if type(count) is not int or count < 0:
            raise ValueError('source byte count must be a nonnegative integer')

        if self.examined_bytes + count > self.req['max_bytes']:
            self.saturated.add('body-byte-budget')
            self._skip(ref, 'body-byte-budget')
            return False
        self.examined_bytes += count

        return True

    def _read_record_bounded(self, ref, ident):
        """Read and verify one hot record in a single open/stat under the discovery byte budget."""

        if self._records_fd is not None:
            filename = packet_id(ident) + '.json'
            full_path = self.records.work / 'records' / filename

            try:
                fd = os.open(filename, _OPEN_RO_NOFOLLOW, dir_fd=self._records_fd)
            except FileNotFoundError:
                byte_limit = self.records.size(ident)
                if not self._budget_bytes(ref, byte_limit):
                    return None
                return self.records.read(ident, byte_limit=byte_limit)
            except OSError as exc:
                if exc.errno in (errno.ELOOP, errno.ENOTDIR, errno.EISDIR, errno.ENXIO):
                    raise ValueError(f'ordinary JSON file required: {full_path}') from None
                raise

            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode):
                    raise ValueError(f'ordinary JSON file required: {full_path}')
                byte_limit = info.st_size
                if not self._budget_bytes(ref, byte_limit):
                    os.close(fd)
                    fd = -1
                    return None
                stream = os.fdopen(fd, 'rb')
                fd = -1
            except BaseException:
                if fd >= 0:
                    os.close(fd)
                raise

            with stream:
                raw = stream.read(byte_limit + 1)
            if len(raw) > byte_limit:
                raise ValueError(f'input exceeds {byte_limit} bytes: {full_path}')
            record = parse_input_bytes(raw, full_path)
            self.records._verify_record(ident, record, raw)
            return record, raw

        if self._records_dir_str is not None:
            filename = packet_id(ident) + '.json'
            full_str = self._records_dir_str + os.sep + filename

            try:
                info = os.lstat(full_str)
            except FileNotFoundError:
                byte_limit = self.records.size(ident)
                if not self._budget_bytes(ref, byte_limit):
                    return None
                return self.records.read(ident, byte_limit=byte_limit)

            if not is_ordinary_file_stat(info):
                raise ValueError(f'ordinary JSON file required: {full_str}')

            byte_limit = info.st_size
            if not self._budget_bytes(ref, byte_limit):
                return None

            with open(full_str, 'rb') as stream:
                raw = stream.read(byte_limit + 1)
            if len(raw) > byte_limit:
                raise ValueError(f'input exceeds {byte_limit} bytes: {full_str}')
            record = parse_input_bytes(raw, Path(full_str))
            self.records._verify_record(ident, record, raw)
            return record, raw

        byte_limit = self.records.size(ident)

        if not self._budget_bytes(ref, byte_limit):
            return None
        return self.records.read(ident, byte_limit=byte_limit)

    def _inspect(self, ref):
        if ref in self.loaded:
            return self.loaded[ref]

        if ref in self.examined:
            return None

        if len(self.examined) >= self.req['scan_limit']:
            self.saturated.add('entry-scan-budget')
            self._skip(ref, 'entry-scan-budget')
            return None

        self.examined.add(ref)
        entry = self.entries[ref]
        scope, ident = entry['scope'], entry['id']

        if scope == 'records':
            loaded = self._read_record_bounded(ref, ident)
            if loaded is None:
                return None
            record, raw = loaded
            raw_sha = digest(raw)
            self.bodies[ref] = raw.decode('utf-8')
            self.body_info[ref] = {'bytes': len(raw), 'sha256': raw_sha, 'verified': True, 'text': True}
            if not self.req['include_inactive'] and record.get('active') is False:
                self._skip(ref, 'inactive-record')
                return None

            source = {
                'scope': scope,
                'id': ident,
                'format': record.get('format'),
                'revision': record['revision'],
                'record_sha256': record['sha256'],
                'body_sha256': raw_sha,
                'bytes': len(raw),
            }
            metadata = ' '.join(str(record.get(k, '')) for k in ('id', 'kind'))
            value = {
                'ref': ref,
                'scope': scope,
                'id': ident,
                'source': source,
                'title': ident,
                'metadata': metadata,
                'record': record,
            }
        elif scope == 'project':
            row = entry['row']
            source = {
                'scope': scope,
                'path': ident,
                'sha256': row['sha256'],
                'bytes': row['bytes'],
                'selection': copy.deepcopy(self._project_selection_copy),
            }
            value = {'ref': ref, 'scope': scope, 'id': ident, 'source': source, 'title': ident, 'metadata': ident}
        else:
            row = entry['row']
            source = {
                'scope': scope,
                'id': ident,
                'sha256': row['sha256'],
                'bytes': row['bytes'],
                'catalogue_snapshot': copy.deepcopy(self._catalogue_snapshot_copy),
                'source_root': row.get('source_root'),
                'source_path': row.get('source_path'),
            }
            metadata = ' '.join(str(row.get(k, '')) for k in ('id', 'title', 'kind', 'family', 'source_path', 'purpose'))
            value = {
                'ref': ref,
                'scope': scope,
                'id': ident,
                'source': source,
                'title': row.get('title', ident),
                'metadata': metadata,
            }

        self.loaded[ref] = value

        return value

    def _body(self, ref):
        if ref in self.bodies:
            return self.bodies[ref]

        if ref in self.body_failures:
            return None

        entry = self.entries[ref]
        row, scope = entry.get('row'), entry['scope']

        if scope == 'records':
            return None

        if scope == 'project' and row['bytes'] > PROJECT_CONTEXT_BYTES:
            self.body_failures[ref] = 'exceeds-project-context-body-bound'
            self._skip(ref, self.body_failures[ref])
            return None

        object_key = None
        if scope == 'techniques':
            if row.get('availability') == 'sealed' and ref not in self.req['anchors']:
                self.body_failures[ref] = 'sealed-source'
                self._skip(ref, 'sealed-source')
                return None

            if not isinstance(row.get('body_path'), str) or not isinstance(row.get('sha256'), str):
                raise ValueError('catalogue body path and digest must be text')
            if type(row.get('bytes')) is not int or row['bytes'] < 0:
                raise ValueError('source byte count must be a nonnegative integer')

            object_key = (row['body_path'], row['sha256'], row['bytes'])
            if object_key in self.body_objects:
                self.reused_body_references += 1
                return self._retain_body(ref, *self.body_objects[object_key])

        if not self._budget_bytes(ref, row['bytes']):
            self.body_failures[ref] = 'body-byte-budget'
            return None

        if scope == 'project':
            raw = self.projects.body(row)
        else:
            raw = self.techniques.body(row, byte_limit=row['bytes'])

        if len(raw) != row['bytes'] or digest(raw) != row['sha256']:
            raise ValueError('discovery source binding changed: ' + ref)

        info = {'bytes': len(raw), 'sha256': digest(raw), 'verified': True, 'text': False}
        failure = None
        try:
            body = raw.decode('utf-8')
        except UnicodeDecodeError:
            body, failure = None, 'not-utf8-text'

        if body is not None and '\x00' in body:
            body, failure = None, 'contains-nul-not-context-text'

        info['text'] = failure is None
        if object_key is not None:
            self.body_objects[object_key] = (info, body, failure)
        return self._retain_body(ref, info, body, failure)

    def _retain_body(self, ref, info, body, failure):
        self.body_info[ref] = dict(info)
        if failure is not None:
            self.body_failures[ref] = failure
            self._skip(ref, failure)
            return None
        self.bodies[ref] = body
        return body

    def _candidate(self, item, match):
        ref = item['ref']
        if ref not in self.candidates:
            self.candidates[ref] = {k: item[k] for k in ('ref', 'title', 'source')}
            self.candidates[ref]['matches'] = []
        if match not in self.candidates[ref]['matches']:
            self.candidates[ref]['matches'].append(match)

    def _selector(self, candidate):
        ref = candidate['ref']
        entry = self.entries[ref]
        body = self._body(ref)
        if body is None:
            candidate['selector'] = None
            candidate['acquisition_needed'] = self.body_failures.get(ref, 'source-not-available-as-context-text')
        else:
            candidate['selector'] = {SELECTOR[entry['scope']]: [entry['id']]}
            candidate['body_identity'] = self.body_info[ref]
            candidate['acquisition_needed'] = None

    def _lexical_order(self):
        scopes = [self.by_scope[s] for s in self.req['scopes']]
        mode = self.req['scan_order']
        if mode == 'scope-round-robin':
            refs = [
                rows[index]
                for index in range(max(map(len, scopes), default=0))
                for rows in scopes
                if index < len(rows)
            ]
        else:
            refs = [ref for rows in scopes for ref in rows]
            if mode == 'seeded':
                refs.sort(key=lambda ref: (digest(canonical({'seed': self.req['seed'], 'ref': ref})), ref))

        self.lexical_nomination = {
            'policy': mode,
            'algorithm': (
                'sha256-canonical-seed-ref-sort/1'
                if mode == 'seeded'
                else 'scope-round-robin/1'
                if mode == 'scope-round-robin'
                else 'requested-scopes-sorted-identities/1'
            ),
            'seed': self.req.get('seed'),
            'entries': len(refs),
            'order_sha256': digest(canonical(refs)),
            'scope': (
                'Order nominates the full declared lexical inventory after anchors. '
                'Entry examination, body acquisition and returned candidates remain separately bounded. '
                'A fixed seed repeats the same order; changed membership can change its prefix. '
                'Seeded hash sorting is pseudorandom exploration, not a uniform-sampling, '
                'independent-trial, semantic-relevance or complete-gap-detection guarantee. '
                'All policies enumerate metadata; seeded sorting costs O(N log N).'
            ),
        }
        return refs

    def _lexical(self):
        terms = self.req['query'].casefold().split()
        records_only_scope = self.req['scopes'] == ['records']
        records_byte_exhausted = False
        zero_progress_record_skips = 0

        for ref in self._lexical_order():
            if len(self.examined) >= self.req['scan_limit'] and ref not in self.examined:
                self.saturated.add('entry-scan-budget')
                self._skip(ref, 'entry-scan-budget')
                break

            scope = self.entries[ref]['scope']
            if scope == 'records' and ref not in self.examined:
                remaining = self.req['max_bytes'] - self.examined_bytes
                if records_byte_exhausted or remaining < _MIN_RECORD_JSON_BYTES:
                    self.saturated.add('body-byte-budget')
                    if records_only_scope:
                        break
                    continue

            item = self._inspect(ref)
            if item is None:
                if scope == 'records' and (ref, 'body-byte-budget') in self._skips:
                    if self.req['max_bytes'] < _MIN_RECORD_JSON_BYTES:
                        records_byte_exhausted = True
                        if records_only_scope:
                            break
                    else:
                        zero_progress_record_skips += 1
                        if zero_progress_record_skips >= _MAX_ZERO_PROGRESS_RECORD_BYTE_SKIPS:
                            records_byte_exhausted = True
                            if records_only_scope:
                                break
                continue

            if scope == 'records':
                zero_progress_record_skips = 0

            metadata = item['metadata'].casefold()
            metadata_match = all(t in metadata for t in terms)
            body = None
            if terms and not metadata_match and self.req['content']:
                body = self._body(ref)

            searchable = metadata + ('\n' + body.casefold() if body is not None else '')
            if all(term in searchable for term in terms):
                self._candidate(
                    item,
                    {
                        'kind': 'lexical' if terms else 'inventory',
                        'terms': terms,
                        'channel': 'metadata' if metadata_match else 'metadata-and-complete-body',
                    },
                )

    def _record_edges(self, item):
        return extract_record_edges(self, item)

    def _incoming(self):
        return build_incoming_edges(self)

    def _traverse(self):
        traverse_anchors(self)
