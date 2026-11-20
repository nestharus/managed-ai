"""Compare and receive one authored composition under its native owner guard."""
from collections import defaultdict
from contextlib import ExitStack
from types import SimpleNamespace

from composition import COLLECTIONS, PREFIXES, Composition
from continuation_support.ownership import NativeLayout
from continuation_support.storage import sha
from discovery import canonical, request as discovery_request
from information import pointer
from native_runtime.identities import exact_fields, identity, nonempty, record_identity, require, strict_json


COLLECTION = {prefix: name for name, prefix in PREFIXES.items()}
METADATA = {'purpose', 'unknowns', 'coverage', 'content'}


def project_edit(original, req):
    """Replace named rows in place. Preserve unmentioned rows and their order."""
    body, tables, _, _ = Composition(None).profile(original)
    candidate = dict(body)
    identity_fields = req['fields']
    exact_fields(identity_fields, set(), METADATA, 'changed composition fields')
    candidate.update(identity_fields)
    patches = req['rows']
    require(isinstance(patches, dict), 'rows must map typed references to rows or null')
    edits = defaultdict(dict)
    for ref, value in patches.items():
        prefix, sep, ident = ref.partition(':')
        require(sep and ident and prefix in COLLECTION, 'unknown composition reference: ' + ref)
        collection = COLLECTION[prefix]
        if value is None:
            require(ident in tables[collection], 'cannot remove absent row: ' + ref)
        else:
            require(isinstance(value, dict) and value.get('id') == ident, 'row identity differs: ' + ref)
        edits[collection][ident] = value
    order = req.get('order', {})
    exact_fields(order, set(), set(COLLECTIONS), 'composition order')
    for collection in COLLECTIONS:
        if collection not in edits and collection not in order:
            continue
        table = dict(tables[collection])
        for ident, value in edits[collection].items():
            if value is None:
                del table[ident]
            else:
                table[ident] = value
        ids = order.get(collection, list(table))
        require(isinstance(ids, list) and len(ids) == len(table) and set(ids) == set(table),
                'order must name each resulting ' + collection + ' row once')
        if ids or collection in body:
            candidate[collection] = [table[ident] for ident in ids]
    staged = dict(original, content=candidate)
    _, revised, _, _ = Composition(None).profile(staged)
    return staged, tables, revised


def row_map(tables):
    return {PREFIXES[c] + ':' + name: value for c in COLLECTIONS for name, value in tables[c].items()}


def row_use(row, keys):
    """Read only the use annotations belonging to the selected adapter."""
    result = {'uses': [], 'sources': []}
    detail = row.get('content')
    if isinstance(detail, dict):
        for key in keys:
            use = detail.get(key, {})
            exact_fields(use, set(), set(result), key)
            for field, values in use.items():
                require(isinstance(values, list), key + ' ' + field + ' must be a list')
                result[field].extend(values)
    return result


def dependencies(rows, keys):
    """Consumer -> supplied references. Resource nodes avoid expanding all pairs."""
    edges = defaultdict(set)
    for ref, row in rows.items():
        deps = edges[ref]
        use = row_use(row, keys)
        for source in use.get('uses', []):
            require(isinstance(source, str) and source.partition(':')[0] in COLLECTION,
                    'row use needs typed composition row references')
            deps.add(source)
        if ref.startswith('relation:'):
            deps.update('element:' + item for k in ('from', 'to') for item in row.get(k, []))
        if ref.startswith('opportunity:'):
            deps.update(row.get('at', []))
            deps.update('premise:' + item for group in row.get('requires', []) for item in group)
            for other in row.get('conflicts', []):
                peer = 'opportunity:' + other
                deps.add(peer)
                edges[peer].add(ref)
            for name in row.get('writes', []):
                edges[('resource', name)].add(ref)
            deps.update(('resource', name) for k in ('reads', 'writes') for name in row.get(k, []))
    return edges


def reach(initial, edges):
    reached = set(initial)
    stack = list(reached)
    while stack:
        for ref in edges.get(stack.pop(), ()):
            if ref not in reached:
                reached.add(ref)
                stack.append(ref)
    return reached


