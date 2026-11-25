"""Tests for guarded completed turn working-copy retirement (G2S-05 / MAINTENANCE.md)."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

RUNTIME = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(RUNTIME / 'native'))
sys.path.insert(0, str(RUNTIME / 'continuation'))

from continuation_support.storage import encode, exclusive, now, sha, write_json
from native_runtime.turn_context.preparation_journal import TurnPreparation
from native_runtime.turn_context.turn_retirement import (
    PLAN_FORMAT,
    RECEIPT_DIRECTORY,
    RECEIPT_FORMAT,
    execute_turn_retirement,
    plan_turn_retirement,
)
from system import System


class TurnRetirementTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.store_root = self.base / 'store'
        System.initialize(self.store_root, {'contribution': 'Test guarded turn working-copy retirement.'})
        self.system = System(self.store_root)
        self.reply = self.base / 'reply.md'
        self.reply.write_text('Completed turn reply.\n')
        self.output = self.base / 'output.md'
        self.output.write_text('Published artifact body.\n')

    def _complete_turn(self, turn: str, *, targets: list[str] = ('outputs/result.md',),
                       extra_request: dict | None = None, adopt_targets: list[str] | None = None,
                       propose_targets: list[str] | None = None):
        req = {'task': f'Run turn {turn}.', 'targets': list(targets)}
        if extra_request:
            req.update(extra_request)
        self.system.context(turn, req)
        propose = list(targets) if propose_targets is None else list(propose_targets)
        proposals = [f'{t}={self.output}' for t in propose]
        self.system.receive(turn, self.reply, 'tester', proposals)
        adopt_list = list(targets) if adopt_targets is None else list(adopt_targets)
        for target in adopt_list:
            (self.system.work / target).parent.mkdir(parents=True, exist_ok=True)
            self.system.adopt(turn, target, f'Adopt {target} for {turn}.')

    def test_eligible_turn_retires_cleanly_and_preserves_view_recover_explain_and_packets(self):
        with exclusive(self.system.ex.root / 'owner.lock'):
            self._complete_turn('done-1', targets=['outputs/one.md'])
            self._complete_turn('done-2', targets=['outputs/two.md'],
                                extra_request={'returns': ['done-1'], 'artifacts': ['done-1:outputs/one.md']})

            # Snapshot exchange/packets and exchange/objects before retirement.
            pkt1_before = {p.name: p.read_bytes() for p in (self.system.ex.directory('done-1')).iterdir() if p.is_file()}
            objs_before = {p.name: p.read_bytes() for p in (self.system.ex.root / 'objects').iterdir() if p.is_file()}
            view_before = self.system.view('done-1')
            recover_before = self.system.recover('done-1')
            explain_before = self.system.explain_context('done-1')

            plan = plan_turn_retirement(self.system, all_eligible=True)
            self.assertEqual(plan['format'], PLAN_FORMAT)
            self.assertEqual(plan['instance_id'], self.system.instance_id)
            self.assertEqual([r['turn'] for r in plan['eligible']], ['done-1', 'done-2'])
            # 'start' has no return.json so it MUST be refused in ineligible.
            inelig_map = {r['turn']: r['reasons'] for r in plan['ineligible']}
            self.assertIn('start', inelig_map)
            self.assertIn('missing_return', inelig_map['start'])
            self.assertGreater(plan['reclaimable_bytes'], 0)
            self.assertGreater(plan['reclaimable_files'], 0)

            # Plan hash is deterministic on unchanged store.
            plan_again = plan_turn_retirement(self.system, all_eligible=True)
            self.assertEqual(plan['plan_sha256'], plan_again['plan_sha256'])

            # Execute retirement for both eligible turns.
            receipt = execute_turn_retirement(
                self.system,
                plan=plan,
                expected_plan=plan['plan_sha256'],
                reason='Retire verified completed working copies.',
            )
            self.assertEqual(receipt['format'], RECEIPT_FORMAT)
            self.assertTrue(receipt['complete'])
            self.assertEqual(receipt['retired_turns'], ['done-1', 'done-2'])
            self.assertEqual(receipt['files_removed'], plan['reclaimable_files'])
            self.assertEqual(receipt['bytes_reclaimed'], plan['reclaimable_bytes'])
            self.assertTrue(all(receipt['post_verification'].values()))

            # Working dirs removed; 'start' untouched.
            self.assertFalse((self.system.work / '.system/turns/done-1').exists())
            self.assertFalse((self.system.work / '.system/turns/done-2').exists())
            self.assertTrue((self.system.work / '.system/turns/start').is_dir())

            # Receipt persisted durably under work/.system/retirements/<plan_sha256>.json.
            receipt_file = self.system.work / RECEIPT_DIRECTORY / f"{plan['plan_sha256']}.json"
            self.assertTrue(receipt_file.is_file())
            self.assertEqual(json.loads(receipt_file.read_bytes()), receipt)

            # exchange/packets/ and exchange/objects/ are 100% untouched.
            pkt1_after = {p.name: p.read_bytes() for p in (self.system.ex.directory('done-1')).iterdir() if p.is_file()}
            objs_after = {p.name: p.read_bytes() for p in (self.system.ex.root / 'objects').iterdir() if p.is_file()}
            self.assertEqual(pkt1_after, pkt1_before)
            self.assertEqual(objs_after, objs_before)

            # view / recover / explain_context remain byte-for-byte identical for BOTH done-1 and done-2.
            self.assertEqual(self.system.view('done-1'), view_before)
            self.assertEqual(self.system.recover('done-1'), recover_before)
            self.assertEqual(self.system.explain_context('done-1'), explain_before)
            self.assertEqual(self.system.explain_context('done-2')['packet'], 'done-2')

            # Re-executing the same plan is refused.
            with self.assertRaisesRegex(ValueError, 'already executed'):
                execute_turn_retirement(
                    self.system,
                    plan=plan,
                    expected_plan=plan['plan_sha256'],
                    reason='Duplicate execution must refuse.',
                )

            # Re-planning after retirement leaves only 'start' (ineligible) and 0 eligible turns.
            after_plan = plan_turn_retirement(self.system, all_eligible=True)
            self.assertEqual(after_plan['eligible'], [])
            self.assertEqual([r['turn'] for r in after_plan['ineligible']], ['start'])

    def test_guard_a_and_b_refuse_missing_return_unreturned_unpublished_and_incomplete_turns(self):
        with exclusive(self.system.ex.root / 'owner.lock'):
            # 1. Missing return (open prepared turn).
            self.system.context('open-turn', {'task': 'Open turn.', 'targets': ['outputs/open.md']})

            # 2. Returned proposal, never published (like coordination-alignment-construction).
            self._complete_turn('unpub-turn', targets=['outputs/unpub.md'], adopt_targets=[])

            # 3. Multi-target with only 1 of 2 returned & published (like coordination-operator-receiving).
            self._complete_turn(
                'partial-turn',
                targets=['outputs/p1.md', 'outputs/p2.md'],
                propose_targets=['outputs/p1.md'],
                adopt_targets=['outputs/p1.md'],
            )

            # 4. Terminal without return (failed launch + closed transport).
            self.system.context('closed-turn', {'task': 'Closed.', 'targets': ['outputs/closed.md']})
            self.system.run('closed-turn', [str(self.base / 'absent-command'), '{context}'])
            self.assertFalse(self.system.receive('closed-turn', None, 'tester', [], wait=8)['received'])
            self.system.close_transport('closed-turn', 'No worker reply arrived.')

            # 5. Incomplete publication intent (not complete).
            self._complete_turn('intent-turn', targets=['outputs/intent.md'], adopt_targets=[])
            pkt_intent = self.system.ex.directory('intent-turn')
            pub_name = 'publish-' + sha(b'outputs/intent.md') + '.json'
            ret_doc = json.loads((pkt_intent / 'return.json').read_bytes())
            prop = ret_doc['proposals'][0]
            write_json(pkt_intent / pub_name, {
                'packet': 'intent-turn',
                'path': 'outputs/intent.md',
                'phase': 'intent',
                'reason': 'Interrupted publish.',
                'sha256': prop['sha256'],
                'base_sha256': prop['base_sha256'],
                'return_sha256': ret_doc['return_sha256'],
                'started_at': now(),
            })

            # 6. Reconciled publication intent (historical_completion=unestablished -> still refused!).
            self._complete_turn('reconciled-turn', targets=['outputs/reconciled.md'], adopt_targets=[])
            pkt_rec = self.system.ex.directory('reconciled-turn')
            rec_pub_name = 'publish-' + sha(b'outputs/reconciled.md') + '.json'
            rec_ret = json.loads((pkt_rec / 'return.json').read_bytes())
            rec_prop = rec_ret['proposals'][0]
            write_json(pkt_rec / rec_pub_name, {
                'packet': 'reconciled-turn',
                'path': 'outputs/reconciled.md',
                'phase': 'intent',
                'reason': 'Interrupted publish.',
                'sha256': rec_prop['sha256'],
                'base_sha256': rec_prop['base_sha256'],
                'return_sha256': rec_ret['return_sha256'],
                'started_at': now(),
            })
            self.system.reconcile_publication('reconciled-turn', 'outputs/reconciled.md', None,
                                              'Observed absent after interrupted intent.')

            # 7. Abandoned preparation without packet.
            prep = TurnPreparation(self.system, 'abandoned-turn')
            prep.begin()
            prep.abandon('Abandoned before packet commit.')

            plan = plan_turn_retirement(self.system, all_eligible=True)
            self.assertEqual(plan['eligible'], [])
            by_turn = {r['turn']: r['reasons'] for r in plan['ineligible']}
            self.assertIn('missing_return', by_turn['start'])
            self.assertIn('missing_return', by_turn['open-turn'])
            self.assertIn('unpublished_targets', by_turn['unpub-turn'])
            self.assertIn('unreturned_targets', by_turn['partial-turn'])
            self.assertIn('unpublished_targets', by_turn['partial-turn'])
            self.assertIn('terminal_without_return', by_turn['closed-turn'])
            self.assertIn('incomplete_or_invalid_publications', by_turn['intent-turn'])
            self.assertIn('unpublished_targets', by_turn['intent-turn'])
            self.assertIn('reconciled_publication_unestablished', by_turn['reconciled-turn'])
            self.assertIn('packet_missing', by_turn['abandoned-turn'])
            self.assertIn('incomplete_preparation_phase', by_turn['abandoned-turn'])

            # Empty eligible plan cannot be executed.
            with self.assertRaisesRegex(ValueError, 'no eligible turns'):
                execute_turn_retirement(self.system, plan=plan, expected_plan=plan['plan_sha256'], reason='Empty.')

    def test_guard_c_refuses_live_structural_refs_but_allows_narrative_mentions(self):
        with exclusive(self.system.ex.root / 'owner.lock'):
            self._complete_turn('rec-ref', targets=['outputs/rec-ref.md'])
            self._complete_turn('pkt-ref', targets=['outputs/pkt-ref.md'])
            self._complete_turn('att-ref', targets=['outputs/att-ref.md'])
            self._complete_turn('log-mention', targets=['outputs/log-mention.md'])
            self._complete_turn('esc-ref', targets=['outputs/esc-ref.md'])
            self._complete_turn('interp-ref', targets=['outputs/interp-ref.md'])
            self._complete_turn('uncommit-ref', targets=['outputs/uncommit-ref.md'])
            self._complete_turn('origin-ref', targets=['outputs/origin-ref.md'])
            self._complete_turn('sealed-ref', targets=['outputs/sealed-ref.md'])
            self._complete_turn('att-prod-ref', targets=['outputs/att-prod-ref.md'])

            # 1. Live record basis references .system/turns/rec-ref/brief.md -> MUST REFUSE.
            self.system.put({
                'id': 'r-basis',
                'kind': 'note',
                'content': {'text': 'Depends directly on working brief.'},
                'basis': ['.system/turns/rec-ref/brief.md'],
            }, 0)

            # 2. Cross-turn packet request references .system/turns/pkt-ref/brief.md -> MUST REFUSE.
            self.system.context('consumer-turn', {
                'task': 'Read another turn working brief.',
                'sources': ['.system/turns/pkt-ref/brief.md'],
            })

            # 3. Active Attention item basis references .system/turns/att-ref/brief.md -> MUST REFUSE.
            att_spec = self.base / 'att.json'
            att_spec.write_text(json.dumps({
                'items': [{
                    'id': 'watch-att-ref',
                    'question': 'Does working copy remain?',
                    'use': 'Guard active attention dependency.',
                    'basis': ['.system/turns/att-ref/brief.md'],
                }],
            }))
            self.system.track(att_spec)

            # 4. Informational narrative mention in record content (like Tier B historical log) -> MUST ALLOW!
            self.system.put({
                'id': 'r-narrative',
                'kind': 'note',
                'content': {'log': 'Historical audit inspected work/.system/turns/log-mention/brief.md previously.'},
            }, 0)

            # 5. Real system.interpret() snapshot-dict evidence referencing a working copy -> MUST REFUSE! (C9-1)
            acct = self.base / 'interp-account.md'
            acct.write_text('Whole-goal interpretation relying directly on turn brief.\n')
            self.system.interpret(1, 'continue', acct, ['.system/turns/interp-ref/brief.md'])

            # 6. Active uncommitted turn staging request referencing a candidate working copy -> MUST REFUSE!
            staging_dir = self.system.work / '.system/turns/staging-consumer'
            staging_dir.mkdir(parents=True, exist_ok=True)
            write_json(staging_dir / 'request.json', {
                'task': 'Consume staged input.',
                'sources': ['.system/turns/uncommit-ref/brief.md'],
            })

            # 7. Live record relations[].origin and content.origin referencing working copy -> MUST REFUSE! (C9-3a)
            self.system.put({
                'id': 'r-origin',
                'kind': 'note',
                'content': {'text': 'Origin note.', 'origin': '.system/turns/origin-ref/brief.md'},
                'relations': [{'relation': 'derives-from', 'target': 'goal',
                               'origin': '.system/turns/origin-ref/brief.md'}],
            }, 0)

            # 8. Staged exchange/packets/.sealed-<other>/request.json referencing working copy -> MUST REFUSE! (C9-3b)
            self.system.ex.prepare(
                'staged-consumer',
                'tester',
                '.system/turns/sealed-ref/brief.md',
                [],
                [],
                [],
                [],
                staged=True,
            )

            # 9. Active Attention item latest assessment products referencing working copy -> MUST REFUSE! (C9-3c)
            att_prod_spec = self.base / 'att-prod.json'
            att_prod_spec.write_text(json.dumps({
                'items': [{
                    'id': 'watch-prod',
                    'question': 'Track product dependency.',
                    'use': 'Guard latest assessment product path.',
                    'basis': [],
                }],
            }))
            self.system.track(att_prod_spec)
            self._complete_turn('prod-assess-turn', targets=['outputs/prod-assess.md'])
            assess_path = self.base / 'assess-prod.json'
            assess_path.write_text(json.dumps({
                'id': 'rec-prod-1',
                'assessments': [{
                    'item': 'watch-prod',
                    'disposition': 'considered',
                    'account': 'Observed product from working copy.',
                    'products': ['.system/turns/att-prod-ref/brief.md'],
                }],
            }))
            self.system.assess('prod-assess-turn', assess_path, 'tester')

            # 10. JSON unicode-escape basis path (\u002esystem/turns/...) -> MUST REFUSE!
            esc_record = self.system.work / 'records' / 'r-escaped.json'
            esc_record.write_bytes(
                b'{"id":"r-escaped","kind":"note","content":{"text":"escaped"},'
                b'"basis":[{"path":"\\u002esystem/turns/esc-ref/brief.md","sha256":"'
                + (b'a' * 64) + b'"}]}\n'
            )

            plan = plan_turn_retirement(
                self.system,
                turns=[
                    'rec-ref', 'pkt-ref', 'att-ref', 'log-mention', 'esc-ref',
                    'interp-ref', 'uncommit-ref', 'origin-ref', 'sealed-ref', 'att-prod-ref',
                ],
            )
            self.assertEqual([r['turn'] for r in plan['eligible']], ['log-mention'])
            inelig = {r['turn']: r['reasons'] for r in plan['ineligible']}
            self.assertIn('referenced_by_records', inelig['rec-ref'])
            self.assertIn('referenced_by_records', inelig['esc-ref'])
            self.assertIn('referenced_by_records', inelig['origin-ref'])
            self.assertIn('referenced_by_other_packets', inelig['pkt-ref'])
            self.assertIn('referenced_by_other_packets', inelig['sealed-ref'])
            self.assertIn('referenced_by_attention', inelig['att-ref'])
            self.assertIn('referenced_by_attention', inelig['att-prod-ref'])
            self.assertIn('referenced_by_interpretations', inelig['interp-ref'])
            self.assertIn('referenced_by_uncommitted_turns', inelig['uncommit-ref'])

    def test_guard_d_refuses_when_captured_object_is_missing_or_corrupted(self):
        with exclusive(self.system.ex.root / 'owner.lock'):
            self._complete_turn('corrupt-obj', targets=['outputs/corrupt.md'])
            # Corrupt the captured full_view_object referenced by exposure.json.
            _, req = self.system.ex.load('corrupt-obj')
            exp_row = next(r for r in req['materials'] if r['path'].endswith('/exposure.json'))
            exp_doc = json.loads(self.system.ex.objects.read(exp_row['sha256']))
            full_view_obj = self.system.ex.root / 'objects' / exp_doc['full_view_object']
            full_view_obj.write_bytes(b'corrupted view bytes')

            plan = plan_turn_retirement(self.system, turns=['corrupt-obj'])
            self.assertEqual(plan['eligible'], [])
            self.assertEqual(plan['ineligible'][0]['turn'], 'corrupt-obj')
            self.assertIn('captured_recovery_failed', plan['ineligible'][0]['reasons'])

    def test_refuses_uncaptured_extra_files_and_post_capture_modifications(self):
        with exclusive(self.system.ex.root / 'owner.lock'):
            # 1. Extra uncaptured file placed inside work/.system/turns/<turn>/ -> MUST REFUSE! (C9-2)
            self._complete_turn('extra-file-turn', targets=['outputs/extra.md'])
            extra_notes = self.system.work / '.system/turns/extra-file-turn/caller-private-notes.md'
            extra_notes.write_text('Unretained caller notes.\n')

            # 2. Captured working file modified on disk after capture -> MUST REFUSE! (C9-2)
            self._complete_turn('edited-brief-turn', targets=['outputs/edited.md'])
            brief_file = self.system.work / '.system/turns/edited-brief-turn/brief.md'
            brief_file.write_text('# Mutated after capture\n')

            # 3. Bookkeeping request.json tampered after capture -> MUST REFUSE! (C9-2)
            self._complete_turn('edited-req-turn', targets=['outputs/req.md'])
            req_file = self.system.work / '.system/turns/edited-req-turn/request.json'
            req_doc = json.loads(req_file.read_bytes())
            req_doc['task'] = 'Tampered task text.'
            write_json(req_file, req_doc)

            # 4. Turn with legitimate attention spec (attention-spec.json) -> MUST ALLOW!
            self._complete_turn(
                'with-att-spec',
                targets=['outputs/att-spec.md'],
                extra_request={
                    'attention': {
                        'items': [{
                            'id': 'inline-item',
                            'question': 'Check inline attention spec.',
                            'use': 'Verify attention-spec.json is accepted when captured.',
                            'basis': [],
                        }],
                    },
                },
            )

            plan = plan_turn_retirement(
                self.system,
                turns=['extra-file-turn', 'edited-brief-turn', 'edited-req-turn', 'with-att-spec'],
            )
            self.assertEqual([r['turn'] for r in plan['eligible']], ['with-att-spec'])
            inelig = {r['turn']: r['reasons'] for r in plan['ineligible']}
            self.assertIn('uncaptured_working_files', inelig['extra-file-turn'])
            self.assertIn('working_dir_modified_after_capture', inelig['edited-brief-turn'])
            self.assertIn('working_dir_modified_after_capture', inelig['edited-req-turn'])
            self.assertTrue(extra_notes.is_file())

            # Retire only the verified 'with-att-spec' turn cleanly.
            receipt = execute_turn_retirement(
                self.system,
                plan=plan,
                expected_plan=plan['plan_sha256'],
                reason='Retire verified turn with captured attention-spec.json.',
            )
            self.assertTrue(receipt['complete'])
            self.assertEqual(receipt['retired_turns'], ['with-att-spec'])
            self.assertTrue(extra_notes.is_file())

    def test_rejects_unknown_escaping_and_symlink_turn_names(self):
        with exclusive(self.system.ex.root / 'owner.lock'):
            for bad in ('../records', 'a/b', '.hidden', '', 'no-such-turn'):
                with self.subTest(bad=bad):
                    with self.assertRaises(ValueError):
                        plan_turn_retirement(self.system, turns=[bad])

            # Symlink turn directory under work/.system/turns/ must be rejected explicitly
            # and marked ineligible under all_eligible=True without ever being deleted!
            victim = self.base / 'victim-dir'
            victim.mkdir()
            (victim / 'keep.txt').write_text('do not delete')
            link_turn = self.system.work / '.system/turns/symlink-turn'
            link_turn.symlink_to(victim)

            with self.assertRaisesRegex(ValueError, 'symlinks refused'):
                plan_turn_retirement(self.system, turns=['symlink-turn'])

            all_plan = plan_turn_retirement(self.system, all_eligible=True)
            inelig = {r['turn']: r['reasons'] for r in all_plan['ineligible']}
            self.assertEqual(inelig['symlink-turn'], ['symlink_or_non_directory'])
            self.assertTrue((victim / 'keep.txt').is_file())

            # Symlink nested INSIDE an otherwise completed turn dir must be refused!
            self._complete_turn('nested-link', targets=['outputs/nested.md'])
            (self.system.work / '.system/turns/nested-link/evil-link').symlink_to(victim / 'keep.txt')
            nested_plan = plan_turn_retirement(self.system, turns=['nested-link'])
            self.assertEqual(nested_plan['eligible'], [])
            self.assertIn('working_dir_unsafe_entries', nested_plan['ineligible'][0]['reasons'])
            self.assertTrue((victim / 'keep.txt').is_file())

    def test_execute_rejects_plan_hash_mismatch_and_toctou_drift(self):
        with exclusive(self.system.ex.root / 'owner.lock'):
            self._complete_turn('drift-turn', targets=['outputs/drift.md'])
            plan = plan_turn_retirement(self.system, turns=['drift-turn'])
            self.assertEqual([r['turn'] for r in plan['eligible']], ['drift-turn'])

            # 1. Wrong expected_plan hash.
            with self.assertRaisesRegex(ValueError, 'digest mismatch'):
                execute_turn_retirement(self.system, plan=plan, expected_plan='0' * 64, reason='Wrong digest.')

            # 2. Tampered plan body.
            tampered = copy.deepcopy(plan)
            tampered['reclaimable_bytes'] += 1
            with self.assertRaisesRegex(ValueError, 'digest mismatch'):
                execute_turn_retirement(self.system, plan=tampered, expected_plan=plan['plan_sha256'], reason='Tampered.')

            # 3. Empty reason rejected.
            with self.assertRaisesRegex(ValueError, 'nonempty text'):
                execute_turn_retirement(self.system, plan=plan, expected_plan=plan['plan_sha256'], reason='   ')

            # 4a. TOCTOU drift: a new uncaptured file appears after planning -> refused before removal.
            extra = self.system.work / '.system/turns/drift-turn/late-note.txt'
            extra.write_text('Added after plan.')
            with self.assertRaisesRegex(ValueError, 'uncaptured_working_files'):
                execute_turn_retirement(self.system, plan=plan, expected_plan=plan['plan_sha256'], reason='Stale plan.')
            self.assertTrue(extra.is_file())
            extra.unlink()

            # 4b. TOCTOU drift: working-dir inventory changes (a captured file is removed after planning) -> refused.
            method_copy = self.system.work / '.system/turns/drift-turn/method.md'
            method_bytes = method_copy.read_bytes()
            method_copy.unlink()
            with self.assertRaisesRegex(ValueError, 'changed since planning'):
                execute_turn_retirement(self.system, plan=plan, expected_plan=plan['plan_sha256'], reason='Changed dir.')
            method_copy.write_bytes(method_bytes)

            # 5. TOCTOU guard failure: a live record adds a basis reference after planning.
            self.system.put({
                'id': 'late-ref',
                'kind': 'note',
                'content': {'text': 'Late basis dependency.'},
                'basis': ['.system/turns/drift-turn/brief.md'],
            }, 0)
            with self.assertRaisesRegex(ValueError, 'became ineligible'):
                execute_turn_retirement(self.system, plan=plan, expected_plan=plan['plan_sha256'], reason='Now referenced.')
            self.assertTrue((self.system.work / '.system/turns/drift-turn').is_dir())

    def test_zero_target_turn_retires_and_interrupted_receipt_is_refused(self):
        with exclusive(self.system.ex.root / 'owner.lock'):
            self._complete_turn('report-only', targets=[])
            plan = plan_turn_retirement(self.system, turns=['report-only'])
            self.assertEqual([r['turn'] for r in plan['eligible']], ['report-only'])
            self.assertEqual(plan['eligible'][0]['declared_targets'], [])
            self.assertEqual(plan['eligible'][0]['published_targets'], [])

            # Simulate an interrupted receipt on disk -> execute must refuse cleanly.
            receipt_dir = self.system.work / RECEIPT_DIRECTORY
            receipt_dir.mkdir(parents=True, exist_ok=True)
            receipt_path = receipt_dir / f"{plan['plan_sha256']}.json"
            write_json(receipt_path, {
                'format': RECEIPT_FORMAT,
                'instance_id': self.system.instance_id,
                'plan_sha256': plan['plan_sha256'],
                'complete': False,
            })
            with self.assertRaisesRegex(ValueError, 'interrupted turn retirement'):
                execute_turn_retirement(
                    self.system,
                    plan=plan,
                    expected_plan=plan['plan_sha256'],
                    reason='Retry after interrupted receipt.',
                )
            self.assertTrue((self.system.work / '.system/turns/report-only').is_dir())
            receipt_path.unlink()

            # Once cleared, zero-target completed turn retires cleanly.
            receipt = execute_turn_retirement(
                self.system,
                plan=plan,
                expected_plan=plan['plan_sha256'],
                reason='Retire zero-target completed turn.',
            )
            self.assertTrue(receipt['complete'])
            self.assertEqual(receipt['retired_turns'], ['report-only'])
            self.assertFalse((self.system.work / '.system/turns/report-only').exists())
            self.assertEqual(self.system.recover('report-only')['packet'], 'report-only')


if __name__ == '__main__':
    unittest.main(verbosity=2)
