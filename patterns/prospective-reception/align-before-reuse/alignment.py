"""Replay alignment of an existing offline carton counter to a receiving commission."""
import argparse
from collections import Counter, defaultdict
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from zipfile import ZipFile, ZipInfo

QUERY = 'Can we reuse our existing carton counter when combining orders submitted in separate batches?'
ARCH = 'research/current/architecture.md'
BOUNDARY = 'research/patterns/receiving-boundaries.md'
COORDINATION = 'research/patterns/coordination-boundaries.md'


def sha(body):
    return hashlib.sha256(body).hexdigest()


def reader(product, *args):
    proc = subprocess.run([sys.executable, '-B', product/'knowledge.py', *args],
                          capture_output=True, check=True, timeout=30)
    return json.loads(proc.stdout)


def acquire(product):
    hits = reader(product, 'discover', '--query', QUERY, '--limit', '6')['libraries'][0]
    found = [row['pin']['id'] for row in hits['bodies']]
    # These are the investigator's selections. Each complete source is independently checked.
    selected = {}
    for ident in [COORDINATION, ARCH, BOUNDARY]:
        row = reader(product, 'show', ident)
        body = row['text'].encode()
        assert sha(body) == row['pin']['sha256'] and len(body) == row['entry']['bytes']
        selected[ident] = {'bytes': len(body), 'sha256': sha(body)}
        if ident == ARCH:
            assert BOUNDARY in [link['target'] for link in row['entry']['references']]
    return dict(query=QUERY, examined=hits['coverage']['examined_bodies'], matched=hits['coverage']['matched_bodies'],
                limit=6, displayed=found, direct_reference=[ARCH, BOUNDARY], acquired=selected,
                catalogue=hits['library_sha256'])


def counter(product, root):
    # Install the released utility through its own teaching producer without copying its formula.
    source = product/'adapters/prospective-reception/receiving-use/partial.py'
    spec = importlib.util.spec_from_file_location('released_feedback_example', source)
    example = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(example)
    result = example.run(product, root)
    assert result['final_state'] == 'accepted'
    return root/'install-3/cartons.py', sha((root/'delivery-3.zip').read_bytes())


def cartons(program, quantity):
    proc = subprocess.run([sys.executable, '-B', str(program), str(quantity)],
                          capture_output=True, text=True, check=True, timeout=5)
    return int(proc.stdout)


def counter_plan(program, groups, slots, count_slots):
    """Return the counter's allocation and proposed contents; arrivals are in manifest order."""
    requests, plan = [], []
    for group in groups:
        quantity = sum(slots[item] for item in group) if count_slots else len(group)
        budget = cartons(program, quantity)
        requests.append({'items': group, 'input': quantity, 'allowed_cartons': budget})
        width = (len(group) + budget - 1) // budget
        for start in range(0, len(group), width):
            plan.append(group[start:start+width])
    return requests, plan


def receive(folder, plan, limit, pieces, accepted, slots):
    """Check the physical fixture at handover; verify actual archive members and payloads."""
    folder.mkdir()
    locations = {}
    failures = []
    for group in plan:
        if not group:
            continue
        address = accepted.get(group[0])
        if address is None:
            failures.append('unresolved destination')
            continue
        box = folder/(str(len(locations)) + '.zip')
        with ZipFile(box, 'x') as z:
            for item in group:
                z.writestr(ZipInfo(item), pieces[item])
        locations[box] = address
    handed, attempts = defaultdict(list), []
    for index, (box, address) in enumerate(locations.items(), 1):
        with ZipFile(box) as z:
            read = {item: z.read(item) for item in z.namelist()}
        attempts.append({'reader': address, 'items': list(read)})
        if index > limit:
            failures.append('carton allowance exhausted')
        elif any(accepted.get(item) != address for item in read):
            failures.append('sealed carton crosses separate deliveries')
        elif any(data != pieces[item] for item, data in read.items()):
            failures.append('changed contents')
        elif sum(slots[item] for item in read) > 3:
            failures.append('item slots exceed carton')
        else:
            handed[address].extend(read)
    delivered = [item for group in handed.values() for item in group]
    assert len(set(delivered)) == len(delivered)
    return dict(created=len(locations), offered=attempts, accepted=dict(handed),
                owed=sorted(set(pieces)-set(delivered)), failures=failures)


