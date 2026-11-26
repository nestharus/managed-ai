"""Portable HMAC-authenticated store-local record head index (no SQLite, domain-neutral)."""
from __future__ import annotations

import hmac
import json
import os
from pathlib import Path
import re

from managed_ai.core.storage import atomic_bytes, has_ordinary_file, is_ordinary_file_stat, sha, sync_dir
from managed_ai.core.formats import (
    HEAD_INDEX_FORMAT as INDEX_FORMAT,
    HEAD_INDEX_FORMAT_V1 as INDEX_FORMAT_V1,
    HEAD_INDEX_SUPPORTED_FORMATS,
)
from managed_ai.store.protocol.inputs import parse_input_bytes

INDEX_DOMAIN = b'g60/verified-head-index/1\0'
DELTA_DOMAIN = b'g60/verified-head-index-delta/1\0'
_MAX_DELTA_OPS = 64
_MAX_DELTA_BYTES = 512 * 1024
_WORD_RE = re.compile(r'\w+')
_MAX_TOKENS_PER_RECORD = 512
_MAX_EXACT_REF_LEN = 512


def _compact_json(value: dict) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8')


def entry_matches_exact_ref(entry: dict, query_cf: str) -> bool:
    """Match a casefolded exact reference against deduplicated exact_refs plus id/title/relation targets."""
    if entry.get('exact_refs_truncated'):
        return True
    if query_cf in entry.get('exact_refs', ()):
        return True
    ident = entry.get('id')
    if isinstance(ident, str) and ident.casefold() == query_cf:
        return True
    graph = entry.get('graph')
    if isinstance(graph, dict):
        title = graph.get('title')
        if isinstance(title, str) and title and title.casefold() == query_cf:
            return True
        for pair in graph.get('relations', ()):
            if len(pair) >= 2 and isinstance(pair[1], str) and pair[1].casefold() == query_cf:
                return True
    return False


def _extract_search_hints(record: dict) -> tuple[list[str], list[str], bool, bool]:
    """Extract domain-neutral exact references and lexical word tokens from one verified record."""
    exact_refs: set[str] = set()
    tokens: set[str] = set()
    tokens_truncated = False
    exact_refs_truncated = False

    def add_exact(text: str) -> None:
        nonlocal exact_refs_truncated
        cf = text.casefold()
        if len(cf) <= _MAX_EXACT_REF_LEN:
            exact_refs.add(cf)
        else:
            exact_refs_truncated = True

    def add_tokens(text: str) -> None:
        nonlocal tokens_truncated
        if tokens_truncated:
            return
        for word in _WORD_RE.findall(text.casefold()):
            tokens.add(word)
            if len(tokens) > _MAX_TOKENS_PER_RECORD:
                tokens_truncated = True
                tokens.clear()
                return

    # Always index record identity as fallback title/exact reference.
    rec_id = record.get('id')
    if isinstance(rec_id, str) and rec_id:
        add_exact(rec_id)
        add_tokens(rec_id)

    content = record.get('content')
    if isinstance(content, dict):
        for key, value in content.items():
            if isinstance(value, str):
                if key in ('title', 'question', 'origin'):
                    add_exact(value)
                add_tokens(value)
            elif key in ('title', 'question') and value:
                rendered = str(value)
                add_exact(rendered)
                add_tokens(rendered)

    relations = record.get('relations')
    if isinstance(relations, list):
        for rel in relations:
            if not isinstance(rel, dict):
                continue
            target = rel.get('target')
            if isinstance(target, str):
                add_exact(target)
            origin = rel.get('origin')
            if isinstance(origin, str):
                add_exact(origin)
            aliases = rel.get('aliases')
            if isinstance(aliases, list):
                for alias in aliases:
                    if isinstance(alias, str):
                        add_exact(alias)

    return sorted(exact_refs), sorted(tokens), tokens_truncated, exact_refs_truncated


_NON_LOCAL_CONTENT_ID_KEYS = frozenset({
    'id',
    'instance_id',
    'store_id',
    'session_id',
    'thread_id',
    'native_session_id',
})
_FOREIGN_STORE_QUALIFIERS = ('instance_id', 'store_id', 'source_instance_id', 'source_instance')


def extract_content_record_links(content: object) -> dict[str, list[str]]:
    """Extract local record-ID reference fields from a record's content object.

    Excludes non-record identifiers (id, instance_id, store_id, etc.) and excludes
    foreign-store `record_id` when `content` carries a cross-instance qualifier.
    """
    if not isinstance(content, dict):
        return {}
    has_foreign_instance = any(
        isinstance(content.get(k), str) and bool(content[k].strip())
        for k in _FOREIGN_STORE_QUALIFIERS
    )
    links: dict[str, list[str]] = {}
    for key, value in content.items():
        if key in _NON_LOCAL_CONTENT_ID_KEYS:
            continue
        if key == 'record_id' and has_foreign_instance:
            continue
        if not (key.endswith(('_id', '_ids')) or key in ('directions', 'selected_question')):
            continue
        if isinstance(value, str):
            links[key] = [value]
        elif isinstance(value, list):
            items = [v for v in value if isinstance(v, str)]
            if items:
                links[key] = items
    return links


