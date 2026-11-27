"""Execute parsed native store operations and render their public results."""

import argparse
import json
import math
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from managed_ai_information.store.governance.feedback import Feedback
from managed_ai_information.store.protocol.inputs import read, strings, text
from managed_ai_information.store.query.information import Information


def emit(result: dict[str, Any], args: argparse.Namespace) -> None:
    """Pure output renderer: formats already-acquired results without filesystem I/O."""
    if getattr(args, "text", False):
        if args.action == "catalogue":
            if "body" not in result:
                raise ValueError("catalogue --text requires an identity")
            sys.stdout.write(result["body"])
        else:
            rendered = (
                result.get("context_text") if args.action != "recover" else result.get("context")
            )
            if rendered is None:
                rendered = result.get("context", "")
            sys.stdout.write(rendered)
    else:
        public_return = getattr(args, "action", None) in {"run", "receive"}
        ascii_json = public_return or getattr(args, "changed_since", None) is not None
        rendered = json.dumps(result, ensure_ascii=ascii_json, indent=2, allow_nan=False)
        if public_return and hasattr(sys.stdout, "buffer"):
            sys.stdout.buffer.write((rendered + "\n").encode("ascii"))
        else:
            print(rendered)
    if (
        getattr(args, "action", None) == "graph"
        and getattr(args, "graph_action", None) == "profile"
        and getattr(args, "check_alerts", False)
        and isinstance(result, dict)
        and result.get("alert_count", 0) > 0
    ):
        raise SystemExit(1)


def _context_request(args: argparse.Namespace) -> dict[str, Any]:
    request = read(args.request) if args.request else {}
    if args.task:
        request["task"] = args.task
    if args.without_feedback:
        if request.get("feedback_uses"):
            raise ValueError("--without-feedback conflicts with selected feedback_uses")
        request["feedback_uses"] = []
    if args.feedback:
        selected = strings(request.get("feedback_uses", []), "feedback_uses")
        request["feedback_uses"] = list(dict.fromkeys([*selected, *args.feedback]))
    if args.project_source:
        selected = strings(request.get("project_sources", []), "project_sources")
        request["project_sources"] = list(dict.fromkeys([*selected, *args.project_source]))
    return request


def _compose(system: Any, args: argparse.Namespace) -> dict[str, Any]:
    if args.interaction_map:
        if (
            any(x is not None for x in (args.boundary, args.changed_since, args.repo, args.focus))
            or args.pair_limit != 200
        ):
            raise ValueError("interaction map takes --record and optional --limit")
        from managed_ai_information.store.composition.composition import Composition

        return Composition(system).interaction_map(args.record, args.limit)
    if args.boundary is not None:
        if (
            args.changed_since is not None
            or args.repo is not None
            or args.focus is not None
            or args.pair_limit != 200
        ):
            raise ValueError(
                "boundary inspection takes --record, --boundary and optional --limit",
            )
        from managed_ai_information.store.composition.composition_boundary import inspect_boundary

        return inspect_boundary(system, args.record, args.boundary, args.limit)
    if args.changed_since is not None or args.repo is not None:
        if not args.changed_since or args.repo is None or args.focus or args.pair_limit != 200:
            raise ValueError(
                "change notice requires --changed-since BASE and --repo CHECKOUT; "
                "use --limit for detail",
            )
        from managed_ai_information.store.composition.composition_change_notice import notice

        return notice(system, args.record, args.repo, args.changed_since, args.limit)
    return system.compose(args.record, args.focus, args.limit, args.pair_limit)


def _with_status(system: Any, args: argparse.Namespace, payload: dict[str, Any]) -> dict[str, Any]:
    if getattr(args, "no_status", False):
        return payload
    return dict(payload, status=system.status())


