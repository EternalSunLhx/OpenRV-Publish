"""Offline callback/proof tests for the separate opt-in package test task."""
import ast
import contextlib
import hashlib
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

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import post_package_smoke as smoke


class Signal:
    def __init__(self): self.callbacks = []
    def connect(self, callback): self.callbacks.append(callback)
    def emit(self, *values):
        for callback in list(self.callbacks): callback(*values)


class Menu:
    def __init__(self, path, actions): self.path, self.items, self.aboutToShow = path, actions, Signal()
    def property(self, name): return self.path if name == 'rvSourceMenuPath' else None
    def actions(self): return self.items


class Bar(Menu): pass


class Action:
    NoRole, PreferencesRole = 'none', 'preferences'
    def __init__(self, source, *, menu=None, callback=lambda: None, role=NoRole):
        self.source, self.submenu, self.callback, self.role = source, menu, callback, role
    def property(self, name): return self.source if name == 'rvSourceText' else None
    def menu(self): return self.submenu
    def isEnabled(self): return True
    def menuRole(self): return self.role
    def trigger(self): self.callback()


class CallbackTests(unittest.TestCase):
    @contextlib.contextmanager
    def fixture(self):
        queued, windows, now, angle, playing = [], [], [0.0], [0.0], [False]
        app, core, commands = mock.Mock(), mock.Mock(), mock.Mock()
        app.quitOnLastWindowClosed.return_value = True
        core.Qt.WA_DeleteOnClose, core.Qt.WA_QuitOnClose = 1, 2
        core.QTimer.singleShot.side_effect = lambda delay, callback: queued.append((delay, callback))
        def window(name):
            value = mock.Mock()
            value.objectName.return_value = name
            value.metaObject.return_value.className.return_value = 'Rv::RvDocument'
            value.isVisible.return_value = True
            value.testAttribute.return_value = True
            value.destroyed = Signal()
            def close():
                def destroy():
                    value.isVisible.return_value = False
                    value.destroyed.emit()
                queued.append((0, destroy))
                return True
            value.close.side_effect = close
            windows.append(value)
            return value
        first = window('rv-session000')
        app.topLevelWidgets.side_effect = lambda: windows
        rotation = Action('180', callback=lambda: angle.__setitem__(0, 180.0))
        rotation_menu = Menu(['Image', 'Rotation'], [rotation])
        image_menu = Menu(['Image'], [Action('Rotation', menu=rotation_menu)])
        new_session = Action('New Session', callback=lambda: window('rv-session001'))
        file_menu = Menu(['File'], [new_session])
        bar = Bar([], [Action('Image', menu=image_menu), Action('File', menu=file_menu)])
        app.allWidgets.return_value = [bar]
        commands.PlayLoop = 0
        commands.getFloatProperty.side_effect = lambda prop: [angle[0]]
        commands.frameStart.return_value = 1
        commands.frame.side_effect = lambda: 1 + int(now[0] * 24) % 24
        commands.stop.side_effect = lambda: playing.__setitem__(0, False)
        commands.play.side_effect = lambda: playing.__setitem__(0, True)
        commands.isPlaying.side_effect = lambda: playing[0]
        functions = [node for node in ast.parse(smoke.GUI_CASE).body if isinstance(node, ast.FunctionDef)]
        with tempfile.TemporaryDirectory() as temporary, contextlib.redirect_stdout(io.StringIO()) as output, \
                contextlib.redirect_stderr(io.StringIO()), mock.patch.dict(os.environ, ORV_POST_REPORT=str(Path(temporary) / 'gui.json')):
            proof = {'case': 'rotation-playback', 'language': 'en', 'status': 'running', 'mp4SourceVerified': True,
                     'requestClose': False, 'closeAccepted': False, 'sessionClosed': False}
            namespace = {'app': app, 'QtCore': core, 'QtGui': types.SimpleNamespace(QAction=Action),
                         'QtWidgets': types.SimpleNamespace(QMenuBar=Bar, QMenu=Menu),
                         'commands': commands, 'proof': proof, 'state': {'finished': False, 'closing': False},
                         'time': types.SimpleNamespace(monotonic=lambda: now[0]), 'deadline': 30,
                         'os': os, 'json': json, 'traceback': traceback}
            exec(compile(ast.Module(body=functions, type_ignores=[]), '<actual-post-package-callbacks>', 'exec'), namespace)
            def drain():
                for _ in range(100):
                    if not queued: return
                    delay, callback = queued.pop(0); now[0] += max(delay / 1000, 0.001); callback()
                raise AssertionError('Callback test exceeded its event bound')
            yield types.SimpleNamespace(ns=namespace, app=app, core=core, commands=commands, window=first,
                                        windows=windows, rotation=rotation, rotation_menu=rotation_menu,
                                        angle=angle, queued=queued, now=now, proof=proof, drain=drain,
                                        output=output, report=Path(os.environ['ORV_POST_REPORT']), new_session=new_session)

    def test_actual_rotation_action_then_real_playback_and_normal_close_protocol(self):
        with self.fixture() as f:
            f.ns['rotation_playback'](f.window)
            self.assertEqual(f.angle[0], 180)
            f.commands.play.assert_called_once_with()
            f.window.close.assert_not_called()
            f.drain()
            self.assertGreaterEqual(f.now[0], 2)
            self.assertTrue(smoke.case_passed(0, False, f.output.getvalue(), f.proof, 'rotation-playback', 'en'))
            f.window.close.assert_called_once_with()
            f.commands.setFloatProperty.assert_not_called()
            f.commands.setFrame.assert_called_once_with(1)  # Playback samples never manually advance it.
            f.core.QCoreApplication.exit.assert_not_called()

    def test_wrong_menu_route_or_callback_cannot_pass(self):
        for fault in ('path', 'caption', 'noop', 'closes-session'):
            with self.subTest(fault=fault), self.fixture() as f:
                if fault == 'path': f.rotation_menu.path = ['File', 'Rotation']
                elif fault == 'caption': f.rotation.source = 'Close Session'
                elif fault == 'noop': f.rotation.callback = lambda: None
                if fault == 'closes-session':
                    def close_instead():
                        f.angle[0] = 180; f.window.isVisible.return_value = False
                    f.rotation.callback = close_instead
                with self.assertRaises(AssertionError): f.ns['rotation_playback'](f.window)
                self.assertNotIn(smoke.MARKER, f.output.getvalue())
                f.window.close.assert_not_called()

    def test_frozen_or_stopped_playback_fails_without_success_marker(self):
        for fault in ('frozen', 'stopped'):
            with self.subTest(fault=fault), self.fixture() as f:
                if fault == 'frozen': f.commands.frame.side_effect = lambda: 1
                else: f.commands.isPlaying.side_effect = lambda: False
                f.ns['rotation_playback'](f.window); f.drain()
                self.assertEqual(f.proof['status'], 'failed')
                self.assertNotIn(smoke.MARKER, f.output.getvalue())
                f.core.QCoreApplication.exit.assert_called_once_with(1)
                f.window.close.assert_not_called()

    def test_session_loss_after_action_or_deadline_is_failure(self):
        for fault in ('session-disappears', 'deadline'):
            with self.subTest(fault=fault), self.fixture() as f:
                f.ns['rotation_playback'](f.window)
                if fault == 'session-disappears': f.window.isVisible.return_value = False
                else: f.now[0] = 31
                f.drain()
                self.assertEqual(f.proof['status'], 'failed')
                self.assertNotIn(smoke.MARKER, f.output.getvalue())
                f.core.QCoreApplication.exit.assert_called_once_with(1)

    def test_premature_or_rejected_last_close_never_reports_success(self):
        for premature in (False, True):
            with self.subTest(premature=premature), self.fixture() as f:
                f.window.close.side_effect = lambda: False
                f.ns['finish'](f.window)
                if premature: f.window.destroyed.emit()
                f.drain()
                self.assertEqual(f.proof['status'], 'failed')
                self.assertNotIn(smoke.MARKER, f.output.getvalue())

    def test_preferences_owner_dies_before_final_session_close(self):
        with self.fixture() as f:
            f.proof['case'] = 'preferences-owner-close'
            prefs = mock.Mock(); prefs.destroyed = Signal(); prefs.parent.return_value = f.window
            prefs.objectName.return_value = 'preferences'
            prefs.metaObject.return_value.className.return_value = 'Rv::RvPreferences'
            prefs.isVisible.return_value = False
            prefs.close.side_effect = lambda: (setattr(prefs.isVisible, 'return_value', False) or True)
            pref_action = Action('Preferences...', role=Action.PreferencesRole,
                                 callback=lambda: setattr(prefs.isVisible, 'return_value', True))
            f.app.findChildren.return_value = [pref_action]
            f.windows.append(prefs)
            f.window.destroyed.connect(lambda: prefs.destroyed.emit())
            f.ns['preferences_owner_close'](f.window)
            self.assertFalse(f.proof['preferences']['ownerDestroyed'])
            f.drain()
            self.assertTrue(smoke.case_passed(0, False, f.output.getvalue(), f.proof, 'preferences-owner-close', 'en'))
            self.assertTrue(f.proof['preferences']['preferencesDestroyed'])
            prefs.deleteLater.assert_not_called()
            f.core.QCoreApplication.exit.assert_not_called()


