"""Resolve selected complete bodies and verify portable data bundle interchange.

Uses managed_ai_information.platform exclusively for cooperative file locking, exclusive
no-clobber publishing, and directory fsync (zero inline OS branches).
"""

import io
import os
import shutil
import stat
import tempfile
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from managed_ai_information.platform import (
    is_reparse_or_symlink,
    lock_file,
    publish_exclusive,
    sync_dir,
    unlock_file,
)

from .config import DEFAULT_LIBRARY_CONFIG, LibraryConfig
from .format import (
    check_manifest,
    decoded,
    digest,
    encoded,
    integer,
    need,
    ordinary,
    read_bytes,
    relative,
    sha,
    text,
)


def resolve(
    libraries: list[Any],
    selection: dict[str, Any],
    limit: int | None = None,
    *,
    config: LibraryConfig = DEFAULT_LIBRARY_CONFIG,
) -> tuple[
    dict[tuple[str, str], tuple[Any, dict[str, Any]]],
    dict[tuple[str, str], tuple[Any, dict[str, Any]]],
    dict[tuple[str, str], bytes],
    int,
]:
    """Resolve and verify every complete body required by an explicit selection."""
    limits = config.limits
    effective_limit = limits.max_package_bytes if limit is None else limit
    integer(effective_limit, "selection body bytes", 1, limits.max_package_bytes)

    need(
        isinstance(selection, dict)
        and set(selection) == {"format", "purpose", "bodies", "modules"},
        "invalid selection fields",
    )
    need(selection["format"] == config.selection_format, "unsupported selection format")
    text(selection["purpose"], "receiving purpose", limits.max_text_bytes)

    by_hash = {lib.sha256: lib for lib in libraries}
    chosen: dict[tuple[str, str], tuple[Any, dict[str, Any]]] = {}
    chosen_modules: dict[tuple[str, str], tuple[Any, dict[str, Any]]] = {}
    seen: set[tuple[str, str]] = set()

    for key in ("bodies", "modules"):
        need(
            isinstance(selection[key], list) and len(selection[key]) <= limits.max_bodies,
            "invalid selected " + key,
        )
        for pin in selection[key]:
            need(
                isinstance(pin, dict) and set(pin) == {"library_sha256", "id", "sha256"},
                "invalid source pin",
            )
            h = sha(pin["library_sha256"])
            sha(pin["sha256"])
            relative(pin["id"])
            need(h in by_hash, "selected library unavailable or changed")

            lib = by_hash[h]
            available = lib.bodies if key == "bodies" else lib.modules
            need(pin["id"] in available, "selected identity unavailable")

            expected = lib.pin(available[pin["id"]], "body" if key == "bodies" else "module")
            need(pin == expected, "selected source pin changed")

            if key == "bodies":
                chosen[(h, pin["id"])] = (lib, available[pin["id"]])
            else:
                pending = [pin["id"]]
                while pending:
                    ident = pending.pop()
                    if (h, ident) in seen:
                        continue
                    seen.add((h, ident))
                    row = lib.modules[ident]
                    chosen_modules[(h, ident)] = (lib, row)
                    for body_id in row["members"]:
                        chosen[(h, body_id)] = (lib, lib.bodies[body_id])
                    pending.extend(row["requires"])

    need(chosen, "selection needs at least one complete body")

    total = 0
    data: dict[tuple[str, str], bytes] = {}
    for body_key, (lib, row) in chosen.items():
        need(row["bytes"] <= effective_limit - total, "selected complete bodies exceed byte bound")
        body = lib.body(row["id"])
        total += len(body)
        data[body_key] = body

    return chosen, chosen_modules, data, total


def context(
    libraries: list[Any],
    selection: dict[str, Any],
    max_bytes: int = DEFAULT_LIBRARY_CONFIG.search.default_max_bytes,
    *,
    config: LibraryConfig = DEFAULT_LIBRARY_CONFIG,
) -> dict[str, Any]:
    """Assemble complete selected bodies and module metadata for explicit context exposure."""
    limits = config.limits
    integer(max_bytes, "context bytes", 1, limits.max_package_bytes)
    rows, modules, data, total = resolve(libraries, selection, max_bytes, config=config)

    result = {
        "format": "knowledge-context/1",
        "purpose": selection["purpose"],
        "selection": selection,
        "body_bytes": total,
        "modules": [{"pin": lib.pin(row, "module"), "entry": row} for lib, row in modules.values()],
        "bodies": [
            {"pin": lib.pin(row), "entry": row, "text": data[key].decode("utf-8")}
            for key, (lib, row) in rows.items()
        ],
        "scope": (
            "Complete selected source exposure. No inferred applicability, model invocation, "
            "publication or effect authority."
        ),
    }

    need(
        len(encoded(result)) <= limits.max_package_bytes + limits.max_manifest_bytes,
        "rendered context exceeds total byte bound",
    )
    return result


