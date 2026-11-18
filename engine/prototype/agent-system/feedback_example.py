"""Exercise a temporary runtime through the release root and an activated engine."""
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory


def call(*args):
    result = subprocess.run([sys.executable, '-B', *map(str, args)], capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError(f'{args}: {result.stderr or result.stdout}')
    return json.loads(result.stdout)


def exercise(product, work):
    state = work / 'state'
    payload = work / 'input.json'
    app = [product/'system.py', '--root', state]
    call(*app, 'init', '--goal-text', 'Prepare a pickup instruction the night shift can use.')

    def spec(data):
        payload.write_text(json.dumps(data))
        return payload

    def put(ident, kind, content, expected=0, basis=None):
        record = dict(id=ident, kind=kind, content=content, basis=basis or [])
        row = call(*app, 'put', '--record', spec(record), '--expected', expected)['record']
        return {k: row[k] for k in ('id', 'revision', 'sha256')}

    ident = 'pickup-trial'
    candidate = put('instruction', 'candidate', 'Use the west desk between 18:00 and 22:00.')
    outside = put('desk-contact', 'source', 'Night-shift pickup from the west desk.')
    intent = put('shift-requirement', 'requirement', 'A night worker must be able to collect after 19:00.')
    opening = dict(id=ident, expected=0, scope='Night shift, west desk', candidate=candidate,
                   forecast='A night worker collects at the west desk after 19:00.',
                   rival='The desk has closed and the worker needs another pickup point.',
                   outside=outside, references=[intent])
    call(*app, 'feedback', 'open', '--spec', spec(opening))
    exercised = [dict(reference=outside, role='outside', observed='The west desk shutter was closed at 19:05.'),
                 dict(reference=intent, role='intended', observed='The night worker could not collect the package.')]
    meter = state/'work/sources/walk.txt'
    meter.write_text('Fixture walk at 19:05: shutter closed; pickup failed.\n')
    reading = put('night-walk', 'evidence', dict(subject=candidate, observed='The pickup attempt failed.', exercised=exercised),
                  basis=['sources/walk.txt'])
    next_work = 'Revise the instruction to name a pickup point open after 19:00.'
    observed = dict(id=ident, expected=1, subject=candidate, observation=reading,
                    interpretation='The posted desk hours fail for this shift.', route='work', next=next_work,
                    witnesses=[dict(reference=outside['id'], role='outside', account='Saw the closed shutter.'),
                               dict(reference=intent['id'], role='intended', account='A night worker could not collect.')])
    call(*app, 'feedback', 'observe', '--spec', spec(observed))
    packet = call(*app, 'context', 'repair', '--task', 'Prepare the next handoff.')
    assert next_work in Path(packet['context']).read_text()
    d = work/'deployment'
    call(product/'deploy.py', '--deployment', d, 'init', '--runtime', state)
    deploy = [d/'deploy.py', '--deployment', d]
    source = work/'project'; source.mkdir()
    (source/'pickup.txt').write_text('Night shift at the west desk.')
    staged = call(*deploy, 'stage', 'current', '--project', source, '--engine', product)
    call(*deploy, 'activate', 'current', '--expected', 'none', '--manifest-sha256', staged['manifest_sha256'],
         '--reason', 'Use the reviewed release for the pickup trial.')
    direct = call(*app, 'feedback', 'inspect', ident)
    selected = call(*deploy, 'system', 'feedback', 'inspect', ident)
    assert selected == direct and next_work in json.dumps(selected)
    captured = call(*deploy, 'system', 'context', 'selected', '--task', 'Repair the pickup instructions.', '--feedback', ident)
    assert next_work in Path(captured['context']).read_text()
    return dict(record=ident, observed=reading['id'], ordinary_next_instruction=next_work,
                selected_engine_files=staged['engine_files'], deployment_inspect_matches_direct=True,
                selected_deployment_context_carries_next=True)


if __name__ == '__main__':
    with TemporaryDirectory() as td:
        print(json.dumps(exercise(Path(__file__).resolve().parents[1], Path(td)), indent=2))
