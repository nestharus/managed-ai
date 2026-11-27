"""Signal-field queue service operations: enrollment, change fan-out, scheduling, and inspection."""

import sqlite3
import uuid
from collections.abc import Generator, Iterable
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .config import (
    DEFAULT_CONFIG,
    SignalFieldConfig,
    digest,
    encoded,
    loads_strict,
    require,
)
from .inspection import (
    field_status,
    inspect_entity,
    invoke_one_packet,
    pending_jobs,
    run_worker_loop,
)
from .models import job_view, oversized_job_summary
from .storage import open_signal_database, validate_db_path
from .worker import run_worker_bounded

_SQL_IN_CHUNK = 400


class Field:
    """One local SQLite owner for recoverable observing-use eligibility and bounded service."""

    def __init__(
        self,
        path: Path | str,
        *,
        product: Path | None = None,
        config: SignalFieldConfig = DEFAULT_CONFIG,
    ) -> None:
        self.config = config
        self.path = validate_db_path(path, product=product)
        self.db = open_signal_database(self.path, config)
        self.sql_calls = 0
        self.rows_returned = 0

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> Field:
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    @contextmanager
    def mutation(self) -> Generator[None]:
        self.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    def execute(self, query: str, values: tuple[Any, ...] = ()) -> sqlite3.Cursor:
        self.sql_calls += 1
        return self.db.execute(query, values)

    def one(self, query: str, values: tuple[Any, ...] = ()) -> Any:
        row = self.execute(query, values).fetchone()
        self.rows_returned += int(row is not None)
        return row

    def all(self, query: str, values: tuple[Any, ...] = ()) -> list[Any]:
        rows = self.execute(query, values).fetchall()
        self.rows_returned += len(rows)
        return rows

    def ticket(self) -> int:
        next_val = int(self.one("SELECT value FROM meta WHERE key=?", ("sequence",))["value"]) + 1
        self.execute("UPDATE meta SET value=? WHERE key=?", (str(next_val), "sequence"))
        return next_val

    def _sources_map(self, names: Iterable[str]) -> dict[str, dict[str, Any]]:
        unique = sorted(set(names))
        if not unique:
            return {}

        loaded: dict[str, dict[str, Any]] = {}
        for offset in range(0, len(unique), _SQL_IN_CHUNK):
            chunk = unique[offset : offset + _SQL_IN_CHUNK]
            rows = self.all(
                "SELECT id, epoch, position, value FROM sources WHERE id IN (SELECT value FROM "
                "json_each(?))",
                (encoded(chunk),),
            )
            for row in rows:
                loaded[row["id"]] = {
                    "epoch": row["epoch"],
                    "position": row["position"],
                    "value": loads_strict(row["value"]),
                }

        return loaded

    def _basis_from_map(
        self,
        spec: dict[str, Any],
        source_map: dict[str, dict[str, Any]],
    ) -> dict[str, dict[str, Any] | None]:
        return {name: source_map.get(name) for name in spec["sources"]}

    def basis(self, spec: dict[str, Any]) -> dict[str, dict[str, Any] | None]:
        return self._basis_from_map(spec, self._sources_map(spec["sources"]))

    def _existing_tickets(self, job_ids: Iterable[str]) -> dict[str, int | None]:
        unique = sorted(set(job_ids))
        if not unique:
            return {}

        tickets: dict[str, int | None] = {}
        for offset in range(0, len(unique), _SQL_IN_CHUNK):
            chunk = unique[offset : offset + _SQL_IN_CHUNK]
            rows = self.all(
                "SELECT id, ticket FROM jobs WHERE id IN (SELECT value FROM json_each(?))",
                (encoded(chunk),),
            )
            for row in rows:
                tickets[row["id"]] = row["ticket"]

        return tickets

    def queue(
        self,
        use: Any,
        basis: dict[str, Any],
        *,
        occurrence: Any = None,
        gap: Any = None,
        existing_ticket: int | None = None,
        ticket_known: bool = False,
    ) -> str:
        key = (
            ("occurrence:" + digest([use["id"], occurrence])) if occurrence else "use:" + use["id"]
        )
        if ticket_known:
            ticket = existing_ticket if existing_ticket is not None else self.ticket()
        else:
            old = self.one("SELECT ticket FROM jobs WHERE id=?", (key,))
            ticket = old["ticket"] if old and old["ticket"] is not None else self.ticket()

        self.execute(
            """INSERT INTO jobs (
                id, use_id, generation, basis, spec, ticket, token, gap, attempt, worker_outcome
            )
            VALUES (?,?,?,?,?,?,NULL,?,NULL,NULL)
            ON CONFLICT(id) DO UPDATE SET generation=excluded.generation,
            basis=excluded.basis, spec=excluded.spec, ticket=excluded.ticket, gap=excluded.gap""",
            (
                key,
                use["id"],
                use["generation"],
                encoded(basis),
                use["spec"],
                ticket,
                encoded(gap) if gap else None,
            ),
        )
        return key

    def enroll(self, spec: dict[str, Any]) -> dict[str, Any]:
        require(
            isinstance(spec, dict)
            and set(spec) == {"id", "mode", "question", "purpose", "sources", "observer", "scope"},
            "supply id, mode, question, purpose, sources, observer and scope",
        )
        require(
            all(
                isinstance(spec[k], str) and spec[k].strip()
                for k in ("id", "mode", "question", "purpose")
            ),
            "use identity, mode, question and purpose must be nonempty",
        )
        require(spec["mode"] in self.config.modes, "unsupported receiving lifetime")
        require(
            isinstance(spec["sources"], list)
            and len(set(spec["sources"])) == len(spec["sources"])
            and all(isinstance(s, str) and s for s in spec["sources"]),
            "sources must be distinct names",
        )

        signature = digest(spec)
        with self.mutation():
            old = self.one("SELECT * FROM uses WHERE id=?", (spec["id"],))
            require(
                not old or loads_strict(old["spec"])["mode"] == spec["mode"],
                "a different receiving lifetime requires a distinct use identity",
            )
            if old and old["signature"] == signature:
                return {"enrolled": spec["id"], "changed": False}

            generation = old["generation"] + 1 if old else 1
            self.execute(
                """INSERT INTO uses VALUES (?,?,?,?,NULL,NULL)
                ON CONFLICT(id) DO UPDATE SET spec=excluded.spec, signature=excluded.signature,
                generation=excluded.generation""",
                (spec["id"], encoded(spec), signature, generation),
            )
            self.execute("DELETE FROM dependencies WHERE use_id=?", (spec["id"],))
            for source in spec["sources"]:
                self.execute("INSERT INTO dependencies VALUES (?,?)", (source, spec["id"]))

            use = self.one("SELECT * FROM uses WHERE id=?", (spec["id"],))
            if spec["mode"] != "each-change":
                self.queue(use, self.basis(spec))

        return {"enrolled": spec["id"], "changed": True, "generation": generation}

    def change(
        self,
        source: str,
        epoch: str,
        position: int,
        value: Any,
        *,
        notify: bool = True,
        reset: bool = False,
    ) -> dict[str, Any]:
        require(
            isinstance(source, str) and source and isinstance(epoch, str) and epoch,
            "source and stream epoch must be nonempty",
        )
        require(type(position) is int and position >= 0, "position must be a nonnegative integer")
        require(type(notify) is bool and type(reset) is bool, "notify and reset must be booleans")

        with self.mutation():
            old = self.one("SELECT * FROM sources WHERE id=?", (source,))
            require(
                not old or old["epoch"] == epoch or reset,
                "stream epoch changed; explicit source reset required",
            )
            if old and old["epoch"] == epoch and position <= old["position"]:
                if position == old["position"]:
                    require(
                        old["value"] == encoded(value), "same stream position has different content"
                    )
                return {
                    "source": source,
                    "admitted": False,
                    "reason": "already observed stream position",
                }

            self.execute(
                """INSERT INTO sources VALUES (?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                epoch=excluded.epoch, position=excluded.position, value=excluded.value""",
                (source, epoch, position, encoded(value)),
            )

            users = self.all(
                """SELECT uses.* FROM uses JOIN dependencies ON uses.id=dependencies.use_id
                WHERE dependencies.source=? ORDER BY uses.id""",
                (source,),
            )

            parsed_users = [(use, loads_strict(use["spec"])) for use in users]
            active_specs = [
                (use, spec)
                for use, spec in parsed_users
                if spec["mode"] != "historical" and (spec["mode"] != "live" or notify)
            ]

            needed_sources = {name for _, spec in active_specs for name in spec["sources"]}
            source_map = self._sources_map(needed_sources)
            live_job_ids = [
                "use:" + use["id"] for use, spec in active_specs if spec["mode"] == "live"
            ]
            tickets = self._existing_tickets(live_job_ids)

            live = 0
            occurrences = 0
            gaps = 0

            for use, spec in active_specs:
                basis = self._basis_from_map(spec, source_map)
                if spec["mode"] == "live":
                    next_gen = use["generation"] + 1
                    self.execute("UPDATE uses SET generation=? WHERE id=?", (next_gen, use["id"]))
                    updated_use = {"id": use["id"], "generation": next_gen, "spec": use["spec"]}
                    job_key = "use:" + use["id"]
                    self.queue(
                        updated_use,
                        basis,
                        existing_ticket=tickets.get(job_key),
                        ticket_known=True,
                    )
                    live += 1
                else:
                    gap = None
                    if old and (old["epoch"] != epoch or position != old["position"] + 1):
                        gap = {
                            "source": source,
                            "before": {"epoch": old["epoch"], "position": old["position"]},
                            "after": {"epoch": epoch, "position": position},
                            "requires": (
                                "Recover missing owed occurrences or obtain a separately "
                                "authorized "
                                "change of duty."
                            ),
                        }
                        gaps += 1
                    self.queue(use, basis, occurrence=[source, epoch, position], gap=gap)
                    occurrences += 1

        return {
            "source": source,
            "admitted": True,
            "live_uses_affected": live,
            "owed_occurrences": occurrences,
            "replay_gaps": gaps,
        }

    def reconcile(self) -> dict[str, Any]:
        """Recover live rechecks from current dependencies; never fabricate missed occurrences."""
        queued = 0
        with self.mutation():
            uses = self.all("SELECT * FROM uses ORDER BY id")
            live_entries = []
            needed_sources: set[str] = set()
            for use in uses:
                spec = loads_strict(use["spec"])
                if spec["mode"] != "live":
                    continue
                live_entries.append((use, spec))
                needed_sources.update(spec["sources"])

            source_map = self._sources_map(needed_sources)
            live_job_ids = ["use:" + use["id"] for use, _ in live_entries]
            jobs_by_id: dict[str, Any] = {}
            for offset in range(0, len(live_job_ids), _SQL_IN_CHUNK):
                chunk = live_job_ids[offset : offset + _SQL_IN_CHUNK]
                for row in self.all(
                    "SELECT * FROM jobs WHERE id IN (SELECT value FROM json_each(?))",
                    (encoded(chunk),),
                ):
                    jobs_by_id[row["id"]] = row

            for use, spec in live_entries:
                basis = self._basis_from_map(spec, source_map)
                basis_encoded = encoded(basis)
                latest = loads_strict(use["latest"]) if use["latest"] else None
                job_key = "use:" + use["id"]
                job = jobs_by_id.get(job_key)
                if job and job["basis"] == basis_encoded and job["spec"] == use["spec"]:
                    continue
                if latest and latest["basis"] == basis and latest["signature"] == use["signature"]:
                    continue

                next_gen = use["generation"] + 1
                self.execute("UPDATE uses SET generation=? WHERE id=?", (next_gen, use["id"]))
                updated_use = {"id": use["id"], "generation": next_gen, "spec": use["spec"]}
                self.queue(
                    updated_use,
                    basis,
                    existing_ticket=job["ticket"] if job is not None else None,
                    ticket_known=True,
                )
                queued += 1

        return {
            "live_rechecks_reconstructed": queued,
            "scope": "Current declared dependencies only.",
        }

    def pull(self) -> dict[str, Any] | None:
        with self.mutation():
            job = self.one(
                "SELECT * FROM jobs WHERE ticket IS NOT NULL AND token IS NULL AND gap IS NULL "
                "ORDER BY ticket, id LIMIT 1"
            )
            if job is None:
                return None

            token = uuid.uuid4().hex
            packet = {
                "job": job["id"],
                "use": job["use_id"],
                "generation": job["generation"],
                "token": token,
                "spec": loads_strict(job["spec"]),
                "basis": loads_strict(job["basis"]),
            }
            self.execute(
                "UPDATE jobs SET token=?, ticket=NULL, attempt=?, worker_outcome=NULL WHERE id=?",
                (token, encoded(packet), job["id"]),
            )
            return packet

    def _record_stale_return(
        self,
        job: Any,
        use: Any,
        packet: dict[str, Any],
        result: dict[str, Any],
    ) -> dict[str, Any]:
        self.execute(
            "UPDATE uses SET last_rejected=? WHERE id=?",
            (
                encoded(
                    {"generation": packet["generation"], "basis": packet["basis"], "result": result}
                ),
                use["id"],
            ),
        )
        if packet["spec"]["mode"] == "live":
            current_spec = loads_strict(use["spec"])
            current_basis = self.basis(current_spec)
            if job["basis"] != encoded(current_basis) or job["spec"] != use["spec"]:
                self.execute("UPDATE uses SET generation=generation+1 WHERE id=?", (use["id"],))
                use = self.one("SELECT * FROM uses WHERE id=?", (use["id"],))
            self.queue(use, current_basis)

        self.execute(
            "UPDATE jobs SET token=NULL, attempt=NULL, worker_outcome=NULL, "
            "ticket=COALESCE(ticket,?) WHERE id=?",
            (self.ticket(), job["id"]),
        )
        return {
            "status": "stale",
            "reason": (
                "Returned work differs from current receiving basis; candidate retained separately "
                "by its native owner."
            ),
        }

    def receive(self, packet: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
        require(
            isinstance(packet, dict)
            and isinstance(packet.get("job"), str)
            and packet["job"]
            and isinstance(packet.get("token"), str)
            and packet["token"],
            "packet must be a captured job attempt",
        )
        require(
            isinstance(result, dict)
            and isinstance(result.get("state"), str)
            and result["state"] in {"complete", "blocked"},
            "worker must return complete or blocked with its actual result",
        )
        require(
            "account" in result
            and isinstance(result["account"], str)
            and result["account"].strip(),
            "worker must supply an account; service is not semantic acceptance",
        )

        with self.mutation():
            job = self.one("SELECT * FROM jobs WHERE id=?", (packet["job"],))
            if not job or job["token"] != packet["token"]:
                return {"status": "refused", "reason": "unknown, completed or replaced attempt"}
            if job["attempt"] != encoded(packet):
                return {
                    "status": "refused",
                    "reason": "packet differs from the actual captured attempt",
                }

            use = self.one("SELECT * FROM uses WHERE id=?", (job["use_id"],))
            matches = (
                job["use_id"] == packet["use"]
                and job["generation"] == packet["generation"]
                and loads_strict(job["spec"]) == packet["spec"]
                and loads_strict(job["basis"]) == packet["basis"]
            )
            if packet["spec"]["mode"] == "live":
                matches = (
                    matches
                    and use["spec"] == job["spec"]
                    and self.basis(packet["spec"]) == packet["basis"]
                )
            if not matches:
                return self._record_stale_return(job, use, packet, result)

            snapshot = {
                "signature": digest(packet["spec"]),
                "generation": packet["generation"],
                "basis": packet["basis"],
                "result": result,
                "mode": packet["spec"]["mode"],
            }
            current_contract = use["signature"] == snapshot["signature"]
            if current_contract:
                self.execute("UPDATE uses SET latest=? WHERE id=?", (encoded(snapshot), use["id"]))

            if result["state"] == "blocked" and packet["spec"]["mode"] == "each-change":
                self.execute(
                    "UPDATE jobs SET token=NULL, attempt=NULL, worker_outcome=NULL, gap=? WHERE "
                    "id=?",
                    (encoded({"requires": result["account"]}), job["id"]),
                )
            else:
                self.execute("DELETE FROM jobs WHERE id=?", (job["id"],))

            return {
                "status": "received",
                "service_state": result["state"],
                "current_contract": current_contract,
                "scope": (
                    "Attributed worker return, not an automatic observation-quality or whole-goal "
                    "judgment."
                ),
            }

    def retry(self, job_id: str, token: str) -> dict[str, Any]:
        """Explicit host recovery fences the old worker. The effect owner must recover effects
        separately.
        """
        require(
            isinstance(job_id, str) and job_id and isinstance(token, str) and token,
            "job_id and token must be nonempty strings",
        )
        with self.mutation():
            row = self.one("SELECT * FROM jobs WHERE id=?", (job_id,))
            require(
                row is not None and row["token"] == token, "attempt is absent or already replaced"
            )
            self.execute(
                "UPDATE jobs SET token=NULL, attempt=NULL, worker_outcome=NULL, "
                "ticket=COALESCE(ticket,?) WHERE id=?",
                (self.ticket(), job_id),
            )
        return {"retry_eligible": job_id, "old_worker_fenced": True}

    def retain(self, use_id: str, name: str) -> dict[str, Any]:
        require(
            isinstance(use_id, str) and use_id and isinstance(name, str) and name.strip(),
            "use_id and retained name must be nonempty strings",
        )
        with self.mutation():
            row = self.one("SELECT latest FROM uses WHERE id=?", (use_id,))
            require(row and row["latest"], "no actual received observation to retain")
            previous = self.one("SELECT snapshot FROM selected_history WHERE name=?", (name,))
            require(
                not previous or previous["snapshot"] == row["latest"],
                "retained name identifies different historical observation",
            )
            self.execute(
                "INSERT OR IGNORE INTO selected_history VALUES (?,?)", (name, row["latest"])
            )
        return {"retained": name, "scope": "Explicit historical cut; no current validity inferred."}

    @staticmethod
    def job_view(row: Any) -> dict[str, Any]:
        return job_view(row)

    @staticmethod
    def _oversized_job_summary(row: Any, full_bytes: int) -> dict[str, Any]:
        return oversized_job_summary(row, full_bytes)

    def pending(
        self,
        after: str = "",
        limit: int = DEFAULT_CONFIG.default_pending_limit,
        max_bytes: int = DEFAULT_CONFIG.default_pending_max_bytes,
    ) -> dict[str, Any]:
        return pending_jobs(self, after, limit, max_bytes)

    def inspect(
        self,
        use_id: str | None = None,
        job_id: str | None = None,
        history_name: str | None = None,
        max_bytes: int = DEFAULT_CONFIG.default_inspect_max_bytes,
    ) -> dict[str, Any]:
        return inspect_entity(self, use_id, job_id, history_name, max_bytes)

    def status(self) -> dict[str, Any]:
        return field_status(self)

    def _record_worker_outcome(self, packet: dict[str, Any], outcome: dict[str, Any]) -> None:
        with self.mutation():
            self.execute(
                "UPDATE jobs SET worker_outcome=? WHERE id=? AND token=?",
                (encoded(outcome), packet["job"], packet["token"]),
            )

    def _unresolved_run(
        self,
        packet: dict[str, Any],
        outcome: dict[str, Any],
        *,
        invoked: int,
        packet_bytes: int,
        received: list[dict[str, Any]],
        reason: str,
    ) -> dict[str, Any]:
        self._record_worker_outcome(packet, outcome)
        return {
            "status": "worker-unresolved",
            "invocations": invoked,
            "packet_bytes": packet_bytes,
            "returns": received,
            "reason": reason,
            "in_flight_job": packet["job"],
            "worker": outcome,
            "recovery": (
                "Inspect the retained attempt and actual worker/native outcome before explicitly "
                "retrying."
            ),
            "remaining": self.status(),
        }

    def _invoke_one_packet(
        self,
        argv: list[str],
        packet: dict[str, Any],
        payload: bytes,
        timeout: int | float,
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None, str | None]:
        return invoke_one_packet(self, argv, packet, payload, timeout, runner=run_worker_bounded)

    def run(self, argv: list[str], budget: int, timeout: int | float) -> dict[str, Any]:
        return run_worker_loop(self, argv, budget, timeout)
