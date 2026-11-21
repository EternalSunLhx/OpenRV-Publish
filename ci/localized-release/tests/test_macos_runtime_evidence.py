"""Offline boundaries for the explicit diagnostic artifact, not native Mac proof."""
import copy
import contextlib
import datetime
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[1] / 'collect_macos_runtime_evidence.py'
spec = importlib.util.spec_from_file_location('macos_runtime_evidence', SCRIPT)
evidence = importlib.util.module_from_spec(spec)
spec.loader.exec_module(evidence)


class RuntimeEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.repo = Path(self.temporary.name).resolve() / 'repo'
        self.repo.mkdir()
        self.work = self.repo / '_release_work'
        self.persistence = self.work / 'persistence-arm64'
        self.persistence.mkdir(parents=True)
        self.diagnostics = Path(self.temporary.name).resolve() / 'DiagnosticReports'
        self.diagnostics.mkdir()
        self.executable = self.repo / '_install_release/RV.app/Contents/MacOS/rv'
        self.executable.parent.mkdir(parents=True)
        self.executable.write_bytes(b'owned fixture')
        self.now = datetime.datetime.now(datetime.timezone.utc)
        self.report = {'startedAtUtc': (self.now - datetime.timedelta(seconds=20)).isoformat(),
                       'finishedAtUtc': self.now.isoformat(), 'status': 'failed',
                       'launches': [{'pid': 123, 'stage': '03-chinese-persisted', 'exitCode': -10}]}
        self.write_json(self.persistence / 'report.json', self.report)
        self.crash = {'pid': 123, 'procPath': str(self.executable), 'faultingThread': 0,
                      'exception': {'type': 'EXC_BAD_ACCESS', 'signal': 'SIGBUS'},
                      'termination': {'namespace': 'SIGNAL', 'code': 10},
                      'threads': [{'triggered': True, 'threadState': {'sensitive_register': 999},
                                   'frames': [{'imageIndex': 0, 'symbol': 'owned_frame', 'imageOffset': 16}]}],
                      'usedImages': [{'name': 'rv', 'path': str(self.executable), 'base': 100}],
                      'crashReporterKey': 'must not copy', 'environment': {'SECRET': 'must not copy'}}

    @staticmethod
    def write_json(path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding='utf-8')

    def ips(self, name, value=None, age=0):
        path = self.diagnostics / name
        path.write_text(json.dumps({'bug_type': '309', 'crashReporterKey': 'private'}) + '\n' +
                        json.dumps(self.crash if value is None else value), encoding='utf-8')
        timestamp = self.now.timestamp() - age
        os.utime(path, (timestamp, timestamp))
        return path

    def test_only_owned_exact_identity_stack_is_selected_without_raw_private_fields(self):
        self.ips('rv-owned.ips')
        other_pid = dict(self.crash, pid=456)
        self.ips('rv-other-pid.ips', other_pid)
        self.ips('rv-other-path.ips', dict(self.crash, procPath='/another/RV.app/Contents/MacOS/rv'))
        self.ips('rv-anonymous-path.ips', dict(self.crash, procPath='/Users/USER/RV.app/Contents/MacOS/rv'))
        self.ips('rv-previous-run.ips', age=500)
        self.ips('unrelated-app.ips')
        output = evidence.collect(self.repo, 'arm64', self.diagnostics)
        stacks = list((output / 'crash-stacks').glob('*.json'))
        self.assertEqual(len(stacks), 1)
        stack = json.loads(stacks[0].read_text())
        self.assertEqual(stack['frames'][0]['symbol'], 'owned_frame')
        self.assertNotIn('must not copy', stacks[0].read_text())
        self.assertNotIn('sensitive_register', stacks[0].read_text())
        self.assertEqual(json.loads((output / 'persistence-arm64/report.json').read_text())['status'], 'failed')

    def test_explicit_files_only_and_bounded_tail_json_and_total(self):
        stage = self.persistence / '03-chinese-persisted'
        stage.mkdir()
        (stage / 'stdout.log').write_bytes(b'a' * (evidence.LOG_LIMIT + 30) + b'LATEST')
        (stage / 'stderr.log').write_bytes(b'b' * evidence.LOG_LIMIT)
        (stage / 'gui.json').write_bytes(b' ' * (evidence.JSON_LIMIT + 1))
        (stage / 'config.json').write_text('private config')
        (self.persistence / 'original-language-key.json').write_text('private preferences')
        (stage / 'crashes').mkdir()
        (stage / 'crashes' / 'memory.dmp').write_text('process memory')
        with mock.patch.object(evidence, 'TOTAL_LIMIT', evidence.INDEX_RESERVE + evidence.LOG_LIMIT + 1024):
            output = evidence.collect(self.repo, 'arm64', self.diagnostics)
            total = sum(path.stat().st_size for path in output.rglob('*') if path.is_file())
            self.assertLessEqual(total, evidence.TOTAL_LIMIT)
        tail = output / 'persistence-arm64/03-chinese-persisted/stdout.log'
        self.assertEqual(tail.stat().st_size, evidence.LOG_LIMIT)
        self.assertTrue(tail.read_bytes().endswith(b'LATEST'))
        names = {path.name for path in output.rglob('*') if path.is_file()}
        self.assertFalse({'config.json', 'original-language-key.json', 'memory.dmp', 'gui.json'} & names)
        index = json.loads((output / 'index.json').read_text())
        self.assertEqual({entry['reason'] for entry in index['omitted']}, {'JSON byte limit', 'total byte limit'})

    def test_missing_pid_cannot_supply_crash_evidence(self):
        self.ips('rv-owned.ips')
        self.report['launches'][0].pop('pid')
        self.write_json(self.persistence / 'report.json', self.report)
        output = evidence.collect(self.repo, 'arm64', self.diagnostics)
        self.assertFalse((output / 'crash-stacks').exists())

    def test_smoke_gui_crashes_are_owned_even_before_persistence_runs(self):
        (self.persistence / 'report.json').unlink()
        smoke = {key: self.report[key] for key in ('startedAtUtc', 'finishedAtUtc')}
        smoke['steps'] = [{'name': 'gui-en', 'pid': 456}, {'name': 'gui-zh_CN', 'pid': 789},
                          {'name': 'python-ocio', 'pid': 987}, {'name': 'aac-mp4', 'pid': 654}]
        self.write_json(self.work / 'smoke-arm64/report.json', smoke)
        for name, pid in (('en', 456), ('zh', 789), ('python', 987), ('aac', 654), ('unknown', 345)):
            self.ips('rv-' + name + '.ips', dict(self.crash, pid=pid))
        self.ips('rv-en-wrong-executable.ips', dict(self.crash, pid=456, procPath='/another/rv'))
        output = evidence.collect(self.repo, 'arm64', self.diagnostics)
        stacks = [json.loads(path.read_text()) for path in (output / 'crash-stacks').glob('*.json')]
        self.assertEqual({stack['pid'] for stack in stacks}, {456, 789})
        self.assertEqual(len(stacks), 2)

    def test_each_pid_remains_bound_to_its_own_report_time_window(self):
        smoke = {'startedAtUtc': (self.now - datetime.timedelta(seconds=500)).isoformat(),
                 'finishedAtUtc': (self.now - datetime.timedelta(seconds=480)).isoformat(),
                 'steps': [{'name': 'gui-en', 'pid': 456}]}
        self.write_json(self.work / 'smoke-arm64/report.json', smoke)
        self.ips('rv-persistence-valid.ips')
        self.ips('rv-persistence-outside-own-window.ips', age=490)
        self.ips('rv-smoke-valid.ips', dict(self.crash, pid=456), age=490)
        self.ips('rv-smoke-outside-own-window.ips', dict(self.crash, pid=456))
        output = evidence.collect(self.repo, 'arm64', self.diagnostics)
        stacks = [json.loads(path.read_text()) for path in (output / 'crash-stacks').glob('*.json')]
        self.assertEqual({stack['pid'] for stack in stacks}, {123, 456})
        self.assertEqual(len(stacks), 2)

    def test_smoke_pid_without_both_valid_timestamps_is_not_owned(self):
        smoke = {'startedAtUtc': self.now.isoformat(), 'steps': [{'name': 'gui-en', 'pid': 456}]}
        self.write_json(self.work / 'smoke-arm64/report.json', smoke)
        self.ips('rv-smoke-no-finish.ips', dict(self.crash, pid=456))
        output = evidence.collect(self.repo, 'arm64', self.diagnostics)
        self.assertFalse((output / 'crash-stacks').exists())

    def test_oversized_ips_cannot_supply_crash_evidence(self):
        oversized = self.ips('rv-owned.ips')
        size = oversized.stat().st_size
        with mock.patch.object(evidence, 'IPS_LIMIT', size - 1):
            output = evidence.collect(self.repo, 'arm64', self.diagnostics)
        self.assertFalse((output / 'crash-stacks').exists())

    def test_cli_rejects_non_hosted_mac_before_collection(self):
        with mock.patch.object(evidence.sys, 'argv', ['collector', '--arch', 'arm64']), \
                mock.patch.object(evidence.sys, 'platform', 'darwin'), \
                mock.patch.dict(os.environ, {'GITHUB_ACTIONS': 'true', 'RUNNER_OS': 'macOS',
                                             'RUNNER_ENVIRONMENT': 'self-hosted'}, clear=True), \
                mock.patch.object(evidence, 'collect') as collect, \
                contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as stopped:
            evidence.main()
        self.assertEqual(stopped.exception.code, 2)
        collect.assert_not_called()

    def test_path_escape_and_symlink_are_rejected(self):
        outside = Path(self.temporary.name).resolve() / 'outside.json'
        outside.write_text('private')
        self.assertFalse(evidence.safe_file(self.repo, self.repo / '..' / 'outside.json'))
        self.assertFalse(evidence.safe_file(self.repo, outside))
        source = self.persistence / 'report.json'
        original = Path.is_symlink
        with mock.patch.object(Path, 'is_symlink', lambda path: path == self.persistence or original(path)):
            self.assertFalse(evidence.safe_file(self.repo, source))
        with mock.patch.object(Path, 'is_symlink', lambda path: path == self.work or original(path)):
            with self.assertRaisesRegex(ValueError, 'symlink'):
                evidence.collect(self.repo, 'arm64', self.diagnostics)

    def test_malformed_identity_and_frame_fields_are_rejected_or_bounded(self):
        for change in ({'pid': '123'}, {'pid': True}, {'faultingThread': -1}, {'threads': {}}, {'usedImages': None}):
            self.assertIsNone(evidence.crash_stack(json.dumps(dict(self.crash, **change)), self.executable, {123}))
        oversized = copy.deepcopy(self.crash)
        oversized['threads'][0]['frames'] *= 300
        selected = evidence.crash_stack(json.dumps(oversized), self.executable, {123})
        self.assertEqual(len(selected['frames']), 256)
        self.assertTrue(selected['framesTruncated'])


if __name__ == '__main__':
    unittest.main()
