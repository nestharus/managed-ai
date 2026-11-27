"""Pinned domain data, typed payload schemas and reference source catalogues.

This is selection and structural validation for the existing native System.
It neither runs domain code nor judges the meaning or acquisition of a source.
"""

import errno
import os
import re
import stat
from pathlib import Path
from typing import Any

from managed_ai_information.core import formats as _formats
from managed_ai_information.core.formats import CATALOGUE_FORMAT, DOMAIN_FORMAT, INFORMATION_FORMAT
from managed_ai_information.core.ownership import packet_id
from managed_ai_information.core.storage import (
    OPEN_RO_NOFOLLOW,
    check_ordinary_ancestors,
    encode,
    is_ordinary_dir_stat,
    is_ordinary_file_stat,
    open_directory,
    safe_relpath,
    sha,
    strict_json_loads,
)
from managed_ai_information.store.selection.payload_schema import (
    PAYLOAD_TYPES as PAYLOAD_TYPES,
)
from managed_ai_information.store.selection.payload_schema import (
    same_literal as same_literal,
)
from managed_ai_information.store.selection.payload_schema import (
    validate_payload as validate_payload,
)
from managed_ai_information.store.selection.payload_schema import (
    validate_schema as validate_schema,
)

# Literal assignments preserved for static AST verification in controller.code_identity().
ENGINE_API = "managed-ai-runtime/1"
STORE_SCHEMA = "managed-ai-store/1"
if ENGINE_API != _formats.ENGINE_API or STORE_SCHEMA != _formats.STORE_SCHEMA:
    raise RuntimeError("domain_selection engine/store schema constants diverged from core.formats")

_DOMAIN_ALLOWED_KEYS = frozenset(
    [
        "format",
        "id",
        "version",
        "engine_api",
        "title",
        "summary",
        "description",
        "source_root",
        "catalogue",
        "requires_domains",
        "schemas",
        "entry_sources",
        "tools",
        "relation_vocabulary",
        "protected_record_ids",
        "content_addressed_basis_prefixes",
    ]
)
_CATALOGUE_ALLOWED_KEYS = frozenset(
    ["format", "id", "version", "title", "summary", "description", "source_root", "sources"]
)
_DIR_OPEN_ERRNOS = (errno.ELOOP, errno.ENOENT, errno.ENOTDIR)
_FILE_OPEN_ERRNOS = (errno.ELOOP, errno.ENOENT, errno.ENOTDIR, errno.EISDIR, errno.ENXIO)


def _verify_ordinary_dir(path: Path) -> Path:
    return check_ordinary_ancestors(
        path, expect_dir=True, error_prefix="ordinary selected source required"
    )


def ordinary(path: Path | str) -> Path:
    return check_ordinary_ancestors(
        Path(path), expect_dir=False, error_prefix="ordinary selected source required"
    )


def _is_safe_member_relpath(name: str) -> bool:
    if not isinstance(name, str) or not name or "\0" in name or "\\" in name:
        return False
    if name.startswith("/") or (len(name) >= 2 and name[1] == ":"):
        return False
    return all(part != ".." and not (len(part) >= 2 and part[1] == ":") for part in name.split("/"))


def _read_stream_bounded(stream: Any, byte_limit: int | None) -> bytes:
    if byte_limit is None:
        return bytes(stream.read())
    raw = bytes(stream.read(byte_limit + 1))
    if len(raw) > byte_limit:
        raise ValueError("source body exceeds the requested acquisition bound")
    return raw


def _open_dir_checked(target: Path | str, err_msg: str, *, dir_fd: int | None = None) -> int:
    try:
        return open_directory(target, dir_fd=dir_fd)
    except OSError as exc:
        if exc.errno in _DIR_OPEN_ERRNOS:
            raise ValueError(err_msg) from None
        raise


