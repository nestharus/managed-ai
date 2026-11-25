#!/usr/bin/env python3
"""Focused synthetic checks for captured-context explanation and recovery hints."""
from __future__ import annotations

if __package__:
    from .native_test_paths import APP, ENGINE, PRODUCT
else:
    from native_test_paths import APP, ENGINE, PRODUCT
import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from native_runtime.composition import Composition
from controller import Deployment
from native_runtime.capture import TurnCapture
from system import System




def digest(body):
    return hashlib.sha256(body).hexdigest()


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + '\n')
    return path


def inventory(root):
    return {p.relative_to(root).as_posix(): (p.stat().st_mode, digest(p.read_bytes()))
            for p in root.rglob('*') if p.is_file()}


class ContextExplanationChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='context-explanation-')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.domain = self.make_domain('fixture-entry', 'Complete fixture entry.\n')
        self.root = self.base / 'store'
        System.initialize(self.root, {'contribution': 'Inspect a concrete captured selection.'},
                          domains=[self.domain])
        self.system = System(self.root)

    def make_domain(self, ident, body, *, sealed=False):
        root = self.base / ident
        root.mkdir()
        source = root / 'source.md'
        source.write_text(body)
        catalogue = write_json(root / 'catalogue.json', {
            'format': 'managed-ai-source-catalogue/1', 'sources': [{
                'id': ident, 'path': 'source.md', 'title': ident, 'kind': 'guide',
                'sha256': digest(source.read_bytes()), 'bytes': source.stat().st_size,
                'exposure': 'sealed' if sealed else 'available'}]})
        return write_json(root / 'domain.json', {
            'format': 'managed-ai-domain/1', 'id': ident, 'version': '1',
            'engine_api': 'managed-ai-runtime/1', 'source_root': '.',
            'catalogue': {'path': 'catalogue.json', 'sha256': digest(catalogue.read_bytes())},
            'schemas': {}, 'entry_sources': [] if sealed else [ident]})

    def file(self, path, body):
        result = self.system.work / path
        result.parent.mkdir(parents=True, exist_ok=True)
        result.write_text(body)
        return result

    def populated(self):
        self.file('sources/shared.md', 'Selected shared basis.\n')
        self.file('sources/closed.md', 'Closed historical evidence.\n')
        self.system.put({'id': 'open-duty', 'kind': 'obligation', 'content': {
            'text': 'Retain the current obligation.', 'disposition': 'open'},
            'basis': ['sources/shared.md']}, 0)
        self.system.put({'id': 'closed-duty', 'kind': 'obligation', 'content': {
            'text': 'Earlier completed work.', 'disposition': 'fulfilled', 'account': 'Fixture closure.'},
            'basis': ['sources/closed.md']}, 0)
        self.system.put({'id': 'note', 'kind': 'note', 'content': 'Useful current material.',
                         'basis': ['sources/shared.md']}, 0)
        self.system.put({'id': 'outside', 'kind': 'note', 'content': 'Outside this bounded view.'}, 0)
        self.system.context('prior', {'targets': ['outputs/proposal.md']})
        reply = self.base / 'reply.md'
        reply.write_text('One retained body under two explicit roles.\n')
        self.system.receive('prior', reply, 'synthetic participant', ['outputs/proposal.md=' + str(reply)])
        return {'records': ['note', 'outside'], 'record_limit': 1,
                'sources': ['sources/shared.md'], 'techniques': ['fixture-entry'],
                'targets': ['outputs/absent.md'], 'returns': ['prior'],
                'artifacts': ['prior:outputs/proposal.md']}

    @staticmethod
    def unit(report, path, role='materials'):
        return next(r for r in report['units'] if r['path'] == path and r['role'] == role)

    def test_new_capture_reasons_and_exact_delivery_counts(self):
        request = self.populated()
        result = self.system.context('selected', request)
        before = inventory(self.root)
        report = self.system.explain_context('selected')
        self.assertEqual(inventory(self.root), before)
        raw = json.loads(Path(result['request']).read_text())
        entries = [raw['brief']] + [r for k in ('methods', 'materials', 'targets', 'returns', 'artifacts') for r in raw[k]]
        self.assertEqual(report['totals']['selected_body_bytes'], sum(r['bytes'] for r in entries))
        self.assertEqual(report['totals']['rendered_context_bytes'], len(Path(result['context']).read_bytes()))
        unique = {r['sha256']: r['bytes'] for r in entries if r['sha256'] is not None}
        self.assertEqual(report['totals']['distinct_selected_object_bytes'], sum(unique.values()))
        self.assertGreaterEqual(report['totals']['repeated_object_entries'], 1)
        shared = self.unit(report, 'sources/shared.md')
        self.assertEqual({r['kind'] for r in shared['inclusion_reasons']}, {'selected-record-basis', 'explicit-source'})
        self.assertEqual({r['record'] for r in shared['inclusion_reasons'] if 'record' in r}, {'open-duty', 'note'})
        self.assertTrue(all(r['same_bytes'] for r in shared['captured_correspondences']))
        technique = next(r for r in report['units'] if any(c.get('source') == 'fixture-entry'
                         for c in r['captured_correspondences']))
        self.assertEqual({r['kind'] for r in technique['inclusion_reasons']},
                         {'domain-entry-technique', 'explicit-technique'})
        self.assertIn('closed-duty', report['captured_selection']['governing_records'])
        self.assertNotIn('sources/closed.md', [r['path'] for r in report['units']])
        self.assertEqual(report['captured_selection']['omitted_records'], ['outside'])
        absent = self.unit(report, 'outputs/absent.md', 'targets')
        self.assertIsNone(absent['sha256']); self.assertEqual(absent['bytes'], 0)
        artifact = self.unit(report, 'prior-proposal:prior:outputs/proposal.md', 'artifacts')
        self.assertEqual(artifact['prior_occurrence']['packet'], 'prior')
        self.assertTrue(all(r['origin_status'] == 'recorded' for r in report['units']))
        exposure = next(r for r in raw['materials'] if r['path'].endswith('/exposure.json'))
        self.assertNotIn('inclusion_origins', json.loads(self.system.ex.get(exposure['sha256'])))
        self.assertIn('inclusion_origins', self.system.view('selected'))

    def test_historical_explanation_ignores_current_heads_sources_and_loose_request(self):
        self.system.context('selected', self.populated())
        original = self.system.explain_context('selected')
        self.file('sources/shared.md', 'Different current source.\n')
        self.system.put({'id': 'note', 'kind': 'note', 'content': 'A later head.'}, 1)
        self.system.put({'id': 'new-duty', 'kind': 'obligation', 'content': {'text': 'Later current duty.'}}, 0)
        self.file('.system/turns/selected/request.json', '{"invented": "not immutable input authority"}')
        self.file('.system/turns/selected/selection.json', '{"invented": "not immutable input authority"}')
        before = inventory(self.root)
        with patch.object(self.system.record_store, 'show', side_effect=AssertionError('current head read')), \
             patch.object(self.system.ex, 'path', side_effect=AssertionError('current workspace path read')):
            self.assertEqual(self.system.explain_context('selected'), original)
        self.assertEqual(inventory(self.root), before)

    def test_old_capture_reports_unknown_origin_and_changed_basis_correspondence(self):
        request = self.populated()
        self.file('sources/shared.md', 'Current file differs from the record basis.\n')
        working = TurnCapture._working_view
        def legacy(capture, directory, turn, techniques, view):
            view.pop('inclusion_origins')
            return working(capture, directory, turn, techniques, view)
        with patch.object(TurnCapture, '_working_view', legacy):
            self.system.context('legacy', request)
        before = inventory(self.root)
        report = self.system.explain_context('legacy')
        shared = self.unit(report, 'sources/shared.md')
        self.assertEqual(shared['origin_status'], 'not-recorded')
        self.assertEqual(shared['inclusion_reasons'], [])
        self.assertTrue(shared['captured_correspondences'])
        self.assertTrue(all(not r['same_bytes'] for r in shared['captured_correspondences']))
        self.assertEqual(inventory(self.root), before)

    def test_composition_injection_and_explicit_limits_are_not_relabelled(self):
        self.system.put({'id': 'composition', 'kind': 'note', 'content': {
            'format': 'g60-composition/1', 'purpose': 'A bounded declared relation.'}}, 0)
        self.system.context('composed', {'compositions': [{'record': 'composition'}]})
        unit = self.unit(self.system.explain_context('composed'), 'records/composition.json')
        self.assertEqual([r['kind'] for r in unit['inclusion_reasons']], ['composition-record'])
        self.file('sources/closed.md', 'Historical duty evidence.\n')
        self.system.put({'id': 'closed', 'kind': 'obligation', 'content': {
            'text': 'Closed fixture.', 'disposition': 'fulfilled', 'account': 'Fixture closure.'},
            'basis': ['sources/closed.md']}, 0)
        self.system.context('limited', {'records': ['composition', 'closed'], 'record_limit': 1})
        report = self.system.explain_context('limited')
        self.assertEqual([r['kind'] for r in self.unit(report, 'records/closed.json')['inclusion_reasons']],
                         ['governing-record'])
        self.assertNotIn('sources/closed.md', [r['path'] for r in report['units']])

    def test_tampered_retained_body_is_refused(self):
        self.system.context('selected', self.populated())
        report = self.system.explain_context('selected')
        key = self.unit(report, 'sources/shared.md')['sha256']
        objects = list((self.root / 'exchange/objects').rglob(key))
        self.assertEqual(len(objects), 1)
        objects[0].write_bytes(b'Tampered retained fixture.\n')
        before = inventory(self.root)
        with self.assertRaises(ValueError):
            self.system.explain_context('selected')
        self.assertEqual(inventory(self.root), before)

    def test_public_entrances_do_not_create_absent_lock_files(self):
        deployment = self.base / 'deployment'
        Deployment.initialize(deployment, self.root, ENGINE)
        for path in (self.root / 'exchange/owner.lock', deployment / 'deployment.lock'):
            path.unlink(missing_ok=True)
        before = inventory(self.base)
        commands = [
            [sys.executable, '-B', str(ENGINE / 'runtime/manage_store.py'), '--root', str(self.root), 'explain-context', 'start'],
            [sys.executable, '-B', str(ENGINE / 'runtime/select_deployment.py'), '--deployment', str(deployment),
             'system', 'explain-context', 'start']]
        answers = []
        for command in commands:
            run = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual(run.returncode, 0, run.stderr)
            answers.append(json.loads(run.stdout))
            self.assertEqual(inventory(self.base), before)
        self.assertEqual(answers[0], answers[1])

    def test_sealed_diagnostic_recovers_via_metadata_without_opening_body(self):
        domain = self.make_domain('fixture-sealed', 'Synthetic protected text.\n', sealed=True)
        root = self.base / 'sealed-store'
        System.initialize(root, {'contribution': 'Respect explicit sealed exposure.'}, domains=[domain])
        system = System(root)
        protected = domain.parent / 'source.md'
        real_read = Path.read_bytes
        def guarded(path):
            if path.resolve() == protected.resolve():
                raise AssertionError('sealed fixture body opened')
            return real_read(path)
        with patch.object(Path, 'read_bytes', guarded):
            metadata = system.catalogue(query='fixture-sealed')
            self.assertEqual(metadata['entries'][0]['availability'], 'sealed')
            for operation in (
                lambda: system.catalogue('fixture-sealed'),
                lambda: system.discover({'anchors': ['technique:fixture-sealed'], 'scopes': ['techniques'], 'limit': 1}),
                lambda: system.context('sealed', {'techniques': ['fixture-sealed']})):
                with self.assertRaisesRegex(ValueError, "sealed source 'fixture-sealed'.*catalogue metadata"):
                    operation()
        self.assertFalse((root / 'exchange/packets/sealed').exists())

    def test_composition_correction_and_ordinary_content_remain_distinct(self):
        prose = 'A supplied interpretation belongs in an explanatory account.'
        body = {'format': 'g60-composition/1', 'purpose': 'Inspect a bounded interpretation.',
                'relations': [{'id': 'receiving-link', 'prediction': 'A useful selected effect.', 'observation': prose}]}
        record = self.system.put({'id': 'bad-profile', 'kind': 'note', 'content': body}, 0)
        with self.assertRaisesRegex(ValueError, "receiving-link.*observation.*account"):
            Composition(self.system).inspect(record['id'])
        body['relations'][0].update(observation='unassessed', account=prose)
        self.system.put({'id': record['id'], 'kind': 'note', 'content': body}, 1)
        self.assertEqual(Composition(self.system).inspect(record['id'])['relations'][0]['account'], prose)
        body['relations'][0]['observation'] = 'supported'
        body['relations'][0].pop('account')
        self.system.put({'id': record['id'], 'kind': 'note', 'content': body}, 2)
        with self.assertRaisesRegex(ValueError, "receiving-link.*account"):
            Composition(self.system).inspect(record['id'])
        body['relations'][0]['account'] = prose
        self.system.put({'id': record['id'], 'kind': 'note', 'content': body}, 3)
        self.assertEqual(Composition(self.system).inspect(record['id'])['relations'][0]['observation'], 'supported')
        ordinary = {'observation': prose, 'unanticipated': [True, None]}
        self.system.put({'id': 'open-content', 'kind': 'unanticipated-kind', 'content': ordinary}, 0)
        self.assertEqual(self.system.show('open-content')['content'], ordinary)


if __name__ == '__main__':
    unittest.main(verbosity=2)
