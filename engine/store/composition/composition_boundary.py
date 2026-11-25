"""Inspect substitutions recorded at a proposed communication boundary."""
import hashlib
import json

from store.query.information import CONTRACT_KIND
from store.protocol.identities import exact_fields, identity, record_identity, require, strict_json
from store.composition.composition import Composition, MAX_PROFILE_BYTES, MAX_VIEW_BYTES, _integer, _strings, _text


from core.formats import (
    BOUNDARY_CONTRACT_FORMAT,
    BOUNDARY_OBSERVATIONS_V1 as TRIAL_V1,
    BOUNDARY_OBSERVATIONS_V2 as TRIAL_V2,
    BOUNDARY_VIEW_V1,
    BOUNDARY_VIEW_V2,
)


def measuring_contract(system, wanted, relation):
    pin = identity(wanted)
    record, _ = system.record_store.read(pin['id'], byte_limit=MAX_PROFILE_BYTES)
    require(record_identity(record) == pin, 'measuring contract revision changed')
    require(record['kind'] == CONTRACT_KIND, 'expected a content-contract record')
    definition = record['content']
    exact_fields(definition, {'format','use','from','to','coordinates','effect'}, {'allowed_effects'}, 'measuring contract')
    require(definition['format'] == BOUNDARY_CONTRACT_FORMAT, 'unsupported measuring contract definition')
    _text(definition['use'], 'receiving use')
    _text(definition['effect'], 'effect meaning')
    for side in ('from', 'to'):
        roles = _strings(definition[side], 'contract roles')
        require(set(roles) == set(relation[side]), 'relation roles differ from measuring contract')
    require(len(_strings(definition['coordinates'], 'ordered coordinate meanings')) <= 64,
            'measuring contract allows at most 64 coordinates')
    if 'allowed_effects' in definition:
        codes = definition['allowed_effects']
        require(isinstance(codes, list) and 1 <= len(codes) <= 4096
                and all(type(code) is int and code.bit_length() <= 63 for code in codes)
                and len(codes) == len(set(codes)), 'invalid allowed effect codes')
    return definition


def analyze(trial, limit, definition=None):
    if definition is None:
        require(trial.get('format') == TRIAL_V1 and 'contract' not in trial,
                'expected unbound v1 observations for this legacy relation')
        use = _text(trial.get('use'), 'receiving use')
    else:
        use = definition['use']
    allowed = set(definition['allowed_effects']) if definition and 'allowed_effects' in definition else None
    variants = _strings(trial.get('variants'), 'variants')
    contexts = _strings(trial.get('contexts'), 'controlled receiver settings')
    if any(len(x.encode('utf-8')) > 200 for x in variants + contexts):
        raise ValueError('variant and context labels must fit in 200 UTF-8 bytes')
    if not variants or not contexts or len(variants) * len(contexts) > 4096:
        raise ValueError('declare between 1 and 4096 variant/setting combinations')
    samples = trial.get('observations')
    if not isinstance(samples, list) or len(samples) > 4096:
        raise ValueError('expected at most 4096 observations')
    observed, groups = {}, {}
    declared = {(v,c) for v in variants for c in contexts}
    for n, row in enumerate(samples, 1):
        if not isinstance(row, dict):
            raise ValueError('invalid observation')
        variant = _text(row.get('variant'), 'variant')
        context = _text(row.get('context'), 'receiver setting')
        if (variant,context) not in declared:
            raise ValueError('undeclared variant or receiver setting')
        message, effect = row.get('message'), row.get('effect')
        if not isinstance(message, list) or len(message) > 64 or any(type(x) is not int or x.bit_length() > 63 for x in message):
            raise ValueError('the measured message must contain at most 64 integer coordinates')
        if type(effect) is not int or effect.bit_length() > 63:
            raise ValueError('the measured receiving effect must be an integer under the declared instrument')
        if definition:
            require(len(message) == len(definition['coordinates']), 'message width differs from measuring contract')
            require(allowed is None or effect in allowed, 'effect outside contract codebook')
        _text(row.get('witness'), 'observation provenance')
        signal = tuple(message)
        observed.setdefault((variant, context), set()).add((signal, effect))
        # Keep the first occurrence of each distinct measured combination.
        groups.setdefault((context, signal), {}).setdefault((variant,effect), n)

    missing = sorted(declared - observed.keys())
    unstable = sorted(key for key, outcomes in observed.items() if len(outcomes) > 1)
    failures, compared = [], 0
    for (setting, signal), by_outcome in groups.items():
        if len({v for v,_ in by_outcome}) < 2:
            continue
        compared += 1
        # Two linear passes suffice: any dissimilar pair must differ in at least
        # one coordinate from the first, or form a cross with its alternatives.
        first = next(iter(by_outcome))
        others = list(by_outcome)
        pair = next(((first, x) for x in others if first[0]!=x[0] and first[1]!=x[1]), None)
        if pair is None:
            left = next((x for x in others if x[0]!=first[0]), None)
            right = next((x for x in others if x[1]!=first[1]), None)
            if left and right:
                pair = left, right
        if pair:
            a,b = pair
            failures.append(dict(setting=setting, message=list(signal),
                                 observations=[by_outcome[a], by_outcome[b]]))
    return dict(use=use, observations=len(samples), declared_cells=len(declared),
                exercised_cells=len(observed), compared_message_groups=compared,
                divergent_groups=len(failures), witnesses=failures[:limit],
                unstable_cells=len(unstable), unstable=unstable[:limit],
                missing_cells=len(missing), missing=missing[:limit],
                limit=limit, scope='Recorded measurements under the stated instrument and controls. A difference can reflect hidden state or measurement variation. Untested uses and variants remain open.')