def _status_summary(full: dict[str, Any]) -> dict[str, Any]:
    """Return a compact operator/agent summary of system.status()."""
    sel = full.get("selection") or {}
    cu = full.get("current_use") or {}
    return {
        "instance_id": full.get("instance_id"),
        "app_version": full.get("app_version"),
        "engine_api": full.get("engine_api"),
        "domains": sel.get("domains", []),
        "source_count": sel.get("source_count", 0),
        "record_count": full.get("record_count", len(full.get("records", []))),
        "current_goal": full.get("current_goal"),
        "turns_count": len(full.get("turns", [])),
        "incomplete_turns_count": len(full.get("incomplete_turns", {})),
        "gaps": full.get("gaps", {}),
        "needs_interpretation": cu.get("needs_interpretation"),
    }


def _discover_request(args: argparse.Namespace) -> dict[str, Any]:
    """Build a discovery request from --request FILE and/or inline CLI flags."""
    has_inline = any(
        val is not None and val is not False
        for val in (
            getattr(args, "query", None),
            getattr(args, "scopes", None),
            getattr(args, "kinds", None),
            getattr(args, "anchors", None),
            getattr(args, "content", False),
            getattr(args, "direction", None),
            getattr(args, "hops", None),
            getattr(args, "limit", None),
            getattr(args, "records_only", False),
            getattr(args, "search_mode", None),
        )
    )
    if args.request is None and not has_inline:
        raise ValueError("supply --request FILE or inline --query/--anchor/--scope options")
    req: dict[str, Any] = dict(read(args.request)) if args.request is not None else {}
    if getattr(args, "query", None) is not None:
        req["query"] = args.query
    if getattr(args, "scopes", None):
        req["scopes"] = list(dict.fromkeys(args.scopes))
    if getattr(args, "kinds", None):
        parsed_kinds = list(
            dict.fromkeys(
                part.strip() for item in args.kinds for part in str(item).split(",") if part.strip()
            )
        )
        if not parsed_kinds:
            raise ValueError("kinds must contain at least one nonempty kind name")
        req["kinds"] = parsed_kinds
    if getattr(args, "anchors", None):
        req["anchors"] = list(dict.fromkeys(args.anchors))
    if getattr(args, "content", False):
        req["content"] = True
    if getattr(args, "direction", None) is not None:
        req["direction"] = args.direction
    if getattr(args, "hops", None) is not None:
        req["hops"] = args.hops
    if getattr(args, "limit", None) is not None:
        req["limit"] = args.limit
    if getattr(args, "records_only", False):
        req["records_only"] = True
    if getattr(args, "search_mode", None) is not None:
        req["search_mode"] = args.search_mode
    return req


def _act_transfer(system: Any, args: argparse.Namespace) -> dict[str, Any]:
    from managed_ai_information.store.records import record_transfer

    if args.transfer_action == "inspect":
        return record_transfer.inspect(system, args.id)
    if args.transfer_action == "export":
        return record_transfer.export(system, args.record, args.output)
    return record_transfer.receive(system, args.bundle, args.id, args.account, args.expected)


def _act_dedup(system: Any, args: argparse.Namespace) -> dict[str, Any]:
    from managed_ai_information.store.records import object_audit

    if args.dedup_action == "plan":
        return object_audit.plan_deduplicate_originals(system, prefix=args.prefix)
    return object_audit.execute_deduplicate_originals(
        system,
        plan=read(args.plan),
        expected_plan=args.expected_plan,
        reason=args.reason,
    )


def _act_compact(system: Any, args: argparse.Namespace) -> dict[str, Any]:
    from managed_ai_information.store.records import object_audit

    if args.compact_action == "plan":
        return object_audit.plan_compact_objects(system, min_savings_bytes=args.min_savings_bytes)
    return object_audit.execute_compact_objects(
        system,
        plan=read(args.plan),
        expected_plan=args.expected_plan,
        reason=args.reason,
    )


