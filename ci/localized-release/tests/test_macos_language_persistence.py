"""Offline refusal/recovery checks; these never access native preferences."""
from __future__ import annotations

import contextlib
import ast
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import traceback
import types
import unittest
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[3] / 'tests/localization'
sys.path.insert(0, str(SCRIPTS))
try:
    import macos_language_persistence as persistence
finally:
    sys.path.remove(str(SCRIPTS))


class HostRefusalTests(unittest.TestCase):
    def test_all_host_markers_and_native_platform_are_required(self):
        valid = dict(persistence.HOST_MARKERS)
        persistence.require_hosted_macos('darwin', valid)
        for platform in ('win32', 'linux'):
            with self.assertRaises(RuntimeError):
                persistence.require_hosted_macos(platform, valid)
        for key in valid:
            missing = dict(valid)
            missing.pop(key)
            with self.subTest(key=key), self.assertRaises(RuntimeError):
                persistence.require_hosted_macos('darwin', missing)

    def test_local_mac_refuses_before_directory_or_process(self):
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary) / 'must-not-exist'
            with mock.patch.object(persistence.sys, 'platform', 'darwin'), mock.patch.dict(os.environ, {}, clear=True), \
                    mock.patch.object(persistence.subprocess, 'Popen') as popen, contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as exit_result:
                    persistence.main(['--runtime', str(Path(temporary) / 'RV.app'), '--work', str(work)])
                self.assertEqual(exit_result.exception.code, 2)
                self.assertFalse(work.exists())
                popen.assert_not_called()

    def test_embedded_guards_refuse_before_qsettings(self):
        qt = types.ModuleType('PySide6')
        qt.QtCore = types.SimpleNamespace(QSettings=mock.Mock())
        with mock.patch.dict(sys.modules, {'PySide6': qt}), \
                mock.patch.object(sys, 'platform', 'darwin'), mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(AssertionError):
                exec(persistence.RESTORE, {})
        qt.QtCore.QSettings.assert_not_called()

    def test_all_generated_embedded_programs_compile(self):
        for name, code in (('probe', persistence.PROBE), ('restore', persistence.RESTORE), ('gui', persistence.gui_check_source())):
            compile(code, name, 'exec')
        self.assertNotIn('window.findChildren(QtWidgets.QMenuBar)', persistence.gui_check_source())


class RecoveryContractTests(unittest.TestCase):
    def run_recovery(self, present, original, wrong_file=False):
        # A fake Qt store verifies restoration semantics only, not CFPreferences itself.
        state = {persistence.KEY: 'zh_CN', 'Other/retained': 'untouched'}
        writes = []
        home = Path.home()
        filename = str(home / 'Library/Preferences/com.fixture.OpenRV.plist')

        class FakeSettings:
            NativeFormat, UserScope, NoError = 1, 2, 0

            def __init__(self, format, scope, organization, application):
                self.format, self.scope = format, scope
                if (format, scope, organization, application) != (1, 2, 'fixture', 'OpenRV'):
                    raise AssertionError('Unexpected preference domain')

            def setFallbacksEnabled(self, enabled):
                if enabled:
                    raise AssertionError('Fallbacks must stay disabled')

            def sync(self):
                pass

            def status(self):
                return self.NoError

            def fileName(self):
                return filename

            def contains(self, key):
                return key in state

            def value(self, key):
                return state[key]

            def setValue(self, key, value):
                writes.append(('set', key))
                state[key] = value

            def remove(self, key):
                writes.append(('remove', key))
                state.pop(key, None)

        qt = types.ModuleType('PySide6')
        existing_app = object()
        core_application = mock.Mock(side_effect=AssertionError('py-interp already owns the Qt application'))
        core_application.instance.return_value = existing_app
        qt.QtCore = types.SimpleNamespace(QSettings=FakeSettings, QCoreApplication=core_application)
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            snapshot = folder / 'snapshot.json'
            snapshot.write_text(json.dumps({'schemaVersion': 1, 'organization': 'fixture', 'application': 'OpenRV',
                                             'settingsFile': filename + '.wrong' if wrong_file else filename,
                                             'present': present, 'value': original}), encoding='utf-8')
            config = folder / 'config.json'
            environment = dict(persistence.HOST_MARKERS, ORV_MAC_PERSIST_CONFIG=str(config))
            with mock.patch.dict(sys.modules, {'PySide6': qt}), mock.patch.object(sys, 'platform', 'darwin'), \
                    mock.patch.dict(os.environ, environment, clear=True), mock.patch.object(Path, 'home', return_value=home), \
                    contextlib.redirect_stdout(io.StringIO()):
                for operation in ('restore', 'verify'):
                    config.write_text(json.dumps({'snapshot': str(snapshot), 'operation': operation}), encoding='utf-8')
                    if wrong_file:
                        with self.assertRaises(AssertionError):
                            exec(persistence.RESTORE, {})
                        break
                    exec(persistence.RESTORE, {})  # A new QSettings instance on each operation.
        core_application.assert_not_called()
        core_application.instance.assert_called()
        return state, writes

    def test_restores_existing_language_without_touching_other_keys(self):
        state, writes = self.run_recovery(True, 'en')
        self.assertEqual(state, {persistence.KEY: 'en', 'Other/retained': 'untouched'})
        self.assertEqual(writes, [('set', persistence.KEY)])

    def test_restores_original_absence(self):
        state, writes = self.run_recovery(False, None)
        self.assertEqual(state, {'Other/retained': 'untouched'})
        self.assertEqual(writes, [('remove', persistence.KEY)])

    def test_refuses_a_different_preference_file_before_write(self):
        state, writes = self.run_recovery(True, 'en', wrong_file=True)
        self.assertEqual(state[persistence.KEY], 'zh_CN')
        self.assertEqual(writes, [])


