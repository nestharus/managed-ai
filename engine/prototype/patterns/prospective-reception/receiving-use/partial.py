"""Exercise an offline utility and feed mixed results into one native receiving use."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from zipfile import ZipFile, ZipInfo


def run(product, scratch, baseline=False):
    state = scratch / 'state'
    request = scratch / 'spec.json'

    def native(*args, spec=None, refused=False):
        cmd = [sys.executable, '-B', str(product / 'system.py'), '--root', str(state), *args]
        if spec is not None:
            request.write_text(json.dumps(spec))
            cmd.append(str(request))
        response = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        if refused:
            assert response.returncode, cmd
            return response.stderr + response.stdout
        if response.returncode:
            raise RuntimeError(response.stderr + response.stdout)
        return json.loads(response.stdout)

    def put(name, content, expected=0):
        row = native('put', '--expected', str(expected), '--record',
                     spec=dict(id=name, kind='source', content=content))['record']
        return {key: row[key] for key in ('id', 'revision', 'sha256')}

    def observe(number, pending, result, route, interpretation, resolutions=(), refused=False):
        actual = put('run-' + str(number), dict(
            subject=candidate, observed=json.dumps(result),
            exercised=[dict(role='outside', reference=bench, observed=json.dumps(result))]))
        args = dict(id='offline-use', expected=pending, observation=actual, subject=candidate,
                    route=route, interpretation=interpretation, next=interpretation,
                    witnesses=[dict(role='outside', reference=bench['id'], account='Ran the extracted program.')],
                    resolutions=[dict(return_number=n, account=why) for n, why in resolutions])
        out = native('feedback', 'observe', '--spec', spec=args, refused=refused)
        return out if refused else out['route']

    def package(version):
        folder = scratch / ('install-' + str(version))
        folder.mkdir()
        archive = scratch / ('delivery-' + str(version) + '.zip')
        negative = 'raise SystemExit(2)' if version == 3 else 'print(0); raise SystemExit()'
        numerator = '(n + size - 1)' if version >= 2 else 'n'
        program = ("import json, sys\nfrom pathlib import Path\nn = int(sys.argv[1])\n"
                   f"if n < 0:\n    {negative}\n"
                   "size = json.loads(Path(__file__).with_name('pack.json').read_bytes())['size']\n"
                   f"print({numerator} // size)\n")
        with ZipFile(archive, 'w') as out:
            out.writestr(ZipInfo('cartons.py'), program)
            if version:
                out.writestr(ZipInfo('pack.json'), '{"size":3}')
        with ZipFile(archive) as source:
            source.extractall(folder)
        return folder, hashlib.sha256(archive.read_bytes()).hexdigest()

    def exercise(quantity):
        p = subprocess.run([sys.executable, '-B', str(installed / 'cartons.py'), str(quantity)],
                           cwd=scratch, text=True, capture_output=True, timeout=5)
        return dict(quantity=quantity, exit=p.returncode, answer=p.stdout.strip(),
                    error=p.stderr.splitlines()[-1].split(':', 1)[0] if p.stderr else '')

    native('init', '--goal-text', 'Receive a working offline carton counter.')
    bench = put('offline-machine', 'Receiver runs the supplied package after extraction.')
    plan = dict(id='offline-use', scope='Produce correct whole cartons or refuse a negative quantity.',
                forecast='Offline execution supplies sufficient cartons for each nonnegative order.',
                rival='The distributed program cannot finish or gives too few cartons.',
                outside=bench, references=[])
    installed, digest = package(0)
    candidate = put('delivery', digest)
    native('feedback', 'open', '--spec', spec=dict(**plan, candidate=candidate, expected=0))
    missing, negative = exercise(6), exercise(-2)
    assert missing['error'] == 'FileNotFoundError' and negative['exit'] == 0
    observe(1, 1, missing, 'work', 'Ship the missing capacity file.')
    observe(2, 2, negative, 'hold', 'Reject negative orders.')
    installed, digest = package(1)
    candidate = put('delivery', digest, 1)
    revised = {k: v for k, v in plan.items() if k != 'scope'}
    native('feedback', 'revise', '--spec', spec=dict(**revised, candidate=candidate, expected=3,
                                                     reason='The capacity file now ships.'))
    exact, split = exercise(6), exercise(7)
    assert exact['answer'] == split['answer'] == '2' and exact['exit'] == split['exit'] == 0
    one = [(1, 'The extracted package processed the six-item order.')]
    mixed = observe(3, 4, [exact, split], 'work', 'Seven items need a third carton.', one, refused=baseline)
    if baseline:
        assert 'resolve prior adverse returns on a review route' in mixed
        original = native('feedback', 'inspect', 'offline-use')['route']
        assert original['unresolved_returns'] == [1, 2] and original['revision'] == 4
        return dict(original_open=[1, 2], attempted_mixed_update='refused', exact=exact, split=split)
    assert mixed['unresolved_returns'] == [2, 3]
    installed, digest = package(2)
    candidate = put('delivery', digest, 2)
    native('feedback', 'revise', '--spec', spec=dict(**revised, candidate=candidate, expected=5,
                                                     reason='Round partial cartons upward.'))
    seven = exercise(7)
    negative_again = exercise(-2)
    assert seven['answer'] == '3' and negative_again['exit'] == 0
    partial = observe(4, 6, [seven, negative_again], 'progress', 'Negative orders still need refusal.',
                      [(3, 'The actual receiver obtained three cartons for seven items.')])
    assert partial['unresolved_returns'] == [2] and partial['state'] == 'hold'
    rejection = native('feedback', 'accept', '--spec', spec=dict(id='offline-use', expected=7,
                       owner='receiver', account='Attempt premature acceptance.'), refused=True)
    assert 'review route' in rejection
    installed, digest = package(3)
    candidate = put('delivery', digest, 3)
    native('feedback', 'revise', '--spec', spec=dict(**revised, candidate=candidate, expected=7,
                                                     reason='Reject negative quantities before computing.'))
    final = [exercise(n) for n in [0, 6, 7, -2]]
    assert [(x['exit'], x['answer']) for x in final] == [(0, '0'), (0, '2'), (0, '3'), (2, '')]
    ready = observe(5, 8, final, 'review', 'The receiver ran the selected orders on this revision.',
                    [(2, 'The negative input returned a refusal.')])
    accepted = native('feedback', 'accept', '--spec', spec=dict(id='offline-use', expected=9,
                      owner='offline receiver', account='The revised utility passed the selected receiving cases.'))
    assert ready['unresolved_returns'] == [] and accepted['route']['state'] == 'accepted'
    history = native('feedback', 'inspect', 'offline-use', '--revision', '3')['record']['content']['returns']
    assert [row['route'] for row in history] == ['work', 'hold']
    return dict(first=[missing, negative], after_shipping=[exact, split], mixed_open=[2, 3],
                after_rounding=[seven, negative_again], partial_open=[2], partial_state=partial['state'], premature_acceptance='refused',
                final=final, final_state=accepted['route']['state'], original_returns_retained=len(history))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('product', type=Path, help='extracted G60-system-use directory')
    parser.add_argument('--baseline-v29', action='store_true', help='exercise the v29 refusal')
    args = parser.parse_args()
    with TemporaryDirectory() as scratch:
        print(json.dumps(run(args.product.resolve(), Path(scratch), args.baseline_v29), indent=2))