def inspect_boundary(system, ident, relation_id, limit):
    _integer(limit, 'boundary detail limit', 200)
    record, _ = system.record_store.read(ident, byte_limit=4 * MAX_PROFILE_BYTES)
    _, rows, _, _ = Composition(system).profile(record)
    relation = rows['relations'].get(relation_id)
    if relation is None or not relation.get('from') or not relation.get('to') or set(relation['from']+relation['to']) - rows['elements'].keys():
        raise ValueError('select a relation with known producing and receiving endpoints')
    detail = relation.get('content')
    require(isinstance(detail, dict), 'relation requires measurement details')
    definition = None
    if 'measurement' in detail:
        require('observations_path' not in detail, 'select one measurement route')
        selection = detail['measurement']
        exact_fields(selection, {'contract','path'}, set(), 'contracted measurement route')
        definition = measuring_contract(system, selection['contract'], relation)
        name = selection['path']
    else:
        name = detail.get('observations_path')
    basis = {r['path']:r for r in record['basis']}
    if not isinstance(name,str) or name not in relation.get('basis',[]) or name not in basis:
        raise ValueError('record and relation must bind the observations path')
    # Read exactly the bytes whose identity is checked. A changed file requires a new binding.
    path = system.ex.path(name)
    if not path.is_file() or path.is_symlink():
        raise ValueError('observations require an ordinary file')
    with path.open('rb') as src:
        data = src.read(MAX_PROFILE_BYTES+1)
    if len(data)>MAX_PROFILE_BYTES or hashlib.sha256(data).hexdigest()!=basis[name]['sha256']:
        raise ValueError('boundary observations changed or exceed 2 MiB; revisit the evidence')
    trial = strict_json(data)
    if definition:
        require(trial.get('format') == TRIAL_V2, 'contracted relation requires v2 observations')
        exact_fields(trial, {'format','contract','variants','contexts','observations'}, set(), 'contracted observations')
        require(identity(trial['contract']) == selection['contract'], 'producer and receiver measuring contracts differ')
        binding = {'status':'bound', 'contract':selection['contract']}
    else:
        binding = {'status':'legacy-unbound'}
    result = analyze(trial, limit, definition)
    report = dict(format=BOUNDARY_VIEW_V2 if definition else BOUNDARY_VIEW_V1, binding=binding, record={k:record[k] for k in ('id','revision','sha256')},
                relation=relation_id, prediction=relation['prediction'], basis=basis[name], **result)
    if len(json.dumps(report, indent=2)) > MAX_VIEW_BYTES:
        raise ValueError('boundary report exceeds 1 MiB; reduce detail or scope')
    return report
