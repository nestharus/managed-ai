#!/usr/bin/env python3
"""A small persistent work context for an actual capable agent.

App 0.4.0 composes the POSIX continuation core 0.4.1 wire format. JSON records
carry public work; the actual host supplies interpretation and current authority.
"""
from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import sys

from feedback import Feedback
from information import Information
from native_runtime.inputs import read, strings, text
from native_runtime.loading import REVIEWED_CONFIGURE


def _file_argument(value):
    """Decode the exact TARGET=FILE argv bytes in an emitted recovery command."""
    return os.fsdecode(bytes.fromhex(value))


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root', required=True, type=Path)
    sub = p.add_subparsers(dest='action', required=True)
    init = sub.add_parser('init')
    goal = init.add_mutually_exclusive_group(required=True)
    goal.add_argument('--goal', type=Path)
    goal.add_argument('--goal-text')
    init.add_argument('--instance-id')
    init.add_argument('--domain', action='append', default=[], type=Path)
    init.add_argument('--catalogue', action='append', default=[], type=Path)
    init.add_argument('--prepared', action='store_true', help='Preserve prepared data in an uninitialized store')
    init.add_argument('--record-retention', choices=['current-use', 'append-history'], default='current-use')
    ingest = sub.add_parser('import'); ingest.add_argument('--spec', required=True, type=Path)
    configure = sub.add_parser('configure', description=f'Configure a current selection or receive {REVIEWED_CONFIGURE}.')
    configure.add_argument('--spec', required=True, type=Path)
    transfer = sub.add_parser('transfer').add_subparsers(dest='transfer_action', required=True)
    export = transfer.add_parser('export'); export.add_argument('--record', required=True)
    export.add_argument('--output', required=True, type=Path)
    receive = transfer.add_parser('receive'); receive.add_argument('--bundle', required=True, type=Path)
    receive.add_argument('--id', required=True); receive.add_argument('--account', required=True)
    receive.add_argument('--expected', required=True, type=int)
    inspect_transfer = transfer.add_parser('inspect'); inspect_transfer.add_argument('id')
    put = sub.add_parser('put'); put.add_argument('--record', required=True, type=Path)
    put.add_argument('--expected', required=True, type=int)
    show = sub.add_parser('show'); show.add_argument('id')
    pin = sub.add_parser('pin'); pin.add_argument('id')
    history = sub.add_parser('history'); history.add_argument('id')
    history.add_argument('--limit', type=int, default=8)
    history.add_argument('--cursor', type=Path, help='cursor JSON from the preceding page')
    sub.add_parser('index-head', help='verify and register one large legacy head for scope scans').add_argument('id')
    sub.add_parser('status')
    abandon = sub.add_parser('abandon-context'); abandon.add_argument('turn'); abandon.add_argument('--account', required=True)
    context = sub.add_parser('context'); context.add_argument('turn')
    context.add_argument('--request', type=Path); context.add_argument('--task')
    context.add_argument('--project-source', action='append', default=[])
    context.add_argument('--text', action='store_true')
    resume = sub.add_parser('resume-context'); resume.add_argument('turn')
    recover = sub.add_parser('recover'); recover.add_argument('turn'); recover.add_argument('--text', action='store_true')
    transport = sub.add_parser('recover-transport', help='recover the captured carrier view from retained evidence')
    transport.add_argument('turn')
    closure = sub.add_parser('close-transport', help='record a terminal command failure without a contribution')
    closure.add_argument('turn'); closure.add_argument('--account', required=True)
    reconciliation = sub.add_parser('reconcile-publication', help='acknowledge current target bytes without replaying publication')
    reconciliation.add_argument('turn'); reconciliation.add_argument('--target', required=True)
    reconciliation.add_argument('--expected-sha256', required=True, metavar='SHA256_OR_absent')
    reconciliation.add_argument('--account', required=True)
    view = sub.add_parser('view'); view.add_argument('turn')
    explain = sub.add_parser('explain-context', help='explain the frozen input selection without reading current sources')
    explain.add_argument('turn')
    receive = sub.add_parser('receive'); receive.add_argument('turn'); receive.add_argument('--reply', type=Path)
    attribution = receive.add_mutually_exclusive_group(required=True)
    attribution.add_argument('--producer')
    attribution.add_argument('--run-nominations', action='store_true',
                             help='Collect the original carrier with its bound files and producer.')
    receive.add_argument('--proposal', action='append', default=[])
    receive.add_argument('--file-argument', action='append', dest='proposal', type=_file_argument,
                         metavar='HEX', help='Original TARGET=FILE argument bytes in a recovery command.')
    receive.add_argument('--assessment', type=Path); receive.add_argument('--wait', type=float, default=0)
    track = sub.add_parser('track'); track.add_argument('--spec', required=True, type=Path)
    assess = sub.add_parser('assess'); assess.add_argument('turn')
    assess.add_argument('--receipt', required=True, type=Path); assess.add_argument('--producer', required=True)
    adopt = sub.add_parser('adopt'); adopt.add_argument('turn'); adopt.add_argument('--target', required=True)
    adopt.add_argument('--reason', required=True)
    use = sub.add_parser('interpret'); use.add_argument('--goal-revision', required=True, type=int)
    use.add_argument('--disposition', required=True, choices=('met', 'continue', 'wait'))
    use.add_argument('--account', required=True, type=Path, metavar='FILE',
                     help='UTF-8 account file on the host. Relative paths start at the caller\'s working directory.')
    use.add_argument('--evidence', action='append', default=[], metavar='WORK_PATH',
                     help='File path relative to the runtime work/ directory. Repeat for each piece of evidence.')
    use.add_argument('--condition')
    run = sub.add_parser('run'); run.add_argument('turn'); run.add_argument('--argv-json', type=Path, required=True)
    run.add_argument('--wait', type=float, default=0); run.add_argument('--producer')
    run.add_argument('--proposal', action='append', default=[], metavar='TARGET=FILE',
                     help='Nominate a proposal file for first receipt with a positive --wait. Repeat for each target.')
    catalogue = sub.add_parser('catalogue'); catalogue.add_argument('id', nargs='?')
    catalogue.add_argument('--text', action='store_true')
    catalogue.add_argument('--query', default=''); catalogue.add_argument('--kind')
    catalogue.add_argument('--limit', type=int, default=25); catalogue.add_argument('--offset', type=int, default=0)
    catalogue.add_argument('--content', action='store_true')
    materialize = sub.add_parser('materialize'); materialize.add_argument('operation')
    discovery = sub.add_parser('discover'); discovery.add_argument('--request', required=True, type=Path)
    composition = sub.add_parser('compose')
    composition.add_argument('--record', required=True)
    composition.add_argument('--interaction-map', action='store_true', help='group declared interactions by resource and explicit conflict')
    composition.add_argument('--boundary')
    composition.add_argument('--changed-since')
    composition.add_argument('--repo', type=Path)
    composition.add_argument('--focus')
    composition.add_argument('--limit', type=int, default=20)
    composition.add_argument('--pair-limit', type=int, default=200)
    revision = sub.add_parser('recompose', help='Review one composition revision against current, selected evidence')
    revision.add_argument('--request', type=Path, required=True,
                          help='Proposal JSON. Optional consumer_scope lists local record IDs to inspect for bound uses.')
    revision.add_argument('--proof-store', type=Path, help='Current external owner bound by this composition')
    decision = revision.add_mutually_exclusive_group()
    decision.add_argument('--accept', metavar='REVIEW_SHA256')
    decision.add_argument('--retain', metavar='REVIEW_SHA256')
    revision.add_argument('--reason')
    inquiry = sub.add_parser('inquiry').add_subparsers(dest='inquiry_action', required=True)
    for name in ('open', 'evidence'):
        inquiry.add_parser(name).add_argument('--spec', required=True, type=Path)
    inquiry.add_parser('stages').add_argument('id')
    feedback = sub.add_parser('feedback')
    actions = feedback.add_subparsers(dest='feedback_action', required=True)
    inspect_feedback = actions.add_parser('inspect')
    inspect_feedback.add_argument('id')
    inspect_feedback.add_argument('--revision', type=int)
    for name in ('open', 'revise', 'observe', 'accept'):
        actions.add_parser(name).add_argument('--spec', required=True, type=Path)
    scope = context.add_mutually_exclusive_group()
    scope.add_argument('--feedback', action='append', default=[])
    scope.add_argument('--without-feedback', action='store_true')
    info = sub.add_parser('info')
    info_actions = info.add_subparsers(dest='information_action', required=True)
    inspect = info_actions.add_parser('inspect'); inspect.add_argument('id')
    for name in ('wrap', 'migrate'):
        action = info_actions.add_parser(name); action.add_argument('--spec', required=True, type=Path)
    return p


