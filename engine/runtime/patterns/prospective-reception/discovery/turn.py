"""Freeze and return one bounded receiving encounter. Python 3.10+, standard library."""
import argparse
import hashlib
import json
import subprocess
import sqlite3
import sys
from pathlib import Path

MAX_PACKET = 32768


def require(condition, message):
    if not condition:
        raise ValueError(message)


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def fields(spec, required, optional=()):
    require(isinstance(spec, dict) and set(required) <= spec.keys() <= set(required) | set(optional),
            f'expected fields {required}, optional {optional}')


def label(value):
    require(isinstance(value, str) and value.strip() and len(value.encode()) <= 2048, 'expected nonempty bounded text')
    return value


def bounded_list(value, limit):
    require(isinstance(value, list) and 1 <= len(value) <= limit, f'expected 1..{limit} entries')
    return value


def frozen(db, base, ref):
    window = isinstance(ref, dict)
    if window:
        fields(ref, ('path', 'start', 'end'))
    path = base / label(ref['path'] if window else ref)
    require(path.is_file(), 'expected an ordinary input file')
    maximum = 16 * 1024 * 1024 if window else MAX_PACKET
    with path.open('rb') as stream:
        data = stream.read(maximum + 1)
    require(0 < len(data) <= maximum, 'empty or oversized input; select a bounded byte window for long files')
    start, end = (ref['start'], ref['end']) if window else (0, len(data))
    require(type(start) is int and type(end) is int and 0 <= start < end <= len(data)
            and end-start <= MAX_PACKET, 'invalid byte window')
    owner = digest(data)
    selected = data[start:end]
    db.execute('INSERT OR IGNORE INTO owners VALUES(?, ?)', (owner, data))
    return {'sha256': digest(selected), 'text': selected.decode('utf-8'),
            'owner_sha256': owner, 'owner_bytes': len(data), 'start': start, 'end': end}


NARRATIVE = ('target', 'gap', 'prediction', 'rival')


def projected(db, base, spec):
    packet = {key: label(spec[key]) for key in NARRATIVE}
    require(packet['prediction'] != packet['rival'], 'prediction and rival must differ')
    packet['partial'] = frozen(db, base, spec['partial'])
    return packet


def prospect(db, spec, base):
    fields(spec, (*NARRATIVE, 'partial', 'query'))
    return {**projected(db, base, spec), 'kind': 'prospect', 'query': label(spec['query'])}


def native_lookup(query, reader, spec, base, limit):
    args = [sys.executable, '-B', str(reader)]
    if 'library' in spec:
        args.extend(['--library', str(base / label(spec['library']))])
    args.extend(['discover', '--limit', str(limit), '--query', query])
    for key in ('scan_limit', 'max_bytes'):
        if key in spec:
            require(type(spec[key]) is int and spec[key] > 0, 'invalid lookup limit')
            args.extend(['--' + key.replace('_', '-'), str(spec[key])])
    if 'index' in spec or 'index_sha256' in spec:
        require('index' in spec and 'index_sha256' in spec, 'index requires its digest')
        args.extend(['--index', str(base / label(spec['index'])), '--backend', 'fts5', '--index-sha256', label(spec['index_sha256'])])
    reply = subprocess.run(args, capture_output=True, timeout=60)
    require(reply.returncode == 0, reply.stderr.decode(errors='replace')[:512] or 'native lookup failed')
    body, = json.loads(reply.stdout)['libraries']
    require(body['format'] == 'knowledge-discovery/1' and body['query']['text'] == query, 'wrong knowledge return')
    return body, len(reply.stdout)


COVERAGE = ('examined_bodies', 'matched_bodies', 'omitted_body_count')


def retain(db, data):
    ident = digest(data)
    db.execute('INSERT OR IGNORE INTO owners VALUES(?, ?)', (ident, data))
    return ident


