"""Offline failure contracts plus real Git provenance fixtures for readiness."""
from __future__ import annotations

import copy
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[1] / 'macos_graphics_gate.py'
spec = importlib.util.spec_from_file_location('macos_graphics_gate', SCRIPT)
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)
GLEW_SHA = 'a' * 64
POLICY_SHA = 'b' * 64


def gl_report():
    return {'contextCurrent': True, 'version': '2.1 native', 'renderer': 'actual renderer', 'vendor': 'actual vendor', 'glslVersion': '1.20',
            'glewStatus': 0, 'glewVersion21': True, 'extFramebufferObject': True, 'glsl120Linked': True,
            'rvioContextReady': True, 'allFloatFormatsPassed': True,
            'floatFboTests': [{'format': name, 'status': 'passed', 'framebufferStatus': 0x8CD5,
                'glError': 0, 'priorGlErrors': [], 'pixelMatched': True, 'pixel': [0.125, 0.5, 0.75, 1.0]}
                for name in gate.FLOAT_FORMATS]}


def results():
    base = {'check': 'native-macos-graphics-diagnostic', 'productRuntimeVerified': False,
            'qtVersion': '6.5.3', 'bundledGlewSourceSha256': GLEW_SHA, 'productionContextPolicySha256': POLICY_SHA}
    cgl = dict(base, mode='cgl', status='diagnostic-collected', cases=[{
        'name': 'production-policy', 'status': 'ready', 'selectedCandidate': 'typed-generic-float-renderer',
        'contextCurrent': True, 'attempts': [
            {'name': 'typed-hardware-float', 'chooseError': 10000, 'pixelFormatCount': 0},
            {'name': 'typed-hardware-rgba8', 'chooseError': 10000, 'pixelFormatCount': 0},
            {'name': 'typed-generic-float-renderer', 'chooseError': 0, 'pixelFormatCount': 1,
             'createError': 0, 'currentError': 0}], 'gl': gl_report()}])
    qt = dict(base, mode='qt-default', status='passed', platformPlugin='cocoa',
        softwareAttributeBeforeApplication=False, openGLES=False, timedOut=False,
        actualMajor=2, actualMinor=1, actualDepth=32, actualStencil=8,
        widgetVisible=True, widgetValid=True, windowExposed=True, visibleReadbackMatched=True,
        imageWidth=96, imageHeight=96, widgetPixel=[32, 128, 191, 255], gl=gl_report())
    return {mode: {'timedOut': False, 'exitCode': 0, 'report': report}
            for mode, report in (('cgl', cgl), ('qt-default', qt))}


