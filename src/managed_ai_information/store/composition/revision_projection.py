"""Projection, dependency traversal, and bounded consumer inspection for composition revisions."""

from collections import defaultdict
from collections.abc import Iterable, Sequence
from typing import Any

from ..protocol.identities import exact_fields, identity, record_identity, require
from ..query.discovery_report import canonical
from ..query.information import pointer
from .composition import COLLECTIONS, MAX_VIEW_BYTES, PREFIXES, PROFILE, Composition

COLLECTION = {prefix: name for name, prefix in PREFIXES.items()}
METADATA = {"purpose", "unknowns", "coverage", "content"}

type RowTables = dict[str, dict[str, dict[str, Any]]]
type TransferMap = dict[tuple[int, str], tuple[dict[str, Any], str, dict[str, Any]]]
type BasisAuthority = tuple[dict[str, Any], dict[str, Any]]
type EvidenceHeadMap = dict[str, tuple[dict[str, Any], int]]


def project_edit(
    original: dict[str, Any],
    req: dict[str, Any],
) -> tuple[dict[str, Any], RowTables, RowTables]:
    """Replace named rows in place. Preserve unmentioned rows and their order."""
    body, tables, _, _ = Composition(None).profile(original)
    candidate = dict(body)

    identity_fields = req["fields"]
    exact_fields(identity_fields, set(), METADATA, "changed composition fields")
    candidate.update(identity_fields)

    patches = req["rows"]
    require(isinstance(patches, dict), "rows must map typed references to rows or null")
    edits: defaultdict[str, dict[str, Any]] = defaultdict(dict)

    for ref, value in patches.items():
        prefix, sep, ident = ref.partition(":")
        require(sep and ident and prefix in COLLECTION, "unknown composition reference: " + ref)
        collection = COLLECTION[prefix]
        if value is None:
            require(ident in tables[collection], "cannot remove absent row: " + ref)
        else:
            require(
                isinstance(value, dict) and value.get("id") == ident, "row identity differs: " + ref
            )
        edits[collection][ident] = value

    order = req.get("order", {})
    exact_fields(order, set(), set(COLLECTIONS), "composition order")

    for collection in COLLECTIONS:
        if collection not in edits and collection not in order:
            continue
        table = dict(tables[collection])
        for ident, value in edits[collection].items():
            if value is None:
                del table[ident]
            else:
                table[ident] = value

        ids = order.get(collection, list(table))
        require(
            isinstance(ids, list) and len(ids) == len(table) and set(ids) == set(table),
            "order must name each resulting " + collection + " row once",
        )
        if ids or collection in body:
            candidate[collection] = [table[ident] for ident in ids]

    staged = dict(original, content=candidate)
    _, revised, _, _ = Composition(None).profile(staged)
    return staged, tables, revised


def row_map(tables: RowTables) -> dict[str, dict[str, Any]]:
    return {
        PREFIXES[c] + ":" + name: value for c in COLLECTIONS for name, value in tables[c].items()
    }


def row_use(row: dict[str, Any], keys: Sequence[str]) -> dict[str, list[Any]]:
    """Read only the use annotations belonging to the selected adapter."""
    result: dict[str, list[Any]] = {"uses": [], "sources": []}
    detail = row.get("content")
    if isinstance(detail, dict):
        for key in keys:
            use = detail.get(key, {})
            exact_fields(use, set(), set(result), key)
            for field, values in use.items():
                require(isinstance(values, list), key + " " + field + " must be a list")
                result[field].extend(values)
    return result


def dependencies(
    rows: dict[str, dict[str, Any]], keys: Sequence[str]
) -> defaultdict[Any, set[Any]]:
    """Consumer -> supplied references. Resource nodes avoid expanding all pairs."""
    edges: defaultdict[Any, set[Any]] = defaultdict(set)

    for ref, row in rows.items():
        deps = edges[ref]
        use = row_use(row, keys)
        for source in use.get("uses", []):
            require(
                isinstance(source, str) and source.partition(":")[0] in COLLECTION,
                "row use needs typed composition row references",
            )
            deps.add(source)

        if ref.startswith("relation:"):
            deps.update("element:" + item for k in ("from", "to") for item in row.get(k, []))

        if ref.startswith("opportunity:"):
            deps.update(row.get("at", []))
            deps.update("premise:" + item for group in row.get("requires", []) for item in group)
            for other in row.get("conflicts", []):
                peer = "opportunity:" + other
                deps.add(peer)
                edges[peer].add(ref)
            for name in row.get("writes", []):
                edges[("resource", name)].add(ref)
            deps.update(("resource", name) for k in ("reads", "writes") for name in row.get(k, []))

    return edges


