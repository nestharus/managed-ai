"""Compare budget opportunities on declared occurrence trees and dependency graphs.

All membership, alternatives and context choices come from the maintained owner
record. The caller supplies only a generation pin. No domain rule is inferred.
"""
from contextlib import contextmanager
from pathlib import Path

from core.formats import OPPORTUNITY_INVENTORY_FORMAT
from core.ownership import NativeLayout
from core.storage import MAX_BYTES, admission_deadline, is_ordinary_file_stat, owner_admission, sha, shared
from store.protocol.identities import digest, exact_fields, identity, nonempty, record_identity, require, strict_json
from store.query.context_discovery import canonical


WIDTH = 'maximal-contained-groups'
REPLAY = {'expanded-applications', 'distinct-applications'}


def names(values, label):
    require(isinstance(values, list) and values, label + ' requires a nonempty list')
    result = [nonempty(value, label) for value in values]
    require(len(result) == len(set(result)), 'duplicate ' + label)
    return result


def mapping(value, label):
    require(isinstance(value, dict) and value, label + ' requires a nonempty map')
    for key in value:
        nonempty(key, label)
    return value


def groups(tree):
    """Return the clades of a possibly multifurcating occurrence tree."""
    clades = []
    seen = set()

    def visit(node):
        if isinstance(node, str):
            nonempty(node, 'occurrence')
            require(node not in seen, 'repeated layout occurrence: ' + node)
            seen.add(node)
            leaves = frozenset([node])
        else:
            require(isinstance(node, list) and len(node) >= 2, 'expected internal group or occurrence')
            leaves = frozenset().union(*(visit(child) for child in node))
        clades.append(leaves)
        return leaves

    visit(tree)
    return clades


def routes(inventory):
    """Resolve grounded roots once. Repeated input edges count on expanded replay."""
    occurrences = mapping(inventory['occurrences'], 'occurrence coding')
    rules = mapping(inventory['rules'], 'rule definitions')
    for coordinate in occurrences.values():
        exact_fields(coordinate, {'symbol', 'source'}, set(), 'coded occurrence')
        nonempty(coordinate['symbol'], 'occurrence symbol')
        source = coordinate['source']
        require(isinstance(source, dict), 'missing dated occurrence coordinate')
        digest(source.get('sha256'), 'dated source digest')
        nonempty(source.get('locator'), 'dated occurrence locator')
        names(source.get('releases'), 'eligible release')
    for rule in rules.values():
        require(isinstance(rule, dict), 'missing rule definition')
        nonempty(rule['head'], 'rule conclusion')
        require(isinstance(rule['body'], list) and rule['body'], 'rule needs ordered premises')
        for premise in rule['body']:
            nonempty(premise, 'rule premise')
    nodes = mapping(inventory['nodes'], 'dependency nodes')
    cache, active = {}, set()

    def resolve(node):
        if node in cache:
            return cache[node]
        require(node in nodes, 'absent dependency: ' + node)
        require(node not in active, 'cyclic dependency: ' + node)
        active.add(node)
        row = nodes[node]
        if isinstance(row, dict) and 'occurrence' in row:
            exact_fields(row, {'occurrence'}, set(), 'grounded node')
            leaf = nonempty(row['occurrence'], 'grounded occurrence')
            require(leaf in occurrences, 'uncoded occurrence: ' + leaf)
            require(inventory['release'] in occurrences[leaf]['source']['releases'],
                    'grounded occurrence is unavailable at this cut: ' + leaf)
            result = frozenset([leaf]), 0, frozenset(), occurrences[leaf]['symbol']
        else:
            exact_fields(row, {'rule', 'inputs'}, set(), 'application')
            rule = nonempty(row['rule'], 'applied rule')
            require(rule in rules, 'unregistered rule: ' + rule)
            require(isinstance(row['inputs'], list) and row['inputs'], 'ungrounded application: ' + node)
            children = [resolve(nonempty(i, 'dependency')) for i in row['inputs']]
            require([child[3] for child in children] == rules[rule]['body'],
                    'application premises differ from the registered rule: ' + node)
            result = (frozenset().union(*(c[0] for c in children)), 1 + sum(c[1] for c in children),
                      frozenset([node]).union(*(c[2] for c in children)), rules[rule]['head'])
        active.remove(node)
        cache[node] = result
        return result

    conclusions = mapping(inventory['conclusions'], 'owed conclusions')
    require(conclusions.keys() == inventory['duties'].keys(), 'missing or extra reason binding')
    duties = {}
    for duty, roots in mapping(inventory['duties'], 'owed duties').items():
        duties[duty] = {}
        for root in names(roots, 'eligible root'):
            support, expanded, distinct, conclusion = resolve(root)
            require(conclusion == conclusions[duty], 'alternative does not establish the owed reason: ' + root)
            duties[duty][root] = support, expanded, distinct
    return duties


