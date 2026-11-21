"""Offline trust-boundary tests; never download artifacts or execute native code."""
from __future__ import annotations

import contextlib
import copy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import unittest
from unittest import mock
import zipfile

SCRIPT = Path(__file__).resolve().parents[1] / 'inspect_mixed_qt_artifact.py'
spec = importlib.util.spec_from_file_location('inspect_mixed_qt_artifact', SCRIPT)
reader = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reader)


class ProofReaderTests(unittest.TestCase):
    def setUp(self):
        self.sha = 'a' * 40
        self.summary = {'check': 'mixed-qt-bundling-mechanism-fixture', 'controllerSourceSnapshot': self.sha,
            'architecture': 'x86_64', 'productRuntimeVerified': False, 'status': 'failed',
            'phase': 'negative-bundling-and-native-gates',
            'cases': {'negative': {'cppBaseline': {'exitCode': -6, 'timedOut': False, 'checkpoints': []}}}}
        self.run = {'id': 456, 'head_sha': self.sha, 'event': 'workflow_dispatch',
            'path': '.github/workflows/macos-mixed-qt-diagnostic.yml',
            'repository': {'id': reader.REPOSITORY_ID, 'full_name': reader.REPOSITORY},
            'head_repository': {'id': reader.REPOSITORY_ID, 'full_name': reader.REPOSITORY}}

    def artifact(self, changes=None, extra=(), *, compression=zipfile.ZIP_STORED, summary_bytes=None):
        values = {'summary.json': json.dumps(self.summary).encode() if summary_bytes is None else summary_bytes,
                  'negative-cpp.stderr.log': b'actual captured native failure\n', 'negative-cpp.stdout.ndjson': b''}
        values.update(changes or {})
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, 'w', compression=compression) as archive:
            for name, value in values.items():
                archive.writestr(name, value)
            for name, value in extra:
                archive.writestr(name, value)
        data = stream.getvalue()
        metadata = self.metadata(data)
        return data, metadata

    def metadata(self, data):
        return {'id': 123, 'name': 'macos-mixed-qt-proof-x86_64', 'expired': False,
            'size_in_bytes': len(data), 'digest': 'sha256:' + hashlib.sha256(data).hexdigest(),
            'archive_download_url': f'https://api.github.com/repos/{reader.REPOSITORY}/actions/artifacts/123/zip',
            'workflow_run': {'id': 456, 'repository_id': reader.REPOSITORY_ID,
                             'head_repository_id': reader.REPOSITORY_ID, 'head_sha': self.sha}}

    def test_verified_captured_failure_is_read_only_and_retains_actual_exit(self):
        data, metadata = self.artifact()
        workflow = reader.validate_metadata(metadata, 123)
        reader.validate_run(self.run, workflow)
        summary, selected = reader.read_proof(data, metadata)
        self.assertEqual(summary['cases']['negative']['cppBaseline']['exitCode'], -6)
        self.assertEqual(selected['negative-cpp.stderr.log'], b'actual captured native failure\n')
        self.assertEqual(set(selected), set(reader.SELECTED))

    def test_metadata_refuses_wrong_name_id_size_digest_url_expiry_or_repository(self):
        data, metadata = self.artifact()
        for changes in ({'id': 124}, {'id': True}, {'name': 'localized-release-x86_64'}, {'expired': True},
                        {'size_in_bytes': reader.MAX_ZIP + 1}, {'size_in_bytes': True},
                        {'digest': 'sha256:' + 'z' * 64}, {'digest': None},
                        {'archive_download_url': 'https://elsewhere.invalid/private'}):
            candidate = {**metadata, **changes}
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                reader.validate_metadata(candidate, 123)
        for changes in ({'repository_id': 2}, {'head_repository_id': 2}, {'head_sha': 'short'}):
            candidate = copy.deepcopy(metadata)
            candidate['workflow_run'].update(changes)
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                reader.validate_metadata(candidate, 123)

    def test_run_must_bind_matching_repository_source_workflow_and_manual_event(self):
        _, metadata = self.artifact()
        for changes in ({'id': 457}, {'head_sha': 'b' * 40}, {'event': 'push'}, {'path': '.github/workflows/other.yml'},
                        {'head_repository': {'id': reader.REPOSITORY_ID, 'full_name': 'someone/foreign'}},
                        {'repository': {'id': 2, 'full_name': reader.REPOSITORY}}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                reader.validate_run({**self.run, **changes}, metadata['workflow_run'])

    def test_recreated_repository_rejects_previous_same_name_repository_id(self):
        old_repository_id = 1462447683
        self.assertEqual(reader.REPOSITORY_ID, 1466054290)
        _, metadata = self.artifact()
        for key in ('repository_id', 'head_repository_id'):
            previous = copy.deepcopy(metadata)
            previous['workflow_run'][key] = old_repository_id
            with self.subTest(metadata_field=key), self.assertRaises(ValueError):
                reader.validate_metadata(previous, 123)
        for key in ('repository', 'head_repository'):
            previous = copy.deepcopy(self.run)
            previous[key]['id'] = old_repository_id
            with self.subTest(run_field=key), self.assertRaises(ValueError):
                reader.validate_run(previous, metadata['workflow_run'])

    def test_size_and_digest_are_checked_before_zip_parsing(self):
        data, metadata = self.artifact()
        with mock.patch.object(reader.zipfile, 'ZipFile') as archive:
            with self.assertRaisesRegex(ValueError, 'byte size'):
                reader.read_proof(data + b'X', metadata)
            with self.assertRaisesRegex(ValueError, 'SHA256'):
                reader.read_proof(bytes([data[0] ^ 1]) + data[1:], metadata)
            archive.assert_not_called()

    def test_summary_cannot_claim_product_acceptance_or_different_source_or_architecture(self):
        for change in ({'productRuntimeVerified': True}, {'productRuntimeVerified': 0},
                       {'controllerSourceSnapshot': 'b' * 40}, {'architecture': 'arm64'}, {'check': 'product-passed'}):
            self.summary.update(change)
            data, metadata = self.artifact()
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, 'summary source'):
                reader.read_proof(data, metadata)
            self.setUp()

    def test_zip_path_symlink_special_duplicate_and_encryption_boundaries(self):
        for name in ('../escape', '/absolute', 'a\\b', 'a//b', './summary.json', 'C:drive'):
            data, metadata = self.artifact(extra=[(name, b'not read')])
            if '\\' in name:
                # Windows ZipInfo normalizes os.sep while writing; construct
                # the actual hostile archive name in both ZIP headers.
                data = data.replace(name.replace('\\', '/').encode(), name.encode())
                metadata = self.metadata(data)
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, 'unsafe member path'):
                reader.read_proof(data, metadata)
        for mode in (stat.S_IFLNK | 0o777, stat.S_IFIFO | 0o600):
            info = zipfile.ZipInfo('unexpected-entry')
            info.create_system = 3
            info.external_attr = mode << 16
            data, metadata = self.artifact(extra=[(info, b'../escape')])
            with self.subTest(mode=mode), self.assertRaisesRegex(ValueError, 'symlink or special'):
                reader.read_proof(data, metadata)
        data, metadata = self.artifact(extra=[('SUMMARY.JSON', b'duplicate identity')])
        with self.assertRaisesRegex(ValueError, 'duplicate member'):
            reader.read_proof(data, metadata)
        encrypted = bytearray(data)
        for signature, offset in ((b'PK\x03\x04', 6), (b'PK\x01\x02', 8)):
            start = encrypted.index(signature)
            encrypted[start + offset] |= 1
        data = bytes(encrypted)
        with self.assertRaisesRegex(ValueError, 'encryption'):
            reader.read_proof(data, self.metadata(data))

    def test_zip_bomb_member_total_entry_count_and_missing_member_limits(self):
        data, metadata = self.artifact({'negative-cpp.stderr.log': b'x' * 200000}, compression=zipfile.ZIP_DEFLATED)
        with self.assertRaisesRegex(ValueError, 'compression-ratio'):
            reader.read_proof(data, metadata)
        data, metadata = self.artifact(extra=[(f'entry-{i}', b'x') for i in range(32)])
        with self.assertRaisesRegex(ValueError, 'too many entries'):
            reader.read_proof(data, metadata)
        data, metadata = self.artifact({'negative-cpp.stderr.log': b'x' * 600})
        with mock.patch.object(reader, 'MAX_MEMBER', 512), self.assertRaisesRegex(ValueError, 'uncompressed byte'):
            reader.read_proof(data, metadata)
        data, metadata = self.artifact(extra=[('one', b'x' * 400), ('two', b'y' * 400)])
        with mock.patch.object(reader, 'MAX_UNCOMPRESSED', 1000), self.assertRaisesRegex(ValueError, 'uncompressed byte'):
            reader.read_proof(data, metadata)
        data, metadata = self.artifact()
        with mock.patch.object(reader, 'SELECTED', (*reader.SELECTED, 'missing.log')), \
             self.assertRaisesRegex(ValueError, 'missing a captured'):
            reader.read_proof(data, metadata)

    def test_duplicate_json_fields_cannot_override_controller_identity(self):
        with self.assertRaisesRegex(ValueError, 'Duplicate JSON'):
            reader.unique_json(b'{"controllerSourceSnapshot":"first","controllerSourceSnapshot":"second"}')

    def test_redaction_precedes_utf8_tail_and_command_markers_surround_remote_text(self):
        remote = ("\x1b[31m::error::remote-command\x1b[0m\nAuthorization: Bearer secret-header\n"
                  "https://blob.invalid/path?sig=secret-sas\nGH_TOKEN=secret-env\nghp_testsecret123\n"
                  "sig=raw-secret\nCookie: private-cookie\n" + '中文' * 10000)
        with mock.patch.dict(os.environ, {'GH_TOKEN': 'secret-env'}):
            sanitized = reader.redact(remote)
            bounded = reader.tail(remote, 12 * 1024)
            with contextlib.redirect_stdout(io.StringIO()) as output:
                reader.emit_sections([('CAPTURED:', sanitized)])
        for secret in ('secret-header', 'secret-sas', 'secret-env', 'ghp_testsecret123', 'raw-secret', 'private-cookie', '\x1b'):
            self.assertNotIn(secret, sanitized)
        self.assertLessEqual(len(bounded.encode()) + 1, 12 * 1024)
        lines = output.getvalue().splitlines()
        token = lines[0].split('::stop-commands::', 1)[1]
        self.assertEqual(lines[-1], '::' + token + '::')
        self.assertIn('::error::remote-command', output.getvalue())

    def test_cli_reads_only_after_metadata_and_run_identity_then_emits_captured_stderr(self):
        data, metadata = self.artifact({'negative-cpp.stderr.log': b'qt.qpa.plugin: actual captured error\n'})
        replies = [json.dumps(metadata).encode(), json.dumps(self.run).encode(), data]
        with mock.patch.dict(os.environ, {'GITHUB_REPOSITORY': reader.REPOSITORY}), \
             mock.patch.object(reader, 'gh_get', side_effect=replies) as request, \
             contextlib.redirect_stdout(io.StringIO()) as output:
            result = reader.main(['123'])
        self.assertEqual(result, 0)
        self.assertEqual(request.call_count, 3)
        self.assertEqual(request.call_args.args[1], len(data))
        self.assertIn('actual captured error', output.getvalue())
        self.assertIn('"negativeCppExitCode": -6', output.getvalue())
        self.assertNotIn('checkpoints', output.getvalue())
        metadata['name'] = 'not-a-proof'
        with mock.patch.dict(os.environ, {'GITHUB_REPOSITORY': reader.REPOSITORY}), \
             mock.patch.object(reader, 'gh_get', return_value=json.dumps(metadata).encode()) as request, \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(reader.main(['123']), 1)
        self.assertEqual(request.call_count, 1)

    def test_numeric_and_repository_inputs_fail_before_any_network(self):
        for arguments in ([], ['0'], ['-1'], ['1;echo unsafe'], ['123', '456'], ['1' * 21]):
            with self.subTest(arguments=arguments), mock.patch.object(reader, 'gh_get') as request, \
                 contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(reader.main(arguments), 1)
                request.assert_not_called()
        with mock.patch.dict(os.environ, {'GITHUB_REPOSITORY': 'someone/foreign'}), \
             mock.patch.object(reader, 'gh_get') as request, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(reader.main(['123']), 1)
            request.assert_not_called()

    def test_transport_stops_at_byte_limit_discards_stderr_and_removes_gh_debug(self):
        process = mock.MagicMock()
        process.__enter__.return_value = process
        process.stdout.fileno.return_value = 9
        process.poll.return_value = None
        process.wait.return_value = 0
        selector = mock.MagicMock()
        selector.__enter__.return_value = selector
        selector.select.return_value = [True]
        with mock.patch.dict(os.environ, {'GH_DEBUG': 'api', 'GH_TOKEN': 'never-print-this'}), \
             mock.patch.object(reader.subprocess, 'Popen', return_value=process) as spawn, \
             mock.patch.object(reader.selectors, 'DefaultSelector', return_value=selector), \
             mock.patch.object(reader.os, 'read', return_value=b'x' * 65):
            with self.assertRaisesRegex(ValueError, 'byte limit'):
                reader.gh_get('repos/authorized/read-only', 64)
        self.assertEqual(spawn.call_args.kwargs['stderr'], subprocess.DEVNULL)
        self.assertNotIn('GH_DEBUG', spawn.call_args.kwargs['env'])
        process.kill.assert_called_once()


if __name__ == '__main__':
    unittest.main()
