"""Find a v26 feedback revision which dropped outstanding adverse returns.

Usage: python audit_feedback.py PATH_TO_PRODUCT RUNTIME_ROOT FEEDBACK_ID
The optional --obligation check follows the verified retained revisions of a native record.
"""
import argparse
import json
from pathlib import Path
import sys


def history(product, root, ident):
    sys.path.insert(0, str(product / ('native' if (product / 'native').is_dir() else 'agent-system')))
    from system import System
    store = System(root).record_store
    for page in store.history_pages(ident):
        if page.get('retention_boundary'):
            raise ValueError('Historical audit stops at the declared current-use retention boundary; absence of earlier revisions is not a clean historical finding')
        yield from page['records']


def scan(product, root, ident):
    records = history(product, root, ident)
    current = next(records)
    from native_runtime.feedback import Feedback, KIND
    if current['kind'] != KIND:
        raise ValueError('record does not own a feedback use')
    head_revision = current['revision']
    gaps = []
    for previous in records:
        content = current['content']
        if content['state'] == 'open' and 'revision_reason' in content:
            carried = {r['return_number'] for r in content.get('carried_returns', [])}
            pending = Feedback.outstanding(previous['content'])
            missing = pending.keys() - carried
            if missing:
                gaps.append(dict(revision=current['revision'], prior_revision=previous['revision'],
                                 returns=[dict(number=n, observation=pending[n]['observation'],
                                               interpretation=pending[n]['interpretation']) for n in sorted(missing)]))
        current = previous
    gaps.reverse()
    return {'feedback':ident,'through_revision':head_revision,'unexplained_drops':gaps,
            'scope':'Historical representational gaps. Inspect the pinned return and test the current receiving use.'}

def scan_obligation(product, root, ident):
    records = history(product, root, ident)
    current = next(records)
    head_revision=current['revision']
    findings=[]
    inactive_revisions=[]
    def owed(record):
        if record['kind']!='obligation':
            return False
        content=record['content']
        return not (isinstance(content,dict) and content.get('disposition') in ('fulfilled','released')
                    and isinstance(content.get('account'),str) and content['account'].strip())
    while True:
        previous = next(records, None)
        issues=[]
        if owed(current):
            if current['active'] is False:
                inactive_revisions.append(current['revision'])
            if isinstance(current['content'],dict) and current['content'].get('disposition') in ('fulfilled','released'):
                issues.append('closure had no account')
        parent=current['previous_object']
        if previous is None:
            if issues:
                findings.append({'revision':1,'observations':issues})
            break
        if owed(previous):
            if current['kind']!='obligation':
                issues.append('retyped before accounted closure')
            elif owed(current):
                basis={r['path']:r['sha256'] for r in current['basis']}
                content=current['content']
                change=content.get('basis_change') if isinstance(content,dict) else None
                accounted=(isinstance(change,dict) and type(change.get('from_revision')) is int and
                           change['from_revision']==previous['revision'] and isinstance(change.get('account'),str)
                           and bool(change['account'].strip()))
                if not accounted and any(
                        r['path'] not in basis or basis[r['path']]!=r['sha256'] for r in previous['basis']):
                    issues.append('earlier evidence removed or replaced without account')
        if issues:
            findings.append({'revision':current['revision'],'observations':issues,'predecessor_object':parent})
        current=previous
    return {'record':ident,'through_revision':head_revision,'history':list(reversed(findings)),
            'inactive_for_navigation':list(reversed(inactive_revisions)),
            'scope':'Historical transitions. Inactive revisions are informational and are excluded from the findings. Ordinary work retains typed open duties.'}


if __name__=='__main__':
    cli=argparse.ArgumentParser(description=__doc__)
    cli.add_argument('product',type=Path)
    cli.add_argument('root',type=Path)
    cli.add_argument('record')
    cli.add_argument('--obligation',action='store_true',help='inspect obligation transitions including former obligations')
    a=cli.parse_args()
    inspect=scan_obligation if a.obligation else scan
    try:
        result = inspect(a.product.resolve(), a.root.resolve(), a.record)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        cli.exit(2, json.dumps({'status': 'unresolved', 'record': a.record, 'error': str(exc)}) + '\n')
    print(json.dumps(result, indent=2, ensure_ascii=False))
