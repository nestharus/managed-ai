"""Execute parsed native store operations and render their public results."""
from __future__ import annotations

import json
import math
from pathlib import Path
import sys

from store.governance.feedback import Feedback
from store.protocol.inputs import read, strings, text
from store.query.information import Information


def emit(result, args):
    """Pure output renderer: formats already-acquired results without filesystem I/O."""
    if getattr(args, 'text', False):
        if args.action == 'catalogue':
            if 'body' not in result:
                raise ValueError('catalogue --text requires an identity')
            sys.stdout.write(result['body'])
        else:
            rendered = result.get('context_text') if args.action != 'recover' else result.get('context')
            if rendered is None:
                rendered = result.get('context', '')
            sys.stdout.write(rendered)
    else:
        public_return = getattr(args, 'action', None) in {'run', 'receive'}
        ascii_json = public_return or getattr(args, 'changed_since', None) is not None
        rendered = json.dumps(result, ensure_ascii=ascii_json, indent=2, allow_nan=False)
        if public_return and hasattr(sys.stdout, 'buffer'):
            sys.stdout.buffer.write((rendered + '\n').encode('ascii'))
        else:
            print(rendered)


def _context_request(args):
    request = read(args.request) if args.request else {}
    if args.task:
        request['task'] = args.task
    if args.without_feedback:
        if request.get('feedback_uses'):
            raise ValueError('--without-feedback conflicts with selected feedback_uses')
        request['feedback_uses'] = []
    if args.feedback:
        selected = strings(request.get('feedback_uses', []), 'feedback_uses')
        request['feedback_uses'] = list(dict.fromkeys([*selected, *args.feedback]))
    if args.project_source:
        selected = strings(request.get('project_sources', []), 'project_sources')
        request['project_sources'] = list(dict.fromkeys([*selected, *args.project_source]))
    return request


def _compose(system, args):
    if args.interaction_map:
        if any(x is not None for x in (args.boundary, args.changed_since, args.repo, args.focus)) or args.pair_limit != 200:
            raise ValueError('interaction map takes --record and optional --limit')
        from store.composition.composition import Composition
        return Composition(system).interaction_map(args.record, args.limit)
    if args.boundary is not None:
        if args.changed_since is not None or args.repo is not None or args.focus is not None or args.pair_limit != 200:
            raise ValueError('boundary inspection takes --record, --boundary and optional --limit')
        from store.composition.composition_boundary import inspect_boundary
        return inspect_boundary(system, args.record, args.boundary, args.limit)
    if args.changed_since is not None or args.repo is not None:
        if not args.changed_since or args.repo is None or args.focus or args.pair_limit != 200:
            raise ValueError('change notice requires --changed-since BASE and --repo CHECKOUT; use --limit for detail')
        from store.composition.composition_change_notice import notice
        return notice(system, args.record, args.repo, args.changed_since, args.limit)
    return system.compose(args.record, args.focus, args.limit, args.pair_limit)


def _with_status(system, args, payload):
    if getattr(args, 'no_status', False):
        return payload
    return dict(payload, status=system.status())


