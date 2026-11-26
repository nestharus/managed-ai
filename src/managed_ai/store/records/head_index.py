"""Portable HMAC-authenticated store-local record head index (no SQLite, domain-neutral)."""
from __future__ import annotations

import hmac
import json
import os
from pathlib import Path
import re

from managed_ai.core.formats import (
    HEAD_INDEX_FORMAT as INDEX_FORMAT,
    HEAD_INDEX_FORMAT_V1 as INDEX_FORMAT_V1,
    HEAD_INDEX_SUPPORTED_FORMATS,
)
from managed_ai.core.storage import (
    atomic_bytes,
    has_ordinary_file,
    is_ordinary_file_stat,
    sha,
    sync_dir,
)
from managed_ai.platform import PLATFORM, strict_json_loads
from managed_ai.store.protocol.inputs import parse_input_bytes
from managed_ai.store.records.head_index_bin import (
    HEAD_INDEX_FORMAT_V3,
    HeadIndexView,
    active_binary_path,
    open_binary_view,
    read_current_pointer,
    write_binary_index,
)

INDEX_DOMAIN = b'g60/verified-head-index/1\0'
DELTA_DOMAIN = b'g60/verified-head-index-delta/1\0'
_MAX_DELTA_OPS = 64
_MAX_DELTA_BYTES = 512 * 1024
_WORD_RE = re.compile(r'\w+')
_MAX_TOKENS_PER_RECORD = 512
_MAX_EXACT_REF_LEN = 512

_NON_LOCAL_CONTENT_ID_KEYS = frozenset({
    'id',
    'instance_id',
    'store_id',
    'session_id',
    'thread_id',
    'native_session_id',
})
_FOREIGN_STORE_QUALIFIERS = (
    'instance_id',
    'store_id',
    'source_instance_id',
    'source_instance',
)


def _compact_json(value: dict) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        separators=(',', ':'),
        allow_nan=False,
    ).encode('utf-8')


def _incremental_hmac(key: bytes, domain: bytes, *chunks: bytes | memoryview) -> str:
    mac = hmac.new(key, domain, 'sha256')
    for chunk in chunks:
        mac.update(chunk)
    return mac.hexdigest()


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

    def add_exact(text_value: str) -> None:
        nonlocal exact_refs_truncated
        cf = text_value.casefold()
        if len(cf) <= _MAX_EXACT_REF_LEN:
            exact_refs.add(cf)
        else:
            exact_refs_truncated = True

    def add_tokens(text_value: str) -> None:
        nonlocal tokens_truncated
        if tokens_truncated:
            return
        for word in _WORD_RE.findall(text_value.casefold()):
            tokens.add(word)
            if len(tokens) > _MAX_TOKENS_PER_RECORD:
                tokens_truncated = True
                tokens.clear()
                return

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


def _extract_relation_pairs(record: dict) -> tuple[list[list[str]], list[list[str]]]:
    rel_pairs: list[list[str]] = []
    exact_dup_pairs: list[list[str]] = []
    relations = record.get('relations')
    if not isinstance(relations, list):
        return rel_pairs, exact_dup_pairs

    first_by_pair: dict[tuple[str, str], dict] = {}
    seen_rel_json: dict[tuple[str, str], set[bytes]] = {}
    reported_dup_pairs: set[tuple[str, str]] = set()

    for rel in relations:
        if not isinstance(rel, dict):
            continue
        label = rel.get('relation')
        target = rel.get('target')
        if not (isinstance(label, str) and isinstance(target, str)):
            continue
        rel_pairs.append([label, target])
        pair = (label, target)
        if pair not in first_by_pair:
            first_by_pair[pair] = rel
            continue
        bucket = seen_rel_json.get(pair)
        if bucket is None:
            bucket = {_compact_json(first_by_pair[pair])}
            seen_rel_json[pair] = bucket
        rel_bytes = _compact_json(rel)
        if rel_bytes in bucket and pair not in reported_dup_pairs:
            reported_dup_pairs.add(pair)
            exact_dup_pairs.append([label, target])
        bucket.add(rel_bytes)

    return rel_pairs, exact_dup_pairs


