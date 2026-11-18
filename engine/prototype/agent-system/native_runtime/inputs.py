"""Bounded native file reads, strict user input, and durable turn inputs."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from continuation_support.storage import atomic_bytes
from continuation_support.ownership import relative_path


def text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f'{label} must be nonempty text')
    return value


def accounted_closure(content: Any) -> bool:
    return (isinstance(content, dict) and content.get('disposition') in ('fulfilled', 'released') and
            isinstance(content.get('account'), str) and bool(content['account'].strip()))


def strings(value: Any, label: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(v, str) or not v for v in value):
        raise ValueError(f'{label} must be a list of nonempty strings')
    if len(value) != len(set(value)):
        raise ValueError(f'{label} contains duplicates')
    return value


def integer(value: Any, label: str, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise ValueError(f'{label} must be an integer >= {minimum}')
    return value


def unique_json(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate JSON key: ' + key)
        result[key] = value
    return result


def bounded_source_bytes(path: Path, limit: int) -> bytes:
    """Read an ordinary source with bounded chunks and one growth sentinel."""
    if type(limit) is not int or limit < 0:
        raise ValueError('source byte limit must be a nonnegative integer')
    if path.is_symlink() or not path.is_file():
        raise ValueError(f'ordinary source file required: {path}')
    if path.stat().st_size > limit:
        raise ValueError(f'source exceeds byte bound: {path}')
    with path.open('rb') as stream:
        chunks, remaining = [], limit + 1
        while remaining:
            chunk = stream.read(min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
    data = b''.join(chunks)
    if len(data) > limit:
        raise ValueError(f'source grew beyond byte bound: {path}')
    return data


def read_input(path: Path, *, byte_limit: int | None = None) -> tuple[dict, bytes]:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f'ordinary JSON file required: {path}')
    data = path.read_bytes() if byte_limit is None else bounded_source_bytes(path, byte_limit)
    obj = json.loads(data.decode('utf-8'), object_pairs_hook=unique_json,
                     parse_constant=lambda value: (_ for _ in ()).throw(ValueError('nonfinite JSON: ' + value)))
    if not isinstance(obj, dict):
        raise ValueError(f'JSON object required: {path}')
    return obj, data


def read(path: Path) -> dict:
    return read_input(path)[0]


def fields(obj: dict, allowed: set[str], label: str):
    extra = set(obj) - allowed
    if extra:
        raise ValueError(f'unknown {label} fields: ' + ', '.join(sorted(extra)))


def artifact_target(value: str) -> str:
    value = relative_path(value)
    if value.split('/', 1)[0] in {'records', 'interpretations', '.system'}:
        raise ValueError('owned system paths use put/interpret; propose ordinary artifact targets')
    return value

def write_input(work: Path, directory: Path, name: str, data: bytes) -> str:
    path = directory / name
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_bytes(path, data)
    return path.relative_to(work).as_posix()