def act(system, args, *, proof_store_locked: bool = False):
    action = args.action
    if action in {'import', 'configure', 'transfer', 'put-batch'}:
        from store.records import record_batch, record_import, record_transfer
        from store.selection import store_configuration
        if action == 'import':
            return record_import.import_records(system, read(args.spec))
        if action == 'put-batch':
            return record_batch.put_batch(system, read(args.spec))
        if action == 'configure':
            return store_configuration.configure(system, read(args.spec))
        if args.transfer_action == 'inspect':
            return record_transfer.inspect(system, args.id)
        if args.transfer_action == 'export':
            return record_transfer.export(system, args.record, args.output)
        return record_transfer.receive(system, args.bundle, args.id, args.account, args.expected)
    if action == 'put':
        return _with_status(system, args, {'record': system.put(read(args.record), args.expected)})
    if action == 'show':
        return system.show(args.id)
    if action == 'pin':
        return system.record_store.pin(system.show(args.id))
    if action == 'history':
        return next(system.record_store.history_pages(args.id, limit=args.limit,
                                                      cursor=read(args.cursor) if args.cursor else None))
    if action == 'index-head':
        return system.record_store.index_scope(args.id)
    if action == 'reindex':
        return system.reindex()
    if action == 'deduplicate-originals':
        from store.records import object_audit
        if args.dedup_action == 'plan':
            return object_audit.plan_deduplicate_originals(system, prefix=args.prefix)
        return object_audit.execute_deduplicate_originals(
            system, plan=read(args.plan), expected_plan=args.expected_plan, reason=args.reason)
    if action == 'compact-objects':
        from store.records import object_audit
        if args.compact_action == 'plan':
            return object_audit.plan_compact_objects(system, min_savings_bytes=args.min_savings_bytes)
        return object_audit.execute_compact_objects(
            system, plan=read(args.plan), expected_plan=args.expected_plan, reason=args.reason)
    if action == 'archive-records':
        from store.records import cold_archive
        if args.archive_action == 'plan':
            return cold_archive.plan_archive_records(system, ids=args.ids, kinds=args.kinds)
        if args.archive_action == 'restore':
            return cold_archive.restore_archived_records(system, ids=args.ids, reason=args.reason)
        return cold_archive.execute_archive_records(
            system, plan=read(args.plan), expected_plan=args.expected_plan, reason=args.reason)
    if action == 'audit-objects':
        from store.records import object_audit
        return object_audit.audit_objects(system)
    if action == 'gc-objects':
        from store.records import object_audit
        return object_audit.gc_objects(
            system, plan=read(args.plan), expected_plan=args.expected_plan, reason=args.reason)
    if action == 'graph':
        from store.query import graph_query
        if args.graph_action == 'neighbors':
            return graph_query.neighbors(
                system, args.id, direction=args.direction, hops=args.hops,
                relations=args.relations, active_only=args.active_only, records_only=args.records_only)
        if args.graph_action == 'links':
            return graph_query.links(
                system, target=args.target, relations=args.relations, active_only=args.active_only)
        if args.graph_action == 'path':
            return graph_query.shortest_path(
                system, args.src, args.dst, max_hops=args.max_hops,
                active_only=args.active_only, relations=args.relations)
        return graph_query.lint(system)
    if action == 'status':
        return system.status(verify_all_basis=getattr(args, 'verify_all_basis', False))
    if action == 'abandon-context':
        return system.abandon_context(args.turn, args.account)
    if action == 'recover':
        return system.recover(args.turn)
    if action == 'recover-transport':
        return system.recover_transport(args.turn)
    if action == 'close-transport':
        return _with_status(system, args, {'transport_outcome': system.close_transport(args.turn, args.account)})
    if action == 'reconcile-publication':
        result = system.reconcile_publication(args.turn, args.target, args.expected_sha256, args.account)
        return _with_status(system, args, {'reconciliation': result})
    if action == 'resume-context':
        return system.resume_context(args.turn)
    if action == 'view':
        return system.view(args.turn)
    if action == 'explain-context':
        return system.explain_context(args.turn)
    if action == 'retire-turns':
        if args.retire_turns_action == 'plan':
            return system.plan_turn_retirement(turns=args.turns or None, all_eligible=args.all_eligible)
        return system.execute_turn_retirement(
            plan=read(args.plan), expected_plan=args.expected_plan, reason=args.reason)
    if action == 'context':
        result = system.context(args.turn, _context_request(args))
        if getattr(args, 'text', False) and isinstance(result.get('context'), str):
            result = dict(result, context_text=Path(result['context']).read_text(encoding='utf-8'))
        return result
    if action == 'receive':
        result = system.receive(args.turn, args.reply, args.producer, args.proposal, args.assessment, args.wait,
                                run_nominations=args.run_nominations)
        return _with_status(system, args, {'return': result})
    if action == 'track':
        return _with_status(system, args, {'attention': system.track(args.spec)})
    if action == 'assess':
        return _with_status(system, args, {'attention': system.assess(args.turn, args.receipt, args.producer)})
    if action == 'adopt':
        return _with_status(system, args, {'publication': system.adopt(args.turn, args.target, args.reason)})
    if action == 'interpret':
        result = system.interpret(args.goal_revision, args.disposition, args.account, args.evidence, args.condition)
        return _with_status(system, args, {'interpretation': result})
    if action == 'run':
        if not math.isfinite(args.wait) or args.wait < 0:
            raise ValueError('run --wait must be finite and nonnegative')
        if args.proposal and args.wait == 0:
            raise ValueError('run --proposal requires a positive --wait')
        producer = text(args.producer, 'positive run --wait producer') if args.wait > 0 else None
        from core.storage import strict_json_loads
        from store.protocol.inputs import _open_ordinary_bytes
        argv = strict_json_loads(_open_ordinary_bytes(args.argv_json, 'JSON'), args.argv_json, require_object=False)
        if args.wait > 0:
            result = system.run(args.turn, argv, proposals=args.proposal, producer=producer, wait=args.wait)
        else:
            result = {'dispatch': system.run(args.turn, argv)}
        return _with_status(system, args, result)
    if action == 'catalogue':
        return system.catalogue(args.id, args.query, args.kind, args.limit, args.offset, args.content)
    if action == 'materialize':
        return system.materialize(args.operation)
    if action == 'discover':
        return system.discover(read(args.request))
    if action == 'recompose':
        from store.composition.composition_revision import revise
        from store.protocol.inputs import read_input
        proposal, _ = read_input(args.request, byte_limit=4 * 1024 * 1024)
        return revise(system, proposal, accept=args.accept, retain=args.retain, reason=args.reason,
                      proof_store=getattr(args, 'proof_store', None),
                      proof_store_locked=proof_store_locked)
    if action == 'compose':
        return _compose(system, args)
    if action == 'inquiry':
        from store.governance.inquiry import Inquiry
        inquiry = Inquiry(system)
        if args.inquiry_action == 'stages':
            return {'stages': inquiry.stages(args.id)}
        spec = read(args.spec)
        if args.inquiry_action == 'open':
            return inquiry.open(spec)
        return inquiry.ensure(spec['id'], 'evidence', spec['content'], inquiry.paths(spec['basis']))
    if action == 'feedback':
        feedback = Feedback(system)
        if args.feedback_action == 'inspect':
            return feedback.inspect(args.id, args.revision)
        return feedback.apply(args.feedback_action, read(args.spec))
    if action == 'info':
        information = Information(system)
        if args.information_action == 'inspect':
            return information.inspect(args.id)
        return getattr(information, args.information_action)(read(args.spec))
    raise ValueError('unknown action')