def changes(before, after, old_tables, new_tables, req, keys):
    old, new = row_map(old_tables), row_map(new_tables)
    changed = {ref: {'before': old.get(ref), 'after': new.get(ref)}
               for ref in sorted(old.keys() | new.keys()) if canonical(old.get(ref)) != canonical(new.get(ref))}
    fields = {k: {'before': before.get(k), 'after': after[k]} for k in req['fields']
              if k not in before or canonical(before[k]) != canonical(after[k])}
    order = {}
    moved = set()
    for c in req.get('order', {}):
        left = list(old_tables[c])
        right = list(new_tables[c])
        if left != right:
            order[c] = {'before': left, 'after': right}
            positions = {k: i for i, k in enumerate(left)}
            moved.update(PREFIXES[c] + ':' + k for i, k in enumerate(right) if positions.get(k) != i)
    old_deps, new_deps = dependencies(old, keys), dependencies(new, keys)
    def missing(rows, edges):
        return {(ref, dep) for ref in rows for dep in edges[ref] if isinstance(dep, str) and dep not in rows}
    newly_missing = missing(new, new_deps) - missing(old, old_deps)
    require(not newly_missing, 'new unresolved composition references: ' + str(sorted(newly_missing)))
    consumers = defaultdict(set)
    for deps in (old_deps, new_deps):
        for ref, inputs in deps.items():
            for item in inputs:
                consumers[item].add(ref)
    # A change to the meaning of the enclosing whole qualifies all its placements.
    seeds = old.keys() | new.keys() if fields else set(changed) | moved
    impact = reach(seeds, consumers)
    used = reach(impact & new.keys(), new_deps) & new.keys()
    inherited = reach(impact & old.keys(), old_deps) & old.keys()
    return ({'fields': fields, 'rows': changed, 'order': order},
            sorted(impact & (old.keys() | new.keys())), sorted(used), sorted(inherited))


def read_evidence(system, req, row_sources, external_reader):
    selected = req['sources']
    require(isinstance(selected, list) and selected, 'select evidence for the decision')
    allowed = {canonical(pin) for pin in selected}
    require(all(canonical(pin) in allowed for pin in row_sources),
            'affected current use requires a source outside the selected read set')
    exact_fields(req['bounds'], {'scan_limit', 'max_bytes'}, set(), 'read bounds')
    bounds = discovery_request(dict(req['bounds'], limit=1), False)
    pins = [strict_json(key) for key in sorted(allowed)]
    external = [pin for pin in pins if 'owner' in pin]
    files = external_bytes = 0
    if external_reader is not None:
        files, external_bytes = external_reader(external, bounds)
    else:
        require(not external, 'external evidence requires its domain reader')
    heads = {}
    native_bytes = 0
    for pin in pins:
        if 'owner' in pin:
            continue
        exact_fields(pin, {'instance_id', 'id', 'revision', 'sha256', 'pointer'}, set(), 'native evidence')
        require(pin['instance_id'] == system.instance_id, 'native evidence owner differs')
        wanted = identity({k: pin[k] for k in ('id', 'revision', 'sha256')})
        if pin['id'] not in heads:
            require(files + len(heads) < bounds['scan_limit'], 'evidence scan limit')
            record, raw = system.record_store.read(pin['id'], byte_limit=bounds['max_bytes'] - external_bytes - native_bytes)
            heads[pin['id']] = record
            native_bytes += len(raw)
        record = heads[pin['id']]
        require(wanted == record_identity(record), 'native evidence head revision conflict: ' + pin['id'])
        require(isinstance(pin['pointer'], str), 'expected native evidence pointer')
        pointer(record, pin['pointer'])
    return {'sources': pins, 'external_files': files, 'native_heads': len(heads),
            'external_bytes': external_bytes, 'native_bytes': native_bytes}


