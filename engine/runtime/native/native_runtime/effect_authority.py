"""Admit dispatch and publication against the selected native owner's current state."""
from continuation_support.ownership import packet_id
from native_runtime.inputs import artifact_target
from native_runtime.turn_context.preparation_journal import feedback_id
from native_runtime.turn_context.record_scope import TurnScope, additional_basis, feedback_guard


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
