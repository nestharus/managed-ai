#!/usr/bin/env python3
"""Read canonical core or store-selected sources, or operate published source libraries.

Explicit top-level --library requests dispatch directly to the published library
reader without mutating sys.argv.
"""

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

from managed_ai.library.config import DEFAULT_LIBRARY_CONFIG

_SUBCOMMANDS = frozenset(
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
_DISCOVER_FLAGS = frozenset(
    {
        "--kind",
        "--limit",
        "--offset",
        "--content",
        "-h",
        "--help",
    }
)


def _is_top_level_library_invocation(argv: list[str]) -> bool:
    """Return True only when --library is given before the subcommand."""
    idx = 0
    while idx < len(argv):
        arg = argv[idx]
        if arg == "--":
            return False
        if arg == "--library" or arg.startswith("--library="):
            return True
        if arg == "--store":
            idx += 2
            continue
        if arg.startswith("--store="):
            idx += 1
            continue
        if arg in _SUBCOMMANDS or not arg.startswith("-"):
            return False
        idx += 1
    return False


def _normalize_query_arg(argv: list[str]) -> list[str]:
    """Allow `discover --query --something` without argparse treating the query as a flag."""
    out = list(argv)
    for idx, arg in enumerate(out):
        if arg == "--query" and idx + 1 < len(out):
            nxt = out[idx + 1]
            if (
                nxt.startswith("-")
                and nxt not in _DISCOVER_FLAGS
                and not any(
                    nxt.startswith(flag + "=") for flag in ("--kind", "--limit", "--offset")
                )
            ):
                out[idx] = "--query=" + nxt
                del out[idx + 1]
                break
    return out


def _build_library_parser() -> argparse.ArgumentParser:
    scfg = DEFAULT_LIBRARY_CONFIG.search
    bcfg = DEFAULT_LIBRARY_CONFIG.batch

    parser = argparse.ArgumentParser(
        description="Browse, verify and acquire published source libraries.", allow_abbrev=False
    )
    parser.add_argument("--library", action="append", type=Path)
    sub = parser.add_subparsers(dest="action", required=True)

    sub.add_parser("verify", allow_abbrev=False)
    sub.add_parser("modules", allow_abbrev=False)

    show = sub.add_parser("show", allow_abbrev=False)
    show.add_argument("identity")
    show.add_argument("--text", action="store_true")

    discover = sub.add_parser("discover", allow_abbrev=False)
    discover.add_argument("--query", required=True)
    discover.add_argument("--limit", type=int, default=scfg.default_limit)
    discover.add_argument("--scan-limit", type=int, default=scfg.default_scan_limit)
    discover.add_argument("--max-bytes", type=int, default=scfg.default_max_bytes)
    discover.add_argument("--backend", choices=("bm25", "fts5", "literal"), default="bm25")
    discover.add_argument(
        "--index",
        type=Path,
        help="Nominate from this prepared derivative; requires FTS5 and digest",
    )
    discover.add_argument(
        "--index-sha256", help="Exact expected digest from the selected prepare-index receipt"
    )

    batch = sub.add_parser(
        "discover-batch",
        allow_abbrev=False,
        help="Share one verified prepared image across queries",
    )
    batch.add_argument(
        "--queries", type=Path, required=True, help="UTF-8 JSON array of 1 to 32 query strings"
    )
    batch.add_argument("--index", type=Path, required=True)
    batch.add_argument("--index-sha256", required=True)
    batch.add_argument("--limit", type=int, default=scfg.default_limit)
    batch.add_argument("--scan-limit", type=int, default=scfg.default_scan_limit)
    batch.add_argument("--max-bytes", type=int, default=scfg.default_max_bytes)
    batch.add_argument("--max-result-bytes", type=int, default=bcfg.max_result_bytes)

    prepare = sub.add_parser(
        "prepare-index", allow_abbrev=False, help="Verify sources and build immutable FTS5 index"
    )
    prepare.add_argument("--output", type=Path, required=True)
    prepare.add_argument("--scan-limit", type=int, default=scfg.default_scan_limit)
    prepare.add_argument("--max-bytes", type=int, default=scfg.default_max_bytes)

    ctx = sub.add_parser("context", allow_abbrev=False)
    ctx.add_argument("--selection", type=Path, required=True)
    ctx.add_argument("--max-bytes", type=int, default=scfg.default_max_bytes)

    exp = sub.add_parser("export", allow_abbrev=False)
    exp.add_argument("--selection", type=Path, required=True)
    exp.add_argument("--edition", required=True)
    exp.add_argument("--output", type=Path, required=True)

    ins = sub.add_parser("inspect", allow_abbrev=False)
    ins.add_argument("--bundle", type=Path, required=True)

    imp = sub.add_parser("import", allow_abbrev=False)
    imp.add_argument("--bundle", type=Path, required=True)
    imp.add_argument("--store", type=Path, required=True)

    return parser


def _dispatch_library_action(args: argparse.Namespace, libraries: list[Any]) -> Any:
    from managed_ai.library.bundle import context, export
    from managed_ai.library.format import decoded, need, read_bytes

    if args.action == "verify":
        return {"libraries": [lib.verify() for lib in libraries]}

    if args.action == "modules":
        return {
            "libraries": [
                {
                    "library_sha256": lib.sha256,
                    "modules": [
                        {"pin": lib.pin(row, "module"), "entry": row}
                        for row in lib.modules.values()
                    ],
                }
                for lib in libraries
            ]
        }

    if args.action == "show":
        found = [lib for lib in libraries if args.identity in lib.bodies]
        need(found, f"identity absent from the selected libraries: {args.identity}")
        need(
            len(found) == 1,
            f"identity is present in several selected libraries: {args.identity}; "
            "select one with --library",
        )
        result = found[0].show(args.identity)
        if args.text:
            sys.stdout.write(result["text"])
            return None
        return result

    if args.action == "prepare-index":
        need(len(libraries) == 1, "prepare-index requires one selected library")
        return libraries[0].prepare_index(args.output, args.scan_limit, args.max_bytes)

    if args.action == "discover":
        need(
            args.index is None or len(libraries) == 1,
            "prepared discovery requires one selected library",
        )
        return {
            "libraries": [
                lib.discover(
                    args.query,
                    args.limit,
                    args.scan_limit,
                    args.max_bytes,
                    args.backend,
                    args.index,
                    args.index_sha256,
                )
                for lib in libraries
            ]
        }

    if args.action == "discover-batch":
        need(len(libraries) == 1, "discover-batch requires one selected library")
        queries = decoded(read_bytes(args.queries, DEFAULT_LIBRARY_CONFIG.batch.max_input_bytes))
        return libraries[0].discover_batch(
            queries,
            index=args.index,
            index_sha256=args.index_sha256,
            limit=args.limit,
            scan_limit=args.scan_limit,
            max_bytes=args.max_bytes,
            max_result_bytes=args.max_result_bytes,
        )

    selection = decoded(
        read_bytes(args.selection, DEFAULT_LIBRARY_CONFIG.limits.max_manifest_bytes)
    )
    if args.action == "context":
        return context(libraries, selection, args.max_bytes)
    return export(libraries, selection, args.edition, args.output)


def run_library_cli(argv: list[str] | None = None) -> None:
    """CLI entrypoint for published source libraries."""
    args = _build_library_parser().parse_args(argv)
    import zipfile

    from managed_ai.library.bundle import import_bundle, inspect_bundle
    from managed_ai.library.catalogue import Library
    from managed_ai.library.lexical import SQLITE_ERRORS

    libraries: list[Library] = []
    cli_errors: tuple[type[BaseException], ...] = (
        OSError,
        ValueError,
        KeyError,
        UnicodeError,
        RuntimeError,
        zipfile.BadZipFile,
        *SQLITE_ERRORS,
    )

    try:
        if args.action == "inspect":
            result: Any = inspect_bundle(args.bundle)
        elif args.action == "import":
            result = import_bundle(args.bundle, args.store, open_library=Library)
        else:
            default_lib = Path(__file__).resolve().parent / "library"
            libraries = [Library(path) for path in (args.library or [default_lib])]
            result = _dispatch_library_action(args, libraries)
            if result is None:
                return

        print(json.dumps(result, ensure_ascii=False, indent=2))
    except cli_errors as exc:
        print(
            json.dumps({"status": "refused", "operation": args.action, "error": str(exc)}),
            file=sys.stderr,
        )
        raise SystemExit(2) from None
    finally:
        for lib in libraries:
            lib.close()


def _execute_catalogue_action(catalogue: Any, args: argparse.Namespace) -> Any:
    if args.action == "show":
        result = catalogue.search(args.identity)
        if args.text:
            sys.stdout.write(result["body"])
            return None
        return result

    if args.action == "discover":
        return catalogue.search(
            query=args.query,
            kind=args.kind,
            limit=args.limit,
            offset=args.offset,
            content=args.content,
        )

    verified = []
    sealed = []
    for entry in catalogue.index()["entries"]:
        if entry.get("availability") == "sealed":
            sealed.append(entry["id"])
            continue
        raw = catalogue.body(entry)
        verified.append({"id": entry["id"], "sha256": entry["sha256"], "bytes": len(raw)})

    if getattr(args, "summary", False):
        return {
            "status": "verified",
            "verified_count": len(verified),
            "verified_bytes": sum(row["bytes"] for row in verified),
            "sealed_count": len(sealed),
            "sealed_without_body_read": sealed,
            "scope": (
                "Exact canonical byte custody; no participant acquisition or semantic acceptance "
                "is "
                "inferred."
            ),
        }

    return {
        "status": "verified",
        "sources": verified,
        "sealed_without_body_read": sealed,
        "scope": (
            "Exact canonical byte custody; no participant acquisition or semantic acceptance is "
            "inferred."
        ),
    }


def _build_store_catalogue_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--store", type=Path)
    sub = parser.add_subparsers(dest="action", required=True)

    verify_cmd = sub.add_parser("verify", allow_abbrev=False)
    verify_cmd.add_argument(
        "--summary",
        action="store_true",
        help="Return verified/sealed counts and byte totals instead of listing every source",
    )

    show = sub.add_parser("show", allow_abbrev=False)
    show.add_argument("identity")
    show.add_argument("--text", action="store_true")

    discover = sub.add_parser("discover", allow_abbrev=False)
    discover.add_argument("--query", default="")
    discover.add_argument("--kind")
    discover.add_argument("--limit", type=int, default=25)
    discover.add_argument("--offset", type=int, default=0)
    discover.add_argument("--content", action="store_true")

    return parser


def main(argv: list[str] | None = None) -> None:
    """Unified managed-ai-library CLI entrypoint."""
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    if _is_top_level_library_invocation(raw_argv):
        run_library_cli(raw_argv)
        return

    from managed_ai.core.ownership import NativeLayout, external_root
    from managed_ai.core.storage import (
        AdmissionContended,
        admission_deadline,
        owner_admission,
        shared,
    )
    from managed_ai.store.selection.installed_catalogue import InstalledCatalogue

    args = _build_store_catalogue_parser().parse_args(_normalize_query_arg(raw_argv))
    engine_root = Path(__file__).resolve().parent.parent

    try:
        if args.store:
            from managed_ai.store.system import System

            root = external_root(args.store)
            lock_path = external_root(NativeLayout(root).exchange) / "owner.lock"
            try:
                with owner_admission(
                    lambda stack: stack.enter_context(shared(lock_path, allow_readonly_open=True)),
                    deadline=admission_deadline(),
                ):
                    catalogue = System(root).techniques
                    result = _execute_catalogue_action(catalogue, args)
            except AdmissionContended as exc:
                print(json.dumps(exc.result(args.action)), file=sys.stderr)
                raise SystemExit(75) from None
        else:
            catalogue = InstalledCatalogue(engine_root / "store")
            result = _execute_catalogue_action(catalogue, args)
    except (ValueError, OSError, KeyError, TypeError) as exc:
        print(json.dumps({"status": "unresolved", "error": str(exc)}), file=sys.stderr)
        raise SystemExit(2) from None

    if result is not None:
        print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