def federate(db, spec, base, prior, reader):
    fields(spec, ('prospect', 'reader', 'manifest', 'manifest_sha256'), ('limit',))
    path = base / label(spec['manifest'])
    with path.open('rb') as stream:
        raw = stream.read(262145)
    require(len(raw) <= 262144 and digest(raw) == spec['manifest_sha256'], 'manifest size or digest mismatch')
    manifest = json.loads(raw)
    fields(manifest, ('shards',))
    shards = bounded_list(manifest['shards'], 128)
    limit = spec.get('limit', 3)
    require(type(limit) is int and 1 <= limit <= 8, 'global limit must be 1..8')
    audit, candidates, names, editions = [], [], set(), set()
    for position, shard in enumerate(shards):
        fields(shard, ('name', 'library', 'index', 'index_sha256', 'scan_limit'), ('max_bytes',))
        name = label(shard['name'])
        require(name not in names, 'duplicate shard name')
        names.add(name)
        returned, wire = native_lookup(prior['query'], reader, shard, path.parent, limit)
        edition = returned['library_sha256']
        require(edition not in editions, 'duplicate library edition in federation')
        editions.add(edition)
        coverage = returned['coverage']
        preparation = returned['preparation']
        # Native lookup can skip index reads when its parser finds no searchable units.
        # Refuse that special case instead of implying pinned shards were verified.
        require(preparation['index_verified'], 'query did not exercise its pinned index; supply a searchable question')
        audit.append(dict(shard=name, library_sha256=edition, index_sha256=shard['index_sha256'],
                          index_read_bytes=preparation['index_read_bytes'], native_response_bytes=wire,
                          limits=coverage['limits'], indexed_body_bytes=coverage['indexed_body_bytes'],
                          **{key: coverage[key] for key in COVERAGE}))
        for rank, row in enumerate(returned['bodies']):
            # Query-unit coverage is comparable for this shared query/analyzer; BM25 scores are local.
            key = (not row['exact_identity'], -len(row['matched_terms']), rank, position)
            candidates.append((key, dict(shard=name, pin=row['pin'], title=row['title'])))
    totals = {key: sum(item[key] for item in audit) for key in COVERAGE}
    bodies = [row for _, row in sorted(candidates, key=lambda candidate: candidate[0])[:limit]]
    totals.update(shards_consulted=len(audit), matches_unissued=totals['matched_bodies'] - len(bodies))
    cost = {key: sum(item[key] for item in audit) for key in ('index_read_bytes', 'native_response_bytes')}
    return dict(kind='nominations', prospect=spec['prospect'], query=prior['query'], backend='federated-fts5',
                manifest_sha256=retain(db, raw), audit_sha256=retain(db, encode(audit)),
                coverage=totals, cost=cost, bodies=bodies)


def lookup(db, spec, base):
    prior = read(db, spec['prospect'], 'prospect')
    reader = base / label(spec['reader'])
    if 'manifest' in spec:
        return federate(db, spec, base, prior, reader)
    fields(spec, ('prospect', 'reader'), ('index', 'index_sha256', 'library', 'scan_limit', 'max_bytes'))
    body, _ = native_lookup(prior['query'], reader, spec, base, 3)
    return {'kind': 'nominations', 'prospect': spec['prospect'], 'query': prior['query'],
            'library_sha256': body['library_sha256'], 'backend': body['query']['backend'],
            'bodies': [{'pin': row['pin'], 'title': row['title']} for row in body['bodies'][:3]],
            'coverage': {k: body['coverage'][k] for k in COVERAGE}}


def one_use(db, base, use):
    fields(use, ('name', 'decision', 'authority'))
    return {'name': label(use['name']), 'decision': label(use['decision']), 'authority': frozen(db, base, use['authority'])}


def read(db, ident, kind=None):
    label(ident)
    row = db.execute('SELECT body FROM records WHERE id=?', (ident,)).fetchone()
    require(row and digest(row[0]) == ident, 'missing or altered record')
    obj = json.loads(row[0])
    require(kind is None or obj['kind'] == kind, f'expected {kind} record')
    return obj


