"""Compare and receive one authored composition under its native owner guard."""

from collections.abc import Callable, Sequence
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from managed_ai_information.core.formats import (
    RECOMPOSITION_FORMAT,
    RECOMPOSITION_VIEW_FORMAT,
    TRANSFER_FORMAT,
)
from managed_ai_information.core.ownership import NativeLayout
from managed_ai_information.core.storage import sha

from ..protocol.identities import (
    exact_fields,
    identity,
    nonempty,
    record_identity,
    require,
    strict_json,
)
from ..query.discovery_report import canonical
from ..query.discovery_report import request as discovery_request
from ..query.information import pointer
from .composition import Composition
from .revision_projection import (
    BasisAuthority,
    EvidenceHeadMap,
    TransferMap,
    changes,
    inspect_consumers,
    project_edit,
    read_capture,
    row_map,
    row_use,
)


def read_evidence(
    system: Any,
    req: dict[str, Any],
    row_sources: list[Any],
    external_reader: Callable[[list[dict[str, Any]], dict[str, Any]], tuple[int, int]] | None,
    selected_basis: list[BasisAuthority] | None = None,
    transfers: TransferMap | None = None,
    *,
    evidence_heads: EvidenceHeadMap | None = None,
) -> dict[str, Any]:
    selected = req["sources"]
    require(isinstance(selected, list) and bool(selected), "select evidence for the decision")

    allowed = {canonical(pin) for pin in selected}
    require(
        all(canonical(pin) in allowed for pin in row_sources),
        "affected current use requires a source outside the selected read set",
    )

    exact_fields(req["bounds"], {"scan_limit", "max_bytes"}, set(), "read bounds")
    bounds = discovery_request(dict(req["bounds"], limit=1), False)
    pins = [strict_json(key) for key in sorted(allowed)]
    external = [pin for pin in pins if "owner" in pin]

    files = external_bytes = 0
    if external_reader is not None:
        files, external_bytes = external_reader(external, bounds)
    else:
        require(not external, "external evidence requires its domain reader")

    heads: EvidenceHeadMap = {}
    native_bytes = 0
    for pin in pins:
        if "owner" in pin:
            continue

        exact_fields(
            pin, {"instance_id", "id", "revision", "sha256", "pointer"}, set(), "native evidence"
        )
        require(pin["instance_id"] == system.instance_id, "native evidence owner differs")
        wanted = identity({k: pin[k] for k in ("id", "revision", "sha256")})

        if pin["id"] not in heads:
            require(files + len(heads) < bounds["scan_limit"], "evidence scan limit")
            record, raw = system.record_store.read(
                pin["id"],
                byte_limit=bounds["max_bytes"] - external_bytes - native_bytes,
            )
            heads[pin["id"]] = (record, len(raw))
            native_bytes += len(raw)

        record, _ = heads[pin["id"]]
        require(
            wanted == record_identity(record),
            "native evidence head revision conflict: " + pin["id"],
        )
        require(isinstance(pin["pointer"], str), "expected native evidence pointer")
        value = pointer(record, pin["pointer"])

        if (
            transfers is not None
            and isinstance(value, dict)
            and value.get("format") == TRANSFER_FORMAT
        ):
            source = value.get("source")
            if isinstance(source, dict) and (
                source.get("instance_id"),
                source.get("record_id"),
            ) == (
                system.instance_id,
                req["target"]["id"],
            ):
                from managed_ai_information.store.records.record_transfer import unpack_transfer

                _, edition, _ = unpack_transfer(value, system.record_store)
                transfers.setdefault(
                    (edition["revision"], edition["sha256"]),
                    (pin, value["object_sha256"], edition),
                )

        if (
            selected_basis is not None
            and pin["pointer"].split("/")[:-1] == ["", "basis"]
            and value["path"] == system.record_store.relative_name(req["target"]["id"])
        ):
            selected_basis.append((dict(wanted, pointer=pin["pointer"]), value))

    if evidence_heads is not None:
        ids = req["consumer_scope"]
        if isinstance(ids, list) and len(ids) <= bounds["scan_limit"]:
            evidence_heads.update((i, heads[i]) for i in ids if isinstance(i, str) and i in heads)

    return {
        "sources": pins,
        "external_files": files,
        "native_heads": len(heads),
        "external_bytes": external_bytes,
        "native_bytes": native_bytes,
    }


