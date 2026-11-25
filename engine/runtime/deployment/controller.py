#!/usr/bin/env python3
"""Select one shared native engine and one schema-governed store by reference.

Deployment metadata never copies an engine, project or store. Native captures
retain the selected occurrence needed by their receiving work.
"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
import argparse
import ast
import errno
import hashlib
import json
import math
import os
import runpy
import shlex
import stat
import sys
import time
from importlib.machinery import EXTENSION_SUFFIXES

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / 'continuation'))
from continuation_support.storage import AdmissionContended, encode, exclusive, owner_admission, shared, atomic_bytes, sha
from continuation_support.ownership import packet_id

VERSION = '0.3.0'
FORMAT = 'agent-system-deployment/2'
ENGINE_API = 'managed-ai-runtime/1'
STORE_SCHEMA = 'managed-ai-store/1'
NATIVE_ENTRY = Path('runtime/native/system.py')
NATIVE_CONTRACT = Path('runtime/native/native_runtime/selection/domain_selection.py')
PORTABLE_ENTRY = Path('prototype/agent-system/system.py')
PORTABLE_CONTRACT = Path('prototype/agent-system/native_runtime/domains.py')
NON_SOURCE_SUFFIXES = ('.pyc', '.pyo', '.pyd', '.so', *EXTENSION_SUFFIXES)


def _unique_json(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate JSON key in descriptor: ' + key)
        result[key] = value
    return result


def _descriptor_bytes(path):
    if path.is_symlink() or not path.is_file():
        raise ValueError('ordinary descriptor required: ' + str(path))
    return path.read_bytes()


def _descriptor_object(path, raw):
    value = json.loads(
        raw.decode('utf-8'),
        object_pairs_hook=_unique_json,
        parse_constant=lambda item: (_ for _ in ()).throw(ValueError('nonfinite JSON in descriptor: ' + item)),
    )
    if not isinstance(value, dict):
        raise ValueError('expected a JSON object in descriptor: ' + str(path))
    return value


def read(path):
    return _descriptor_object(path, _descriptor_bytes(path))


def _ordinary_mode(path, info):
    if stat.S_ISLNK(info.st_mode) or (
            getattr(info, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
        raise ValueError(f'shared engine may not use symbolic links or reparse points: {path}')
    if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
        raise ValueError(f'ordinary shared engine file or directory required: {path}')
    return info.st_mode


def _engine_directory(path):
    path = Path(path)
    if not path.is_absolute():
        if path.anchor:
            raise ValueError(f'fully qualified or relative shared engine path required: {path}')
        path = Path.cwd() / path
    current = Path(path.anchor)
    _ordinary_mode(current, current.lstat())
    for part in path.parts[1:]:
        if part == '..':
            current = current.parent
            continue
        current /= part
        if not stat.S_ISDIR(_ordinary_mode(current, current.lstat())):
            raise ValueError(f'ordinary shared engine directory required: {current}')
    return current.resolve(strict=True)


def code_identity(engine):
    engine = _engine_directory(engine)
    paths = []
    directories = [(engine, False)]
    while directories:
        directory, cache = directories.pop()
        with os.scandir(directory) as entries:
            for entry in entries:
                path = directory / entry.name
                mode = _ordinary_mode(path, entry.stat(follow_symlinks=False))
                if stat.S_ISDIR(mode):
                    directories.append((path, cache or path.name.casefold() == '__pycache__'))
                elif cache:
                    if path.suffix.lower() != '.pyc':
                        raise ValueError(f'ordinary bytecode cache files required below __pycache__: {path}')
                elif path.name.lower().endswith(NON_SOURCE_SUFFIXES):
                    raise ValueError(f'shared engine requires Python source for executable modules: {path}')
                elif path.match('*.py'):
                    paths.append(path)
    sorted_paths = sorted(paths)
    interfaces = [(PORTABLE_ENTRY, PORTABLE_CONTRACT)]
    if (engine / NATIVE_ENTRY).is_file():
        schema = NATIVE_CONTRACT if (engine / NATIVE_CONTRACT).is_file() else PORTABLE_CONTRACT
        interfaces.append((NATIVE_ENTRY, schema))
    declaration_paths = {path for pair in interfaces for path in pair}

    def hash_one(path):
        relative = path.relative_to(engine)
        raw = path.read_bytes()
        return relative, {'path': relative.as_posix(), 'sha256': sha(raw)}, (raw if relative in declaration_paths else None)

    rows, declarations = [], {}
    if len(sorted_paths) >= 16:
        with ThreadPoolExecutor(max_workers=8) as pool:
            hashed = list(pool.map(hash_one, sorted_paths))
    else:
        hashed = [hash_one(path) for path in sorted_paths]
    for relative, row, raw in hashed:
        rows.append(row)
        if raw is not None:
            declarations[relative] = raw
    if declaration_paths != declarations.keys():
        raise ValueError('select the current engine root containing its native entry and API declarations')

    def declared(path):
        result = {}
        try:
            declaration = ast.parse(declarations[path], filename=str(engine / path))
        except SyntaxError as exc:
            raise ValueError(f'invalid engine API declaration in {engine / path}: {exc.msg}') from exc
        for node in declaration.body:
            if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                try: result[node.targets[0].id] = ast.literal_eval(node.value)
                except (ValueError, TypeError): pass
        return result

    for entry, schema in interfaces:
        contract = declared(schema)
        if (contract.get('ENGINE_API') != ENGINE_API or contract.get('STORE_SCHEMA') != STORE_SCHEMA or
                declared(entry).get('DEPLOYMENT_API') != FORMAT):
            raise ValueError('selected engine declares incompatible storage/deployment APIs')
    return sha(encode(rows))


def runtime_binding(root):
    config = read(root / 'system.json')
    binding = read(root / 'exchange/workspace.json')
    if (config.get('format') != 'g60-agent-system/0.1' or
            config.get('store_schema') != STORE_SCHEMA or config.get('engine_api') != ENGINE_API):
        raise ValueError('store/engine schema incompatibility; explicit import or migration required')
    packet_id(config['instance_id'])
    if (type(binding.get('version')) is not int or binding['version'] != 2 or
            binding.get('workspace_relative') != '../work' or
            binding.get('instance_id') != config['instance_id']):
        raise ValueError('portable store binding differs')
    return {'instance_id': config['instance_id'], 'store_schema': STORE_SCHEMA,
            'engine_api': ENGINE_API}


class ActivationIncomplete(RuntimeError):
    def __init__(self, failed_step, descriptor_sha256, cause):
        super().__init__(f'activation committed but {failed_step} failed: {cause}')
        self.failed_step = failed_step
        self.descriptor_sha256 = descriptor_sha256


class DeploymentAdoptBasisChanged(ValueError):
    def __init__(self, message, basis):
        super().__init__(message)
        self.basis = basis


class DeploymentSelection:
    """Read or replace deployment metadata, including when the incumbent code has drifted."""

    def __init__(self, root, *, descriptor=None):
        self.root = Path(root).resolve()
        path = self.root / 'deployment.json'
        raw = _descriptor_bytes(path) if descriptor is None else descriptor
        self.config = _descriptor_object(path, raw)
        if self.config.get('format') != FORMAT:
            raise ValueError('copied-engine deployment is not supported; create a shared reference deployment')
        self.descriptor_sha256 = sha(raw)

    def _require_current(self):
        if sha(_descriptor_bytes(self.root / 'deployment.json')) != self.descriptor_sha256:
            raise ValueError('deployment selection revision conflict; reopen the deployment')

    def stage(self, edition, engine):
        self._require_current()
        packet_id(edition)
        engine = _engine_directory(engine)
        value = dict(self.config, edition=edition,
                     engine={'path': os.path.relpath(engine, self.root), 'code_sha256': code_identity(engine),
                             'engine_api': ENGINE_API})
        atomic_bytes(self.root / 'candidate.json', encode(value))
        return {'edition': edition, 'candidate_sha256': sha(encode(value)), 'engine': str(engine)}

    def activate(self, edition, expected, manifest_sha, reason):
        if sha(_descriptor_bytes(self.root / 'deployment.json')) != expected:
            raise ValueError('deployment selection revision conflict')
        candidate_path = self.root / 'candidate.json'
        candidate_raw = _descriptor_bytes(candidate_path)
        candidate = _descriptor_object(candidate_path, candidate_raw)
        if candidate.get('edition') != edition or (
                sha(candidate_raw) != manifest_sha and sha(encode(candidate)) != manifest_sha):
            raise ValueError('staged engine reference differs')
        if not reason.strip():
            raise ValueError('new code selection needs a compatibility/receiving account')
        if candidate.get('format') != FORMAT or candidate['engine'].get('engine_api') != ENGINE_API:
            raise ValueError('candidate declares an incompatible deployment/engine API')
        engine = _engine_directory(self.root / candidate['engine']['path'])
        if code_identity(engine) != candidate['engine']['code_sha256']:
            raise ValueError('staged code changed')
        binding = runtime_binding((self.root / candidate['store']['path']).resolve())
        if any(candidate['store'].get(k) != v for k, v in binding.items()):
            raise ValueError('candidate selects an incompatible or different store')
        candidate['account'] = reason
        descriptor_bytes = encode(candidate)
        atomic_bytes(self.root / 'deployment.json', descriptor_bytes)
        step = 'candidate_cleanup'
        try:
            (self.root / 'candidate.json').unlink()
            step = 'status_readback'
            return Deployment.current_status(self.root)
        except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
            raise ActivationIncomplete(step, sha(descriptor_bytes), exc) from exc


class Deployment(DeploymentSelection):
    """Open the selected engine and store after verifying their declared identities."""

    def __init__(self, root, *, descriptor=None):
        super().__init__(root, descriptor=descriptor)
        self.engine = _engine_directory(self.root / self.config['engine']['path'])
        self.runtime = (self.root / self.config['store']['path']).resolve()
        if any(p.is_symlink() for p in (self.root, self.engine, self.runtime)):
            raise ValueError('ordinary deployment, engine and store directories required')
        self._verify_binding()

    @classmethod
    def initialize(cls, root, runtime, engine=None):
        root, runtime = Path(root).resolve(), Path(runtime).resolve()
        engine = _engine_directory(engine or HERE.parents[1])
        if root == runtime or root.is_relative_to(runtime) or root == engine or root.is_relative_to(engine):
            raise ValueError('deployment metadata must be separate from store and engine')
        if (root / 'deployment.json').exists():
            raise ValueError('deployment already exists; stage and activate an explicit new selection')
        binding = runtime_binding(runtime)
        value = {'format': FORMAT, 'controller_version': VERSION,
                 'engine': {'path': os.path.relpath(engine, root), 'code_sha256': code_identity(engine),
                            'engine_api': ENGINE_API},
                 'store': {'path': os.path.relpath(runtime, root), **binding},
                 'account': 'Initial explicit shared engine/store selection.'}
        root.mkdir(parents=True, exist_ok=True)
        atomic_bytes(root / 'deployment.json', encode(value))
        return cls.current_status(root)

    def _verify_engine(self):
        if self.config['engine'].get('engine_api') != ENGINE_API:
            raise ValueError('incompatible engine API')
        if code_identity(self.engine) != self.config['engine']['code_sha256']:
            raise ValueError('shared engine code changed; qualify and activate its current identity')

    def _verify_binding(self):
        binding = runtime_binding(self.runtime)
        if any(self.config['store'].get(k) != v for k, v in binding.items()):
            raise ValueError('deployment selected a different store identity/schema')
        self._verify_engine()
        return binding

    def verify(self):
        self._require_current()
        return self._verify_binding()

    @classmethod
    @contextmanager
    def admitted(cls, root, *, engine, instance, deployment_sha, read_only, store_sha=None, deadline=None):
        """Enter a pinned domain operation under the deployment, then its selected store."""
        root = Path(root).resolve()
        guard = shared if read_only else exclusive

        def acquire(guards):
            guards.enter_context(guard(root / 'deployment.lock'))
            descriptor = _descriptor_bytes(root / 'deployment.json')
            selection = DeploymentSelection(root, descriptor=descriptor)
            if selection.descriptor_sha256 != deployment_sha:
                raise ValueError('deployment descriptor differs')
            if (root / selection.config['engine']['path']).resolve() != engine:
                raise ValueError('selected native engine differs')
            if selection.config['store']['instance_id'] != instance:
                raise ValueError('recipient instance differs')
            runtime = (root / selection.config['store']['path']).resolve()
            guards.enter_context(guard(runtime / 'exchange/owner.lock'))
            return descriptor, runtime

        with owner_admission(acquire, deadline=admission_deadline(deadline)) as (descriptor, runtime):
            if store_sha is not None and sha(_descriptor_bytes(runtime / 'system.json')) != store_sha:
                raise ValueError('store descriptor differs')
            selected = cls(root, descriptor=descriptor)
            if selected.runtime != runtime:
                raise ValueError('selected store path differs')
            yield selected

    @classmethod
    def current_status(cls, root):
        selected = cls(root)
        store = selected.config['store']
        return {'format': FORMAT, 'deployment': str(selected.root), 'engine': str(selected.engine),
                'engine_code_sha256': selected.config['engine']['code_sha256'],
                'store': str(selected.runtime), 'instance_id': store['instance_id'],
                'store_schema': store['store_schema'], 'engine_api': store['engine_api'],
                'descriptor_sha256': selected.descriptor_sha256,
                'scope': 'One shared code tree; explicit store and data-domain selection. No copied runtime.'}

    def status(self):
        return type(self).current_status(self.root)

    @classmethod
    def dispatch_current(cls, root, arguments):
        return cls(root)._dispatch(arguments)

    def system(self, arguments):
        self._require_current()
        return self._dispatch(arguments)

    def _dispatch(self, arguments, *, deadline=None, already_admitted=False):
        if not arguments or any(v == '--root' or v.startswith('--root=') for v in arguments):
            raise ValueError('supply a native command; deployment owns its selected store')
        if self.engine != HERE.parents[1] or arguments[0] in {'adopt', 'reconcile-publication', 'recompose'}:
            return self._selected_system(arguments, deadline, already_admitted=already_admitted)
        return self._system_action(arguments, deadline, already_admitted=already_admitted)

    def _native_entry(self):
        entry = self.engine / NATIVE_ENTRY
        return entry if entry.is_file() else self.engine / PORTABLE_ENTRY

    def _selected_system(self, arguments, deadline=None, *, already_admitted=False):
        # The controller imported its own core before it verified the selected
        # engine. A cached package would otherwise mask the selected engine.
        app = self._native_entry().parent
        names = {'continuation_support', 'native_runtime'}
        names.update({'knowledge', 'source_producer', 'source_library'})
        names.update(path.stem for path in app.glob('*.py'))

        def selected_name(name):
            return name.partition('.')[0] in names

        previous = {name: sys.modules.pop(name) for name in tuple(sys.modules) if selected_name(name)}
        search = sys.path[:]
        try:
            sys.path[:] = [p for p in search if Path(p).resolve() not in (HERE, HERE.parent / 'continuation')]
            return self._system_action(arguments, deadline, already_admitted=already_admitted)
        finally:
            for name in tuple(sys.modules):
                if selected_name(name):
                    del sys.modules[name]
            sys.modules.update(previous)
            sys.path[:] = search

    def _system_action(self, arguments, deadline=None, *, already_admitted=False):
        api = runpy.run_path(str(self._native_entry()))
        if api.get('DEPLOYMENT_API') != FORMAT or api.get('ENGINE_API') != ENGINE_API:
            raise ValueError('selected engine does not implement the declared deployment API')
        args = api['parser']().parse_args(['--root', str(self.runtime), *arguments])
        if args.action in ('show', 'catalogue') and arguments[0] != args.action:
            raise ValueError('supply the native store observation as the command')
        if args.action == 'init':
            raise ValueError('initialize a store through the central system entrance before deployment')
        try:
            with self._guarded_system(api, args, deadline, already_admitted=already_admitted) as system:
                return api['act'](system, args), args, api
        except ValueError as exc:
            if args.action == 'adopt':
                basis = self._adopt_basis(api, exc)
                if basis is not None:
                    raise DeploymentAdoptBasisChanged(str(exc), basis) from exc
            raise

    @staticmethod
    def _adopt_basis(api, exc):
        describe = api.get('adopt_basis')
        if describe is not None:
            try:
                basis = describe(exc)
                if type(basis) is dict:
                    json.dumps(basis, allow_nan=False)
                    return basis
            except (OSError, ValueError, KeyError, TypeError, RuntimeError):
                pass
        return None

    @contextmanager
    def _guarded_system(self, api, args, deadline=None, *, already_admitted=False):
        if already_admitted:
            yield self._open_selected_store(api, args)
            return
        if args.action == 'explain-context':
            self.verify()
            yield self._open_selected_store(api, args)
            return
        readonly_fn = api.get('is_readonly_action')
        read_only = bool(readonly_fn(args)) if readonly_fn is not None else (args.action == 'catalogue')
        if args.action in {'show', 'catalogue'}:
            with type(self).admitted(
                    self.root,
                    engine=self.engine,
                    instance=self.config['store']['instance_id'],
                    deployment_sha=self.descriptor_sha256,
                    read_only=read_only,
                    deadline=deadline,
            ) as selected:
                yield selected._open_selected_store(api, args)
            return
        # Direct Python API (Deployment.system): acquire owner.lock BEFORE verifying/opening store.
        from continuation_support.ownership import external_root as external_exchange
        layout = api['NativeLayout'](api['external'](self.runtime))
        exchange_root = external_exchange(layout.exchange)
        guard = shared if read_only else exclusive
        with observation_guard(guard, exchange_root / 'owner.lock', deadline=deadline):
            self.verify()
            yield self._open_selected_store(api, args)

    def _open_selected_store(self, api, args):
        if args.action == 'configure':
            from native_runtime.loading import Loading
            system = api['System'](self.runtime, _selection_override=Loading.configuration_descriptor(self.runtime, read(args.spec)))
        else:
            system = api['System'](self.runtime)
        system.deployment = {'format': FORMAT, 'selection': self.config,
                             'engine_root': str(self.engine), 'project_root': str(system.work),
                             'project_files': [], 'project_file_count': 0,
                             'selection_scope': 'Per-store catalogues and domains are selected by the native store descriptor.'}
        system.command_prefix = [sys.executable, '-B', str(self.engine / 'prototype/deploy.py'),
                                 '--deployment', str(self.root), 'system']
        return system


def admission_deadline(forwarded=None):
    """Share an explicit caller's monotonic cutoff across the POSIX exec boundary."""
    if forwarded is not None and not math.isfinite(forwarded):
        raise ValueError('invalid admission deadline')
    value = os.environ.get('MANAGED_AI_DOMAIN_ADMISSION_SECONDS')
    if value is None:
        return forwarded
    seconds = float(value)
    if not 0 <= seconds <= 600:
        raise ValueError('MANAGED_AI_DOMAIN_ADMISSION_SECONDS must be between 0 and 600')
    end = time.monotonic() + seconds
    return end if forwarded is None else min(forwarded, end)


