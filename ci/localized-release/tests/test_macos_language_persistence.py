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


class SnapshotCloseContractTests(unittest.TestCase):
    """Generated snapshot callback protocol only; no native preferences or Qt."""

    @contextlib.contextmanager
    def fixture(self, *, accepted=True):
        queued, destroyed = [], []
        window = mock.Mock()
        window.objectName.return_value = 'rv-session000'
        window.isVisible.return_value = True
        window.testAttribute.return_value = True
        window.close.return_value = accepted
        window.destroyed.connect.side_effect = destroyed.append
        app = mock.Mock()
        app.topLevelWidgets.return_value = [window]
        app.quitOnLastWindowClosed.return_value = True
        app.organizationName.return_value, app.applicationName.return_value = 'fixture', 'OpenRV'
        settings = mock.Mock()
        settings.contains.return_value = True
        settings.value.return_value = 'en'
        settings.status.return_value = 0
        fsync = mock.Mock(wraps=os.fsync)
        core = types.SimpleNamespace(Qt=types.SimpleNamespace(WA_DeleteOnClose=1, WA_QuitOnClose=2),
                                     QTimer=types.SimpleNamespace(singleShot=lambda delay, callback: queued.append((delay, callback))),
                                     QSettings=types.SimpleNamespace(NoError=0))
        functions = [node for node in ast.parse(persistence.PROBE).body
                     if isinstance(node, ast.FunctionDef) and node.name == 'probe']
        with tempfile.TemporaryDirectory() as temporary, contextlib.redirect_stdout(io.StringIO()) as output, \
                contextlib.redirect_stderr(io.StringIO()):
            snapshot = Path(temporary) / 'snapshot.json'
            def mutation(key, value):
                fsync.assert_called_once()
                self.assertEqual(json.loads(snapshot.read_text())['value'], 'en')
                self.assertEqual((key, value), (persistence.KEY, 'system'))
            settings.setValue.side_effect = mutation
            namespace = {'probe_state': {'closing': False, 'finished': False, 'requestClose': False, 'closeAccepted': False},
                         'probe_deadline': 20, 'time': types.SimpleNamespace(monotonic=mock.Mock(return_value=0)),
                         'app': app, 'QtCore': core, 'json': json, 'traceback': traceback,
                         'config': {'snapshot': str(snapshot)}, 'os': types.SimpleNamespace(fsync=fsync),
                         'KEY': persistence.KEY, 'explicit_settings': mock.Mock(return_value=(settings, '/owned/fixture.plist'))}
            exec(compile(ast.Module(body=functions, type_ignores=[]), '<actual-snapshot-probe>', 'exec'), namespace)
            yield namespace, window, app, settings, queued, destroyed, snapshot, output

    def test_snapshot_fsync_and_mutation_precede_close_but_marker_waits_for_destruction(self):
        with self.fixture() as (namespace, window, app, settings, queued, destroyed, snapshot, output):
            namespace['probe']()
            self.assertEqual(len(queued), 1)
            self.assertEqual(queued[0][0], 0)
            settings.setValue.assert_called_once_with(persistence.KEY, 'system')
            original = snapshot.read_bytes()
            self.assertNotIn('MAC_PREFERENCE_SNAPSHOT_OK', output.getvalue())
            window.close.assert_not_called()
            queued[0][1]()
            self.assertNotIn('MAC_PREFERENCE_SNAPSHOT_OK', output.getvalue())
            destroyed[0]()
            self.assertIn('MAC_PREFERENCE_SNAPSHOT_OK', output.getvalue())
            self.assertEqual(snapshot.read_bytes(), original)
            app.exit.assert_not_called()

    def test_snapshot_close_rejection_fails_without_success_marker_and_preserves_snapshot(self):
        with self.fixture(accepted=False) as (namespace, window, app, settings, queued, destroyed, snapshot, output):
            namespace['probe']()
            queued[0][1]()
            destroyed[0]()
            app.exit.assert_called_once_with(1)
            self.assertNotIn('MAC_PREFERENCE_SNAPSHOT_OK', output.getvalue())
            self.assertTrue(snapshot.exists(), 'Recovery must retain its original snapshot')

    def test_waits_for_owned_window_only_within_original_budget_before_preferences_access(self):
        with self.fixture() as (namespace, window, app, settings, queued, destroyed, snapshot, output):
            app.topLevelWidgets.return_value = []
            namespace['probe']()
            self.assertEqual(queued[0][0], 200)
            namespace['explicit_settings'].assert_not_called()
            self.assertFalse(snapshot.exists())
            namespace['time'].monotonic.return_value = 21
            queued[0][1]()
            app.exit.assert_called_once_with(1)
            namespace['explicit_settings'].assert_not_called()


