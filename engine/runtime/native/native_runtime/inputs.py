"""Bounded native file reads, strict user input, and durable turn inputs."""
from __future__ import annotations

import errno
import json
import os
from pathlib import Path
from stat import S_ISREG
from typing import Any

from continuation_support.storage import atomic_bytes
from continuation_support.ownership import relative_path

_OPEN_RO_NOFOLLOW = (
    os.O_RDONLY
    | getattr(os, 'O_NOFOLLOW', 0)
    | getattr(os, 'O_NONBLOCK', 0)
    | getattr(os, 'O_NOCTTY', 0)
)


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


def _ordinary_file_size(path: Path, role: str) -> int:
    try:
        info = path.lstat()
    except (FileNotFoundError, NotADirectoryError):
        pass
    else:
        if S_ISREG(info.st_mode):
            return info.st_size
    raise ValueError(f'ordinary {role} file required: {path}')


def _check_limit(limit: int) -> None:
    if type(limit) is not int or limit < 0:
        raise ValueError('source byte limit must be a nonnegative integer')


def _open_source(name, flags):
    return os.open(name, flags | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_NOCTTY)


def _bounded_bytes(path: Path, limit: int, size: int) -> bytes:
    if size > limit:
        raise ValueError(f'source exceeds byte bound: {path}')
    with open(path, 'rb', opener=_open_source) as stream:
        if not S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError(f'ordinary source file required: {path}')
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


def _open_ordinary_bytes(path: Path, role: str, *, limit: int | None = None,
                         dir_fd: int | None = None, filename: str | None = None) -> bytes:
    if limit is not None:
        _check_limit(limit)
    if hasattr(os, 'O_NOFOLLOW'):
        target = filename if (dir_fd is not None and filename is not None) else path
        try:
            fd = os.open(target, _OPEN_RO_NOFOLLOW, dir_fd=dir_fd)
        except OSError as exc:
            if exc.errno in (errno.ELOOP, errno.ENOENT, errno.ENOTDIR, errno.EISDIR, errno.ENXIO):
                raise ValueError(f'ordinary {role} file required: {path}') from None
            raise
        try:
            info = os.fstat(fd)
            if not S_ISREG(info.st_mode):
                raise ValueError(f'ordinary {role} file required: {path}')
            if limit is not None and info.st_size > limit:
                raise ValueError(f'source exceeds byte bound: {path}')
            stream = os.fdopen(fd, 'rb')
        except BaseException:
            os.close(fd)
            raise
        with stream:
            if limit is None:
                return stream.read()
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
    size = _ordinary_file_size(path, role)
    if limit is None:
        return path.read_bytes()
    return _bounded_bytes(path, limit, size)


def bounded_source_bytes(path: Path, limit: int) -> bytes:
    """Read an ordinary source with bounded chunks and one growth sentinel."""
    return _open_ordinary_bytes(path, 'source', limit=limit)


def parse_input_bytes(data: bytes, path: Path | str) -> dict:
    obj = json.loads(data.decode('utf-8'), object_pairs_hook=unique_json,
                     parse_constant=lambda value: (_ for _ in ()).throw(ValueError('nonfinite JSON: ' + value)))
    if not isinstance(obj, dict):
        raise ValueError(f'JSON object required: {path}')
    return obj


def read_input(path: Path, *, byte_limit: int | None = None,
               dir_fd: int | None = None, filename: str | None = None) -> tuple[dict, bytes]:
    data = _open_ordinary_bytes(path, 'JSON', limit=byte_limit, dir_fd=dir_fd, filename=filename)
    return parse_input_bytes(data, path), data


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
