#!/usr/bin/env python3
"""Read canonical core or store-selected sources, or operate published source libraries."""

import argparse
import json
import sys
from pathlib import Path
from typing import Any

if __name__ == "__main__":
    sys.dont_write_bytecode = True
    _src_root = str(Path(__file__).resolve().parents[2])
    if _src_root not in sys.path:
        sys.path.insert(0, _src_root)

from managed_ai_information import __version__
from managed_ai_information.library.config import DEFAULT_LIBRARY_CONFIG

_SUBS = frozenset(
    {
        "verify",
        "show",
        "discover",
        "modules",
        "prepare-index",
        "discover-batch",
        "context",
        "export",
        "inspect",
        "import",
    }
)
_DFLAGS = frozenset({"--kind", "--limit", "--offset", "--content", "-h", "--help"})


def _is_top_level_library_invocation(argv: list[str]) -> bool:
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--":
            return False
        if a == "--library" or a.startswith("--library="):
            return True
        if a == "--store":
            i += 2
            continue
        if a.startswith("--store="):
            i += 1
            continue
        if a in _SUBS or not a.startswith("-"):
            return False
        i += 1
    return False


def _normalize_query_arg(argv: list[str]) -> list[str]:
    out = list(argv)
    for i, a in enumerate(out):
        if a == "--query" and i + 1 < len(out):
            n = out[i + 1]
            if (
                n.startswith("-")
                and n not in _DFLAGS
                and not n.startswith(("--kind=", "--limit=", "--offset="))
            ):
                out[i] = "--query=" + n
                del out[i + 1]
                break
    return out


def _build_library_parser() -> argparse.ArgumentParser:
    sc, bc = DEFAULT_LIBRARY_CONFIG.search, DEFAULT_LIBRARY_CONFIG.batch
    p = argparse.ArgumentParser(
        description="Browse, verify and acquire published source libraries.", allow_abbrev=False
    )
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    p.add_argument("--library", action="append", type=Path)
    sub = p.add_subparsers(dest="action", required=True)
    sub.add_parser("verify", allow_abbrev=False)
    sub.add_parser("modules", allow_abbrev=False)
    sh = sub.add_parser("show", allow_abbrev=False)
    sh.add_argument("identity")
    sh.add_argument("--text", action="store_true")

    def _lim(cmd: argparse.ArgumentParser, *, result: bool = True) -> None:
        if result:
            cmd.add_argument("--limit", type=int, default=sc.default_limit)
        cmd.add_argument("--scan-limit", type=int, default=sc.default_scan_limit)
        cmd.add_argument("--max-bytes", type=int, default=sc.default_max_bytes)

    d = sub.add_parser("discover", allow_abbrev=False)
    d.add_argument("--query", required=True)
    _lim(d)
    d.add_argument("--backend", choices=("bm25", "fts5", "literal"), default="bm25")
    d.add_argument(
        "--index",
        type=Path,
        help="Nominate from this prepared derivative; requires FTS5 and digest",
    )
    d.add_argument(
        "--index-sha256", help="Exact expected digest from the selected prepare-index receipt"
    )
    b = sub.add_parser(
        "discover-batch",
        allow_abbrev=False,
        help="Share one verified prepared image across queries",
    )
    b.add_argument(
        "--queries", type=Path, required=True, help="UTF-8 JSON array of 1 to 32 query strings"
    )
    b.add_argument("--index", type=Path, required=True)
    b.add_argument("--index-sha256", required=True)
    _lim(b)
    b.add_argument("--max-result-bytes", type=int, default=bc.max_result_bytes)
    pr = sub.add_parser(
        "prepare-index", allow_abbrev=False, help="Verify sources and build immutable FTS5 index"
    )
    pr.add_argument("--output", type=Path, required=True)
    _lim(pr, result=False)
    c = sub.add_parser("context", allow_abbrev=False)
    c.add_argument("--selection", type=Path, required=True)
    c.add_argument("--max-bytes", type=int, default=sc.default_max_bytes)
    e = sub.add_parser("export", allow_abbrev=False)
    e.add_argument("--selection", type=Path, required=True)
    e.add_argument("--edition", required=True)
    e.add_argument("--output", type=Path, required=True)
    sub.add_parser("inspect", allow_abbrev=False).add_argument("--bundle", type=Path, required=True)
    im = sub.add_parser("import", allow_abbrev=False)
    im.add_argument("--bundle", type=Path, required=True)
    im.add_argument("--store", type=Path, required=True)
    return p


def _dispatch_library_action(a: argparse.Namespace, libs: list[Any]) -> Any:
    from managed_ai_information.library.bundle import context, export
    from managed_ai_information.library.format import decoded, need, read_bytes

    if a.action == "verify":
        return {"libraries": [lb.verify() for lb in libs]}
    if a.action == "modules":
        return {
            "libraries": [
                {
                    "library_sha256": lb.sha256,
                    "modules": [
                        {"pin": lb.pin(r, "module"), "entry": r} for r in lb.modules.values()
                    ],
                }
                for lb in libs
            ]
        }
    if a.action == "show":
        found = [lb for lb in libs if a.identity in lb.bodies]
        need(found, f"identity absent from the selected libraries: {a.identity}")
        need(
            len(found) == 1,
            f"identity is present in several selected libraries: {a.identity}; "
            "select one with --library",
        )
        res = found[0].show(a.identity)
        if a.text:
            sys.stdout.write(res["text"])
            return None
        return res
    if a.action == "prepare-index":
        need(len(libs) == 1, "prepare-index requires one selected library")
        return libs[0].prepare_index(a.output, a.scan_limit, a.max_bytes)
    if a.action == "discover":
        need(a.index is None or len(libs) == 1, "prepared discovery requires one selected library")
        return {
            "libraries": [
                lb.discover(
                    a.query, a.limit, a.scan_limit, a.max_bytes, a.backend, a.index, a.index_sha256
                )
                for lb in libs
            ]
        }
    if a.action == "discover-batch":
        need(len(libs) == 1, "discover-batch requires one selected library")
        qs = decoded(read_bytes(a.queries, DEFAULT_LIBRARY_CONFIG.batch.max_input_bytes))
        return libs[0].discover_batch(
            qs,
            index=a.index,
            index_sha256=a.index_sha256,
            limit=a.limit,
            scan_limit=a.scan_limit,
            max_bytes=a.max_bytes,
            max_result_bytes=a.max_result_bytes,
        )
    sel = decoded(read_bytes(a.selection, DEFAULT_LIBRARY_CONFIG.limits.max_manifest_bytes))
    return (
        context(libs, sel, a.max_bytes)
        if a.action == "context"
        else export(libs, sel, a.edition, a.output)
    )