def _act_archive(system: Any, args: argparse.Namespace) -> dict[str, Any]:
    from managed_ai_information.store.records import cold_archive

    if args.archive_action == "plan":
        return cold_archive.plan_archive_records(system, ids=args.ids, kinds=args.kinds)
    if args.archive_action == "restore":
        return cold_archive.restore_archived_records(system, ids=args.ids, reason=args.reason)
    return cold_archive.execute_archive_records(
        system,
        plan=read(args.plan),
        expected_plan=args.expected_plan,
        reason=args.reason,
    )


def _act_graph(system: Any, args: argparse.Namespace) -> dict[str, Any]:
    from managed_ai_information.store.query import graph_query

    if args.graph_action == "neighbors":
        return graph_query.neighbors(
            system,
            args.id,
            direction=args.direction,
            hops=args.hops,
            relations=args.relations,
            active_only=args.active_only,
            records_only=args.records_only,
        )
    if args.graph_action == "links":
        return graph_query.links(
            system,
            target=args.target,
            relations=args.relations,
            active_only=args.active_only,
        )
    if args.graph_action == "path":
        return graph_query.shortest_path(
            system,
            args.src,
            args.dst,
            max_hops=args.max_hops,
            active_only=args.active_only,
            relations=args.relations,
        )
    if args.graph_action == "profile":
        from managed_ai_information.store.query import graph_profile

        def _csv_list(attr: str) -> list[str]:
            raw = getattr(args, attr, None) or []
            return [part.strip() for item in raw for part in item.split(",") if part.strip()]

        return graph_profile.profile(
            system,
            roots=_csv_list("roots"),
            include_cochange=bool(getattr(args, "include_cochange", False)),
            baseline_path=getattr(args, "baseline", None),
            write_baseline_path=getattr(args, "write_baseline", None),
            check_alerts=bool(getattr(args, "check_alerts", False)),
            profile_config_path=getattr(args, "profile_config", None),
            claim_kinds=_csv_list("claim_kinds"),
            navigational_kinds=_csv_list("navigational_kinds"),
            qualifying_predicates=_csv_list("qualifying_predicates"),
            structural_predicates=_csv_list("structural_predicates"),
            bypass_exempt_predicates=_csv_list("bypass_exempt_predicates"),
        )
    return graph_query.lint(system)


def _act_status(system: Any, args: argparse.Namespace) -> dict[str, Any]:
    full = system.status(verify_all_basis=getattr(args, "verify_all_basis", False))
    return _status_summary(full) if getattr(args, "summary", False) else full


def _act_context(system: Any, args: argparse.Namespace) -> dict[str, Any]:
    result = system.context(args.turn, _context_request(args))
    if getattr(args, "text", False) and isinstance(result.get("context"), str):
        result = dict(result, context_text=Path(result["context"]).read_text(encoding="utf-8"))
    return result


def _act_run(system: Any, args: argparse.Namespace) -> dict[str, Any]:
    if not math.isfinite(args.wait) or args.wait < 0:
        raise ValueError("run --wait must be finite and nonnegative")
    if args.proposal and args.wait == 0:
        raise ValueError("run --proposal requires a positive --wait")
    producer = text(args.producer, "positive run --wait producer") if args.wait > 0 else None
    from managed_ai_information.core.storage import strict_json_loads
    from managed_ai_information.store.protocol.inputs import _open_ordinary_bytes

    argv = strict_json_loads(
        _open_ordinary_bytes(args.argv_json, "JSON"),
        args.argv_json,
        require_object=False,
    )
    if args.wait > 0:
        result = system.run(
            args.turn,
            argv,
            proposals=args.proposal,
            producer=producer,
            wait=args.wait,
        )
    else:
        result = {"dispatch": system.run(args.turn, argv)}
    return _with_status(system, args, result)


def _act_inquiry(system: Any, args: argparse.Namespace) -> dict[str, Any]:
    from managed_ai_information.store.governance.inquiry import Inquiry

    inquiry = Inquiry(system)
    if args.inquiry_action == "stages":
        return {"stages": inquiry.stages(args.id)}
    spec = read(args.spec)
    if args.inquiry_action == "open":
        return inquiry.open(spec)
    return inquiry.ensure(spec["id"], "evidence", spec["content"], inquiry.paths(spec["basis"]))


