"""Bind nominated proposal files to their original directory and checked file descriptors."""
from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
from stat import S_ISREG
from typing import Any

from ..ownership import relative_path as _relative
from ..storage import MAX_BYTES, is_ordinary_dir_stat, is_ordinary_file_stat


def _run_nominations(producer: str, mappings: dict[str, str]) -> dict:
    """The frozen target is text. A file operand and its physical origin are OS bytes."""
    origin = None
    if any(not os.path.isabs(source) for source in mappings.values()):
        cwd = os.stat('.')
        origin = {'device': cwd.st_dev, 'inode': cwd.st_ino, 'path_hex': os.getcwdb().hex()}

    return {
        'version': 1,
        'producer': producer,
        'origin': origin,
        'files': [[target, os.fsencode(source).hex()] for target, source in mappings.items()],
    }


def _components(path: bytes):
    if not os.path.isabs(path) or b'\0' in path:
        raise ValueError('absolute proposal origin required')

    yield b'//' if path.startswith(b'//') and not path.startswith(b'///') else b'/'
    yield from filter(None, path.split(b'/'))


@contextmanager
def _proposal_directory(origin):
    """Walk the original directory once. Each lookup uses just one component."""
    if origin is None:
        yield None
        return

    path = bytes.fromhex(origin['path_hex'])
    identity = origin['device'], origin['inode']

    if not hasattr(os, 'O_DIRECTORY'):
        path_text = os.fsdecode(path)
        if not os.path.isabs(path_text) or '\0' in path_text:
            raise ValueError('absolute proposal origin required')

        target = Path(path_text)
        try:
            reached = target.lstat()
        except OSError as exc:
            raise ValueError('proposal origin directory unavailable') from exc

        if not is_ordinary_dir_stat(reached) or (reached.st_dev, reached.st_ino) != identity:
            raise ValueError('proposal origin directory changed')

        yield target
        return

    search = getattr(os, 'O_SEARCH', getattr(os, 'O_PATH', None))
    directory = None
    try:
        if search is None:
            previous = os.getcwdb()
            try:
                for part in _components(path):
                    os.chdir(part)
                directory = os.open(b'.', os.O_RDONLY | os.O_DIRECTORY)
            finally:
                for part in _components(previous):
                    os.chdir(part)
        else:
            for part in _components(path):
                child = os.open(part, search | os.O_DIRECTORY, dir_fd=directory)
                if directory is not None:
                    os.close(directory)
                directory = child

        reached = os.fstat(directory)
        if (reached.st_dev, reached.st_ino) != identity:
            raise ValueError('proposal origin directory changed')
        yield directory
    finally:
        if directory is not None:
            os.close(directory)


def _proposal_bytes(source: str, directory: int | Path | None = None) -> bytes:
    """Read the checked regular file through the same descriptor (POSIX) or verified path (Windows)."""
    if isinstance(directory, Path) or (directory is None and not hasattr(os, 'O_NOFOLLOW')):
        target = Path(source)
        if not target.is_absolute() and isinstance(directory, Path):
            target = directory / target

        try:
            expected = target.lstat()
        except (FileNotFoundError, NotADirectoryError) as exc:
            raise ValueError(f'ordinary file required: {source}') from exc

        if not is_ordinary_file_stat(expected):
            raise ValueError(f'ordinary file required: {source}')

        with target.open('rb') as stream:
            opened = os.fstat(stream.fileno())
            if not S_ISREG(opened.st_mode) or not os.path.samestat(expected, opened):
                raise ValueError(f'proposal file changed during lookup: {source}')
            body = stream.read(MAX_BYTES + 1)

        if len(body) > MAX_BYTES:
            raise ValueError(f'file exceeds {MAX_BYTES} bytes: {source}')
        return body

    try:
        expected = os.stat(source, dir_fd=directory, follow_symlinks=False)
    except (FileNotFoundError, NotADirectoryError) as exc:
        raise ValueError(f'ordinary file required: {source}') from exc

    if not S_ISREG(expected.st_mode):
        raise ValueError(f'ordinary file required: {source}')

    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_NOCTTY
    fd = os.open(source, flags, dir_fd=directory)
    try:
        opened = os.fstat(fd)
        if not S_ISREG(opened.st_mode) or not os.path.samestat(expected, opened):
            raise ValueError(f'proposal file changed during lookup: {source}')
        with os.fdopen(fd, 'rb', closefd=False) as stream:
            body = stream.read(MAX_BYTES + 1)
    finally:
        os.close(fd)

    if len(body) > MAX_BYTES:
        raise ValueError(f'file exceeds {MAX_BYTES} bytes: {source}')
    return body


def proposal_mappings(request: dict[str, Any], proposals: list[str]) -> dict[str, str]:
    declared = {row['path'] for row in request['targets']}
    mappings = {}

    for item in proposals:
        target, sep, source = item.partition('=')
        target = _relative(target)
        if not sep or not source or '\0' in source or target not in declared or target in mappings:
            raise ValueError('proposal must name one unique declared target as target=file')
        mappings[target] = source

    return mappings