def observation_guard(guard, path, *, deadline=None):
    return owner_admission(lambda guards: guards.enter_context(guard(path)),
                           deadline=admission_deadline(deadline))


@contextmanager
def report_admission(operation):
    """Emit the CLI disposition only for an exhausted pre-operation admission."""
    try:
        yield
    except AdmissionContended as exc:
        print(json.dumps(exc.result(operation)), file=sys.stderr)
        raise SystemExit(75)


def parser():
    p = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    p.add_argument('--deployment', type=Path, required=True)
    p.add_argument('--expected-descriptor', help='Deployment edition verified by the forwarding entrance')
    p.add_argument('--admission-deadline', type=float, help=argparse.SUPPRESS)
    sub = p.add_subparsers(dest='action', required=True)
    init = sub.add_parser('init', allow_abbrev=False); init.add_argument('--runtime', '--store', dest='runtime', required=True, type=Path)
    init.add_argument('--engine', type=Path)
    sub.add_parser('status', allow_abbrev=False); sub.add_parser('verify', allow_abbrev=False)
    stage = sub.add_parser('stage', allow_abbrev=False); stage.add_argument('edition'); stage.add_argument('--engine', required=True, type=Path)
    activate = sub.add_parser('activate', allow_abbrev=False); activate.add_argument('edition'); activate.add_argument('--expected', required=True)
    activate.add_argument('--manifest-sha256', required=True); activate.add_argument('--reason', required=True)
    system = sub.add_parser('system', allow_abbrev=False); system.add_argument('arguments', nargs=argparse.REMAINDER)
    return p


