#!/usr/bin/env python3
"""A small persistent work context for an actual capable agent.

App 0.5.0 composes the shared POSIX continuation core and portable store binding. JSON records
carry public work; the actual host supplies interpretation and current authority.
"""
from __future__ import annotations

from contextlib import contextmanager, nullcontext
from importlib import import_module
from importlib.util import module_from_spec, spec_from_file_location
import json
import math
import os
from pathlib import Path
import shlex
import sys
import uuid

if __name__ == '__main__':
    sys.dont_write_bytecode = True

APP = Path(__file__).resolve().parent
PRODUCT = APP.parent
CORE_CATALOGUE = APP
sys.path.insert(0, str(PRODUCT / 'source_library'))
sys.path.insert(0, str(APP))
sys.path.insert(0, str(PRODUCT / 'continuation'))
from continuation_support.attention import Attention
from continuation_support import exchange
from continuation_support.exchange import Exchange
from continuation_support.ownership import NativeLayout, packet_id
from continuation_support.storage import encode, exclusive, shared, now, sha, write_json
from feedback import Feedback
from information import InformationEnvelope
from native_runtime.turn_context.capture_inputs import TurnCapture
from native_runtime.projects import ProjectSelection
from native_runtime.selection.installed_catalogue import InstalledCatalogue
from native_runtime.command_dispatch import act, emit
from native_runtime.command_parser import parser
from native_runtime.goals import GoalUse
from native_runtime.inputs import accounted_closure, read, strings, text, write_input
from native_runtime.records import RecordStore
from native_runtime.selection.domain_selection import DomainSelection, ENGINE_API, STORE_SCHEMA, pin
from native_runtime.effect_authority import NativeEffects
from native_runtime.turn_context.preparation_journal import TurnPreparation, feedback_id


VERSION = '0.5.0'
FORMAT = 'g60-agent-system/0.1'
DEPLOYMENT_API = 'agent-system-deployment/2'


def adopt_basis(exc):
    """Project only a changed basis bound by this native adopt adapter."""
    if isinstance(exc, getattr(exchange, 'PublicationBasisChanged', ())):
        return exc.native_basis
    return None


def external(root: Path) -> Path:
    root = root.expanduser().resolve()
    if root == PRODUCT.parent or root.is_relative_to(PRODUCT.parent):
        raise ValueError('runtime root must be outside the distributed product')
    return root


