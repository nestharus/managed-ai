#!/usr/bin/env python3
"""Run the bounded current shared-engine acceptance suite and retain its report."""
from pathlib import Path
import argparse
import json
import os
import stat
import subprocess
import sys
import tempfile

REPARSE_FLAG = getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0)


def _is_reparse_or_symlink(info):
    return bool(
        stat.S_ISLNK(info.st_mode)
        or getattr(info, 'st_reparse_tag', 0)
        or (getattr(info, 'st_file_attributes', 0) & REPARSE_FLAG)
    )


def _require_ordinary_ancestors(path, label, *, allow_missing_self=False):
    normalized = Path(os.path.abspath(path))
    for ancestor in reversed(normalized.parents):
        try:
            info = ancestor.lstat()
        except FileNotFoundError:
            if allow_missing_self:
                continue
            raise ValueError(f'{label}: missing ancestor {ancestor}') from None
        if _is_reparse_or_symlink(info) or not stat.S_ISDIR(info.st_mode):
            raise ValueError(f'{label}: ordinary directory required at {ancestor}')
    try:
        info = normalized.lstat()
    except FileNotFoundError:
        if allow_missing_self:
            return normalized
        raise ValueError(f'{label}: missing directory {normalized}') from None
    if _is_reparse_or_symlink(info) or not stat.S_ISDIR(info.st_mode):
        raise ValueError(f'{label}: ordinary directory required at {normalized}')
    return normalized


def _write_json_atomic(directory, filename, payload):
    target = directory / filename
    try:
        info = target.lstat()
    except FileNotFoundError:
        pass
    else:
        if _is_reparse_or_symlink(info) or not stat.S_ISREG(info.st_mode):
            raise ValueError(f'ordinary report target required: {target}')
    raw = (json.dumps(payload, indent=2, allow_nan=False) + '\n').encode('utf-8')
    fd, tmp = tempfile.mkstemp(prefix=f'.{filename}.', suffix='.tmp', dir=directory)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, target)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def main():
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument('--product', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--records', type=Path)
    parser.add_argument('--work', type=Path, help='Compatibility argument; tests own isolated temporary stores')
    args = parser.parse_args()
    product_dir = _require_ordinary_ancestors(args.product, 'invalid --product')
    product = product_dir.resolve(strict=True)
    records_dir = None
    if args.records is not None:
        records_dir = _require_ordinary_ancestors(args.records, 'invalid --records', allow_missing_self=True)
    program = product.parent / 'tests/engine/store/test_shared_instances.py'
    run = subprocess.run(
        [sys.executable, '-B', str(program)],
        capture_output=True,
        text=True,
        encoding='utf-8',
        errors='replace',
        env=dict(os.environ, G60_TEST_PRODUCT=str(product)),
    )
    report = {
        'status': 'passed' if run.returncode == 0 else 'failed',
        'program': str(program),
        'product': str(product),
        'runtime': str(product),
        'returncode': run.returncode,
        'stdout': run.stdout,
        'stderr': run.stderr,
        'scope': 'Bounded shared-code, store/schema/domain, portability, receiving and retention cases; no broad scientific-efficacy claim.',
    }
    if records_dir is not None:
        records_dir.mkdir(parents=True, exist_ok=True)
        _require_ordinary_ancestors(records_dir, 'invalid --records')
        _write_json_atomic(records_dir, 'ACCEPTANCE.json', report)
    print(json.dumps({k: v for k, v in report.items() if k not in {'stdout', 'stderr'}}, indent=2))
    return run.returncode


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (OSError, ValueError) as error:
        raise SystemExit(str(error)) from None
