"""Guarded receive, crash/partial resume, supersession handoff, and status projection."""

import stat
from pathlib import Path
from typing import Any

from managed_ai_information.host.continuing_work.occurrence_files import (
    atomic_bytes,
    check_unfollowed_ancestors,
    digest,
    ident,
    nonempty,
    now,
    read,
)


def record_partial_publication(
    host: Any,
    path: Path,
    scope: dict[str, Any],
    *,
    producer: str,
    account: str,
    returned: dict[str, Any],
    publications: list[dict[str, Any]],
    published_outputs: list[dict[str, Any]],
    pending_targets: list[str],
    state: str,
    failed_target: str | None = None,
    error: str | None = None,
) -> None:
    """Persist partial multi-target publication progress so resume can settle or complete
    cleanly.
    """
    entry: dict[str, Any] = {
        "at_utc": now(),
        "producer": producer,
        "account": account,
        "return": returned,
        "publications": list(publications),
        "outputs": list(published_outputs),
        "pending_targets": list(pending_targets),
        "state": state,
    }
    if failed_target is not None:
        entry["failed_target"] = failed_target
    if error is not None:
        entry["error"] = error

    scope["partial_publication"] = entry
    host._write(path, scope)

    if state != "publishing":
        event_payload: dict[str, Any] = {
            "id": scope["id"],
            "scope": host.pin(path),
            "published_targets": [pub["path"] for pub in publications],
            "pending_targets": list(pending_targets),
        }
        if failed_target is not None:
            event_payload["failed_target"] = failed_target
        if error is not None:
            event_payload["error"] = error
        host.event("partial-publication", event_payload)


def _ordinary_file(path: Path | str, label: str) -> Path:
    checked = check_unfollowed_ancestors(path, label, include_target=True)
    if not checked.exists() or not stat.S_ISREG(checked.lstat().st_mode):
        raise ValueError(f"{label} must be an existing ordinary file")
    return checked.resolve()


def _validate_receive_inputs(
    host: Any,
    sid: str,
    scope: dict[str, Any],
    spec: dict[str, Any],
) -> tuple[str, str, list[str]]:
    if "superseded" in scope:
        raise ValueError("This preparation was superseded; use its actual successor")
    if "result" in scope:
        raise ValueError(
            "Retain the completed occurrence; use native recovery for uncertain transport"
        )
    if "supersession_request" in scope:
        successor = scope["supersession_request"]["successor"]
        raise ValueError(
            f"{sid}: inspect the outstanding handoff to {successor} "
            "before receiving another return",
        )

    goal = host.goal()
    if goal["sha256"] != scope["parent_goal"]["sha256"]:
        raise ValueError(
            "Parent commission/duties changed; retain the proposal and prepare a fresh receiving "
            "occasion",
        )

    host.current(host.config["commission"], "Current commission")
    for source in scope["sources"]:
        host.current(source, "Selected current source " + source["id"])

    proposals = spec["proposals"]
    expected = {Path(target).name: target for target in scope["targets"]}
    if set(proposals) != set(expected):
        raise ValueError("Receive exactly the selected output names")

    account = nonempty(spec["account"], "receiving account")
    producer = nonempty(spec["producer"], "producer")
    reply_path = _ordinary_file(spec["reply"], "Receive reply")
    args = ["receive", sid, "--reply", str(reply_path), "--producer", producer]
    for name, source in proposals.items():
        prop_path = _ordinary_file(source, f"Proposal {name}")
        args += ["--proposal", expected[name] + "=" + str(prop_path)]
    if spec.get("assessment"):
        args += ["--assessment", str(_ordinary_file(spec["assessment"], "Assessment file"))]
    return account, producer, args