def _read_ordinary_posix(
    root: Path,
    parts: list[str],
    err_msg: str,
    byte_limit: int | None,
    validated_root: bool,
    root_fd: int | None,
) -> bytes:
    opened_fds: list[int] = []
    try:
        if root_fd is not None:
            cur = root_fd
        else:
            if not validated_root:
                _verify_ordinary_dir(root)
            cur = _open_dir_checked(root, err_msg)
            opened_fds.append(cur)

        for segment in parts[:-1]:
            cur = _open_dir_checked(segment, err_msg, dir_fd=cur)
            opened_fds.append(cur)

        try:
            leaf_fd = os.open(parts[-1], OPEN_RO_NOFOLLOW, dir_fd=cur)
        except OSError as exc:
            if exc.errno in _FILE_OPEN_ERRNOS:
                raise ValueError(err_msg) from None
            raise

        try:
            info = os.fstat(leaf_fd)
            if not stat.S_ISREG(info.st_mode):
                raise ValueError(err_msg)
            if byte_limit is not None and info.st_size > byte_limit:
                raise ValueError("source body exceeds the requested acquisition bound")
            stream = os.fdopen(leaf_fd, "rb")
        except BaseException:
            os.close(leaf_fd)
            raise

        with stream:
            return _read_stream_bounded(stream, byte_limit)
    finally:
        for fd in reversed(opened_fds):
            os.close(fd)


def _read_ordinary_win(
    root: Path, parts: list[str], err_msg: str, byte_limit: int | None, validated_root: bool
) -> bytes:
    if not validated_root:
        _verify_ordinary_dir(root)

    cur_str = str(root)
    for segment in parts[:-1]:
        cur_str = cur_str + os.sep + segment
        try:
            seg_info = os.lstat(cur_str)
        except OSError:
            raise ValueError(err_msg) from None
        if not is_ordinary_dir_stat(seg_info):
            raise ValueError(err_msg)

    leaf_str = cur_str + os.sep + parts[-1]
    try:
        leaf_info = os.lstat(leaf_str)
    except OSError:
        raise ValueError(err_msg) from None
    if not is_ordinary_file_stat(leaf_info):
        raise ValueError(err_msg)
    if byte_limit is not None and leaf_info.st_size > byte_limit:
        raise ValueError("source body exceeds the requested acquisition bound")

    with open(leaf_str, "rb") as stream:
        return _read_stream_bounded(stream, byte_limit)


def _read_ordinary_under(
    root: Path,
    name: str,
    *,
    byte_limit: int | None = None,
    validated_root: bool = False,
    root_fd: int | None = None,
) -> bytes:
    """Read an ordinary non-symlink file relative to root via dir-FD or lstat."""
    if not _is_safe_member_relpath(name):
        raise ValueError("invalid selected member path: " + str(name))
    parts = [part for part in name.split("/") if part not in ("", ".")]
    if not parts:
        raise ValueError("invalid selected member path: " + str(name))

    err_msg = "ordinary selected source required: " + str(root.joinpath(*parts))
    if hasattr(os, "O_NOFOLLOW") and hasattr(os, "O_DIRECTORY"):
        return _read_ordinary_posix(root, parts, err_msg, byte_limit, validated_root, root_fd)
    return _read_ordinary_win(root, parts, err_msg, byte_limit, validated_root)


def source_path(root: Path, name: str) -> Path:
    if not _is_safe_member_relpath(name):
        raise ValueError("invalid selected member path: " + str(name))
    return ordinary(root / Path(name))


def load_json(path: Path | str, expected: str | None = None) -> dict[str, Any]:
    raw = ordinary(path).read_bytes()
    if expected is not None and sha(raw) != expected:
        raise ValueError("selected manifest changed: " + str(path))
    return strict_json_loads(raw, path, require_object=True)


def pin(root: Path, path: Path | str) -> dict[str, str]:
    resolved = ordinary(Path(path).resolve())
    return {"path": safe_relpath(resolved, root), "sha256": sha(resolved.read_bytes())}


def _validate_nonempty_str_list(domain: dict[str, Any], field: str) -> None:
    if field not in domain:
        return
    values = domain[field]
    if not isinstance(values, list) or any(not isinstance(v, str) or not v.strip() for v in values):
        raise ValueError(f"{domain['id']} {field} must be a list of nonempty strings")


