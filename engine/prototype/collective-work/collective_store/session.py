"""Compose concrete owners inside one SQLite transaction; the database owns commit."""
from .claims import Claims
from .contract import account, fields, pin
from .notices import NoticeBoard
from .proposals import Proposals
from .reviews import ImpactReviews
from .revisions import Revisions
from .uses import ReceivingUses


class Session:
    def __init__(self, db, clock):
        self.revisions = Revisions(db, clock)
        self.notices = NoticeBoard(db, self.revisions, clock)
        self.proposals = Proposals(db, self.revisions, self.notices, clock)
        self.reviews = ImpactReviews(db, self.revisions, self.proposals)
        self.claims = Claims(db, self.revisions, clock)
        self.uses = ReceivingUses(db, self.revisions, self.notices, self.claims, self.reviews)

    def operations(self):
        return {'create': self.create, 'watch': self.notices.watch, 'offer': self.notices.offer,
                  'announce': self.notices.announce, 'capture': self.proposals.capture,
                  'propose': self.proposals.propose, 'claim': self.claims.claim, 'release': self.claims.release,
                  'review': self.reviews.review, 'publish': self.publish, 'acknowledge': self.notices.acknowledge,
                  'qualify': self.uses.qualify, 'take': self.uses.take}

    def create(self, request):
        record = self.revisions.first(request)
        self.revisions.save(record)
        seq = self.notices.accepted_revision(record, request['actor'])
        return {'artifact': pin(record), 'event': seq}

    def publish(self, r):
        fields(r, {'actor','command','op','proposal','token','treatments','account'})
        proposal, context, target = self.proposals.current(r.get('proposal'), r['actor'])
        self.revisions.verify(context['basis'], current=True, keep_bodies=False)
        lease=self.claims.check(target['id'],r['actor'],r.get('token'))
        why=account(r.get('account'))
        treated, staged = self.reviews.prepare(target, context, proposal, r.get('treatments', []))
        record = self.revisions.successor(target, proposal['body'], context['basis'],
                    context=context['id'], proposal=proposal['id'], account=why, treatments=list(treated.values()))
        if staged:
            record['staged_treatments'] = staged
        self.uses.prepare_publication(target, context, proposal, why)
        self.revisions.save(record)
        event = self.notices.accepted_revision(record, r['actor'])
        self.reviews.settle(target, context, proposal, record, treated, staged)
        if lease:
            self.claims.finish(target['id'])
        result = {'artifact': pin(record), 'event': event, 'received_notices': list(treated)}
        if staged:
            result['staged_received'] = staged
        return result