def costs(alternatives, clades, replay):
    """Disjoint maximal contained clades uniquely partition an exact support."""
    result = []
    for root, (support, expanded, distinct) in alternatives.items():
        remaining = set(support)
        cover = []
        for clade in reversed(clades):
            if clade <= remaining:
                remaining.difference_update(clade)
                cover.append(sorted(clade))
        require(not remaining, 'layout omits a required occurrence')
        result.append({'root': root, 'groups': cover,
                       'budget': [len(cover), expanded if replay == 'expanded-applications' else len(distinct)]})
    return result


def affordable(cost, budget):
    return cost[0] <= budget[0] and cost[1] <= budget[1]


def frontier(rows):
    points = sorted({tuple(row['budget']) for row in rows})
    frontier_points = []
    for width, replay in points:
        if not frontier_points or replay < frontier_points[-1][1]:
            frontier_points.append([width, replay])
    return frontier_points


def opportunities(points, rows, rivals):
    return [{'budget': point, 'witness': next(r for r in rows if r['budget'] == point),
             'other_alternatives': rivals}
            for point in points if not any(affordable(r['budget'], point) for r in rivals)]


def outcome(gained, lost):
    if lost:
        return 'incomparable' if gained else 'loss'
    return 'gain' if gained else 'equal'


def compare(inventory, before, after, route_reader=routes):
    exact_fields(inventory, {'format', 'target', 'release', 'portfolio', 'width', 'replay', 'occurrences',
                             'rules', 'nodes', 'duties', 'conclusions', 'receivers', 'contexts', 'actual_context',
                             'coverage'}, set(), 'opportunity inventory')
    require(inventory['format'] == OPPORTUNITY_INVENTORY_FORMAT, 'unsupported inventory')
    require(inventory['width'] == WIDTH and inventory['replay'] in REPLAY, 'unsupported budget convention')
    for key in ('release', 'portfolio'):
        nonempty(inventory[key], key)
    coverage = inventory['coverage']
    exact_fields(coverage, {'scope', 'complete', 'unknowns'}, set(), 'coverage')
    nonempty(coverage['scope'], 'coverage scope')
    require(type(coverage['complete']) is bool and isinstance(coverage['unknowns'], list), 'invalid coverage')
    for unknown in coverage['unknowns']:
        nonempty(unknown, 'unresolved coverage')
    require(coverage['complete'] == (not coverage['unknowns']), 'coverage declaration conflicts with unknowns')
    mapping(inventory['duties'], 'owed duties')
    mapping(inventory['conclusions'], 'owed reason bindings')
    alternatives = route_reader(inventory)
    require(alternatives.keys() == inventory['duties'].keys() and
            all(set(alternatives[d]) == set(roots) for d, roots in inventory['duties'].items()),
            'qualified routes omit an owed duty or alternative')
    members = set(inventory['occurrences'])
    contexts = mapping(inventory['contexts'], 'bound exterior contexts')
    actual = nonempty(inventory['actual_context'], 'actual exterior context')
    require(actual in contexts, 'actual exterior is outside the checked class')
    for tree in (before, after):
        require(isinstance(tree, list) and len(tree) == 2 and tree[1] == contexts[actual],
                'composition exterior differs from the receiving context')
    receivers = mapping(inventory['receivers'], 'consuming contracts')
    for duties in receivers.values():
        require(set(names(duties, 'consumed duty')) <= alternatives.keys(), 'consumer requires an absent duty')
    require(set().union(*(set(x) for x in receivers.values())) == alternatives.keys(),
            'owed and consumed duty membership differs')
    results, context_outcomes, changed = {}, {}, set()
    gained = lost = False
    report_bytes = 0
    for context, exterior in contexts.items():
        old, new = groups([before[0], exterior]), groups([after[0], exterior])
        require(old[-1] == new[-1] == members, 'layout and coded occurrence membership differ')
        changed_groups = set(old).symmetric_difference(new)
        duties = {}
        for duty, roots in alternatives.items():
            left = costs(roots, old, inventory['replay'])
            # An edited clade can change a read only if a complete eligible
            # support contains it. Other duties keep their exact read witnesses.
            affected = any(group <= support for group in changed_groups for support, _, _ in roots.values())
            right = costs(roots, new, inventory['replay']) if affected else left
            a, b = frontier(left), frontier(right)
            plus, minus = opportunities(b, right, left), opportunities(a, left, right)
            duties[duty] = {'old': a, 'new': b, 'gained': plus, 'lost': minus,
                            'old_alternatives': left, 'new_alternatives': right}
            report_bytes += len(canonical(duties[duty]))
            require(report_bytes <= MAX_BYTES, 'context class exceeds the retained report bound')
            if plus or minus:
                changed.add(duty)
            gained |= bool(plus)
            lost |= bool(minus)
        results[context] = duties
        context_outcomes[context] = outcome(any(d['gained'] for d in duties.values()),
                                            any(d['lost'] for d in duties.values()))
    relation = outcome(gained, lost)
    return {'coverage': coverage, 'available_relation': relation,
            'outcome': relation if coverage['complete'] else 'unresolved',
            'preserves': coverage['complete'] and not lost, 'contexts': results,
            'release': inventory['release'], 'portfolio': inventory['portfolio'],
            'context_outcomes': context_outcomes, 'width': inventory['width'], 'replay': inventory['replay'], 'actual_context': actual,
            'receivers': receivers, 'changed_frontiers': sorted(changed),
            'reopen_receivers': sorted(name for name, uses in receivers.items() if changed.intersection(uses))}