def _validate_domain_tools(domain: dict[str, Any]) -> None:
    if "tools" not in domain:
        return
    if not isinstance(domain["tools"], list):
        raise ValueError(f"{domain['id']} tools must be a list")
    for tool in domain["tools"]:
        if (
            not isinstance(tool, dict)
            or not isinstance(tool.get("id"), str)
            or not tool["id"].strip()
        ):
            raise ValueError(f"{domain['id']} tool must be an object with nonempty id")
        if "dependency_source_ids" not in tool:
            continue
        deps = tool["dependency_source_ids"]
        if (
            not isinstance(deps, list)
            or any(not isinstance(d, str) or not d.strip() for d in deps)
            or len(deps) != len(set(deps))
        ):
            raise ValueError(
                f"{domain['id']} tool {tool['id']} dependency_source_ids must be "
                "unique nonempty strings"
            )


def _validate_domain_guards(domain: dict[str, Any]) -> None:
    if "protected_record_ids" in domain:
        pids = domain["protected_record_ids"]
        if not isinstance(pids, list) or len(pids) != len(set(pids)):
            raise ValueError(
                f"{domain['id']} protected_record_ids must be a list of unique record IDs"
            )
        for pid in pids:
            packet_id(pid)

    if "content_addressed_basis_prefixes" in domain:
        prefixes = domain["content_addressed_basis_prefixes"]
        if not isinstance(prefixes, list) or len(prefixes) != len(set(prefixes)):
            raise ValueError(
                f"{domain['id']} content_addressed_basis_prefixes must be unique relative prefixes"
            )
        for pref in prefixes:
            if (
                not isinstance(pref, str)
                or not pref
                or pref.startswith("/")
                or "\\" in pref
                or ":" in pref
                or ".." in pref.split("/")
                or not pref.endswith("/")
            ):
                raise ValueError(f"{domain['id']} invalid content_addressed_basis_prefix: {pref!r}")


def _validate_domain_manifest(domain: Any, path: Path) -> None:
    if not isinstance(domain, dict):
        raise ValueError("domain manifest must be a JSON object: " + str(path))
    extra = set(domain) - _DOMAIN_ALLOWED_KEYS
    if extra:
        raise ValueError(f"unknown domain manifest fields ({path}): " + ", ".join(sorted(extra)))
    if domain.get("format") != DOMAIN_FORMAT or domain.get("engine_api") != ENGINE_API:
        raise ValueError("incompatible domain manifest: " + str(path))
    if not isinstance(domain.get("id"), str) or not domain["id"].strip():
        raise ValueError("domain manifest requires nonempty id: " + str(path))
    if "version" in domain:
        ver = domain["version"]
        if not isinstance(ver, str) or not re.fullmatch(
            r"[0-9]+(?:\.[0-9]+){0,2}(?:-[0-9A-Za-z._-]+)?", ver
        ):
            raise ValueError(f"unsupported domain manifest version {ver!r}: {path}")
    _validate_nonempty_str_list(domain, "requires_domains")
    if "schemas" in domain and not isinstance(domain["schemas"], dict):
        raise ValueError(f"{domain['id']} schemas must be an object")
    _validate_nonempty_str_list(domain, "entry_sources")
    _validate_domain_tools(domain)
    _validate_domain_guards(domain)


def _validate_relation_vocabulary(raw: Any, owner: str) -> dict[str, list[str]]:
    if not isinstance(raw, dict):
        raise ValueError(f"{owner} relation_vocabulary must be an object")
    extra = set(raw) - {"predicates", "provenance_self_loop_relations"}
    if extra:
        raise ValueError(f"{owner} relation_vocabulary unknown fields: " + ", ".join(sorted(extra)))
    parsed: dict[str, list[str]] = {}
    for key in ("predicates", "provenance_self_loop_relations"):
        values = raw.get(key, [])
        if not isinstance(values, list) or any(
            not isinstance(v, str) or not v.strip() for v in values
        ):
            raise ValueError(
                f"{owner} relation_vocabulary.{key} must be a list of nonempty strings"
            )
        if len(values) != len(set(values)):
            raise ValueError(f"{owner} relation_vocabulary.{key} contains duplicates")
        parsed[key] = list(values)
    return parsed


