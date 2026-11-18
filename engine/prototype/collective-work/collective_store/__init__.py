"""Public collective store facade; internal objects live only for one local transaction."""
from pathlib import Path
import time

from .contract import CONTEXT_LIMIT, Refused, pin
from .database import CollectiveDatabase
from .session import Session

PRODUCT = Path(__file__).resolve().parents[1]


class Collective:
    def __init__(self, database, *, clock=time.time, product=PRODUCT):
        self.database = CollectiveDatabase(database, product)
        self.path = self.database.path
        self.clock = clock

    @classmethod
    def initialize(cls, database, *, product=PRODUCT):
        return CollectiveDatabase.initialize(database, product)

    @classmethod
    def upgrade(cls, database, *, product=PRODUCT):
        return CollectiveDatabase.upgrade(database, product)

    def call(self, request):
        return self.database.execute(request, lambda db: Session(db, self.clock).operations())

    def show(self, ident, revision=None, *, content=False):
        with self.database.read() as db:
            records = Session(db, self.clock).revisions
            if content:
                return records.source(ident, revision, with_body=True)
            return records.get(ident, revision)

    def next_review(self, ident):
        with self.database.read() as db:
            return Session(db, self.clock).reviews.next(ident)

    def use_source(self, target, source=None):
        with self.database.read() as db:
            return Session(db, self.clock).uses.source(target, source)

    def retained(self, kind, ident):
        with self.database.read() as db:
            return Session(db, self.clock).proposals.retained(kind, ident)

    def receipt(self, actor, command):
        return self.database.receipt(actor, command)

    def notice(self, ident):
        with self.database.read() as db:
            return Session(db, self.clock).notices.notice(ident)

    def encounters(self, *, after=0, limit=20, scan_limit=256):
        with self.database.read() as db:
            return Session(db, self.clock).notices.encounters(after=after, limit=limit, scan_limit=scan_limit)

    def view(self, kind, *, actor=None, after=0, limit=20):
        with self.database.read() as db:
            return Session(db, self.clock).notices.view(kind, actor=actor, after=after, limit=limit)