def emit(result, args):
    if getattr(args, 'text', False):
        if args.action == 'catalogue':
            if 'body' not in result: raise ValueError('catalogue --text requires an identity')
            sys.stdout.write(result['body'])
        else:
            context = result.get('context')
            sys.stdout.write(context if args.action == 'recover' else Path(context).read_text(encoding='utf-8'))
    else:
        public_return = getattr(args, 'action', None) in {'run', 'receive'}
        ascii_json = public_return or getattr(args, 'changed_since', None) is not None
        rendered = json.dumps(result, ensure_ascii=ascii_json, indent=2, allow_nan=False)
        if public_return and hasattr(sys.stdout, 'buffer'):
            sys.stdout.buffer.write((rendered + '\n').encode('ascii'))
        else:
            print(rendered)


def act(system, args):
    action = args.action
    if action in {'import', 'configure', 'transfer'}:
        from native_runtime.loading import Loading
        loading = Loading(system)
        if action == 'import': return loading.import_records(read(args.spec))
        if action == 'configure': return loading.configure(read(args.spec))
        if args.transfer_action == 'inspect': return loading.inspect(args.id)
        if args.transfer_action == 'export': return loading.export(args.record, args.output)
        return loading.receive(args.bundle, args.id, args.account, args.expected)
    if action == 'put':
        return {'record': system.put(read(args.record), args.expected), 'status': system.status()}
    if action == 'show': return system.show(args.id)
    if action == 'pin': return system.record_store.pin(system.show(args.id))
    if action == 'history':
        return next(system.record_store.history_pages(args.id, limit=args.limit,
                                                     cursor=read(args.cursor) if args.cursor else None))
    if action == 'index-head': return system.record_store.index_scope(args.id)
    if action == 'status': return system.status()
    if action == 'abandon-context': return system.abandon_context(args.turn, args.account)
    if action == 'recover': return system.recover(args.turn)
    if action == 'recover-transport': return system.recover_transport(args.turn)
    if action == 'close-transport':
        return {'transport_outcome': system.close_transport(args.turn, args.account), 'status': system.status()}
    if action == 'reconcile-publication':
        result = system.reconcile_publication(args.turn, args.target, args.expected_sha256, args.account)
        return {'reconciliation': result, 'status': system.status()}
    if action == 'resume-context': return system.resume_context(args.turn)
    if action == 'view': return system.view(args.turn)
    if action == 'explain-context': return system.explain_context(args.turn)
    if action == 'context':
        request = read(args.request) if args.request else {}
        if args.task: request['task'] = args.task
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
        return system.context(args.turn, request)
    if action == 'receive':
        result = system.receive(args.turn, args.reply, args.producer, args.proposal, args.assessment, args.wait,
                                run_nominations=args.run_nominations)
        return {'return': result, 'status': system.status()}
    if action == 'track': return {'attention': system.track(args.spec), 'status': system.status()}
    if action == 'assess':
        return {'attention': system.assess(args.turn, args.receipt, args.producer), 'status': system.status()}
    if action == 'adopt':
        return {'publication': system.adopt(args.turn, args.target, args.reason), 'status': system.status()}
    if action == 'interpret':
        result = system.interpret(args.goal_revision, args.disposition, args.account, args.evidence, args.condition)
        return {'interpretation': result, 'status': system.status()}
    if action == 'run':
        if not math.isfinite(args.wait) or args.wait < 0:
            raise ValueError('run --wait must be finite and nonnegative')
        if args.proposal and args.wait == 0:
            raise ValueError('run --proposal requires a positive --wait')
        producer = text(args.producer, 'positive run --wait producer') if args.wait > 0 else None
        argv = json.loads(args.argv_json.read_text(encoding='utf-8'))
        if args.wait > 0:
            result = system.run(args.turn, argv, proposals=args.proposal, producer=producer, wait=args.wait)
        else:
            result = {'dispatch': system.run(args.turn, argv)}
        return dict(result, status=system.status())
    if action == 'catalogue':
        return system.catalogue(args.id, args.query, args.kind, args.limit, args.offset, args.content)
    if action == 'materialize': return system.materialize(args.operation)
    if action == 'discover': return system.discover(read(args.request))
    if action == 'recompose':
        from composition_revision import revise
        from native_runtime.inputs import read_input
        proposal, _ = read_input(args.request, byte_limit=4 * 1024 * 1024)
        return revise(system, proposal, accept=args.accept, retain=args.retain, reason=args.reason,
                      proof_store=getattr(args, 'proof_store', None))
    if action == 'compose':
        if args.interaction_map:
            if any(x is not None for x in (args.boundary, args.changed_since, args.repo, args.focus)) or args.pair_limit != 200:
                raise ValueError('interaction map takes --record and optional --limit')
            from composition import Composition
            return Composition(system).interaction_map(args.record, args.limit)
        if args.boundary is not None:
            if args.changed_since is not None or args.repo is not None or args.focus is not None or args.pair_limit != 200:
                raise ValueError('boundary inspection takes --record, --boundary and optional --limit')
            from boundary import inspect_boundary
            return inspect_boundary(system, args.record, args.boundary, args.limit)
        if args.changed_since is not None or args.repo is not None:
            if not args.changed_since or args.repo is None or args.focus or args.pair_limit != 200:
                raise ValueError('change notice requires --changed-since BASE and --repo CHECKOUT; use --limit for detail')
            from change_notice import notice
            return notice(system, args.record, args.repo, args.changed_since, args.limit)
        return system.compose(args.record, args.focus, args.limit, args.pair_limit)
    if action == 'inquiry':
        from native_runtime.inquiry import Inquiry
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
        if args.information_action == 'inspect': return information.inspect(args.id)
        return getattr(information, args.information_action)(read(args.spec))
    raise ValueError('unknown action')