class DomainSelection:
    def __init__(self, root: Path | str, descriptor: dict[str, Any]) -> None:
        self.root = Path(root)
        self.schemas: dict[str, Any] = {}
        self.entries: list[dict[str, Any]] = []
        self.entry_sources: list[str] = []
        self.domains: list[str] = []
        self.tools: list[dict[str, Any]] = []
        self.bindings: dict[str, Path] = {}
        self.schema_bindings: dict[str, str] = {}
        self._validated_roots: set[str] = set()
        if descriptor.get("store_schema", STORE_SCHEMA) != STORE_SCHEMA:
            raise ValueError("unsupported store schema; explicit migration required")
        if descriptor.get("engine_api", ENGINE_API) != ENGINE_API:
            raise ValueError("store requires a different engine API")

        acc: dict[str, Any] = {
            "preds": set(),
            "loops": set(),
            "protected": set(),
            "prefixes": [],
            "declaring": {},
            "required": set(),
        }
        for binding in descriptor.get("domains", []):
            self._load_domain_binding(binding, acc)
        for binding in descriptor.get("catalogues", []):
            self.add_catalogue(self.root / binding["path"], binding["sha256"])

        self._check_selection_invariants(descriptor, acc["required"])
        self.relation_predicates = sorted(acc["preds"])
        self.provenance_self_loop_relations = sorted(acc["loops"])
        self.relation_vocabulary = {
            "predicates": self.relation_predicates,
            "provenance_self_loop_relations": self.provenance_self_loop_relations,
        }
        self.protected_record_ids = frozenset(acc["protected"])
        self.content_addressed_basis_prefixes = tuple(acc["prefixes"])

    def _register_domain_schemas(
        self, owner: str, schemas: dict[str, Any], declaring: dict[str, str]
    ) -> None:
        for kind, schema in schemas.items():
            try:
                validate_schema(schema)
            except ValueError as exc:
                raise ValueError(f"{owner} schema for {kind}: {exc}") from exc
            schema_hash = sha(encode(schema))
            if kind in self.schema_bindings:
                if self.schema_bindings[kind] != schema_hash:
                    raise ValueError(
                        f"conflicting payload schemas for {kind}: {declaring[kind]} and {owner}"
                    )
                continue
            declaring[kind] = owner
            self.schemas[kind] = schema
            self.schema_bindings[kind] = schema_hash

    def _load_domain_binding(self, binding: dict[str, Any], acc: dict[str, Any]) -> None:
        path = self.root / binding["path"]
        domain = load_json(path, binding["sha256"])
        _validate_domain_manifest(domain, path)
        owner = domain["id"]
        if owner in self.domains:
            raise ValueError("duplicate selected domain identity")

        self.domains.append(owner)
        acc["required"].update(domain.get("requires_domains", []))
        acc["protected"].update(domain.get("protected_record_ids", ()))
        for pref in domain.get("content_addressed_basis_prefixes", ()):
            if pref not in acc["prefixes"]:
                acc["prefixes"].append(pref)
        if "relation_vocabulary" in domain:
            vocab = _validate_relation_vocabulary(domain["relation_vocabulary"], owner)
            acc["preds"].update(vocab["predicates"])
            acc["loops"].update(vocab["provenance_self_loop_relations"])

        self._register_domain_schemas(owner, domain.get("schemas", {}), acc["declaring"])
        self.entry_sources.extend(domain.get("entry_sources", []))
        self.tools.extend(dict(tool, domain=owner) for tool in domain.get("tools", []))
        if domain.get("catalogue"):
            cat = domain["catalogue"]
            self.add_catalogue(
                path.parent / cat["path"],
                cat["sha256"],
                source_root=(path.parent / domain.get("source_root", ".")).resolve(),
            )

    def _check_selection_invariants(
        self, descriptor: dict[str, Any], required_domains: set[str]
    ) -> None:
        seen_ids: set[str] = set()
        collisions: list[str] = []
        for entry in self.entries:
            ident = entry["id"]
            if ident in seen_ids and ident not in collisions:
                collisions.append(ident)
            seen_ids.add(ident)
        if collisions:
            sample = ", ".join(sorted(collisions)[:5])
            suffix = f" (+{len(collisions) - 5} more)" if len(collisions) > 5 else ""
            hint = (
                " (note: selected domain manifests auto-load their declared catalogues; "
                "omit duplicate --catalogue flags)"
                if descriptor.get("catalogues")
                else ""
            )
            raise ValueError(f"selected source identity collision: {sample}{suffix}{hint}")

        missing = required_domains - set(self.domains)
        if missing:
            raise ValueError("required domains are not selected: " + ", ".join(sorted(missing)))
        if len({tool["id"] for tool in self.tools}) != len(self.tools):
            raise ValueError("selected domain tool identity collision")

    def validate_catalogue(self, available: dict[str, dict[str, Any]]) -> None:
        requested = [*self.entry_sources]
        for tool in self.tools:
            for key in ("source_id", "entry_source_id", "contract_source_id"):
                if key in tool:
                    requested.append(tool[key])
            requested.extend(tool.get("dependency_source_ids", ()))
        if any(
            ident not in available or available[ident].get("availability") == "sealed"
            for ident in requested
        ):
            raise ValueError(
                "entry guidance and tool contracts must name available selected sources"
            )

    def add_catalogue(
        self, path: Path, expected: str | None = None, source_root: Path | None = None
    ) -> None:
        catalogue = load_json(path, expected)
        if catalogue.get("format") != CATALOGUE_FORMAT:
            raise ValueError("unsupported reference source catalogue")
        extra = set(catalogue) - _CATALOGUE_ALLOWED_KEYS
        if extra:
            raise ValueError(
                f"unknown reference source catalogue fields ({path}): " + ", ".join(sorted(extra))
            )
        if not isinstance(catalogue.get("sources"), list):
            raise ValueError("reference source catalogue requires a sources list: " + str(path))
        root = (
            (path.parent / catalogue["source_root"]).resolve()
            if "source_root" in catalogue
            else (source_root or path.parent.resolve())
        )
        if str(root) not in self._validated_roots:
            _verify_ordinary_dir(root)
            self._validated_roots.add(str(root))
        for row in catalogue["sources"]:
            name = row["path"]
            if not _is_safe_member_relpath(name):
                raise ValueError("unsafe source catalogue path")
            exposure = row.get("exposure", "available")
            if exposure not in {"available", "sealed"}:
                raise ValueError("unknown source exposure policy")
            if (
                not re.fullmatch("[0-9a-f]{64}", row["sha256"])
                or type(row["bytes"]) is not int
                or row["bytes"] < 0
            ):
                raise ValueError("invalid source identity")
            self.entries.append(
                dict(
                    row,
                    title=row.get("title", row["id"]),
                    kind=row.get("kind", "source"),
                    purpose=row.get("summary", ""),
                    family="selected-reference-source",
                    body_role="complete-current-source-reference",
                    availability=exposure,
                    source_path=row["id"],
                    body_path=name,
                    source_root=str(root),
                )
            )
            self.bindings[row["id"]] = root

    def validate_record(
        self, record: dict[str, Any], previous: dict[str, Any] | None = None
    ) -> None:
        kind = record.get("kind")
        if previous and previous["kind"] in self.schemas and kind != previous["kind"]:
            raise ValueError("typed record reclassification requires an explicit migration")
        if kind in self.schemas:
            payload = record.get("content")
            if isinstance(payload, dict) and payload.get("format") == INFORMATION_FORMAT:
                payload = payload["payload"]
            validate_payload(payload, self.schemas[kind])

    def body(self, entry: dict[str, Any], *, byte_limit: int | None = None) -> bytes:
        validated = entry.get("source_root") in self._validated_roots
        return read_selected_body(entry, byte_limit=byte_limit, validated_root=validated)

    def view(self) -> dict[str, Any]:
        return {
            "domains": self.domains,
            "typed_kinds": sorted(self.schemas),
            "source_count": len(self.entries),
            "entry_sources": self.entry_sources,
            "tools": self.tools,
            "tool_scope": "Selected source and contract references; loading does not execute code.",
        }


def read_selected_body(
    entry: dict[str, Any],
    *,
    byte_limit: int | None = None,
    validated_root: bool = False,
    root_fd: int | None = None,
) -> bytes:
    if entry.get("availability") == "sealed":
        raise ValueError(
            f"sealed source {entry['id']!r}: body reading and context capture are prohibited; "
            "inspect availability with a catalogue metadata listing (no identity argument), "
            "then select an available source"
        )
    raw = _read_ordinary_under(
        Path(entry["source_root"]),
        entry["body_path"],
        byte_limit=byte_limit,
        validated_root=validated_root,
        root_fd=root_fd,
    )
    if len(raw) != entry["bytes"] or sha(raw) != entry["sha256"]:
        raise ValueError("source reference changed: " + entry["id"])
    return raw