def issue(db, spec, base):
    continuation = ('parent', 'relation', 'because')
    if 'prospect' in spec:
        fields(spec, ('prospect', 'sources', 'uses'), (*continuation, 'nominations'))
        prior = read(db, spec['prospect'], 'prospect')
        packet = {key: prior[key] for key in (*NARRATIVE, 'partial')}
        packet['prospect'] = spec['prospect']
        if 'nominations' in spec:
            nominations = read(db, spec['nominations'], 'nominations')
            require(nominations['prospect'] == spec['prospect'], 'nomination belongs to a different prospect')
            packet['nominations'] = spec['nominations']
    else:
        fields(spec, (*NARRATIVE, 'partial', 'sources', 'uses'), continuation)
        packet = projected(db, base, spec)
    packet['sources'] = []
    for source in bounded_list(spec['sources'], 3):
        fields(source, ('role', 'path'), ('identity', 'shard'))
        require('shard' not in source or 'identity' in source, 'shard needs a nominated identity')
        entry = {'role': label(source['role']), **frozen(db, base, source['path'])}
        if 'identity' in source:
            require('nominations' in packet, 'a nominated identity needs its lookup record')
            pins = [row['pin'] for row in nominations['bodies'] if row['pin']['id'] == source['identity']
                    and row.get('shard') == source.get('shard')]
            require(len(pins) == 1 and pins[0]['sha256'] == entry['owner_sha256'], 'owner differs from nominated pin')
            entry['pin'] = pins[0]
            if 'shard' in source:
                entry['shard'] = source['shard']
        packet['sources'].append(entry)
    require(len({s['role'] for s in packet['sources']}) == len(packet['sources']), 'duplicate source role')
    packet['uses'] = [one_use(db, base, use) for use in bounded_list(spec['uses'], 4)]
    require(len({use['name'] for use in packet['uses']}) == len(packet['uses']), 'duplicate use name')
    if any(key in spec for key in ('parent', 'relation', 'because')):
        require(all(key in spec for key in ('parent', 'relation', 'because')), 'incomplete continuation')
        parent = read(db, spec['parent'], 'observation')
        previous = read(db, parent['issue'], 'issue')
        old_use = next(use for use in previous['uses'] if use['name'] == parent['use'])
        relation = spec['relation']
        require(relation in ('reuse', 'reopen', 'split'), 'unknown relation')
        if relation == 'reuse':
            require(parent['verdict'] == 'supports', 'prior result does not support reuse')
            require(all(previous[key] == packet[key] for key in (*NARRATIVE, 'partial', 'sources')),
                    'changed encounter needs reopen or split')
            require(all((u['authority'], u['decision']) == (old_use['authority'], old_use['decision'])
                        for u in packet['uses']), 'changed authority or decision needs reopen or split')
        packet.update(parent=spec['parent'], relation=relation, because=label(spec['because']))
    packet['kind'] = 'issue'
    require(len(encode(packet)) <= MAX_PACKET, 'packet byte budget exceeded')
    return packet