def run(product, root):
    discovery = acquire(product)
    program, package_sha = counter(product, root)
    pieces = {'one': b'Copper plate 17', 'two': b'Copper plate 22', 'three': b'Copper plate 41'}
    one_slot = {key: 1 for key in pieces}
    names = list(pieces)
    apart = dict(one='north', two='south', three='south')
    pooled = dict.fromkeys(names, 'conservation')
    one_batch = [names]
    two_batches = [['one'], ['two', 'three']]
    trials = {}
    def case(key, groups, destinations, capacity, slots=one_slot, by_size=False):
        requests, plan = counter_plan(program, groups, slots, by_size)
        receipt = receive(root/key, plan, capacity, pieces, destinations, slots)
        trials[key] = dict(scalar_results=requests, proposed=sum(row['allowed_cartons'] for row in requests),
                           available=capacity, **receipt)
    # Paired controls. Only arrival grouping, or only the receiving commission, changes.
    case('pooled-one-batch', one_batch, pooled, 1)
    case('pooled-two-batches', two_batches, pooled, 1)
    case('separate-one-batch', one_batch, apart, 2)
    case('separate-two-batches', two_batches, apart, 2)
    def by_destination(targets):
        groups = defaultdict(list)
        for item in names:
            if targets.get(item) is not None:
                groups[targets[item]].append(item)
        return list(groups.values())
    for key, targets, available in [('pooled-by-delivery', pooled, 1), ('separate-by-delivery', apart, 2)]:
        case(key, by_destination(targets), targets, available)
    partial = dict(one='north', two=None, three=None)
    case('known-delivery-only', by_destination(partial), partial, 3)
    assert not trials['pooled-one-batch']['owed'] and not trials['separate-two-batches']['owed']
    assert trials['pooled-two-batches']['failures'] == ['carton allowance exhausted']
    assert trials['separate-one-batch']['failures'] == ['sealed carton crosses separate deliveries']
    assert all(not trials[k]['owed'] for k in ['pooled-by-delivery', 'separate-by-delivery'])
    assert trials['known-delivery-only']['accepted'] == {'north': ['one']}
    # Reopen a hidden condition in the proposed replacement. An indivisible load now fills two slots.
    bulky = dict.fromkeys(names, 2)
    case('bulky-by-delivery', by_destination(pooled), pooled, 3, bulky)
    case('bulky-by-total-slots', by_destination(pooled), pooled, 3, bulky, by_size=True)
    assert trials['bulky-by-delivery']['proposed'] == 1
    assert trials['bulky-by-total-slots']['proposed'] == 2
    assert trials['bulky-by-delivery']['failures'] == ['item slots exceed carton']
    # Exhaust all allocations of three distinct units to two bins. This tests the scalar claim,
    # independent of the preceding packing heuristic. Keep the witness for three cartons.
    from itertools import product as assignments
    allocations = {}
    for n in (2, 3):
        fits = []
        for assignment in assignments(range(n), repeat=len(names)):
            loads = Counter()
            for item, dest in zip(names, assignment):
                loads[dest] += bulky[item]
            if max(loads.values()) <= 3:
                fits.append(assignment)
        allocations[str(n)] = len(fits)
    assert allocations == {'2': 0, '3': 6}
    trials['bulky-witness'] = receive(root/'bulky-witness', [[i] for i in names], 3, pieces, pooled, bulky)
    assert not trials['bulky-witness']['owed']
    return dict(source_manifest_sha256=sha(json.dumps({i:sha(v) for i,v in pieces.items()},sort_keys=True).encode()),
                unchanged_packaged_counter_sha256=package_sha, executable_sha256=sha(program.read_bytes()),
                discovery=discovery, trials=trials, feasible_assignments=allocations,
                scope='Trusted local ZIP-backed dispatch fixture; choosing the receiving partition and item dimensions is supplied interpretation.')


if __name__ == '__main__':
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument('product', type=Path, help='Extracted operating compact')
    args = cli.parse_args()
    with TemporaryDirectory() as directory:
        print(json.dumps(run(args.product.resolve(), Path(directory)), indent=2))
