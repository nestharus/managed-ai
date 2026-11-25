"""Location and authority at effect boundaries for native and standalone workspaces."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Protocol

from .formats import STORE_FORMAT
from .storage import now, read_json, write_json


_PACKET_ID_RE = re.compile(r'^[A-Za-z0-9_-]{1,120}\Z')


def packet_id(value: str) -> str:
    if not isinstance(value, str) or not _PACKET_ID_RE.match(value):
        raise ValueError('identity must contain 1..120 letters, digits, hyphens or underscores')
    return value


def relative_path(value: str) -> str:
    p = Path(value)
    if not value or p.is_absolute() or '..' in p.parts or '\\' in value or str(p) in {'.', ''}:
        raise ValueError('a nonempty relative workspace path without traversal is required')
    return p.as_posix()


def external_root(root: Path, role: str = 'runtime root', *, module_file: str | None = None) -> Path:
    """Ensure runtime/exchange roots stay strictly outside the distributed engine tree."""
    root = root.expanduser().resolve()
    package = Path(module_file or __file__).resolve().parent
    # Protect the entire engine/ distribution when running from source, or the
    # installed core package directory when installed as a standalone wheel.
    parent = package.parent
    if (parent / 'CURRENT-RELEASE.json').is_file() or (
            (parent / 'deployment' / 'controller.py').is_file() and (parent / 'store' / 'system.py').is_file()):
        protected = parent
    else:
        protected = package
    if root == protected or root.is_relative_to(protected):
        raise ValueError(f'{role} must be outside the distributed package')
    return root


@dataclass(frozen=True)
class NativeLayout:
    root: Path

    FORMAT = STORE_FORMAT
    WORK = 'work'
    EXCHANGE = 'exchange'
    DESCRIPTOR = 'system.json'

    @property
    def workspace(self) -> Path:
        return self.root / self.WORK

    @property
    def exchange(self) -> Path:
        return self.root / self.EXCHANGE

    @property
    def descriptor(self) -> Path:
        return self.root / self.DESCRIPTOR

    def read_descriptor(self, reader=read_json) -> dict:
        record = reader(self.descriptor)
        if record.get('format') != self.FORMAT:
            raise ValueError('unsupported agent-system format')
        return record

    def write_descriptor(self, *, app_version: str, core_version: str, **selection) -> None:
        write_json(self.descriptor, {'format': self.FORMAT, 'app_version': app_version,
                   'core_version': core_version, 'created_at': now(), **selection})

    @classmethod
    def owner_of(cls, path: Path) -> Path | None:
        """Find the existing native declaration along the actual effect destination."""
        for part in (path, *path.parents):
            if part.name not in {cls.WORK, cls.EXCHANGE} and not (part == path and part.name == cls.DESCRIPTOR):
                continue
            layout = cls(part.parent)
            if ((layout.workspace / '.system').is_dir() and layout.descriptor.is_file()
                    and (layout.exchange / 'workspace.json').is_file()
                    and read_json(layout.descriptor).get('format') == cls.FORMAT):
                return layout.root
        return None


class EffectAuthority(Protocol):
    def authorize_dispatch(self, owner: Path | None, packet: str) -> None: ...

    def authorize_publication(self, owner: Path | None, packet: str, target: str) -> None: ...


class StandaloneAuthority:
    def authorize_dispatch(self, owner: Path | None, packet: str) -> None:
        if owner is not None:
            raise ValueError(f'dispatch addresses a native runtime; use manage_store.py --root {owner} '
                             'run for a native turn, or choose a standalone workspace')

    def authorize_publication(self, owner: Path | None, packet: str, target: str) -> None:
        if owner is not None:
            raise ValueError(f'publish addresses a native runtime; use manage_store.py --root {owner} '
                             'adopt for a native turn, or choose a standalone workspace')
