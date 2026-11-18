"""Continuing evidence enrollment and actual consumption at a receiving target."""
import json
from .contract import Refused, account, digest, encoded, fields, name, owned, pin, require


class ReceivingUses:
    def __init__(self, db, revisions, notices, claims, reviews):
        self.db = db
        self.revisions = revisions
        self.notices = notices
        self.claims = claims
        self.reviews = reviews

    @staticmethod
    def ready(content):
        return isinstance(content, dict) and content.get('format') == 'g60-receiver-use/1' and content.get('disposition') == 'ready'

    def prepare_publication(self, target, context, proposal, why):
        if self.ready(proposal['body']):
            self.renew_captured(target, context, proposal, why)
            self.require_current(target['id'])
            self.enroll(target['id'], proposal['id'], context['basis'])

    def changed(self, target):
        return self.db.execute("""
            SELECT pin FROM use_sources s WHERE target=? AND EXISTS (
                SELECT 1 FROM versions v WHERE v.id=s.source
                AND v.revision>json_extract(s.pin,'$.revision'))
            ORDER BY source LIMIT 1""", (target,)).fetchone()

    def source(self, target, source=None):
        """Expose one carried relation at its original and current source editions."""
        name(target)
        if source is None:
            row = self.changed(target)
        else:
            row = self.db.execute('SELECT pin FROM use_sources WHERE target=? AND source=?',
                             (target, name(source))).fetchone()
        if row is None:
            return None
        prior = json.loads(row[0])
        return {'source': prior, 'current': self.revisions.source(prior['id'])}

    def require_current(self, target):
        stale = self.changed(target)
        if stale:
            prior = json.loads(stale[0])
            raise Refused('basis-changed', 'Requalify the receiving relationship for ' + prior['id'])

    def qualify(self, r):
        fields(r, {'actor','command','op','target','prior','current','disposition','account','token'})
        target, = self.revisions.verify([r.get('target')], current=True)
        owned(target, r['actor'])
        self.claims.check(target['id'], r['actor'], r.get('token'))
        current = r.get('current')
        self.revisions.verify([current], current=True, keep_bodies=False)
        row = self.db.execute('SELECT pin FROM use_sources WHERE target=? AND source=?',
                         (target['id'], current['id'])).fetchone()
        require(row is not None and json.loads(row[0]) == r.get('prior'), 'use-relation-changed',
                'Return the exact prior source edition enrolled for this receiving target')
        disposition = r.get('disposition')
        require(disposition in ('renew','release'), 'invalid-input', 'Choose renew or release for this source')
        why = account(r.get('account'))
        if disposition == 'renew':
            self.db.execute('UPDATE use_sources SET pin=? WHERE target=? AND source=?',
                       (encoded(current), target['id'], current['id']))
        else:
            self.db.execute('DELETE FROM use_sources WHERE target=? AND source=?', (target['id'], current['id']))
        decision = {'target':pin(target),'prior':r['prior'],'current':current,'disposition':disposition,'account':why}
        event = self.notices.emit( 'qualified-use-source', target['id'], r['actor'], decision)
        return {'judgment':decision,'event':event}

    def renew_captured(self, target, context, proposal, why):
        # The ordinary direct route exposes each full current source to this proposal.
        # An omitted source requires its own explicit qualification before this use.
        for current in context['basis']:
            old = self.db.execute('SELECT pin FROM use_sources WHERE target=? AND source=?',
                             (target['id'],current['id'])).fetchone()
            if old and json.loads(old[0]) != current:
                self.db.execute('UPDATE use_sources SET pin=? WHERE target=? AND source=?',
                           (encoded(current),target['id'],current['id']))
                self.notices.emit( 'qualified-use-source', target['id'], target['owner'],
                           {'target':pin(target),'prior':json.loads(old[0]),'current':current,
                            'disposition':'renew','account':why,'context':context['id'],'proposal':proposal['id']})

    def enroll(self, target, proposal, basis):
        # Every active relation was checked under the same write transaction. If a
        # source is already enrolled, the current pin is exactly the retained one.
        self.db.executemany('INSERT OR IGNORE INTO use_sources VALUES (?,?,?)',
                       ((target,p['id'],encoded(p)) for p in basis if p['id'] != target))
        self.db.execute("""
            INSERT OR IGNORE INTO use_sources(target,source,pin)
            SELECT ?,json_extract(p.value,'$.id'),p.value
            FROM impact_reviews r,json_each(r.judgment,'$.basis') p
            WHERE r.proposal=? AND json_extract(p.value,'$.id') != ?""", (target, proposal, target))

    def take(self, r):
        """Admit the exact reviewed edition at this receiver's local point of use."""
        fields(r, {'actor', 'command', 'op', 'approval', 'token'})
        approval, = self.revisions.verify([r.get('approval')], current=True)
        owned(approval, r['actor'])
        decision = approval['body']
        require(self.ready(decision),
                'use-not-ready', 'This receiver has no ready decision at that revision')
        candidate_id = name(decision.get('candidate'), 'candidate identity')
        observation_id = name(decision.get('observation'), 'observation identity')
        # The target's own cited predecessor was current at publication. The approval
        # pin above now fences its successor; the old target is historical evidence.
        outside = [p for p in approval['basis'] if p['id'] != approval['id']]
        sources = {v['id']: v for v in self.revisions.verify(outside, current=True)}
        require(candidate_id != observation_id and candidate_id in sources and observation_id in sources,
                'unbound-use', 'Capture both the candidate and the receiving observation before approval')
        require(sources[candidate_id]['owner'] != r['actor'] and
                sources[observation_id]['owner'] == r['actor'] and observation_id != approval['id'],
                'wrong-use-authority', 'This use requires a separate producer and a receiver-owned observation')
        self.require_current(approval['id'])
        qualified = self.db.execute("""SELECT 1 FROM events WHERE kind='qualified-use-source'
                AND subject=? AND json_extract(body,'$.target.revision')=? LIMIT 1""",
                (approval['id'], approval['revision'])).fetchone()
        require(not qualified, 'basis-changed', 'Issue a new decision after changing its receiving relationship')
        if approval.get('staged_treatments'):
            require(not self.reviews.changed(approval['proposal'], installed_target=approval['id']), 'basis-changed',
                    'A source used to treat an offer changed before receiving use')
        pending = self.notices.has_pending_required(approval)
        require(not pending, 'reconsideration-required', 'Treat the new required observation before use')
        lease = self.claims.check(approval['id'], r['actor'], r.get('token'))
        bindings = {'approval': pin(approval), 'candidate': pin(sources[candidate_id]),
                    'observation': pin(sources[observation_id])}
        content = {'format': 'g60-receiver-use/1', 'disposition': 'taken', **bindings}
        record = self.revisions.successor(approval, content, approval['basis'],
                                          account='Admitted the approved edition at this receiving target.')
        self.revisions.save(record)
        self.notices.accepted_revision(record, r['actor'])
        self.db.execute('DELETE FROM use_sources WHERE target=?', (approval['id'],))
        if lease:
            self.claims.finish(approval['id'])
        event = self.notices.emit( 'received-use', approval['id'], r['actor'], bindings)
        return {'event': event, 'artifact': pin(record), **bindings,
                'scope': 'One local admission. Follow the pinned editions for the actual payload and instructions.'}
