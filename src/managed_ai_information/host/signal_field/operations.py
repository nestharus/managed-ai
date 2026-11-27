"""Signal-field queue service operations: enrollment, change fan-out, scheduling, and inspection."""

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
from .storage import SignalFieldStore, open_signal_database, validate_db_path
from .worker import run_worker_bounded


class Field:
    """One local atomic JSON store owner for recoverable observing-use eligibility."""

    def __init__(
        self,
        path: Path | str,
        *,
        product: Path | None = None,
        config: SignalFieldConfig = DEFAULT_CONFIG,
        backend: Any | None = None,
        namespace: str = "signal-field",
    ) -> None:
        self.config = config
        self.path = validate_db_path(path, product=product)
        self.db: SignalFieldStore = open_signal_database(self.path, config)
        self._backend = backend
        self._namespace = namespace
        self.sql_calls = 0
        self.rows_returned = 0

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> Field:
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    @contextmanager
    def mutation(self) -> Generator[dict[str, Any]]:
        self.sql_calls += 1
        with self.db.transaction() as state:
            yield state
        committed = self.db.last_committed_bytes
        if self._backend is not None and committed is not None:
            self._backend.put_record(
                self._namespace,
                f"signal-field/{self.path.name}",
                committed,
                expected_version=None,
                visibility="MetadataOnly",
                metadata={"kind": "signal-field", "title": self.path.name},
            )

    def _read_state(self) -> dict[str, Any]:
        self.sql_calls += 1
        return self.db.refresh()

    def _next_ticket(self, state: dict[str, Any]) -> int:
        next_val = int(state["meta"]["sequence"]) + 1
        state["meta"]["sequence"] = str(next_val)
        return next_val

    def _sources_map_from_state(
        self, state: dict[str, Any], names: Iterable[str]
    ) -> dict[str, dict[str, Any]]:
        loaded: dict[str, dict[str, Any]] = {}
        sources = state["sources"]
        for src in sorted(set(names)):
            row = sources.get(src)
            if row is not None:
                self.rows_returned += 1
                loaded[src] = {
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
        state = self._read_state()
        return self._basis_from_map(spec, self._sources_map_from_state(state, spec["sources"]))

    def _queue_in_state(
        self,
        state: dict[str, Any],
        use: dict[str, Any],
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
        old = state["jobs"].get(key)
        if ticket_known:
            ticket = existing_ticket if existing_ticket is not None else self._next_ticket(state)
        else:
            ticket = (
                old["ticket"]
                if old is not None and old["ticket"] is not None
                else self._next_ticket(state)
            )

        encoded_gap = encoded(gap) if gap else None
        if old is None:
            state["jobs"][key] = {
                "id": key,
                "use_id": use["id"],
                "generation": use["generation"],
                "basis": encoded(basis),
                "spec": use["spec"],
                "ticket": ticket,
                "token": None,
                "gap": encoded_gap,
                "attempt": None,
                "worker_outcome": None,
            }
        else:
            old["generation"] = use["generation"]
            old["basis"] = encoded(basis)
            old["spec"] = use["spec"]
            old["ticket"] = ticket
            old["gap"] = encoded_gap
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
        with self.mutation() as state:
            old = state["uses"].get(spec["id"])
            require(
                not old or loads_strict(old["spec"])["mode"] == spec["mode"],
                "a different receiving lifetime requires a distinct use identity",
            )
            if old and old["signature"] == signature:
                return {"enrolled": spec["id"], "changed": False}

            generation = int(old["generation"]) + 1 if old else 1
            if old is None:
                use_row: dict[str, Any] = {
                    "id": spec["id"],
                    "spec": encoded(spec),
                    "signature": signature,
                    "generation": generation,
                    "latest": None,
                    "last_rejected": None,
                }
                state["uses"][spec["id"]] = use_row
            else:
                old["spec"] = encoded(spec)
                old["signature"] = signature
                old["generation"] = generation
                use_row = old

            # Remove stale dependency edges for this use_id.
            for src_key in list(state["dependencies"]):
                bucket = state["dependencies"][src_key]
                if spec["id"] in bucket:
                    bucket = [uid for uid in bucket if uid != spec["id"]]
                    if bucket:
                        state["dependencies"][src_key] = bucket
                    else:
                        del state["dependencies"][src_key]

            for source in spec["sources"]:
                deps = state["dependencies"].setdefault(source, [])
                if spec["id"] not in deps:
                    deps.append(spec["id"])
                    deps.sort()

            if spec["mode"] != "each-change":
                src_map = self._sources_map_from_state(state, spec["sources"])
                self._queue_in_state(state, use_row, self._basis_from_map(spec, src_map))

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

        with self.mutation() as state:
            old = state["sources"].get(source)
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

            old_snapshot = dict(old) if old is not None else None
            state["sources"][source] = {
                "id": source,
                "epoch": epoch,
                "position": position,
                "value": encoded(value),
            }

            dep_ids = sorted(state["dependencies"].get(source, ()))
            users = [state["uses"][uid] for uid in dep_ids if uid in state["uses"]]
            self.rows_returned += len(users)

            parsed_users = [(use, loads_strict(use["spec"])) for use in users]
            active_specs = [
                (use, spec)
                for use, spec in parsed_users
                if spec["mode"] != "historical" and (spec["mode"] != "live" or notify)
            ]

            needed_sources = {name for _, spec in active_specs for name in spec["sources"]}
            source_map = self._sources_map_from_state(state, needed_sources)

            live = 0
            occurrences = 0
            gaps = 0

            for use, spec in active_specs:
                basis = self._basis_from_map(spec, source_map)
                if spec["mode"] == "live":
                    next_gen = int(use["generation"]) + 1
                    use["generation"] = next_gen
                    job_key = "use:" + use["id"]
                    existing_job = state["jobs"].get(job_key)
                    self._queue_in_state(
                        state,
                        use,
                        basis,
                        existing_ticket=existing_job["ticket"] if existing_job else None,
                        ticket_known=True,
                    )
                    live += 1
                else:
                    gap = None
                    if old_snapshot and (
                        old_snapshot["epoch"] != epoch or position != old_snapshot["position"] + 1
                    ):
                        gap = {
                            "source": source,
                            "before": {
                                "epoch": old_snapshot["epoch"],
                                "position": old_snapshot["position"],
                            },
                            "after": {"epoch": epoch, "position": position},
                            "requires": (
                                "Recover missing owed occurrences or obtain a separately "
                                "authorized "
                                "change of duty."
                            ),
                        }
                        gaps += 1
                    self._queue_in_state(
                        state, use, basis, occurrence=[source, epoch, position], gap=gap
                    )
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
        with self.mutation() as state:
            live_entries = []
            needed_sources: set[str] = set()
            for _, use in sorted(state["uses"].items()):
                self.rows_returned += 1
                spec = loads_strict(use["spec"])
                if spec["mode"] != "live":
                    continue
                live_entries.append((use, spec))
                needed_sources.update(spec["sources"])

            source_map = self._sources_map_from_state(state, needed_sources)

            for use, spec in live_entries:
                basis = self._basis_from_map(spec, source_map)
                basis_encoded = encoded(basis)
                latest = loads_strict(use["latest"]) if use["latest"] else None
                job_key = "use:" + use["id"]
                job = state["jobs"].get(job_key)
                if job and job["basis"] == basis_encoded and job["spec"] == use["spec"]:
                    continue
                if latest and latest["basis"] == basis and latest["signature"] == use["signature"]:
                    continue

                use["generation"] = int(use["generation"]) + 1
                self._queue_in_state(
                    state,
                    use,
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
        with self.mutation() as state:
            eligible = [
                job
                for job in state["jobs"].values()
                if job["ticket"] is not None and job["token"] is None and job["gap"] is None
            ]
            if not eligible:
                return None

            job = min(eligible, key=lambda row: (int(row["ticket"]), str(row["id"])))
            self.rows_returned += 1
            token = uuid.uuid4().hex
            packet = {
                "job": job["id"],
                "use": job["use_id"],
                "generation": job["generation"],
                "token": token,
                "spec": loads_strict(job["spec"]),
                "basis": loads_strict(job["basis"]),
            }
            job["token"] = token
            job["ticket"] = None
            job["attempt"] = encoded(packet)
            job["worker_outcome"] = None
            return packet

    def _record_stale_return(
        self,
        state: dict[str, Any],
        job: dict[str, Any],
        use: dict[str, Any],
        packet: dict[str, Any],
        result: dict[str, Any],
    ) -> dict[str, Any]:
        use["last_rejected"] = encoded(
            {"generation": packet["generation"], "basis": packet["basis"], "result": result}
        )
        if packet["spec"]["mode"] == "live":
            current_spec = loads_strict(use["spec"])
            current_basis = self._basis_from_map(
                current_spec, self._sources_map_from_state(state, current_spec["sources"])
            )
            if job["basis"] != encoded(current_basis) or job["spec"] != use["spec"]:
                use["generation"] = int(use["generation"]) + 1
            self._queue_in_state(state, use, current_basis)

        job["token"] = None
        job["attempt"] = None
        job["worker_outcome"] = None
        if job["ticket"] is None:
            job["ticket"] = self._next_ticket(state)
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

        with self.mutation() as state:
            job = state["jobs"].get(packet["job"])
            if not job or job["token"] != packet["token"]:
                return {"status": "refused", "reason": "unknown, completed or replaced attempt"}
            if job["attempt"] != encoded(packet):
                return {
                    "status": "refused",
                    "reason": "packet differs from the actual captured attempt",
                }

            use = state["uses"][job["use_id"]]
            matches = (
                job["use_id"] == packet["use"]
                and job["generation"] == packet["generation"]
                and loads_strict(job["spec"]) == packet["spec"]
                and loads_strict(job["basis"]) == packet["basis"]
            )
            if packet["spec"]["mode"] == "live":
                live_basis = self._basis_from_map(
                    packet["spec"], self._sources_map_from_state(state, packet["spec"]["sources"])
                )
                matches = matches and use["spec"] == job["spec"] and live_basis == packet["basis"]
            if not matches:
                return self._record_stale_return(state, job, use, packet, result)

            snapshot = {
                "signature": digest(packet["spec"]),
                "generation": packet["generation"],
                "basis": packet["basis"],
                "result": result,
                "mode": packet["spec"]["mode"],
            }
            current_contract = use["signature"] == snapshot["signature"]
            if current_contract:
                use["latest"] = encoded(snapshot)

            if result["state"] == "blocked" and packet["spec"]["mode"] == "each-change":
                job["token"] = None
                job["attempt"] = None
                job["worker_outcome"] = None
                job["gap"] = encoded({"requires": result["account"]})
            else:
                del state["jobs"][job["id"]]

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
        with self.mutation() as state:
            row = state["jobs"].get(job_id)
            require(
                row is not None and row["token"] == token, "attempt is absent or already replaced"
            )
            row["token"] = None
            row["attempt"] = None
            row["worker_outcome"] = None
            if row["ticket"] is None:
                row["ticket"] = self._next_ticket(state)
        return {"retry_eligible": job_id, "old_worker_fenced": True}

    def retain(self, use_id: str, name: str) -> dict[str, Any]:
        require(
            isinstance(use_id, str) and use_id and isinstance(name, str) and name.strip(),
            "use_id and retained name must be nonempty strings",
        )
        with self.mutation() as state:
            row = state["uses"].get(use_id)
            require(row and row["latest"], "no actual received observation to retain")
            previous = state["selected_history"].get(name)
            require(
                not previous or previous["snapshot"] == row["latest"],
                "retained name identifies different historical observation",
            )
            if previous is None:
                state["selected_history"][name] = {"name": name, "snapshot": row["latest"]}
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
        with self.mutation() as state:
            job = state["jobs"].get(packet["job"])
            if job is not None and job["token"] == packet["token"]:
                job["worker_outcome"] = encoded(outcome)

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