def later_different_verdicts(db, ident, obs):
    # A separate projection leaves the optional v40 recall index and its cursor intact.
    db.execute('CREATE TABLE IF NOT EXISTS transfer_outcomes '
               '(issue TEXT, use TEXT, verdict TEXT, seq INTEGER, observation TEXT, PRIMARY KEY(issue,use,verdict))')
    db.execute('CREATE TABLE IF NOT EXISTS transfer_cursor (id INTEGER PRIMARY KEY CHECK(id=1), through INTEGER NOT NULL)')
    db.execute('INSERT OR IGNORE INTO transfer_cursor VALUES(1,0)')
    through, = db.execute('SELECT through FROM transfer_cursor WHERE id=1').fetchone()
    for through, ref, raw in db.execute('SELECT rowid,id,body FROM records WHERE rowid>? ORDER BY rowid', (through,)):
        require(digest(raw) == ref, 'altered custody record')
        record = json.loads(raw)
        if record['kind'] == 'observation':
            db.execute('INSERT OR REPLACE INTO transfer_outcomes VALUES(?,?,?,?,?)',
                       (record['issue'], record['use'], record['verdict'], through, ref))
    db.execute('UPDATE transfer_cursor SET through=? WHERE id=1', (through,))

    parent_seq, = db.execute('SELECT rowid FROM records WHERE id=?', (ident,)).fetchone()
    found = []
    for ref, verdict in db.execute('SELECT observation,verdict FROM transfer_outcomes '
            'WHERE issue=? AND use=? AND verdict<>? AND seq>? ORDER BY seq DESC',
            (obs['issue'], obs['use'], obs['verdict'], parent_seq)):
        record = read(db, ref, 'observation')
        require((record['issue'], record['use'], record['verdict']) == (obs['issue'], obs['use'], verdict),
                'transfer index differs from observation')
        found.append(dict(observation=ref, verdict=verdict))
    return found


def transfer(db, spec, base):
    fields(spec, ('parent', 'prospect', 'use', 'warrant', 'challenge'), ('roles',))
    parent = read(db, spec['parent'], 'observation')
    prior_issue = read(db, parent['issue'], 'issue')
    new = read(db, spec['prospect'], 'prospect')
    use = one_use(db, base, spec['use'])
    old = next(u for u in prior_issue['uses'] if u['name'] == parent['use'])
    sources = prior_issue['sources']
    if 'roles' in spec:
        roles = [label(role) for role in bounded_list(spec['roles'], 3)]
        sources = [source for source in sources if source['role'] in roles]
        require(len(sources) == len(roles), 'unknown or duplicate source role')
    later = later_different_verdicts(db, spec['parent'], parent)
    return {**{k: new[k] for k in (*NARRATIVE, 'partial')}, 'kind': 'issue', 'relation': 'transfer',
            'parent': spec['parent'], 'prospect': spec['prospect'], 'uses': [use],
            'sources': sources, 'previous_result': {'use': old['name'], 'decision': old['decision'],
                'verdict': parent['verdict'], 'result': parent['result'], 'evidence': parent['evidence']['sha256']},
            'authority_changed': old['authority'] != use['authority'], 'warrant': label(spec['warrant']),
            'challenge': label(spec['challenge']), **({'later_different_verdicts': later} if later else {})}


def observe(db, spec, base):
    fields(spec, ('issue', 'use', 'evidence', 'result', 'verdict'))
    previous = read(db, spec['issue'], 'issue')
    require(any(u['name'] == spec['use'] for u in previous['uses']), 'use was not issued')
    require(spec['verdict'] in ('supports', 'contradicts', 'unresolved'), 'unknown verdict')
    return dict(kind='observation', issue=spec['issue'], use=spec['use'], evidence=frozen(db, base, spec['evidence']),
                result=label(spec['result']), verdict=spec['verdict'])


