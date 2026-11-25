#!/usr/bin/env python3
"""Operate a native store, its selected sources and its captured turns."""
from __future__ import annotations

import argparse
import os
from pathlib import Path


READONLY_ACTIONS = frozenset({
    'show', 'catalogue', 'status', 'discover', 'compose',
    'history', 'view', 'recover', 'recover-transport',
    'audit-objects', 'graph',
})


def is_readonly_action(args) -> bool:
    """Return True when the parsed native action does not mutate store state."""
    action = args.action
    if action in READONLY_ACTIONS:
        return True
    if action == 'transfer':
        return getattr(args, 'transfer_action', None) in {'inspect', 'export'}
    if action == 'feedback':
        return getattr(args, 'feedback_action', None) == 'inspect'
    if action == 'info':
        return getattr(args, 'information_action', None) == 'inspect'
    if action == 'inquiry':
        return getattr(args, 'inquiry_action', None) == 'stages'
    if action == 'deduplicate-originals':
        return getattr(args, 'dedup_action', None) == 'plan'
    if action == 'compact-objects':
        return getattr(args, 'compact_action', None) == 'plan'
    if action == 'archive-records':
        return getattr(args, 'archive_action', None) == 'plan'
    if action == 'retire-turns':
        return getattr(args, 'retire_turns_action', None) == 'plan'
    return False


def _file_argument(value):
    """Decode the exact TARGET=FILE argv bytes in an emitted recovery command."""
    return os.fsdecode(bytes.fromhex(value))


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    p.add_argument('--root', required=True, type=Path)
    p.add_argument('--admission-deadline', type=float, help=argparse.SUPPRESS)
    sub = p.add_subparsers(dest='action', required=True)
    _store_commands(sub)
    _turn_commands(sub)
    _source_and_composition_commands(sub)
    _inquiry_feedback_information_commands(sub)
    return p


