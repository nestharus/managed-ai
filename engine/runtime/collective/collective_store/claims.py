"""Optional owner leases. Issuance, expiration, generation fencing and release."""
import uuid
from .contract import fields, integer, name, owned, require


class Claims:
    def __init__(self, db, revisions, clock):
        self.db = db
        self.revisions = revisions
        self.clock = clock

    def claim(self, r):
        fields(r, {'actor','command','op','target','revision','seconds'})
        target = self.revisions.get(r.get('target')); owned(target, r['actor'])
        require(target['revision'] == integer(r.get('revision'),'revision',1), 'target-changed', 'Target revision changed')
        seconds = integer(r.get('seconds',60), 'claim seconds', 1, 300)
        old = self.db.execute('SELECT * FROM claims WHERE target=?', (target['id'],)).fetchone()
        require(not old or not old['active'] or old['expires'] <= self.clock(), 'already-claimed', 'This target has an active claim')
        generation = old['generation'] + 1 if old else 1
        token, expiry = uuid.uuid4().hex, self.clock() + seconds
        self.db.execute('INSERT INTO claims VALUES (?,?,?,?,?,1) ON CONFLICT(target) DO UPDATE SET actor=excluded.actor,generation=excluded.generation,token=excluded.token,expires=excluded.expires,active=1',
                   (target['id'],r['actor'],generation,token,expiry))
        return {'target':target['id'],'generation':generation,'token':token,'expires':expiry}

    def check(self, target, actor, token):
        lease = self.db.execute('SELECT * FROM claims WHERE target=?', (target,)).fetchone()
        if token is not None or (lease and lease['active']):
            require(lease is not None and lease['active'] and lease['token'] == token and
                    lease['actor'] == actor and lease['expires'] > self.clock(),
                    'claim-stale', 'The claim is absent, expired or superseded')
        return lease

    def release(self, r):
        fields(r, {'actor','command','op','target','token'})
        target = self.revisions.get(r.get('target')); owned(target,r['actor'])
        self.check(target['id'],r['actor'],name(r.get('token')))
        self.finish(target['id'])
        return {'released':target['id']}

    def finish(self, target):
        self.db.execute('UPDATE claims SET active=0 WHERE target=?', (target,))
