"""Addressed notices, open announcements, subscription snapshots and their cursor views."""
import json
from .contract import VIEW_LIMIT, account, body, encoded, fields, integer, name, owned, pin, require


def decode_notice(row):
    value = dict(row)
    value['required'] = bool(value['required'])
    value['body'] = json.loads(value['body'])
    value['acknowledgment'] = json.loads(value['acknowledgment']) if value['acknowledgment'] else None
    value['receipt'] = json.loads(value['receipt']) if value['receipt'] else None
    return value


class NoticeBoard:
    def __init__(self, db, revisions, clock):
        self.db = db
        self.revisions = revisions
        self.clock = clock

    def emit(self, kind, subject, actor, payload):
        seq = self.db.execute('INSERT INTO events(kind,subject,actor,body) VALUES (?,?,?,?)',
                         (kind, subject, actor, encoded(payload))).lastrowid
        return seq

    def accepted_revision(self, record, actor):
        seq = self.emit( 'accepted-artifact', record['id'], actor, {'artifact': pin(record)})
        for watch in self.db.execute('SELECT actor,reason FROM watches WHERE subject=?', (record['id'],)).fetchall():
            payload = {'event': seq, 'artifact': pin(record), 'reason': watch['reason'],
                       'meaning': 'A declared followed artifact changed; applicability needs interpretation.'}
            self.db.execute('INSERT INTO notices(receiver,target,kind,required,body,state) VALUES (?,?,?,?,?,?)',
                       (watch['actor'], record['id'], 'source-change', 0, encoded(payload), 'pending'))
        return seq

    def offer(self, r):
        fields(r, {'actor','command','op','target','basis','account','body'})
        target = self.revisions.get(r.get('target'))
        pins = r.get('basis', [])
        self.revisions.verify(pins, current=False, keep_bodies=False)
        content = body(r.get('body'))
        why = account(r.get('account'))
        required = r['actor'] in target['reconsider_from'] or '*' in target['reconsider_from']
        payload = {'from': r['actor'], 'basis': pins, 'account': why, 'contribution': content,
                   'target_at_offer': pin(target), 'policy_at_offer': target['reconsider_from']}
        notice = self.db.execute('INSERT INTO notices(receiver,target,kind,required,body,state) VALUES (?,?,?,?,?,?)',
                            (target['owner'], target['id'], 'proposed-impact', int(required), encoded(payload), 'pending')).lastrowid
        seq = self.emit( 'offered-impact', target['id'], r['actor'], {'notice': notice, 'required': required})
        return {'notice': notice, 'receiver': target['owner'], 'required': required, 'event': seq,
                'scope': 'An attributed proposed relationship; delivery does not establish uptake or truth.'}

    def watch(self, r):
        fields(r, {'actor','command','op','subject','reason'})
        subject, reason = name(r.get('subject')), account(r.get('reason'))
        self.db.execute('INSERT INTO watches VALUES (?,?,?) ON CONFLICT(actor,subject) DO UPDATE SET reason=excluded.reason',
                   (r['actor'], subject, reason))
        snapshot = self.revisions.find(subject)
        cursor = self.db.execute('SELECT COALESCE(MAX(seq),0) FROM events').fetchone()[0]
        return {'subject': subject, 'snapshot': snapshot, 'cursor': cursor,
                'scope': 'Subscription and snapshot share this transaction; subsequent accepted changes create durable notices.'}

    def announce(self, r):
        fields(r, {'actor', 'command', 'op', 'artifact', 'account'})
        record, = self.revisions.verify([r.get('artifact')], current=True)
        owned(record, r['actor'])
        note = account(r.get('account'))
        require(len(note.encode()) <= 2048, 'account-bound', 'An announcement must fit in 2 KiB')
        seq = self.emit( 'open-encounter', record['id'], r['actor'],
                         {'artifact': pin(record), 'account': note})
        return {'event': seq, 'artifact': pin(record),
                'scope': 'Available for inspection in this store. No addressed notice or acceptance decision.'}

    def acknowledge(self, r):
        fields(r, {'actor','command','op','notice','account'})
        no=integer(r.get('notice'),'notice',1); why=account(r.get('account'))
        row=self.db.execute('SELECT * FROM notices WHERE id=?',(no,)).fetchone()
        require(row is not None and row['receiver']==r['actor'],'wrong-recipient','Notice is addressed elsewhere')
        require(row['state']=='pending','notice-changed','Notice already received')
        require(row['acknowledgment'] is None, 'already-acknowledged', 'Delivery was already acknowledged')
        acknowledgment={'actor':r['actor'],'account':why,'at':self.clock(),
                        'scope':'Acknowledged delivery; no semantic uptake or target acceptance inferred.'}
        # A required impact stays pending until an accepted receiving contribution
        # records its treatment. An optional notice can leave the delivery inbox.
        state = 'pending' if row['required'] else 'received'
        self.db.execute('UPDATE notices SET state=?,acknowledgment=? WHERE id=?',
                   (state,encoded(acknowledgment),no))
        return {'notice':no,'state':state,'acknowledgment':acknowledgment}

    def notice(self, ident):
        row = self.db.execute('SELECT * FROM notices WHERE id=?', (integer(ident, 'notice', 1),)).fetchone()
        require(row is not None, 'missing-notice', 'Notice is unavailable')
        return decode_notice(row)

    def encounters(self, *, after=0, limit=20, scan_limit=256):
        """Return owner-selected accounts within a bounded traversal of the existing event log."""
        integer(after, 'cursor')
        integer(limit, 'limit', 1, 100)
        integer(scan_limit, 'scan limit', 1, 2048)
        cursor, examined, items = after, 0, []
        rows = self.db.execute(
            "SELECT seq, actor, CASE WHEN kind='open-encounter' THEN body END AS account "
            "FROM events WHERE seq>? ORDER BY seq LIMIT ?", (after, scan_limit))
        for row in rows:
            cursor = row['seq']
            examined += 1
            if row['account'] is not None:
                items.append({'event': cursor, 'actor': row['actor'], **json.loads(row['account'])})
                if len(items) == limit:
                    break
        more = self.db.execute('SELECT 1 FROM events WHERE seq>? LIMIT 1', (cursor,)).fetchone() is not None
        return {'items': items, 'after': after, 'next_after': cursor, 'examined_events': examined,
                'more_events': more,
                'scope': 'Owner-selected accounts at their recorded revisions. Inspect the source and current receiving use before acting.'}

    def view(self, kind, *, actor=None, after=0, limit=20):
        integer(after,'cursor'); integer(limit,'limit',1,100)
        if kind=='inbox':
            name(actor,'actor')
            total=self.db.execute('SELECT COUNT(*) FROM notices WHERE receiver=? AND state=? AND id>?',(actor,'pending',after)).fetchone()[0]
            rows=self.db.execute('SELECT * FROM notices WHERE receiver=? AND state=? AND id>? ORDER BY id LIMIT ?',
                            (actor,'pending',after,limit)).fetchall()
            values=[decode_notice(row) for row in rows]; key='id'
        elif kind=='changes':
            total=self.db.execute('SELECT COUNT(*) FROM events WHERE seq>?',(after,)).fetchone()[0]
            values=[{**dict(row),'body':json.loads(row['body'])} for row in self.db.execute('SELECT * FROM events WHERE seq>? ORDER BY seq LIMIT ?',(after,limit)).fetchall()]; key='seq'
        else: raise ValueError('Unknown view')
        selected=[]; size=0
        for value in values:
            n=len(encoded(value).encode())
            if size+n>VIEW_LIMIT: break
            selected.append(value); size+=n
        return {'items':selected,'after':after,'next_after':selected[-1][key] if selected else after,
                'remaining_count':total-len(selected),'complete':total==len(selected),
                'scope':'A bounded exact view of installed records; unknown relevance, unregistered routes and semantic completeness are not inferred.'}

    def select_for_capture(self, actor, target, nominated, staged):
        required = [] if staged else self.db.execute(
            "SELECT * FROM notices WHERE receiver=? AND target=? AND required=1 AND state='pending' LIMIT 129",
            (actor, target)).fetchall()
        selected = {row['id']: decode_notice(row) for row in required}
        for no in nominated:
            row = self.db.execute('SELECT * FROM notices WHERE id=?', (integer(no, 'notice', 1),)).fetchone()
            require(row is not None and row['receiver'] == actor, 'wrong-recipient', 'Notice is not addressed to this participant')
            selected[no] = decode_notice(row)
        require(len(selected) <= 128, 'context-bound', 'This bounded adapter cannot capture more than 128 selected notices; the unresolved receiving duty remains pending')
        return list(selected.values())

    def has_pending_required(self, target):
        return self.db.execute("SELECT 1 FROM notices WHERE target=? AND receiver=? AND required=1 AND state='pending' LIMIT 1",
                               (target['id'], target['owner'])).fetchone() is not None