def revise(system, req, *, accept=None, retain=None, reason=None, external_reader=None,
           request_format='g60-recomposition/1', view_format='g60-recomposition-view/1',
           binding_keys=('composition_use',), proof_store=None, proof_routes=None):
    """Caller owns the store guard. A domain reader qualifies its external editions."""
    optional = {'order', 'focus', 'proof_preservation'}
    require(proof_store is None or 'proof_preservation' in req, 'proof store requires a preservation claim')
    if external_reader is not None:
        optional.add('withheld')
    exact_fields(req, {'format', 'instance_id', 'target', 'fields', 'rows', 'sources', 'bounds',
                       'judgment'}, optional, 'composition proposal')
    require(req['format'] == request_format, 'unsupported composition proposal')
    require(not (accept is not None and retain is not None), 'select one decision')
    nonempty(req['judgment'], 'proposal judgment and alternative')
    descriptor = NativeLayout(system.root).descriptor.read_bytes()
    require(strict_json(descriptor) == system.descriptor, 'selected store policy changed')
    require(req['instance_id'] == system.instance_id, 'selected instance differs')
    original = system.record_store.head(req['target'])
    staged, old_tables, new_tables = project_edit(original, req)
    system.domain_selection.validate_record(staged, original)
    delta, impact, used, inherited = changes(original['content'], staged['content'], old_tables, new_tables, req, binding_keys)
    new = row_map(new_tables)
    if req.get('focus') is not None:
        require(req['focus'] in new, 'requested focus is absent from proposed composition')
    evidence = [pin for ref in used for pin in row_use(new[ref], binding_keys).get('sources', [])]
    read_set = read_evidence(system, req, evidence, external_reader)
    # Use the shared bounded reader on the staged body, without publishing a speculative head.
    view = Composition(SimpleNamespace(ex=system.ex, show=lambda _: staged)).inspect(
        original['id'], focus=req.get('focus'), limit=10, pair_limit=0)
    view.pop('record')
    require(all(b['status'] == 'current' or (b['status'] == 'missing' and b['captured_sha256'] is None)
                for b in view['basis']),
            'captured workspace basis changed or unavailable')
    review = {'format': view_format, 'target': req['target'],
              'instance_id': system.instance_id, 'descriptor_sha256': sha(descriptor),
              'request_sha256': sha(canonical(req)), 'content_sha256': sha(canonical(staged['content'])),
              'judgment': req['judgment'], 'diff': delta, 'affected': impact,
              'previous_inputs': inherited, 'current_inputs': used, 'read_set': read_set, 'projection': view}
    with ExitStack() as stack:
        bound = None
        if 'proof_preservation' in req:
            from composition_opportunities import BoundOpportunities, guarded_inventory
            try:
                before = original['content']['content']
                after = staged['content']['content']
                binding = before['proof_use']
                require(binding == after['proof_use'], 'proposed proof authority differs')
                # This contract interprets a tree rotation. A changed surrounding use
                # needs its own qualified interpretation before a current claim.
                comparable = dict(staged['content'], content=dict(after, tree=before['tree']))
                require(canonical(original['content']) == canonical(comparable),
                        'changed receiving contract outside the bound tree')
                require(read_set['external_files'] + read_set['native_heads'] < req['bounds']['scan_limit'],
                        'inventory exceeds evidence scan limit')
                remaining = req['bounds']['max_bytes'] - read_set['native_bytes'] - read_set['external_bytes']
                owner = stack.enter_context(guarded_inventory(system, binding, proof_store))
                bound = BoundOpportunities(system, owner, binding, req['proof_preservation'],
                                           original, staged, remaining, proof_routes)
                review['proof_preservation'] = bound.observe()
            except (KeyError, TypeError, ValueError, OSError) as exc:
                review['proof_preservation'] = {'outcome': 'unresolved', 'preserves': False,
                                                'coverage': {'complete': False, 'unknowns': [str(exc)]}}
        def current_read_set():
            return read_evidence(system, req, evidence, external_reader)

        return receive(system, review, original, staged, delta, bound, current_read_set, accept, retain, reason)


def receive(system, review, original, staged, delta, bound, current_read_set, accept, retain, reason):
    certificate = canonical(review)
    basis = sha(certificate)
    review.update(review_sha256=basis, status='proposed')
    if accept is not None or retain is not None:
        require((accept if accept is not None else retain) == basis, 'reviewed composition or read set differs')
        review['decision_reason'] = nonempty(reason, 'owner decision reason')
        proof = review.get('proof_preservation')

        def verify_at_use():
            require(current_read_set() == review['read_set'], 'selected evidence changed at publication')
            require(bound is not None and canonical(bound.observe()) == canonical(proof),
                    'proof inventory or receiving contract changed at publication')
            require(proof['preserves'], 'current complete proof preservation required: ' + proof['outcome'])
            bound.retain_inputs()
            review['certificate_object'] = system.ex.objects.put(certificate)

        if retain is not None or not any(delta.values()):
            if proof is not None:
                if accept is not None:
                    verify_at_use()
                else:
                    if bound is not None:
                        bound.retain_inputs()
                    review['certificate_object'] = system.ex.objects.put(certificate)
            review.update(status='retained', head=record_identity(original))
        else:
            record = {k: staged[k] for k in ('id', 'kind', 'active', 'relations', 'content')}
            record['basis'] = [b['path'] for b in original['basis']]
            options = {'verify': verify_at_use} if proof is not None else {}
            written = system.record_store.put_reviewed(record, original['revision'], original['basis'], **options)
            installed = system.record_store.head(record_identity(written))
            require(canonical(installed['content']) == canonical(staged['content']),
                    'published head differs from reviewed content')
            review.update(status='accepted', head=record_identity(installed))
    else:
        require(reason is None, 'supply a reason with an owner decision')
    return review
