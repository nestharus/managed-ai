"""Versioned target bodies and their exact source pins, within a caller's transaction."""
import json
from .contract import (CONTEXT_DETAIL, account, body, digest, encoded, fields, integer,
                       name, owned, pin, require)


class Revisions:
    def __init__(self, db, clock):
        self.db = db
        self.clock = clock

    def get(self, ident, revision=None, *, byte_limit=None):
        name(ident)
        # Capture's allowance must gate the SQL payload, before Python decodes it.
        # Stores initialized here use canonical JSON in SQLite's UTF-8 encoding.
        projection = 'record' if byte_limit is None else 'CASE WHEN length(CAST(record AS BLOB)) <= ? THEN record END'
        budget = () if byte_limit is None else (byte_limit,)
        if revision is None:
            row = self.db.execute('SELECT ' + projection + ' FROM artifacts WHERE id=?', budget + (ident,)).fetchone()
        else:
            row = self.db.execute('SELECT ' + projection + ' FROM versions WHERE id=? AND revision=?',
                             budget + (ident, integer(revision, 'revision', 1))).fetchone()
        require(row is not None, 'missing-artifact', 'Artifact or version is unavailable: ' + ident)
        require(row[0] is not None, 'context-bound', CONTEXT_DETAIL)
        return json.loads(row[0])

    def source(self, ident, revision=None, *, with_body=False):
        """Read a bounded content slice or just its pin, without expanding a revision's audit trail."""
        name(ident)
        if revision is None:
            table, selector, params = 'artifacts', 'id=?', (ident,)
        else:
            table, selector, params = 'versions', 'id=? AND revision=?', (ident, integer(revision, 'revision', 1))
        project = "json_extract(record,'$.revision'),json_extract(record,'$.sha256')"
        if with_body:
            project += ",json_extract(record,'$.owner'),record -> '$.body'"
        row = self.db.execute(f'SELECT {project} FROM {table} WHERE {selector}', params).fetchone()
        require(row is not None, 'missing-artifact', 'Artifact or version is unavailable: ' + ident)
        value = {'id': ident, 'revision': row[0], 'sha256': row[1]}
        if with_body:
            value.update(owner=row[2], body=json.loads(row[3]))
        return value

    def verify(self, pins, *, current, keep_bodies=True):
        require(isinstance(pins, list) and len(pins) <= 128, 'invalid-input', 'Basis must contain at most 128 pins')
        seen = set()
        complete = []
        for value in pins:
            fields(value, {'id', 'revision', 'sha256'})
            require(set(value) == {'id', 'revision', 'sha256'}, 'invalid-input', 'Incomplete source pin')
            name(value['id'], 'source identity')
            integer(value['revision'], 'source revision', 1)
            require(value['id'] not in seen, 'invalid-input', 'Duplicate basis identity')
            seen.add(value['id'])
            revision = None if current else value['revision']
            read = self.get if keep_bodies else self.source
            actual = read(value['id'], revision)
            require(pin(actual) == value, 'basis-changed' if current else 'unknown-pin',
                    'The nominated basis does not match: ' + value['id'])
            if keep_bodies:
                complete.append(actual)
        return complete

    def save(self, record):
        raw = encoded(record)
        self.db.execute('INSERT INTO artifacts(id,record) VALUES (?,?) ON CONFLICT(id) DO UPDATE SET record=excluded.record',
                        (record['id'], raw))
        self.db.execute('INSERT INTO versions VALUES (?,?,?)', (record['id'], record['revision'], raw))

    def successor(self, target, content, basis, **account_fields):
        return {'id': target['id'], 'revision': target['revision'] + 1, 'owner': target['owner'],
                'body': content, 'sha256': digest(content), 'basis': basis,
                'reconsider_from': target['reconsider_from'], 'accepted_at': self.clock(),
                'previous': pin(target), **account_fields}

    def find(self, ident):
        row = self.db.execute('SELECT record FROM artifacts WHERE id=?', (ident,)).fetchone()
        return json.loads(row[0]) if row else None

    def first(self, r):
        fields(r, {'actor','command','op','id','body','reconsider_from'})
        ident = name(r.get('id'))
        require(self.db.execute('SELECT 1 FROM artifacts WHERE id=?', (ident,)).fetchone() is None,
                'already-exists', 'Artifact identity already exists')
        policy = r.get('reconsider_from', [])
        require(isinstance(policy, list) and len(policy) <= 128,
                'invalid-input', 'reconsider_from must be a unique participant list')
        for a in policy: name(a, 'participant')
        require(len(set(policy)) == len(policy), 'invalid-input', 'Duplicate participant')
        content = body(r.get('body'))
        record = {'id': ident, 'revision': 1, 'owner': r['actor'], 'body': content,
                  'sha256': digest(content), 'basis': [], 'reconsider_from': policy,
                  'accepted_at': self.clock(), 'previous': None}
        return record