def _store_commands(sub):
    from native_runtime.store_configuration import REVIEWED_CONFIGURE

    init = sub.add_parser('init', allow_abbrev=False)
    goal = init.add_mutually_exclusive_group(required=True)
    goal.add_argument('--goal', type=Path)
    goal.add_argument('--goal-text')
    init.add_argument('--instance-id')
    init.add_argument('--domain', action='append', default=[], type=Path)
    init.add_argument('--catalogue', action='append', default=[], type=Path)
    init.add_argument('--prepared', action='store_true', help='Preserve prepared data in an uninitialized store')
    init.add_argument('--record-retention', choices=['current-use', 'append-history'], default='current-use')
    ingest = sub.add_parser('import', allow_abbrev=False)
    ingest.add_argument('--spec', required=True, type=Path)
    configure = sub.add_parser('configure', allow_abbrev=False,
                               description=f'Configure a current selection or receive {REVIEWED_CONFIGURE}.')
    configure.add_argument('--spec', required=True, type=Path)
    transfer = sub.add_parser('transfer', allow_abbrev=False).add_subparsers(dest='transfer_action', required=True)
    export = transfer.add_parser('export', allow_abbrev=False)
    export.add_argument('--record', required=True)
    export.add_argument('--output', required=True, type=Path)
    incoming = transfer.add_parser('receive', allow_abbrev=False)
    incoming.add_argument('--bundle', required=True, type=Path)
    incoming.add_argument('--id', required=True)
    incoming.add_argument('--account', required=True)
    incoming.add_argument('--expected', required=True, type=int)
    inspect_transfer = transfer.add_parser('inspect', allow_abbrev=False)
    inspect_transfer.add_argument('id')
    put = sub.add_parser('put', allow_abbrev=False)
    put.add_argument('--record', required=True, type=Path)
    put.add_argument('--expected', required=True, type=int)
    put.add_argument('--no-status', action='store_true', help='Skip full store status scan after commit')
    put_batch = sub.add_parser('put-batch', allow_abbrev=False,
                               help='Pre-validate and apply a guarded batch of record updates')
    put_batch.add_argument('--spec', required=True, type=Path)
    put_batch.add_argument('--no-status', action='store_true', help='Accepted for CLI symmetry; batch returns its receipt')
    show = sub.add_parser('show', allow_abbrev=False)
    show.add_argument('id')
    pin = sub.add_parser('pin', allow_abbrev=False)
    pin.add_argument('id')
    history = sub.add_parser('history', allow_abbrev=False)
    history.add_argument('id')
    history.add_argument('--limit', type=int, default=8)
    history.add_argument('--cursor', type=Path, help='cursor JSON from the preceding page')
    sub.add_parser('index-head', allow_abbrev=False,
                   help='verify and register one large legacy head for scope scans').add_argument('id')
    sub.add_parser('reindex', allow_abbrev=False,
                   help='rebuild the HMAC-verified store-local head index')
    status = sub.add_parser('status', allow_abbrev=False)
    status.add_argument('--verify-all-basis', action='store_true',
                        help='Cryptographically verify basis files for inactive non-duty records too')
    dedup = sub.add_parser('deduplicate-originals', allow_abbrev=False,
                           help='plan or execute deduplication of object-backed content-addressed originals').add_subparsers(
                               dest='dedup_action', required=True)
    dedup_plan = dedup.add_parser('plan', allow_abbrev=False)
    dedup_plan.add_argument('--prefix', default=None,
                            help='Workspace prefix for content-addressed originals (defaults to selected domain configuration)')
    dedup_exec = dedup.add_parser('execute', allow_abbrev=False)
    dedup_exec.add_argument('--plan', required=True, type=Path)
    dedup_exec.add_argument('--expected-plan', required=True)
    dedup_exec.add_argument('--reason', required=True)

    compact = sub.add_parser('compact-objects', allow_abbrev=False,
                             help='plan or execute transparent DEFLATE compaction of exchange/objects').add_subparsers(
                                 dest='compact_action', required=True)
    compact_plan = compact.add_parser('plan', allow_abbrev=False)
    compact_plan.add_argument('--min-savings-bytes', type=int, default=64)
    compact_exec = compact.add_parser('execute', allow_abbrev=False)
    compact_exec.add_argument('--plan', required=True, type=Path)
    compact_exec.add_argument('--expected-plan', required=True)
    compact_exec.add_argument('--reason', required=True)

    archive = sub.add_parser('archive-records', allow_abbrev=False,
                             help='plan or execute compressed cold-tier archival of inactive records').add_subparsers(
                                 dest='archive_action', required=True)
    archive_plan = archive.add_parser('plan', allow_abbrev=False)
    archive_plan.add_argument('--id', action='append', default=[], dest='ids')
    archive_plan.add_argument('--kind', action='append', default=[], dest='kinds')
    archive_exec = archive.add_parser('execute', allow_abbrev=False)
    archive_exec.add_argument('--plan', required=True, type=Path)
    archive_exec.add_argument('--expected-plan', required=True)
    archive_exec.add_argument('--reason', required=True)
    archive_restore = archive.add_parser('restore', allow_abbrev=False)
    archive_restore.add_argument('--id', action='append', required=True, dest='ids')
    archive_restore.add_argument('--reason', required=True)

    sub.add_parser('audit-objects', allow_abbrev=False,
                   help='read-only 9-layer transitive reachability audit over exchange/objects')
    gc = sub.add_parser('gc-objects', allow_abbrev=False,
                        help='explicitly prune verified unreachable objects from a reviewed audit plan')
    gc.add_argument('--plan', required=True, type=Path)
    gc.add_argument('--expected-plan', required=True)
    gc.add_argument('--reason', required=True)

    graph = sub.add_parser('graph', allow_abbrev=False,
                           help='read-only indexed graph queries and structural integrity lint').add_subparsers(
                               dest='graph_action', required=True)
    g_nb = graph.add_parser('neighbors', allow_abbrev=False)
    g_nb.add_argument('id')
    g_nb.add_argument('--direction', choices=('in', 'out', 'both'), default='both')
    g_nb.add_argument('--hops', type=int, default=1)
    g_nb.add_argument('--relation', action='append', default=[], dest='relations')
    g_nb.add_argument('--active-only', action='store_true')
    g_nb.add_argument('--records-only', action='store_true')
    g_links = graph.add_parser('links', allow_abbrev=False)
    g_links.add_argument('--target', required=True)
    g_links.add_argument('--relation', action='append', default=[], dest='relations')
    g_links.add_argument('--active-only', action='store_true')
    g_path = graph.add_parser('path', allow_abbrev=False)
    g_path.add_argument('src')
    g_path.add_argument('dst')
    g_path.add_argument('--max-hops', type=int, default=6)
    g_path.add_argument('--relation', action='append', default=[], dest='relations')
    g_path.add_argument('--active-only', action='store_true')
    graph.add_parser('lint', allow_abbrev=False)


