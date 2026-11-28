"""Deterministic pure-stdlib composable domain package export, verify, and import.

Implements the ``managed-ai-domain-package/1`` packaging contract so loadable domains
(schemas, extractors, observers, workflows, evals, source catalogues) can be exported,
verified, imported, shared, or distributed in a marketplace without executing domain
code or weakening fail-closed path, symlink, or SHA-256 integrity boundaries.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import stat
import tempfile
import zipfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from managed_ai_information.core.formats import DOMAIN_FORMAT, ENGINE_API
from managed_ai_information.core.ownership import packet_id, relative_path
from managed_ai_information.core.storage import (
    atomic_bytes,
    check_ordinary_ancestors,
    encode,
    is_ordinary_dir_stat,
    is_ordinary_file_stat,
    is_reparse_or_symlink,
    sha,
    strict_json_loads,
)

DOMAIN_PACKAGE_FORMAT = "managed-ai-domain-package/1"
DOMAIN_PACKAGE_MANIFEST_FILE = "domain-package.json"
DOMAIN_PACKAGE_FILES_PREFIX = "files/"
MAX_PACKAGE_BYTES = 128 * 1024 * 1024
MAX_MEMBER_BYTES = 32 * 1024 * 1024
MAX_MEMBER_COUNT = 10_000

_DETERMINISTIC_ZIP_DATE = (2020, 1, 1, 0, 0, 0)
_VERSION_RE = re.compile(r"[0-9]+(?:\.[0-9]+){0,2}(?:-[0-9A-Za-z._-]+)?")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_IGNORED_EXPORT_DIRS = frozenset({".git", "__pycache__"})
_MANIFEST_LIST_FIELDS = (
    "capabilities",
    "requires_domains",
    "exports_relations",
    "artifact_types",
    "workflow_entrypoints",
)
_MANIFEST_ALLOWED_KEYS = frozenset(
    {
        "format",
        "package_id",
        "version",
        "domain_kind",
        "license",
        *_MANIFEST_LIST_FIELDS,
        "content_sha256_tree",
        "pricing_metadata",
    }
)
_MANIFEST_REQUIRED_KEYS = _MANIFEST_ALLOWED_KEYS - {"pricing_metadata"}


def _require_nonempty_str(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a nonempty string")
    return value.strip()


def _normalize_unique_strings(values: Iterable[str], label: str) -> tuple[str, ...]:
    items = list(values)
    cleaned: list[str] = []
    seen: set[str] = set()
    for item in items:
        text = _require_nonempty_str(item, label)
        if text in seen:
            raise ValueError(f"{label} contains duplicate entry: {text!r}")
        seen.add(text)
        cleaned.append(text)
    return tuple(sorted(cleaned))


def _validate_sha256_tree(raw_tree: Any) -> dict[str, str]:
    if not isinstance(raw_tree, Mapping):
        raise ValueError("content_sha256_tree must be an object mapping relative paths to SHA-256")
    normalized: dict[str, str] = {}
    for key, digest in sorted(raw_tree.items()):
        if not isinstance(key, str):
            raise ValueError("content_sha256_tree keys must be strings")
        safe_key = relative_path(key)
        if safe_key == DOMAIN_PACKAGE_MANIFEST_FILE:
            raise ValueError(f"reserved manifest path not allowed in tree: {safe_key}")
        if not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest):
            raise ValueError(f"invalid SHA-256 digest for {safe_key!r}")
        if safe_key in normalized:
            raise ValueError(f"duplicate normalized path in content_sha256_tree: {safe_key!r}")
        normalized[safe_key] = digest
    return normalized


def _validate_pricing_metadata(raw: Any) -> dict[str, Any] | None:
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise ValueError("pricing_metadata must be a JSON object or null")
    try:
        serialized = json.dumps(dict(raw), sort_keys=True, allow_nan=False, ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"pricing_metadata must be finite JSON-serializable: {exc}") from exc
    parsed = strict_json_loads(serialized.encode("utf-8"), "pricing_metadata", require_object=True)
    if not isinstance(parsed, dict):
        raise ValueError("pricing_metadata must be a JSON object")
    return dict(parsed)


@dataclass(frozen=True)
class DomainPackageManifest:
    """Deterministic manifest for a composable ``managed-ai-domain-package/1`` bundle."""

    package_id: str
    version: str
    domain_kind: str
    license: str
    capabilities: tuple[str, ...]
    requires_domains: tuple[str, ...]
    exports_relations: tuple[str, ...]
    artifact_types: tuple[str, ...]
    workflow_entrypoints: tuple[str, ...]
    content_sha256_tree: dict[str, str]
    pricing_metadata: dict[str, Any] | None = None
    format: str = DOMAIN_PACKAGE_FORMAT

    def __post_init__(self) -> None:
        if self.format != DOMAIN_PACKAGE_FORMAT:
            raise ValueError(f"unsupported domain package format: {self.format!r}")
        pid = packet_id(_require_nonempty_str(self.package_id, "package_id"))
        ver = _require_nonempty_str(self.version, "version")
        if not _VERSION_RE.fullmatch(ver):
            raise ValueError(f"invalid domain package version: {ver!r}")
        dkind = _require_nonempty_str(self.domain_kind, "domain_kind")
        lic = _require_nonempty_str(self.license, "license")
        for key, value in (
            ("package_id", pid),
            ("version", ver),
            ("domain_kind", dkind),
            ("license", lic),
            ("capabilities", _normalize_unique_strings(self.capabilities, "capabilities")),
            (
                "requires_domains",
                _normalize_unique_strings(self.requires_domains, "requires_domains"),
            ),
            (
                "exports_relations",
                _normalize_unique_strings(self.exports_relations, "exports_relations"),
            ),
            ("artifact_types", _normalize_unique_strings(self.artifact_types, "artifact_types")),
            (
                "workflow_entrypoints",
                _normalize_unique_strings(self.workflow_entrypoints, "workflow_entrypoints"),
            ),
            ("content_sha256_tree", _validate_sha256_tree(self.content_sha256_tree)),
            ("pricing_metadata", _validate_pricing_metadata(self.pricing_metadata)),
        ):
            object.__setattr__(self, key, value)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "format": self.format,
            "package_id": self.package_id,
            "version": self.version,
            "domain_kind": self.domain_kind,
            "license": self.license,
            **{f: list(getattr(self, f)) for f in _MANIFEST_LIST_FIELDS},
            "content_sha256_tree": dict(sorted(self.content_sha256_tree.items())),
        }
        if self.pricing_metadata is not None:
            payload["pricing_metadata"] = self.pricing_metadata
        return payload

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> DomainPackageManifest:
        if not isinstance(data, Mapping):
            raise ValueError("domain package manifest must be a JSON object")
        keys = set(data.keys())
        extra = keys - _MANIFEST_ALLOWED_KEYS
        if extra:
            raise ValueError("unknown domain package manifest fields: " + ", ".join(sorted(extra)))
        missing = _MANIFEST_REQUIRED_KEYS - keys
        if missing:
            raise ValueError(
                "missing required domain package manifest fields: " + ", ".join(sorted(missing))
            )
        for list_field in _MANIFEST_LIST_FIELDS:
            if not isinstance(data[list_field], list):
                raise ValueError(f"{list_field} must be a JSON array")
        return cls(
            format=str(data["format"]),
            package_id=data["package_id"],
            version=data["version"],
            domain_kind=data["domain_kind"],
            license=data["license"],
            capabilities=tuple(data["capabilities"]),
            requires_domains=tuple(data["requires_domains"]),
            exports_relations=tuple(data["exports_relations"]),
            artifact_types=tuple(data["artifact_types"]),
            workflow_entrypoints=tuple(data["workflow_entrypoints"]),
            content_sha256_tree=dict(data["content_sha256_tree"]),
            pricing_metadata=data.get("pricing_metadata"),
        )


@dataclass(frozen=True)
class DomainPackageVerification:
    """Verified inspection summary for a domain package bundle."""

    manifest: DomainPackageManifest
    bundle_sha256: str
    member_count: int
    total_bytes: int


def _check_output_path_ancestors(target: Path, label: str) -> None:
    if target.anchor and not target.is_absolute():
        raise ValueError(f"ordinary {label} path required: {target}")
    for candidate in (target, *target.parents):
        try:
            info = candidate.lstat()
        except FileNotFoundError:
            continue
        except OSError:
            raise ValueError(f"ordinary {label} path required: {candidate}") from None
        if is_reparse_or_symlink(info):
            raise ValueError(f"symlink or junction refused for {label}: {candidate}")


def _walk_ordinary_domain_files(domain_root: Path) -> list[tuple[str, Path]]:
    checked_root = check_ordinary_ancestors(
        domain_root, expect_dir=True, error_prefix="ordinary domain directory required"
    ).resolve(strict=True)
    found: list[tuple[str, Path]] = []
    stack: list[tuple[Path, str]] = [(checked_root, "")]
    while stack:
        current, rel_prefix = stack.pop()
        with os.scandir(current) as entries:
            ordered = sorted(entries, key=lambda item: item.name)
        for entry in ordered:
            name = entry.name
            if name in _IGNORED_EXPORT_DIRS:
                continue
            info = entry.stat(follow_symlinks=False)
            if is_reparse_or_symlink(info):
                raise ValueError(f"symlink or reparse point refused in domain tree: {entry.path}")
            child_rel = f"{rel_prefix}/{name}" if rel_prefix else name
            child_path = Path(entry.path)
            if stat.S_ISDIR(info.st_mode):
                if not is_ordinary_dir_stat(info):
                    raise ValueError(f"ordinary directory required in domain tree: {entry.path}")
                stack.append((child_path, child_rel))
            elif stat.S_ISREG(info.st_mode):
                if not is_ordinary_file_stat(info):
                    raise ValueError(f"ordinary file required in domain tree: {entry.path}")
                if child_rel == DOMAIN_PACKAGE_MANIFEST_FILE:
                    continue
                safe_rel = relative_path(child_rel)
                found.append((safe_rel, child_path))
            else:
                raise ValueError(f"special file refused in domain tree: {entry.path}")
    found.sort(key=lambda item: item[0])
    if len(found) > MAX_MEMBER_COUNT:
        raise ValueError(f"domain package exceeds max member count ({MAX_MEMBER_COUNT})")
    return found


def _derive_defaults_from_domain_json(
    domain_root: Path, files_map: Mapping[str, Path]
) -> dict[str, Any]:
    defaults: dict[str, Any] = {}
    if "domain.json" in files_map:
        raw = files_map["domain.json"].read_bytes()
        doc = strict_json_loads(raw, domain_root / "domain.json", require_object=True)
        if doc.get("format") == DOMAIN_FORMAT:
            if isinstance(doc.get("id"), str) and doc["id"].strip():
                defaults["package_id"] = doc["id"].strip()
                defaults["domain_kind"] = doc["id"].strip()
            if isinstance(doc.get("version"), str) and doc["version"].strip():
                defaults["version"] = doc["version"].strip()
            if isinstance(doc.get("requires_domains"), list):
                defaults["requires_domains"] = list(doc["requires_domains"])
            vocab = doc.get("relation_vocabulary")
            if isinstance(vocab, dict) and isinstance(vocab.get("predicates"), list):
                defaults["exports_relations"] = list(vocab["predicates"])
            if isinstance(doc.get("schemas"), dict):
                defaults["artifact_types"] = sorted(doc["schemas"].keys())
            tools = doc.get("tools")
            if isinstance(tools, list):
                defaults["workflow_entrypoints"] = [
                    str(tool["id"])
                    for tool in tools
                    if isinstance(tool, dict) and isinstance(tool.get("id"), str)
                ]
    pkg_meta = domain_root / DOMAIN_PACKAGE_MANIFEST_FILE
    if pkg_meta.exists() and not pkg_meta.is_symlink() and is_ordinary_file_stat(pkg_meta.lstat()):
        prev = strict_json_loads(pkg_meta.read_bytes(), pkg_meta, require_object=True)
        for key in (
            "package_id",
            "version",
            "domain_kind",
            "license",
            *_MANIFEST_LIST_FIELDS,
            "pricing_metadata",
        ):
            if key in prev:
                defaults[key] = prev[key]
    return defaults


def _write_zip_entry(zf: zipfile.ZipFile, arcname: str, data: bytes) -> None:
    zinfo = zipfile.ZipInfo(filename=arcname, date_time=_DETERMINISTIC_ZIP_DATE)
    zinfo.compress_type = zipfile.ZIP_DEFLATED
    zinfo.create_system = 3
    zinfo.external_attr = (stat.S_IFREG | 0o644) << 16
    zf.writestr(zinfo, data, compress_type=zipfile.ZIP_DEFLATED)


def export_domain_package(
    domain_dir: Path | str,
    out_bundle: Path | str,
    *,
    package_id: str | None = None,
    version: str | None = None,
    domain_kind: str | None = None,
    license: str | None = None,
    capabilities: Iterable[str] | None = None,
    requires_domains: Iterable[str] | None = None,
    exports_relations: Iterable[str] | None = None,
    artifact_types: Iterable[str] | None = None,
    workflow_entrypoints: Iterable[str] | None = None,
    pricing_metadata: Mapping[str, Any] | None = None,
) -> DomainPackageManifest:
    """Export an ordinary domain directory into a deterministic verified ZIP bundle."""
    domain_path = Path(domain_dir)
    bundle_path = Path(out_bundle)
    _check_output_path_ancestors(bundle_path, "domain package bundle")

    files = _walk_ordinary_domain_files(domain_path)
    if not files:
        raise ValueError("cannot export an empty domain directory")

    files_map = dict(files)
    defaults = _derive_defaults_from_domain_json(domain_path, files_map)

    sha_tree: dict[str, str] = {}
    payloads: list[tuple[str, bytes]] = []
    total_bytes = 0
    for rel, path in files:
        raw = path.read_bytes()
        if len(raw) > MAX_MEMBER_BYTES:
            raise ValueError(f"domain member exceeds max member size ({MAX_MEMBER_BYTES}): {rel}")
        total_bytes += len(raw)
        if total_bytes > MAX_PACKAGE_BYTES:
            raise ValueError(f"domain package exceeds max total size ({MAX_PACKAGE_BYTES})")
        sha_tree[rel] = sha(raw)
        payloads.append((rel, raw))

    resolved_pricing = (
        dict(pricing_metadata) if pricing_metadata is not None else defaults.get("pricing_metadata")
    )

    def _pick_list(
        val: Iterable[str] | None, key: str, default: tuple[str, ...] = ()
    ) -> tuple[str, ...]:
        return tuple(val if val is not None else defaults.get(key, default))

    manifest = DomainPackageManifest(
        package_id=package_id or defaults.get("package_id") or domain_path.name,
        version=version or defaults.get("version") or "0.1.0",
        domain_kind=domain_kind or defaults.get("domain_kind") or "generic",
        license=license or defaults.get("license") or "UNLICENSED",
        capabilities=_pick_list(capabilities, "capabilities", ("schemas",)),
        requires_domains=_pick_list(requires_domains, "requires_domains"),
        exports_relations=_pick_list(exports_relations, "exports_relations"),
        artifact_types=_pick_list(artifact_types, "artifact_types"),
        workflow_entrypoints=_pick_list(workflow_entrypoints, "workflow_entrypoints"),
        content_sha256_tree=sha_tree,
        pricing_metadata=resolved_pricing,
    )

    manifest_bytes = encode(manifest.to_dict())
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        _write_zip_entry(zf, DOMAIN_PACKAGE_MANIFEST_FILE, manifest_bytes)
        for rel, raw in payloads:
            _write_zip_entry(zf, DOMAIN_PACKAGE_FILES_PREFIX + rel, raw)

    bundle_path.parent.mkdir(parents=True, exist_ok=True)
    check_ordinary_ancestors(
        bundle_path.parent, expect_dir=True, error_prefix="ordinary bundle directory required"
    )
    atomic_bytes(bundle_path, buf.getvalue())
    return manifest


def _validate_zip_member_info(info: zipfile.ZipInfo, seen_names: set[str]) -> str | None:
    name = info.filename
    if not name or "\x00" in name or "\\" in name or name.startswith("/"):
        raise ValueError(f"unsafe archive member path refused: {name!r}")
    if name in seen_names:
        raise ValueError(f"duplicate archive member refused: {name!r}")
    seen_names.add(name)

    unix_mode = (info.external_attr >> 16) & 0o170000
    if unix_mode == stat.S_IFLNK:
        raise ValueError(f"symlink archive member refused: {name!r}")
    if unix_mode not in (0, stat.S_IFREG, stat.S_IFDIR):
        raise ValueError(f"special archive member mode refused: {name!r}")

    if info.is_dir():
        stripped = name.rstrip("/")
        if stripped and stripped != DOMAIN_PACKAGE_FILES_PREFIX.rstrip("/"):
            if not stripped.startswith(DOMAIN_PACKAGE_FILES_PREFIX):
                raise ValueError(f"unexpected archive directory refused: {name!r}")
            relative_path(stripped[len(DOMAIN_PACKAGE_FILES_PREFIX) :])
        return None

    if name == DOMAIN_PACKAGE_MANIFEST_FILE:
        return DOMAIN_PACKAGE_MANIFEST_FILE
    if not name.startswith(DOMAIN_PACKAGE_FILES_PREFIX):
        raise ValueError(f"unexpected archive member outside files/: {name!r}")
    rel = name[len(DOMAIN_PACKAGE_FILES_PREFIX) :]
    safe_rel = relative_path(rel)
    if safe_rel != rel:
        raise ValueError(f"non-canonical archive member path refused: {name!r}")
    return safe_rel


def _read_zip_member_bounded(
    zf: zipfile.ZipFile, info: zipfile.ZipInfo, *, max_bytes: int
) -> bytes:
    if info.file_size > max_bytes:
        raise ValueError(f"archive member exceeds size bound ({max_bytes}): {info.filename!r}")
    chunks: list[bytes] = []
    read_total = 0
    with zf.open(info, "r") as stream:
        while True:
            chunk = stream.read(65536)
            if not chunk:
                break
            read_total += len(chunk)
            if read_total > max_bytes:
                raise ValueError(
                    f"archive member exceeds decompressed bound ({max_bytes}): {info.filename!r}"
                )
            chunks.append(chunk)
    return b"".join(chunks)


def _inspect_and_verify_bundle(
    bundle: Path | str,
) -> tuple[DomainPackageVerification, dict[str, bytes]]:
    checked_bundle = check_ordinary_ancestors(
        Path(bundle), expect_dir=False, error_prefix="ordinary domain package bundle required"
    )
    bundle_info = checked_bundle.lstat()
    if not is_ordinary_file_stat(bundle_info):
        raise ValueError(f"ordinary domain package file required: {checked_bundle}")

    bundle_hasher = hashlib.sha256()
    with checked_bundle.open("rb") as handle:
        while True:
            block = handle.read(65536)
            if not block:
                break
            bundle_hasher.update(block)
    bundle_sha = bundle_hasher.hexdigest()

    try:
        zf = zipfile.ZipFile(checked_bundle, "r")
    except zipfile.BadZipFile as exc:
        raise ValueError(f"invalid domain package archive: {exc}") from exc

    with zf:
        infos = zf.infolist()
        if len(infos) > MAX_MEMBER_COUNT + 2:
            raise ValueError(f"archive member count exceeds limit ({MAX_MEMBER_COUNT})")

        seen_names: set[str] = set()
        manifest_info: zipfile.ZipInfo | None = None
        payload_infos: dict[str, zipfile.ZipInfo] = {}
        for info in infos:
            classified = _validate_zip_member_info(info, seen_names)
            if classified is None:
                continue
            if classified == DOMAIN_PACKAGE_MANIFEST_FILE:
                manifest_info = info
            else:
                payload_infos[classified] = info

        if manifest_info is None:
            raise ValueError(f"domain package missing {DOMAIN_PACKAGE_MANIFEST_FILE}")

        manifest_raw = _read_zip_member_bounded(zf, manifest_info, max_bytes=MAX_MEMBER_BYTES)
        manifest_doc = strict_json_loads(
            manifest_raw, DOMAIN_PACKAGE_MANIFEST_FILE, require_object=True
        )
        manifest = DomainPackageManifest.from_dict(manifest_doc)

        expected_keys = set(manifest.content_sha256_tree.keys())
        actual_keys = set(payload_infos.keys())
        if actual_keys != expected_keys:
            missing = sorted(expected_keys - actual_keys)
            extra = sorted(actual_keys - expected_keys)
            raise ValueError(
                f"archive payload mismatch against manifest tree "
                f"(missing={missing[:5]}, extra={extra[:5]})"
            )

        verified_bodies: dict[str, bytes] = {}
        total_bytes = 0
        for rel in sorted(payload_infos):
            body = _read_zip_member_bounded(zf, payload_infos[rel], max_bytes=MAX_MEMBER_BYTES)
            total_bytes += len(body)
            if total_bytes > MAX_PACKAGE_BYTES:
                raise ValueError(f"archive total payload exceeds bound ({MAX_PACKAGE_BYTES})")
            if sha(body) != manifest.content_sha256_tree[rel]:
                raise ValueError(f"SHA-256 mismatch for domain package member: {rel}")
            verified_bodies[rel] = body

        if "domain.json" in verified_bodies:
            domain_doc = strict_json_loads(
                verified_bodies["domain.json"], "domain.json", require_object=True
            )
            if domain_doc.get("format") == DOMAIN_FORMAT and domain_doc.get("engine_api") not in (
                None,
                ENGINE_API,
            ):
                raise ValueError("incompatible domain.json engine_api in domain package")

    verification = DomainPackageVerification(
        manifest=manifest,
        bundle_sha256=bundle_sha,
        member_count=len(verified_bodies),
        total_bytes=total_bytes,
    )
    return verification, verified_bodies


def verify_domain_package(bundle: Path | str) -> DomainPackageVerification:
    """Verify archive safety, manifest schema, and SHA-256 integrity of a domain package."""
    verification, _ = _inspect_and_verify_bundle(bundle)
    return verification


def _ensure_ordinary_destination(dest: Path, *, overwrite: bool) -> Path:
    _check_output_path_ancestors(dest, "domain import destination")
    if dest.exists():
        info = dest.lstat()
        if not is_ordinary_dir_stat(info):
            raise ValueError(f"ordinary destination directory required: {dest}")
        if any(dest.iterdir()) and not overwrite:
            raise ValueError(f"destination directory is not empty: {dest}")
    else:
        dest.mkdir(parents=True, exist_ok=True)
    return check_ordinary_ancestors(
        dest, expect_dir=True, error_prefix="ordinary destination directory required"
    ).resolve(strict=True)


def import_domain_package(
    bundle: Path | str,
    dest_dir: Path | str,
    *,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Fail-closed verify and extract a composable domain package into ``dest_dir``."""
    verification, verified_bodies = _inspect_and_verify_bundle(bundle)
    checked_dest = _ensure_ordinary_destination(Path(dest_dir), overwrite=overwrite)

    with tempfile.TemporaryDirectory(
        prefix=".domain-pkg-stage-", dir=str(checked_dest.parent)
    ) as stage_str:
        stage_root = Path(stage_str).resolve(strict=True)
        for rel, body in sorted(verified_bodies.items()):
            safe_rel = relative_path(rel)
            staged_target = (stage_root / safe_rel).resolve()
            if not staged_target.is_relative_to(stage_root):
                raise ValueError(f"staged import target escapes stage root: {rel}")
            staged_target.parent.mkdir(parents=True, exist_ok=True)
            staged_target.write_bytes(body)

        manifest_bytes = encode(verification.manifest.to_dict())
        (stage_root / DOMAIN_PACKAGE_MANIFEST_FILE).write_bytes(manifest_bytes)

        for rel in (*sorted(verified_bodies), DOMAIN_PACKAGE_MANIFEST_FILE):
            target_path = checked_dest / rel
            _check_output_path_ancestors(target_path, "domain import target")
            resolved_target = target_path.parent.resolve() / target_path.name
            if not resolved_target.is_relative_to(checked_dest):
                raise ValueError(f"import target escapes destination root: {rel}")
            target_path.parent.mkdir(parents=True, exist_ok=True)
            check_ordinary_ancestors(
                target_path.parent,
                expect_dir=True,
                error_prefix="ordinary import parent directory required",
            )
            if target_path.exists() and not is_ordinary_file_stat(target_path.lstat()):
                raise ValueError(f"non-ordinary existing target refused: {target_path}")
            shutil.copyfile(stage_root / rel, target_path)

    return {
        "format": DOMAIN_PACKAGE_FORMAT,
        "manifest": verification.manifest.to_dict(),
        "bundle_sha256": verification.bundle_sha256,
        "dest_dir": str(checked_dest),
        "extracted_files": sorted(verified_bodies),
        "member_count": verification.member_count,
        "total_bytes": verification.total_bytes,
    }


__all__ = [
    "DOMAIN_PACKAGE_FILES_PREFIX",
    "DOMAIN_PACKAGE_FORMAT",
    "DOMAIN_PACKAGE_MANIFEST_FILE",
    "MAX_MEMBER_BYTES",
    "MAX_MEMBER_COUNT",
    "MAX_PACKAGE_BYTES",
    "DomainPackageManifest",
    "DomainPackageVerification",
    "export_domain_package",
    "import_domain_package",
    "verify_domain_package",
]