def reach(initial: Iterable[Any], edges: dict[Any, set[Any]]) -> set[Any]:
    reached = set(initial)
    stack = list(reached)
    while stack:
        for ref in edges.get(stack.pop(), ()):
            if ref not in reached:
                reached.add(ref)
                stack.append(ref)
    return reached


def reverse_dependencies(*graphs: dict[Any, set[Any]]) -> defaultdict[Any, set[Any]]:
    consumers: defaultdict[Any, set[Any]] = defaultdict(set)
    for graph in graphs:
        for ref, inputs in graph.items():
            for item in inputs:
                consumers[item].add(ref)
    return consumers


def changes(
    before: dict[str, Any],
    after: dict[str, Any],
    old_tables: RowTables,
    new_tables: RowTables,
    req: dict[str, Any],
    keys: Sequence[str],
) -> tuple[dict[str, Any], list[str], list[str], list[str]]:
    old, new = row_map(old_tables), row_map(new_tables)
    changed = {
        ref: {"before": old.get(ref), "after": new.get(ref)}
        for ref in sorted(old.keys() | new.keys())
        if canonical(old.get(ref)) != canonical(new.get(ref))
    }
    fields = {
        k: {"before": before.get(k), "after": after[k]}
        for k in req["fields"]
        if k not in before or canonical(before[k]) != canonical(after[k])
    }

    order: dict[str, dict[str, list[str]]] = {}
    moved: set[str] = set()
    for c in req.get("order", {}):
        left = list(old_tables[c])
        right = list(new_tables[c])
        if left != right:
            order[c] = {"before": left, "after": right}
            positions = {k: i for i, k in enumerate(left)}
            moved.update(
                PREFIXES[c] + ":" + k for i, k in enumerate(right) if positions.get(k) != i
            )

    old_deps, new_deps = dependencies(old, keys), dependencies(new, keys)

    def missing(
        rows: dict[str, dict[str, Any]],
        edges: dict[Any, set[Any]],
    ) -> set[tuple[str, str]]:
        return {
            (ref, dep)
            for ref in rows
            for dep in edges[ref]
            if isinstance(dep, str) and dep not in rows
        }

    newly_missing = missing(new, new_deps) - missing(old, old_deps)
    require(
        not newly_missing, "new unresolved composition references: " + str(sorted(newly_missing))
    )

    consumers = reverse_dependencies(old_deps, new_deps)
    seeds = old.keys() | new.keys() if fields else set(changed) | moved
    impact = reach(seeds, consumers)
    used = reach(impact & new.keys(), new_deps) & new.keys()
    inherited = reach(impact & old.keys(), old_deps) & old.keys()

    return (
        {"fields": fields, "rows": changed, "order": order},
        sorted(impact & (old.keys() | new.keys())),
        sorted(used),
        sorted(inherited),
    )


def projected_coordinate(
    original: dict[str, Any],
    staged: dict[str, Any],
    positions: dict[str, dict[str, int]],
    path: str,
) -> dict[str, Any]:
    """Compare the selected value, following a declared composition row by its typed ID."""
    try:
        before = pointer(original, path)
    except ValueError:
        return {"status": "unresolved-incumbent-coordinate"}

    if path != "/content" and not path.startswith("/content/"):
        if original is staged:
            return {"status": "unchanged", "proposed_pointer": path}
        return {"status": "unprojected-envelope"}

    parts = path.split("/")
    proposed = path
    row: dict[str, Any] = {}
    if len(parts) >= 4 and parts[2] in COLLECTIONS:
        ident = original["content"][parts[2]][int(parts[3])]["id"]
        row = {"source_row": PREFIXES[parts[2]] + ":" + ident}
        index = positions[parts[2]].get(ident)
        if index is None:
            return dict(row, status="removed")
        parts[3] = str(index)
        proposed = "/".join(parts)

    try:
        after = pointer(staged, proposed)
    except ValueError:
        return dict(row, status="removed")

    changed = canonical(before) != canonical(after)
    status = "changed" if changed else "unchanged"
    if path != proposed:
        status = "relocated-and-changed" if changed else "relocated"
    return dict(row, proposed_pointer=proposed, status=status)