def _act_feedback(system: Any, args: argparse.Namespace) -> dict[str, Any]:
    feedback = Feedback(system)
    if args.feedback_action == "inspect":
        return feedback.inspect(args.id, args.revision)
    return feedback.apply(args.feedback_action, read(args.spec))


def _act_info(system: Any, args: argparse.Namespace) -> dict[str, Any]:
    information = Information(system)
    if args.information_action == "inspect":
        return information.inspect(args.id)
    return getattr(information, args.information_action)(read(args.spec))


def _act_import(system: Any, args: argparse.Namespace) -> dict[str, Any]:
    from managed_ai_information.store.records import record_import

    return record_import.import_records(system, read(args.spec))


def _act_put_batch(system: Any, args: argparse.Namespace) -> dict[str, Any]:
    return system.put_batch(read(args.spec))


def _act_backend(system: Any, args: argparse.Namespace) -> dict[str, Any]:
    bridge = system.backend_bridge
    if args.backend_action == "status":
        return bridge.status(
            check_parity=not getattr(args, "no_parity", False),
            verify_blob_bytes=bool(getattr(args, "verify_blob_bytes", False)),
        )
    if args.backend_action == "sync":
        return bridge.sync(
            url=args.url,
            namespace=args.namespace,
            mode=args.mode,
            token_env=args.token_env,
            include_blobs=not args.no_blobs,
            verify_blob_bytes=args.verify_blob_bytes,
        )
    if args.backend_action == "cutover":
        return bridge.cutover(
            url=args.url,
            namespace=args.namespace,
            mode=args.mode,
            token_env=args.token_env,
            include_blobs=not args.no_blobs,
            verify_blob_bytes=args.verify_blob_bytes,
        )
    return bridge.rollback(reason=args.reason)


def _act_configure(system: Any, args: argparse.Namespace) -> dict[str, Any]:
    from managed_ai_information.store.selection import store_configuration

    return store_configuration.configure(system, read(args.spec))


def _act_audit_objects(system: Any, _args: argparse.Namespace) -> dict[str, Any]:
    from managed_ai_information.store.records import object_audit

    return object_audit.audit_objects(system)


def _act_gc_objects(system: Any, args: argparse.Namespace) -> dict[str, Any]:
    from managed_ai_information.store.records import object_audit

    return object_audit.gc_objects(
        system,
        plan=read(args.plan),
        expected_plan=args.expected_plan,
        reason=args.reason,
    )


def _act_pack_objects(system: Any, args: argparse.Namespace) -> dict[str, Any]:
    from managed_ai_information.store.records import pack_slab

    codec = getattr(args, "codec", None)
    if getattr(args, "apply", False):
        return pack_slab.execute_pack_objects(
            system,
            shard_target_bytes=args.shard_bytes,
            compress=args.compress,
            codec=codec,
            include_cold_archive=args.include_cold_archive,
            expected_plan=args.expected_plan,
            reason=args.reason,
        )
    return pack_slab.plan_pack_objects(
        system,
        shard_target_bytes=args.shard_bytes,
        compress=args.compress,
        codec=codec,
        include_cold_archive=args.include_cold_archive,
    )


def _act_retire_turns(system: Any, args: argparse.Namespace) -> dict[str, Any]:
    if args.retire_turns_action == "plan":
        return system.plan_turn_retirement(
            turns=args.turns or None,
            all_eligible=args.all_eligible,
        )
    return system.execute_turn_retirement(
        plan=read(args.plan),
        expected_plan=args.expected_plan,
        reason=args.reason,
    )