def extract_graph_hints(record: dict) -> dict:
    """Extract compact domain-neutral adjacency, content-link and structural hints."""
    rel_pairs: list[list[str]] = []
    exact_dup_pairs: list[list[str]] = []
    relations = record.get('relations')
    if isinstance(relations, list):
        first_by_pair: dict[tuple[str, str], dict] = {}
        seen_rel_json: dict[tuple[str, str], set[bytes]] = {}
        reported_dup_pairs: set[tuple[str, str]] = set()
        for rel in relations:
            if isinstance(rel, dict):
                label = rel.get('relation')
                target = rel.get('target')
                if isinstance(label, str) and isinstance(target, str):
                    rel_pairs.append([label, target])
                    pair = (label, target)
                    if pair not in first_by_pair:
                        first_by_pair[pair] = rel
                    else:
                        bucket = seen_rel_json.get(pair)
                        if bucket is None:
                            bucket = {_compact_json(first_by_pair[pair])}
                            seen_rel_json[pair] = bucket
                        rel_bytes = _compact_json(rel)
                        if rel_bytes in bucket and pair not in reported_dup_pairs:
                            reported_dup_pairs.add(pair)
                            exact_dup_pairs.append([label, target])
                        bucket.add(rel_bytes)

    content_links: dict[str, list[str]] = {}
    title = ''
    selection_state = ''
    question_norm: str | None = None
    content_format: str | None = None
    content = record.get('content')
    if isinstance(content, dict):
        raw_fmt = content.get('format')
        if isinstance(raw_fmt, str):
            content_format = raw_fmt[:120]
        raw_title = content.get('title') or content.get('question') or ''
        if raw_title:
            title = str(raw_title)[:300]
        raw_state = (content.get('selection') or content.get('disposition')
                     or content.get('recorded_status') or content.get('activity') or '')
        if raw_state:
            selection_state = str(raw_state)[:80]
        if record.get('kind') == 'question' and isinstance(content.get('question'), str):
            question_norm = ' '.join(content['question'].casefold().split())
        content_links = extract_content_record_links(content)

    hints: dict = {
        'relations': rel_pairs,
        'content_links': content_links,
        'title': title,
        'selection_state': selection_state,
    }
    if exact_dup_pairs:
        hints['duplicate_relations'] = exact_dup_pairs
    if question_norm is not None:
        hints['question_norm'] = question_norm
    if content_format is not None:
        hints['content_format'] = content_format
    return hints


def build_entry(record: dict, raw: bytes, info: os.stat_result) -> dict:
    """Build one compact domain-neutral index entry for an already verified record head."""
    exact_refs, tokens, tokens_truncated, exact_refs_truncated = _extract_search_hints(record)
    graph = extract_graph_hints(record)
    # Deduplicate exact_refs already implied by id, graph.title, and graph.relations targets.
    implied = {record['id'].casefold()}
    title = graph.get('title')
    if isinstance(title, str) and title:
        implied.add(title.casefold())
    for pair in graph.get('relations', ()):
        if len(pair) >= 2 and isinstance(pair[1], str):
            implied.add(pair[1].casefold())
    dedup_refs = [ref for ref in exact_refs if ref not in implied]
    entry = {
        'format': record['format'],
        'id': record['id'],
        'kind': record['kind'],
        'revision': record['revision'],
        'sha256': record['sha256'],
        'active': record['active'],
        'basis': record['basis'],
        'raw_sha256': sha(raw),
        'size': info.st_size,
        'mtime_ns': info.st_mtime_ns,
        'ino': info.st_ino,
        'platform': os.name,
        'exact_refs': dedup_refs,
        'tokens': tokens,
        'graph': graph,
    }
    if tokens_truncated:
        entry['tokens_truncated'] = True
    if exact_refs_truncated:
        entry['exact_refs_truncated'] = True
    return entry


