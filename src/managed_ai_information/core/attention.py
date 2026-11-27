"""Scoped public consideration and re-entry through an existing exchange.

No model calls, semantic classification, automatic effects or completeness claims.
One cooperative owner; use the CLI's lock or serialize embedded calls yourself.
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Any

if __name__ == "__main__":
    sys.dont_write_bytecode = True
    _src_root = str(Path(__file__).resolve().parents[2])
    if _src_root not in sys.path:
        sys.path.insert(0, _src_root)

from managed_ai_information import __version__
from managed_ai_information.core.exchange import Exchange
from managed_ai_information.core.ownership import external_root
from managed_ai_information.core.ownership import packet_id as _id
from managed_ai_information.core.ownership import relative_path as _relative
from managed_ai_information.core.storage import (
    MAX_BYTES,
    AdmissionContended,
    admission_deadline,
    encode,
    exclusive,
    now,
    owner_admission,
    read_json,
    sha,
    shared,
    write_json,
)
from managed_ai_information.core.storage import atomic_bytes as _atomic_bytes
from managed_ai_information.platform import strict_json_loads

BOUNDARY = (
    "Records describe declared public work, not hidden attention, truth, fulfillment, "
    "or all possible relationships. Changed bytes nominate interpretation; they do "
    "not invalidate history. A product binding records captured bytes, not successful "
    "uptake. No pending entry authorizes an effect or makes every inquiry mandatory."
)
MAX_ITEMS = 2000
MAX_TEXT = 24000
DISPOSITIONS = {"considered", "deferred"}


def _text(value: Any, name: str, limit: int = MAX_TEXT) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f"{name} must be nonempty text of at most {limit} characters")
    return value


def _strings(value: Any, name: str, limit: int = 100) -> list[str]:
    if (
        not isinstance(value, list)
        or len(value) > limit
        or any(not isinstance(x, str) for x in value)
    ):
        raise ValueError(f"{name} must be a bounded list of strings")
    if len(value) != len(set(value)):
        raise ValueError(f"{name} contains duplicates")
    return value


def _render_feedback_entry(row: dict[str, Any]) -> str:
    spec = row["spec"]
    lines = [
        f"\n### {row['id']} — {row['status']}",
        f"Use: {spec['use']}",
        f"Question: {spec['question']}",
        f"Groups: {', '.join(spec['groups']) or '(none)'}",
        f"Basis key: {row['basis_key']}",
    ]

    for entry in row["basis"]:
        lines.append(f"Source: {entry['path']}; object={entry['sha256']}; bytes={entry['bytes']}")

    if row["assessment"]:
        assessment = row["assessment"]
        lines.append(f"Last public account ({assessment['producer']}): {assessment['account']}")
        if assessment["remaining"]:
            lines.append("Open in that account: " + "; ".join(assessment["remaining"]))
        for prod in row["products"]:
            lines.append(
                f"Captured product: {prod['path']}; object={prod['sha256']}; "
                f"current bytes match={prod['current_bytes_match']}. "
                "Binding alone does not establish the claimed contribution."
            )

    return "\n".join(lines) + "\n"


class Attention:
    """Small receiving interface over Exchange's existing content store."""

    def __init__(self, exchange: Exchange, *, directory: Path | None = None):
        self.ex = exchange
        self.directory = directory if directory is not None else exchange.root / "attention"
        self.state_path = self.directory / "index.json"

    def index_identity(self) -> str | None:
        return sha(self.state_path.read_bytes()) if self.state_path.exists() else None

    def start_draft(self, draft: Attention) -> str | None:
        """Fork the index under the owner lock. Immutable evidence objects stay in one exchange."""
        draft.directory.mkdir()
        if self.state_path.exists():
            data = self.state_path.read_bytes()
            _atomic_bytes(draft.state_path, data)
            return sha(data)
        return None

    def feedback_object(self, ident: str) -> str | None:
        return self._load()["feedbacks"].get(_id(ident))

    def install_draft(
        self,
        draft: Attention,
        before: str | None,
        after: str,
        feedback: str,
        occurrence: str,
    ) -> None:
        """Install that selection once; preserve intervening work after its occurrence."""
        if self.feedback_object(feedback) == occurrence:
            return
        if self.index_identity() != before:
            raise ValueError(
                "Attention changed before commit; inspect and abandon-context, then reselect"
            )

        data = draft.state_path.read_bytes()
        if sha(data) != after or draft.feedback_object(feedback) != occurrence:
            raise ValueError("sealed Attention selection changed")

        self.directory.mkdir(exist_ok=True)
        _atomic_bytes(self.state_path, data)

    def _load(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return {
                "version": 1,
                "items": {},
                "field": None,
                "revisions": [],
                "feedbacks": {},
                "receipts": [],
                "sequence": 0,
            }

        doc = read_json(self.state_path)
        if doc.get("sha256") != sha(encode(doc.get("body"))):
            raise ValueError("attention index integrity mismatch")

        body = doc["body"]
        if body.get("version") != 1:
            raise ValueError("unsupported attention index version")
        return body

    def _save(self, state: dict[str, Any]) -> None:
        self.directory.mkdir(exist_ok=True)
        state["sequence"] += 1
        write_json(self.state_path, {"body": state, "sha256": sha(encode(state))})

    def _put(self, obj: Any) -> str:
        return self.ex.put(encode(obj))

    def _get(self, key: str) -> Any:
        return strict_json_loads(self.ex.get(key), f"attention object {key}", require_object=False)

    @staticmethod
    def _valid_latest_entries(latest: dict[str, Any], count: int) -> bool:
        for ident, bases in latest.items():
            if _id(ident) != ident or not isinstance(bases, dict) or not bases:
                return False
            for basis, ref in bases.items():
                if (
                    len(basis) != 64
                    or any(c not in "0123456789abcdef" for c in basis)
                    or not isinstance(ref, list)
                    or len(ref) != 2
                    or type(ref[0]) is not int
                    or type(ref[1]) is not int
                    or not (0 <= ref[0] < count and 0 <= ref[1] < MAX_ITEMS)
                ):
                    return False
        return True

    @staticmethod
    def _valid_current(view: Any, receipts_sha256: str, count: int) -> bool:
        """Check a disposable projection, not authenticate a cooperating owner."""
        if not isinstance(view, dict) or not isinstance(view.get("body"), dict):
            return False

        body = view["body"]
        if (
            view.get("sha256") != sha(encode(body))
            or body.get("version") != 1
            or body.get("receipts_sha256") != receipts_sha256
        ):
            return False

        ids = body.get("receipt_ids")
        latest = body.get("latest")
        if not isinstance(ids, dict) or not isinstance(latest, dict) or len(ids) != count:
            return False

        try:
            if any(
                _id(ident) != ident or type(pos) is not int or not 0 <= pos < count
                for ident, pos in ids.items()
            ) or set(ids.values()) != set(range(count)):
                return False
            return Attention._valid_latest_entries(latest, count)
        except TypeError, ValueError:
            return False

    @staticmethod
    def _index_receipt(current: dict[str, Any], position: int, record: dict[str, Any]) -> None:
        if record["id"] in current["receipt_ids"]:
            raise ValueError("duplicate receipt identity in attention history")

        current["receipt_ids"][record["id"]] = position
        for offset, assessment in enumerate(record["assessments"]):
            current["latest"].setdefault(assessment["item"], {})[assessment["basis_key"]] = [
                position,
                offset,
            ]

    @staticmethod
    def _seal_current(state: dict[str, Any], current: dict[str, Any]) -> None:
        current["receipts_sha256"] = sha(encode(state["receipts"]))
        state["current"] = {"body": current, "sha256": sha(encode(current))}

    def _current(self, state: dict[str, Any], *, rebuild: bool = False) -> dict[str, Any]:
        basis = sha(encode(state["receipts"]))
        if not rebuild and self._valid_current(state.get("current"), basis, len(state["receipts"])):
            return state["current"]["body"]

        current: dict[str, Any] = {"version": 1, "receipt_ids": {}, "latest": {}}
        for position, key in enumerate(state["receipts"]):
            self._index_receipt(current, position, self._get(key))
        self._seal_current(state, current)
        return current

    def _assessments(
        self,
        state: dict[str, Any],
        current: dict[str, Any],
        bases: list[tuple[str, str]],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        for attempt in range(2):
            records: dict[int, dict[str, Any]] = {}
            selected: dict[str, Any] = {}
            for ident, basis in bases:
                ref = current["latest"].get(ident, {}).get(basis)
                if ref is None:
                    continue
                position, offset = ref
                if position not in records:
                    records[position] = self._get(state["receipts"][position])
                record = records[position]
                if (
                    offset >= len(record["assessments"])
                    or current["receipt_ids"].get(record["id"]) != position
                ):
                    break
                assessment = record["assessments"][offset]
                if assessment["item"] != ident or assessment["basis_key"] != basis:
                    break
                selected[ident] = assessment | {
                    "receipt": record["id"],
                    "producer": record["producer"],
                }
            else:
                return current, selected

            if not attempt:
                current = self._current(state, rebuild=True)

        raise ValueError("attention current view does not match receipt history")

    def _read_input(self, path: str) -> tuple[dict[str, Any], dict[str, Any]]:
        binding = self.ex.snapshot(path)
        obj = self._get(binding["sha256"])
        if not isinstance(obj, dict):
            raise ValueError("input must be a JSON object")
        return obj, binding

    def _observed(
        self,
        paths: list[str],
        cache: dict[str, dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        rows = []
        cache = {} if cache is None else cache

        for path in paths:
            if path in cache:
                rows.append(cache[path])
                continue
            try:
                rows.append(self.ex.snapshot(path, optional=True))
            except (OSError, ValueError) as exc:
                self.ex.path(path)
                rows.append({"path": path, "sha256": None, "bytes": 0, "unavailable": str(exc)})
            cache[path] = rows[-1]

        return rows

    @staticmethod
    def _basis_key(spec: dict[str, Any], basis: list[dict[str, Any]]) -> str:
        return sha(encode({"question": spec["question"], "use": spec["use"], "basis": basis}))

    def track(self, spec: str) -> dict[str, Any]:
        request, source = self._read_input(spec)
        unknown = set(request) - {"items", "field"}
        if unknown:
            raise ValueError("unknown track fields: " + ", ".join(sorted(unknown)))

        rows = request.get("items", [])
        if not isinstance(rows, list) or len(rows) > MAX_ITEMS:
            raise ValueError("items must be a bounded list")

        state = self._load()
        normalized: dict[str, dict[str, Any]] = {}
        for row in rows:
            if not isinstance(row, dict) or set(row) - {
                "id",
                "question",
                "use",
                "basis",
                "groups",
                "active",
            }:
                raise ValueError("invalid item fields")
            ident = _id(row["id"])
            if ident in normalized:
                raise ValueError("duplicate item identity")

            basis = sorted(_relative(p) for p in _strings(row.get("basis", []), "basis"))
            for path in basis:
                self.ex.path(path)
            groups = sorted(_id(g) for g in _strings(row.get("groups", []), "groups"))
            active = row.get("active", True)
            if type(active) is not bool:
                raise ValueError("active must be Boolean navigation metadata")

            normalized[ident] = {
                "id": ident,
                "question": _text(row["question"], "question"),
                "use": _text(row["use"], "use"),
                "basis": basis,
                "groups": groups,
                "active": active,
            }

        if len(set(state["items"]) | set(normalized)) > MAX_ITEMS:
            raise ValueError(f"at most {MAX_ITEMS} item identities in this local journal")

        if "field" in request:
            field = _relative(request["field"])
            captured = self.ex.snapshot(field)
            self.ex.get(captured["sha256"]).decode("utf-8")
            state["field"] = field

        revision = {
            "created_at": now(),
            "source": source,
            "items": normalized,
            "field": state["field"],
        }
        for ident, item in normalized.items():
            state["items"][ident] = self._put(item)
        state["revisions"].append(self._put(revision))
        self._save(state)

        return {
            "tracked": list(normalized),
            "total_named": len(state["items"]),
            "field": state["field"],
            "sequence": state["sequence"],
            "boundary": BOUNDARY,
        }

    def _rows(
        self,
        state: dict[str, Any],
        selected_ids: set[str] | None = None,
        captured: dict[str, dict[str, Any]] | None = None,
        specs: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        current = self._current(state)
        prepared = []
        captured = {} if captured is None else captured
        if specs is None:
            specs = [self._get(key) for key in state["items"].values()]

        for spec in specs:
            ident = spec["id"]
            if not spec["active"] or (selected_ids is not None and ident not in selected_ids):
                continue
            basis = self._observed(spec["basis"], captured)
            prepared.append((ident, spec, basis, self._basis_key(spec, basis)))

        current, assessments = self._assessments(state, current, [(r[0], r[3]) for r in prepared])
        rows = []

        for ident, spec, basis, basis_key in prepared:
            assessment = assessments.get(ident)
            if assessment is None:
                status = "changed-basis" if ident in current["latest"] else "unaccounted"
            elif assessment["disposition"] == "deferred":
                status = "deferred"
            elif assessment["remaining"]:
                status = "considered-with-open-questions"
            else:
                status = "considered"

            products = []
            if assessment:
                for product in assessment["products"]:
                    present = self._observed([product["path"]], captured)[0]
                    products.append(
                        product
                        | {
                            "current_sha256": present["sha256"],
                            "current_bytes_match": product == present,
                        }
                    )

            rows.append(
                {
                    "id": ident,
                    "spec": spec,
                    "basis": basis,
                    "basis_key": basis_key,
                    "status": status,
                    "assessment": assessment,
                    "products": products,
                }
            )

        return rows

    def _render_feedback_body(
        self,
        ident: str,
        active: list[dict[str, Any]],
        selected: list[dict[str, Any]],
        selected_ids: set[str],
        counts: dict[str, int],
        field: dict[str, Any] | None,
        limit: int,
        max_bytes: int,
    ) -> tuple[str, list[dict[str, Any]], list[str], list[str]]:
        field_text = (
            self.ex.get(field["sha256"]).decode("utf-8") if field else "(No wider field supplied.)"
        )
        text = (
            f"# Attention return: {ident}\n\n{BOUNDARY}\n\n"
            f"Active declared items: {len(active)}. Selected by this view: {len(selected)}. "
            "Current counts within this selected view only: "
            f"{json.dumps(counts, ensure_ascii=False)}. "
            "Sources outside this view were not revalidated.\n\n"
            "## Wider field (caller/model-authored material, not a priority oracle)\n\n"
            + field_text
            + "\n\n## Selected considerations\n"
        )

        tail_max = 800
        if len(text.encode("utf-8")) + tail_max > max_bytes:
            raise ValueError(
                "wider field cannot fit; supply a smaller explicit field or increase the bound"
            )

        delivered: list[dict[str, Any]] = []
        omitted: list[str] = []
        for row in selected:
            block = _render_feedback_entry(row)
            if (
                len(delivered) >= limit
                or len((text + block).encode("utf-8")) + tail_max > max_bytes
            ):
                omitted.append(row["id"])
                continue
            text += block
            delivered.append(row)

        unselected = [spec["id"] for spec in active if spec["id"] not in selected_ids]
        text += (
            f"\nDelivery: {len(delivered)} shown; {len(omitted)} selected items omitted by bounds; "
            f"{len(unselected)} active items outside this view. "
            "Inspect the saved feedback for exact IDs.\n"
            "No absence from this view establishes irrelevance. "
            "No named-list completion establishes exhausted discovery. "
            "Develop further implications or change the view when that is useful.\n"
        )
        return text, delivered, omitted, unselected

    def feedback(
        self,
        feedback: str,
        groups: list[str] | None = None,
        items: list[str] | None = None,
        limit: int = 8,
        max_bytes: int = 16000,
    ) -> dict[str, Any]:
        ident = _id(feedback)
        if not 1 <= limit <= MAX_ITEMS or not 1024 <= max_bytes <= MAX_BYTES:
            raise ValueError("invalid delivery bounds")

        groups = _strings(groups or [], "groups")
        items = _strings(items or [], "items", MAX_ITEMS)
        state = self._load()
        if ident in state["feedbacks"]:
            raise ValueError("feedback identity already used")

        specs = [self._get(key) for key in state["items"].values()]
        active = [spec for spec in specs if spec["active"]]
        active_ids = {spec["id"] for spec in active}
        active_groups = {g for spec in active for g in spec["groups"]}
        if set(items) - active_ids or set(groups) - active_groups:
            raise ValueError("unknown active item or group")

        selected_ids = {
            spec["id"]
            for spec in active
            if not (items or groups)
            or spec["id"] in items
            or set(spec["groups"]).intersection(groups)
        }
        captured: dict[str, dict[str, Any]] = {}
        selected = self._rows(state, selected_ids, captured, specs)

        order = {
            "unaccounted": 0,
            "changed-basis": 0,
            "considered-with-open-questions": 1,
            "deferred": 2,
            "considered": 3,
        }
        selected.sort(key=lambda r: order[r["status"]])
        counts = {s: sum(r["status"] == s for r in selected) for s in order}

        field = self._observed([state["field"]], captured)[0] if state["field"] else None
        if field is not None and field["sha256"] is None:
            raise ValueError("wider field is unavailable; historical feedback remains inspectable")

        text, delivered, omitted, unselected = self._render_feedback_body(
            ident,
            active,
            selected,
            selected_ids,
            counts,
            field,
            limit,
            max_bytes,
        )
        encoded_text = text.encode("utf-8")
        body = {
            "version": 1,
            "id": ident,
            "created_at": now(),
            "sequence": state["sequence"],
            "field": field,
            "delivered": delivered,
            "omitted": omitted,
            "outside_view": unselected,
            "counts": counts,
            "counts_scope": "selected-view",
            "active": len(active),
            "selected": len(selected),
            "context_sha256": self.ex.put(encoded_text),
            "context_bytes": len(encoded_text),
            "boundary": BOUNDARY,
        }
        key = self._put(body)
        state["feedbacks"][ident] = key
        self._save(state)
        return body | {"feedback_object": key, "context": text}

    def record(self, feedback: str, receipt: str, producer: str) -> dict[str, Any]:
        state = self._load()
        snapshot = self._get(state["feedbacks"][_id(feedback)])
        supplied, source = self._read_input(receipt)

        if set(supplied) != {"id", "assessments"}:
            raise ValueError("receipt must contain id and assessments")

        ident = _id(supplied["id"])
        current = self._current(state)
        if ident in current["receipt_ids"]:
            raise ValueError("receipt identity already used")

        _text(producer, "producer", 1000)
        selected = {r["id"]: r for r in snapshot["delivered"]}
        assessments = supplied["assessments"]
        if not isinstance(assessments, list) or not assessments or len(assessments) > MAX_ITEMS:
            raise ValueError("nonempty bounded assessments required")

        seen: set[str] = set()
        made = []
        for assessment in assessments:
            if not isinstance(assessment, dict) or set(assessment) - {
                "item",
                "disposition",
                "account",
                "remaining",
                "products",
            }:
                raise ValueError("invalid assessment fields")

            item = assessment["item"]
            if item not in selected or item in seen:
                raise ValueError("each assessment must name a distinct actually delivered item")
            seen.add(item)

            if assessment.get("disposition") not in DISPOSITIONS:
                raise ValueError("disposition must be considered or deferred")

            account = _text(assessment["account"], "public account")
            remaining = _strings(assessment.get("remaining", []), "remaining")
            for question in remaining:
                _text(question, "remaining question")

            products = [
                self.ex.snapshot(p) for p in _strings(assessment.get("products", []), "products")
            ]
            row = selected[item]
            made.append(
                {
                    "item": item,
                    "basis_key": row["basis_key"],
                    "spec": row["spec"],
                    "basis": row["basis"],
                    "disposition": assessment["disposition"],
                    "account": account,
                    "remaining": remaining,
                    "products": products,
                }
            )

        body = {
            "version": 1,
            "id": ident,
            "created_at": now(),
            "feedback": feedback,
            "feedback_object": state["feedbacks"][feedback],
            "producer": producer,
            "source": source,
            "assessments": made,
            "boundary": BOUNDARY,
        }
        key = self._put(body)
        state["receipts"].append(key)
        self._index_receipt(current, len(state["receipts"]) - 1, body)
        self._seal_current(state, current)
        self._save(state)
        return {"receipt": ident, "object": key, "recorded": list(seen), "boundary": BOUNDARY}

    def inspect_receipt(self, rid: str) -> dict[str, Any] | None:
        """Return retained receipt record and binding metadata, or None if unrecorded."""
        ident = _id(rid)
        state = self._load()
        current = self._current(state)
        pos = current["receipt_ids"].get(ident)
        if pos is None:
            return None

        key = state["receipts"][pos]
        body = self._get(key)
        return {
            "id": ident,
            "object": key,
            "record": body,
            "feedback_object": state["feedbacks"].get(body["feedback"]),
        }

    def inspect(self, feedback: str) -> dict[str, Any]:
        state = self._load()
        body = self._get(state["feedbacks"][_id(feedback)])
        self.ex.get(body["context_sha256"])

        for row in body["delivered"]:
            for binding in row["basis"]:
                if binding["sha256"] is not None:
                    self.ex.get(binding["sha256"])

        return body | {"context": self.ex.get(body["context_sha256"]).decode("utf-8")}

    def object(self, object_id: str) -> bytes:
        return self.ex.get(object_id)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--root", required=True, type=Path, help="existing external exchange root")
    parser.add_argument("--admission-deadline", type=float, help=argparse.SUPPRESS)
    sub = parser.add_subparsers(dest="action", required=True)

    tr = sub.add_parser("track", allow_abbrev=False)
    tr.add_argument("--spec", required=True)

    fe = sub.add_parser("feedback", allow_abbrev=False)
    fe.add_argument("feedback")
    fe.add_argument("--group", dest="groups", action="append", default=[])
    fe.add_argument("--item", dest="items", action="append", default=[])
    fe.add_argument("--limit", type=int, default=8)
    fe.add_argument("--max-bytes", type=int, default=16000)
    fe.add_argument("--text", action="store_true")

    rec = sub.add_parser("record", allow_abbrev=False)
    rec.add_argument("feedback")
    rec.add_argument("--receipt", required=True)
    rec.add_argument("--producer", required=True)

    ins = sub.add_parser("inspect", allow_abbrev=False)
    ins.add_argument("feedback")
    ins.add_argument("--text", action="store_true")

    obj = sub.add_parser("object", allow_abbrev=False)
    obj.add_argument("object_id")
    obj.add_argument("--text", action="store_true")

    return parser


def main() -> None:
    args = vars(_build_parser().parse_args())
    root = args.pop("root")
    action = args.pop("action")
    text = args.pop("text", False)
    deadline = admission_deadline(args.pop("admission_deadline", None))
    read_only = action in {"inspect", "object"}
    guard = shared if read_only else exclusive

    try:
        resolved = external_root(root)
        if not (resolved / "workspace.json").is_file():
            raise ValueError("initialize the external exchange first")

        lock_path = resolved / "owner.lock"
        with owner_admission(
            lambda guards: guards.enter_context(guard(lock_path, allow_readonly_open=read_only)),
            deadline=deadline,
        ):
            ex = Exchange(resolved)
            service = Attention(ex)
            result = getattr(service, action)(**args)

        if isinstance(result, bytes):
            if not text:
                raise ValueError(
                    "object reading requires --text; "
                    "use the existing content store for binary bytes"
                )
            sys.stdout.write(result.decode("utf-8"))
        elif text:
            sys.stdout.write(result["context"])
        else:
            print(json.dumps(result, ensure_ascii=False, indent=2))
    except AdmissionContended as exc:
        print(json.dumps(exc.result(action)), file=sys.stderr)
        raise SystemExit(75) from None
    except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
        print(json.dumps({"error": str(exc), "operation": action}), file=sys.stderr)
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
