#!/usr/bin/env python3
"""Run the bounded current shared-engine acceptance suite and retain its report."""
from pathlib import Path
import argparse
import json
import subprocess
import sys

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--product', type=Path, default=Path(__file__).resolve().parents[1])
parser.add_argument('--records', type=Path)
parser.add_argument('--work', type=Path, help='Compatibility argument; tests own isolated temporary stores')
args = parser.parse_args()
product = args.product.resolve()
if (product / 'runtime').is_dir():
    program = product / 'runtime/checks/store/check_shared_instances.py'
else:
    program = product / 'prototype/agent-system/check_shared_instances.py'
run = subprocess.run([sys.executable, '-B', str(program)], capture_output=True, text=True)
report = {'status': 'passed' if run.returncode == 0 else 'failed', 'program': str(program),
          'returncode': run.returncode, 'stdout': run.stdout, 'stderr': run.stderr,
          'scope': 'Bounded shared-code, store/schema/domain, portability, receiving and retention cases.'}
if args.records:
    args.records.mkdir(parents=True, exist_ok=True)
    (args.records / 'ACCEPTANCE.json').write_text(json.dumps(report, indent=2) + '\n')
print(json.dumps({k: v for k, v in report.items() if k not in {'stdout', 'stderr'}}, indent=2))
raise SystemExit(run.returncode)