def product(
    libraries: list[Any],
    selection: dict[str, Any],
    edition: str,
    *,
    config: LibraryConfig = DEFAULT_LIBRARY_CONFIG,
) -> tuple[bytes, dict[str, bytes]]:
    """Produce a validated manifest and object map for a selected library bundle."""
    limits = config.limits
    rows, modules, data, _ = resolve(libraries, selection, config=config)

    body_map: dict[str, dict[str, Any]] = {}
    module_map: dict[str, dict[str, Any]] = {}
    objects: dict[str, bytes] = {}

    for key, (_, row) in rows.items():
        need(
            row["id"] not in body_map or body_map[row["id"]] == row,
            "body identity collision; select one version explicitly: " + row["id"],
        )
        body_map[row["id"]] = row
        objects[row["object"]] = data[key]

    for _, row in modules.values():
        need(
            row["id"] not in module_map or module_map[row["id"]] == row,
            "module identity collision; select one interpretation explicitly: " + row["id"],
        )
        module_map[row["id"]] = row

    refs = [
        {"from": row["id"], **ref}
        for row in body_map.values()
        for ref in row["references"]
        if ref["kind"] == "url" or ref["target"] not in body_map
    ]

    manifest = {
        "format": config.library_format,
        "edition": text(edition, "edition", limits.max_edition_bytes),
        "bodies": sorted(body_map.values(), key=lambda x: x["id"]),
        "modules": sorted(module_map.values(), key=lambda x: x["id"]),
        "provenance": {
            "selection": selection,
            "source_libraries": sorted({lib.sha256 for lib, _ in rows.values()}),
            "outside_reference_routes": refs,
            "scope": (
                "The required bodies/modules declared by this selection are included. "
                "Other reference routes are disclosed, not asserted to be supplied prerequisites. "
                "Parent roles do not imply inherited permission or execution."
            ),
        },
    }

    check_manifest(manifest, config=config)
    raw = encoded(manifest)
    need(len(raw) <= limits.max_manifest_bytes, "product manifest exceeds byte bound")
    return raw, objects


def export(
    libraries: list[Any],
    selection: dict[str, Any],
    edition: str,
    output: Path | str,
    *,
    config: LibraryConfig = DEFAULT_LIBRARY_CONFIG,
) -> dict[str, Any]:
    """Deterministically export a verified data bundle ZIP without clobbering existing files."""
    raw, objects = product(libraries, selection, edition, config=config)
    target = ordinary(output)
    need(target.parent.is_dir(), "existing output parent required")
    need(not target.exists(), "output exists; inspect it rather than overwrite")

    temp = target.with_name("." + target.name + "." + os.urandom(16).hex() + ".partial")
    try:
        with temp.open("xb") as stream:
            with zipfile.ZipFile(
                stream, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
            ) as archive:
                for name, data in sorted({"catalogue.json": raw, **objects}.items()):
                    info = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
                    info.compress_type = zipfile.ZIP_DEFLATED
                    info.external_attr = 0o100644 << 16
                    archive.writestr(info, data)
            stream.flush()
            os.fsync(stream.fileno())

        publish_exclusive(
            temp, target, error_message="output exists; inspect it rather than overwrite"
        )
        sync_dir(target.parent)
    finally:
        temp.unlink(missing_ok=True)

    return inspect_bundle(target, config=config)


def bundle_bytes(
    path: Path | str,
    *,
    config: LibraryConfig = DEFAULT_LIBRARY_CONFIG,
) -> tuple[bytes, bytes, dict[str, Any], dict[str, bytes]]:
    """Read and cryptographically verify every member of a portable data bundle archive."""
    limits = config.limits
    archive_bytes = read_bytes(path, limits.max_archive_bytes)

    with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
        infos = archive.infolist()
        names = [x.filename for x in infos]
        need(
            len(names) == len(set(names)) and len(names) <= limits.max_bodies + 1,
            "duplicate or excessive archive members",
        )
        need("catalogue.json" in names, "bundle catalogue absent")

        for info in infos:
            need(not info.is_dir() and not info.flag_bits & 1, "unsupported bundle member")
            mode = info.external_attr >> 16
            need(stat.S_IFMT(mode) in {0, stat.S_IFREG}, "bundle member is not an ordinary file")
            bound = (
                limits.max_manifest_bytes
                if info.filename == "catalogue.json"
                else limits.max_body_bytes
            )
            need(info.file_size <= bound, "bundle member exceeds byte bound")

        need(
            sum(x.file_size for x in infos) <= limits.max_package_bytes + limits.max_manifest_bytes,
            "bundle total exceeds byte bound",
        )

        def get(member_name: str, bound: int) -> bytes:
            with archive.open(member_name) as stream:
                data = stream.read(bound + 1)
            need(len(data) <= bound, "bundle member grew beyond byte bound")
            return data

        raw = get("catalogue.json", limits.max_manifest_bytes)
        manifest = check_manifest(decoded(raw), config=config)
        expected = {"catalogue.json"} | {row["object"] for row in manifest["bodies"]}
        need(set(names) == expected, "bundle has missing or unselected members")

        objects: dict[str, bytes] = {}
        for row in manifest["bodies"]:
            if row["object"] not in objects:
                objects[row["object"]] = get(row["object"], limits.max_body_bytes)
            data = objects[row["object"]]
            need(
                len(data) == row["bytes"] and digest(data) == row["sha256"],
                "bundle body/source mismatch: " + row["id"],
            )
            data.decode("utf-8")

    return archive_bytes, raw, manifest, objects


