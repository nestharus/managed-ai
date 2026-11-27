#!/usr/bin/env python3
"""CLI for component & symbol-level blast-radius impact analysis (managed-ai-impact-report/1)."""

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src"
for _entry in (str(SRC_ROOT), str(REPO_ROOT)):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

from managed_ai_information.platform import check_ordinary_ancestors  # noqa: E402
from managed_ai_information.store.query.impact_analysis import (  # noqa: E402
    IMPACT_REPORT_FORMAT,
    analyze_impact,
    resolve_git_diff_targets,
)
from managed_ai_information.store.system import System  # noqa: E402
from scripts.consolidation._common import dump_json  # noqa: E402


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Component and symbol-level blast-radius impact analysis over repo + stores.",
        allow_abbrev=False,
    )
    parser.add_argument("--repo", type=Path, default=REPO_ROOT)
    parser.add_argument("--store", type=Path, required=True)
    parser.add_argument("--targets", nargs="+", default=[])
    parser.add_argument("--git-diff", default=None)
    parser.add_argument("--crw-store", type=Path, default=None)
    parser.add_argument("--package-dir", default="src/managed_ai_information")
    parser.add_argument("--scan-roots", nargs="+", default=["src", "scripts", "tests"])
    parser.add_argument("--max-crw-constituents", type=int, default=8)
    parser.add_argument("--max-crw-controls", type=int, default=12)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        targets = list(args.targets)
        if args.git_diff is not None:
            targets.extend(resolve_git_diff_targets(args.repo, args.git_diff))
        if not targets:
            raise ValueError("provide --targets <path ...> or --git-diff <rev>")

        checked_store = check_ordinary_ancestors(
            args.store, expect_dir=True, error_prefix="ordinary store root required"
        ).resolve(strict=True)
        system = System(checked_store)

        crw_system = None
        if args.crw_store is not None:
            checked_crw = check_ordinary_ancestors(
                args.crw_store, expect_dir=True, error_prefix="ordinary CRW store root required"
            ).resolve(strict=True)
            crw_system = System(checked_crw)

        report = analyze_impact(
            args.repo,
            system,
            targets,
            package_rel=args.package_dir,
            scan_roots=tuple(args.scan_roots),
            crw_system=crw_system,
            max_crw_constituents=args.max_crw_constituents,
            max_crw_controls=args.max_crw_controls,
        )
        print(dump_json(report))
        return 0
    except (OSError, ValueError) as exc:
        print(
            json.dumps(
                {"format": IMPACT_REPORT_FORMAT, "status": "error", "error": str(exc)},
                ensure_ascii=False,
                indent=2,
                allow_nan=False,
            ),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
