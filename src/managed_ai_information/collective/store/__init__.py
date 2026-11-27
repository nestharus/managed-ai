"""Public collective store facade; internal objects live only for one local transaction."""

import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .contract import CONTEXT_LIMIT, Refused, pin
from .database import CollectiveDatabase
from .session import Session

PRODUCT = Path(__file__).resolve().parents[1]

__all__ = [
    "CONTEXT_LIMIT",
    "PRODUCT",
    "Collective",
    "Refused",
    "pin",
]


class Collective:
    def __init__(
        self,
        database: Path | str,
        *,
        clock: Callable[[], float] = time.time,
        product: Path | str = PRODUCT,
        backend: Any | None = None,
        namespace: str = "collective",
    ) -> None:
        self.database = CollectiveDatabase(database, product, backend=backend, namespace=namespace)
        self.path = self.database.path
        self.clock = clock

    @classmethod
    def initialize(cls, database: Path | str, *, product: Path | str = PRODUCT) -> dict[str, str]:
        return CollectiveDatabase.initialize(database, product)

    @classmethod
    def upgrade(cls, database: Path | str, *, product: Path | str = PRODUCT) -> dict[str, Any]:
        return CollectiveDatabase.upgrade(database, product)

    def call(self, request: dict[str, Any]) -> dict[str, Any]:
        return self.database.execute(request, lambda db: Session(db, self.clock).operations())

    def show(
        self, ident: str, revision: int | None = None, *, content: bool = False
    ) -> dict[str, Any]:
        with self.database.read() as db:
            records = Session(db, self.clock).revisions
            if content:
                return records.source(ident, revision, with_body=True)
            return records.get(ident, revision)

    def next_review(self, ident: str) -> dict[str, Any] | None:
        with self.database.read() as db:
            return Session(db, self.clock).reviews.next(ident)

    def use_source(self, target: str, source: str | None = None) -> dict[str, Any] | None:
        with self.database.read() as db:
            return Session(db, self.clock).uses.source(target, source)

    def retained(self, kind: str, ident: str) -> Any:
        with self.database.read() as db:
            return Session(db, self.clock).proposals.retained(kind, ident)

    def receipt(self, actor: str, command: str) -> Any:
        return self.database.receipt(actor, command)

    def notice(self, ident: int) -> dict[str, Any]:
        with self.database.read() as db:
            return Session(db, self.clock).notices.notice(ident)

    def encounters(
        self, *, after: int = 0, limit: int = 20, scan_limit: int = 256
    ) -> dict[str, Any]:
        with self.database.read() as db:
            return Session(db, self.clock).notices.encounters(
                after=after, limit=limit, scan_limit=scan_limit
            )

    def view(
        self,
        kind: str,
        *,
        actor: str | None = None,
        after: int = 0,
        limit: int = 20,
    ) -> dict[str, Any]:
        with self.database.read() as db:
            return Session(db, self.clock).notices.view(kind, actor=actor, after=after, limit=limit)
