"""Portable HMAC-authenticated store-local record head index (no SQLite, domain-neutral)."""
from __future__ import annotations

import hmac
import json
import os
from pathlib import Path
import re

from continuation_support.storage import atomic_bytes, sha
from native_runtime.inputs import parse_input_bytes

INDEX_FORMAT = 'managed-ai-head-index/1'
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

    def load(self, record_format: str) -> dict[str, dict] | None:
        """Read and HMAC-verify the stored index without creating keys or files."""
        key = self.receipts._key(create=False)
        if key is None:
            return None
        try:
            if self.path.is_symlink() or not self.path.is_file():
                return None
            raw = self.path.read_bytes()
            doc = parse_input_bytes(raw, self.path)
        except (OSError, UnicodeDecodeError, ValueError):
            return None
        if not isinstance(doc, dict):
            return None
        tag = doc.get('hmac')
        if not isinstance(tag, str) or len(tag) != 64:
            return None
        if doc.get('format') != INDEX_FORMAT or doc.get('record_format') != record_format:
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
        return entries

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
        signed = dict(body, hmac=tag)
        self.directory.mkdir(parents=True, exist_ok=True)
        atomic_bytes(self.path, _compact_json(signed))
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
