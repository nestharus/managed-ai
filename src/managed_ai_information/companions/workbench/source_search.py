"""Verified lexical search loader, path guards, and canonical receipt helpers for SourceService."""

import builtins
import json
import os
import stat
import sys
import threading
from pathlib import Path, PurePosixPath, PureWindowsPath
from types import ModuleType
from typing import Any

from managed_ai_information.companions.workbench.io_utils import (
    canonical_bytes as encoded,
)
from managed_ai_information.companions.workbench.io_utils import (
    is_reparse_or_symlink,
    loads_strict,
)
from managed_ai_information.companions.workbench.io_utils import (
    sha256_bytes as digest,
)

VERSION = "g60-source-service/1"
PROTECTED_PREFIXES = ("evidence/fleet/D12/", "instruments/incident-corpus/D12/")
_LEDGER_CATEGORIES = ("inventories", "selections", "sources", "uses")
_SCHEMA_VALUE_BYTES = json.dumps(VERSION, ensure_ascii=False, allow_nan=False).encode("utf-8")

_RUNTIME_ENTRY = Path("library/lexical.py")
_MAX_SEARCH_FILES = 8
_MAX_SEARCH_FILE_BYTES = 256 * 1024

_SEARCH_CACHE_LOCK = threading.Lock()
_SEARCH_CACHE: dict[str, SearchImplementation] = {}


def need(condition: Any, message: str) -> None:
    if not condition:
        raise ValueError(message)


def relative(value: Any) -> str:
    need(
        isinstance(value, str) and bool(value) and "\\" not in value and "\x00" not in value,
        "A POSIX relative source path is required.",
    )
    need(
        not PureWindowsPath(value).drive and not (len(value) >= 2 and value[1] == ":"),
        "Unsafe source path.",
    )
    parsed = PurePosixPath(value)
    parts = value.split("/")
    need(
        not parsed.is_absolute()
        and all(
            part not in {"", ".", ".."}
            and not PureWindowsPath(part).drive
            and not (len(part) >= 2 and part[1] == ":")
            for part in parts
        ),
        "Unsafe source path.",
    )
    need(parsed.suffix.lower() == ".md", "Only complete Markdown sources are exposed.")
    need(
        not any(value.startswith(prefix) for prefix in PROTECTED_PREFIXES),
        "Source is protected by this study exposure boundary.",
    )
    return value


def _default_runtime_entry(base_root: Path | str) -> Path:
    base = Path(base_root)
    candidates = (
        base / _RUNTIME_ENTRY,
        base / "src/managed_ai_information" / _RUNTIME_ENTRY,
        base / "engine" / _RUNTIME_ENTRY,
    )
    for candidate in candidates:
        if candidate.exists() or candidate.is_symlink():
            return candidate
    return candidates[0]


def _search_bytes(path: Path | str) -> bytes:
    abs_path = Path(os.path.abspath(path))
    need(
        not any(is_reparse_or_symlink(part) for part in (abs_path, *abs_path.parents)),
        "Search code cannot pass through a symlink or reparse point: " + str(abs_path),
    )
    need(abs_path.is_file(), "Required search code is not an ordinary file: " + str(abs_path))

    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_NONBLOCK", 0)
    with os.fdopen(os.open(abs_path, flags), "rb") as stream:
        metadata = os.fstat(stream.fileno())
        need(
            stat.S_ISREG(metadata.st_mode),
            "Required search code is not an ordinary file: " + str(abs_path),
        )
        need(
            metadata.st_size <= _MAX_SEARCH_FILE_BYTES,
            "Search code exceeds byte bound: " + str(abs_path),
        )
        data = stream.read(_MAX_SEARCH_FILE_BYTES + 1)

    need(len(data) <= _MAX_SEARCH_FILE_BYTES, "Search code exceeds byte bound: " + str(abs_path))
    return data


def _library_root_for(entry: Path) -> Path:
    """Determine the bounded root directory for verified library relative imports."""
    return entry.parent


def _matches_cached_pins(implementation: SearchImplementation) -> bool:
    """Verify every pinned file still matches its exact cryptographic digest and non-reparse
    guard.
    """
    try:
        for path_str, expected in implementation.pins.items():
            raw = _search_bytes(path_str)
            if len(raw) != expected["bytes"] or digest(raw) != expected["sha256"]:
                return False
    except OSError, ValueError:
        return False
    return True