class System:
    def __init__(self, root: Path, *, _selection_override=None):
        self.root = external(root)
        layout = NativeLayout(self.root)
        self.persisted_descriptor = layout.read_descriptor(read)
        self.descriptor = self.persisted_descriptor if _selection_override is None else _selection_override
        self.instance_id = self.descriptor.get('instance_id')
        self.domain_selection = DomainSelection(self.root, self.descriptor)
        bindings = {kind: sha(encode(schema)) for kind, schema in self.domain_selection.schemas.items()}
        if 'schema_bindings' in self.descriptor and self.descriptor['schema_bindings'] != bindings:
            raise ValueError('selected typed-schema bindings changed')
        self.work = layout.workspace
        self.ex = Exchange(layout.exchange, authority=self)
        if self.ex.workspace != self.work.resolve():
            raise ValueError('system workspace binding changed')
        self.attention = Attention(self.ex)
        self.record_store = RecordStore(self.ex, FORMAT, InformationEnvelope(self.ex.objects))
        self.record_store.payload_validator = self.domain_selection.validate_record
        self.record_store.retention_policy = self.descriptor.get('record_retention',
            'current-use' if self.descriptor.get('store_schema') == STORE_SCHEMA else 'append-history')
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
    def initialize(cls, root: Path, goal: dict, *, instance_id=None, domains=(), catalogues=(), prepared=False,
                   record_retention='current-use'):
        root = external(root)
        if root.exists() and any(root.iterdir()) and not prepared:
            raise ValueError('initialization requires a new or empty runtime root')
        if any((root / name).exists() for name in ('system.json', 'exchange')) or (root / 'work/records/goal.json').exists():
            raise ValueError('native ownership already exists; do not reinitialize it')
        instance_id = packet_id(instance_id or uuid.uuid4().hex)
        selection = {'store_schema': STORE_SCHEMA, 'engine_api': ENGINE_API, 'instance_id': instance_id,
                     'record_retention': record_retention,
                     'domains': [pin(root, path) for path in domains],
                     'catalogues': [pin(root, path) for path in catalogues]}
        selected = DomainSelection(root, selection)
        selection['schema_bindings'] = {kind: sha(encode(schema)) for kind, schema in selected.schemas.items()}
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
            return {'initialized': str(root), 'goal': first, 'first_context': context,
                    'status': system.status()}

    # Stable application operations used by the CLI and existing Python clients.
    def show(self, ident: str) -> dict:
        return self.record_store.show(ident)

    def records(self) -> list[dict]:
        return self.record_store.all()

    def completed_import_records(self):
        """Yield the exact record identities acknowledged by this store's complete imports."""
        from native_runtime.record_import import completed_receipt_records
        return completed_receipt_records(self)

    def record_path(self, ident: str) -> Path:
        return self.record_store.path(ident)

    def put(self, record: dict, expected: int) -> dict:
        return self.record_store.put(record, expected)

    def current_use(self) -> dict:
        return self.goal_use.current_use()

    def interpret(self, revision: int, disposition: str, account: Path, evidence: list[str],
                  condition: str | None = None) -> dict:
        return self.goal_use.interpret(revision, disposition, account, evidence, condition)

    def catalogue(self, ident: str | None = None, query: str = '', kind: str | None = None,
                  limit: int = 25, offset: int = 0, content: bool = False) -> dict:
        return self.techniques.search(ident, query, kind, limit, offset, content)

    def materialize(self, operation: str) -> dict:
        return self.techniques.materialize(operation, self.ex)

    def status(self) -> dict:
        records = list(self.record_store.scoped_heads(lambda ident, kind: ident == 'goal' or kind == 'obligation'))
        goal = next(r for r in records if r['id'] == 'goal')
        owed = [r for r in goal['content'].get('obligations', [])
                if r.get('disposition') not in {'fulfilled', 'released'}]
        separate = [r['id'] for r in records if r['kind'] == 'obligation' and
                    not accounted_closure(r['content'])]
        packets = self.ex.list()
        current_use = self.goal_use.current_use(records, goal=goal)
        rows = [{'id': r['id'], 'kind': r['kind'], 'revision': r['revision'],
                 'active': r['active'], 'changed_basis': self.record_store.basis_changes(r)} for r in records]
        return {'app_version': VERSION, 'core_version': '0.4.2', 'root': str(self.root),
                'instance_id': self.instance_id, 'engine_api': ENGINE_API,
                'engine_entry': str(APP / 'system.py'), 'selection': self.domain_selection.view(),
                'deployment': self.projects.view(),
                'workspace': str(self.work), 'current_goal': goal,
                'current_use': current_use, 'owed_goal_obligations': owed,
                'owed_record_obligations': separate,
                'records': rows,
                'turns': packets, 'incomplete_turns': self._incomplete_turns(),
                'gaps': {'whole_goal_requires_interpretation': current_use['needs_interpretation'],
                         'pending_returns': [p['packet'] for p in packets
                                             if p['transport'] not in {'returned', 'terminal-without-return'}],
                         'changed_record_basis': [r['id'] for r in rows if r['changed_basis']]},
                'scope': 'Declared public work and observed local custody; no inferred semantic completeness.'}

    def _write_input(self, directory: Path, name: str, data: bytes) -> str:
        return write_input(self.work, directory, name, data)

    def command(self, *arguments: str, proposal_origin: str | None = None) -> str:
        prefix = self.command_prefix or [sys.executable, str(PRODUCT / 'manage_store.py'), '--root', str(self.root)]
        if proposal_origin is not None:
            selector = '--deployment' if self.command_prefix else '--root'
            position = prefix.index(selector)
            prefix = [*prefix[:position], '--file-origin=' + proposal_origin, *prefix[position:]]
        return shlex.join([*prefix, *arguments])

    def _incomplete_turns(self) -> dict:
        turns = self.work / '.system' / 'turns'
        return {d.name: TurnPreparation(self, d.name).state() for d in sorted(turns.glob('*'))
                if not d.name.startswith('.') and d.is_dir() and not self.ex.directory(d.name).is_dir()}

    def abandon_context(self, turn: str, account: str) -> dict:
        return TurnPreparation(self, turn).abandon(text(account, 'reconciliation account'))

    def resume_context(self, turn: str) -> dict:
        prep = TurnPreparation(self, turn)
        prep.resume()
        return self.ex.context(turn)

    def context(self, turn: str, request: dict) -> dict:
        request = dict(request)
        explicit_techniques = tuple(request.get('techniques', []))
        request['techniques'] = list(dict.fromkeys([*self.domain_selection.entry_sources,
                                                  *request.get('techniques', [])]))
        # A successor selecting a proposal needs the selected Exchange's
        # provenance rendering even when the deployment controller was retained.
        selected = self._selected_exchange() if request.get('artifacts') else nullcontext(self.ex)
        with selected as owner:
            previous, self.ex = self.ex, owner
            try:
                capture = TurnCapture(self, APP / 'GUIDE.md',
                                      entry_sources=self.domain_selection.entry_sources,
                                      explicit_techniques=explicit_techniques)
                return TurnPreparation(self, turn).prepare(capture, request)
            finally:
                self.ex = previous

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
        with self._selected_exchange('reconciliation') as owner:
            return owner.reconcile_publication(turn, target, expected_sha256, account)

    @contextmanager
    def _selected_exchange(self, namespace='exchange'):
        core = PRODUCT / 'continuation' / 'continuation_support'
        if Path(exchange.__file__).resolve() == core / 'exchange' / '__init__.py':
            yield self.ex
            return

        # A retained deployment controller can select this System while keeping
        # its own continuation package cached. Use the selected Exchange under
        # the owner lock already held by the caller.
        package_name = '_managed_ai_' + namespace + '_' + uuid.uuid4().hex
        spec = spec_from_file_location(package_name, core / '__init__.py',
                                       submodule_search_locations=[str(core)])
        if spec is None or spec.loader is None:
            raise RuntimeError('selected continuation package cannot be loaded')
        package = module_from_spec(spec)
        sys.modules[package_name] = package
        try:
            spec.loader.exec_module(package)
            selected = import_module(package_name + '.exchange')
            owner = selected.Exchange(self.ex.root, authority=self)
            if owner.workspace != self.work.resolve():
                raise ValueError('system workspace binding changed')
            yield owner
        finally:
            for name in tuple(sys.modules):
                if name == package_name or name.startswith(package_name + '.'):
                    del sys.modules[name]

    def view(self, turn: str) -> dict:
        from native_runtime.turn_context.explain_delivery import captured_view
        _, request = self.ex.load(turn)
        return captured_view(self.ex, turn, request)

    def explain_context(self, turn: str) -> dict:
        from native_runtime.turn_context.explain_delivery import explain_context
        return explain_context(self.ex, turn)

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
                # Split collection and existing dispatches have no original run binding.
                result = dict(result, recovery=self._proposal_recovery_command(turn, producer, proposals))
        return result

    def receive(self, turn: str, reply: Path | None, producer: str | None, proposals: list[str],
                assessment: Path | None = None, wait: float = 0, *, run_nominations: bool = False) -> dict:
        strings(proposals, 'proposals')
        with self._selected_exchange() as owner:
            result = self._receive(owner, turn, reply, producer, proposals, wait, run_nominations)
        if assessment is not None and result.get('received', True):
            try:
                result = dict(result, attention_account=self.assess(turn, assessment, result['producer']))
            except (OSError, ValueError, KeyError, TypeError) as exc:
                result = dict(result, attention_account={'status': 'unresolved', 'error': str(exc),
                              'recovery': self.command('assess', turn, '--receipt', str(assessment), '--producer', result['producer'])})
        return result

    def track(self, source: Path) -> dict:
        relative = self._write_input(self.work / '.system' / 'attention-specs',
                                     uuid.uuid4().hex + '.json', encode(read(source)))
        return self.attention.track(relative)

    def assess(self, turn: str, source: Path, producer: str) -> dict:
        self.ex.load(turn)
        relative = self._write_input(self.work / '.system' / 'assessments',
                                     uuid.uuid4().hex + '.json', encode(read(source)))
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

    def run(self, turn: str, argv: list[str], *, proposals: list[str] | None = None,
            producer: str | None = None, wait: float = 0) -> dict:
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
        with self._selected_exchange() as owner:
            dispatch = owner.dispatch(turn, argv, proposals=proposals, producer=producer)
            if not wait:
                return dispatch
            bound = bool(proposals)
            received = self._receive(owner, turn, None, None if bound else producer, [], wait, bound)
            return {'dispatch': dispatch, 'return': received}

    def discover(self, request: dict) -> dict:
        from context_discovery import Discovery
        return Discovery(self).search(request)

    def compose(self, record: str, focus: str | None = None, limit: int = 20,
                pair_limit: int = 200) -> dict:
        from composition import Composition
        return Composition(self).inspect(record, focus=focus, limit=limit, pair_limit=pair_limit)