class AcceptanceTests(unittest.TestCase):
    def proof(self, case='rotation-playback'):
        return {'case': case, 'language': 'en', 'status': 'passed', 'mp4SourceVerified': True,
                'requestClose': True, 'closeAccepted': True, 'sessionClosed': True,
                'rotation': {'menuPath': 'Image/Rotation/180', 'initialAngle': 0, 'angle': 180,
                             'sessionAlive': True, 'playbackObserved': True, 'playbackSeconds': 2.1, 'sampledFrames': [1, 4, 8]},
                'preferences': {'ownerDestroyed': True, 'preferencesDestroyed': True}}

    def test_native_crash_or_timeout_remains_failure_even_with_complete_proof(self):
        for code, timeout, stdout in [(-11, False, smoke.MARKER), (-10, False, smoke.MARKER),
                                      (1, False, smoke.MARKER), (0, True, smoke.MARKER), (0, False, '')]:
            self.assertFalse(smoke.case_passed(code, timeout, stdout, self.proof(), 'rotation-playback', 'en'))

    def test_incomplete_rotation_or_close_proof_fails(self):
        for field in ('mp4SourceVerified', 'requestClose', 'closeAccepted', 'sessionClosed'):
            for value in (False, None, 1):
                proof = self.proof(); proof[field] = value
                self.assertFalse(smoke.case_passed(0, False, smoke.MARKER, proof, 'rotation-playback', 'en'))
        for field, value in [('menuPath', 'File/Close Session'), ('initialAngle', 90), ('angle', 90),
                             ('sessionAlive', False), ('playbackObserved', False), ('playbackSeconds', 0.5),
                             ('sampledFrames', [1, 1, 1]),
                             ('sampledFrames', [1, 2, True])]:
            proof = self.proof(); proof['rotation'][field] = value
            self.assertFalse(smoke.case_passed(0, False, smoke.MARKER, proof, 'rotation-playback', 'en'))

    def test_preferences_requires_real_owner_and_dialog_destruction(self):
        for field in ('ownerDestroyed', 'preferencesDestroyed'):
            proof = self.proof('preferences-owner-close'); proof['preferences'][field] = False
            self.assertFalse(smoke.case_passed(0, False, smoke.MARKER, proof, 'preferences-owner-close', 'en'))

    def test_user_computers_and_unrecognized_runners_are_refused(self):
        base = {'GITHUB_ACTIONS': 'true', 'RUNNER_ENVIRONMENT': 'github-hosted', 'RUNNER_OS': 'macOS'}
        with mock.patch.object(smoke.sys, 'platform', 'darwin'), mock.patch.dict(os.environ, base, clear=True):
            smoke.require_hosted_runner()
            for key in base:
                with mock.patch.dict(os.environ, {key: ''}), self.assertRaises(ValueError): smoke.require_hosted_runner()
        with mock.patch.object(smoke.sys, 'platform', 'win32'), \
                mock.patch.dict(os.environ, dict(base, RUNNER_OS='Windows'), clear=True):
            smoke.require_hosted_runner()


