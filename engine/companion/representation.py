#!/usr/bin/env python3
"""Bind authored representation operations to the existing native turn owner.

This opt-in application adapter does not interpret domain shapes or edit core.
It makes selected accepted JSON artifacts and source applications actual native
inputs, retains a before/after witness, and delegates every adoption to native
Exchange. Callers author the candidate and judge its semantic adequacy.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys


OPERATION = 'g60-representation-operation/1'
WITNESS = 'g60-representation-return/1'
BOUND_RETURN = 'g60-representation-bound-return/1'


def require(condition, message):
    if not condition:
        raise ValueError(message)


def text(value, name):
    require(isinstance(value, str) and bool(value.strip()), name + ' must be nonempty text')
    return value


def strings(value, name):
    require(isinstance(value, list) and all(isinstance(v, str) and v.strip() for v in value),
            name + ' must be a list of nonempty strings')
    require(len(value) == len(set(value)), name + ' contains duplicates')
    return value


def digest(data):
    return hashlib.sha256(data).hexdigest()


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n').encode()


def read_json(path):
    return json.loads(Path(path).read_bytes())


def changed_paths(before, after, prefix=''):
    """Exact structural differences, not semantic effects or independence."""
    if type(before) is not type(after):
        return [prefix]
    if isinstance(before, dict):
        result = []
        for key in sorted(set(before) | set(after)):
            at = prefix + '/' + key.replace('~', '~0').replace('/', '~1')
            if key not in before or key not in after:
                result.append(at)
            else:
                result.extend(changed_paths(before[key], after[key], at))
        return result
    if isinstance(before, list):
        if len(before) != len(after):
            return [prefix]
        return [p for n, (a, b) in enumerate(zip(before, after))
                for p in changed_paths(a, b, prefix + '/' + str(n))]
    return [] if before == after else [prefix]


class RepresentationOperations:
    """Use under the native cooperative owner lock, as the CLI does."""

    def __init__(self, system):
        self.system = system
        self.ex = system.ex

    def location(self, turn):
        # Reuse the native path/identity validators; never touch reserved records.
        from continuation_support.ownership import packet_id
        return self.ex.path('representation-operations/' + packet_id(turn))

    def pin(self, path, accepted_in=None):
        from native_runtime.inputs import artifact_target
        name = artifact_target(path)
        actual = self.ex.path(name)
        require(actual.is_file() and not actual.is_symlink(), 'pin requires an ordinary file: ' + name)
        row = {'path': name, 'sha256': digest(actual.read_bytes())}
        if accepted_in is not None:
            row['accepted_in'] = text(accepted_in, 'accepted_in')
            self.check_pin(row, representation=True)
        return row

    def check_pin(self, row, representation=False):
        require(isinstance(row, dict), 'input pin must be an object')
        name = text(row.get('path'), 'input path')
        actual = self.ex.path(name)
        require(actual.is_file() and not actual.is_symlink(), 'input is not an ordinary file: ' + name)
        data = actual.read_bytes()
        require(digest(data) == row.get('sha256'), 'input basis changed: ' + name)
        if representation:
            json.loads(data)  # Arbitrary complete JSON; no domain schema.
            turn = text(row.get('accepted_in'), 'representation accepted_in')
            observed = self.ex.inspect(turn)
            received = observed.get('return')
            require(received is not None, 'representation acceptance has no native return')
            proposals = {p['path']: p for p in received['proposals']}
            proposal = proposals.get(name)
            require(proposal is not None and proposal['sha256'] == row['sha256'],
                    'representation differs from its nominated accepted proposal: ' + name)
            matching = [p for p in observed['publications'] if p.get('record', {}).get('path') == name]
            require(len(matching) == 1 and not matching[0].get('observation_error'),
                    'representation acceptance is unavailable: ' + name)
            publication = matching[0]['record']
            require(publication.get('phase') == 'complete' and publication.get('sha256') == row['sha256']
                    and publication.get('return_sha256') == received['return_sha256'],
                    'representation needs a completed matching native adoption: ' + name)
        return name

    def validate_operation(self, spec):
        require(isinstance(spec, dict) and spec.get('format') == OPERATION, 'unsupported operation format')
        text(spec.get('turn'), 'turn')
        mode = spec.get('mode')
        require(mode in ('develop', 'transform', 'realize'), 'mode must be develop, transform or realize')
        text(spec.get('task'), 'task')
        text(spec.get('consumer'), 'consumer')
        representations = spec.get('representations')
        sources = spec.get('sources')
        require(isinstance(representations, list) and isinstance(sources, list), 'inputs must be explicit lists')
        if mode != 'develop':
            require(bool(representations), mode + ' requires an accepted representation binding')
        rep_paths = [self.check_pin(p, representation=True) for p in representations]
        source_paths = [self.check_pin(p) for p in sources]
        require(len(rep_paths + source_paths) == len(set(rep_paths + source_paths)), 'duplicate input paths')
        targets = strings(spec.get('targets'), 'targets')
        require(bool(targets), 'operation requires a target')
        from native_runtime.inputs import artifact_target
        for target in targets:
            artifact_target(target)
            require(self.ex.path(target).parent.is_dir(), 'target parent must already exist: ' + target)
        applications = spec.get('applications')
        require(isinstance(applications, list) and bool(applications), 'at least one authored application is required')
        ids = []
        for app in applications:
            require(isinstance(app, dict), 'application must be an object')
            ids.append(text(app.get('id'), 'application id'))
            used_sources = strings(app.get('sources'), 'application sources')
            used_representations = strings(app.get('representations'), 'application representations')
            require(set(used_sources) <= set(source_paths), 'application source was not selected')
            require(set(used_representations) <= set(rep_paths), 'application representation was not selected')
            if mode != 'develop':
                require(bool(used_representations), 'application must bind an accepted representation')
            text(app.get('operation'), 'authored application operation')
            text(app.get('prediction'), 'authored application prediction')
        require(len(ids) == len(set(ids)), 'duplicate application identities')
        interactions = spec.get('interactions')
        require(isinstance(interactions, dict), 'interactions must be explicitly supplied')
        text(interactions.get('account'), 'interaction scope account')
        strings(interactions.get('unknowns'), 'unknown interactions')
        return rep_paths, source_paths

    def prepare(self, spec):
        reps, sources = self.validate_operation(spec)
        directory = self.location(spec['turn'])
        require(not directory.exists(), 'operation identity already exists; inspect it, do not overwrite')
        native = dict(spec.get('native', {}))
        require(not set(native) & {'task', 'consumer', 'sources', 'targets'},
                'native additions cannot replace the bound task, consumer, sources or targets')
        directory.mkdir(parents=True)
        operation_bytes = encoded(spec)
        request_path = directory / 'operation.json'
        request_path.write_bytes(operation_bytes)
        contract = Path(__file__).with_name('CONTRACT.md')
        require(contract.is_file(), 'adapter contract is missing')
        (directory / 'contract.md').write_bytes(contract.read_bytes())
        rel = directory.relative_to(self.ex.workspace).as_posix()
        native.update(task=spec['task'], consumer=spec['consumer'], targets=spec['targets'],
                      sources=[*reps, *sources, rel + '/operation.json', rel + '/contract.md'])
        # Native preparation captures actual complete selected bodies and targets.
        result = self.system.context(spec['turn'], native)
        result['representation_operation'] = {'path': rel + '/operation.json',
            'sha256': digest(operation_bytes), 'mode': spec['mode'],
            'adapter_sha256': digest(Path(__file__).read_bytes()),
            'scope': 'Exact selected artifact files; unknown interactions remain authored questions.'}
        return result

    def operation(self, turn):
        """Read the operation from its native immutable capture, not a mutable index."""
        _, request = self.ex.load(turn)
        name = (self.location(turn) / 'operation.json').relative_to(self.ex.workspace).as_posix()
        row = next((r for r in request['materials'] if r['path'] == name), None)
        require(row is not None, 'native turn has no captured representation operation')
        raw = self.ex.get(row['sha256'])
        spec = json.loads(raw)
        require(spec.get('format') == OPERATION and spec.get('turn') == turn, 'captured operation identity changed')
        return spec, row['sha256'], request

    def template(self, turn):
        spec, key, _ = self.operation(turn)
        value = {'format': WITNESS, 'operation_sha256': key,
                 'representations': spec['representations'],
                 'applications': [{'id': app['id'], 'account': '', 'outputs': [], 'remaining': []}
                                  for app in spec['applications']]}
        if spec['mode'] == 'realize':
            value['realization'] = {'representations': spec['representations'], 'account': ''}
        return value

    def receive(self, turn, reply, witness, producer, proposals):
        spec, key, request = self.operation(turn)
        targets = {t['path']: t for t in request['targets']}
        outputs = []
        for supplied in proposals:
            target, sep, source = supplied.partition('=')
            require(sep and target in targets and target not in {o['path'] for o in outputs},
                    'proposal must name one unique captured target')
            raw = Path(source).read_bytes()
            after = json.loads(raw) if spec['mode'] != 'realize' else None
            before_digest = targets[target]['sha256']
            delta = None
            if spec['mode'] != 'realize':
                before = json.loads(self.ex.get(before_digest)) if before_digest else None
                delta = changed_paths(before, after)
            outputs.append({'path': target, 'before_sha256': before_digest,
                            'after_sha256': digest(raw), 'changed_json_pointers': delta})
        output_paths = {o['path'] for o in outputs}
        require(output_paths == set(targets), 'every selected target needs an actual candidate; use a narrower turn otherwise')
        self.validate_witness(spec, key, witness, output_paths)
        body = {'format': BOUND_RETURN, 'operation_sha256': key,
                'inputs': {'representations': spec['representations'], 'sources': spec['sources']},
                'outputs': outputs, 'authored_witness': witness,
                'actual_reply': Path(reply).read_text(),
                'scope': 'The author supplies meaning. Exact bindings and structural changes do not establish semantic adequacy.'}
        reply_path = self.location(turn) / 'bound-return.json'
        require(not reply_path.exists(), 'bound return already exists; inspect native return before recovery')
        reply_path.write_bytes(encoded(body))
        # Receive retains stale contributions too. Native adoption is the guard.
        return self.system.receive(turn, reply_path, producer, proposals)

    @staticmethod
    def validate_witness(spec, key, witness, output_paths):
        require(isinstance(witness, dict) and witness.get('format') == WITNESS, 'unsupported witness format')
        require(witness.get('operation_sha256') == key, 'witness names a different operation basis')
        require(witness.get('representations') == spec['representations'], 'witness representation basis is inconsistent')
        expected_apps = {a['id'] for a in spec['applications']}
        applications = witness.get('applications')
        require(isinstance(applications, list) and all(isinstance(a, dict) for a in applications),
                'application witnesses must be a list of objects')
        require(len(applications) == len(expected_apps) and {a.get('id') for a in applications} == expected_apps,
                'missing or duplicate source-application witness')
        for app in applications:
            text(app.get('account'), 'actual application account')
            strings(app.get('remaining'), 'remaining application questions')
            used = strings(app.get('outputs'), 'application output bindings')
            require(bool(used) and set(used) <= output_paths, 'application witness lacks an actual output binding')
        require(set().union(*(set(a['outputs']) for a in applications)) == output_paths,
                'an actual output lacks a source-application witness')
        if spec['mode'] == 'realize':
            realization = witness.get('realization')
            require(isinstance(realization, dict) and realization.get('representations') == spec['representations'],
                    'realization lacks the exact accepted representation binding')
            text(realization.get('account'), 'actual realization account')

    def adopt(self, turn, target, reason):
        spec, key, request = self.operation(turn)
        observed = self.ex.inspect(turn)
        received = observed.get('return')
        require(received is not None, 'no native representation return')
        bound = json.loads(self.ex.get(received['reply_sha256']))
        require(bound.get('format') == BOUND_RETURN and bound.get('operation_sha256') == key,
                'native return lacks the representation application/realization binding')
        require(bound.get('inputs') == {'representations': spec['representations'], 'sources': spec['sources']},
                'native return input bindings differ from the captured operation')
        proposed = {p['path']: p['sha256'] for p in received['proposals']}
        require({p['path']: p['after_sha256'] for p in bound['outputs']} == proposed,
                'bound outputs differ from native received proposals')
        captured_bases = {p['path']: p['sha256'] for p in request['targets']}
        require({p['path']: p['before_sha256'] for p in bound['outputs']} == captured_bases,
                'bound before-state differs from native captured targets')
        self.validate_witness(spec, key, bound.get('authored_witness'), set(proposed))
        require(target in spec['targets'], 'target outside representation operation')
        return self.system.adopt(turn, target, reason)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--engine', required=True, type=Path, metavar='PROTOTYPE',
                        help='native prototype directory, for example engine/prototype')
    parser.add_argument('--root', required=True, type=Path)
    actions = parser.add_subparsers(dest='action', required=True)
    actions.add_parser('prepare').add_argument('--spec', required=True, type=Path)
    template = actions.add_parser('template'); template.add_argument('turn')
    pin = actions.add_parser('pin'); pin.add_argument('path'); pin.add_argument('--accepted-in')
    receive = actions.add_parser('receive'); receive.add_argument('turn')
    receive.add_argument('--reply', required=True, type=Path)
    receive.add_argument('--witness', required=True, type=Path)
    receive.add_argument('--producer', required=True)
    receive.add_argument('--proposal', action='append', default=[])
    adopt = actions.add_parser('adopt'); adopt.add_argument('turn')
    adopt.add_argument('--target', required=True); adopt.add_argument('--reason', required=True)
    args = parser.parse_args()
    sys.path.insert(0, str(args.engine.resolve() / 'agent-system'))
    from system import System
    from continuation_support.storage import exclusive
    try:
        system = System(args.root)
        adapter = RepresentationOperations(system)
        with exclusive(system.ex.root / 'owner.lock'):
            if args.action == 'prepare':
                result = adapter.prepare(read_json(args.spec))
            elif args.action == 'template':
                result = adapter.template(args.turn)
            elif args.action == 'pin':
                result = adapter.pin(args.path, args.accepted_in)
            elif args.action == 'receive':
                result = adapter.receive(args.turn, args.reply, read_json(args.witness), args.producer, args.proposal)
            else:
                result = adapter.adopt(args.turn, args.target, args.reason)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
        print(json.dumps({'status': 'unresolved', 'operation': args.action, 'error': str(exc)}), file=sys.stderr)
        raise SystemExit(2)


if __name__ == '__main__':
    main()