class HeadIndex:
    """Store-local HMAC-verified head index under exchange/index/head-index.json (+ HMAC delta sidecar).

    Read-only operations never create the owner key or write the index. Any HMAC
    mismatch, missing key, or filesystem metadata mismatch falls back to strict
    RecordStore.read() verification.
    """

    def __init__(self, owner: Path, receipts):
        self.directory = owner / 'index'
        self.path = self.directory / 'head-index.json'
        self.delta_path = self.directory / 'head-index.delta.jsonl'
        self.receipts = receipts

    @staticmethod
    def _parse_fast_header(raw: bytes) -> tuple[str, bytes] | None:
        if raw.startswith(b'{"hmac":"') and len(raw) > 76 and raw[73:75] == b'",':
            try:
                tag = raw[9:73].decode('ascii')
            except UnicodeDecodeError:
                return None
            if len(tag) == 64 and all(c in '0123456789abcdef' for c in tag):
                return tag, b'{' + raw[75:]
        return None

    def _read_base_tag(self) -> str | None:
        """Read only the 76-byte leading HMAC tag of head-index.json without loading the full file."""
        try:
            info = self.path.lstat()
        except OSError:
            return None
        if not is_ordinary_file_stat(info) or info.st_size <= 76:
            return None
        try:
            with self.path.open('rb') as stream:
                head = stream.read(76)
        except OSError:
            return None
        parsed = self._parse_fast_header(head + b'x')
        return None if parsed is None else parsed[0]

    def _apply_delta(self, key: bytes, base_tag: str, record_format: str,
                     entries: dict[str, dict]) -> bool:
        """Verify and fold exchange/index/head-index.delta.jsonl into entries. Returns False on any tamper."""
        try:
            info = self.delta_path.lstat()
        except FileNotFoundError:
            return True
        except OSError:
            return False
        if not is_ordinary_file_stat(info):
            return False
        try:
            raw_lines = self.delta_path.read_bytes().splitlines()
        except OSError:
            return False
        for line in raw_lines:
            if not line:
                continue
            parsed = self._parse_fast_header(line)
            if parsed is None:
                return False
            tag, payload = parsed
            expected = hmac.digest(key, DELTA_DOMAIN + payload, 'sha256').hex()
            if not hmac.compare_digest(tag, expected):
                return False
            try:
                row = json.loads(payload)
            except (UnicodeDecodeError, ValueError):
                return False
            if (not isinstance(row, dict)
                    or row.get('base_hmac') != base_tag
                    or row.get('record_format') != record_format):
                return False
            op = row.get('op')
            if op == 'put' and isinstance(row.get('entries'), dict):
                entries.update(row['entries'])
            elif op == 'del' and isinstance(row.get('ids'), list):
                for ident in row['ids']:
                    if isinstance(ident, str):
                        entries.pop(ident, None)
            else:
                return False
        return True

    def load_with_format(self, record_format: str) -> tuple[dict[str, dict], str] | None:
        """Read and HMAC-verify the stored index (+ delta sidecar), returning (entries, index_format) or None."""
        key = self.receipts._key(create=False)
        if key is None:
            return None
        try:
            if not has_ordinary_file(self.path):
                return None
            raw = self.path.read_bytes()
        except OSError:
            return None

        # Fast path: valid JSON with leading `"hmac":"<64hex>",` prefix authenticated over raw payload bytes
        # BEFORE json.loads() (avoids re-serializing multi-MB JSON solely for HMAC verification).
        fast = self._parse_fast_header(raw)
        if fast is not None:
            tag, payload = fast
            expected = hmac.digest(key, INDEX_DOMAIN + payload, 'sha256').hex()
            if not hmac.compare_digest(tag, expected):
                return None
            try:
                doc = json.loads(payload)
            except (UnicodeDecodeError, ValueError):
                return None
            fmt = doc.get('format') if isinstance(doc, dict) else None
            if (isinstance(doc, dict)
                    and fmt in HEAD_INDEX_SUPPORTED_FORMATS
                    and doc.get('record_format') == record_format
                    and isinstance(doc.get('entries'), dict)):
                entries = doc['entries']
                if not self._apply_delta(key, tag, record_format, entries):
                    return None
                return entries, fmt
            return None

        # Backward-compatible fallback for Wave 1 sorted-key JSON envelope (v1 or v2).
        try:
            doc = parse_input_bytes(raw, self.path)
        except (OSError, UnicodeDecodeError, ValueError):
            return None
        if not isinstance(doc, dict):
            return None
        tag = doc.get('hmac')
        if not isinstance(tag, str) or len(tag) != 64:
            return None
        fmt = doc.get('format')
        if fmt not in HEAD_INDEX_SUPPORTED_FORMATS or doc.get('record_format') != record_format:
            return None
        entries = doc.get('entries')
        if not isinstance(entries, dict):
            return None
        body = {k: v for k, v in doc.items() if k != 'hmac'}
        try:
            payload = _compact_json(body)
        except (TypeError, ValueError):
            return None
        expected = hmac.digest(key, INDEX_DOMAIN + payload, 'sha256').hex()
        if not hmac.compare_digest(tag, expected):
            return None
        if not self._apply_delta(key, tag, record_format, entries):
            return None
        return entries, fmt

    def load(self, record_format: str) -> dict[str, dict] | None:
        """Read and HMAC-verify the stored index without creating keys or files."""
        loaded = self.load_with_format(record_format)
        return None if loaded is None else loaded[0]

    def save(self, record_format: str, entries: dict[str, dict], *, create_key: bool = True) -> dict:
        """Atomically write the HMAC-signed compact JSON index and clear any delta sidecar."""
        key = self.receipts._key(create=create_key)
        if key is None:
            raise ValueError('head index key is unavailable')
        body = {
            'format': INDEX_FORMAT,
            'record_format': record_format,
            'platform': os.name,
            'entries': entries,
        }
        payload = _compact_json(body)
        tag = hmac.digest(key, INDEX_DOMAIN + payload, 'sha256').hex()
        signed_bytes = b'{"hmac":"' + tag.encode('ascii') + b'",' + payload[1:]
        self.directory.mkdir(parents=True, exist_ok=True)
        atomic_bytes(self.path, signed_bytes)
        try:
            if has_ordinary_file(self.delta_path):
                self.delta_path.unlink(missing_ok=True)
                sync_dir(self.directory)
        except OSError:
            pass
        return {'format': INDEX_FORMAT, 'indexed_records': len(entries), 'path': str(self.path)}

    def _append_delta(self, key: bytes, base_tag: str, row: dict) -> bool:
        """Append one HMAC-signed delta line; return False if compaction into head-index.json is needed."""
        payload = _compact_json(row)
        tag = hmac.digest(key, DELTA_DOMAIN + payload, 'sha256').hex()
        line = b'{"hmac":"' + tag.encode('ascii') + b'",' + payload[1:] + b'\n'
        existing_bytes = 0
        existing_lines = 0
        try:
            info = self.delta_path.lstat()
        except FileNotFoundError:
            info = None
        except OSError:
            return False
        if info is not None:
            if not is_ordinary_file_stat(info):
                return False
            existing_bytes = info.st_size
            if existing_bytes + len(line) > _MAX_DELTA_BYTES:
                return False
            try:
                raw_existing = self.delta_path.read_bytes()
            except OSError:
                return False
            existing_lines = raw_existing.count(b'\n')
            if existing_lines + 1 > _MAX_DELTA_OPS:
                return False
            # Ensure existing delta binds the exact same base_hmac before appending.
            first = raw_existing.split(b'\n', 1)[0]
            if first and (b'"base_hmac":"' + base_tag.encode('ascii') + b'"') not in first:
                return False
        if len(line) > _MAX_DELTA_BYTES:
            return False
        created = info is None
        try:
            if hasattr(os, 'O_NOFOLLOW'):
                fd = os.open(self.delta_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o644)
                try:
                    stream = os.fdopen(fd, 'ab')
                except BaseException:
                    os.close(fd)
                    raise
            else:
                stream = self.delta_path.open('ab')
            with stream:
                stream.write(line)
                stream.flush()
                os.fsync(stream.fileno())
            if created:
                sync_dir(self.directory)
            return True
        except OSError:
            return False

    def update_if_present(self, record_format: str, updates: dict[str, dict]) -> bool:
        """Incrementally update entries via HMAC delta sidecar (auto-compacting on thresholds)."""
        if not updates or not self.path.exists():
            return False
        key = self.receipts._key(create=False)
        if key is None:
            return False
        base_tag = self._read_base_tag()
        if base_tag is not None:
            row = {
                'base_hmac': base_tag,
                'record_format': record_format,
                'op': 'put',
                'entries': updates,
            }
            if self._append_delta(key, base_tag, row):
                return True
        entries = self.load(record_format)
        if entries is None:
            return False
        entries = dict(entries)
        entries.update(updates)
        try:
            self.save(record_format, entries, create_key=False)
            return True
        except (OSError, ValueError):
            return False

    def remove_if_present(self, record_format: str, removed_ids: list[str]) -> bool:
        """Remove cold-archived record entries via HMAC delta sidecar (auto-compacting on thresholds)."""
        if not removed_ids or not self.path.exists():
            return False
        key = self.receipts._key(create=False)
        if key is None:
            return False
        base_tag = self._read_base_tag()
        if base_tag is not None:
            row = {
                'base_hmac': base_tag,
                'record_format': record_format,
                'op': 'del',
                'ids': list(removed_ids),
            }
            if self._append_delta(key, base_tag, row):
                return True
        entries = self.load(record_format)
        if entries is None:
            return False
        entries = dict(entries)
        changed = False
        for ident in removed_ids:
            if ident in entries:
                del entries[ident]
                changed = True
        if not changed:
            return False
        try:
            self.save(record_format, entries, create_key=False)
            return True
        except (OSError, ValueError):
            return False
