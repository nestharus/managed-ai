#!/usr/bin/env python3
"""Publish complete knowledge bodies from an explicitly selected source tree.

This authoring operation constructs a fresh library. It does not activate it,
change accepted sources, select an engine, or install executable knowledge.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path, PurePosixPath
import posixpath
import re
import shutil
import sys
from urllib.parse import unquote, urlsplit

sys.dont_write_bytecode = True

REPO_ROOT = Path(__file__).resolve().parents[1]
for _entry in (str(REPO_ROOT / "src"), str(REPO_ROOT)):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

from managed_ai.library.reader import knowledge
from managed_ai.platform import sync_dir

DEFAULT_SOURCE_MAP = REPO_ROOT / "src/managed_ai/library/source-map.json"
DEFAULT_LIBRARY = REPO_ROOT / "src/managed_ai/library/library"


def owner(root: Path, overlay: Path | None, path: str) -> Path:
    knowledge.relative(path)
    candidate = overlay / path if overlay is not None else None
    if candidate is not None and (candidate.exists() or candidate.is_symlink()):
        return knowledge.ordinary(candidate)

    return knowledge.ordinary(root / path)


def navigation(body: bytes, ident: str) -> tuple[str, str]:
    text = body.decode("utf-8")
    title = next(
        (line.lstrip("#").strip() for line in text.splitlines() if re.match(r"^#\s+", line)),
        ident,
    )
    paragraphs = re.split(r"\n\s*\n", text)
    summary = next(
        (
            re.sub(r"\s+", " ", paragraph.strip())
            for paragraph in paragraphs
            if paragraph.strip() and not paragraph.lstrip().startswith(("#", "```", "|", "<"))
        ),
        title,
    )
    return title[:240], summary[:700]


def references(body: bytes, link_base: str) -> list[dict[str, str]]:
    text = re.sub(r"```.*?```", "", body.decode("utf-8"), flags=re.S)
    found: list[dict[str, str]] = []
    for target in re.findall(r'\[[^\]]*\]\(([^\s)]+)(?:\s+"[^"]*")?\)', text):
        parts = urlsplit(target)
        if parts.scheme or target.startswith("//"):
            row = {"target": target, "anchor": "", "kind": "url"}
        else:
            if parts.path:
                joined = posixpath.join(str(PurePosixPath(link_base).parent), unquote(parts.path))
                path = posixpath.normpath(joined)
            else:
                path = link_base
            row = {"target": path, "anchor": unquote(parts.fragment), "kind": "source"}

        if row not in found:
            found.append(row)

    return found


def source_map(path: Path) -> dict:
    spec = knowledge.decoded(knowledge.read_bytes(path, knowledge.MAX_MANIFEST))
    knowledge.need(
        isinstance(spec, dict) and set(spec) == {"format", "families", "additional", "overrides", "modules"},
        "invalid source-map fields",
    )
    knowledge.need(spec["format"] == "knowledge-source-map/1", "unsupported source map")

    for key in ("families", "additional"):
        knowledge.need(isinstance(spec[key], list), "invalid " + key)
        for item in spec[key]:
            knowledge.relative(item)

    knowledge.need(isinstance(spec["overrides"], dict), "invalid source overrides")
    for ident, row in spec["overrides"].items():
        knowledge.relative(ident)
        knowledge.need(isinstance(row, dict) and set(row) == {"owner", "link_base"}, "invalid owner override")
        knowledge.relative(row["owner"])
        knowledge.relative(row["link_base"])

    knowledge.need(isinstance(spec["modules"], list), "invalid module views")
    return spec


def _collect_family_ids(root: Path, overlay: Path | None, families: list[str]) -> set[str]:
    ids: set[str] = set()
    bases = [root] + ([overlay] if overlay is not None else [])
    for family in families:
        seen = False
        for base in bases:
            directory = knowledge.ordinary(base / family)
            if not directory.exists():
                continue
            knowledge.need(directory.is_dir(), "source family is not a directory")
            seen = True
            for path in directory.rglob("*.md"):
                knowledge.ordinary(path)
                if path.is_file():
                    ids.add(path.relative_to(base).as_posix())
        knowledge.need(seen, "required source family unavailable: " + family)

    return ids


def construct(root: Path, overlay: Path | None, spec: dict, edition: str) -> tuple[bytes, dict[str, bytes]]:
    ordinary_root = knowledge.ordinary(root)
    knowledge.need(ordinary_root.is_dir(), "source root required")

    ordinary_overlay = knowledge.ordinary(overlay) if overlay is not None else None
    if ordinary_overlay is not None:
        knowledge.need(ordinary_overlay.is_dir(), "overlay directory required")

    ids = set(spec["additional"]) | set(spec["overrides"]) | _collect_family_ids(
        ordinary_root, ordinary_overlay, spec["families"]
    )
    rows = []
    objects: dict[str, bytes] = {}
    total = 0

    for ident in sorted(ids):
        override = spec["overrides"].get(ident, {"owner": ident, "link_base": ident})
        path = owner(ordinary_root, ordinary_overlay, override["owner"])
        body = knowledge.read_bytes(path, knowledge.MAX_BODY)
        body.decode("utf-8")
        total += len(body)
        knowledge.need(total <= knowledge.MAX_PACKAGE, "source collection exceeds total byte bound")

        title, summary = navigation(body, ident)
        digest_hex = knowledge.digest(body)
        object_path = "objects/" + digest_hex + ".txt"
        rows.append(
            {
                "id": ident,
                "title": title,
                "summary": summary,
                "object": object_path,
                "sha256": digest_hex,
                "bytes": len(body),
                "source": {"path": override["owner"], "sha256": digest_hex},
                "link_base": override["link_base"],
                "references": references(body, override["link_base"]),
            }
        )
        objects[object_path] = body

    manifest = {
        "format": knowledge.FORMAT,
        "edition": knowledge.text(edition, "edition", 512),
        "bodies": rows,
        "modules": spec["modules"],
        "provenance": {
            "source_map_sha256": knowledge.digest(knowledge.encoded(spec)),
            "scope": (
                "Complete ordinary UTF-8 source copies from the selected authoring tree. "
                "Navigation summaries and overlapping module views are authored nominations. "
                "Publication does not prove an atomic source snapshot, complete theory, "
                "applicability or effect authority."
            ),
            "publication": "source-tree-acquisition/1",
        },
    }
    knowledge.check_manifest(manifest)
    raw = knowledge.encoded(manifest)
    knowledge.need(len(raw) <= knowledge.MAX_MANIFEST, "published catalogue exceeds byte bound")
    return raw, objects


def publish(root: Path, overlay: Path | None, spec: dict, edition: str, output: Path) -> dict:
    raw, objects = construct(root, overlay, spec, edition)
    target = knowledge.ordinary(output)
    knowledge.need(target.parent.is_dir(), "existing output parent required")

    if target.exists():
        existing = knowledge.Library(target)
        existing.verify()
        knowledge.need(existing.raw == raw, "output contains a different publication; choose a fresh directory")
        expected = {"catalogue.json"} | set(objects)
        actual = {
            item.relative_to(target).as_posix()
            for item in target.rglob("*")
            if item.is_file() or item.is_symlink()
        }
        knowledge.need(actual == expected, "existing publication has unexpected members")
        status = "already-present-verified"
    else:
        staging = target.parent / (".publish-" + target.name + "-" + os.urandom(16).hex())
        try:
            staging.mkdir()
            (staging / "objects").mkdir()
            for name, body in {"catalogue.json": raw, **objects}.items():
                with (staging / name).open("xb") as stream:
                    stream.write(body)
                    stream.flush()
                    os.fsync(stream.fileno())
            sync_dir(staging / "objects")
            sync_dir(staging)
            os.rename(staging, target)
            sync_dir(target.parent)
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        status = "published-data"

    result = knowledge.Library(target).verify()
    result["status"] = status
    result["library"] = str(target)
    return result


def audit(root: Path, overlay: Path | None, library, spec: dict | None = None) -> dict:
    ordinary_root = knowledge.ordinary(root)
    ordinary_overlay = knowledge.ordinary(overlay) if overlay is not None else None
    results = []

    for row in library.bodies.values():
        try:
            body = knowledge.read_bytes(owner(ordinary_root, ordinary_overlay, row["source"]["path"]), knowledge.MAX_BODY)
            observed = knowledge.digest(body)
            status = "same" if observed == row["sha256"] else "changed"
            results.append(
                {
                    "id": row["id"],
                    "owner": row["source"]["path"],
                    "status": status,
                    "observed_sha256": observed,
                }
            )
        except (OSError, ValueError) as error:
            results.append(
                {
                    "id": row["id"],
                    "owner": row["source"]["path"],
                    "status": "unavailable",
                    "reason": str(error),
                }
            )

    map_same = (
        None
        if spec is None
        else knowledge.digest(knowledge.encoded(spec)) == library.catalogue["provenance"].get("source_map_sha256")
    )
    inventory = None
    if spec is not None:
        expected_raw, _ = construct(ordinary_root, ordinary_overlay, spec, library.catalogue["edition"])
        expected = knowledge.decoded(expected_raw)
        before = {row["id"]: row for row in library.catalogue["bodies"]}
        after = {row["id"]: row for row in expected["bodies"]}
        inventory = {
            "missing_current_bodies": sorted(set(after) - set(before)),
            "no_longer_selected_bodies": sorted(set(before) - set(after)),
            "changed_entries": sorted(ident for ident in set(before) & set(after) if before[ident] != after[ident]),
            "modules_same": expected["modules"] == library.catalogue["modules"],
        }

    inventory_same = inventory is None or (
        not inventory["missing_current_bodies"]
        and not inventory["no_longer_selected_bodies"]
        and not inventory["changed_entries"]
        and inventory["modules_same"]
    )
    overall = (
        "same"
        if all(item["status"] == "same" for item in results) and map_same is not False and inventory_same
        else "different"
    )
    return {
        "format": "knowledge-owner-audit/1",
        "library_sha256": library.sha256,
        "status": overall,
        "source_map_same": map_same,
        "current_inventory": inventory,
        "bodies": results,
        "scope": "Observed complete source bytes at nominated owners. "
        "No semantic equivalence or atomic concurrent-tree claim.",
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--overlay", type=Path)
    parser.add_argument("--source-map", type=Path, default=DEFAULT_SOURCE_MAP)
    sub = parser.add_subparsers(dest="action", required=True)

    pub = sub.add_parser("publish", allow_abbrev=False)
    pub.add_argument("--edition", required=True)
    pub.add_argument("--output", type=Path, required=True)

    check = sub.add_parser("audit", allow_abbrev=False)
    check.add_argument("--library", type=Path, default=DEFAULT_LIBRARY)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    try:
        spec = source_map(args.source_map)
        if args.action == "publish":
            result = publish(args.root, args.overlay, spec, args.edition, args.output)
        else:
            result = audit(args.root, args.overlay, knowledge.Library(args.library), spec)

        sys.stdout.buffer.write(knowledge.encoded(result))
        if args.action == "audit" and result["status"] != "same":
            raise SystemExit(1)
    except (OSError, ValueError, KeyError, UnicodeError) as error:
        sys.stderr.buffer.write(knowledge.encoded({"status": "refused", "error": str(error)}))
        raise SystemExit(2)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