def revise(
    system: Any,
    req: dict[str, Any],
    *,
    accept: str | None = None,
    retain: str | None = None,
    reason: str | None = None,
    external_reader: Callable[[list[dict[str, Any]], dict[str, Any]], tuple[int, int]]
    | None = None,
    request_format: str = RECOMPOSITION_FORMAT,
    view_format: str = RECOMPOSITION_VIEW_FORMAT,
    binding_keys: Sequence[str] = ("composition_use",),
    proof_store: Path | str | None = None,
    proof_routes: Callable[[dict[str, Any]], Any] | None = None,
    proof_store_locked: bool = False,
) -> dict[str, Any]:
    """Caller owns the store guard. A domain reader qualifies its external editions."""
    optional = {"order", "focus", "proof_preservation", "consumer_scope"}
    require(
        proof_store is None or "proof_preservation" in req,
        "proof store requires a preservation claim",
    )
    if external_reader is not None:
        optional.add("withheld")

    exact_fields(
        req,
        {"format", "instance_id", "target", "fields", "rows", "sources", "bounds", "judgment"},
        optional,
        "composition proposal",
    )
    require(req["format"] == request_format, "unsupported composition proposal")
    require(not (accept is not None and retain is not None), "select one decision")
    nonempty(req["judgment"], "proposal judgment and alternative")

    descriptor = NativeLayout(system.root).descriptor.read_bytes()
    require(strict_json(descriptor) == system.descriptor, "selected store policy changed")
    require(req["instance_id"] == system.instance_id, "selected instance differs")

    original = system.record_store.head(req["target"])
    staged, old_tables, new_tables = project_edit(original, req)
    system.domain_selection.validate_record(staged, original)
    delta, impact, used, inherited = changes(
        original["content"],
        staged["content"],
        old_tables,
        new_tables,
        req,
        binding_keys,
    )

    new = row_map(new_tables)
    if req.get("focus") is not None:
        require(req["focus"] in new, "requested focus is absent from proposed composition")

    evidence = [pin for ref in used for pin in row_use(new[ref], binding_keys).get("sources", [])]
    selected_basis: list[BasisAuthority] | None
    transfers: TransferMap | None
    evidence_heads: EvidenceHeadMap | None
    if "consumer_scope" in req:
        selected_basis, transfers, evidence_heads = [], {}, {}
    else:
        selected_basis, transfers, evidence_heads = None, None, None
    read_set = read_evidence(
        system,
        req,
        evidence,
        external_reader,
        selected_basis,
        transfers,
        evidence_heads=evidence_heads,
    )

    view = Composition(SimpleNamespace(ex=system.ex, show=lambda _: staged)).inspect(
        original["id"],
        focus=req.get("focus"),
        limit=10,
        pair_limit=0,
    )
    view.pop("record")
    require(
        all(
            b["status"] == "current" or (b["status"] == "missing" and b["captured_sha256"] is None)
            for b in view["basis"]
        ),
        "captured workspace basis changed or unavailable",
    )

    review: dict[str, Any] = {
        "format": view_format,
        "target": req["target"],
        "instance_id": system.instance_id,
        "descriptor_sha256": sha(descriptor),
        "request_sha256": sha(canonical(req)),
        "content_sha256": sha(canonical(staged["content"])),
        "judgment": req["judgment"],
        "diff": delta,
        "affected": impact,
        "previous_inputs": inherited,
        "current_inputs": used,
        "read_set": read_set,
        "projection": view,
    }

    remaining_heads = (
        req["bounds"]["scan_limit"] - read_set["external_files"] - read_set["native_heads"]
    )
    remaining_bytes = (
        req["bounds"]["max_bytes"] - read_set["external_bytes"] - read_set["native_bytes"]
    )
    if "consumer_scope" in req:
        projected = staged if any(delta.values()) else original
        scope = inspect_consumers(
            system,
            req["consumer_scope"],
            original,
            projected,
            new_tables,
            binding_keys,
            remaining_heads,
            remaining_bytes,
            selected_basis or (),
            transfers,
            evidence_heads=evidence_heads,
        )
        del evidence_heads
        review["consumer_scope"] = scope
        remaining_heads -= len(scope["examined"]) + len(scope.get("retained_objects", ()))
        remaining_bytes -= scope["bytes"]

    with ExitStack() as stack:
        bound: Any = None
        if "proof_preservation" in req:
            from managed_ai_information.store.composition.composition_opportunities import (
                BoundOpportunities,
                guarded_inventory,
            )

            try:
                before = original["content"]["content"]
                after = staged["content"]["content"]
                binding = before["proof_use"]
                require(binding == after["proof_use"], "proposed proof authority differs")
                comparable = dict(staged["content"], content=dict(after, tree=before["tree"]))
                require(
                    canonical(original["content"]) == canonical(comparable),
                    "changed receiving contract outside the bound tree",
                )
                require(remaining_heads > 0, "inventory exceeds evidence scan limit")

                owner = stack.enter_context(
                    guarded_inventory(
                        system, binding, proof_store, already_locked=proof_store_locked
                    )
                )
                bound = BoundOpportunities(
                    system,
                    owner,
                    binding,
                    req["proof_preservation"],
                    original,
                    staged,
                    remaining_bytes,
                    proof_routes,
                )
                review["proof_preservation"] = bound.observe()
            except (KeyError, TypeError, ValueError, OSError) as exc:
                review["proof_preservation"] = {
                    "outcome": "unresolved",
                    "preserves": False,
                    "coverage": {"complete": False, "unknowns": [str(exc)]},
                }

        def current_read_set() -> dict[str, Any]:
            current = read_evidence(system, req, evidence, external_reader)
            for item in review.get("consumer_scope", {}).get("retained_objects", ()):
                basis = {
                    "path": system.record_store.relative_name(original["id"]),
                    "bytes": item["bytes"],
                    "sha256": item["sha256"],
                }
                observed, _ = read_capture(system.record_store, original["id"], basis)
                require(observed == item, "retained composition evidence changed at publication")
            return current

        return receive(
            system,
            review,
            original,
            staged,
            delta,
            bound,
            current_read_set,
            accept,
            retain,
            reason,
        )


