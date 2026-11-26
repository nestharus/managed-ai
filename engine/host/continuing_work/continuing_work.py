#!/usr/bin/env python3
"""Optional G60 host bridge: continuing commission, useful reminders and causal repair custody.

No model client, semantic scheduler, universal ontology or inferred completeness.
Uses the existing native runtime for context, Attention, receive/adopt and goal use.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import stat
import subprocess
import sys

HERE = Path(__file__).resolve().parent

# This entry also supports loading by an explicit filename from a deployment.
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from capture_native import native_sources
from native_bridge import (
    MAX_EVENT_LINE_BYTES,
    MAX_EVENT_STREAM_BYTES,
    NativeBridge,
    _safe_jsonl,
    capture_error,
    resolve_sibling_cli,
)
from occurrence_files import (
    atomic_bytes,
    check_unfollowed_ancestors,
    digest,
    file_lock,
    ident,
    is_reparse_or_symlink,
    loads_strict,
    nonempty,
    now,
    read,
    sync_dir,
    write,
)
from repair_custody import RepairCustody
from scope_lifecycle import (
    ScopeLifecycle,
    overlap_rows,
    prepare_attention_selection,
    prepare_questions,
)

_FIELD_TEXT = (
    'The full commission remains active. A local contribution may finish while other duties remain owed. '
    'Reopen broader alternatives when evidence changes the useful contribution. '
    'Discover unrepresented relationships; the host only routes nominated ones.\n'
)
_START_RECEIPT_TEXT = (
    'Host initialized exact commission, explicit duties and the continuing commission Attention item. '
    'This is enrollment, not a model reading or semantic completion claim.\n'
)


def _safe_relpath(path: Path | str, start: Path | str) -> str:
    """Return a POSIX-separated relative path when possible, falling back to an absolute path across Windows drives."""
    try:
        return os.path.relpath(path, start).replace('\\', '/')
    except ValueError:
        return str(Path(path).resolve())


def _read_ordinary_file_bytes(raw_path, role: str) -> tuple[Path, bytes]:
    checked = check_unfollowed_ancestors(raw_path, role, include_target=True)
    try:
        info = checked.lstat()
    except FileNotFoundError as exc:
        raise ValueError(f'{role} is absent: {checked}') from exc
    if not stat.S_ISREG(info.st_mode):
        raise ValueError(f'{role} must be an ordinary file: {checked}')
    resolved = checked.resolve()
    return resolved, resolved.read_bytes()


class Host:
    def __init__(self, root):
        checked_root = check_unfollowed_ancestors(root, 'Host root', include_target=True)
        self.root = checked_root.resolve()
        self.config_path = self.root / 'host.json'
        self.config = read(self.config_path) if self.config_path.exists() else None
        self.native = (self.root / (self.config or {}).get('native_root', 'native')).resolve()
        self.work = self.native / 'work'
        self.bridge = NativeBridge(self)
        self.repairs = RepairCustody(self)
        self.lifecycle = ScopeLifecycle(self)

    def _write(self, path, value):
        write(path, value)

    def _put_object_bytes(self, raw: bytes) -> tuple[str, str]:
        return self.bridge._put_object_bytes(raw)

    def event(self, action, value):
        return self.bridge.event(action, value)

    def call(self, *args, scoped_errors=False):
        return self.bridge.call(*args, scoped_errors=scoped_errors)

    def inspect_native(self, sid, scoped_errors=False):
        return self.bridge.inspect_native(sid, scoped_errors=scoped_errors)

    def capture_native(self, sid, receipt, producer):
        return self.bridge.capture_native(sid, receipt, producer)

    def reconcile_capture_native(self, sid, receipt, producer):
        return self.bridge.reconcile_capture_native(sid, receipt, producer)

    def temp(self, name, value):
        return self.bridge.temp(name, value)

    def pin(self, path):
        return self.bridge.pin(path)

    def capture(self, sid, spec):
        return self.bridge.capture(sid, spec)

    def repair_open(self, spec):
        return self.repairs.repair_open(spec)

    def repair_accept(self, spec):
        return self.repairs.repair_accept(spec)

    def prepare(self, spec):
        return self.lifecycle.prepare(spec)

    def receive(self, sid, spec):
        return self.lifecycle.receive(sid, spec)

    def resume(self, sid, spec):
        return self.lifecycle.resume(sid, spec)

    def supersede(self, sid, spec):
        return self.lifecycle.supersede(sid, spec)

    def status(self):
        return self.lifecycle.status()

    @property
    def engine(self):
        return str((self.root / self.config['engine']).resolve())

    def producer_dependencies(self):
        return [
            self.pin(HERE / name)
            for name in ('occurrence_files.py', 'repair_custody.py', 'native_bridge.py', 'scope_lifecycle.py')
        ]

    def goal(self):
        return self.call('show', 'goal')

    def current(self, pin, label):
        path = Path(pin['path'])
        if is_reparse_or_symlink(path):
            raise ValueError(label + ' must not be a symlink or reparse point')
        actual = digest(path.read_bytes()) if path.is_file() else None
        if actual != pin['sha256']:
            self.event('changed-current-source', {'label': label, 'captured': pin, 'current_sha256': actual})
            raise ValueError(label + ' changed outside this captured occurrence; preserve the proposal and prepare current work')

    def received_outputs(self, scope):
        if 'result' not in scope:
            raise ValueError('Scope has no actual received output')
        for pin in scope['result']['outputs']:
            self.current(pin, 'Received output ' + scope['id'])
            obj = self.root / pin['object']
            if is_reparse_or_symlink(obj) or not obj.is_file() or digest(obj.read_bytes()) != pin['sha256']:
                raise ValueError('Retained received-output object differs')
        return scope['result']['outputs']

    def init(self, spec):
        if self.config is not None or self.root.exists() and any(self.root.iterdir()):
            raise ValueError('Retain the existing host; initialize only a new empty root')
        engine_checked = check_unfollowed_ancestors(spec['engine'], 'Current native engine', include_target=True)
        if not engine_checked.exists() or not stat.S_ISREG(engine_checked.lstat().st_mode):
            raise ValueError('Current native engine is absent')
        engine = engine_checked.resolve()
        commission, commission_bytes = _read_ordinary_file_bytes(spec['commission'], 'Commission source')
        commission_text = commission_bytes.decode('utf-8')
        operation_bytes = (HERE / 'OPERATION.md').read_bytes()
        duties = spec['duties']
        if not duties:
            raise ValueError('This continuing-work route requires explicit commissioned duties')
        for row in duties:
            ident(row['id'])
            nonempty(row['text'], 'duty text')
        if len({r['id'] for r in duties}) != len(duties):
            raise ValueError('Duplicate duty')
        if spec.get('native_root'):
            native_checked = check_unfollowed_ancestors(spec['native_root'], 'Native root', include_target=True)
            self.native = native_checked.resolve()
            self.work = self.native / 'work'
            descriptor = read(self.native / 'system.json')
            self.root.mkdir(parents=True, exist_ok=True)
            candidate_config = {
                'format': 'g60-continuing-work-host/1',
                'engine': _safe_relpath(engine, self.root),
                'created_at_utc': now(),
                'commission': self.pin(commission),
                'native_root': _safe_relpath(self.native, self.root),
                'attached_instance_id': descriptor['instance_id'],
                'scope': 'Caller-enrolled continuing work; semantic completeness is not inferred.',
            }
            self.config = candidate_config
            try:
                status = self.call('status')
                supplied = set(status['owed_record_obligations']) | {r['id'] for r in status['owed_goal_obligations']}
                if any(row['id'] not in supplied for row in duties):
                    raise ValueError('attached host duties must name existing owed native obligations')
            except BaseException:
                self.config = None
                raise
            self._write(self.config_path, self.config)
            return {
                'root': str(self.root),
                'native': str(self.native),
                'attached': True,
                'instance_id': descriptor['instance_id'],
                'goal': status['current_goal'],
                'boundary': 'The existing native store retains goals and duty ownership. Attachment creates no new runtime or artificial participant return.',
            }

        self.root.mkdir(parents=True, exist_ok=True)
        candidate_config = {
            'format': 'g60-continuing-work-host/1',
            'engine': _safe_relpath(engine, self.root),
            'created_at_utc': now(),
            'commission': self.pin(commission),
            'scope': 'Caller-enrolled continuing work; semantic completeness is not inferred.',
        }
        # Hold config in memory during native enrollment; persist host.json only after enrollment succeeds.
        self.config = candidate_config
        try:
            initial = {
                'id': 'goal',
                'kind': 'goal',
                'content': {
                    'contribution': commission_text,
                    'obligations': [dict(r, disposition='open') for r in duties],
                },
            }
            self.call('init', '--goal', self.temp('init-goal.json', initial))
            atomic_bytes(self.work / 'sources/host/COMMISSION.md', commission_bytes)
            atomic_bytes(self.work / 'sources/host/OPERATION.md', operation_bytes)
            current = self.goal()
            bound = {
                'id': 'goal',
                'kind': 'goal',
                'content': current['content'],
                'basis': ['sources/host/COMMISSION.md'],
            }
            self.call('put', '--record', self.temp('bind-commission.json', bound), '--expected', current['revision'])
            atomic_bytes(self.work / 'sources/host/FIELD.md', _FIELD_TEXT.encode('utf-8'))
            question = {
                'field': 'sources/host/FIELD.md',
                'items': [{
                    'id': 'continuing-commission',
                    'question': 'Which commissioned effects remain owed beyond this local contribution?',
                    'use': 'Keep current work aligned with the containing commission.',
                    'basis': ['sources/host/COMMISSION.md', 'records/goal.json'],
                    'groups': ['continuation'],
                }],
            }
            self.call('track', '--spec', self.temp('initial-attention.json', question))
            reply = self.root / 'requests/start-receipt.md'
            atomic_bytes(reply, _START_RECEIPT_TEXT.encode('utf-8'))
            self.call('receive', 'start', '--reply', reply, '--producer', 'continuing-work enrollment adapter')
            final_goal = self.goal()
            self._write(self.config_path, self.config)
        except BaseException:
            self.config = None
            raise
        return {
            'root': str(self.root),
            'native': str(self.native),
            'goal': final_goal,
            'boundary': 'Subsequent actual capable participant must read the prepared context.',
        }

    def bind_continuation(self, spec):
        """Add current-goal dependency to an existing host-owned inquiry through native APIs."""
        if set(spec) != {'id', 'account'}:
            raise ValueError('Continuation binding requires a fresh id and account only')
        operation = ident(spec['id'])
        account = nonempty(spec['account'], 'continuation binding account')
        destination = self.root / 'continuation-bindings' / f'{operation}.json'
        if destination.exists():
            raise ValueError('Retain the existing continuation binding; use a fresh operation identity')
        self.current(self.config['commission'], 'Current commission')
        goal = self.goal()
        if not (self.work / 'records/goal.json').is_file():
            raise ValueError('Current native goal material is absent')
        # Public, item-filtered feedback obtains the actual current specification.
        # Its administrative observation is not a semantic consideration receipt.
        attention_cli = resolve_sibling_cli(Path(self.engine), 'core/cli/track_attention.py', 'track_attention.py')
        command = [
            sys.executable, '-B', str(attention_cli),
            '--root', str(self.native / 'exchange'), 'feedback', operation,
            '--item', 'continuing-commission', '--limit', '1',
        ]
        result = subprocess.run(command, capture_output=True, text=True, encoding='utf-8')
        self.event('continuation-binding-observation', {
            'argv': command, 'exit': result.returncode, 'stdout': result.stdout, 'stderr': result.stderr,
        })
        if result.returncode:
            raise ValueError(result.stderr or result.stdout)
        observed = loads_strict(result.stdout)
        if observed['omitted'] or len(observed['delivered']) != 1 or observed['delivered'][0]['id'] != 'continuing-commission':
            raise ValueError('The continuing inquiry was not completely delivered; preserve the observation and inspect it')
        before = observed['delivered'][0]['spec']
        after = dict(before, basis=sorted(set(before['basis']) | {'records/goal.json'}))
        changed = after != before
        tracked = None
        if changed:
            tracked = self.call('track', '--spec', self.temp(operation + '-continuation-binding.json', {'items': [after]}))
        record = {
            'id': operation,
            'at_utc': now(),
            'account': account,
            'goal': {k: goal[k] for k in ('revision', 'sha256')},
            'observation': {
                'id': observed['id'],
                'object': observed['feedback_object'],
                'context_sha256': observed['context_sha256'],
            },
            'before': before,
            'current': after,
            'changed': changed,
            'track': tracked,
            'boundary': 'Basis enrollment only. Historical accounts remain unchanged; the next actual feedback and semantic receiving determine current applicability. External effects absent from the basis are not inferred.',
        }
        self._write(destination, record)
        self.event('continuation-basis-bound', {'id': operation, 'record': self.pin(destination), 'changed': changed})
        return record

    def commission(self, spec):
        """Explicitly revise current instructions while preserving earlier enrolled duties."""
        old = self.config['commission']
        account = nonempty(spec['account'], 'commission continuation account')
        source_path, new_bytes = _read_ordinary_file_bytes(spec['path'], 'Commission source')
        new_text = new_bytes.decode('utf-8')
        goal = self.goal()
        duties = {r['id']: dict(r) for r in goal['content']['obligations']}
        for row in spec.get('duties', []):
            rid = ident(row['id'])
            nonempty(row['text'], 'duty text')
            if rid in duties and duties[rid]['text'] != row['text']:
                raise ValueError('Do not silently replace an earlier duty; add a new identity or reopen/account for it')
            if rid not in duties:
                duties[rid] = dict(row, disposition='open')
        new = self.pin(source_path)
        target = self.work / 'sources/host/COMMISSION.md'
        prior_exists = target.exists() and not is_reparse_or_symlink(target)
        prior_bytes = target.read_bytes() if prior_exists else None
        atomic_bytes(target, new_bytes)
        try:
            content = {
                'contribution': new_text,
                'obligations': list(duties.values()),
                'continuation_account': account,
            }
            record = {'id': 'goal', 'kind': 'goal', 'content': content, 'basis': ['sources/host/COMMISSION.md']}
            result = self.call(
                'put', '--record', self.temp('commission-' + str(goal['revision']) + '.json', record),
                '--expected', goal['revision'],
            )
        except BaseException:
            if prior_exists and prior_bytes is not None:
                atomic_bytes(target, prior_bytes)
            else:
                target.unlink(missing_ok=True)
            raise
        self.config['commission'] = new
        self._write(self.config_path, self.config)
        self.event('commission-continued', {
            'before': old, 'current': new, 'account': account, 'preserved_duties': list(duties),
        })
        return result

    def upgrade(self, spec):
        """Select a regenerated host/guidance release with byte-identical native programs."""
        new_checked = check_unfollowed_ancestors(spec['engine'], 'New installed engine', include_target=True)
        if not new_checked.exists() or not stat.S_ISREG(new_checked.lstat().st_mode):
            raise ValueError('New installed engine is absent')
        new = new_checked.resolve()
        old = Path(self.engine)
        if native_sources(old)[1] != native_sources(new)[1]:
            raise ValueError('This upgrade route requires identical native programs; a native-code change needs its own compatibility receiving')
        account = nonempty(spec['account'], 'upgrade account')
        before = self.call('status')
        previous = dict(self.config)
        self.config = dict(self.config, engine=_safe_relpath(new, self.root))
        try:
            after = self.call('status')
        except Exception:
            self.config = previous
            raise
        if before['current_goal']['sha256'] != after['current_goal']['sha256']:
            self.config = previous
            raise ValueError('Current goal changed while checking the selected release')
        self._write(self.config_path, self.config)
        host_producer = self.pin(Path(__file__).resolve())
        host_dependencies = self.producer_dependencies()
        host_method = self.pin(HERE / 'OPERATION.md')
        self.event('producing-release-selected', {
            'before_engine': self.pin(old),
            'current_engine': self.pin(new),
            'host_producer': host_producer,
            'host_dependencies': host_dependencies,
            'host_method': host_method,
            'account': account,
            'native_programs_unchanged': True,
            'prior_captures_unchanged': True,
        })
        return {
            'engine': str(new),
            'host_producer': host_producer,
            'host_method': host_method,
            'host_dependencies': host_dependencies,
            'native_programs_unchanged': True,
            'parent_duties_unchanged': True,
        }

    def refresh_resource(self, rid, source):
        rid = ident(rid)
        old_path = self.root / 'resources' / f'{rid}.json'
        previous = read(old_path) if old_path.exists() else None
        pin = self.pin(source)
        suffix = Path(source).suffix or '.txt'
        # The stable resource path is the relevant native publication basis.
        path = 'sources/resources/' + rid + suffix
        if previous and previous['work_path'] != path:
            raise ValueError('A resource retains its file type/path; use a new identity for another interpretation')
        target = self.work / path
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists() or digest(target.read_bytes()) != pin['sha256']:
            atomic_bytes(target, (self.root / pin['object']).read_bytes())
        row = {'id': rid, **pin, 'work_path': path}
        self._write(old_path, row)
        return row, previous if previous and previous['sha256'] != row['sha256'] else None

    resource = refresh_resource  # Earlier host callers use this spelling.

    def scopes(self):
        return [read(p) for p in sorted((self.root / 'scopes').glob('*.json'))]

    def duty(self, spec):
        self.current(self.config['commission'], 'Current commission')
        goal = self.goal()
        rows = goal['content']['obligations']
        selected = next((r for r in rows if r['id'] == spec['id']), None)
        if selected is None:
            raise ValueError('Unknown parent duty')
        if spec['disposition'] not in ('fulfilled', 'open'):
            raise ValueError('This route cannot release a user duty')
        evidence = spec.get('evidence', [])
        if spec['disposition'] == 'fulfilled':
            if not evidence:
                raise ValueError('Fulfillment requires actual received evidence')
            published = {p['path'] for s in self.scopes() if 'result' in s for p in s['result']['publications']}
            if set(evidence) - published:
                raise ValueError('Use actual current returned outputs as evidence')
            for scope in self.scopes():
                if 'result' in scope and {p['path'] for p in scope['result']['publications']} & set(evidence):
                    self.received_outputs(scope)
            selected.update(disposition='fulfilled', account=nonempty(spec['account'], 'duty account'), evidence=evidence)
        else:
            selected.update(disposition='open', account=nonempty(spec['account'], 'reopened duty account'))
        record = {'id': 'goal', 'kind': 'goal', 'content': goal['content'], 'basis': ['sources/host/COMMISSION.md']}
        result = self.call(
            'put', '--record', self.temp('duty-' + ident(spec['id']) + '-' + str(goal['revision']) + '.json', record),
            '--expected', goal['revision'],
        )
        self.event('duty-accounted', {'id': spec['id'], 'disposition': spec['disposition'], 'evidence': evidence})
        return result

    def finish(self, spec):
        self.current(self.config['commission'], 'Current commission')
        goal = self.goal()
        owed = [r['id'] for r in goal['content']['obligations'] if r['disposition'] not in ('fulfilled', 'released')]
        if owed:
            raise ValueError('Containing commission remains owed: ' + ', '.join(owed))
        account = self.root / 'requests/whole-goal-account.md'
        atomic_bytes(account, nonempty(spec['account'], 'whole goal account').encode('utf-8'))
        args = ['interpret', '--goal-revision', goal['revision'], '--disposition', 'met', '--account', account]
        for p in spec.get('evidence', []):
            args += ['--evidence', p]
        return self.call(*args)


def main():
    p = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    p.add_argument('--root', required=True, type=Path)
    sub = p.add_subparsers(dest='action', required=True)
    for name in ('init', 'commission', 'upgrade', 'prepare', 'repair-open', 'repair-accept', 'duty', 'finish', 'bind-continuation'):
        sub.add_parser(name, allow_abbrev=False).add_argument('--spec', required=True, type=Path)
    receiver = sub.add_parser('receive', allow_abbrev=False); receiver.add_argument('id'); receiver.add_argument('--spec', required=True, type=Path)
    capture = sub.add_parser('capture', allow_abbrev=False); capture.add_argument('id'); capture.add_argument('--spec', required=True, type=Path)
    resume = sub.add_parser('resume', allow_abbrev=False); resume.add_argument('id'); resume.add_argument('--spec', required=True, type=Path)
    supersede = sub.add_parser('supersede', allow_abbrev=False); supersede.add_argument('id'); supersede.add_argument('--spec', required=True, type=Path)
    sub.add_parser('status', allow_abbrev=False)
    args = p.parse_args()
    checked_root = check_unfollowed_ancestors(args.root, 'Host root', include_target=True)
    root = checked_root.resolve()
    root.parent.mkdir(parents=True, exist_ok=True)
    # One host operation owns resource refresh plus native capture/admission.
    # This is custody serialization, not a permanent semantic integrator.
    with file_lock(root.parent / (root.name + '.host-lock')):
        host = Host(root)
        if args.action != 'init' and host.config is None:
            raise ValueError('Initialize the explicit continuing commission first')
        if args.action == 'status':
            result = host.status()
        elif args.action in ('capture', 'receive', 'resume', 'supersede'):
            result = getattr(host, args.action)(args.id, read(args.spec))
        else:
            result = getattr(host, args.action.replace('-', '_'))(read(args.spec))
    print(json.dumps(result, indent=2, ensure_ascii=False, allow_nan=False))


if __name__ == '__main__':
    try:
        main()
    except (ValueError, KeyError, FileNotFoundError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2)