def read_capture(
    store: Any,
    ident: str,
    basis: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Report the native object actually consulted, including a failed capture."""
    observed: dict[str, Any] = {"sha256": basis["sha256"], "bytes": basis["bytes"]}
    try:
        record = store.captured_record(ident, basis)
    except FileNotFoundError:
        observed["reason"] = "captured record object is missing"
    except OSError, ValueError, KeyError:
        observed["reason"] = "captured record object failed verification"
    else:
        observed["record"] = record_identity(record)
        return observed, record
    return observed, None


class _ConsumerInspector:
    """Stateful bounded inspector for same-instance consumer references during recomposition."""

    def __init__(
        self,
        system: Any,
        ids: list[str],
        original: dict[str, Any],
        staged: dict[str, Any],
        new_tables: RowTables,
        keys: Sequence[str],
        scan_limit: int,
        max_bytes: int,
        selected_basis: Sequence[BasisAuthority],
        transfers: TransferMap | None,
    ) -> None:
        self.system = system
        self.ids = ids
        self.original = original
        self.staged = staged
        self.keys = keys
        self.scan_limit = scan_limit
        self.max_bytes = max_bytes
        self.selected_basis = list(selected_basis)
        self.transfers: TransferMap = transfers or {}
        self.positions = {
            c: {ident: i for i, ident in enumerate(new_tables[c])} for c in COLLECTIONS
        }
        self.incumbent = record_identity(original)
        self.material = system.record_store.relative_name(self.incumbent["id"])
        self.coordinates: dict[Any, dict[str, Any]] = {}
        self.objects: dict[str, dict[str, Any]] = {}
        self.editions: dict[str, dict[str, Any] | None] = {}
        self.validated: set[str] = set()
        self.used_bytes = 0
        self.view_bytes = len(canonical({"examined": [], "bytes": max_bytes, "candidates": []}))

    def charge(self, fragment: Any) -> None:
        self.view_bytes += len(canonical(fragment)) + 1
        require(
            self.view_bytes <= MAX_VIEW_BYTES, "consumer scope exceeds composition view byte bound"
        )

    def _project_retained(
        self,
        record: dict[str, Any],
        key: str,
        path: str,
    ) -> dict[str, Any]:
        coord = (key, path)
        if coord not in self.coordinates:
            if key not in self.validated:
                try:
                    Composition(None).profile(record)
                except ValueError:
                    return {
                        "status": "unresolved-edition",
                        "reason": "captured edition has no valid typed composition",
                    }
                self.validated.add(key)
            self.coordinates[coord] = projected_coordinate(
                record, self.staged, self.positions, path
            )
        return self.coordinates[coord]

    def _project_historical(
        self,
        captured: dict[str, Any],
        authorities: Sequence[BasisAuthority],
        path: str,
    ) -> dict[str, Any]:
        reason = "no selected basis or native transfer supplies this local record edition"
        if captured["revision"] >= self.incumbent["revision"]:
            return {
                "status": "unresolved-edition",
                "reason": "pin conflicts with the current revision",
            }

        transfer = self.transfers.get((captured["revision"], captured["sha256"]))
        if transfer:
            authority, key, record = transfer
            return dict(
                self._project_retained(record, key, path),
                transfer_source=authority,
                object_sha256=key,
            )

        for authority, basis in authorities:
            key, size = basis["sha256"], basis["bytes"]
            if key is None:
                reason = "source path was absent at capture"
                continue

            if key not in self.objects:
                require(
                    len(self.ids) + len(self.objects) < self.scan_limit,
                    "retained source exceeds evidence scan limit",
                )
                require(
                    type(size) is int and 0 <= size <= self.max_bytes - self.used_bytes,
                    "retained source exceeds evidence byte bound",
                )
                self.used_bytes += size
                item, self.editions[key] = read_capture(
                    self.system.record_store, self.incumbent["id"], basis
                )
                self.charge(item)
                self.objects[key] = item

            item = self.objects[key]
            require(item["bytes"] == size, "conflicting lengths for retained source object")
            edition = self.editions[key]
            if item.get("record") != captured or edition is None:
                reason = item.get("reason", "captured basis holds a different record edition")
                continue
            return dict(
                self._project_retained(edition, key, path),
                basis_source=authority,
                object_sha256=key,
            )

        return {"status": "unresolved-edition", "reason": reason}

    def inspect_record(
        self,
        ident: str,
        evidence_heads: EvidenceHeadMap | None,
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        remaining = self.max_bytes - self.used_bytes
        if evidence_heads and ident in evidence_heads:
            record, size = evidence_heads[ident]
            if size > remaining:
                raise ValueError(
                    f"source exceeds byte bound: {self.system.record_store.path(ident)}"
                )
        else:
            record, raw = self.system.record_store.read(ident, byte_limit=remaining)
            size = len(raw)

        self.used_bytes += size
        head = record_identity(record)
        self.charge(head)

        body = record.get("content")
        if not isinstance(body, dict) or body.get("format") != PROFILE:
            return head, None

        _, tables, _, _ = Composition(None).profile(record)
        rows = row_map(tables)
        own_basis = [
            (dict(head, pointer="/basis/" + str(i)), basis)
            for i, basis in enumerate(record["basis"])
            if basis["path"] == self.material
        ]
        authorities = own_basis + self.selected_basis
        bindings: list[dict[str, Any]] = []
        seeds: set[str] = set()

        for ref, row in rows.items():
            for pin in row_use(row, self.keys)["sources"]:
                if (
                    not isinstance(pin, dict)
                    or "owner" in pin
                    or pin.get("id") != self.original["id"]
                    or pin.get("instance_id") != self.system.instance_id
                ):
                    continue

                exact_fields(
                    pin,
                    {"instance_id", "id", "revision", "sha256", "pointer"},
                    set(),
                    "consumer native evidence",
                )
                captured = identity(record_identity(pin))
                path = pin["pointer"]
                require(isinstance(path, str), "expected native evidence pointer")

                if captured != self.incumbent:
                    result = self._project_historical(captured, authorities, path)
                else:
                    if path not in self.coordinates:
                        self.coordinates[path] = projected_coordinate(
                            self.original, self.staged, self.positions, path
                        )
                    result = self.coordinates[path]

                binding = dict(result, consumer_row=ref, captured=pin)
                self.charge(binding)
                bindings.append(binding)
                if result["status"] != "unchanged":
                    seeds.add(ref)

        if not bindings:
            return head, None

        affected = (
            reach(seeds, reverse_dependencies(dependencies(rows, self.keys))) & rows.keys()
            if seeds
            else set()
        )
        candidate: dict[str, Any] = {"target": head, "bindings": [], "affected": sorted(affected)}
        self.charge(candidate)
        candidate["bindings"] = bindings
        return head, candidate


def inspect_consumers(
    system: Any,
    ids: Any,
    original: dict[str, Any],
    staged: dict[str, Any],
    new_tables: RowTables,
    keys: Sequence[str],
    scan_limit: int,
    max_bytes: int,
    selected_basis: Sequence[BasisAuthority] = (),
    transfers: TransferMap | None = None,
    *,
    evidence_heads: EvidenceHeadMap | None = None,
) -> dict[str, Any]:
    """Locate authored same-instance row uses within the explicitly supplied records."""
    require(
        isinstance(ids, list)
        and bool(ids)
        and all(isinstance(i, str) and bool(i) for i in ids)
        and len(ids) == len(set(ids)),
        "consumer_scope needs distinct record IDs",
    )
    require(len(ids) <= scan_limit, "consumer scope exceeds evidence scan limit")

    inspector = _ConsumerInspector(
        system,
        ids,
        original,
        staged,
        new_tables,
        keys,
        scan_limit,
        max_bytes,
        selected_basis,
        transfers,
    )
    checked: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []

    for ident in ids:
        head, candidate = inspector.inspect_record(ident, evidence_heads)
        checked.append(head)
        if candidate is not None:
            candidates.append(candidate)

    scope: dict[str, Any] = {
        "examined": checked,
        "bytes": inspector.used_bytes,
        "candidates": candidates,
    }
    if inspector.objects:
        scope["retained_objects"] = list(inspector.objects.values())
        require(
            len(canonical(scope)) <= MAX_VIEW_BYTES,
            "consumer scope exceeds composition view byte bound",
        )
    return scope
