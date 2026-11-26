#!/usr/bin/env python3
"""A small persistent work context for an actual capable agent.

App 0.5.0 composes the shared continuation core and portable store binding. JSON records
carry public work; the actual host supplies interpretation and current authority.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import shlex
import sys
from typing import Any, Callable
import uuid

if __name__ == '__main__':
    sys.dont_write_bytecode = True
    _src_root = str(Path(__file__).resolve().parents[2])
    if _src_root not in sys.path:
        sys.path.insert(0, _src_root)

from managed_ai.core import exchange
from managed_ai.core.attention import Attention
from managed_ai.core.exchange import Exchange
from managed_ai.core.formats import DEPLOYMENT_FORMAT, STORE_FORMAT
from managed_ai.core.ownership import NativeLayout, external_root, packet_id
from managed_ai.core.storage import (
    AdmissionContended,
    admission_deadline,
    encode,
    exclusive,
    is_ordinary_file_stat,
    now,
    owner_admission,
    sha,
    shared,
    write_json,
)
from managed_ai.store.cli.command_parser import is_readonly_action, parser
from managed_ai.store.governance.feedback import Feedback
from managed_ai.store.governance.goals import GoalUse
from managed_ai.store.governance.projects import ProjectSelection
from managed_ai.store.protocol.inputs import accounted_closure, read, strings, text, write_input
from managed_ai.store.query.information import InformationEnvelope
from managed_ai.store.records.records import RecordStore
from managed_ai.store.selection.domain_selection import DomainSelection, ENGINE_API, STORE_SCHEMA, pin
from managed_ai.store.selection.installed_catalogue import InstalledCatalogue
from managed_ai.store.selection.store_configuration import configuration_descriptor
from managed_ai.store.turns.effect_authority import NativeEffects
from managed_ai.store.turns.preparation_journal import TurnPreparation, feedback_id

APP = Path(__file__).resolve().parent
ENGINE_ROOT = APP.parent
CORE_CATALOGUE = APP

VERSION = '0.5.0'
FORMAT = STORE_FORMAT
# Literal assignment preserved for static AST verification in controller.code_identity().
DEPLOYMENT_API = 'agent-system-deployment/2'
assert DEPLOYMENT_API == DEPLOYMENT_FORMAT


class NativeError(ValueError):
    """Structured refusal raised by native store operations."""


def adopt_basis(exc):
    """Project only a changed basis bound by this native adopt adapter."""
    if isinstance(exc, getattr(exchange, 'PublicationBasisChanged', ())):
        return exc.native_basis
    return None


def _resolve_guide_file() -> Path:
    local = APP / 'GUIDE.md'
    if local.is_file():
        return local

    if (
        APP.name == 'store'
        and len(APP.parents) >= 3
        and APP.parent.name == 'managed_ai'
        and APP.parents[1].name == 'src'
    ):
        fallback = APP.parents[2] / 'docs/engine/store/GUIDE.md'
        if fallback.is_file():
            return fallback

    return local


def external(root: Path) -> Path:
    return external_root(root)


def _act(*args, **kwargs):
    from managed_ai.store.cli.command_dispatch import act

    return act(*args, **kwargs)


def _emit(*args, **kwargs):
    from managed_ai.store.cli.command_dispatch import emit

    return emit(*args, **kwargs)


class System:
    def __init__(self, root: Path, *, _selection_override=None):
        self.root = external(root)
        layout = NativeLayout(self.root)
        self.persisted_descriptor = layout.read_descriptor(read)
        self.descriptor = self.persisted_descriptor if _selection_override is None else _selection_override
        self.instance_id = self.descriptor.get('instance_id')
        self.domain_selection = DomainSelection(self.root, self.descriptor)

        bindings = self.domain_selection.schema_bindings
        if 'schema_bindings' in self.descriptor and self.descriptor['schema_bindings'] != bindings:
            raise ValueError('selected typed-schema bindings changed')

        self.work = layout.workspace
        self.ex = Exchange(layout.exchange, authority=self)
        if self.ex.workspace != self.work.resolve():
            raise ValueError('system workspace binding changed')

        self.attention = Attention(self.ex)
        self.record_store = RecordStore(
            self.ex,
            FORMAT,
            InformationEnvelope(self.ex.objects),
            content_addressed_basis_prefixes=self.domain_selection.content_addressed_basis_prefixes,
        )
        self.record_store.payload_validator = self.domain_selection.validate_record
        self.record_store.retention_policy = self.descriptor.get(
            'record_retention',
            'current-use' if self.descriptor.get('store_schema') == STORE_SCHEMA else 'append-history',
        )
        if self.record_store.retention_policy not in {'current-use', 'append-history'}:
            raise ValueError('unsupported native record retention policy')

        self.techniques = InstalledCatalogue(CORE_CATALOGUE, self.domain_selection)
        self.domain_selection.validate_catalogue({e['id']: e for e in self.techniques.index()['entries']})
        self.goal_use = GoalUse(self.record_store, self.ex)
        self.effects = NativeEffects(self.root, self.view, self.record_store, Feedback(self), self.attention)
        self.projects = ProjectSelection()
        self.command_prefix = None

    @property
    def deployment(self):
        return self.projects.current

    @deployment.setter
    def deployment(self, selected):
        self.projects.current = selected

    goal_record = staticmethod(RecordStore.goal_record)

    def authorize_dispatch(self, owner, packet):
        self.effects.authorize_dispatch(owner, packet)

    def authorize_publication(self, owner, packet, target):
        self.effects.authorize_publication(owner, packet, target)

    @classmethod
    def initialize(
        cls,
        root: Path,
        goal: dict,
        *,
        instance_id=None,
        domains=(),
        catalogues=(),
        prepared: bool = False,
        record_retention: str = 'current-use',
    ):
        root = external(root)
        if root.exists() and any(root.iterdir()) and not prepared:
            raise ValueError('initialization requires a new or empty runtime root')
        if any((root / name).exists() for name in ('system.json', 'exchange')) or (root / 'work/records/goal.json').exists():
            raise ValueError('native ownership already exists; do not reinitialize it')

        instance_id = packet_id(instance_id or uuid.uuid4().hex)
        selection = {
            'store_schema': STORE_SCHEMA,
            'engine_api': ENGINE_API,
            'instance_id': instance_id,
            'record_retention': record_retention,
            'domains': [pin(root, path) for path in domains],
            'catalogues': [pin(root, path) for path in catalogues],
        }
        selected = DomainSelection(root, selection)
        selection['schema_bindings'] = selected.schema_bindings
        available = {e['id']: e for e in InstalledCatalogue(CORE_CATALOGUE, selected).index()['entries']}
        selected.validate_catalogue(available)

        if record_retention not in {'current-use', 'append-history'}:
            raise ValueError('unknown native record retention policy')

        goal = RecordStore.goal_record(goal)
        root.mkdir(parents=True, exist_ok=True)
        layout = NativeLayout(root)
        work = layout.workspace
        work.mkdir(exist_ok=prepared)
        for child in ('records', 'interpretations', '.system', 'sources', 'outputs'):
            (work / child).mkdir(exist_ok=prepared)

        layout.write_descriptor(app_version=VERSION, core_version='0.4.2', **selection)
        Exchange.initialize(layout.exchange, work, instance_id=instance_id)

        system = cls(root)
        with exclusive(system.ex.root / 'owner.lock'):
            first = system.put(goal, 0)
            context = system.context('start', {'task': goal['content']['contribution']})
            return {
                'initialized': str(root),
                'goal': first,
                'first_context': context,
                'status': system.status(),
            }

    # Stable application operations used by the CLI and existing Python clients.
    def show(self, ident: str) -> dict:
        return self.record_store.show(ident)

    def records(self) -> list[dict]:
        return self.record_store.all()

    def completed_import_records(self):
        """Yield the exact record identities acknowledged by this store's complete imports."""
        from managed_ai.store.records.record_import import completed_receipt_records

        return completed_receipt_records(self)

    def record_path(self, ident: str) -> Path:
        return self.record_store.path(ident)

    def put(self, record: dict, expected: int) -> dict:
        return self.record_store.put(record, expected)

    def put_batch(self, spec: dict) -> dict:
        from managed_ai.store.records.record_batch import put_batch

        return put_batch(self, spec)

    def reindex(self) -> dict:
        return self.record_store.reindex()

    def current_use(self) -> dict:
        return self.goal_use.current_use()

    def interpret(
        self,
        revision: int,
        disposition: str,
        account: Path,
        evidence: list[str],
        condition: str | None = None,
    ) -> dict:
        return self.goal_use.interpret(revision, disposition, account, evidence, condition)

    def catalogue(
        self,
        ident: str | None = None,
        query: str = '',
        kind: str | None = None,
        limit: int = 25,
        offset: int = 0,
        content: bool = False,
    ) -> dict:
        return self.techniques.search(ident, query, kind, limit, offset, content)

    def materialize(self, operation: str) -> dict:
        return self.techniques.materialize(operation, self.ex)

    def _load_status_records(self, verify_all_basis: bool) -> list[dict]:
        needs_body = lambda ident, kind: ident == 'goal' or kind == 'obligation'
        if verify_all_basis:
            return list(self.record_store.scoped_heads(needs_body))

        summaries = self.record_store.indexed_summaries()
        if summaries is None:
            return list(self.record_store.scoped_heads(needs_body))

        needed = [ident for ident, entry in summaries.items() if needs_body(ident, entry['kind'])]
        full_by_id = {rec['id']: rec for rec in self.record_store.scan_records(needed)}
        return [full_by_id[ident] if ident in full_by_id else summaries[ident] for ident in summaries]

    def _status_record_rows(self, records: list[dict], verify_all_basis: bool) -> tuple[list[dict], int]:
        basis_paths: list[str] = []
        checked_ids: set[str] = set()
        deferred_inactive = 0

        for rec in records:
            should_check = (
                verify_all_basis
                or rec['id'] == 'goal'
                or rec['kind'] == 'obligation'
                or rec.get('active', True)
            )
            if should_check:
                checked_ids.add(rec['id'])
                for item in rec['basis']:
                    if not item['path'].startswith('object:'):
                        basis_paths.append(item['path'])
            elif rec['basis']:
                deferred_inactive += 1

        resolved_hashes = self.ex.hash_workspace_paths(basis_paths) if basis_paths else {}
        verified_objects: set[tuple[str, int]] = set()
        rows = []

        for rec in records:
            if rec['id'] in checked_ids:
                changed = self.record_store.basis_changes(
                    rec,
                    resolved_hashes=resolved_hashes,
                    verified_objects=verified_objects,
                )
            elif rec['basis'] and all(item['path'] in resolved_hashes for item in rec['basis']):
                changed = self.record_store.basis_changes(
                    rec,
                    resolved_hashes=resolved_hashes,
                    verified_objects=verified_objects,
                )
                deferred_inactive -= 1
            else:
                changed = []

            rows.append({
                'id': rec['id'],
                'kind': rec['kind'],
                'revision': rec['revision'],
                'active': rec['active'],
                'changed_basis': changed,
            })

        return rows, deferred_inactive

    def status(self, *, verify_all_basis: bool = False) -> dict:
        records = self._load_status_records(verify_all_basis)
        goal = next(r for r in records if r['id'] == 'goal')
        owed = [
            r for r in goal['content'].get('obligations', [])
            if r.get('disposition') not in {'fulfilled', 'released'}
        ]
        separate = [
            r['id'] for r in records
            if r['kind'] == 'obligation' and not accounted_closure(r['content'])
        ]
        packets = self.ex.list()
        current_use = self.goal_use.current_use(records, goal=goal)
        rows, deferred_inactive = self._status_record_rows(records, verify_all_basis)

        full_basis = verify_all_basis or deferred_inactive == 0
        scope_text = (
            'Declared public work and observed local custody; no inferred semantic completeness.'
            if full_basis
            else (
                'Declared public work and observed local custody; inactive non-duty record basis hashes '
                'deferred (pass --verify-all-basis for full basis verification); no inferred semantic completeness.'
            )
        )
        result = {
            'app_version': VERSION,
            'core_version': '0.4.2',
            'root': str(self.root),
            'instance_id': self.instance_id,
            'engine_api': ENGINE_API,
            'engine_entry': str(APP / 'system.py'),
            'selection': self.domain_selection.view(),
            'deployment': self.projects.view(),
            'workspace': str(self.work),
            'current_goal': goal,
            'current_use': current_use,
            'owed_goal_obligations': owed,
            'owed_record_obligations': separate,
            'records': rows,
            'turns': packets,
            'incomplete_turns': self._incomplete_turns(),
            'gaps': {
                'whole_goal_requires_interpretation': current_use['needs_interpretation'],
                'pending_returns': [
                    p['packet'] for p in packets
                    if p['transport'] not in {'returned', 'terminal-without-return'}
                ],
                'changed_record_basis': [r['id'] for r in rows if r['changed_basis']],
            },
            'basis_verification_scope': 'full' if full_basis else 'active-and-governing',
            'scope': scope_text,
        }
        if not full_basis:
            result['deferred_inactive_basis_records'] = deferred_inactive
        return result

    def _write_input(self, directory: Path, name: str, data: bytes) -> str:
        return write_input(self.work, directory, name, data)

    def command(self, *arguments: str, proposal_origin: str | None = None) -> str:
        prefix = self.command_prefix or [sys.executable, str(APP / 'manage_store.py'), '--root', str(self.root)]
        if proposal_origin is not None:
            selector = '--deployment' if self.command_prefix else '--root'
            position = prefix.index(selector)
            prefix = [*prefix[:position], '--file-origin=' + proposal_origin, *prefix[position:]]
        return shlex.join([*prefix, *arguments])

    def _incomplete_turns(self) -> dict:
        turns = self.work / '.system' / 'turns'
        return {
            d.name: TurnPreparation(self, d.name).state()
            for d in sorted(turns.glob('*'))
            if not d.name.startswith('.') and d.is_dir() and not self.ex.directory(d.name).is_dir()
        }

    def abandon_context(self, turn: str, account: str) -> dict:
        return TurnPreparation(self, turn).abandon(text(account, 'reconciliation account'))

    def resume_context(self, turn: str) -> dict:
        prep = TurnPreparation(self, turn)
        prep.resume()
        return self.ex.context(turn)

    def context(self, turn: str, request: dict) -> dict:
        from managed_ai.store.turns.capture_inputs import TurnCapture

        request = dict(request)
        explicit_techniques = tuple(request.get('techniques', []))
        request['techniques'] = list(dict.fromkeys([
            *self.domain_selection.entry_sources,
            *request.get('techniques', []),
        ]))
        capture = TurnCapture(
            self,
            _resolve_guide_file(),
            entry_sources=self.domain_selection.entry_sources,
            explicit_techniques=explicit_techniques,
        )
        return TurnPreparation(self, turn).prepare(capture, request)

    def recover(self, turn: str) -> dict:
        prep = TurnPreparation(self, turn)
        if prep.directory.is_dir() and not self.ex.directory(turn).exists():
            raise ValueError(prep.recovery_direction())
        return self.ex.context(turn)

    def recover_transport(self, turn: str) -> dict | None:
        return self.ex.recover_transport(turn)

    def close_transport(self, turn: str, account: str) -> dict:
        return self.ex.close_transport(turn, account)

    def reconcile_publication(self, turn: str, target: str, expected_sha256: str | None, account: str) -> dict:
        return self.ex.reconcile_publication(turn, target, expected_sha256, account)

    def view(self, turn: str) -> dict:
        from managed_ai.store.turns.explain_delivery import captured_view

        _, request = self.ex.load(turn)
        return captured_view(self.ex, turn, request)

    def explain_context(self, turn: str) -> dict:
        from managed_ai.store.turns.explain_delivery import explain_context

        return explain_context(self.ex, turn)

    def plan_turn_retirement(
        self,
        *,
        turns: list[str] | tuple[str, ...] | None = None,
        all_eligible: bool = False,
    ) -> dict:
        from managed_ai.store.turns.turn_retirement import plan_turn_retirement

        return plan_turn_retirement(self, turns=turns, all_eligible=all_eligible)

    def execute_turn_retirement(self, *, plan: dict, expected_plan: str, reason: str) -> dict:
        from managed_ai.store.turns.turn_retirement import execute_turn_retirement

        return execute_turn_retirement(self, plan=plan, expected_plan=expected_plan, reason=reason)

    def restore_archived_records(self, *, ids: list[str], reason: str) -> dict:
        from managed_ai.store.records.cold_archive import restore_archived_records

        return restore_archived_records(self, ids=ids, reason=reason)

    def _proposal_recovery_command(self, turn: str, producer: str, proposals: list[str]) -> str:
        origin = None
        if any(not os.path.isabs(item.partition('=')[2]) for item in proposals):
            cwd = os.stat('.')
            origin = f'{cwd.st_dev}:{cwd.st_ino}:{os.getcwdb().hex()}'
        arguments = ['--file-argument=' + os.fsencode(item).hex() for item in proposals]
        return self.command('receive', '--producer=' + producer, *arguments, '--', turn, proposal_origin=origin)

    def _receive(self, owner, turn, reply, producer, proposals, wait, run_nominations):
        result = owner.receive(turn, reply, producer, proposals, wait=wait, run_nominations=run_nominations)
        if result.get('received') is False and result['carrier']['status'] != 'launch-failed':
            if result.get('run_nominations'):
                result = dict(result, recovery=self.command('receive', '--run-nominations', '--', turn))
            elif proposals:
                result = dict(result, recovery=self._proposal_recovery_command(turn, producer, proposals))
        return result

    def receive(
        self,
        turn: str,
        reply: Path | None,
        producer: str | None,
        proposals: list[str],
        assessment: Path | None = None,
        wait: float = 0,
        *,
        run_nominations: bool = False,
    ) -> dict:
        strings(proposals, 'proposals')
        result = self._receive(self.ex, turn, reply, producer, proposals, wait, run_nominations)

        if assessment is not None and result.get('received', True):
            try:
                result = dict(result, attention_account=self.assess(turn, assessment, result['producer']))
            except (OSError, ValueError, KeyError, TypeError) as exc:
                result = dict(
                    result,
                    attention_account={
                        'status': 'unresolved',
                        'error': str(exc),
                        'recovery': self.command(
                            'assess', turn, '--receipt', str(assessment), '--producer', result['producer'],
                        ),
                    },
                )
        return result

    def track(self, source: Path) -> dict:
        relative = self._write_input(
            self.work / '.system' / 'attention-specs',
            uuid.uuid4().hex + '.json',
            encode(read(source)),
        )
        return self.attention.track(relative)

    def assess(self, turn: str, source: Path, producer: str) -> dict:
        self.ex.load(turn)
        relative = self._write_input(
            self.work / '.system' / 'assessments',
            uuid.uuid4().hex + '.json',
            encode(read(source)),
        )
        return self.attention.record(feedback_id(turn), relative, producer)

    def adopt(self, turn: str, target: str, reason: str) -> dict:
        try:
            return self.ex.publish(turn, target, reason)
        except ValueError as exc:
            if isinstance(exc, getattr(exchange, 'PublicationBasisChanged', ())) and exc.basis is not None:
                if self.instance_id is None:
                    exc.native_basis = exc.basis
                else:
                    try:
                        exc.native_basis = dict(exc.basis, instance_id=packet_id(self.instance_id))
                    except (ValueError, TypeError):
                        pass
            raise

    def run(
        self,
        turn: str,
        argv: list[str],
        *,
        proposals: list[str] | None = None,
        producer: str | None = None,
        wait: float = 0,
    ) -> dict:
        if not isinstance(argv, list) or not argv or any(not isinstance(a, str) for a in argv):
            raise ValueError('argv must be a nonempty ordered list of strings')
        if not argv[0] or any('\0' in a for a in argv):
            raise ValueError('argv must start with a nonempty executable and contain no NUL characters')
        if not math.isfinite(wait) or wait < 0:
            raise ValueError('run wait must be finite and nonnegative')

        if not proposals and not wait:
            return self.ex.dispatch(turn, argv)

        proposals = strings(proposals or [], 'proposals')
        if wait:
            producer = text(producer, 'positive run --wait producer')
        dispatch = self.ex.dispatch(turn, argv, proposals=proposals, producer=producer)
        if not wait:
            return dispatch

        bound = bool(proposals)
        received = self._receive(self.ex, turn, None, None if bound else producer, [], wait, bound)
        return {'dispatch': dispatch, 'return': received}

    def discover(self, request: dict) -> dict:
        from managed_ai.store.query.context_discovery import Discovery

        return Discovery(self).search(request)

    def compose(
        self,
        record: str,
        focus: str | None = None,
        limit: int = 20,
        pair_limit: int = 200,
    ) -> dict:
        from managed_ai.store.composition.composition import Composition

        return Composition(self).inspect(record, focus=focus, limit=limit, pair_limit=pair_limit)