def run_library_cli(argv: list[str] | None = None) -> None:
    """CLI entrypoint for published source libraries."""
    import zipfile

    from managed_ai_information.library.bundle import import_bundle, inspect_bundle
    from managed_ai_information.library.catalogue import Library

    args = _build_library_parser().parse_args(argv)
    libs: list[Library] = []
    errs = (OSError, ValueError, KeyError, UnicodeError, RuntimeError, zipfile.BadZipFile)
    try:
        if args.action == "inspect":
            res: Any = inspect_bundle(args.bundle)
        elif args.action == "import":
            res = import_bundle(args.bundle, args.store, open_library=Library)
        else:
            dlib = Path(__file__).resolve().parent / "library"
            libs = [Library(p) for p in (args.library or [dlib])]
            if (res := _dispatch_library_action(args, libs)) is None:
                return
        print(json.dumps(res, ensure_ascii=False, indent=2))
    except errs as exc:
        print(
            json.dumps({"status": "refused", "operation": args.action, "error": str(exc)}),
            file=sys.stderr,
        )
        raise SystemExit(2) from None
    finally:
        for lb in libs:
            lb.close()


def _execute_catalogue_action(cat: Any, a: argparse.Namespace) -> Any:
    if a.action == "show":
        res = cat.search(a.identity)
        if a.text:
            sys.stdout.write(res["body"])
            return None
        return res
    if a.action == "discover":
        return cat.search(
            query=a.query, kind=a.kind, limit=a.limit, offset=a.offset, content=a.content
        )
    ver, sld = [], []
    for e in cat.index()["entries"]:
        if e.get("availability") == "sealed":
            sld.append(e["id"])
        else:
            ver.append({"id": e["id"], "sha256": e["sha256"], "bytes": len(cat.body(e))})
    scope = (
        "Exact canonical byte custody; no participant acquisition or semantic acceptance is "
        "inferred."
    )
    extra: dict[str, Any] = (
        {
            "verified_count": len(ver),
            "verified_bytes": sum(r["bytes"] for r in ver),
            "sealed_count": len(sld),
        }
        if getattr(a, "summary", False)
        else {"sources": ver}
    )
    return {"status": "verified", **extra, "sealed_without_body_read": sld, "scope": scope}


def _build_store_catalogue_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    p.add_argument("--store", type=Path)
    sub = p.add_subparsers(dest="action", required=True)
    sub.add_parser("verify", allow_abbrev=False).add_argument(
        "--summary",
        action="store_true",
        help="Return verified/sealed counts and byte totals instead of listing every source",
    )
    sh = sub.add_parser("show", allow_abbrev=False)
    sh.add_argument("identity")
    sh.add_argument("--text", action="store_true")
    d = sub.add_parser("discover", allow_abbrev=False)
    d.add_argument("--query", default="")
    d.add_argument("--kind")
    d.add_argument("--limit", type=int, default=25)
    d.add_argument("--offset", type=int, default=0)
    d.add_argument("--content", action="store_true")
    return p


def main(argv: list[str] | None = None) -> None:
    """Unified managed-ai-library CLI entrypoint."""
    raw = list(sys.argv[1:] if argv is None else argv)
    if _is_top_level_library_invocation(raw):
        run_library_cli(raw)
        return
    from managed_ai_information.core.ownership import NativeLayout, external_root
    from managed_ai_information.core.storage import (
        AdmissionContended,
        admission_deadline,
        owner_admission,
        shared,
    )
    from managed_ai_information.store.selection.installed_catalogue import InstalledCatalogue

    args = _build_store_catalogue_parser().parse_args(_normalize_query_arg(raw))
    eroot = Path(__file__).resolve().parent.parent
    try:
        if args.store:
            from managed_ai_information.store.system import System

            root = external_root(args.store)
            lp = external_root(NativeLayout(root).exchange) / "owner.lock"
            try:
                with owner_admission(
                    lambda s: s.enter_context(shared(lp, allow_readonly_open=True)),
                    deadline=admission_deadline(),
                ):
                    res = _execute_catalogue_action(System(root).techniques, args)
            except AdmissionContended as exc:
                print(json.dumps(exc.result(args.action)), file=sys.stderr)
                raise SystemExit(75) from None
        else:
            res = _execute_catalogue_action(InstalledCatalogue(eroot / "store"), args)
    except (ValueError, OSError, KeyError, TypeError) as exc:
        print(json.dumps({"status": "unresolved", "error": str(exc)}), file=sys.stderr)
        raise SystemExit(2) from None
    if res is not None:
        print(json.dumps(res, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
