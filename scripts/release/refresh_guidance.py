#!/usr/bin/env python3
"""Refresh source identities from canonical maintained files, without body copies."""
from __future__ import annotations

import argparse
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
from stat import S_IMODE, S_ISDIR, S_ISREG
import sys
from tempfile import TemporaryDirectory

REPO_ROOT = Path(__file__).resolve().parents[2]
for _entry in (str(REPO_ROOT / "src"), str(REPO_ROOT)):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

from managed_ai.platform import (
    check_ordinary_ancestors,
    is_reparse_or_symlink_stat,
    strict_json_loads,
)

ROOT = REPO_ROOT / "src/managed_ai"


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def anchor(ident: str) -> str:
    return "source-" + digest(ident.encode())[:20]


def source_identity_line(row: dict) -> str:
    return (
        f"[{row['source']}]({row['source']}) — {row['bytes']} bytes; "
        f"SHA-256 `{row['sha256']}`."
    )


def _ordinary_entry(path: Path, kind, label: str):
    try:
        info = path.lstat()
    except OSError:
        raise ValueError(f"{label}: {path}") from None

    if is_reparse_or_symlink_stat(info) or not kind(info.st_mode):
        raise ValueError(f"{label}: {path}")

    return info


def _ordinary_path(path: Path, kind, label: str) -> Path:
    """Validate every ancestor and the entry itself before resolving."""
    validated = check_ordinary_ancestors(
        path,
        expect_dir=(kind is S_ISDIR),
        error_prefix=label,
    )
    return validated.resolve(strict=True)


def _ordinary_within(path: Path, boundary: Path, kind, label: str) -> Path:
    """Verify every segment from boundary down to path is ordinary."""
    resolved_boundary = _ordinary_path(boundary, S_ISDIR, label)
    normalized = Path(os.path.abspath(path))
    try:
        rel = normalized.relative_to(resolved_boundary)
    except ValueError:
        raise ValueError(f"{label}: {path}") from None

    cursor = resolved_boundary
    for part in rel.parts[:-1]:
        cursor = cursor / part
        _ordinary_entry(cursor, S_ISDIR, label)

    if rel.parts:
        cursor = cursor / rel.parts[-1]
        _ordinary_entry(cursor, kind, label)

    resolved = cursor.resolve(strict=True)
    if not resolved.is_relative_to(resolved_boundary):
        raise ValueError(f"{label}: {path}")

    return resolved


def _read_strict_object(path: Path, boundary: Path | None = None) -> dict:
    if boundary is not None:
        target = _ordinary_within(path, boundary, S_ISREG, "ordinary JSON file required")
    else:
        _ordinary_entry(path, S_ISREG, "ordinary JSON file required")
        target = path

    return strict_json_loads(target.read_bytes(), str(target), require_object=True)


def render_sources_markdown(selected: dict, nav: dict) -> bytes:
    """Render SOURCES.md bytes from current_sources and navigation objects."""
    lines = [
        "# Current source owners",
        "",
        "The native catalogue verifies the same canonical files linked here. Domain sources are selected by each store. "
        "No second library of source copies or predecessor runtime is required.",
        "",
    ]
    selected_ids = set()
    for row in selected["bodies"]:
        selected_ids.add(row["id"])
        lines += [
            '<a id="' + anchor(row["id"]) + '"></a>',
            "## " + row["id"],
            "",
            source_identity_line(row),
            "",
        ]

    for ident, route in sorted(nav.get("reference_routes", {}).items()):
        if ident in selected_ids:
            continue
        lines += [
            '<a id="' + anchor(ident) + '"></a>',
            "## " + ident,
            "",
            "[" + route["label"] + "](" + route["path"] + ").",
            "",
            route["scope"],
            "",
        ]

    for ident in nav.get("unavailable_references", []):
        lines += [
            '<a id="' + anchor(ident) + '"></a>',
            "## " + ident,
            "",
            "The former source is not installed at this logical path. "
            "Select a maintained account or explicitly acquire the exact original when its identity matters.",
            "",
        ]

    return "\n".join(lines).encode("utf-8")


