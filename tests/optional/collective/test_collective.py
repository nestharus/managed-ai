"""Concrete admission, routing, recovery and concurrency risks for the local adapter."""
from __future__ import annotations

import multiprocessing
import json
import os
import tempfile
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

SRC_ROOT = Path(__file__).resolve().parents[3] / 'src'
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))
from managed_ai.optional.collective.collective import Collective, Refused, CONTEXT_LIMIT
from importlib import import_module

notice_module = import_module(Collective.__module__ + '.notices')


def claim_worker(database, ready, release, results, command):
    store = Collective(database)
    ready.put(command)
    if not release.wait(20):
        raise RuntimeError('Claim study did not release participants')
    results.put(store.call({'actor': 'owner', 'command': command, 'op': 'claim',
                            'target': 'shared-target', 'revision': 1, 'seconds': 60}))


class CollectiveContract(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='collective-test-',
                                               dir=os.environ.get('COLLECTIVE_TEST_ROOT'))
        self.path = Path(self.temp.name) / 'store.sqlite'
        Collective.initialize(self.path)
        self.now = 1000.0
        self.store = Collective(self.path, clock=lambda: self.now)
        self.serial = 0

    def tearDown(self):
        self.temp.cleanup()

    def call(self, actor, op, **args):
        self.serial += 1
        return self.store.call({'actor': actor, 'command': f'c{self.serial}', 'op': op, **args})

    def accepted(self, result):
        self.assertEqual(result['status'], 'accepted', result)
        return result

    def create(self, ident, actor='owner', content=None, policy=None):
        return self.accepted(self.call(actor, 'create', id=ident,
                                      body=content or {'purpose': ident},
                                      reconsider_from=policy or []))['artifact']

    def prepare(self, target, actor='owner', sources=None, content=None, notices=None):
        suffix = str(self.serial + 1)
        context = self.accepted(self.call(actor, 'capture', id='context-' + suffix,
                                         target=target, sources=sources or [], notices=notices or []))['context']
        proposal = self.accepted(self.call(actor, 'propose', id='proposal-' + suffix,
                                          context=context['id'], body=content or {'result': target}))['proposal']
        return context, proposal

    def publish(self, proposal, actor='owner', **args):
        return self.call(actor, 'publish', proposal=proposal['id'],
                         account='The selected target is received for this declared use.', **args)

    def test_exact_command_recovers_committed_result_without_repeating_effect(self):
        request = {'actor': 'owner', 'command': 'possibly-lost-response', 'op': 'create',
                   'id': 'artifact', 'body': {'answer': 4}}
        first = self.store.call(request)
        recovered = Collective(self.path).call(request)
        self.assertEqual(first, recovered)
        self.assertEqual(self.store.show('artifact')['revision'], 1)
        self.assertEqual(len(self.store.view('changes')['items']), 1)
        self.assertEqual(self.store.receipt('owner', request['command']), first)
        with self.assertRaises(Refused) as raised:
            self.store.call({**request, 'body': {'answer': 5}})
        self.assertEqual(raised.exception.code, 'command-conflict')
        self.assertEqual(self.store.show('artifact')['body'], {'answer': 4})

    def test_changed_source_refuses_but_keeps_actual_proposal(self):
        self.create('source', content={'source': 'before'})
        self.create('use')
        _, old_proposal = self.prepare('use', sources=['source'], content={'return': 'actual old body'})
        _, change = self.prepare('source', content={'source': 'after'})
        self.accepted(self.publish(change))
        refused = self.publish(old_proposal)
        self.assertEqual(refused['code'], 'basis-changed')
        self.assertEqual(self.store.retained('proposal', old_proposal['id'])['body'], {'return': 'actual old body'})
        self.assertEqual(self.store.show('use')['revision'], 1)

    def test_same_bytes_new_affected_use_blocks_only_its_receiving_commitment(self):
        source = self.create('source', content={'unchanged': 'body'})
        self.create('affected', policy=['reader'])
        self.create('independent', actor='peer')
        old_context, old_proposal = self.prepare('affected', sources=['source'], content={'local': 'adequate'})
        _, independent = self.prepare('independent', actor='peer')
        impact = self.accepted(self.call('reader', 'offer', target='affected', basis=[source],
                                         account='A newly discovered use changes the joining explanation.',
                                         body={'relationship': 'source meaning affects this receiving promise'}))
        self.assertTrue(impact['required'])
        acknowledgment = self.accepted(self.call('owner', 'acknowledge', notice=impact['notice'],
                                                 account='The finding arrived; interpretation still remains.'))
        self.assertEqual(acknowledgment['state'], 'pending')
        self.assertIsNone(self.store.notice(impact['notice'])['receipt'])
        refused = self.publish(old_proposal)
        self.assertEqual(refused['code'], 'reconsideration-required')
        self.assertEqual(self.store.show('source')['sha256'], source['sha256'])
        self.assertEqual(self.store.show('source')['revision'], source['revision'])
        self.assertEqual(old_context['basis'], [source])
        self.accepted(self.publish(independent, actor='peer'))
        context, received = self.prepare('affected', sources=['source'], content={'local': 'adequate'})
        self.assertEqual([n['id'] for n in context['notices']], [impact['notice']])
        self.assertEqual(received['sha256'], old_proposal['sha256'])
        admitted = self.accepted(self.publish(received, treatments=[{
            'notice': impact['notice'], 'disposition': 'retained-adequate',
            'account': 'The component stays adequate; this accepted use explicitly supplies the missing host role.'
        }]))
        notice = self.store.notice(impact['notice'])
        self.assertEqual(notice['state'], 'received')
        self.assertIsNotNone(notice['acknowledgment'])
        self.assertEqual(notice['receipt']['artifact'], admitted['artifact'])
        self.assertEqual(self.store.show('affected', 1)['revision'], 1)
        self.assertEqual(self.store.view('inbox', actor='owner')['items'], [])

    def test_optional_offer_does_not_install_a_receiving_obligation(self):
        self.create('target', policy=['trusted-reader'])
        _, proposal = self.prepare('target')
        offer = self.accepted(self.call('other-reader', 'offer', target='target', basis=[],
                                       account='This is only a proposed connection.', body={'possible': True}))
        self.assertFalse(offer['required'])
        self.accepted(self.publish(proposal))
        self.accepted(self.call('owner', 'acknowledge', notice=offer['notice'], account='Received the optional suggestion.'))
        self.assertEqual(self.store.notice(offer['notice'])['state'], 'received')
        self.assertIsNone(self.store.notice(offer['notice'])['receipt'])
        _, receiving = self.prepare('target', notices=[offer['notice']])
        self.accepted(self.publish(receiving, treatments=[{'notice': offer['notice'], 'disposition': 'not-applicable',
                                                          'account': 'The optional finding concerns another use.'}]))

    def test_another_target_cannot_clear_the_required_receiving_duty(self):
        self.create('target-a', policy=['reader'])
        self.create('target-b')
        notice = self.accepted(self.call('reader', 'offer', target='target-a', basis=[],
                                        account='Finding belongs to A.', body={'change': 'A'}))['notice']
        _, proposal = self.prepare('target-b', notices=[notice])
        refused = self.publish(proposal, treatments=[{'notice': notice, 'disposition': 'incorporated',
                                                       'account': 'Cannot settle A by accepting B.'}])
        self.assertEqual(refused['code'], 'wrong-receiving-target')
        self.assertEqual(self.store.show('target-b')['revision'], 1)
        self.assertEqual(self.store.notice(notice)['state'], 'pending')
        self.assertIsNone(self.store.notice(notice)['receipt'])

    def test_late_claimant_is_fenced_after_replacement(self):
        self.create('target')
        _, proposal = self.prepare('target')
        old = self.accepted(self.call('owner', 'claim', target='target', revision=1, seconds=1))
        self.now += 2
        current = self.accepted(self.call('owner', 'claim', target='target', revision=1, seconds=30))
        self.assertGreater(current['generation'], old['generation'])
        self.assertEqual(self.publish(proposal, token=old['token'])['code'], 'claim-stale')
        accepted = self.accepted(self.publish(proposal, token=current['token']))
        self.assertEqual(accepted['artifact']['revision'], 2)

    def test_watch_snapshot_and_durable_change_are_recoverable(self):
        first = self.create('source', content={'v': 1})
        watch = self.accepted(self.call('reader', 'watch', subject='source', reason='This source participates in my use.'))
        self.assertEqual(watch['snapshot']['sha256'], first['sha256'])
        _, proposal = self.prepare('source', content={'v': 2})
        accepted = self.accepted(self.publish(proposal))
        inbox = Collective(self.path).view('inbox', actor='reader')
        self.assertTrue(inbox['complete'])
        self.assertEqual(inbox['items'][0]['body']['artifact'], accepted['artifact'])
        self.assertGreater(inbox['items'][0]['body']['event'], watch['cursor'])
        self.assertFalse(inbox['items'][0]['required'])

    def test_bounded_inbox_reports_omission_and_does_not_claim_completion(self):
        self.create('target', policy=['reader'])
        for i in range(3):
            self.accepted(self.call('reader', 'offer', target='target', basis=[],
                                     account=f'Finding {i}', body={'n': i}))
        first = self.store.view('inbox', actor='owner', limit=1)
        self.assertFalse(first['complete'])
        self.assertEqual(first['remaining_count'], 2)
        second = self.store.view('inbox', actor='owner', after=first['next_after'])
        self.assertTrue(second['complete'])
        self.assertEqual(len(second['items']), 2)
        self.assertEqual(len(self.store.view('inbox', actor='owner')['items']), 3)

    def test_malformed_inputs_are_refused_without_partial_state(self):
        result = self.call('owner', 'create', id='bad', body={}, reconsider_from=[{}])
        self.assertEqual(result['code'], 'invalid-input')
        with self.assertRaises(Refused):
            self.store.show('bad')
        self.create('target')
        result = self.call('owner', 'capture', id='bad-context', target='target', sources=[{}])
        self.assertEqual(result['code'], 'invalid-input')
        with self.assertRaises(ValueError):
            self.store.retained('context', 'bad-context')
        result = self.call('reader', 'offer', target='target', basis=[{'id': {}, 'revision': 1, 'sha256': 'x'}],
                           account='Malformed pin', body={})
        self.assertEqual(result['code'], 'invalid-input')
        self.assertEqual(self.store.view('inbox', actor='owner')['items'], [])

    def test_complete_notice_boundary_keeps_order_and_accepts_all_treatments(self):
        self.create('target', policy=['reader'])
        notices = [self.accepted(self.call('reader', 'offer', target='target', basis=[],
                                            account=f'Required finding {i}', body={'n': i}))['notice']
                   for i in range(128)]
        context, proposal = self.prepare('target', notices=[notices[-1], notices[0], notices[-1]])
        self.assertEqual([n['id'] for n in context['notices']], notices)
        result = self.accepted(self.publish(proposal, treatments=[
            {'notice': no, 'disposition': 'retained-adequate',
             'account': 'This complete current use treats the nominated finding.'} for no in notices]))
        self.assertEqual(result['received_notices'], notices)
        self.assertEqual(self.store.view('inbox', actor='owner')['items'], [])

    def test_overload_preserves_nominee_refusals_with_bounded_required_decoding(self):
        self.create('target', policy=['reader'])
        self.create('other', actor='peer')
        foreign = self.accepted(self.call('reader', 'offer', target='other', basis=[],
                                         account='An offer addressed elsewhere.', body={}))['notice']
        required_count = 256
        for i in range(required_count):
            self.accepted(self.call('reader', 'offer', target='target', basis=[],
                                    account=f'Required finding {i}', body={'n': i}))
        for nominees, code in ((['invalid-id'], 'invalid-input'), ([foreign], 'wrong-recipient'),
                               ([999999], 'wrong-recipient')):
            result = self.call('owner', 'capture', id=f'refused-{self.serial}',
                               target='target', notices=nominees)
            self.assertEqual(result['code'], code)
        request = {'actor': 'owner', 'command': 'bounded-overload', 'op': 'capture',
                   'id': 'oversized-context', 'target': 'target'}
        decoded = []
        original_notice = notice_module.decode_notice

        def observe_notice(row):
            if row['required']:
                decoded.append(row['id'])
            return original_notice(row)

        with patch.object(notice_module, 'decode_notice', side_effect=observe_notice):
            refused = self.store.call(request)
        self.assertEqual(refused['code'], 'context-bound')
        self.assertEqual(len(decoded), 129)
        self.assertEqual(self.store.call(request), refused)
        self.assertEqual(self.store.receipt('owner', request['command']), refused)
        with self.assertRaises(ValueError):
            self.store.retained('context', 'oversized-context')
        self.assertEqual(self.store.view('inbox', actor='owner', limit=100)['remaining_count'], required_count - 100)

    def test_oversized_required_set_stays_pending_while_another_target_publishes(self):
        self.create('affected', policy=['reader'])
        self.create('independent', actor='peer')
        _, affected = self.prepare('affected')
        _, independent = self.prepare('independent', actor='peer')
        for i in range(129):
            self.accepted(self.call('reader', 'offer', target='affected', basis=[],
                                    account=f'Required finding {i}', body={'n': i}))
        refused = self.publish(affected)
        self.assertEqual(refused['code'], 'reconsideration-required')
        self.assertEqual(self.store.retained('proposal', affected['id']), affected)
        self.assertEqual(self.store.show('affected')['revision'], 1)
        self.accepted(self.publish(independent, actor='peer'))
        self.assertEqual(self.store.show('independent')['revision'], 2)
        pending = self.store.view('inbox', actor='owner', limit=100)
        remainder = self.store.view('inbox', actor='owner', after=pending['next_after'], limit=100)
        self.assertEqual(len(pending['items']) + len(remainder['items']), 129)
        self.assertTrue(remainder['complete'])
        self.assertTrue(all(n['state'] == 'pending' and n['receipt'] is None
                            for n in pending['items'] + remainder['items']))

    def test_two_processes_cannot_both_claim_the_same_target(self):
        # Reopen with wall clock for worker processes.
        self.create('shared-target')
        context = multiprocessing.get_context('spawn')
        ready, results = context.Queue(), context.Queue()
        release = context.Event()
        processes = [context.Process(target=claim_worker,
                                     args=(str(self.path), ready, release, results, f'claim-{i}')) for i in range(2)]
        for process in processes:
            process.start()
        try:
            for _ in processes:
                ready.get(timeout=20)
            release.set()
            outcomes = [results.get(timeout=20) for _ in processes]
            self.assertEqual(sorted(x['status'] for x in outcomes), ['accepted', 'refused'])
            self.assertEqual([x['code'] for x in outcomes if x['status'] == 'refused'], ['already-claimed'])
        finally:
            release.set()
            for process in processes:
                process.join(20)
                if process.is_alive():
                    process.terminate()
                    process.join()
                self.assertEqual(process.exitcode, 0)

    def capture_source_bytes(self, request):
        """Observe complete source record payloads actually handed to the decoder."""
        received = []
        original = json.loads

        def observe(raw, *args, **kwargs):
            value = original(raw, *args, **kwargs)
            if isinstance(value, dict) and str(value.get('id', '')).startswith('source-') and 'owner' in value:
                received.append(len(raw.encode()))
            return value

        with patch.object(json, 'loads', side_effect=observe):
            result = self.store.call(request)
        return result, received

    def test_repeated_history_is_complete_without_repeated_acquisition(self):
        source = self.create('source-shared', content={'text': 'x' * (200 * 1024)})
        self.create('target', policy=['reader'])
        notices = [self.accepted(self.call('reader', 'offer', target='target', basis=[source],
                                          account=f'Distinct receiving finding {i}', body={'n': i}))['notice']
                   for i in range(128)]
        result, received = self.capture_source_bytes(dict(actor='owner', command='capture-repeated-history',
                                                          op='capture', id='history-context', target='target'))
        context = self.accepted(result)['context']
        self.assertEqual([n['id'] for n in context['notices']], notices)
        self.assertEqual(len(context['historical_notice_sources']), 1)
        self.assertEqual(context['historical_notice_sources'][0], self.store.show('source-shared', 1))
        self.assertEqual(context['sources'], [self.store.show('source-shared')])
        self.assertEqual(len(received), 2)
        self.assertLessEqual(sum(received), CONTEXT_LIMIT)

    def test_historical_overflow_bounds_acquisition_and_preserves_duties(self):
        self.create('target', policy=['reader'])
        notices = []
        for i in range(10):
            content = {'text': 'x' * (240 * 1024), 'version': i}
            if i == 0:
                source = self.create('source-history', content=content)
            else:
                _, proposal = self.prepare('source-history', content=content)
                source = self.accepted(self.publish(proposal))['artifact']
            notices.append(self.accepted(self.call('reader', 'offer', target='target', basis=[source],
                                                    account=f'Historical finding {i}', body={'n': i}))['notice'])
        request = dict(actor='owner', command='capture-large-history', op='capture', id='oversized-history', target='target')
        result, received = self.capture_source_bytes(request)
        self.assertEqual(result['code'], 'context-bound')
        self.assertLessEqual(sum(received), CONTEXT_LIMIT)
        self.assertLess(len(received), 10)
        self.assertEqual(self.store.call(request), result)
        with self.assertRaises(ValueError):
            self.store.retained('context', 'oversized-history')
        self.assertTrue(all(self.store.notice(no)['state'] == 'pending' and
                            self.store.notice(no)['receipt'] is None for no in notices))

    def test_multibyte_current_sources_use_bytes_before_decoding(self):
        self.create('target')
        sources = [f'source-{i}' for i in range(9)]
        for identity in sources:
            self.create(identity, content={'text': '🧭' * (63 * 1024)})
        result, received = self.capture_source_bytes(dict(actor='owner', command='capture-multibyte', op='capture',
                                                          id='multibyte-context', target='target', sources=sources))
        self.assertEqual(result['code'], 'context-bound')
        self.assertLessEqual(sum(received), CONTEXT_LIMIT)
        self.assertEqual(len(received), 8)


if __name__ == '__main__':
    unittest.main()