def receive(
    system: Any,
    review: dict[str, Any],
    original: dict[str, Any],
    staged: dict[str, Any],
    delta: dict[str, Any],
    bound: Any,
    current_read_set: Callable[[], dict[str, Any]],
    accept: str | None,
    retain: str | None,
    reason: str | None,
) -> dict[str, Any]:
    certificate = canonical(review)
    basis = sha(certificate)
    review.update(review_sha256=basis, status="proposed")

    if accept is not None or retain is not None:
        require(
            (accept if accept is not None else retain) == basis,
            "reviewed composition or read set differs",
        )
        review["decision_reason"] = nonempty(reason, "owner decision reason")
        proof = review.get("proof_preservation")

        def verify_at_use() -> None:
            require(
                current_read_set() == review["read_set"], "selected evidence changed at publication"
            )
            if proof is not None:
                if bound is None or canonical(bound.observe()) != canonical(proof):
                    raise ValueError("proof inventory or receiving contract changed at publication")
                require(
                    proof["preserves"],
                    "current complete proof preservation required: " + proof["outcome"],
                )
                bound.retain_inputs()
                review["certificate_object"] = system.ex.objects.put(certificate)

        observed_objects = review.get("consumer_scope", {}).get("retained_objects")
        verify = (
            verify_at_use
            if proof is not None or review["read_set"]["external_files"] or observed_objects
            else None
        )

        if retain is not None or not any(delta.values()):
            if accept is not None and verify is not None:
                verify()
            elif proof is not None:
                if bound is not None:
                    bound.retain_inputs()
                review["certificate_object"] = system.ex.objects.put(certificate)
            review.update(status="retained", head=record_identity(original))
        else:
            record: dict[str, Any] = {
                k: staged[k] for k in ("id", "kind", "active", "relations", "content")
            }
            record["basis"] = [b["path"] for b in original["basis"]]
            written = system.record_store.put_reviewed(
                record, original["revision"], original["basis"], verify=verify
            )
            installed = system.record_store.head(record_identity(written))
            require(
                canonical(installed["content"]) == canonical(staged["content"]),
                "published head differs from reviewed content",
            )
            review.update(status="accepted", head=record_identity(installed))
    else:
        require(reason is None, "supply a reason with an owner decision")

    return review