@dataclass(frozen=True)
class EngineEntrypoint:
    """Typed boundary consumed by managed_ai.deployment.controller instead of raw runpy dicts."""

    deployment_api: str
    engine_api: str
    store_schema: str
    system_cls: type[System]
    parser: Callable[[], Any]
    is_readonly_action: Callable[[Any], bool]
    act: Callable[..., Any]
    emit: Callable[[Any, Any], None]
    adopt_basis: Callable[[BaseException], Any]
    native_layout: type[NativeLayout]
    external: Callable[[Path], Path]
    configuration_descriptor: Callable[[Path, dict], dict]


ENTRYPOINT = EngineEntrypoint(
    deployment_api=DEPLOYMENT_API,
    engine_api=ENGINE_API,
    store_schema=STORE_SCHEMA,
    system_cls=System,
    parser=parser,
    is_readonly_action=is_readonly_action,
    act=_act,
    emit=_emit,
    adopt_basis=adopt_basis,
    native_layout=NativeLayout,
    external=external,
    configuration_descriptor=configuration_descriptor,
)


def _admission_deadline(forwarded=None):
    return admission_deadline(forwarded)


def _proof_lock_candidate(root: Path, proof_store: Path | None) -> Path | None:
    if proof_store is None:
        return None

    try:
        resolved = Path(proof_store).expanduser().resolve(strict=True)
    except OSError:
        return None
    if resolved == root:
        return None

    try:
        proof_exchange = external_root(NativeLayout(resolved).exchange)
        lock_path = proof_exchange / 'owner.lock'
        if not is_ordinary_file_stat(lock_path.lstat()):
            return None
    except (OSError, ValueError):
        return None

    return lock_path


