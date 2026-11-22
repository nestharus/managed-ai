"""Capture one view of current heads; measure effect admission against fresh heads."""
from __future__ import annotations

from dataclasses import dataclass

from continuation_support.ownership import packet_id
from feedback import KIND as FEEDBACK_KIND
from native_runtime.inputs import artifact_target
from turn_preparation import feedback_id


@dataclass
class RecordSelection:
    records: list[dict]
    omitted: list[str]
    outside: list[str]
    routes: list[dict]
    guard: dict
    origins: dict


class TurnScope:
    def __init__(self, records, selected=()):
        self.store = records
        self.digests = {}
        self.retained = {}
        self.governing = {}
        self.feedback = {}
        selected = set(selected)
        def needs_body(ident, kind):
            return self.governs(ident, kind) or kind == FEEDBACK_KIND or ident in selected

        for record in records.scoped_heads(needs_body):
            ident = record['id']
            self.digests[ident] = record['sha256']
            if self.governs(ident, record['kind']):
                self.governing[ident] = record
            if record['kind'] == FEEDBACK_KIND:
                self.feedback[ident] = record
            if ident in selected:
                self.retained[ident] = record

    @staticmethod
    def governs(ident, kind):
        return ident in {'goal', 'field', 'frontier'} or kind == 'obligation'

    def selected_head(self, ident):
        if ident in self.governing:
            return self.governing[ident]
        if ident in self.feedback:
            return self.feedback[ident]
        if ident in self.retained:
            return self.retained[ident]
        # A feedback use may select a source only after the complete scan.
        record = self.store.show(ident)
        if record['sha256'] != self.digests.get(ident):
            raise ValueError('feedback source changed during turn preparation')
        self.retained[ident] = record
        return record

    def select(self, request: dict) -> tuple[list[dict], list[str], list[str], dict]:
        requested = request['records']
        missing = set(requested).difference(self.digests)
        if missing:
            raise ValueError('unknown records: ' + ', '.join(sorted(missing)))
        limit = request['record_limit']
        selected, origins = {}, {}
        for ident in self.governing:
            selected[ident] = None
            origins[ident] = [{'kind': 'governing-record'}]
        for ident in requested[:limit]:
            selected[ident] = None
            origins.setdefault(ident, []).append({'kind': 'normalized-request-record'})
        omitted = [ident for ident in requested[limit:] if ident not in selected]
        inside = set(selected).union(omitted)
        outside = [ident for ident in self.digests if ident not in inside]
        return [self.selected_head(ident) for ident in selected], omitted, outside, origins

    def route(self, request, feedback, all_feedback):
        uses = self.feedback.values()
        chosen = set(request.get('feedback_uses', []))
        if chosen.difference(self.feedback):
            raise ValueError('unknown feedback use')
        guarded = []
        for use in uses:
            if use['id'] in chosen:
                guarded.append(feedback.route(use, lookup=self.selected_head))
            elif all_feedback:
                guarded.append(feedback.route(use))
        routes = [r for r in guarded if r['state'] != 'accepted' or r['id'] in chosen]
        feedback_sources, feedback_origins = set(), {}
        for use in uses:
            if use['id'] in chosen:
                feedback_sources.add(use['id'])
                feedback_origins.setdefault(use['id'], []).append(
                    {'kind': 'feedback-use', 'feedback': use['id']})
                for pin in feedback.bindings(use['content']):
                    if pin['id'] in self.digests:
                        feedback_sources.add(pin['id'])
                        reason = {'kind': 'feedback-bound-record', 'feedback': use['id']}
                        reasons = feedback_origins.setdefault(pin['id'], [])
                        if reason not in reasons:
                            reasons.append(reason)
        selected, omitted, outside, origins = self.select(request)
        for ident, reasons in feedback_origins.items():
            origins.setdefault(ident, []).extend(reasons)
        for ident in sorted(feedback_sources - {r['id'] for r in selected}):
            selected.append(self.selected_head(ident))
        omitted = [ident for ident in omitted if ident not in feedback_sources]
        outside = [ident for ident in outside if ident not in feedback_sources]
        selected_ids = {r['id'] for r in selected}
        if any(row['record'] not in selected_ids for row in request['compositions']):
            raise ValueError('a composition record was omitted by record_limit; select its complete basis explicitly')
        return RecordSelection(selected, omitted, outside, routes, feedback_guard(guarded), origins)


def additional_basis(unavailable: list[dict], feedback: dict) -> list[dict]:
    rows = [{'path': r['path'], 'sha256': None, 'bytes': 0} for r in unavailable]
    if feedback.get('field') is not None:
        rows.append(feedback['field'])
    for delivered in feedback['delivered']:
        rows.extend(delivered['basis'])
        rows.extend({'path': p['path'], 'sha256': p['current_sha256']}
                    for p in delivered['products'])
    return rows


def feedback_guard(routes: list[dict]) -> dict:
    return {r['id']: {'revision': r['revision'], 'stale': r['stale']} for r in routes}


class NativeEffects:
    """Use the native owner's current view before a dispatch or publication."""

    def __init__(self, root, view, records, feedback, attention):
        self.root = root
        self.view = view
        self.records = records
        self.feedback = feedback
        self.attention = attention

    def require_owner(self, owner):
        if owner != self.root:
            raise ValueError('effect destination belongs to a different or unavailable native owner')

    def authorize_dispatch(self, owner, turn):
        self.require_owner(owner)
        self.check_feedback(self.view(turn))

    def authorize_publication(self, owner, turn: str, target: str) -> None:
        self.require_owner(owner)
        artifact_target(target)
        view = self.view(packet_id(turn))
        heads = TurnScope(self.records)
        self.check_feedback(view, heads=heads)
        if list(heads.governing) != view['governing_records']:
            raise ValueError('current goal/obligation/field membership changed; receive a fresh current use')
        basis = view.get('additional_basis')
        if basis is None:
            basis = additional_basis(view['unavailable_record_basis'], self.attention.inspect(feedback_id(turn)))
        if self.records.basis_changes({'basis': basis}):
            raise ValueError('declared additional basis changed; preserve this return and construct its current use')

    def check_feedback(self, view: dict, heads=None):
        guard = view.get('feedback_guard')
        if guard is None:  # Packets prepared before native feedback existed.
            return
        if guard['all']:
            records = (heads or TurnScope(self.records)).feedback.values()
        else:
            records = [self.records.show(ident) for ident in guard['uses']]
        current = feedback_guard([self.feedback.route(r) for r in records])
        if current != guard['uses']:
            raise ValueError('routed feedback changed; prepare a fresh context before use')