def _assemble_ledger_bytes(
    encoded_entries: dict[str, dict[str, bytes]],
    extra_category: str | None = None,
    extra_ident: str | None = None,
    extra_raw: bytes | None = None,
) -> bytes:
    """Assemble exact canonical ledger JSON bytes from pre-encoded receipt fragments."""
    sections: list[bytes] = []
    for key in ("inventories", "schema", "selections", "sources", "uses"):
        if key == "schema":
            sections.append(b'"schema":' + _SCHEMA_VALUE_BYTES)
            continue

        bucket = encoded_entries[key]
        if key == extra_category and extra_ident is not None and extra_raw is not None:
            items = sorted((*bucket.items(), (extra_ident, extra_raw)))
        else:
            items = sorted(bucket.items())

        inner = b",".join(
            json.dumps(ident, ensure_ascii=False, allow_nan=False).encode("utf-8") + b":" + raw
            for ident, raw in items
        )
        sections.append(f'"{key}":{{'.encode("ascii") + inner + b"}")

    return b"{" + b",".join(sections) + b"}\n"


def read_receipt_ledger(ledger_path: Path) -> tuple[dict[str, Any], dict[str, dict[str, bytes]]]:
    """Read, validate, and cache pre-encoded receipt entries from ledger_path."""
    encoded_entries: dict[str, dict[str, bytes]] = {category: {} for category in _LEDGER_CATEGORIES}
    need(not ledger_path.is_symlink(), "Receipt symlinks are unsupported.")

    try:
        data = ledger_path.read_bytes()
    except FileNotFoundError:
        ledger: dict[str, Any] = {
            "schema": VERSION,
            "inventories": {},
            "selections": {},
            "sources": {},
            "uses": {},
        }
        return ledger, encoded_entries

    ledger = loads_strict(data)
    need(
        isinstance(ledger, dict)
        and ledger.get("schema") == VERSION
        and set(ledger) == {"schema", "inventories", "selections", "sources", "uses"},
        "Unknown receipt ledger.",
    )
    for category in _LEDGER_CATEGORIES:
        need(isinstance(ledger[category], dict), "Invalid receipt category.")
        bucket = encoded_entries[category]
        for ident, record in ledger[category].items():
            raw_record = encoded(record)
            need(digest(raw_record) == ident, "Receipt identity mismatch: " + ident)
            bucket[ident] = raw_record

    return ledger, encoded_entries


def validate_use_artifacts(artifacts: Any) -> None:
    """Validate accepted artifact identity mapping for SourceService.record_use."""
    need(
        isinstance(artifacts, dict) and bool(artifacts),
        "Accepted artifact identity mapping required.",
    )
    for path, record in artifacts.items():
        need(
            isinstance(path, str) and bool(path) and isinstance(record, dict),
            "Invalid artifact identity.",
        )
        need(
            type(record.get("bytes")) is int and record["bytes"] >= 0,
            "Invalid artifact byte count.",
        )
        sha = record.get("sha256")
        need(
            isinstance(sha, str) and len(sha) == 64 and all(ch in "0123456789abcdef" for ch in sha),
            "Invalid artifact digest.",
        )