class ReadinessTests(unittest.TestCase):
    def reject(self, data):
        with self.assertRaises(ValueError):
            gate.readiness(data, GLEW_SHA, POLICY_SHA)

    def test_actual_production_policy_and_visible_default_qt_are_required(self):
        self.assertEqual(gate.readiness(results(), GLEW_SHA, POLICY_SHA), 'typed-generic-float-renderer')

    def test_each_real_product_selection_is_allowed_without_changing_its_priority(self):
        for index, name in enumerate(gate.PRODUCT_CGL_SEQUENCE):
            data = results()
            case = data['cgl']['report']['cases'][0]
            final = dict(case['attempts'][-1], name=name)
            case['attempts'] = case['attempts'][:index] + [final]
            case['selectedCandidate'] = name
            with self.subTest(selected=name):
                self.assertEqual(gate.readiness(data, GLEW_SHA, POLICY_SHA), name)

    def test_shared_policy_context_and_actual_attempt_identity_must_agree(self):
        for change in ({'contextCurrent': False}, {'selectedCandidate': 'typed-minimal'},
                       {'attempts': []}, {'attempts': None}, {'status': 'failed'}):
            data = results()
            data['cgl']['report']['cases'][0].update(change)
            with self.subTest(change=change):
                self.reject(data)
        data = results()
        data['cgl']['report']['cases'].append(copy.deepcopy(data['cgl']['report']['cases'][0]))
        self.reject(data)
        data = results()
        data['cgl']['report']['cases'][0]['attempts'][-1]['currentError'] = 10000
        self.reject(data)

    def test_collection_completion_and_unrelated_ready_candidate_do_not_pass(self):
        self.reject({'collectionStatus': 'completed'})
        data = results()
        case = data['cgl']['report']['cases'][0]
        case['name'] = 'typed-minimal'
        data['cgl']['report']['rvioCandidateReady'] = True
        self.reject(data)

    def test_failed_production_policy_cannot_be_hidden_by_other_passing_candidates(self):
        data = results()
        passing = copy.deepcopy(data['cgl']['report']['cases'][0])
        passing['name'] = 'typed-minimal'
        data['cgl']['report']['cases'].append(passing)
        data['cgl']['report']['cases'][0]['gl']['floatFboTests'][1]['pixel'][0] = 0
        self.reject(data)

    def test_all_three_float_formats_must_really_draw_and_read_back(self):
        changes = ({'status': 'failed'}, {'framebufferStatus': 0}, {'glError': 1282},
                   {'pixelMatched': False}, {'pixel': [0, 0, 0, 0]}, {'priorGlErrors': [1280]})
        for mode in ('cgl', 'qt-default'):
            for change in changes:
                data = results()
                report = data[mode]['report']
                gl = report['cases'][0]['gl'] if mode == 'cgl' else report['gl']
                gl['floatFboTests'][2].update(change)
                with self.subTest(mode=mode, change=change):
                    self.reject(data)
        for formats in ([], [gate.FLOAT_FORMATS[0]] * 3):
            data = results()
            data['cgl']['report']['cases'][0]['gl']['floatFboTests'] = [
                dict(gl_report()['floatFboTests'][0], format=name) for name in formats]
            self.reject(data)

    def test_context_glew_and_shader_failures_are_failures(self):
        for field, value in (('contextCurrent', False), ('glewStatus', 1), ('glewStatus', False), ('glewVersion21', False),
                             ('extFramebufferObject', False), ('glsl120Linked', False),
                             ('allFloatFormatsPassed', False), ('rvioContextReady', False)):
            data = results()
            data['cgl']['report']['cases'][0]['gl'][field] = value
            with self.subTest(field=field):
                self.reject(data)

    def test_real_cocoa_widget_visibility_and_pixels_cannot_be_substituted(self):
        changes = ({'platformPlugin': 'offscreen'}, {'softwareAttributeBeforeApplication': True},
                   {'widgetVisible': False}, {'widgetValid': False}, {'windowExposed': False},
                   {'visibleReadbackMatched': False}, {'timedOut': True}, {'openGLES': True},
                   {'actualMajor': 1, 'actualMinor': 5}, {'actualMajor': 2, 'actualMinor': 0},
                   {'actualMajor': True}, {'actualMinor': None}, {'actualDepth': 16}, {'actualStencil': 0},
                   {'imageWidth': 0}, {'widgetPixel': [0, 0, 0, 0]}, {'status': 'diagnostic-collected'})
        for change in changes:
            data = results()
            data['qt-default']['report'].update(change)
            with self.subTest(change=change):
                self.reject(data)

    def test_wrong_probe_source_identity_failure_timeout_or_malformed_reports_cannot_pass(self):
        for mode in ('cgl', 'qt-default'):
            for change in ({'timedOut': True}, {'exitCode': 3}, {'exitCode': False}, {'report': None}):
                data = results()
                data[mode].update(change)
                with self.subTest(mode=mode, change=change):
                    self.reject(data)
            for change in ({'bundledGlewSourceSha256': 'b' * 64}, {'productionContextPolicySha256': 'c' * 64}, {'qtVersion': '6.6.0'},
                           {'mode': 'qt-software'}, {'productRuntimeVerified': True}):
                data = results()
                data[mode]['report'].update(change)
                with self.subTest(mode=mode, change=change):
                    self.reject(data)

    def test_collector_records_actual_process_results_and_clears_qt_overrides(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            raw = results()
            responses = [subprocess.CompletedProcess([], 0, json.dumps(raw[mode]['report']), 'native stderr')
                         for mode in ('cgl', 'qt-default')]
            with mock.patch.dict(os.environ, {'QT_OPENGL': 'software', 'QT_QPA_PLATFORM': 'offscreen',
                                              'QT_QUICK_BACKEND': 'software'}), \
                    mock.patch.object(gate.subprocess, 'run', side_effect=responses) as run:
                collected = gate.collect(output / 'probe', output)
            self.assertEqual(collected, raw)
            for call in run.call_args_list:
                self.assertEqual(call.kwargs['timeout'], 90)
                for key in ('QT_OPENGL', 'QT_QPA_PLATFORM', 'QT_QUICK_BACKEND'):
                    self.assertNotIn(key, call.kwargs['env'])
            self.assertEqual((output / 'cgl.stderr.log').read_text(), 'native stderr')
            self.assertEqual(json.loads((output / 'qt-default.stdout.json').read_text())['mode'], 'qt-default')

    def test_timeout_stays_failed_even_if_partial_stdout_claims_pass(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            timeout = subprocess.TimeoutExpired(['probe'], 90,
                output=json.dumps(results()['cgl']['report']).encode(), stderr=b'timed out')
            with mock.patch.object(gate.subprocess, 'run', side_effect=[timeout, OSError('not executable')]):
                collected = gate.collect(output / 'probe', output)
            self.assertTrue(collected['cgl']['timedOut'])
            self.reject(collected)
            self.assertTrue((output / 'qt-default.stderr.log').exists())

    def test_cli_outputs_real_failed_json_and_nonzero_exit_for_compile_or_native_failure(self):
        for failure in ('none', 'compile', 'native'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as temporary:
                repo = Path(temporary)
                qt, output = repo / 'Qt SDK', repo / 'reports'
                qt.mkdir()
                policy = repo / gate.POLICY_HEADER
                policy.parent.mkdir(parents=True)
                policy.write_bytes(b'actual composed header bytes\n')
                policy_hash = gate.hashlib.sha256(policy.read_bytes()).hexdigest()
                modes = results()
                for record in modes.values():
                    record['report']['productionContextPolicySha256'] = policy_hash
                if failure == 'native':
                    modes['cgl']['report']['cases'][0]['gl']['floatFboTests'][0]['pixel'][0] = 0
                plan = {'localization_snapshot': 'c' * 40, 'official': {'commit_sha': 'd' * 40},
                        'patched_tree': 'e' * 40}
                verify = mock.Mock(return_value=plan)
                with mock.patch.dict(sys.modules, {'official_workspace': SimpleNamespace(verify_materialized=verify)}), \
                        mock.patch.object(sys, 'argv', ['gate', '--repo', str(repo), '--qt', str(qt),
                            '--build', str(repo / 'build'), '--output', str(output), '--arch', 'arm64']), \
                        mock.patch.dict(os.environ, {'GITHUB_ACTIONS': 'true', 'GITHUB_SHA': 'c' * 40,
                            'ORV_BUILD_NUMBER': '26', 'ORV_BUILD_PLAN_SHA256': 'f' * 64}), \
                        mock.patch.object(gate.platform, 'system', return_value='Darwin'), \
                        mock.patch.object(gate.platform, 'machine', return_value='arm64'), \
                        mock.patch.object(gate, 'vendor_identity', return_value={
                            'files': {'glew/glew.c': {'sha256': GLEW_SHA}}}), \
                        mock.patch.object(gate, 'build_probe', side_effect=ValueError('actual compile failed')
                            if failure == 'compile' else None, return_value=repo / 'probe'), \
                        mock.patch.object(gate, 'collect', return_value=modes), \
                        contextlib.redirect_stdout(io.StringIO()) as stdout:
                    code = gate.main()
                report = json.loads((output / 'readiness.json').read_text(encoding='utf-8'))
                self.assertEqual(json.loads(stdout.getvalue()), report)
                self.assertIs(report['productRuntimeVerified'], False)
                self.assertEqual(report['status'], 'passed' if failure == 'none' else 'failed')
                self.assertEqual(code, 0 if failure == 'none' else 1)
                self.assertEqual(report['officialSourceCommit'], 'd' * 40)
                verify.assert_called_once_with(repo, expected_snapshot='c' * 40, expected_build='26')
                if failure != 'compile':
                    self.assertEqual(report['modes'], modes)
                if failure != 'none':
                    self.assertIn('error', report)


@unittest.skipUnless(shutil.which('git'), 'A real Git is required for vendor provenance fixtures')
class VendorProvenanceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.repo = Path(temporary.name)
        self.vendor = self.repo / 'src/pub'
        self.vendor.mkdir(parents=True)
        for repo in (self.repo, self.vendor):
            self.git(repo, 'init', '-q')
            self.git(repo, 'config', 'user.name', 'Offline fixture')
            self.git(repo, 'config', 'user.email', 'fixture@example.invalid')
            self.git(repo, 'config', 'core.autocrlf', 'false')
        for name in ('glew/glew.c', 'glew/GL/glew.h'):
            path = self.vendor / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'original vendor bytes\n')
        self.git(self.vendor, 'add', '.')
        self.git(self.vendor, 'commit', '-qm', 'original vendor')
        self.official_vendor = self.git(self.vendor, 'rev-parse', 'HEAD')
        self.git(self.repo, 'update-index', '--add', '--cacheinfo', '160000,' + self.official_vendor + ',src/pub')
        self.git(self.repo, 'commit', '-qm', 'original official source')
        self.official = self.git(self.repo, 'rev-parse', 'HEAD')
        (self.vendor / 'glew/glew.c').write_bytes(b'verified maintenance vendor overlay\n')
        self.git(self.vendor, 'add', '.')
        self.git(self.vendor, 'commit', '-qm', 'new composed vendor')
        self.composed_vendor = self.git(self.vendor, 'rev-parse', 'HEAD')
        self.git(self.repo, 'update-index', '--cacheinfo', '160000,' + self.composed_vendor + ',src/pub')
        self.plan = {'official': {'commit_sha': self.official}, 'patched_tree': self.git(self.repo, 'write-tree')}

    def git(self, repo, *args):
        return subprocess.check_output(['git', '-C', str(repo), *args], stderr=subprocess.PIPE).decode().strip()

    def test_exact_composed_vendor_is_verified_without_claiming_it_is_the_original(self):
        identity = gate.vendor_identity(self.repo, self.plan)
        self.assertEqual(identity['officialVendorCommit'], self.official_vendor)
        self.assertEqual(identity['composedVendorCommit'], self.composed_vendor)
        self.assertEqual(identity['checkedOutVendorCommit'], self.composed_vendor)
        data = (self.vendor / 'glew/glew.c').read_bytes()
        self.assertEqual(identity['files']['glew/glew.c']['sha256'], gate.hashlib.sha256(data).hexdigest())

    def test_stale_vendor_checkout_is_rejected(self):
        self.git(self.vendor, 'checkout', '--detach', self.official_vendor)
        with self.assertRaisesRegex(ValueError, 'differs from composed'):
            gate.vendor_identity(self.repo, self.plan)

    def test_modified_vendor_bytes_are_rejected(self):
        (self.vendor / 'glew/GL/glew.h').write_bytes(b'unverified bytes\n')
        with self.assertRaises(ValueError):
            gate.vendor_identity(self.repo, self.plan)


if __name__ == '__main__':
    unittest.main()
