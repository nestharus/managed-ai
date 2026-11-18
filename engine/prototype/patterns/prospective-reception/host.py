"""Single-writer host journal for a prospective encounter. Python standard library."""
import argparse
import hashlib
import json
import os
import shutil
from pathlib import Path


def digest(data):
    return hashlib.sha256(data).hexdigest()


def require(condition, reason):
    if not condition:
        raise ValueError(reason)


def sync_dir(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def write_synced(path, data):
    with path.open('wb') as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def replace_bytes(path, data):
    temporary = path.with_name('.' + path.name + '.tmp')
    write_synced(temporary, data)
    os.replace(temporary, path)
    sync_dir(path.parent)


def read_input(directory, name):
    require(isinstance(name, str) and name not in ('', '.', '..') and Path(name).name == name,
            'input must be a single filename in the trusted spec directory')
    path = directory / name
    require(path.is_file() and not path.is_symlink(), 'input must be a regular local file')
    return path.read_bytes()


PURPOSES = {
    'unaided-product-use': ('product','question'),
    'informed-source-comparison': ('product','question','owner'),
}


class Host:
    def __init__(self, root):
        self.root = Path(root)
        self.events = self.root / 'events.jsonl'
        self.objects = self.root / 'objects'
        self.objects.mkdir(parents=True, exist_ok=True)

    def store(self, data):
        key = digest(data)
        path = self.objects / key
        if not path.exists():
            replace_bytes(path, data)
        require(digest(path.read_bytes()) == key, 'corrupt stored bytes')
        return key

    def record(self, kind, data):
        history = self.history()
        event = dict(sequence=len(history) + 1, previous=history[-1]['digest'] if history else None,
                     kind=kind, data=data)
        event['digest'] = digest(json.dumps(event, sort_keys=True).encode())
        body = ''.join(json.dumps(item) + '\n' for item in [*history, event]).encode()
        replace_bytes(self.events, body)
        return event

    def history(self):
        events = []
        body = self.events.read_bytes() if self.events.exists() else b''
        require(not body or body.endswith(b'\n'), 'uncertain journal tail: preserve journal; verify against last independent pin')
        for line in body.splitlines():
            event = json.loads(line)
            checksum = event.pop('digest')
            require(checksum == digest(json.dumps(event, sort_keys=True).encode()), 'edited event')
            require(event['sequence'] == len(events) + 1 and event['previous'] ==
                    (events[-1]['digest'] if events else None), 'broken event sequence')
            events.append(dict(event, digest=checksum))
        return events

    def issue(self, previous, actor, expected_actor, purpose, question, product, owner):
        require(actor == expected_actor, 'wrong recipient')
        require(question.strip(), 'receiver question missing')
        require(purpose in PURPOSES, 'unknown measurement purpose')
        custody = read_input(self.objects, owner)
        require(digest(custody) == owner, 'changed source in custody')
        pins = {'product':product,'owner':owner,'question':self.store(question)}
        if purpose == 'unaided-product-use':
            require(custody and custody not in question, 'complete owner leaked through question')
        packet = {name + '.txt': pins[name] for name in PURPOSES[purpose]}
        return dict(event=previous, actor=actor, owner_basis=owner, product=product,
                    purpose=purpose, packet=packet)

    def prepare(self, issuance):
        key = issuance['event']
        destination = self.root / 'outbox' / key
        require(not destination.exists() and not destination.is_symlink(),
                'uncertain delivery: public packet precedes journal commitment; preserve encounter')
        prepared = self.root / 'prepared'
        prepared.mkdir(exist_ok=True)
        ready = prepared / key
        if ready.exists():
            self.check_packet(ready, issuance)
            return
        # Only the single writer can access prepared. A partial build was never visible.
        building = prepared / ('.' + key)
        if building.exists():
            shutil.rmtree(building)
        building.mkdir()
        for name, pin in issuance['packet'].items():
            body = read_input(self.objects, pin)
            require(digest(body) == pin, 'changed frozen ' + name)
            write_synced(building / name, body)
        sync_dir(building)
        building.rename(ready)
        sync_dir(prepared)
        sync_dir(self.root)

    def publish(self, issuance):
        key = issuance['event']
        ready = self.root / 'prepared' / key
        outbox = self.root / 'outbox'
        outbox.mkdir(exist_ok=True)
        sync_dir(self.root)
        destination = outbox / key
        if destination.exists() or destination.is_symlink():
            require(not ready.exists(), 'uncertain delivery: both prepared and public packets exist')
            self.check_packet(destination, issuance)
        else:
            require(ready.is_dir(), 'uncertain delivery: neither prepared nor public packet exists')
            self.check_packet(ready, issuance)
            ready.rename(destination)
        # Repeat the directory syncs if interruption followed the publication rename.
        sync_dir(outbox)
        if ready.parent.is_dir():
            sync_dir(ready.parent)

    def verify_delivery(self, issuance):
        delivery = self.root / 'outbox' / issuance['event']
        require(delivery.is_dir(), 'no public packet; retry its committed issuance before receiving')
        require(not (self.root / 'prepared' / issuance['event']).exists(),
                'uncertain delivery: both prepared and public packets exist')
        self.check_packet(delivery, issuance)

    @staticmethod
    def check_packet(delivery, issuance):
        require(delivery.is_dir() and not delivery.is_symlink(), 'invalid packet directory')
        require({p.name for p in delivery.iterdir()} == set(issuance['packet']), 'packet membership changed')
        for name, pin in issuance['packet'].items():
            require(digest(read_input(delivery,name)) == pin, 'changed issued ' + name)

    def apply(self, op, spec, directory):
        history = self.history()
        state = history[-1]['kind'] if history else None
        expected = {'init': [None], 'project': ['init', 'revise', 'review'],
                    'issue': ['project'], 'return': ['issue'], 'revise': ['return'], 'issue-review': ['revise'], 'review': ['issue-review']}
        if op in ('issue', 'issue-review') and state == op:
            committed = history[-1]
            data = committed['data']
            require(set(spec) == {'event','question','actor','purpose'}, 'unexpected or missing fields')
            require(all(spec[k] == data[k] for k in ('event','actor','purpose')) and
                    digest(read_input(directory, spec['question'])) == data['packet']['question.txt'],
                    'different intent from committed issuance')
            self.publish(data)
            return committed
        require(state in expected[op], 'event out of order')
        read = lambda name: read_input(directory, name)
        if op == 'init':
            require(set(spec) - {'reviewer'} == {'owner', 'owner_sha256', 'writer', 'receiver', 'required_human'}, 'invalid setup')
            reviewer = spec.get('reviewer', spec['receiver'])
            require(isinstance(reviewer, str) and reviewer.strip() and reviewer != spec['writer'], 'invalid reviewer')
            require(all(isinstance(spec[k], str) and spec[k].strip() for k in ('writer','receiver')) and spec['writer'] != spec['receiver'] and type(spec['required_human']) is bool, 'invalid roles')
            require(self.store(read(spec['owner'])) == spec['owner_sha256'], 'owner bytes differ from authority pin')
            return self.record(op, spec)
        setup = history[0]['data']
        fields = {'project': {'product','owner','passage','decision','alternative','obligations','purpose'},
                  'issue': {'event','question','actor','purpose'},
                  'issue-review': {'event','question','actor','purpose'},
                  'return': {'event','product','actor','response','observations'},
                  'revise': {'event','product','response','revision','uptake'},
                  'review': {'event','revision','actor','report','outcomes','human_receipt'}}
        revised_owner = op == 'revise' and bool({'owner','owner_sha256'} & spec.keys())
        if revised_owner:
            fields[op].update({'owner','owner_sha256'})
        if op == 'review':
            fields[op].add('owner_sha256')
        require(set(spec) == fields[op], 'unexpected or missing fields')
        active_owner = next((e['data']['owner_basis'] for e in reversed(history) if 'owner_basis' in e['data']), setup['owner_sha256'])
        data = spec.copy()
        previous = history[-1]
        if op == 'project':
            require(spec['purpose'] in PURPOSES, 'declare the intended receiver condition')
            require(not setup['required_human'], 'external human approval pending before projection')
            owner = read(spec['owner'])
            require(self.store(owner) == active_owner, 'open pinned authority body')
            require(spec['passage'].strip() and spec['passage'].encode() in owner, 'owner passage absent')
            require(spec['decision'].strip() and spec['alternative'].strip() and spec['alternative'] != spec['decision'], 'supply contrasting decisions')
            ids = spec['obligations']
            require(type(ids) is list and ids and all(type(x) is str and x.strip() for x in ids) and len(set(ids)) == len(ids), 'invalid obligations')
            data['product'] = self.store(read(spec['product']))
            if state in ('review', 'revise'):
                require(data['product'] == previous['data']['revision'], 'stale product')
            data['owner'] = active_owner
            data['owner_basis'] = active_owner
        else:
            require(spec['event'] == previous['digest'], 'stale event')
            if op in ('issue','issue-review'):
                purpose = spec['purpose']
                require(purpose == previous['data']['purpose'], 'purpose differs from frozen intention')
                reviewer = setup.get('reviewer',setup['receiver'])
                product = previous['data']['product' if op == 'issue' else 'revision']
                data = self.issue(previous['digest'],spec['actor'],setup['receiver'] if op == 'issue' else reviewer,
                                  purpose,read(spec['question']),product,active_owner)
                data['obligations'] = previous['data']['obligations']
                self.prepare(data)
                committed = self.record(op, data)
                self.publish(data)
                return committed
            elif op == 'return':
                self.verify_delivery(previous['data'])
                data['purpose'] = previous['data']['purpose']
                data['obligations'] = previous['data']['obligations']
                require(spec['actor'] == setup['receiver'], 'receiver mismatch')
                require(self.store(read(spec['product'])) == previous['data']['product'], 'stale partial')
                data['product'] = previous['data']['product']
                response = read(spec['response'])
                self.check_quotes(spec['observations'], previous['data']['obligations'], response)
                data['response'] = self.store(response)
            elif op == 'revise':
                for field in ('product','response'):
                    data[field] = self.store(read(spec[field]))
                    require(data[field] == previous['data'][field], 'stale ' + field)
                data['purpose'] = previous['data']['purpose']
                data['obligations'] = list(previous['data']['observations'])
                data['prior_owner_basis'] = active_owner
                if revised_owner:
                    active_owner = self.store(read(spec['owner']))
                    require(active_owner == spec['owner_sha256'], 'changed source differs from independent authority pin')
                    data['owner'] = active_owner
                data['owner_basis'] = active_owner
                require(spec['uptake'].strip(), 'record revision or retention rationale')
                data['revision'] = self.store(read(spec['revision']))
            else:
                self.verify_delivery(previous['data'])
                data['purpose'] = previous['data']['purpose']
                require(spec['actor'] == setup.get('reviewer', setup['receiver']), 'reviewer mismatch')
                require(spec['owner_sha256'] == active_owner, 'stale review source basis')
                require(digest(read_input(self.objects, active_owner)) == active_owner, 'changed stored review source')
                data['owner_basis'] = active_owner
                data['revision'] = self.store(read(spec['revision']))
                require(data['revision'] == previous['data']['product'], 'changed basis requires recheck')
                report = read(spec['report'])
                self.check_quotes(spec['outcomes'], previous['data']['obligations'], report)
                data['report'] = self.store(report)
                # Real human approval needs out-of-band identity and phase verification.
                # This adapter deliberately cannot elevate a model's receipt to that status.
                require(spec['human_receipt'] is None, 'external human authority must be independently verified')
                data['authority'] = 'external human gate pending' if setup['required_human'] else 'scoped model encounter'
        return self.record(op, data)

    @staticmethod
    def check_quotes(mapping, allowed, body):
        require(type(mapping) is dict and set(mapping) <= set(allowed), 'unobserved obligation')
        require(all(isinstance(q, str) and q.strip() and q.encode() in body for q in mapping.values()), 'observation absent from receiver bytes')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('journal', type=Path)
    parser.add_argument('action', choices=['init','project','issue','return','revise','issue-review','review'])
    parser.add_argument('spec', type=Path)
    args = parser.parse_args()
    print(json.dumps(Host(args.journal).apply(args.action, json.loads(args.spec.read_text()), args.spec.parent), indent=2))