class RestartNoteMatcherTests(unittest.TestCase):
    body = ('Language preference saved. Save your work, then restart OpenRV to apply it.\n'
            '\u754c\u9762\u8bed\u8a00\u8bbe\u7f6e\u5df2\u4fdd\u5b58\u3002\u8bf7\u5148\u4fdd\u5b58\u5de5\u4f5c\uff0c\u518d\u91cd\u65b0\u542f\u52a8 OpenRV \u4f7f\u5176\u751f\u6548\u3002')
    suffix = ('\n\nRV_UI_LANGUAGE overrides this preference until it is removed.\n'
              '\u5f53\u524d RV_UI_LANGUAGE \u73af\u5883\u53d8\u91cf\u4f18\u5148\u4e8e\u6b64\u8bbe\u7f6e\uff1b\u79fb\u9664\u8be5\u53d8\u91cf\u540e\u624d\u4f1a\u4f7f\u7528\u4fdd\u5b58\u7684\u9009\u62e9\u3002')

    def invoke_callback(self, *, windows=False, override=False, title='', body=None,
                        owner=True, visible=True, icon=1, buttons=1, enabled=True,
                        missing_button=False, message_box=True, custom_button=False):
        menu = types.SimpleNamespace(parent=lambda: None)
        other_owner = types.SimpleNamespace(parent=lambda: None)
        timer = mock.Mock()

        class Button:
            clicks = 0
            def isEnabled(self):
                return enabled
            def click(self):
                self.clicks += 1

        button = Button()

        class MessageBox:
            Information, Warning, Ok, Cancel = 1, 2, 1, 2
            accepts = 0
            def parent(self):
                return menu if owner else other_owner
            def isVisible(self):
                return visible
            def windowTitle(self):
                return title
            def text(self):
                return body if body is not None else self_body
            def icon(self):
                return icon
            def standardButtons(self):
                return buttons
            def button(self, standard):
                self.asserted_standard = standard
                return None if missing_button else button
            def buttons(self):
                return [button, object()] if custom_button else [button]
            def accept(self):
                self.accepts += 1

        self_body = self.body + (self.suffix if override else '')
        dialog = MessageBox()
        state = {'menu': menu, 'closeTimer': timer, 'restartNote': None}
        failures = []
        source = persistence.WINDOWS_GUI_CHECK if windows else persistence.gui_check_source()
        functions = [node for node in ast.parse(source).body if isinstance(node, ast.FunctionDef)
                     and node.name in {'belongs_to_language_menu', 'close_owned_restart_note'}]
        self.assertEqual(len(functions), 2)
        namespace = {'app': types.SimpleNamespace(topLevelWidgets=lambda: [dialog if message_box else object()]),
                     'QtWidgets': types.SimpleNamespace(QMessageBox=MessageBox), 'state': state,
                     'config': {'overrideLanguage': 'zh_CN'} if override else {},
                     'traceback': traceback, 'finish': lambda *args: failures.append(args)}
        exec(compile(ast.Module(body=functions, type_ignores=[]), '<actual-restart-callback>', 'exec'), namespace)
        namespace['close_owned_restart_note']()
        self.assertEqual(failures, [])
        return state, dialog, button, timer

    def test_mac_empty_title_clicks_actual_ok_for_both_exact_bodies(self):
        for override in (False, True):
            with self.subTest(override=override):
                state, dialog, button, timer = self.invoke_callback(override=override)
                self.assertEqual(button.clicks, 1)
                self.assertEqual(dialog.accepts, 0)
                self.assertEqual(state['restartNote']['title'], '')
                self.assertEqual(state['restartNote']['text'], self.body + (self.suffix if override else ''))
                self.assertTrue(state['restartNote']['closedByTest'])
                timer.stop.assert_called_once_with()

    def test_mac_refuses_unrelated_or_different_dialogs(self):
        cases = [dict(owner=False), dict(body=self.body + ' extra'), dict(body=self.body + self.suffix),
                 dict(override=True, body=self.body), dict(icon=2), dict(buttons=3), dict(buttons=2),
                 dict(enabled=False), dict(missing_button=True), dict(visible=False), dict(message_box=False),
                 dict(custom_button=True)]
        for case in cases:
            with self.subTest(case=case):
                state, dialog, button, timer = self.invoke_callback(**case)
                self.assertEqual(button.clicks, 0)
                self.assertEqual(dialog.accepts, 0)
                self.assertIsNone(state['restartNote'])
                timer.stop.assert_not_called()

    def test_windows_original_title_requirement_and_accept_are_unchanged(self):
        for title, expected in [('', 0), ('Restart required / \u9700\u8981\u91cd\u65b0\u542f\u52a8', 1)]:
            with self.subTest(title=title):
                state, dialog, button, timer = self.invoke_callback(windows=True, title=title)
                self.assertEqual(dialog.accepts, expected)
                self.assertEqual(button.clicks, 0)
                self.assertEqual(state['restartNote'] is not None, bool(expected))