class NormalCloseContractTests(unittest.TestCase):
    """Exercise the generated callback protocol, without claiming native Qt execution."""

    @contextlib.contextmanager
    def fixture(self, *, accepted=True, auto_quit=True):
        callbacks, destroyed_callbacks = [], []
        window = mock.Mock()
        window.objectName.return_value = 'rv-session000'
        window.isVisible.return_value = True
        window.testAttribute.return_value = True
        window.close.return_value = accepted
        window.destroyed.connect.side_effect = destroyed_callbacks.append
        app = mock.Mock()
        app.quitOnLastWindowClosed.return_value = auto_quit
        def single_shot(delay, callback):
            self.assertEqual(delay, 0, 'Normal close must be queued without a timed delay')
            callbacks.append(callback)
        core = types.SimpleNamespace(Qt=types.SimpleNamespace(WA_DeleteOnClose=1, WA_QuitOnClose=2),
                                     QTimer=types.SimpleNamespace(singleShot=single_shot))
        functions = [node for node in ast.parse(persistence.gui_check_source()).body
                     if isinstance(node, ast.FunctionDef) and node.name == 'finish']
        self.assertEqual(len(functions), 1)
        with tempfile.TemporaryDirectory() as temporary, contextlib.redirect_stdout(io.StringIO()) as output:
            report = Path(temporary) / 'gui.json'
            namespace = {'state': {'finished': False, 'window': window, 'actions': [], 'menu': object()},
                         'result': {}, 'config': {'childReport': str(report)},
                         'app': app, 'QtCore': core, 'checkpoint': mock.Mock(),
                         'pathlib': __import__('pathlib'), 'json': json, 'traceback': traceback}
            exec(compile(ast.Module(body=functions, type_ignores=[]), '<actual-mac-finish>', 'exec'), namespace)
            yield namespace, window, app, callbacks, destroyed_callbacks, report, output

    def test_success_requires_real_close_acceptance_then_destruction_and_never_exits_directly(self):
        with self.fixture() as (namespace, window, app, callbacks, destroyed, report, output):
            namespace['finish'](0)
            namespace['finish'](0)  # Duplicate callbacks cannot request a second close.
            self.assertEqual(len(callbacks), 1)
            self.assertEqual(namespace['result']['status'], 'closing')
            self.assertFalse(report.exists())
            window.close.assert_not_called()
            callbacks.pop()()
            window.close.assert_called_once_with()
            self.assertTrue(namespace['result']['requestClose'])
            self.assertTrue(namespace['result']['closeAccepted'])
            self.assertFalse(namespace['result']['sessionClosed'])
            self.assertFalse(report.exists())
            self.assertNotIn('LANGUAGE_PERSISTENCE_STAGE_OK', output.getvalue())
            destroyed[0]()
            details = json.loads(report.read_text(encoding='utf-8'))
            self.assertEqual(details['status'], 'passed')
            self.assertTrue(all(details[key] is True for key in ('requestClose', 'closeAccepted', 'sessionClosed')))
            self.assertNotIn('window', namespace['state'])
            self.assertIn('LANGUAGE_PERSISTENCE_STAGE_OK', output.getvalue())
            app.exit.assert_not_called()

    def test_rejected_or_premature_close_cannot_become_success(self):
        for premature in (False, True):
            with self.subTest(premature=premature), self.fixture(accepted=False) as fixture:
                namespace, window, app, callbacks, destroyed, report, output = fixture
                namespace['finish'](0)
                if premature:
                    destroyed[0]()
                callbacks[0]()
                details = json.loads(report.read_text(encoding='utf-8'))
                self.assertEqual(details['status'], 'failed')
                self.assertNotIn('LANGUAGE_PERSISTENCE_STAGE_OK', output.getvalue())
                app.exit.assert_called_once_with(1)

    def test_disabled_normal_auto_quit_fails_instead_of_forcing_successful_exit(self):
        with self.fixture(auto_quit=False) as (namespace, window, app, callbacks, destroyed, report, output):
            namespace['finish'](0)
            self.assertEqual(callbacks, [])
            self.assertEqual(json.loads(report.read_text())['status'], 'failed')
            app.exit.assert_called_once_with(1)

    def test_explicit_failure_keeps_nonzero_exit_and_never_requests_close(self):
        with self.fixture() as (namespace, window, app, callbacks, destroyed, report, output):
            namespace['finish'](2, 'fixture failure')
            self.assertEqual(callbacks, [])
            self.assertEqual(json.loads(report.read_text())['error'], 'fixture failure')
            app.exit.assert_called_once_with(2)
            window.close.assert_not_called()


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
                for phase in ('boot', 'ready', 'before-action', 'after-action', 'request-close', 'finish'):
                    namespace['checkpoint'](phase)
                    entry = json.loads(path.read_text(encoding='utf-8'))
                    self.assertEqual(entry, {'stage': config['stage'], 'phase': phase, 'time': 1234.567})
            self.assertEqual(len(output.getvalue().splitlines()), 6)
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
                'details': {'status': 'passed', 'hostedRunnerPreferencesVerifiedBeforeAction': True,
                            'requestClose': True, 'closeAccepted': True, 'sessionClosed': True},
                'progress': {'phase': 'finish'}}
        self.assertTrue(eval(code, dict(base)))
        cases = [dict(details=None), dict(details={}), dict(stdout=''),
                 dict(result={'timedOut': True, 'exitCode': 0}), dict(result={'timedOut': False, 'exitCode': 1}),
                 dict(result={'timedOut': False, 'exitCode': -10})]
        for case in cases:
            with self.subTest(case=case):
                self.assertFalse(eval(code, dict(base, **case)))
        for key in ('requestClose', 'closeAccepted', 'sessionClosed'):
            for invalid in (False, None, 1):
                with self.subTest(key=key, invalid=invalid):
                    details = dict(base['details'], **{key: invalid})
                    self.assertFalse(eval(code, dict(base, details=details)))

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