def _precheck_local_system_args(engine, runtime, arguments):
    """Parse arguments with this controller's own parser before locks (zero store reads, zero code_identity)."""
    if not arguments or any(v == '--root' or v.startswith('--root=') for v in arguments):
        raise ValueError('supply a native command; deployment owns its selected store')
    if engine != HERE.parents[1]:
        return arguments[:1] in (['show'], ['catalogue'])
    native_root = str(HERE.parent / 'native')
    added = native_root not in sys.path
    if added:
        sys.path.insert(0, native_root)
    try:
        from native_runtime.command_parser import is_readonly_action, parser as native_parser
        parsed = native_parser().parse_args(['--root', str(runtime), *arguments])
        if parsed.action in ('show', 'catalogue') and arguments[0] != parsed.action:
            raise ValueError('supply the native store observation as the command')
        if parsed.action == 'init':
            raise ValueError('initialize a store through the central system entrance before deployment')
        return is_readonly_action(parsed)
    finally:
        if added and native_root in sys.path:
            sys.path.remove(native_root)


def main():
    args = parser().parse_args()
    root = args.deployment.resolve()
    try:
        if args.action == 'system' and args.arguments[:1] == ['explain-context']:
            # Read frozen evidence without creating deployment or store lock files.
            selected = Deployment(root)
            if args.expected_descriptor is not None and selected.descriptor_sha256 != args.expected_descriptor:
                raise ValueError('deployment descriptor differs')
            result, app_args, api = selected.system(args.arguments)
            api['emit'](result, app_args)
            return
        if args.action == 'system':
            descriptor = _descriptor_bytes(root / 'deployment.json')
            selection = DeploymentSelection(root, descriptor=descriptor)
            if args.expected_descriptor is not None and selection.descriptor_sha256 != args.expected_descriptor:
                raise ValueError('deployment descriptor differs')
            engine = _engine_directory(root / selection.config['engine']['path'])
            runtime = (root / selection.config['store']['path']).resolve()
            instance = selection.config['store']['instance_id']
            read_only = _precheck_local_system_args(engine, runtime, args.arguments)
            with report_admission(args.action), Deployment.admitted(
                    root, engine=engine, instance=instance, deployment_sha=selection.descriptor_sha256,
                    read_only=read_only, deadline=args.admission_deadline) as selected:
                result, app_args, api = selected._dispatch(
                    args.arguments, deadline=args.admission_deadline, already_admitted=True)
            api['emit'](result, app_args)
            return
        if args.action == 'init':
            root.mkdir(parents=True, exist_ok=True)
        lock = root / 'deployment.lock'
        guard = shared if args.action in {'status', 'verify'} else exclusive
        admission = observation_guard(guard, lock, deadline=args.admission_deadline)
        with report_admission(args.action), admission:
            if args.expected_descriptor is not None and DeploymentSelection(root).descriptor_sha256 != args.expected_descriptor:
                raise ValueError('deployment descriptor differs')
            if args.action == 'init':
                result = Deployment.initialize(root, args.runtime, args.engine)
            elif args.action == 'stage':
                result = DeploymentSelection(root).stage(args.edition, args.engine)
            elif args.action == 'activate':
                result = DeploymentSelection(root).activate(args.edition, args.expected, args.manifest_sha256, args.reason)
            elif args.action in {'status', 'verify'}:
                result = Deployment.current_status(root)
        print(json.dumps(result, indent=2))
    except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
        failure = {'status': 'unresolved', 'operation': args.action, 'error': str(exc)}
        if isinstance(exc, ActivationIncomplete):
            failure.update(status='committed_incomplete', failed_step=exc.failed_step,
                           descriptor_sha256=exc.descriptor_sha256)
        elif isinstance(exc, DeploymentAdoptBasisChanged):
            failure['publication_basis'] = exc.basis
        print(json.dumps(failure), file=sys.stderr)
        raise SystemExit(2)


if __name__ == '__main__':
    main()