@contextmanager
def guarded_inventory(system, binding, selected, *, already_locked: bool = False, deadline=None):
    """Use the target owner's existing guard or hold a shared reader guard on the bound proof store."""
    exact_fields(binding, {'store', 'instance_id', 'id'}, set(), 'inventory authority')
    location = (system.root / nonempty(binding['store'], 'inventory store')).resolve()
    if location == system.root.resolve():
        require(selected is None or Path(selected).resolve() == location, 'wrong proof store')
        yield system
    else:
        require(selected is not None and Path(selected).resolve(strict=True) == location,
                'select the incumbent composition\u2019s bound proof store')
        layout = NativeLayout(location)
        lock_path = layout.exchange / 'owner.lock'
        try:
            descriptor_ok = is_ordinary_file_stat(layout.descriptor.lstat())
            lock_ok = is_ordinary_file_stat(lock_path.lstat())
        except OSError:
            descriptor_ok = lock_ok = False
        require(descriptor_ok and lock_ok, 'proof store unavailable')
        if already_locked:
            yield type(system)(location)
            return
        with owner_admission(
                lambda stack: stack.enter_context(shared(lock_path, allow_readonly_open=True)),
                deadline=admission_deadline(deadline)):
            yield type(system)(location)


class BoundOpportunities:
    def __init__(self, system, owner, binding, claim, original, proposed, bounds, route_reader):
        self.system, self.owner, self.binding = system, owner, binding
        exact_fields(claim, {'inventory'}, set(), 'proof preservation selection')
        self.pin = identity(claim['inventory'])
        self.original, self.proposed = original, proposed
        self.bounds, self.route_reader = bounds, route_reader or routes
        self.inventory_bytes = None

    def observe(self):
        base = {'authority': self.binding, 'inventory': self.pin,
                'old_sha256': sha(canonical(self.original['content'])),
                'proposed_sha256': sha(canonical(self.proposed['content']))}
        try:
            require(self.binding['instance_id'] == self.owner.instance_id and self.binding['id'] == self.pin['id'],
                    'inventory owner or selected record differs')
            require(self.owner is not self.system or self.pin['id'] != self.original['id'],
                    'inventory must be independent of its proposed replacement')
            descriptor = NativeLayout(self.owner.root).descriptor.read_bytes()
            require(strict_json(descriptor) == self.owner.descriptor, 'inventory owner policy changed')
            base['inventory_descriptor_sha256'] = sha(descriptor)
            record, raw = self.owner.record_store.read(self.pin['id'], byte_limit=self.bounds)
            base['observed_inventory'] = record_identity(record)
            require(record_identity(record) == self.pin and record['active'], 'inventory generation changed or inactive')
            self.inventory_bytes = raw
            base['inventory_object'] = sha(raw)
            inventory = record['content']
            require(inventory['target'] == {'instance_id': self.system.instance_id, 'id': self.original['id']},
                    'receiving authority differs')
            require(not record['basis'], 'current inventory requires a self-contained owner generation')
            before, after = (r['content']['content']['tree'] for r in (self.original, self.proposed))
            result = compare(inventory, before, after, self.route_reader)
            return dict(base, **result)
        except (KeyError, ValueError, TypeError, OSError, RecursionError) as exc:
            return dict(base, outcome='unresolved', preserves=False,
                        coverage={'complete': False, 'unknowns': [str(exc)]})

    def retain_inputs(self):
        objects = self.system.ex.objects
        for record in (self.original, self.proposed):
            objects.put(canonical(record['content']))
        if self.inventory_bytes is not None:
            objects.put(self.inventory_bytes)