def _engine_layout(root: Path) -> tuple[bool, Path, str, int]:
    if root.name == "managed_ai" and root.parent.name == "src":
        bundle_root = _ordinary_path(root.parents[1], S_ISDIR, "ordinary bundle root required")
        return True, bundle_root, "../../..", -2

    if root.name == "engine":
        bundle_root = _ordinary_path(root.parent, S_ISDIR, "ordinary bundle root required")
        return True, bundle_root, "../..", -1

    return False, root, "..", 0


def _resolve_source_entry(root: Path, bundle_root: Path, min_depth: int, row: dict) -> Path:
    spelling = row["source"]
    if not isinstance(spelling, str) or not spelling or "\\" in spelling or Path(spelling).anchor:
        raise ValueError("current source reference must be relative to the engine: " + row["id"])

    relative = Path(spelling)
    depth = 0
    for part in relative.parts:
        depth += -1 if part == ".." else 1
        if depth < min_depth:
            raise ValueError("current source reference escapes allowed package root: " + spelling)

    cursor = root
    for idx, part in enumerate(relative.parts):
        cursor = cursor.parent if part == ".." else cursor / part
        expected_kind = S_ISREG if idx == len(relative.parts) - 1 else S_ISDIR
        _ordinary_entry(cursor, expected_kind, "ordinary canonical source required")

    allowed_root = bundle_root if ".." in relative.parts else root
    return _ordinary_within(cursor, allowed_root, S_ISREG, "ordinary canonical source required")


def _locate_release_assets(resolved_root: Path, bundle_root: Path) -> tuple[Path, Path, Path]:
    """Locate current_sources.json and navigation.json (scripts/release/ first, legacy core/release/ fallback)."""
    if bundle_root != resolved_root:
        scripts_selection = bundle_root / "scripts/release/current_sources.json"
        try:
            scripts_selection.lstat()
        except FileNotFoundError:
            pass
        else:
            return scripts_selection, bundle_root / "scripts/release/navigation.json", bundle_root

    return (
        resolved_root / "release/current_sources.json",
        resolved_root / "release/navigation.json",
        resolved_root,
    )