def main(args=None) -> None:
    if args is None:
        args = parser().parse_args()

    try:
        if args.action == 'init':
            goal = read(args.goal) if args.goal else {'contribution': text(args.goal_text, 'goal')}
            result = System.initialize(
                args.root,
                goal,
                instance_id=args.instance_id,
                domains=args.domain,
                catalogues=args.catalogue,
                prepared=args.prepared,
                record_retention=args.record_retention,
            )
        elif args.action == 'explain-context':
            result = _act(System(args.root), args)
        else:
            root = external(args.root)
            exchange_root = external_root(NativeLayout(root).exchange)
            primary_lock = exchange_root / 'owner.lock'
            guard = shared if is_readonly_action(args) else exclusive
            proof_lock = (
                _proof_lock_candidate(root, getattr(args, 'proof_store', None))
                if args.action == 'recompose' else None
            )
            deadline = admission_deadline(getattr(args, 'admission_deadline', None))

            def acquire(stack):
                if proof_lock is None:
                    stack.enter_context(guard(primary_lock))
                    return

                locks = sorted(
                    [
                        (str(primary_lock), primary_lock, guard, False),
                        (str(proof_lock), proof_lock, shared, True),
                    ],
                    key=lambda item: item[0],
                )
                for _, path, lock_fn, readonly_open in locks:
                    if readonly_open:
                        stack.enter_context(lock_fn(path, allow_readonly_open=True))
                    else:
                        stack.enter_context(lock_fn(path))

            with owner_admission(acquire, deadline=deadline):
                if args.action == 'configure':
                    override = configuration_descriptor(root, read(args.spec))
                    result = _act(System(root, _selection_override=override), args)
                else:
                    result = _act(System(root), args, proof_store_locked=proof_lock is not None)

        _emit(result, args)
    except AdmissionContended as exc:
        print(json.dumps(exc.result(args.action)), file=sys.stderr)
        raise SystemExit(75)
    except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
        failure = {'status': 'unresolved', 'operation': args.action, 'error': str(exc)}
        if args.action == 'adopt':
            try:
                basis = adopt_basis(exc)
                if basis is not None:
                    rendered = json.dumps(dict(failure, publication_basis=basis), allow_nan=False)
                else:
                    rendered = json.dumps(failure)
            except (OSError, ValueError, KeyError, TypeError, RuntimeError):
                rendered = json.dumps(failure)
        else:
            failure.update(getattr(exc, 'details', {}))
            rendered = json.dumps(failure)
        print(rendered, file=sys.stderr)
        raise SystemExit(2)


if __name__ == '__main__':
    sys.dont_write_bytecode = True
    main()