def _adopt_receive_targets(
    host: Any,
    path: Path,
    scope: dict[str, Any],
    *,
    sid: str,
    producer: str,
    account: str,
    returned: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    publications: list[dict[str, Any]] = []
    published_outputs: list[dict[str, Any]] = []

    for idx, target in enumerate(scope["targets"]):
        try:
            publication = host.call("adopt", sid, "--target", target, "--reason", account)[
                "publication"
            ]
        except Exception as exc:
            if publications:
                record_partial_publication(
                    host,
                    path,
                    scope,
                    producer=producer,
                    account=account,
                    returned=returned,
                    publications=publications,
                    published_outputs=published_outputs,
                    pending_targets=scope["targets"][idx:],
                    state="partial-publication-needs-reconciliation",
                    failed_target=target,
                    error=str(exc).strip(),
                )
            raise

        publications.append(publication)
        published_outputs.append(host.pin(host.work / target))
        if idx + 1 < len(scope["targets"]):
            record_partial_publication(
                host,
                path,
                scope,
                producer=producer,
                account=account,
                returned=returned,
                publications=publications,
                published_outputs=published_outputs,
                pending_targets=scope["targets"][idx + 1 :],
                state="publishing",
            )

    return publications, published_outputs


def receive_scope(host: Any, sid: str, spec: dict[str, Any]) -> dict[str, Any]:
    """Receive candidate proposals and adopt verified target publications."""
    sid = ident(sid)
    path = host.root / "scopes" / f"{sid}.json"
    scope = read(path)
    account, producer, args = _validate_receive_inputs(host, sid, scope, spec)

    result = host.call(*args)
    scope["transport"] = {"at_utc": now(), "return": result["return"], "account": account}
    host._write(path, scope)
    host.event("native-return-retained", {"id": sid, "scope": host.pin(path)})

    publications, published_outputs = _adopt_receive_targets(
        host,
        path,
        scope,
        sid=sid,
        producer=producer,
        account=account,
        returned=result["return"],
    )

    scope.pop("partial_publication", None)
    scope["result"] = {
        "at_utc": now(),
        "producer": producer,
        "return": result["return"],
        "publications": publications,
        "outputs": published_outputs,
        "account": account,
    }
    host._write(path, scope)
    host.event(
        "received",
        {
            "id": sid,
            "return_sha256": result["return"]["return_sha256"],
            "scope": host.pin(path),
            "parent_duties_closed": False,
        },
    )
    return {"id": sid, "outputs": scope["result"]["outputs"], "parent_duties_closed": False}


def _settle_partial_return(
    host: Any,
    path: Path,
    scope: dict[str, Any],
    *,
    sid: str,
    returned: dict[str, Any],
    account: str,
    completed_pubs: list[dict[str, Any]],
    completed_outputs: list[dict[str, Any]],
    remaining_targets: list[str],
    reconciled_captures: list[dict[str, Any]],
) -> dict[str, Any]:
    scope.pop("partial_publication", None)
    scope["result"] = {
        "at_utc": now(),
        "producer": returned["producer"],
        "return": returned,
        "publications": completed_pubs,
        "outputs": completed_outputs,
        "unpublished_targets": remaining_targets,
        "partial": True,
        "account": account,
        "recovered_from_native": True,
    }
    host._write(path, scope)
    host.event(
        "resumed-partial-return",
        {
            "id": sid,
            "scope": host.pin(path),
            "published_targets": [pub["path"] for pub in completed_pubs],
            "unpublished_targets": remaining_targets,
            "new_receive_performed": False,
            "previous_publications_replayed": False,
        },
    )
    res: dict[str, Any] = {
        "id": sid,
        "outputs": scope["result"]["outputs"],
        "published_targets": [pub["path"] for pub in completed_pubs],
        "unpublished_targets": remaining_targets,
        "partial": True,
        "recovered": True,
        "parent_duties_closed": False,
    }
    if reconciled_captures:
        res["reconciled_captures"] = reconciled_captures
    return res


def _classify_native_publications(
    host: Any,
    sid: str,
    scope: dict[str, Any],
    observed: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    known = {
        pub["record"]["path"]: pub
        for pub in observed["publications"]
        if isinstance(pub.get("record"), dict)
    }
    completed_pubs: list[dict[str, Any]] = []
    completed_outputs: list[dict[str, Any]] = []
    remaining_targets: list[str] = []

    for target in scope["targets"]:
        if target in known:
            row = known[target]
            if (
                row.get("observation_error")
                or row["record"].get("phase") != "complete"
                or not row.get("current_target", {}).get("matches_proposal")
            ):
                raise ValueError(
                    f"{sid}: publication of {target} has an unresolved outcome; "
                    "inspect and reconcile the original native publication before continuing",
                )
            completed_pubs.append(row["record"])
            completed_outputs.append(host.pin(host.work / target))
        else:
            remaining_targets.append(target)

    return completed_pubs, completed_outputs, remaining_targets


def _verify_resume_basis(
    host: Any,
    path: Path,
    scope: dict[str, Any],
    *,
    sid: str,
    goal: dict[str, Any],
    returned: dict[str, Any],
    account: str,
    completed_pubs: list[dict[str, Any]],
    completed_outputs: list[dict[str, Any]],
    remaining_targets: list[str],
) -> None:
    try:
        if goal["sha256"] != scope["parent_goal"]["sha256"]:
            raise ValueError(
                "Parent duty changed; preserve old return and prepare a new current receiving "
                "scope",
            )
        host.current(host.config["commission"], "Current commission")
        for source in scope["sources"]:
            host.current(source, "Selected current source " + source["id"])
    except ValueError as exc:
        if completed_pubs and remaining_targets:
            detail = str(exc).strip()
            record_partial_publication(
                host,
                path,
                scope,
                producer=returned["producer"],
                account=account,
                returned=returned,
                publications=completed_pubs,
                published_outputs=completed_outputs,
                pending_targets=remaining_targets,
                state="partial-publication-needs-reconciliation",
                error=detail,
            )
            raise ValueError(
                f"{sid}: partial publication recorded ({len(completed_pubs)} published, "
                f"{len(remaining_targets)} pending); {detail}. "
                "Inspect and settle with settle_partial=true or prepare a fresh receiving scope",
            ) from exc
        raise


def _adopt_remaining_targets(
    host: Any,
    path: Path,
    scope: dict[str, Any],
    *,
    sid: str,
    returned: dict[str, Any],
    account: str,
    completed_pubs: list[dict[str, Any]],
    completed_outputs: list[dict[str, Any]],
    remaining_targets: list[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    pubs_by_target = {pub["path"]: pub for pub in completed_pubs}
    outs_by_target = {
        pub["path"]: out for pub, out in zip(completed_pubs, completed_outputs, strict=True)
    }

    for idx, target in enumerate(remaining_targets):
        target_parent = check_unfollowed_ancestors(
            (host.work / target).parent, "Target directory", include_target=True
        )
        target_parent.mkdir(parents=True, exist_ok=True)
        try:
            pub = host.call("adopt", sid, "--target", target, "--reason", account)["publication"]
        except Exception as exc:
            if pubs_by_target:
                detail = str(exc).strip()
                ordered_pubs = [pubs_by_target[t] for t in scope["targets"] if t in pubs_by_target]
                ordered_outs = [outs_by_target[t] for t in scope["targets"] if t in outs_by_target]
                pending_targets = list(remaining_targets[idx:])
                record_partial_publication(
                    host,
                    path,
                    scope,
                    producer=returned["producer"],
                    account=account,
                    returned=returned,
                    publications=ordered_pubs,
                    published_outputs=ordered_outs,
                    pending_targets=pending_targets,
                    state="partial-publication-needs-reconciliation",
                    failed_target=target,
                    error=detail,
                )
                raise ValueError(
                    f"{sid}: partial publication recorded ({len(ordered_pubs)} published, "
                    f"{len(pending_targets)} pending) after {target} failed: {detail}; "
                    "inspect and settle with settle_partial=true or prepare a fresh receiving "
                    "scope",
                ) from exc
            raise

        pubs_by_target[target] = pub
        outs_by_target[target] = host.pin(host.work / target)
        if idx + 1 < len(remaining_targets):
            ordered_pubs = [pubs_by_target[t] for t in scope["targets"] if t in pubs_by_target]
            ordered_outs = [outs_by_target[t] for t in scope["targets"] if t in outs_by_target]
            record_partial_publication(
                host,
                path,
                scope,
                producer=returned["producer"],
                account=account,
                returned=returned,
                publications=ordered_pubs,
                published_outputs=ordered_outs,
                pending_targets=remaining_targets[idx + 1 :],
                state="publishing",
            )

    return (
        [pubs_by_target[target] for target in scope["targets"]],
        [outs_by_target[target] for target in scope["targets"]],
    )


def resume_scope(host: Any, sid: str, spec: dict[str, Any]) -> dict[str, Any]:
    """Recover an actual retained return without repeating receive or prior publications."""
    sid = ident(sid)
    path = host.root / "scopes" / f"{sid}.json"
    scope = read(path)

    unknown = set(spec) - {"account", "settle_partial"}
    if unknown:
        raise ValueError("resume has unsupported fields: " + ", ".join(sorted(unknown)))
    account = nonempty(spec["account"], "recovery account")
    settle_partial = spec.get("settle_partial", False)
    if type(settle_partial) is not bool:
        raise ValueError("settle_partial must be a boolean")

    if "superseded" in scope:
        return {
            "id": sid,
            "state": "superseded-without-output",
            "successor": scope["superseded"]["successor"],
        }
    if "result" in scope:
        host.received_outputs(scope)
        out: dict[str, Any] = {
            "id": sid,
            "already_received": True,
            "outputs": scope["result"]["outputs"],
        }
        if scope["result"].get("partial"):
            out["partial"] = True
            out["unpublished_targets"] = scope["result"].get("unpublished_targets", [])
        return out

    reconciled_captures = host.bridge._reconcile_pending_captures(sid)
    goal = host.goal()
    observed = host.inspect_native(sid)
    returned = observed.get("return")
    handoff = scope.get("supersession_request")

    if handoff is not None and returned is None:
        raise ValueError(
            f"{sid}: no return confirms the handoff to {handoff['successor']}; "
            "inspect the original packet before retrying supersede",
        )
    if returned is None:
        if reconciled_captures:
            return {
                "id": sid,
                "state": "open-with-reconciled-captures",
                "reconciled_captures": reconciled_captures,
                "parent_duties_closed": False,
            }
        raise ValueError(
            "No native return exists; ordinary receive is still the appropriate operation"
        )

    if handoff is not None:
        if returned["transport"]["kind"] != "local-command" or not returned["proposals"]:
            return finish_supersession(host, path, scope, returned)
        if returned["packet"] != sid or returned["request_sha256"] != handoff["request_sha256"]:
            raise ValueError(f"{sid}: carrier return differs from the prepared handoff occurrence")

    if not returned["proposals"]:
        raise ValueError(
            f"{sid}: no-output return has no recorded successor binding; "
            "retain the original return and reconcile the handoff at its host",
        )

    proposals = {row["path"]: row for row in returned["proposals"]}
    if set(proposals) != set(scope["targets"]):
        raise ValueError("Observed native return differs from selected host targets")

    completed_pubs, completed_outputs, remaining_targets = _classify_native_publications(
        host,
        sid,
        scope,
        observed,
    )
    if settle_partial:
        if not completed_pubs:
            raise ValueError(f"{sid}: no published targets to settle partially")
        if remaining_targets:
            return _settle_partial_return(
                host,
                path,
                scope,
                sid=sid,
                returned=returned,
                account=account,
                completed_pubs=completed_pubs,
                completed_outputs=completed_outputs,
                remaining_targets=remaining_targets,
                reconciled_captures=reconciled_captures,
            )

    _verify_resume_basis(
        host,
        path,
        scope,
        sid=sid,
        goal=goal,
        returned=returned,
        account=account,
        completed_pubs=completed_pubs,
        completed_outputs=completed_outputs,
        remaining_targets=remaining_targets,
    )
    publications, outputs = _adopt_remaining_targets(
        host,
        path,
        scope,
        sid=sid,
        returned=returned,
        account=account,
        completed_pubs=completed_pubs,
        completed_outputs=completed_outputs,
        remaining_targets=remaining_targets,
    )

    scope.pop("partial_publication", None)
    scope["result"] = {
        "at_utc": now(),
        "producer": returned["producer"],
        "return": returned,
        "publications": publications,
        "outputs": outputs,
        "account": account,
        "recovered_from_native": True,
    }
    host._write(path, scope)
    host.event(
        "resumed-return",
        {
            "id": sid,
            "scope": host.pin(path),
            "new_receive_performed": False,
            "previous_publications_replayed": False,
        },
    )
    res: dict[str, Any] = {
        "id": sid,
        "outputs": scope["result"]["outputs"],
        "recovered": True,
        "parent_duties_closed": False,
    }
    if reconciled_captures:
        res["reconciled_captures"] = reconciled_captures
    return res


def finish_supersession(
    host: Any,
    path: Path,
    scope: dict[str, Any],
    returned: dict[str, Any],
) -> dict[str, Any]:
    """Finalize a verified no-output supersession return against its bound successor."""
    sid = scope["id"]
    request = scope["supersession_request"]
    successor = request["successor"]
    transport = returned["transport"]

    if (
        returned["packet"] != sid
        or returned["request_sha256"] != request["request_sha256"]
        or returned["producer"] != request["producer"]
        or returned["proposals"]
        or returned["reply_sha256"] != request["reply_sha256"]
        or transport["kind"] != "caller-import"
        or transport.get("source_path") != request["reply_path"]
    ):
        raise ValueError(
            f"{sid}: native return does not confirm the recorded handoff to {successor}; "
            "retain both for host reconciliation",
        )

    scope["superseded"] = {
        "at_utc": now(),
        "successor": successor,
        "account": request["account"],
        "return": returned,
    }
    del scope["supersession_request"]
    host._write(path, scope)
    host.event(
        "preparation-superseded",
        {
            "id": sid,
            "successor": successor,
            "scope": host.pin(path),
            "parent_duties_closed": False,
        },
    )
    return {
        "id": sid,
        "state": "superseded-without-output",
        "successor": successor,
        "return_sha256": returned["return_sha256"],
        "parent_duties_closed": False,
    }


def supersede_scope(host: Any, sid: str, spec: dict[str, Any]) -> dict[str, Any]:
    """Return an obsolete preparation without a candidate and carry duties to a real successor."""
    sid = ident(sid)
    path = host.root / "scopes" / f"{sid}.json"
    scope = read(path)
    if "result" in scope or "superseded" in scope:
        raise ValueError("The occurrence already has a host outcome; preserve it")

    successor = read(host.root / "scopes" / f"{ident(spec['successor'])}.json")
    if successor["id"] == sid or successor["prepared_at_utc"] <= scope["prepared_at_utc"]:
        raise ValueError("Select a distinct later actual preparation")
    if not set(scope["spec"]["duty_ids"]).issubset(successor["spec"]["duty_ids"]):
        raise ValueError("The actual successor must retain every selected parent duty")

    observed = host.inspect_native(sid)
    if observed.get("return") is not None or observed.get("transport_outcome") is not None:
        raise ValueError(
            "Native outcome already exists; inspect/reconcile it instead of receiving twice",
        )

    account = nonempty(spec["account"], "supersession account")
    producer = nonempty(spec["producer"], "actual responding participant")
    reply = host.root / "requests" / f"{sid}-supersession.md"
    body = (
        account
        + "\n\nNo candidate output is returned by this occurrence. Actual continuing scope: "
        + successor["id"]
        + ". Parent duties remain owed.\n"
    ).encode("utf-8")

    request_sha = scope["native_context"].get("request_sha256")
    if request_sha is None:
        request_sha = host.call("recover", sid)["request_sha256"]

    request = {
        "successor": successor["id"],
        "account": account,
        "producer": producer,
        "request_sha256": request_sha,
        "reply_path": str(reply),
        "reply_sha256": digest(body),
    }
    if "supersession_request" in scope and scope["supersession_request"] != request:
        raise ValueError(
            f"{sid}: the outstanding handoff to {scope['supersession_request']['successor']} "
            "differs; inspect the original request",
        )

    atomic_bytes(reply, body)
    scope["supersession_request"] = request
    host._write(path, scope)
    returned = host.call("receive", sid, "--reply", reply, "--producer", producer)["return"]
    return finish_supersession(host, path, scope, returned)


def collect_status(host: Any) -> dict[str, Any]:
    """Project native status alongside host scope states, partial publications, and pending
    captures.
    """
    native = host.call("status")
    transport = {row["packet"]: row for row in native["turns"]}
    scopes: list[dict[str, Any]] = []

    for scope in host.scopes():
        current = transport.get(scope["id"], {})
        if "result" in scope:
            label = "partially-received" if scope["result"].get("partial") else "received"
        elif "superseded" in scope:
            label = "superseded-without-output"
        elif "partial_publication" in scope:
            label = "partial-publication-needs-reconciliation"
        elif current.get("transport") == "returned":
            label = "native-return-needs-host-reconciliation"
        else:
            label = "prepared"

        row: dict[str, Any] = {"id": scope["id"], "host_state": label, "native": current}
        if "result" in scope and scope["result"].get("partial"):
            row["published_targets"] = [pub["path"] for pub in scope["result"]["publications"]]
            row["unpublished_targets"] = scope["result"].get("unpublished_targets", [])
        if "partial_publication" in scope and "result" not in scope:
            partial = scope["partial_publication"]
            row["published_targets"] = [pub["path"] for pub in partial["publications"]]
            row["pending_targets"] = partial["pending_targets"]
            if "failed_target" in partial:
                row["failed_target"] = partial["failed_target"]
            if "error" in partial:
                row["error"] = partial["error"]
        if "supersession_request" in scope and "result" not in scope:
            row.update(
                host_state="supersession-needs-reconciliation",
                successor=scope["supersession_request"]["successor"],
            )

        pending_caps = [
            path.name.removeprefix(".pending-").removesuffix(".json")
            for path in host.bridge._pending_capture_paths(scope["id"])
        ]
        if pending_caps:
            row["pending_captures"] = pending_caps
        scopes.append(row)

    return {
        "native": native,
        "scopes": scopes,
        "repairs": [read(path) for path in sorted((host.root / "repairs").glob("*.json"))],
    }