def render(root: Path, *, include_sources_md: bool = False) -> tuple[dict[str, bytes], dict]:
    """Read canonical UTF-8 inputs and prepare each projection without writing."""
    resolved_root = _ordinary_path(root, S_ISDIR, "ordinary engine directory required")
    _, bundle_root, source_root_rel, min_depth = _engine_layout(resolved_root)

    selection_path, nav_path, selection_boundary = _locate_release_assets(resolved_root, bundle_root)
    catalogue_name = "store/catalogue.json"
    selected = _read_strict_object(selection_path, selection_boundary)
    catalogue = _read_strict_object(resolved_root / catalogue_name, resolved_root)
    prior = {entry["id"]: entry for entry in catalogue["entries"]}
    entries = []

    for row in selected["bodies"]:
        resolved = _resolve_source_entry(resolved_root, bundle_root, min_depth, row)
        raw = resolved.read_bytes()
        text = raw.decode("utf-8")
        row.update(sha256=digest(raw), bytes=len(raw), presentation=row["source"])
        title = next((line.lstrip("#").strip() for line in text.splitlines() if line.startswith("# ")), row["id"])
        entry = dict(prior[row["id"]])
        body_path = resolved.relative_to(bundle_root).as_posix()
        if "\\" in body_path:
            raise ValueError("current source reference has an unsupported filename: " + row["source"])

        entry.update(
            title=title,
            body_path=body_path,
            source_root_relative=source_root_rel,
            body_role="complete-current-source-reference",
            availability="available",
            source_root=selected["edition"],
            source_sha256=row["sha256"],
            sha256=row["sha256"],
            bytes=row["bytes"],
            body_id="sha256:" + row["sha256"],
        )
        entries.append(entry)

    selected_ids = {row["id"] for row in selected["bodies"]}
    catalogue["entries"] = entries + [entry for entry in catalogue["entries"] if entry["id"] not in selected_ids]
    catalogue["source_root"] = selected["edition"]
    selection = json_bytes(selected)
    selection_sha = digest(selection)
    catalogue["snapshot"] = {
        "edition": selected["edition"],
        "selection_sha256": selection_sha,
        "relationship": "References to one canonical source body; domain sources are selected per store.",
    }
    nav = _read_strict_object(nav_path, selection_boundary)
    marker = {
        "format": "managed-ai-shared-engine/1",
        "edition": selected["edition"],
        "engine_api": "managed-ai-runtime/1",
        "deployment_api": "agent-system-deployment/2",
        "store_schema": "managed-ai-store/1",
        "selection_sha256": selection_sha,
        "source_count": len(entries),
        "source_ownership": "Canonical maintained source files; reference catalogues do not duplicate their bodies.",
        "scope": "Same executable engine for independent schema-governed stores; domains and selected knowledge are data.",
    }

    selection_key = selection_path.relative_to(bundle_root).as_posix()
    core_prefix = "" if resolved_root == bundle_root else resolved_root.relative_to(bundle_root).as_posix() + "/"
    projections: dict[str, bytes] = {
        selection_key: selection,
        core_prefix + catalogue_name: json_bytes(catalogue),
    }
    if include_sources_md:
        projections[core_prefix + "SOURCES.md"] = render_sources_markdown(selected, nav)

    projections[core_prefix + "CURRENT-RELEASE.json"] = json_bytes(marker)
    summary = {"status": "refreshed", "canonical_sources": len(entries), "selection_sha256": selection_sha}
    return projections, summary


def refresh(root: Path, *, include_sources_md: bool = False) -> dict:
    """Stage complete files, then replace targets in order with the marker last."""
    resolved_root = _ordinary_path(root, S_ISDIR, "ordinary engine directory required")
    _, bundle_root, _, _ = _engine_layout(resolved_root)
    projections, summary = render(resolved_root, include_sources_md=include_sources_md)

    with ExitStack() as cleanup:
        staged_paths = []
        for name, raw in projections.items():
            rel_target = Path(name)
            cursor = bundle_root
            for part in rel_target.parts[:-1]:
                cursor = cursor / part
                _ordinary_entry(cursor, S_ISDIR, "ordinary projection directory required")

            target = cursor / rel_target.parts[-1]
            try:
                info = target.lstat()
            except FileNotFoundError:
                mode = None
            else:
                if is_reparse_or_symlink_stat(info) or not S_ISREG(info.st_mode):
                    raise ValueError("ordinary projection target required: " + str(target))
                if not target.resolve(strict=True).is_relative_to(bundle_root):
                    raise ValueError("ordinary projection target required: " + str(target))
                mode = S_IMODE(info.st_mode)

            scratch = cleanup.enter_context(TemporaryDirectory(dir=target.parent, prefix=f".{target.name}."))
            staged = Path(scratch) / target.name
            with staged.open("xb") as stream:
                if stream.write(raw) != len(raw):
                    raise OSError(f"incomplete write while staging {name}")
            if mode is not None:
                staged.chmod(mode)
            staged_paths.append((staged, target))

        for staged, target in staged_paths:
            staged.replace(target)

    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument(
        "--write-sources-md",
        action="store_true",
        help="Also write generated SOURCES.md (normally generated at package time)",
    )
    args = parser.parse_args()
    try:
        print(json.dumps(refresh(args.root, include_sources_md=args.write_sources_md)))
    except (OSError, ValueError) as error:
        raise SystemExit(str(error)) from None

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