class SearchImplementation:
    """Select and execute one bounded snapshot of the trusted lexical code."""

    def __init__(
        self,
        module_path: Path | str | None = None,
        *,
        repo_root: Path | None = None,
    ) -> None:
        resolved_root = Path(__file__).resolve().parents[2] if repo_root is None else repo_root
        self.runtime_entry = _default_runtime_entry(resolved_root)
        if module_path is None:
            module_path = self.runtime_entry

        entry = Path(os.path.abspath(module_path))
        data = _search_bytes(entry)
        self.modules: dict[Path, ModuleType] = {}
        self.pins: dict[str, dict[str, Any]] = {}
        self.entry = entry
        self.library_root = _library_root_for(entry)
        self._sealed = False
        self.module = self._load(entry, data)
        self._sealed = True

        analyzer = self.module.FtsAnalyzer
        for implementation in (
            self.module.search_query,
            analyzer.identity,
            analyzer.create,
            analyzer.search,
        ):
            code = getattr(implementation, "__code__", None)
            need(
                code is not None and code.co_filename in self.pins,
                "Selected search callable is outside the verified source files.",
            )
        self.analyzer = analyzer.identity()

    @classmethod
    def cached(
        cls,
        module_path: Path | str | None = None,
        *,
        repo_root: Path | None = None,
    ) -> SearchImplementation:
        """Return a digest-verified cached SearchImplementation, recompiling immediately on any
        drift.
        """
        resolved_root = Path(__file__).resolve().parents[2] if repo_root is None else repo_root
        resolved_entry = Path(os.path.abspath(module_path or _default_runtime_entry(resolved_root)))
        cache_key = str(resolved_entry)

        with _SEARCH_CACHE_LOCK:
            existing = _SEARCH_CACHE.get(cache_key)
        if existing is not None and _matches_cached_pins(existing):
            return existing

        fresh = cls(resolved_entry, repo_root=resolved_root)
        with _SEARCH_CACHE_LOCK:
            _SEARCH_CACHE[cache_key] = fresh
        return fresh

    def _load(self, path: Path | str, data: bytes | None = None) -> ModuleType:
        abs_path = Path(os.path.abspath(path))
        need(
            abs_path.is_relative_to(self.library_root),
            "Search code dependency escapes bounded library root: " + str(abs_path),
        )
        if abs_path in self.modules:
            return self.modules[abs_path]

        need(
            not self._sealed,
            "Search cannot load a new code dependency after binding: " + abs_path.name,
        )
        need(len(self.modules) < _MAX_SEARCH_FILES, "Search code exceeds dependency count bound.")
        if data is None:
            data = _search_bytes(abs_path)

        rel = abs_path.relative_to(self.library_root).with_suffix("")
        dotted = ".".join(rel.parts)
        package_parts = rel.parts[:-1]
        prefix = f"_g60_verified_source_library_{id(self):x}"
        mod_name = f"{prefix}.{dotted}"
        module = ModuleType(mod_name)
        module.__file__ = str(abs_path)
        module.__package__ = f"{prefix}." + ".".join(package_parts) if package_parts else prefix
        module.__dict__["__builtins__"] = {**vars(builtins), "__import__": self._import}
        self.modules[abs_path] = module
        self.pins[str(abs_path)] = {"bytes": len(data), "sha256": digest(data)}

        sys.modules[mod_name] = module
        try:
            exec(compile(data, str(abs_path), "exec", dont_inherit=True), module.__dict__)  # noqa: S102
        except SyntaxError as exc:
            raise ValueError("Invalid lexical source: " + str(exc)) from exc
        finally:
            sys.modules.pop(mod_name, None)
        return module

    def _import(
        self,
        name: str,
        globals: dict[str, Any] | None = None,
        locals: dict[str, Any] | None = None,
        fromlist: tuple[str, ...] = (),
        level: int = 0,
    ) -> Any:
        if level:
            need(
                level in (1, 2) and bool(fromlist),
                "Search can bind only explicit bounded relative source imports: " + name,
            )
            caller_file = globals.get("__file__") if isinstance(globals, dict) else None
            need(
                isinstance(caller_file, str) and caller_file in self.pins,
                "Relative search import originated outside verified source files.",
            )
            if not isinstance(caller_file, str):
                raise ValueError("Relative search import originated outside verified source files.")
            base = Path(caller_file).parent
            if level == 2:
                base = base.parent
            need(
                base == self.library_root or base.is_relative_to(self.library_root),
                "Search relative import escapes bounded library root: " + name,
            )
            if name:
                parts = name.split(".")
                need(
                    1 <= len(parts) <= 2 and all(part.isidentifier() for part in parts),
                    "Search can bind only explicit bounded relative source imports: " + name,
                )
                target = base.joinpath(*parts[:-1], parts[-1] + ".py")
                return self._load(target)

            need(
                all(isinstance(item, str) and item.isidentifier() for item in fromlist),
                "Search can bind only explicit bounded relative source imports.",
            )
            namespace = ModuleType("_g60_verified_source_library._rel")
            for item in fromlist:
                setattr(namespace, item, self._load(base / (item + ".py")))
            return namespace

        root = name.split(".", 1)[0]
        need(
            root not in {"runpy", "importlib"},
            "Search code must use a supported entrance and explicit sibling imports: " + name,
        )
        need(
            root in sys.stdlib_module_names,
            "Search dependency must be standard library code or an explicit sibling import: "
            + name,
        )
        return builtins.__import__(name, globals, locals, fromlist, level)

    @property
    def entry_pin(self) -> dict[str, Any]:
        return self.pins[str(self.entry)]
