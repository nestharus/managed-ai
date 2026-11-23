#!/usr/bin/env python3
"""Run the bounded current shared-engine acceptance suite and retain its report."""
from pathlib import Path
import argparse
import json
import os
import subprocess
import sys

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--product', type=Path, default=Path(__file__).resolve().parents[1])
parser.add_argument('--records', type=Path)
parser.add_argument('--work', type=Path, help='Compatibility argument; tests own isolated temporary stores')
args = parser.parse_args()
runtime = args.product.resolve() / 'runtime'
program = runtime / 'tests/native_store/test_shared_instances.py'
run = subprocess.run([sys.executable, '-B', str(program)], capture_output=True, text=True,
                     env=dict(os.environ, G60_TEST_PRODUCT=str(runtime)))
report = {'status': 'passed' if run.returncode == 0 else 'failed', 'program': str(program), 'runtime': str(runtime),
          'returncode': run.returncode, 'stdout': run.stdout, 'stderr': run.stderr,
          'scope': 'Bounded shared-code, store/schema/domain, portability, receiving and retention cases; no broad scientific-efficacy claim.'}
if args.records:
    args.records.mkdir(parents=True, exist_ok=True)
    (args.records / 'ACCEPTANCE.json').write_text(json.dumps(report, indent=2) + '\n')
print(json.dumps({k: v for k, v in report.items() if k not in {'stdout', 'stderr'}}, indent=2))
raise SystemExit(run.returncode)
