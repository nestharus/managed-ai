#!/usr/bin/env python3
"""Run the bounded current shared-engine acceptance suite and retain its report."""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
for _entry in (str(REPO_ROOT / "src"), str(REPO_ROOT)):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

from managed_ai.platform import (  # noqa: E402
    atomic_bytes,
    check_ordinary_ancestors,
    check_unfollowed_ancestors,
    is_ordinary_dir_stat,
    is_ordinary_file_stat,
)


def _require_ordinary_ancestors(
    path: Path, label: str, *, allow_missing_self: bool = False
) -> Path:
    if not allow_missing_self:
        return check_ordinary_ancestors(
            path,
            expect_dir=True,
            error_prefix=f"{label}: ordinary directory required",
        )

    try:
        validated = check_unfollowed_ancestors(
            path,
            role=f"{label}: ordinary directory required",
            include_target=True,
        )
    except ValueError as exc:
        raise ValueError(f"{label}: ordinary directory required: {exc}") from None

    for ancestor in reversed(validated.parents):
        try:
            info = ancestor.lstat()
        except FileNotFoundError:
            continue
        if not is_ordinary_dir_stat(info):
            raise ValueError(f"{label}: ordinary directory required at {ancestor}")

    try:
        info = validated.lstat()
    except FileNotFoundError:
        return validated

    if not is_ordinary_dir_stat(info):
        raise ValueError(f"{label}: ordinary directory required at {validated}")

    return validated


def _write_json_atomic(directory: Path, filename: str, payload: dict) -> None:
    target = directory / filename
    try:
        info = target.lstat()
    except FileNotFoundError:
        pass
    else:
        if not is_ordinary_file_stat(info):
            raise ValueError(f"ordinary report target required: {target}")

    raw = (json.dumps(payload, indent=2, allow_nan=False) + "\n").encode("utf-8")
    atomic_bytes(target, raw)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--product", type=Path, default=REPO_ROOT / "src/managed_ai")
    parser.add_argument("--records", type=Path)
    parser.add_argument(
        "--work", type=Path, help="Compatibility argument; tests own isolated temporary stores"
    )
    args = parser.parse_args()

    product_dir = _require_ordinary_ancestors(args.product, "invalid --product")
    product = product_dir.resolve(strict=True)
    records_dir = None
    if args.records is not None:
        records_dir = _require_ordinary_ancestors(
            args.records, "invalid --records", allow_missing_self=True
        )

    repo_root = (
        product.parents[1]
        if (product.name == "managed_ai" and product.parent.name == "src")
        else product.parent
    )
    program = repo_root / "tests/store/test_shared_instances.py"
    if (
        not program.is_file()
        and (repo_root / "tests/engine/store/test_shared_instances.py").is_file()
    ):
        program = repo_root / "tests/engine/store/test_shared_instances.py"

    run = subprocess.run(  # noqa: S603
        [sys.executable, "-B", str(program)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=dict(os.environ, G60_TEST_PRODUCT=str(product), G60_TEST_ENGINE=str(product)),
    )
    report = {
        "status": "passed" if run.returncode == 0 else "failed",
        "program": str(program),
        "product": str(product),
        "runtime": str(product),
        "returncode": run.returncode,
        "stdout": run.stdout,
        "stderr": run.stderr,
        "scope": "Bounded shared-code, store/schema/domain, portability, receiving and retention "
        "cases; "
        "no broad scientific-efficacy claim.",
    }
    if records_dir is not None:
        records_dir.mkdir(parents=True, exist_ok=True)
        _require_ordinary_ancestors(records_dir, "invalid --records")
        _write_json_atomic(records_dir, "ACCEPTANCE.json", report)

    print(
        json.dumps(
            {key: value for key, value in report.items() if key not in {"stdout", "stderr"}},
            indent=2,
        )
    )
    return run.returncode


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError) as error:
        raise SystemExit(str(error)) from None