class WindowsProviderTests(unittest.TestCase):
    def test_only_pinned_external_provider_is_forwarded_after_qt_cleanup(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); runtime = root / 'runtime'; runtime.mkdir()
            provider = root / 'private-provider/opengl32.dll'; provider.parent.mkdir()
            payload = b'bounded test provider'; provider.write_bytes(payload)
            digest = hashlib.sha256(payload).hexdigest()
            environment = {'RUNNER_TEMP': str(root), 'OPENRV_CI_GL_PROVIDER': str(provider), 'OPENRV_CI_GL_SHA256': digest}
            with mock.patch.object(smoke.sys, 'platform', 'win32'), \
                    mock.patch.object(smoke, 'GL_PROVIDER_SHA256', digest), mock.patch.object(smoke, 'GL_PROVIDER_SIZE', len(payload)), \
                    mock.patch.dict(os.environ, environment, clear=True):
                self.assertEqual(smoke.windows_gl_settings(runtime), {'QT_OPENGL': 'desktop', 'QT_OPENGL_DLL': str(provider.resolve())})
                for fault in ('missing', 'digest', 'bytes', 'inside-runtime', 'outside-temp'):
                    with self.subTest(fault=fault), mock.patch.dict(os.environ, environment, clear=True):
                        provider.write_bytes(payload)
                        if fault == 'missing': os.environ.pop('OPENRV_CI_GL_PROVIDER')
                        elif fault == 'digest': os.environ['OPENRV_CI_GL_SHA256'] = '0' * 64
                        elif fault == 'bytes': provider.write_bytes(b'X' * len(payload))
                        elif fault == 'inside-runtime':
                            inside = runtime / 'opengl32.dll'; inside.write_bytes(payload)
                            os.environ['OPENRV_CI_GL_PROVIDER'] = str(inside)
                        else: os.environ['RUNNER_TEMP'] = str(root / 'other')
                        with self.assertRaises(ValueError): smoke.windows_gl_settings(runtime)

    def test_mac_does_not_request_a_windows_provider(self):
        with mock.patch.object(smoke.sys, 'platform', 'darwin'), mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(smoke.windows_gl_settings(Path('/owned/runtime')), {})


if __name__ == '__main__':
    unittest.main()