def _turn_commands(sub):
    abandon = sub.add_parser('abandon-context', allow_abbrev=False)
    abandon.add_argument('turn')
    abandon.add_argument('--account', required=True)
    context = sub.add_parser('context', allow_abbrev=False)
    context.add_argument('turn')
    context.add_argument('--request', type=Path)
    context.add_argument('--task')
    context.add_argument('--project-source', action='append', default=[])
    context.add_argument('--text', action='store_true')
    scope = context.add_mutually_exclusive_group()
    scope.add_argument('--feedback', action='append', default=[])
    scope.add_argument('--without-feedback', action='store_true')
    resume = sub.add_parser('resume-context', allow_abbrev=False)
    resume.add_argument('turn')
    recover = sub.add_parser('recover', allow_abbrev=False)
    recover.add_argument('turn')
    recover.add_argument('--text', action='store_true')
    transport = sub.add_parser('recover-transport', allow_abbrev=False,
                               help='recover the captured carrier view from retained evidence')
    transport.add_argument('turn')
    closure = sub.add_parser('close-transport', allow_abbrev=False,
                             help='record a terminal command failure without a contribution')
    closure.add_argument('turn')
    closure.add_argument('--account', required=True)
    closure.add_argument('--no-status', action='store_true', help='Skip full store status scan after commit')
    reconciliation = sub.add_parser('reconcile-publication', allow_abbrev=False,
                                    help='acknowledge current target bytes without replaying publication')
    reconciliation.add_argument('turn')
    reconciliation.add_argument('--target', required=True)
    reconciliation.add_argument('--expected-sha256', required=True, metavar='SHA256_OR_absent')
    reconciliation.add_argument('--account', required=True)
    reconciliation.add_argument('--no-status', action='store_true', help='Skip full store status scan after commit')
    view = sub.add_parser('view', allow_abbrev=False)
    view.add_argument('turn')
    explain = sub.add_parser('explain-context', allow_abbrev=False,
                             help='explain the frozen input selection without reading current sources')
    explain.add_argument('turn')
    receive = sub.add_parser('receive', allow_abbrev=False)
    receive.add_argument('turn')
    receive.add_argument('--reply', type=Path)
    attribution = receive.add_mutually_exclusive_group(required=True)
    attribution.add_argument('--producer')
    attribution.add_argument('--run-nominations', action='store_true',
                             help='Collect the original carrier with its bound files and producer.')
    receive.add_argument('--proposal', action='append', default=[])
    receive.add_argument('--file-argument', action='append', dest='proposal', type=_file_argument,
                         metavar='HEX', help='Original TARGET=FILE argument bytes in a recovery command.')
    receive.add_argument('--assessment', type=Path)
    receive.add_argument('--wait', type=float, default=0)
    receive.add_argument('--no-status', action='store_true', help='Skip full store status scan after commit')
    track = sub.add_parser('track', allow_abbrev=False)
    track.add_argument('--spec', required=True, type=Path)
    track.add_argument('--no-status', action='store_true', help='Skip full store status scan after commit')
    assess = sub.add_parser('assess', allow_abbrev=False)
    assess.add_argument('turn')
    assess.add_argument('--receipt', required=True, type=Path)
    assess.add_argument('--producer', required=True)
    assess.add_argument('--no-status', action='store_true', help='Skip full store status scan after commit')
    adopt = sub.add_parser('adopt', allow_abbrev=False)
    adopt.add_argument('turn')
    adopt.add_argument('--target', required=True)
    adopt.add_argument('--reason', required=True)
    adopt.add_argument('--no-status', action='store_true', help='Skip full store status scan after commit')
    use = sub.add_parser('interpret', allow_abbrev=False)
    use.add_argument('--goal-revision', required=True, type=int)
    use.add_argument('--disposition', required=True, choices=('met', 'continue', 'wait'))
    use.add_argument('--account', required=True, type=Path, metavar='FILE',
                     help='UTF-8 account file on the host. Relative paths start at the caller\'s working directory.')
    use.add_argument('--evidence', action='append', default=[], metavar='WORK_PATH',
                     help='File path relative to the runtime work/ directory. Repeat for each piece of evidence.')
    use.add_argument('--condition')
    use.add_argument('--no-status', action='store_true', help='Skip full store status scan after commit')
    run = sub.add_parser('run', allow_abbrev=False)
    run.add_argument('turn')
    run.add_argument('--argv-json', type=Path, required=True)
    run.add_argument('--wait', type=float, default=0)
    run.add_argument('--producer')
    run.add_argument('--proposal', action='append', default=[], metavar='TARGET=FILE',
                     help='Nominate a proposal file for first receipt with a positive --wait. Repeat for each target.')
    run.add_argument('--no-status', action='store_true', help='Skip full store status scan after commit')
    retire = sub.add_parser('retire-turns', allow_abbrev=False,
                            help='plan or execute guarded retirement of completed turn working copies').add_subparsers(
                                dest='retire_turns_action', required=True)
    retire_plan = retire.add_parser('plan', allow_abbrev=False)
    retire_plan.add_argument('--turn', action='append', default=[], dest='turns',
                             help='Completed turn name to audit for working-copy retirement. Repeat for multiple turns.')
    retire_plan.add_argument('--all-eligible', action='store_true',
                             help='Audit every working copy directory under work/.system/turns/')
    retire_exec = retire.add_parser('execute', allow_abbrev=False)
    retire_exec.add_argument('--plan', required=True, type=Path)
    retire_exec.add_argument('--expected-plan', required=True)
    retire_exec.add_argument('--reason', required=True)