_ACTION_HANDLERS: dict[str, Callable[[Any, argparse.Namespace], dict[str, Any]]] = {
    "import": _act_import,
    "put-batch": _act_put_batch,
    "configure": _act_configure,
    "transfer": _act_transfer,
    "put": lambda system, args: _with_status(
        system,
        args,
        {"record": system.put(read(args.record), args.expected)},
    ),
    "show": lambda system, args: system.show(args.id),
    "pin": lambda system, args: system.record_store.pin(system.show(args.id)),
    "history": lambda system, args: next(
        system.record_store.history_pages(
            args.id,
            limit=args.limit,
            cursor=read(args.cursor) if args.cursor else None,
        )
    ),
    "index-head": lambda system, args: system.record_store.index_scope(args.id),
    "reindex": lambda system, _args: system.reindex(),
    "deduplicate-originals": _act_dedup,
    "compact-objects": _act_compact,
    "archive-records": _act_archive,
    "audit-objects": _act_audit_objects,
    "gc-objects": _act_gc_objects,
    "pack-objects": _act_pack_objects,
    "backend": _act_backend,
    "graph": _act_graph,
    "status": _act_status,
    "abandon-context": lambda system, args: system.abandon_context(args.turn, args.account),
    "recover": lambda system, args: system.recover(args.turn),
    "recover-transport": lambda system, args: system.recover_transport(args.turn),
    "close-transport": lambda system, args: _with_status(
        system,
        args,
        {"transport_outcome": system.close_transport(args.turn, args.account)},
    ),
    "reconcile-publication": lambda system, args: _with_status(
        system,
        args,
        {
            "reconciliation": system.reconcile_publication(
                args.turn,
                args.target,
                args.expected_sha256,
                args.account,
            )
        },
    ),
    "resume-context": lambda system, args: system.resume_context(args.turn),
    "view": lambda system, args: system.view(args.turn),
    "explain-context": lambda system, args: system.explain_context(args.turn),
    "retire-turns": _act_retire_turns,
    "context": _act_context,
    "receive": lambda system, args: _with_status(
        system,
        args,
        {
            "return": system.receive(
                args.turn,
                args.reply,
                args.producer,
                args.proposal,
                args.assessment,
                args.wait,
                run_nominations=args.run_nominations,
            )
        },
    ),
    "track": lambda system, args: _with_status(
        system, args, {"attention": system.track(args.spec)}
    ),
    "assess": lambda system, args: _with_status(
        system,
        args,
        {"attention": system.assess(args.turn, args.receipt, args.producer)},
    ),
    "adopt": lambda system, args: _with_status(
        system,
        args,
        {"publication": system.adopt(args.turn, args.target, args.reason)},
    ),
    "interpret": lambda system, args: _with_status(
        system,
        args,
        {
            "interpretation": system.interpret(
                args.goal_revision,
                args.disposition,
                args.account,
                args.evidence,
                args.condition,
            )
        },
    ),
    "run": _act_run,
    "catalogue": lambda system, args: system.catalogue(
        args.id,
        args.query,
        args.kind,
        args.limit,
        args.offset,
        args.content,
    ),
    "materialize": lambda system, args: system.materialize(args.operation),
    "discover": lambda system, args: system.discover(_discover_request(args)),
    "compose": _compose,
    "inquiry": _act_inquiry,
    "feedback": _act_feedback,
    "info": _act_info,
}


def act(
    system: Any, args: argparse.Namespace, *, proof_store_locked: bool = False
) -> dict[str, Any]:
    action = args.action
    if action == "recompose":
        from managed_ai_information.store.composition.composition_revision import revise
        from managed_ai_information.store.protocol.inputs import read_input

        proposal, _ = read_input(args.request, byte_limit=4 * 1024 * 1024)
        return revise(
            system,
            proposal,
            accept=args.accept,
            retain=args.retain,
            reason=args.reason,
            proof_store=getattr(args, "proof_store", None),
            proof_store_locked=proof_store_locked,
        )
    handler = _ACTION_HANDLERS.get(action)
    if handler is None:
        raise ValueError("unknown action")
    return handler(system, args)
