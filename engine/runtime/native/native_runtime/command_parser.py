#!/usr/bin/env python3
"""Operate a native store, its selected sources and its captured turns."""
from __future__ import annotations

import argparse
import os
from pathlib import Path


def _file_argument(value):
    """Decode the exact TARGET=FILE argv bytes in an emitted recovery command."""
    return os.fsdecode(bytes.fromhex(value))


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root', required=True, type=Path)
    sub = p.add_subparsers(dest='action', required=True)
    _store_commands(sub)
    _turn_commands(sub)
    _source_and_composition_commands(sub)
    _inquiry_feedback_information_commands(sub)
    return p


def _store_commands(sub):
    from native_runtime.store_configuration import REVIEWED_CONFIGURE

    init = sub.add_parser('init')
    goal = init.add_mutually_exclusive_group(required=True)
    goal.add_argument('--goal', type=Path)
    goal.add_argument('--goal-text')
    init.add_argument('--instance-id')
    init.add_argument('--domain', action='append', default=[], type=Path)
    init.add_argument('--catalogue', action='append', default=[], type=Path)
    init.add_argument('--prepared', action='store_true', help='Preserve prepared data in an uninitialized store')
    init.add_argument('--record-retention', choices=['current-use', 'append-history'], default='current-use')
    ingest = sub.add_parser('import')
    ingest.add_argument('--spec', required=True, type=Path)
    configure = sub.add_parser('configure', description=f'Configure a current selection or receive {REVIEWED_CONFIGURE}.')
    configure.add_argument('--spec', required=True, type=Path)
    transfer = sub.add_parser('transfer').add_subparsers(dest='transfer_action', required=True)
    export = transfer.add_parser('export')
    export.add_argument('--record', required=True)
    export.add_argument('--output', required=True, type=Path)
    incoming = transfer.add_parser('receive')
    incoming.add_argument('--bundle', required=True, type=Path)
    incoming.add_argument('--id', required=True)
    incoming.add_argument('--account', required=True)
    incoming.add_argument('--expected', required=True, type=int)
    inspect_transfer = transfer.add_parser('inspect')
    inspect_transfer.add_argument('id')
    put = sub.add_parser('put')
    put.add_argument('--record', required=True, type=Path)
    put.add_argument('--expected', required=True, type=int)
    show = sub.add_parser('show')
    show.add_argument('id')
    pin = sub.add_parser('pin')
    pin.add_argument('id')
    history = sub.add_parser('history')
    history.add_argument('id')
    history.add_argument('--limit', type=int, default=8)
    history.add_argument('--cursor', type=Path, help='cursor JSON from the preceding page')
    sub.add_parser('index-head', help='verify and register one large legacy head for scope scans').add_argument('id')
    sub.add_parser('status')


def _turn_commands(sub):
    abandon = sub.add_parser('abandon-context')
    abandon.add_argument('turn')
    abandon.add_argument('--account', required=True)
    context = sub.add_parser('context')
    context.add_argument('turn')
    context.add_argument('--request', type=Path)
    context.add_argument('--task')
    context.add_argument('--project-source', action='append', default=[])
    context.add_argument('--text', action='store_true')
    scope = context.add_mutually_exclusive_group()
    scope.add_argument('--feedback', action='append', default=[])
    scope.add_argument('--without-feedback', action='store_true')
    resume = sub.add_parser('resume-context')
    resume.add_argument('turn')
    recover = sub.add_parser('recover')
    recover.add_argument('turn')
    recover.add_argument('--text', action='store_true')
    transport = sub.add_parser('recover-transport', help='recover the captured carrier view from retained evidence')
    transport.add_argument('turn')
    closure = sub.add_parser('close-transport', help='record a terminal command failure without a contribution')
    closure.add_argument('turn')
    closure.add_argument('--account', required=True)
    reconciliation = sub.add_parser('reconcile-publication', help='acknowledge current target bytes without replaying publication')
    reconciliation.add_argument('turn')
    reconciliation.add_argument('--target', required=True)
    reconciliation.add_argument('--expected-sha256', required=True, metavar='SHA256_OR_absent')
    reconciliation.add_argument('--account', required=True)
    view = sub.add_parser('view')
    view.add_argument('turn')
    explain = sub.add_parser('explain-context', help='explain the frozen input selection without reading current sources')
    explain.add_argument('turn')
    receive = sub.add_parser('receive')
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
    track = sub.add_parser('track')
    track.add_argument('--spec', required=True, type=Path)
    assess = sub.add_parser('assess')
    assess.add_argument('turn')
    assess.add_argument('--receipt', required=True, type=Path)
    assess.add_argument('--producer', required=True)
    adopt = sub.add_parser('adopt')
    adopt.add_argument('turn')
    adopt.add_argument('--target', required=True)
    adopt.add_argument('--reason', required=True)
    use = sub.add_parser('interpret')
    use.add_argument('--goal-revision', required=True, type=int)
    use.add_argument('--disposition', required=True, choices=('met', 'continue', 'wait'))
    use.add_argument('--account', required=True, type=Path, metavar='FILE',
                     help='UTF-8 account file on the host. Relative paths start at the caller\'s working directory.')
    use.add_argument('--evidence', action='append', default=[], metavar='WORK_PATH',
                     help='File path relative to the runtime work/ directory. Repeat for each piece of evidence.')
    use.add_argument('--condition')
    run = sub.add_parser('run')
    run.add_argument('turn')
    run.add_argument('--argv-json', type=Path, required=True)
    run.add_argument('--wait', type=float, default=0)
    run.add_argument('--producer')
    run.add_argument('--proposal', action='append', default=[], metavar='TARGET=FILE',
                     help='Nominate a proposal file for first receipt with a positive --wait. Repeat for each target.')


def _source_and_composition_commands(sub):
    catalogue = sub.add_parser('catalogue')
    catalogue.add_argument('id', nargs='?')
    catalogue.add_argument('--text', action='store_true')
    catalogue.add_argument('--query', default='')
    catalogue.add_argument('--kind')
    catalogue.add_argument('--limit', type=int, default=25)
    catalogue.add_argument('--offset', type=int, default=0)
    catalogue.add_argument('--content', action='store_true')
    materialize = sub.add_parser('materialize')
    materialize.add_argument('operation')
    discovery = sub.add_parser('discover')
    discovery.add_argument('--request', required=True, type=Path)
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


def _inquiry_feedback_information_commands(sub):
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
    info = sub.add_parser('info')
    info_actions = info.add_subparsers(dest='information_action', required=True)
    inspect = info_actions.add_parser('inspect')
    inspect.add_argument('id')
    for name in ('wrap', 'migrate'):
        action = info_actions.add_parser(name)
        action.add_argument('--spec', required=True, type=Path)