def inspect_bundle(
    path: Path | str,
    *,
    config: LibraryConfig = DEFAULT_LIBRARY_CONFIG,
) -> dict[str, Any]:
    """Verify a portable bundle archive and report its content-addressed summary."""
    archive, raw, manifest, objects = bundle_bytes(path, config=config)
    return {
        "format": "knowledge-package-inspection/1",
        "archive_sha256": digest(archive),
        "archive_bytes": len(archive),
        "library_sha256": digest(raw),
        "edition": manifest["edition"],
        "bodies": len(manifest["bodies"]),
        "modules": len(manifest["modules"]),
        "body_bytes": sum(len(x) for x in objects.values()),
        "complete_declared_closure": True,
        "provenance": manifest["provenance"],
        "scope": "Verified data package; no semantic acceptance or executable installation.",
    }


def _open_import_lock(lock_path: Path) -> Any:
    flags = (
        os.O_CREAT
        | os.O_RDWR
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_BINARY", 0)
    )
    fd = os.open(lock_path, flags, 0o600)
    try:
        need(stat.S_ISREG(os.fstat(fd).st_mode), "import lock must be an ordinary file")
        return os.fdopen(fd, "a+b")
    except BaseException:
        os.close(fd)
        raise


def import_bundle(
    path: Path | str,
    store: Path | str,
    *,
    open_library: Callable[[Path], Any] | None = None,
    config: LibraryConfig = DEFAULT_LIBRARY_CONFIG,
) -> dict[str, Any]:
    """Import a verified bundle archive into a caller-owned content-addressed directory."""
    if open_library is None:
        from .catalogue import Library

        open_library = Library

    archive, raw, manifest, objects = bundle_bytes(path, config=config)
    store_dir = ordinary(store)
    need(store_dir.is_dir(), "existing caller-owned store directory required")

    ident = digest(archive)
    destination = store_dir / ident
    lock_path = ordinary(store_dir / "import.lock")

    with _open_import_lock(lock_path) as stream:
        locked = False
        try:
            lock_file(stream, shared=False, nonblocking=False, timeout=60.0)
            locked = True

            need(not is_reparse_or_symlink(destination), "import destination is a symlink")
            if destination.exists():
                existing = open_library(destination)
                try:
                    existing.verify()
                    need(existing.raw == raw, "content-addressed import destination differs")
                finally:
                    existing.close()

                expected = {"catalogue.json"} | set(objects)
                actual = {
                    p.relative_to(destination).as_posix()
                    for p in destination.rglob("*")
                    if p.is_file() or p.is_symlink()
                }
                need(actual == expected, "existing import has unexpected members")
                status = "already-present-verified"
            else:
                temporary = Path(tempfile.mkdtemp(prefix="." + ident + ".", dir=store_dir))
                try:
                    (temporary / "objects").mkdir()
                    for name, body in {"catalogue.json": raw, **objects}.items():
                        with (temporary / name).open("xb") as out_stream:
                            out_stream.write(body)
                            out_stream.flush()
                            os.fsync(out_stream.fileno())
                    sync_dir(temporary / "objects")
                    sync_dir(temporary)
                    os.rename(temporary, destination)
                    sync_dir(store_dir)
                    status = "imported-data"
                except BaseException:
                    if temporary.exists():
                        shutil.rmtree(temporary)
                    raise
        finally:
            if locked:
                unlock_file(stream)

    return {
        "format": "knowledge-import/1",
        "status": status,
        "archive_sha256": ident,
        "library_sha256": digest(raw),
        "library": str(destination),
        "edition": manifest["edition"],
        "bodies": len(manifest["bodies"]),
        "modules": len(manifest["modules"]),
        "authority": (
            "Data only. No source owner, credentials, effect permission, "
            "current selection or executable capability is imported."
        ),
    }
