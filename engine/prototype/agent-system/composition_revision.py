"""Compare and receive one authored composition under its native owner guard."""
from collections import defaultdict
from contextlib import ExitStack
from types import SimpleNamespace

from composition import COLLECTIONS, MAX_VIEW_BYTES, PREFIXES, PROFILE, Composition
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


def reverse_dependencies(*graphs):
    consumers = defaultdict(set)
    for graph in graphs:
        for ref, inputs in graph.items():
            for item in inputs:
                consumers[item].add(ref)
    return consumers


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
    consumers = reverse_dependencies(old_deps, new_deps)
    # A change to the meaning of the enclosing whole qualifies all its placements.
    seeds = old.keys() | new.keys() if fields else set(changed) | moved
    impact = reach(seeds, consumers)
    used = reach(impact & new.keys(), new_deps) & new.keys()
    inherited = reach(impact & old.keys(), old_deps) & old.keys()
    return ({'fields': fields, 'rows': changed, 'order': order},
            sorted(impact & (old.keys() | new.keys())), sorted(used), sorted(inherited))


def projected_coordinate(original, staged, positions, path):
    """Compare the selected value, following a declared composition row by its typed ID."""
    try:
        before = pointer(original, path)
    except ValueError:
        return {'status': 'unresolved-incumbent-coordinate'}
    if path != '/content' and not path.startswith('/content/'):
        if original is staged:
            return {'status': 'unchanged', 'proposed_pointer': path}
        return {'status': 'unprojected-envelope'}
    parts = path.split('/')
    proposed = path
    row = {}
    if len(parts) >= 4 and parts[2] in COLLECTIONS:
        # The successful incumbent read established a canonical array index.
        ident = original['content'][parts[2]][int(parts[3])]['id']
        row = {'source_row': PREFIXES[parts[2]] + ':' + ident}
        index = positions[parts[2]].get(ident)
        if index is None:
            return dict(row, status='removed')
        parts[3] = str(index)
        proposed = '/'.join(parts)
    try:
        after = pointer(staged, proposed)
    except ValueError:
        return dict(row, status='removed')
    changed = canonical(before) != canonical(after)
    status = 'changed' if changed else 'unchanged'
    if path != proposed:
        status = 'relocated-and-changed' if changed else 'relocated'
    return dict(row, proposed_pointer=proposed, status=status)


def inspect_consumers(system, ids, original, staged, new_tables, keys, scan_limit, max_bytes):
    """Locate authored same-instance row uses within the explicitly supplied records."""
    require(isinstance(ids, list) and ids and all(isinstance(i, str) and i for i in ids)
            and len(ids) == len(set(ids)), 'consumer_scope needs distinct record IDs')
    require(len(ids) <= scan_limit, 'consumer scope exceeds evidence scan limit')
    positions = {c: {ident: i for i, ident in enumerate(new_tables[c])} for c in COLLECTIONS}
    incumbent = record_identity(original)
    coordinates, checked, candidates = {}, [], []
    used_bytes = 0
    # Charge each output fragment once, including its separator, before accumulating it.
    view_bytes = len(canonical({'examined': [], 'bytes': max_bytes, 'candidates': []}))

    def charge(fragment):
        nonlocal view_bytes
        view_bytes += len(canonical(fragment)) + 1
        require(view_bytes <= MAX_VIEW_BYTES, 'consumer scope exceeds composition view byte bound')

    for ident in ids:
        record, raw = system.record_store.read(ident, byte_limit=max_bytes - used_bytes)
        used_bytes += len(raw)
        head = record_identity(record)
        charge(head)
        checked.append(head)
        body = record.get('content')
        if not isinstance(body, dict) or body.get('format') != PROFILE:
            continue
        _, tables, _, _ = Composition(None).profile(record)
        rows = row_map(tables)
        bindings, seeds = [], set()
        for ref, row in rows.items():
            for pin in row_use(row, keys)['sources']:
                if (not isinstance(pin, dict) or 'owner' in pin or pin.get('id') != original['id']
                        or pin.get('instance_id') != system.instance_id):
                    continue
                exact_fields(pin, {'instance_id', 'id', 'revision', 'sha256', 'pointer'}, set(),
                             'consumer native evidence')
                captured = identity(record_identity(pin))
                path = pin['pointer']
                require(isinstance(path, str), 'expected native evidence pointer')
                if captured != incumbent:
                    result = {'status': 'unresolved-edition'}
                else:
                    if path not in coordinates:
                        coordinates[path] = projected_coordinate(original, staged, positions, path)
                    result = coordinates[path]
                binding = dict(result, consumer_row=ref, captured=pin)
                charge(binding)
                bindings.append(binding)
                if result['status'] != 'unchanged':
                    seeds.add(ref)
        if bindings:
            affected = set()
            if seeds:
                affected = reach(seeds, reverse_dependencies(dependencies(rows, keys))) & rows.keys()
            candidate = dict(target=head, bindings=[], affected=sorted(affected))
            charge(candidate)
            candidate['bindings'] = bindings
            candidates.append(candidate)
    return {'examined': checked, 'bytes': used_bytes, 'candidates': candidates}


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
    optional = {'order', 'focus', 'proof_preservation', 'consumer_scope'}
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
    remaining_heads = req['bounds']['scan_limit'] - read_set['external_files'] - read_set['native_heads']
    remaining_bytes = req['bounds']['max_bytes'] - read_set['external_bytes'] - read_set['native_bytes']
    if 'consumer_scope' in req:
        projected = staged if any(delta.values()) else original
        scope = inspect_consumers(system, req['consumer_scope'], original, projected, new_tables, binding_keys,
                                  remaining_heads, remaining_bytes)
        review['consumer_scope'] = scope
        remaining_heads -= len(scope['examined'])
        remaining_bytes -= scope['bytes']
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
                require(remaining_heads > 0, 'inventory exceeds evidence scan limit')
                owner = stack.enter_context(guarded_inventory(system, binding, proof_store))
                bound = BoundOpportunities(system, owner, binding, req['proof_preservation'],
                                           original, staged, remaining_bytes, proof_routes)
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
            if proof is not None:
                require(bound is not None and canonical(bound.observe()) == canonical(proof),
                        'proof inventory or receiving contract changed at publication')
                require(proof['preserves'], 'current complete proof preservation required: ' + proof['outcome'])
                bound.retain_inputs()
                review['certificate_object'] = system.ex.objects.put(certificate)

        # Native heads share our owner guard. External originals can advance independently.
        verify = verify_at_use if proof is not None or review['read_set']['external_files'] else None
        if retain is not None or not any(delta.values()):
            if accept is not None and verify is not None:
                verify()
            elif proof is not None:
                if bound is not None:
                    bound.retain_inputs()
                review['certificate_object'] = system.ex.objects.put(certificate)
            review.update(status='retained', head=record_identity(original))
        else:
            record = {k: staged[k] for k in ('id', 'kind', 'active', 'relations', 'content')}
            record['basis'] = [b['path'] for b in original['basis']]
            written = system.record_store.put_reviewed(record, original['revision'], original['basis'], verify=verify)
            installed = system.record_store.head(record_identity(written))
            require(canonical(installed['content']) == canonical(staged['content']),
                    'published head differs from reviewed content')
            review.update(status='accepted', head=record_identity(installed))
    else:
        require(reason is None, 'supply a reason with an owner decision')
    return review
