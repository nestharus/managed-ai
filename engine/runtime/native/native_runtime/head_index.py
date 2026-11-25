"""Portable HMAC-authenticated store-local record head index (no SQLite, domain-neutral)."""
from __future__ import annotations

import hmac
import json
import os
from pathlib import Path
import re

from continuation_support.storage import atomic_bytes, has_ordinary_file, sha
from native_runtime.formats import (
    HEAD_INDEX_FORMAT as INDEX_FORMAT,
    HEAD_INDEX_FORMAT_V1 as INDEX_FORMAT_V1,
    HEAD_INDEX_SUPPORTED_FORMATS,
)
from native_runtime.inputs import parse_input_bytes

INDEX_DOMAIN = b'g60/verified-head-index/1\0'
_WORD_RE = re.compile(r'\w+')
_MAX_TOKENS_PER_RECORD = 512
_MAX_EXACT_REF_LEN = 512


def _compact_json(value: dict) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8')


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
        seen_rel_json: set[bytes] = set()
        reported_dup_pairs: set[tuple[str, str]] = set()
        for rel in relations:
            if isinstance(rel, dict):
                label = rel.get('relation')
                target = rel.get('target')
                if isinstance(label, str) and isinstance(target, str):
                    rel_pairs.append([label, target])
                    rel_bytes = _compact_json(rel)
                    if rel_bytes in seen_rel_json and (label, target) not in reported_dup_pairs:
                        reported_dup_pairs.add((label, target))
                        exact_dup_pairs.append([label, target])
                    seen_rel_json.add(rel_bytes)

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
        'exact_refs': exact_refs,
        'tokens': tokens,
        'graph': extract_graph_hints(record),
    }
    if tokens_truncated:
        entry['tokens_truncated'] = True
    if exact_refs_truncated:
        entry['exact_refs_truncated'] = True
    return entry


class HeadIndex:
    """Store-local HMAC-verified head index under exchange/index/head-index.json.

    Read-only operations never create the owner key or write the index. Any HMAC
    mismatch, missing key, or filesystem metadata mismatch falls back to strict
    RecordStore.read() verification.
    """

    def __init__(self, owner: Path, receipts):
        self.directory = owner / 'index'
        self.path = self.directory / 'head-index.json'
        self.receipts = receipts

    def load_with_format(self, record_format: str) -> tuple[dict[str, dict], str] | None:
        """Read and HMAC-verify the stored index, returning (entries, index_format) or None."""
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
        if raw.startswith(b'{"hmac":"') and len(raw) > 76 and raw[73:75] == b'",':
            try:
                tag = raw[9:73].decode('ascii')
            except UnicodeDecodeError:
                tag = ''
            if len(tag) == 64 and all(c in '0123456789abcdef' for c in tag):
                payload = b'{' + raw[75:]
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
                    return doc['entries'], fmt
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
        return entries, fmt

    def load(self, record_format: str) -> dict[str, dict] | None:
        """Read and HMAC-verify the stored index without creating keys or files."""
        loaded = self.load_with_format(record_format)
        return None if loaded is None else loaded[0]

    def save(self, record_format: str, entries: dict[str, dict], *, create_key: bool = True) -> dict:
        """Atomically write the HMAC-signed compact JSON index under an exclusive store lock."""
        key = self.receipts._key(create=create_key)
        if key is None:
            raise ValueError('head index key is unavailable')
        body = {
            'format': INDEX_FORMAT,
            'record_format': record_format,
            'entries': entries,
        }
        payload = _compact_json(body)
        tag = hmac.digest(key, INDEX_DOMAIN + payload, 'sha256').hex()
        signed_bytes = b'{"hmac":"' + tag.encode('ascii') + b'",' + payload[1:]
        self.directory.mkdir(parents=True, exist_ok=True)
        atomic_bytes(self.path, signed_bytes)
        return {'format': INDEX_FORMAT, 'indexed_records': len(entries), 'path': str(self.path)}

    def update_if_present(self, record_format: str, updates: dict[str, dict]) -> bool:
        """Incrementally update entries when an authentic index already exists."""
        if not updates or not self.path.exists():
            return False
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
        """Remove cold-archived record entries when an authentic index already exists."""
        if not removed_ids or not self.path.exists():
            return False
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
