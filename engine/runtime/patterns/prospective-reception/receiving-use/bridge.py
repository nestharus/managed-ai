"""Replay with: python3 -B bridge.py /path/to/G60-system-use"""

import argparse
import base64
import json
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from venue import Venue, digest


def pin(row):
    return {key: row[key] for key in ('id', 'revision', 'sha256')}


def run(release, work):
    state = work / 'native'
    payload = work / 'request.json'
    export = work / 'venue-note.txt'
    store = work / 'collective.sqlite'
    venue = Venue(work / 'venue.sqlite')
    trace = {}

    def cli(script, *args, refused=False):
        result = subprocess.run([sys.executable, '-B', str(release / script), *map(str, args)],
                                capture_output=True, text=True)
        if refused:
            if result.returncode == 0:
                raise AssertionError('Foreign occurrence unexpectedly admitted.')
            return (result.stderr or result.stdout).strip()
        if result.returncode:
            raise RuntimeError(f'{script} {args}: {result.stdout} {result.stderr}')
        return json.loads(result.stdout)

    def spec(data):
        payload.write_text(json.dumps(data))
        return payload

    def app(*args, **kw):
        return cli('system.py', '--root', state, *args, **kw)

    def evidence(*args):
        return cli('evidence.py', '--db', store, *args)

    def put(ident, kind, content, expected=0, basis=()):
        result = app('put', '--record', spec(dict(id=ident, kind=kind, content=content, basis=list(basis))),
                     '--expected', expected)
        return pin(result['record'])

    def import_reading(interpretation, expected):
        # Venue issues the export. Scout owns the interpretation. Editor reissues it in native.
        edition = venue.export('quay', export)
        item = f'quay-reading-{edition}'
        locator = f'venue-office/quay/editions/{edition}'
        evidence('record', item, '--actor', 'scout', '--command', f'probe-{edition}', '--source', export,
                 '--spec', spec(dict(source_locator=locator, use='Direct an evening courier.',
                                     observation=interpretation, limits='One published notice.')))
        row = evidence('show', item)
        witness = row['body']['witness']
        relative = f'sources/quay-{edition}.txt'
        (state / 'work' / relative).write_bytes(base64.b64decode(witness['base64'], validate=True))
        content = dict(issuer='venue-office', site='quay', edition=edition,
                       source_hash=witness['source_sha256'], source_locator=locator,
                       collective={'store': 'courier-intake', 'owner': row['owner'], 'pin': pin(row)},
                       reading=row['body']['account'])
        local = put('quay-source', 'source', content, expected, [relative])
        return pin(row), local

    def draft(door, expected):
        source = app('show', 'quay-source')
        body = {k: source['content'][k] for k in ('issuer', 'site', 'edition', 'source_hash')}
        body.update(native_source=pin(source), door=door)
        return put('courier-card', 'candidate', body, expected)

    def candidate_body():
        return app('show', 'courier-card')['content']

    def feedback(action, expected, **fields):
        return app('feedback', action, '--spec', spec(dict(id='courier-use', expected=expected, **fields)))

    def observe(expected, subject, source, outcome, route, next_step, resolution=None):
        observation = put(f'return-{expected}', 'evidence',
                          dict(subject=subject, observed=outcome,
                               exercised=[dict(reference=source, role='outside', observed=outcome)]))
        fields = dict(subject=subject, observation=observation, interpretation=outcome,
                      route=route, next=next_step,
                      witnesses=[dict(reference=source['id'], role='outside', account=outcome)])
        if resolution:
            fields['resolutions'] = [resolution]
        return feedback('observe', expected, **fields)['route']

    venue.post('quay', 'During the evening shift, deliveries are collected at the garden hatch.\n', 'garden')
    venue.post('orchard', 'Bring the crate to the packing shed.\n', 'shed')
    for job, site, reader, mode in [('live', 'quay', 'courier', 'current'),
                                    ('citation', 'quay', 'archivist', 'history'),
                                    ('withdrawn', 'quay', 'guest', 'current'),
                                    ('rival', 'quay', 'alternate-courier', 'current'),
                                    ('detached', 'quay', 'remote', 'unsupported-live'),
                                    ('other', 'orchard', 'packer', 'current')]:
        venue.commission('tour-organizer', job, site, reader, mode, 'editor')

    app('init', '--goal-text', 'Give the evening courier a usable pickup direction.')
    evidence('init')
    foreign1, source1 = import_reading('The garden hatch currently serves evening pickup.', 0)
    card1 = draft('garden', 0)
    first = candidate_body()
    plan = dict(scope='First shared direction for the quay courier', candidate=card1,
                forecast='The courier can use the prepared direction after it is shared.',
                rival='The pickup moves during preparation.', references=[])
    bad = dict(id='foreign-trial', expected=0, outside=foreign1, **plan)
    error = app('feedback', 'open', '--spec', spec(bad), refused=True)
    assert foreign1['id'] in error, error
    feedback('open', 0, outside=source1, **plan)
    preflight = evidence('check', foreign1['id'], '--source', export)
    assert preflight['source_edition_matches']
    assert venue.active_door('quay') == first['door']
    pre_review = observe(1, card1, source1,
                         'A private check with the venue found the prepared door active at the time of reading.',
                         'review', 'Recheck with the board owner at first public disclosure.')
    assert pre_review['state'] == 'review'
    trace['first reading'] = dict(collective=foreign1, native=source1, foreign_pin_refused=True,
                                  current_at_preflight=preflight['source_edition_matches'], private_feedback=pre_review['state'])

    # The board changes after preflight. Native and collective stores receive no notification.
    assert venue.share('live', first) == 'contributor approval required'
    live1 = venue.approve('editor', 'live', 'courier', first)
    citation = venue.approve('editor', 'citation', 'archivist', first)
    detached = venue.approve('editor', 'detached', 'remote', first)
    guest = venue.approve('editor', 'withdrawn', 'guest', first)
    assert not venue.withdraw('tour-organizer', guest)
    assert venue.withdraw('editor', guest)
    assert venue.approve('editor', 'withdrawn', 'guest', first) == guest
    assert venue.share('withdrawn', first, guest) == 'disclosure withdrawn' and venue.read('guest') == []
    # Identical source, current edition, second open commission, different audience.
    moved = venue.share('rival', first, guest)
    active_moved = venue.share('rival', first, live1)
    renamed = venue.share('rival', dict(first, job='rival', reader='alternate-courier'), live1)
    altered = venue.share('live', dict(first, door='cellar'), live1)
    assert moved == active_moved == renamed == altered == 'contributor approval required'
    assert venue.read('alternate-courier') == venue.read('courier') == []
    trace['contributor scope'] = dict(withdrawn='disclosure withdrawn', other_job=moved,
                                      active_grant_other_job=active_moved, relabeled=renamed,
                                      changed_door=altered, other_reader_board=[])
    venue.post('quay', 'Outdoor access closes at dusk. Find the night porter by the canal door for parcels.\n',
               'canal')
    assert venue.export('quay', export) == 2
    checked = evidence('check', foreign1['id'], '--source', export)
    local = app('feedback', 'inspect', 'courier-use')['route']
    assert not checked['source_edition_matches'] and not checked['selection_at_old_position_matches']
    assert local['state'] == 'review' and local['stale'] == [] and pin(app('show', 'quay-source')) == source1
    trace['after external change'] = dict(native_state=local['state'], local_stale=local['stale'],
                                          canonical_recheck=checked)
    held = venue.share('live', first, live1)
    assert held == 'source changed' and venue.read('courier') == []
    assert venue.share('citation', first, citation) == 'shared'
    assert venue.read('archivist') == [('citation', 1, 'garden')]
    assert venue.share('detached', first, detached) == 'no admission contract' and venue.read('remote') == []
    # A direct job can use the actual owner's means without first going through G60.
    orchard_export = work / 'orchard.txt'
    orchard_edition = venue.export('orchard', orchard_export)
    other = dict(issuer='venue-office', site='orchard', edition=orchard_edition,
                 source_hash=digest(orchard_export.read_text()), door='shed')
    other_grant = venue.approve('editor', 'other', 'packer', other)
    assert venue.share('other', other, other_grant) == 'shared'
    assert venue.read('packer') == [('other', 1, 'shed')]
    failed = observe(2, card1, source1, f'The venue held the first shared proposal: {held}. The courier saw no direction.',
                     'work', 'Read the changed notice, reinterpret the pickup and try a revised direction.')
    assert failed['unresolved_returns'] == [2]
    trace['first use'] = dict(live=held, courier_visible=[], archived=venue.read('archivist'),
                              revoked_visible=venue.read('guest'), detached_visible=venue.read('remote'),
                              independent=venue.read('packer'), native_return=failed['state'])

    foreign2, source2 = import_reading('The night porter now handles evening pickup at the canal door.', 1)
    card2 = draft('canal', 1)
    renewed = feedback('revise', 3, candidate=card2, outside=source2, references=[],
                       forecast='The courier receives the canal instruction and collects the parcel.',
                       rival='The direction is held or sends the courier to the wrong door.',
                       reason='The venue changed before first publication.')
    assert renewed['route']['unresolved_returns'] == [2]
    second = candidate_body()
    assert venue.share('live', second, live1) == 'contributor approval required'
    live2 = venue.approve('editor', 'live', 'courier', second)
    assert venue.share('live', second, live2) == 'shared'
    assert venue.share('live', second, live2) == 'already shared'
    visible = venue.read('courier')
    assert visible == [('live', 2, 'canal')]
    outcome = venue.collect('live', 'courier')
    assert outcome == 'parcel collected'
    assert venue.collect('live', 'courier') == 'already collected'
    review = observe(4, card2, source2, f'One revised direction was visible. At its door: {outcome}.',
                     'review', 'The editor may accept this particular use.',
                     dict(return_number=2, account='The previous direction was withheld. The revised one was received and exercised.'))
    assert review['unresolved_returns'] == []
    accepted = feedback('accept', 5, owner='courier-editor', account='The revised direction served this pickup.')
    assert accepted['route']['state'] == 'accepted'
    historical = app('feedback', 'inspect', 'courier-use', '--revision', 1)['record']['content']
    assert pin(historical['outside']) == source1 and pin(historical['candidate']) == card1
    # A different experimenter-supplied interpretation of exactly the same current notice.
    rival = dict(second, door='garden')
    rival_grant = venue.approve('editor', 'rival', 'alternate-courier', rival)
    assert venue.share('rival', rival, rival_grant) == 'shared'
    rival_visible = venue.read('alternate-courier')
    assert rival_visible == [('rival', 2, 'garden')]
    rival_outcome = venue.collect('rival', 'alternate-courier')
    assert rival_outcome == 'wrong entrance'
    trace['wrong reading of current notice'] = dict(board=rival_visible, outcome=rival_outcome)
    trace['recovery'] = dict(new_collective=foreign2, new_native=source2, actual_courier_board=visible,
                             actual_fixture_use=outcome, native_state=accepted['route']['state'],
                             original_forecast_recoverable=True)
    assert venue.withdraw('editor', live2)
    assert venue.share('live', second, live2) == 'disclosure withdrawn'
    assert venue.read('courier') == visible
    trace['withdrawal after publication'] = dict(retry='disclosure withdrawn', existing_board=visible)
    return trace


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('release', type=Path)
    parser.add_argument('--work', type=Path, help='New directory outside the release for retained records')
    args = parser.parse_args()
    product = args.release.resolve()
    if args.work:
        work = args.work.resolve()
        if work == product or product in work.parents:
            parser.error('Use a directory outside the release.')
        work.mkdir(parents=True, exist_ok=False)
        result = run(product, work)
    else:
        with TemporaryDirectory() as tmp:
            result = run(product, Path(tmp))
    print(json.dumps(result, indent=2))