def catchup(state, spec):
    """Read a bounded part of one observation stream at a fixed local journal cut."""
    fields(spec, ('parent',), ('limit', 'scan_limit', 'cut', 'cursor'))
    parent = label(spec['parent'])
    limit, scan_limit = spec.get('limit', 16), spec.get('scan_limit', 128)
    require(type(limit) is int and 1 <= limit <= 64, 'limit must be 1..64')
    require(type(scan_limit) is int and 1 <= scan_limit <= 256, 'scan_limit must be 1..256')
    require('cursor' not in spec or 'cut' in spec, 'cursor needs its original cut')
    path = Path(state).resolve()
    require(path.is_file(), 'catchup needs an existing journal')
    file_id = path.stat()
    with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as db:
        db.execute('BEGIN')
        origin = read(db, parent, 'observation')
        start, = db.execute('SELECT rowid FROM records WHERE id=?', (parent,)).fetchone()

        def pin(position, binding):
            row = db.execute('SELECT id FROM records WHERE rowid=?', (position,)).fetchone()
            require(row is not None, 'unknown journal position')
            return {'position': position, 'pin': digest(encode([binding, position, row[0]]))}

        def checked(token, low, high, binding):
            fields(token, ('position', 'pin'))
            n = token['position']
            require(type(n) is int and low <= n <= high and token == pin(n, binding),
                    'journal position outside scope or incompatible pin')
            return n

        head, = db.execute('SELECT MAX(rowid) FROM records').fetchone()
        binding = ['catchup/1', parent, file_id.st_dev, file_id.st_ino]
        if 'cut' in spec:
            end = checked(spec['cut'], start, head, binding)
            cut = spec['cut']
        else:
            end, cut = head, pin(head, binding)
        pos = checked(spec['cursor'], start, end, cut['pin']) if 'cursor' in spec else start
        refs = []
        scanned = scanned_bytes = 0
        for seq, ident, raw in db.execute('SELECT rowid,id,body FROM records WHERE rowid>? AND rowid<=? '
                                           'ORDER BY rowid LIMIT ?', (pos, end, scan_limit)):
            require(seq == pos+1 and digest(raw) == ident, 'missing or altered custody record')
            pos = seq
            scanned += 1
            scanned_bytes += len(raw)
            item = json.loads(raw)
            if item['kind'] == 'observation' and (item['issue'], item['use']) == (origin['issue'], origin['use']):
                refs.append(dict(observation=ident, verdict=item['verdict'], evidence_sha256=item['evidence']['sha256']))
                if len(refs) == limit:
                    break
        complete = pos == end
        result = dict(parent=parent, issue=origin['issue'], use=origin['use'], cut=cut, observations=refs,
                      complete=complete, records_scanned=scanned, record_body_bytes_scanned=scanned_bytes)
        if not complete:
            result['next'] = dict(parent=parent, cut=cut, cursor=pin(pos, cut['pin']), limit=limit, scan_limit=scan_limit)
        require(len(encode(result)) + 1 <= MAX_PACKET, 'catchup response byte budget exceeded')
        return result


def run(state, action, argument):
    if action not in ('show', 'recover'):
        file = Path(argument)
        require(file.stat().st_size <= MAX_PACKET, 'spec byte budget exceeded')
        spec = json.loads(file.read_bytes())
        if action == 'catchup':
            return catchup(state, spec)
    with sqlite3.connect(state) as db:
        db.execute('CREATE TABLE IF NOT EXISTS records(id TEXT PRIMARY KEY, body BLOB NOT NULL)')
        db.execute('CREATE TABLE IF NOT EXISTS owners(id TEXT PRIMARY KEY, body BLOB NOT NULL)')
        if action == 'recover':
            row = db.execute('SELECT body FROM owners WHERE id=?', (argument,)).fetchone()
            require(row and digest(row[0]) == argument, 'missing or altered owner')
            return row[0]
        if action == 'show':
            return {'id': argument, **read(db, argument)}
        result = {'open': prospect, 'lookup': lookup, 'issue': issue, 'observe': observe, 'transfer': transfer}[action](db, spec, file.parent)
        body = encode(result)
        ident = digest(body)
        require(len(encode({'id': ident, **result})) + 1 <= MAX_PACKET, 'packet byte budget exceeded')
        db.execute('INSERT OR IGNORE INTO records VALUES(?, ?)', (ident, body))
        return {'id': ident, **result}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('state', help='caller-owned SQLite file')
    parser.add_argument('action', choices=('open', 'lookup', 'issue', 'observe', 'transfer', 'catchup', 'show', 'recover'))
    parser.add_argument('argument', help='spec JSON path or, for show, record ID')
    args = parser.parse_args()
    try:
        result = run(args.state, args.action, args.argument)
        if isinstance(result, bytes):
            sys.stdout.buffer.write(result)
        else:
            print(encode(result).decode())
    except (ValueError, OSError, sqlite3.Error, subprocess.SubprocessError) as error:
        sys.exit(str(error))