class ProgressContractTests(unittest.TestCase):
    def test_actual_checkpoint_contains_only_stage_phase_time(self):
        source = persistence.gui_check_source()
        function = next(node for node in ast.parse(source).body
                        if isinstance(node, ast.FunctionDef) and node.name == 'checkpoint')
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'progress.json'
            config = {'stage': '01-chinese-select-english', 'progressReport': str(path)}
            namespace = {'config': config, 'json': json, 'pathlib': __import__('pathlib'),
                         'os': os, 'time': types.SimpleNamespace(time=lambda: 1234.567)}
            exec(compile(ast.Module(body=[function], type_ignores=[]), '<actual-checkpoint>', 'exec'), namespace)
            with contextlib.redirect_stdout(io.StringIO()) as output:
                for phase in ('boot', 'ready', 'before-action', 'after-action', 'finish'):
                    namespace['checkpoint'](phase)
                    entry = json.loads(path.read_text(encoding='utf-8'))
                    self.assertEqual(entry, {'stage': config['stage'], 'phase': phase, 'time': 1234.567})
            self.assertEqual(len(output.getvalue().splitlines()), 5)
            self.assertFalse(path.with_name('progress.json.tmp').exists())
            self.assertEqual(persistence.partial_progress(path, config['stage'])['phase'], 'finish')
            self.assertIsNone(persistence.partial_progress(path, 'different-stage'))

    def test_progress_cannot_replace_the_original_final_proof(self):
        tree = ast.parse(Path(persistence.__file__).read_text(encoding='utf-8'))
        expression = next(node.value for node in ast.walk(tree)
                          if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'passed' for t in node.targets)
                          and 'details' in ast.dump(node.value))
        code = compile(ast.Expression(expression), '<actual-parent-pass-condition>', 'eval')
        base = {'result': {'timedOut': False, 'exitCode': 0}, 'stdout': 'LANGUAGE_PERSISTENCE_STAGE_OK',
                'details': {'status': 'passed', 'hostedRunnerPreferencesVerifiedBeforeAction': True},
                'progress': {'phase': 'finish'}}
        self.assertTrue(eval(code, dict(base)))
        cases = [dict(details=None), dict(details={}), dict(stdout=''),
                 dict(result={'timedOut': True, 'exitCode': 0}), dict(result={'timedOut': False, 'exitCode': 1})]
        for case in cases:
            with self.subTest(case=case):
                self.assertFalse(eval(code, dict(base, **case)))

    def test_original_child_deadlines_and_new_outer_budget(self):
        source = persistence.gui_check_source()
        self.assertIn('deadline = time.monotonic() + 30', source)
        self.assertIn('QtCore.QTimer.singleShot(35000, watchdog)', source)
        tree = ast.parse(Path(persistence.__file__).read_text(encoding='utf-8'))
        budgets = [node.value for node in ast.walk(tree) if isinstance(node, ast.keyword)
                   and node.arg == 'timeout' and isinstance(node.value, ast.IfExp)]
        self.assertEqual(len(budgets), 1)
        self.assertEqual(ast.literal_eval(budgets[0].body), 90)
        self.assertEqual(ast.literal_eval(budgets[0].orelse), 25)


if __name__ == '__main__':
    unittest.main()