def main():
    args = parser().parse_args()
    try:
        if args.action == 'init':
            goal = read(args.goal) if args.goal else {'contribution': text(args.goal_text, 'goal')}
            result = System.initialize(args.root, goal, instance_id=args.instance_id,
                                       domains=args.domain, catalogues=args.catalogue, prepared=args.prepared,
                                       record_retention=args.record_retention)
        elif args.action == 'catalogue':
            # Select the descriptor and complete sources inside the same read guard.
            with shared(external(args.root) / 'exchange' / 'owner.lock'):
                result = act(System(args.root), args)
        elif args.action in {'put', 'recompose'}:
            from continuation_support.ownership import external_root as external_exchange
            root = external(args.root)
            exchange_root = external_exchange(NativeLayout(root).exchange)
            with exclusive(exchange_root / 'owner.lock'):
                result = act(System(root), args)
        elif args.action == 'configure':
            from continuation_support.ownership import external_root as external_exchange
            from native_runtime.store_operations import StoreOperations
            root = external(args.root)
            exchange_root = external_exchange(NativeLayout(root).exchange)
            with exclusive(exchange_root / 'owner.lock'):
                override = StoreOperations.configuration_descriptor(root, read(args.spec))
                result = act(System(root, _selection_override=override), args)
        else:
            system = System(args.root)
            if args.action == 'explain-context':
                # Frozen packet/object validation needs no current-owner write lock.
                result = act(system, args)
            else:
                with exclusive(system.ex.root / 'owner.lock'):
                    result = act(system, args)
        emit(result, args)
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
    main()
