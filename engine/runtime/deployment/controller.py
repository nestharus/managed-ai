#!/usr/bin/env python3
"""Select one shared native engine and one schema-governed store by reference.

Deployment metadata never copies an engine, project or store. Native captures
retain the selected occurrence needed by their receiving work.
"""
from contextlib import contextmanager
from pathlib import Path
import argparse
import ast
import hashlib
import json
import os
import runpy
import shlex
import stat
import sys
from importlib.machinery import EXTENSION_SUFFIXES

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / 'continuation'))
from continuation_support.storage import encode, exclusive, shared, atomic_bytes, sha
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


def read(path):
    if path.is_symlink() or not path.is_file():
        raise ValueError('ordinary descriptor required: ' + str(path))
    value = json.loads(path.read_bytes())
    if not isinstance(value, dict):
        raise ValueError('expected a JSON object in descriptor: ' + str(path))
    return value


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
    rows, declarations = [], {}
    interfaces = [(PORTABLE_ENTRY, PORTABLE_CONTRACT)]
    if (engine / NATIVE_ENTRY).is_file():
        schema = NATIVE_CONTRACT if (engine / NATIVE_CONTRACT).is_file() else PORTABLE_CONTRACT
        interfaces.append((NATIVE_ENTRY, schema))
    declaration_paths = {path for pair in interfaces for path in pair}
    for path in sorted(paths):
        relative = path.relative_to(engine)
        raw = path.read_bytes()
        rows.append({'path': relative.as_posix(), 'sha256': sha(raw)})
        if relative in declaration_paths:
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
    if (binding.get('version') != 2 or binding.get('workspace_relative') != '../work' or
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


class Deployment:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.config = read(self.root / 'deployment.json')
        if self.config.get('format') != FORMAT:
            raise ValueError('copied-engine deployment is not supported; create a shared reference deployment')
        self.engine = _engine_directory(self.root / self.config['engine']['path'])
        self.runtime = (self.root / self.config['store']['path']).resolve()
        if any(p.is_symlink() for p in (self.root, self.engine, self.runtime)):
            raise ValueError('ordinary deployment, engine and store directories required')
        self.verify()

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
        return cls(root).status()

    def _verify_engine(self):
        if self.config['engine'].get('engine_api') != ENGINE_API:
            raise ValueError('incompatible engine API')
        if code_identity(self.engine) != self.config['engine']['code_sha256']:
            raise ValueError('shared engine code changed; qualify and activate its current identity')

    def verify(self):
        self._verify_engine()
        binding = runtime_binding(self.runtime)
        if any(self.config['store'].get(k) != v for k, v in binding.items()):
            raise ValueError('deployment selected a different store identity/schema')
        return binding

    def status(self):
        return {'format': FORMAT, 'deployment': str(self.root), 'engine': str(self.engine),
                'engine_code_sha256': self.config['engine']['code_sha256'],
                'store': str(self.runtime), **self.verify(),
                'descriptor_sha256': sha((self.root / 'deployment.json').read_bytes()),
                'scope': 'One shared code tree; explicit store and data-domain selection. No copied runtime.'}

    def stage(self, edition, engine):
        packet_id(edition)
        engine = _engine_directory(engine)
        value = dict(self.config, edition=edition,
                     engine={'path': os.path.relpath(engine, self.root), 'code_sha256': code_identity(engine),
                             'engine_api': ENGINE_API})
        atomic_bytes(self.root / 'candidate.json', encode(value))
        return {'edition': edition, 'candidate_sha256': sha(encode(value)), 'engine': str(engine)}

    def activate(self, edition, expected, manifest_sha, reason):
        if sha((self.root / 'deployment.json').read_bytes()) != expected:
            raise ValueError('deployment selection revision conflict')
        candidate = read(self.root / 'candidate.json')
        if candidate.get('edition') != edition or sha(encode(candidate)) != manifest_sha:
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
            return type(self)(self.root).status()
        except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
            raise ActivationIncomplete(step, sha(descriptor_bytes), exc) from exc

    def system(self, arguments):
        if not arguments or any(v == '--root' or v.startswith('--root=') for v in arguments):
            raise ValueError('supply a native command; deployment owns its selected store')
        self._verify_engine()
        if self.engine != HERE.parents[1] or arguments[0] in {'adopt', 'reconcile-publication', 'recompose'}:
            return self._selected_system(arguments)
        return self._system_action(arguments)

    def _native_entry(self):
        entry = self.engine / NATIVE_ENTRY
        return entry if entry.is_file() else self.engine / PORTABLE_ENTRY

    def _selected_system(self, arguments):
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
            return self._system_action(arguments)
        finally:
            for name in tuple(sys.modules):
                if selected_name(name):
                    del sys.modules[name]
            sys.modules.update(previous)
            sys.path[:] = search

    def _system_action(self, arguments):
        api = runpy.run_path(str(self._native_entry()))
        if api.get('DEPLOYMENT_API') != FORMAT or api.get('ENGINE_API') != ENGINE_API:
            raise ValueError('selected engine does not implement the declared deployment API')
        args = api['parser']().parse_args(['--root', str(self.runtime), *arguments])
        if args.action == 'init':
            raise ValueError('initialize a store through the central system entrance before deployment')
        try:
            with self._guarded_system(api, args) as system:
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
    def _guarded_system(self, api, args):
        if args.action in {'catalogue', 'configure'}:
            guard = shared if args.action == 'catalogue' else exclusive
            with guard(self.runtime / 'exchange' / 'owner.lock'):
                yield self._open_selected_store(api, args)
        elif args.action in {'put', 'recompose'}:
            # Resolve the owner before admission. Read its current policy inside the guard.
            layout = api['NativeLayout'](api['external'](self.runtime))
            owner = api['Exchange'](layout.exchange)
            if owner.workspace != layout.workspace.resolve():
                raise ValueError('system workspace binding changed')
            with exclusive(owner.root / 'owner.lock'):
                yield self._open_selected_store(api, args)
        else:
            system = self._open_selected_store(api, args)
            if args.action == 'explain-context':
                yield system
            else:
                with exclusive(system.ex.root / 'owner.lock'):
                    yield system

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


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--deployment', type=Path, required=True)
    sub = p.add_subparsers(dest='action', required=True)
    init = sub.add_parser('init'); init.add_argument('--runtime', '--store', dest='runtime', required=True, type=Path)
    init.add_argument('--engine', type=Path)
    sub.add_parser('status'); sub.add_parser('verify')
    stage = sub.add_parser('stage'); stage.add_argument('edition'); stage.add_argument('--engine', required=True, type=Path)
    activate = sub.add_parser('activate'); activate.add_argument('edition'); activate.add_argument('--expected', required=True)
    activate.add_argument('--manifest-sha256', required=True); activate.add_argument('--reason', required=True)
    system = sub.add_parser('system'); system.add_argument('arguments', nargs=argparse.REMAINDER)
    return p


def main():
    args = parser().parse_args()
    root = args.deployment.resolve()
    try:
        if args.action == 'system' and args.arguments[:1] == ['explain-context']:
            # Read frozen evidence without creating deployment or store lock files.
            result, app_args, api = Deployment(root).system(args.arguments)
            api['emit'](result, app_args)
            return
        if args.action == 'init':
            root.mkdir(parents=True, exist_ok=True)
        guard = shared if args.action == 'system' and args.arguments[:1] == ['catalogue'] else exclusive
        with guard(root / 'deployment.lock'):
            if args.action == 'init':
                result = Deployment.initialize(root, args.runtime, args.engine)
            else:
                # A changed shared engine may be explicitly reselected without first
                # passing the old code pin. All ordinary execution still verifies it.
                if args.action in {'stage', 'activate'}:
                    deployment = object.__new__(Deployment)
                    deployment.root = root
                    deployment.config = read(root / 'deployment.json')
                    if deployment.config.get('format') != FORMAT:
                        raise ValueError('unsupported deployment format')
                else:
                    deployment = Deployment(root)
                if args.action == 'stage': result = deployment.stage(args.edition, args.engine)
                elif args.action == 'activate': result = deployment.activate(args.edition, args.expected, args.manifest_sha256, args.reason)
                elif args.action in {'status', 'verify'}: result = deployment.status()
                else:
                    result, app_args, api = deployment.system(args.arguments)
                    api['emit'](result, app_args)
                    return
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