def _source_and_composition_commands(sub):
    catalogue = sub.add_parser('catalogue', allow_abbrev=False)
    catalogue.add_argument('id', nargs='?')
    catalogue.add_argument('--text', action='store_true')
    catalogue.add_argument('--query', default='')
    catalogue.add_argument('--kind')
    catalogue.add_argument('--limit', type=int, default=25)
    catalogue.add_argument('--offset', type=int, default=0)
    catalogue.add_argument('--content', action='store_true')
    materialize = sub.add_parser('materialize', allow_abbrev=False)
    materialize.add_argument('operation')
    discovery = sub.add_parser('discover', allow_abbrev=False)
    discovery.add_argument('--request', required=True, type=Path)
    composition = sub.add_parser('compose', allow_abbrev=False)
    composition.add_argument('--record', required=True)
    composition.add_argument('--interaction-map', action='store_true', help='group declared interactions by resource and explicit conflict')
    composition.add_argument('--boundary')
    composition.add_argument('--changed-since')
    composition.add_argument('--repo', type=Path)
    composition.add_argument('--focus')
    composition.add_argument('--limit', type=int, default=20)
    composition.add_argument('--pair-limit', type=int, default=200)
    revision = sub.add_parser('recompose', allow_abbrev=False,
                              help='Review one composition revision against current, selected evidence')
    revision.add_argument('--request', type=Path, required=True,
                          help='Proposal JSON. Optional consumer_scope lists local record IDs to inspect for bound uses.')
    revision.add_argument('--proof-store', type=Path, help='Current external owner bound by this composition')
    decision = revision.add_mutually_exclusive_group()
    decision.add_argument('--accept', metavar='REVIEW_SHA256')
    decision.add_argument('--retain', metavar='REVIEW_SHA256')
    revision.add_argument('--reason')


def _inquiry_feedback_information_commands(sub):
    inquiry = sub.add_parser('inquiry', allow_abbrev=False).add_subparsers(dest='inquiry_action', required=True)
    for name in ('open', 'evidence'):
        inquiry.add_parser(name, allow_abbrev=False).add_argument('--spec', required=True, type=Path)
    inquiry.add_parser('stages', allow_abbrev=False).add_argument('id')
    feedback = sub.add_parser('feedback', allow_abbrev=False)
    actions = feedback.add_subparsers(dest='feedback_action', required=True)
    inspect_feedback = actions.add_parser('inspect', allow_abbrev=False)
    inspect_feedback.add_argument('id')
    inspect_feedback.add_argument('--revision', type=int)
    for name in ('open', 'revise', 'observe', 'accept'):
        actions.add_parser(name, allow_abbrev=False).add_argument('--spec', required=True, type=Path)
    info = sub.add_parser('info', allow_abbrev=False)
    info_actions = info.add_subparsers(dest='information_action', required=True)
    inspect = info_actions.add_parser('inspect', allow_abbrev=False)
    inspect.add_argument('id')
    for name in ('wrap', 'migrate'):
        action = info_actions.add_parser(name, allow_abbrev=False)
        action.add_argument('--spec', required=True, type=Path)
