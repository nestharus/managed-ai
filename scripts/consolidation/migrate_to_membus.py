#!/usr/bin/env python3
"""Mirror, verify, and query a managed-ai-information store in a live Membus v2 daemon.

Dry-run is the default mode (read-only inventory, zero writes to source store or Membus).
Pass --apply to run Expand -> Dual-Write -> Verify -> Contract against --endpoint,
--verify-parity to run standalone SHA-256 parity verification, or --search QUERY to query
live Tantivy BM25/LiteralSubstring search on the mirrored namespace.
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from managed_ai_information.core.membus_mirror import (  # noqa: E402
    MembusStoreMirror,
    build_mirror_config,
)
from managed_ai_information.core.storage_backend import (  # noqa: E402
    MembusV2Backend,
    StorageBackendError,
)


def _split_csv(values: list[str] | None) -> list[str]:
    if not values:
        return []
    return [part.strip() for item in values for part in item.split(",") if part.strip()]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Mirror a managed-ai-information store into Membus v2 (dry-run by default).",
        allow_abbrev=False,
    )
    parser.add_argument(
        "--store",
        type=Path,
        help="Path to the managed-ai-information store root (required unless --search only)",
    )
    parser.add_argument(
        "--namespace",
        required=True,
        help="Target Membus v2 namespace identifier",
    )
    parser.add_argument(
        "--endpoint",
        help="Membus v2 HTTP(S) base URL (required for --apply, --verify-parity, --search)",
    )
    parser.add_argument(
        "--token",
        default=None,
        help="Optional bearer token for Membus v2",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=15.0,
        help="HTTP request timeout in seconds (default 15.0)",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="Read-only mirror plan showing counts and sealed custody classification (default)",
    )
    mode.add_argument(
        "--apply",
        action="store_true",
        help="Execute Expand -> Dual-Write -> Verify -> Contract mirror into Membus v2",
    )
    mode.add_argument(
        "--verify-parity",
        action="store_true",
        help="Verify SHA-256 record and CAS blob parity against live Membus v2",
    )
    mode.add_argument(
        "--search",
        metavar="QUERY",
        help="Query live Membus v2 Tantivy search on the mirrored namespace",
    )
    parser.add_argument(
        "--search-mode",
        choices=("Bm25", "LiteralSubstring"),
        default="Bm25",
        help="Tantivy search mode for --search (default Bm25)",
    )
    parser.add_argument(
        "--kind",
        default=None,
        help="Optional kind filter for --search",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Maximum hits returned by --search (default 20)",
    )
    parser.add_argument(
        "--sealed-id",
        action="append",
        dest="sealed_ids",
        default=[],
        help="Record ID enforced as SearchVisibility=Sealed (repeatable or comma-separated)",
    )
    parser.add_argument(
        "--sealed-kind",
        action="append",
        dest="sealed_kinds",
        default=[],
        help="Record kind enforced as SearchVisibility=Sealed (repeatable or comma-separated)",
    )
    parser.add_argument(
        "--sealed-prefix",
        action="append",
        dest="sealed_prefixes",
        default=[],
        help="Record ID prefix enforced as SearchVisibility=Sealed (repeatable or comma-separated)",
    )
    parser.add_argument(
        "--metadata-only-id",
        action="append",
        dest="metadata_only_ids",
        default=[],
        help="Record ID indexed as MetadataOnly (repeatable or comma-separated)",
    )
    parser.add_argument(
        "--no-blobs",
        action="store_true",
        help="Skip mirroring exchange/objects CAS blobs",
    )
    parser.add_argument(
        "--active-only",
        action="store_true",
        help="Mirror only active records (skip active=False records)",
    )
    parser.add_argument(
        "--verify-blob-bytes",
        action="store_true",
        help="Download and re-hash every CAS blob byte-for-byte during parity verification",
    )
    parser.add_argument(
        "--no-cutover",
        action="store_true",
        help="Stop after record_verification without committing schema cutover",
    )
    parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON output")
    return parser


def _require_store(args: argparse.Namespace) -> Path:
    if args.store is None:
        raise ValueError("--store is required for dry-run, --apply, and --verify-parity")
    return args.store


def _require_backend(args: argparse.Namespace) -> MembusV2Backend:
    if not args.endpoint:
        raise ValueError("--endpoint is required for --apply, --verify-parity, and --search")
    return MembusV2Backend(args.endpoint, token=args.token, timeout=args.timeout)


def _execute_args(args: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    mirror = MembusStoreMirror()

    if args.search is not None:
        backend = _require_backend(args)
        report = mirror.search(
            backend,
            args.search,
            namespace=args.namespace,
            kind=args.kind,
            mode=args.search_mode,
            limit=args.limit,
        )
        return 0, report

    store_root = _require_store(args)
    config = build_mirror_config(
        args.namespace,
        sealed_ids=_split_csv(args.sealed_ids),
        sealed_kinds=_split_csv(args.sealed_kinds),
        sealed_prefixes=_split_csv(args.sealed_prefixes),
        metadata_only_ids=_split_csv(args.metadata_only_ids),
        include_blobs=not args.no_blobs,
        include_inactive=not args.active_only,
    )

    if args.apply:
        backend = _require_backend(args)
        report = mirror.apply(
            store_root,
            backend,
            config,
            contract_cutover=not args.no_cutover,
            verify_blob_bytes=args.verify_blob_bytes,
        )
        return 0, report

    if args.verify_parity:
        backend = _require_backend(args)
        report = mirror.verify_parity(
            store_root,
            backend,
            config,
            verify_blob_bytes=args.verify_blob_bytes,
        )
        return (0 if report["status"] == "passed" else 1), report

    plan = mirror.plan(store_root, config)
    return 0, plan.to_report()


def run_cli(argv: list[str] | None = None) -> tuple[int, dict[str, Any]]:
    args = build_parser().parse_args(argv)
    return _execute_args(args)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        rc, payload = _execute_args(args)
    except (OSError, RuntimeError, ValueError, StorageBackendError) as exc:
        err = {"ok": False, "error": str(exc)}
        print(json.dumps(err, ensure_ascii=False, sort_keys=True), file=sys.stderr)
        return 2

    if args.pretty:
        print(json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True))
    else:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
