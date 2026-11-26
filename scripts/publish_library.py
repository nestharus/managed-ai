#!/usr/bin/env python3
"""Publish complete knowledge bodies from an explicitly selected source tree.

This authoring operation constructs a fresh library. It does not activate it,
change accepted sources, select an engine, or install executable knowledge.
"""

import argparse
import os
import posixpath
import re
import shutil
import sys
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit

sys.dont_write_bytecode = True

REPO_ROOT = Path(__file__).resolve().parents[1]
for _entry in (str(REPO_ROOT / "src"), str(REPO_ROOT)):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

from managed_ai.library.catalogue import Library  # noqa: E402
from managed_ai.library.format import (  # noqa: E402
    FORMAT,
    MAX_BODY,
    MAX_MANIFEST,
    MAX_PACKAGE,
    check_manifest,
    decoded,
    digest,
    encoded,
    need,
    ordinary,
    read_bytes,
    relative,
)
from managed_ai.library.format import (  # noqa: E402
    text as check_text,
)
from managed_ai.platform import sync_dir  # noqa: E402

DEFAULT_SOURCE_MAP = REPO_ROOT / "src/managed_ai/library/source-map.json"
DEFAULT_LIBRARY = REPO_ROOT / "src/managed_ai/library/library"


def owner(root: Path, overlay: Path | None, path: str) -> Path:
    relative(path)
    candidate = overlay / path if overlay is not None else None
    if candidate is not None and (candidate.exists() or candidate.is_symlink()):
        return ordinary(candidate)

    return ordinary(root / path)


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
    spec = decoded(read_bytes(path, MAX_MANIFEST))
    need(
        isinstance(spec, dict)
        and set(spec) == {"format", "families", "additional", "overrides", "modules"},
        "invalid source-map fields",
    )
    need(spec["format"] == "knowledge-source-map/1", "unsupported source map")

    for key in ("families", "additional"):
        need(isinstance(spec[key], list), "invalid " + key)
        for item in spec[key]:
            relative(item)

    need(isinstance(spec["overrides"], dict), "invalid source overrides")
    for ident, row in spec["overrides"].items():
        relative(ident)
        need(isinstance(row, dict) and set(row) == {"owner", "link_base"}, "invalid owner override")
        relative(row["owner"])
        relative(row["link_base"])

    need(isinstance(spec["modules"], list), "invalid module views")
    return spec


def _collect_family_ids(root: Path, overlay: Path | None, families: list[str]) -> set[str]:
    ids: set[str] = set()
    bases = [root] + ([overlay] if overlay is not None else [])
    for family in families:
        seen = False
        for base in bases:
            directory = ordinary(base / family)
            if not directory.exists():
                continue
            need(directory.is_dir(), "source family is not a directory")
            seen = True
            for path in directory.rglob("*.md"):
                ordinary(path)
                if path.is_file():
                    ids.add(path.relative_to(base).as_posix())
        need(seen, "required source family unavailable: " + family)

    return ids


def construct(
    root: Path, overlay: Path | None, spec: dict, edition: str
) -> tuple[bytes, dict[str, bytes]]:
    ordinary_root = ordinary(root)
    need(ordinary_root.is_dir(), "source root required")

    ordinary_overlay = ordinary(overlay) if overlay is not None else None
    if ordinary_overlay is not None:
        need(ordinary_overlay.is_dir(), "overlay directory required")

    ids = (
        set(spec["additional"])
        | set(spec["overrides"])
        | _collect_family_ids(ordinary_root, ordinary_overlay, spec["families"])
    )
    rows = []
    objects: dict[str, bytes] = {}
    total = 0

    for ident in sorted(ids):
        override = spec["overrides"].get(ident, {"owner": ident, "link_base": ident})
        path = owner(ordinary_root, ordinary_overlay, override["owner"])
        body = read_bytes(path, MAX_BODY)
        body.decode("utf-8")
        total += len(body)
        need(total <= MAX_PACKAGE, "source collection exceeds total byte bound")

        title, summary = navigation(body, ident)
        digest_hex = digest(body)
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
        "format": FORMAT,
        "edition": check_text(edition, "edition", 512),
        "bodies": rows,
        "modules": spec["modules"],
        "provenance": {
            "source_map_sha256": digest(encoded(spec)),
            "scope": (
                "Complete ordinary UTF-8 source copies from the selected authoring tree. "
                "Navigation summaries and overlapping module views are authored nominations. "
                "Publication does not prove an atomic source snapshot, complete theory, "
                "applicability or effect authority."
            ),
            "publication": "source-tree-acquisition/1",
        },
    }
    check_manifest(manifest)
    raw = encoded(manifest)
    need(len(raw) <= MAX_MANIFEST, "published catalogue exceeds byte bound")
    return raw, objects


def publish(root: Path, overlay: Path | None, spec: dict, edition: str, output: Path) -> dict:
    raw, objects = construct(root, overlay, spec, edition)
    target = ordinary(output)
    need(target.parent.is_dir(), "existing output parent required")

    if target.exists():
        existing = Library(target)
        existing.verify()
        need(
            existing.raw == raw, "output contains a different publication; choose a fresh directory"
        )
        expected = {"catalogue.json"} | set(objects)
        actual = {
            item.relative_to(target).as_posix()
            for item in target.rglob("*")
            if item.is_file() or item.is_symlink()
        }
        need(actual == expected, "existing publication has unexpected members")
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

    result = Library(target).verify()
    result["status"] = status
    result["library"] = str(target)
    return result


def audit(root: Path, overlay: Path | None, library: Library, spec: dict | None = None) -> dict:
    ordinary_root = ordinary(root)
    ordinary_overlay = ordinary(overlay) if overlay is not None else None
    results = []

    for row in library.bodies.values():
        try:
            body = read_bytes(
                owner(ordinary_root, ordinary_overlay, row["source"]["path"]), MAX_BODY
            )
            observed = digest(body)
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
        else digest(encoded(spec)) == library.catalogue["provenance"].get("source_map_sha256")
    )
    inventory = None
    if spec is not None:
        expected_raw, _ = construct(
            ordinary_root, ordinary_overlay, spec, library.catalogue["edition"]
        )
        expected = decoded(expected_raw)
        before = {row["id"]: row for row in library.catalogue["bodies"]}
        after = {row["id"]: row for row in expected["bodies"]}
        inventory = {
            "missing_current_bodies": sorted(set(after) - set(before)),
            "no_longer_selected_bodies": sorted(set(before) - set(after)),
            "changed_entries": sorted(
                ident for ident in set(before) & set(after) if before[ident] != after[ident]
            ),
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
        if all(item["status"] == "same" for item in results)
        and map_same is not False
        and inventory_same
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
            result = audit(args.root, args.overlay, Library(args.library), spec)

        sys.stdout.buffer.write(encoded(result))
        if args.action == "audit" and result["status"] != "same":
            raise SystemExit(1)
    except (OSError, ValueError, KeyError, UnicodeError) as error:
        sys.stderr.buffer.write(encoded({"status": "refused", "error": str(error)}))
        raise SystemExit(2) from None

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