def extract_graph_hints(record: dict) -> dict:
    """Extract compact domain-neutral adjacency, content-link and structural hints."""
    rel_pairs, exact_dup_pairs = _extract_relation_pairs(record)

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

        raw_state = (
            content.get('selection')
            or content.get('disposition')
            or content.get('recorded_status')
            or content.get('activity')
            or ''
        )
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
        'platform': PLATFORM.platform_key,
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
    """Store-local HMAC-verified head index (binary mmap v3 + legacy JSON fallback + HMAC delta sidecar).

    Read-only operations never create the owner key or write the index. Any HMAC
    mismatch, missing key, or filesystem metadata mismatch falls back to strict
    RecordStore.read() verification.
    """

    def __init__(self, owner: Path, receipts):
        self.directory = owner / 'index'
        self.path = self.directory / 'head-index.json'
        self.current_path = self.directory / 'head-index.current.json'
        self.delta_path = self.directory / 'head-index.delta.jsonl'
        self.receipts = receipts

    def exists(self) -> bool:
        """Return True if either binary v3 current pointer or legacy JSON head index exists."""
        return self.current_path.exists() or self.path.exists()

    def binary_path(self) -> Path | None:
        """Return the active immutable head-index.gen<N>.bin path if present."""
        return active_binary_path(self.directory)

    @staticmethod
    def _extract_fast_tag(raw: bytes) -> str | None:
        if raw.startswith(b'{"hmac":"') and len(raw) > 76 and raw[73:75] == b'",':
            try:
                tag = raw[9:73].decode('ascii')
            except UnicodeDecodeError:
                return None
            if len(tag) == 64 and all(c in '0123456789abcdef' for c in tag):
                return tag
        return None

    @classmethod
    def _parse_fast_header(cls, raw: bytes) -> tuple[str, bytes] | None:
        tag = cls._extract_fast_tag(raw)
        if tag is None:
            return None
        return tag, b'{' + raw[75:]

    def _read_base_tag(self, record_format: str | None = None) -> str | None:
        """Read the base HMAC tag from head-index.current.json (v3) or head-index.json (legacy)."""
        pointer = read_current_pointer(self.directory)
        if pointer is not None:
            if record_format is not None and pointer.get('record_format') != record_format:
                return None
            if has_ordinary_file(self.directory / pointer['file']):
                return pointer['hmac']
            return None

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

        return self._extract_fast_tag(head + b'x')

    def _read_delta_overlay(
        self,
        key: bytes,
        base_tag: str,
        record_format: str,
    ) -> tuple[dict[str, dict], set[str]] | None:
        """Verify and parse exchange/index/head-index.delta.jsonl into (puts, dels), or None on tamper."""
        try:
            info = self.delta_path.lstat()
        except FileNotFoundError:
            return {}, set()
        except OSError:
            return None

        if not is_ordinary_file_stat(info):
            return None

        try:
            raw_lines = self.delta_path.read_bytes().splitlines()
        except OSError:
            return None

        puts: dict[str, dict] = {}
        dels: set[str] = set()

        for line in raw_lines:
            if not line:
                continue

            tag = self._extract_fast_tag(line)
            if tag is None:
                return None

            expected = _incremental_hmac(key, DELTA_DOMAIN, b'{', memoryview(line)[75:])
            if not hmac.compare_digest(tag, expected):
                return None

            try:
                row = strict_json_loads(line, self.delta_path, require_object=True)
            except (UnicodeDecodeError, ValueError):
                return None

            if (
                row.get('base_hmac') != base_tag
                or row.get('record_format') != record_format
            ):
                return None

            op = row.get('op')
            if op == 'put' and isinstance(row.get('entries'), dict):
                for ident, entry in row['entries'].items():
                    puts[ident] = entry
                    dels.discard(ident)
            elif op == 'del' and isinstance(row.get('ids'), list):
                for ident in row['ids']:
                    if isinstance(ident, str):
                        puts.pop(ident, None)
                        dels.add(ident)
            else:
                return None

        return puts, dels

    def _apply_delta(
        self,
        key: bytes,
        base_tag: str,
        record_format: str,
        entries: dict[str, dict],
    ) -> bool:
        """Verify and fold exchange/index/head-index.delta.jsonl into entries. Returns False on any tamper."""
        overlay = self._read_delta_overlay(key, base_tag, record_format)
        if overlay is None:
            return False

        puts, dels = overlay
        for ident in dels:
            entries.pop(ident, None)
        entries.update(puts)
        return True

    def open_view(self, record_format: str) -> HeadIndexView | None:
        """Open a zero-copy mmap HeadIndexView over binary v3 (+ verified delta overlay) or return None."""
        key = self.receipts._key(create=False)
        if key is None:
            return None

        view = open_binary_view(self.directory, key, record_format)
        if view is None:
            return None

        overlay = self._read_delta_overlay(key, view.hmac_hex, record_format)
        if overlay is None:
            view.close()
            return None

        puts, dels = overlay
        view.delta_puts = puts
        view.delta_dels = dels
        return view

    def get_entry(self, record_format: str, ident: str) -> dict | None:
        """O(log N) single-record lookup via binary v3 mmap, falling back to legacy JSON if needed."""
        view = self.open_view(record_format)
        if view is not None:
            with view:
                return view.get_entry(ident)

        entries = self.load(record_format)
        return None if entries is None else entries.get(ident)

    def _load_legacy_envelope(
        self,
        key: bytes,
        raw: bytes,
        record_format: str,
    ) -> tuple[dict[str, dict], str] | None:
        """Backward-compatible fallback for Wave 1 sorted-key JSON envelopes."""
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

        expected = _incremental_hmac(key, INDEX_DOMAIN, payload)
        if not hmac.compare_digest(tag, expected):
            return None

        if not self._apply_delta(key, tag, record_format, entries):
            return None

        return entries, fmt

    def _load_json_with_format(self, key: bytes, record_format: str) -> tuple[dict[str, dict], str] | None:
        """Read and HMAC-verify legacy head-index.json (+ delta sidecar)."""
        try:
            if not has_ordinary_file(self.path):
                return None
            raw = self.path.read_bytes()
        except OSError:
            return None

        tag = self._extract_fast_tag(raw)
        if tag is not None:
            expected = _incremental_hmac(key, INDEX_DOMAIN, b'{', memoryview(raw)[75:])
            if not hmac.compare_digest(tag, expected):
                return None

            try:
                doc = strict_json_loads(raw, self.path, require_object=True)
            except (UnicodeDecodeError, ValueError):
                return None
            finally:
                raw = b''

            fmt = doc.get('format')
            if (
                fmt in HEAD_INDEX_SUPPORTED_FORMATS
                and doc.get('record_format') == record_format
                and isinstance(doc.get('entries'), dict)
            ):
                entries = doc['entries']
                if not self._apply_delta(key, tag, record_format, entries):
                    return None
                return entries, fmt
            return None

        return self._load_legacy_envelope(key, raw, record_format)

    def load_with_format(self, record_format: str) -> tuple[dict[str, dict], str] | None:
        """Read and HMAC-verify binary v3 or legacy JSON index (+ delta), returning (entries, format) or None."""
        key = self.receipts._key(create=False)
        if key is None:
            return None

        view = self.open_view(record_format)
        if view is not None:
            with view:
                return view.all_entries(), INDEX_FORMAT

        return self._load_json_with_format(key, record_format)

    def load(self, record_format: str) -> dict[str, dict] | None:
        """Read and HMAC-verify the stored index without creating keys or files."""
        loaded = self.load_with_format(record_format)
        return None if loaded is None else loaded[0]

    def save(self, record_format: str, entries: dict[str, dict], *, create_key: bool = True) -> dict:
        """Atomically publish a new immutable binary mmap HeadIndex v3 generation and clear delta sidecar."""
        key = self.receipts._key(create=create_key)
        if key is None:
            raise ValueError('head index key is unavailable')
        return write_binary_index(self.directory, key, record_format, entries)

    def save_json(self, record_format: str, entries: dict[str, dict], *, create_key: bool = True) -> dict:
        """Atomically write the legacy HMAC-signed compact JSON index and clear any delta sidecar."""
        key = self.receipts._key(create=create_key)
        if key is None:
            raise ValueError('head index key is unavailable')

        body = {
            'format': INDEX_FORMAT,
            'record_format': record_format,
            'platform': PLATFORM.platform_key,
            'entries': entries,
        }
        payload = _compact_json(body)
        tag = _incremental_hmac(key, INDEX_DOMAIN, payload)
        prefix = b'{"hmac":"' + tag.encode('ascii') + b'",'

        self.directory.mkdir(parents=True, exist_ok=True)
        atomic_bytes(self.path, (prefix, memoryview(payload)[1:]))

        for obsolete in (self.current_path, self.delta_path):
            try:
                if has_ordinary_file(obsolete):
                    obsolete.unlink(missing_ok=True)
            except OSError:
                pass
        try:
            sync_dir(self.directory)
        except OSError:
            pass

        return {'format': INDEX_FORMAT, 'indexed_records': len(entries), 'path': str(self.path)}

    def _append_delta(self, key: bytes, base_tag: str, row: dict) -> bool:
        """Append one HMAC-signed delta line; return False if compaction into head-index.gen<N+1>.bin is needed."""
        payload = _compact_json(row)
        tag = _incremental_hmac(key, DELTA_DOMAIN, payload)
        line = b'{"hmac":"' + tag.encode('ascii') + b'",' + payload[1:] + b'\n'

        try:
            info = self.delta_path.lstat()
        except FileNotFoundError:
            info = None
        except OSError:
            return False

        if info is not None:
            if not is_ordinary_file_stat(info):
                return False
            if info.st_size + len(line) > _MAX_DELTA_BYTES:
                return False
            try:
                raw_existing = self.delta_path.read_bytes()
            except OSError:
                return False
            if raw_existing.count(b'\n') + 1 > _MAX_DELTA_OPS:
                return False
            first = raw_existing.split(b'\n', 1)[0]
            if first and (b'"base_hmac":"' + base_tag.encode('ascii') + b'"') not in first:
                return False

        if len(line) > _MAX_DELTA_BYTES:
            return False

        created = info is None
        try:
            if hasattr(os, 'O_NOFOLLOW'):
                fd = os.open(
                    self.delta_path,
                    os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW,
                    0o644,
                )
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
        """Incrementally update entries via HMAC delta sidecar (auto-compacting into v3 gen<N+1>)."""
        if not updates or not self.exists():
            return False

        key = self.receipts._key(create=False)
        if key is None:
            return False

        base_tag = self._read_base_tag(record_format)
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
        """Remove cold-archived record entries via HMAC delta sidecar (auto-compacting into v3 gen<N+1>)."""
        if not removed_ids or not self.exists():
            return False

        key = self.receipts._key(create=False)
        if key is None:
            return False

        base_tag = self._read_base_tag(record_format)
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
