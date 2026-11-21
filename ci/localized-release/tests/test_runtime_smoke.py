"""Generated GUI shutdown protocol and acceptance gates; no native Qt execution."""
from __future__ import annotations

import ast
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import traceback
import types
import unittest
from unittest import mock

SPEC = importlib.util.spec_from_file_location('runtime_smoke_under_test', Path(__file__).resolve().parents[1] / 'runtime_smoke.py')
smoke = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(smoke)


class GuiCloseProtocolTests(unittest.TestCase):
    @contextlib.contextmanager
    def fixture(self, *, platform='darwin', accepted=True):
        queued, destroyed = [], []
        window = mock.Mock()
        window.objectName.return_value = 'rv-session000'
        window.isVisible.return_value = True
        window.testAttribute.return_value = True
        window.close.return_value = accepted
        window.destroyed.connect.side_effect = destroyed.append
        app = mock.Mock()
        app.quitOnLastWindowClosed.return_value = True
        def single_shot(delay, callback):
            self.assertEqual(delay, 0, 'Close must be queued without an elapsed-time delay')
            queued.append(callback)
        core = types.SimpleNamespace(Qt=types.SimpleNamespace(WA_DeleteOnClose=1, WA_QuitOnClose=2),
                                     QTimer=types.SimpleNamespace(singleShot=single_shot),
                                     QCoreApplication=mock.Mock())
        functions = [node for node in ast.parse(smoke.GUI_SMOKE).body
                     if isinstance(node, ast.FunctionDef) and node.name == 'finish_gui']
        self.assertEqual(len(functions), 1)
        with tempfile.TemporaryDirectory() as temporary, contextlib.redirect_stdout(io.StringIO()) as output, \
                contextlib.redirect_stderr(io.StringIO()), \
                mock.patch.dict(os.environ, {'ORV_GUI_REPORT': str(Path(temporary) / 'gui.json')}):
            proof = {'language': 'zh_CN', 'fileSources': ['sourceGroup000001']}
            namespace = {'sys': types.SimpleNamespace(platform=platform), 'os': os, 'json': json,
                         'QtCore': core, 'traceback': traceback,
                         'gui_state': {'closing': False, 'finished': False}}
            exec(compile(ast.Module(body=functions, type_ignores=[]), '<actual-gui-close>', 'exec'), namespace)
            yield namespace, app, window, proof, queued, destroyed, Path(os.environ['ORV_GUI_REPORT']), output, core

    def test_mac_success_waits_for_close_acceptance_and_destruction_without_direct_exit(self):
        with self.fixture() as (namespace, app, window, proof, queued, destroyed, report, output, core):
            namespace['finish_gui'](app, proof, [window])
            namespace['finish_gui'](app, proof, [window])
            self.assertEqual(len(queued), 1)
            self.assertEqual(proof['status'], 'closing')
            window.close.assert_not_called()
            self.assertFalse(report.exists())
            queued[0]()
            window.close.assert_called_once_with()
            self.assertTrue(proof['requestClose'] and proof['closeAccepted'])
            self.assertFalse(proof['sessionClosed'])
            self.assertFalse(report.exists())
            self.assertNotIn('GUI_LANGUAGE_OK', output.getvalue())
            destroyed[0]()
            actual = json.loads(report.read_text(encoding='utf-8'))
            self.assertTrue(smoke.step_passed(0, False, output.getvalue(), 'GUI_LANGUAGE_OK',
                                             gui_language='zh_CN', gui_details=actual))
            self.assertTrue(namespace['gui_state']['finished'])
            app.exit.assert_not_called()
            core.QCoreApplication.exit.assert_not_called()

    def test_rejected_or_premature_close_cannot_write_a_success_report(self):
        for premature in (False, True):
            with self.subTest(premature=premature), self.fixture(accepted=False) as fixture:
                namespace, app, window, proof, queued, destroyed, report, output, core = fixture
                namespace['finish_gui'](app, proof, [window])
                if premature:
                    destroyed[0]()
                queued[0]()
                self.assertFalse(report.exists())
                self.assertNotIn('GUI_LANGUAGE_OK', output.getvalue())
                core.QCoreApplication.exit.assert_called_once_with(1)
                app.exit.assert_not_called()

    def test_mac_refuses_ambiguous_sessions_or_disabled_normal_auto_quit(self):
        for ambiguous in (False, True):
            with self.subTest(ambiguous=ambiguous), self.fixture() as fixture:
                namespace, app, window, proof, queued, destroyed, report, output, core = fixture
                if not ambiguous:
                    app.quitOnLastWindowClosed.return_value = False
                with self.assertRaises(AssertionError):
                    namespace['finish_gui'](app, proof, [window, window] if ambiguous else [window])
                self.assertEqual(queued, [])
                self.assertFalse(report.exists())

    def test_windows_keeps_existing_report_marker_and_exit_behavior(self):
        with self.fixture(platform='win32') as fixture:
            namespace, app, window, proof, queued, destroyed, report, output, core = fixture
            original = dict(proof)
            namespace['finish_gui'](app, proof, [])
            self.assertEqual(json.loads(report.read_text()), original)
            self.assertIn('GUI_LANGUAGE_OK', output.getvalue())
            app.exit.assert_called_once_with(0)
            self.assertEqual(queued, [])
            window.close.assert_not_called()


class StepAcceptanceTests(unittest.TestCase):
    details = {'language': 'en', 'status': 'passed', 'requestClose': True,
               'closeAccepted': True, 'sessionClosed': True}

    def test_sigbus_timeout_or_missing_marker_still_fail_after_completed_gui_proof(self):
        for exit_code, timed_out, stdout in ((-10, False, 'GUI_LANGUAGE_OK'), (1, False, 'GUI_LANGUAGE_OK'),
                                            (0, True, 'GUI_LANGUAGE_OK'), (0, False, '')):
            with self.subTest(exit_code=exit_code, timed_out=timed_out, stdout=stdout):
                self.assertFalse(smoke.step_passed(exit_code, timed_out, stdout, 'GUI_LANGUAGE_OK',
                                                  gui_language='en', gui_details=self.details))

    def test_mac_requires_exact_language_status_and_all_three_true_lifecycle_flags(self):
        invalid = [None, [], {}, dict(self.details, language='zh_CN'), dict(self.details, status='closing')]
        for key in ('requestClose', 'closeAccepted', 'sessionClosed'):
            invalid.extend(dict(self.details, **{key: value}) for value in (None, False, 1))
        for details in invalid:
            with self.subTest(details=details):
                self.assertFalse(smoke.step_passed(0, False, 'GUI_LANGUAGE_OK', 'GUI_LANGUAGE_OK',
                                                  gui_language='en', gui_details=details))

    def test_non_mac_steps_retain_existing_exit_timeout_marker_contract(self):
        self.assertTrue(smoke.step_passed(0, False, '', ''))
        self.assertTrue(smoke.step_passed(0, False, 'GUI_LANGUAGE_OK', 'GUI_LANGUAGE_OK'))
        self.assertFalse(smoke.step_passed(-10, False, 'GUI_LANGUAGE_OK', 'GUI_LANGUAGE_OK'))


if __name__ == '__main__':
    unittest.main()
