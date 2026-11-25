"""Local owner of the transition from an Attention selection to a published context."""
import os
import shutil
from pathlib import Path

from continuation_support.attention import Attention
from continuation_support.storage import now, read_json, sha, sync_dir, write_json
from continuation_support.ownership import packet_id
from native_runtime.formats import TURN_PREPARATION_FORMAT, validate_optional_format


def feedback_id(turn):
    # Ordinary identities retain their historical spelling. The long form has a disjoint prefix.
    turn = packet_id(turn)
    key = 'turn-' + turn
    return key if len(key) <= 120 else 'long-turn-' + sha(turn.encode('ascii'))


class TurnPreparation:
    def __init__(self, system, turn):
        self.ex = system.ex
        self.live = system.attention
        self.turn = packet_id(turn)
        self.directory = system.work / '.system' / 'turns' / turn
        self.marker = self.directory / 'preparation.json'
        self.draft = Attention(self.ex, directory=self.directory / 'attention-draft')

    def prepare(self, capture, request):
        """Publish an input capture and its Attention draft through one recoverable commit."""
        try:
            before = self.begin()
            result, feedback = capture.build(self, request)
            self.seal(before, feedback, result)
            self.resume()
            return result
        except (ValueError, OSError, KeyError, TypeError, RuntimeError):
            if self.directory.is_dir():
                self.discard_inputs()
            raise

    def state(self):
        if not self.marker.is_file():
            return {'phase': 'unrecorded'}
        doc = read_json(self.marker)
        validate_optional_format(doc, TURN_PREPARATION_FORMAT, 'preparation.json')
        return doc

    def discard_inputs(self):
        phase = self.state()['phase']
        if self.ex.directory(self.turn).exists():
            return False
        if phase == 'draft':
            sealed = self.ex.prepared_directory(self.turn)
            if sealed.is_dir():
                shutil.rmtree(sealed)
        if phase in ('inputs', 'draft'):
            shutil.rmtree(self.directory)
            return True
        return False

    def begin(self):
        from tempfile import TemporaryDirectory

        if self.ex.directory(self.turn).exists():
            raise ValueError('turn identity already used; recover it or choose another')
        if self.directory.exists() and not self.discard_inputs():
            raise ValueError(self.recovery_direction())
        parent = self.directory.parent
        parent.mkdir(parents=True, exist_ok=True)
        # An unsealed, unused staging directory does not reserve the public identity.
        with TemporaryDirectory(prefix='.new-', dir=parent) as tmp:
            staging = Path(tmp)
            write_json(staging / 'preparation.json', {'format': TURN_PREPARATION_FORMAT, 'phase': 'draft'})
            os.rename(staging, self.directory)
            sync_dir(parent)
        return self.live.start_draft(self.draft)

    def seal(self, before, feedback, packet):
        write_json(self.marker, dict(format=TURN_PREPARATION_FORMAT, phase='sealed', before=before,
                   after=self.draft.index_identity(), feedback=feedback_id(self.turn),
                   feedback_object=feedback['feedback_object'], request_sha256=packet['request_sha256'],
                   context_sha256=packet['context_sha256']))

    def resume(self):
        if self.ex.directory(self.turn).exists():
            self.ex.load(self.turn)
            self.cleanup_committed()
            return
        state = self.state()
        if state['phase'] != 'sealed':
            raise ValueError(self.recovery_direction())

        def install_attention():
            self.live.install_draft(self.draft, state['before'], state['after'],
                                    state['feedback'], state['feedback_object'])

        self.ex.commit_prepared(self.turn, state['request_sha256'], state['context_sha256'], install_attention)
        self.cleanup_committed()
        return None

    def cleanup_committed(self):
        if self.draft.directory.exists():
            shutil.rmtree(self.draft.directory)

    def recovery_direction(self):
        phase = self.state()['phase']
        if phase in ('inputs', 'draft'):
            return 'preparation stopped before commitment; retry context with this turn'
        if phase == 'sealed':
            return 'sealed preparation; use resume-context to publish its frozen selection'
        if phase == 'abandoned':
            return 'preparation was abandoned; inspect status for the reconciliation account'
        return 'incomplete preparation: inspect status, use abandon-context with an account, then prepare a fresh turn'

    def abandon(self, account):
        if not self.directory.is_dir() or self.ex.directory(self.turn).exists():
            raise ValueError('abandon requires an incomplete local turn without an exchange packet')
        before = self.state()
        if before['phase'] == 'abandoned':
            raise ValueError('preparation already abandoned')
        if before['phase'] == 'sealed' and self.live.feedback_object(before['feedback']) == before['feedback_object']:
            raise ValueError('Attention occurrence is installed; use resume-context to finish delivery')
        marker = dict(before, format=TURN_PREPARATION_FORMAT, phase='abandoned', previous_phase=before['phase'], account=account, at=now())
        write_json(self.marker, marker)
        return marker
