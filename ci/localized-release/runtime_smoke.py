"""Bounded native runtime checks. Writes only into a caller-selected work directory."""
from __future__ import annotations

import argparse
import datetime
import json
import os
import pathlib
import signal
import subprocess
import sys
import time

PYTHON_SMOKE = r'''
import json, os, pathlib
import rv, PySide6, numpy as np, PyOpenColorIO as ocio
from PySide6 import QtCore, QtGui
root = pathlib.Path(os.environ['ORV_RUNTIME']).resolve()
for module in (rv, PySide6, np, ocio):
    assert pathlib.Path(module.__file__).resolve().is_relative_to(root), module.__file__
config = ocio.Config.CreateRaw()
transform = ocio.MatrixTransform()
transform.setMatrix([2,0,0,0, 0,2,0,0, 0,0,2,0, 0,0,0,1])
cpu = config.getProcessor(transform).getDefaultCPUProcessor()
assert np.allclose(cpu.applyRGB([0.5,0.25,1]), [1,0.5,2], atol=1e-6)
image = QtGui.QImage(32, 32, QtGui.QImage.Format_RGB888)
for y in range(32):
    for x in range(32): image.setPixelColor(x, y, QtGui.QColor(x*255//31, y*255//31, (x+y)*255//62))
assert image.save(os.environ['ORV_INPUT'])
print('PYTHON_OCIO_OK', flush=True)
'''

DIMENSION_SMOKE = r'''
import os, pathlib, struct
from PySide6 import QtGui
for name in ('ORV_INPUT', 'ORV_ROUNDTRIP'):
    image = QtGui.QImage(os.environ[name])
    assert not image.isNull() and image.size().width() == image.size().height() == 32
# Parse the standardized EXR dataWindow attribute; this checks the real RVIO output.
with open(os.environ['ORV_EXR'], 'rb') as stream:
    assert stream.read(4) == b'\x76\x2f\x31\x01'
    stream.read(4)
    def string():
        data = bytearray()
        while True:
            value = stream.read(1)
            if not value: raise EOFError('Unexpected EXR end')
            if value == b'\0': return bytes(data)
            data += value
    found = False
    while True:
        name = string()
        if not name: break
        typ = string()
        size = struct.unpack('<I', stream.read(4))[0]
        data = stream.read(size)
        if name == b'dataWindow':
            xmin, ymin, xmax, ymax = struct.unpack('<4i', data)
            assert xmax-xmin+1 == ymax-ymin+1 == 32
            found = True
    assert found
print('ROUNDTRIP_OK', flush=True)
'''

GUI_SMOKE = r'''
import json, os, pathlib, runpy, sys, time, traceback
from PySide6 import QtCore, QtWidgets
from rv import commands
deadline = time.monotonic() + 20
gui_state = {'closing': False, 'finished': False}

def finish_gui(app, proof, windows):
    if sys.platform != 'darwin':
        with open(os.environ['ORV_GUI_REPORT'], 'w', encoding='utf-8') as stream:
            json.dump(proof, stream, ensure_ascii=False)
        print('GUI_LANGUAGE_OK', flush=True)
        app.exit(0)
        return
    if gui_state['closing'] or gui_state['finished']:
        return
    sessions = [window for window in windows if window.objectName().startswith('rv-session')]
    assert len(sessions) == 1, 'Expected exactly one owned visible RV session'
    window = sessions[0]
    assert window.isVisible() and window.testAttribute(QtCore.Qt.WA_DeleteOnClose)
    assert window.testAttribute(QtCore.Qt.WA_QuitOnClose) and app.quitOnLastWindowClosed()
    gui_state['closing'] = True
    proof.update(status='closing', requestClose=False, closeAccepted=False, sessionClosed=False)

    def fail_close():
        gui_state['finished'] = True
        traceback.print_exc()
        QtCore.QCoreApplication.exit(1)

    def session_closed(*unused):
        if gui_state['finished']:
            return
        try:
            assert proof['requestClose'] and proof['closeAccepted'], 'Session closed before its owned close request completed'
            proof.update(status='passed', sessionClosed=True)
            with open(os.environ['ORV_GUI_REPORT'], 'w', encoding='utf-8') as stream:
                json.dump(proof, stream, ensure_ascii=False)
            print('GUI_LANGUAGE_OK', flush=True)
            gui_state['finished'] = True
        except BaseException:
            fail_close()

    def request_close():
        if gui_state['finished']:
            return
        try:
            proof['requestClose'] = True
            # Same RvDocument.close() used by RvApplication::quitAll. Keep the
            # event loop running through normal session/GL/menu destruction.
            assert window.close(), 'RV rejected the owned session close request'
            proof['closeAccepted'] = True
        except BaseException:
            fail_close()

    window.destroyed.connect(session_closed)
    # Unwind the menu/HUD check before requesting close; no timed sleep.
    QtCore.QTimer.singleShot(0, request_close)

def check():
    if gui_state['closing'] or gui_state['finished']:
        return
    try:
        app = QtWidgets.QApplication.instance()
        expected = os.environ['RV_UI_LANGUAGE']
        actual = app.property('rvInterfaceLanguage')
        translated = QtCore.QCoreApplication.translate('RvMenu', 'File')
        assert actual == expected, (expected, actual)
        overlay_font = app.property('rvOverlayFontFile') or ''
        if expected == 'zh_CN':
            # Native selection has already validated FreeType face-zero Unicode
            # glyph coverage. A translated caption alone cannot establish this.
            assert overlay_font and pathlib.Path(overlay_font).is_file(), ('Chinese HUD font unavailable', overlay_font)
        else:
            assert not overlay_font, ('English HUD must retain the embedded default font', overlay_font)
        qt_base_loaded = app.findChild(QtCore.QTranslator, 'rvQtBaseTranslator') is not None
        assert qt_base_loaded == (expected == 'zh_CN'), (expected, qt_base_loaded)
        assert translated == ('\u6587\u4ef6' if expected == 'zh_CN' else 'File'), translated
        commands.redraw()
        sources = commands.nodesOfType('RVFileSource')
        windows = [window for window in app.topLevelWidgets() if window.isVisible()]
        # macOS uses RvApplication's parentless native menu bar.
        bars = ([widget for widget in app.allWidgets() if isinstance(widget, QtWidgets.QMenuBar)]
                if sys.platform == 'darwin' else
                [bar for window in windows for bar in window.findChildren(QtWidgets.QMenuBar)])
        actions = [action for bar in bars for action in bar.actions()
                   if action.property('rvSourceText') == 'File']
        language_menus = [widget for widget in app.allWidgets()
                          if isinstance(widget, QtWidgets.QMenu) and widget.objectName() == 'rvInterfaceLanguageMenu']
        if not sources or not windows or not actions or not language_menus:
            if time.monotonic() < deadline:
                QtCore.QTimer.singleShot(250, check)
                return
            raise RuntimeError('GUI did not load its image, visible window and File menu within 20 seconds')
        expected_file = '\u6587\u4ef6' if expected == 'zh_CN' else 'File'
        assert all(action.text().replace('&','') == expected_file for action in actions), [action.text() for action in actions]
        required_menu_paths = {
            'View/Presentation Mode', 'View/Frame', 'View/Stereo/Off',
            'View/Matte/Custom...', 'View/Channel Display/Alpha',
            'Help/Online Resources', "Help/RV User's Manual", 'Help/Show Environment',
            'Tools/Annotation', 'Color/Cineon/DPX Log', 'Color/Hybrid Log-Gamma',
        }
        menu_checks = {}
        for widget in app.allWidgets():
            if not isinstance(widget, QtWidgets.QMenu):
                continue
            parent = widget.property('rvSourceMenuPath')
            if parent is None:
                continue
            for action in widget.actions():
                original = action.property('rvSourceText')
                if original is None:
                    continue
                original_path = '/'.join([str(part).strip() for part in parent] + [original.strip()])
                if original_path not in required_menu_paths:
                    continue
                expected_caption = QtCore.QCoreApplication.translate('RvMenu', original)
                assert action.text() == expected_caption, (original_path, expected_caption, action.text())
                if expected == 'zh_CN':
                    assert any('\u4e00' <= char <= '\u9fff' for char in expected_caption), (original_path, expected_caption)
                menu_checks[original_path] = action.text()
        missing_menu_paths = required_menu_paths - menu_checks.keys()
        if missing_menu_paths:
            if time.monotonic() < deadline:
                QtCore.QTimer.singleShot(250, check)
                return
            raise RuntimeError('Required native menu paths not loaded: ' + repr(sorted(missing_menu_paths)))
        expected_names = {'rvInterfaceLanguage_system', 'rvInterfaceLanguage_en', 'rvInterfaceLanguage_zh_CN'}
        for menu in language_menus:
            assert {action.objectName() for action in menu.actions()} == expected_names
            assert all(action.isCheckable() for action in menu.actions())
        default_view_result = runpy.run_path(os.environ['ORV_DEFAULT_VIEW_FIXTURE'])['check_default_view_labels']()
        hud_result = runpy.run_path(os.environ['ORV_HUD_FIXTURE'])['check_hud_localization']()
        proof = {'language': actual, 'fileMenuTranslation': translated,
                       'qtBaseTranslationLoaded': qt_base_loaded, 'overlayFontFile': overlay_font,
                       'checkedMenuPaths': sorted(menu_checks),
                       'defaultViewLabels': default_view_result, 'hudLocalization': hud_result,
                       'actualFileMenuTexts': [action.text() for action in actions],
                       'languageMenuActionNames': sorted(expected_names),
                       'fileSources': commands.nodesOfType('RVFileSource')}
        finish_gui(app, proof, windows)
    except BaseException:
        traceback.print_exc()
        QtCore.QCoreApplication.exit(1)
QtCore.QTimer.singleShot(200, check)
'''


def step_passed(exit_code, timed_out, stdout, marker, *, gui_language=None, gui_details=None):
    """A completed GUI proof never substitutes for a clean native process exit."""
    passed = not timed_out and exit_code == 0 and (not marker or marker in stdout)
    if gui_language is not None:
        passed = (passed and isinstance(gui_details, dict) and gui_details.get('status') == 'passed'
                  and gui_details.get('language') == gui_language
                  and all(gui_details.get(key) is True for key in ('requestClose', 'closeAccepted', 'sessionClosed')))
    return passed


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--runtime', required=True, type=pathlib.Path)
    parser.add_argument('--work', required=True, type=pathlib.Path)
    parser.add_argument('--require-gui', action='store_true')
    args = parser.parse_args()
    if sys.platform == 'darwin' and not (os.environ.get('GITHUB_ACTIONS') == 'true'
            and os.environ.get('RUNNER_OS') == 'macOS'
            and os.environ.get('RUNNER_ENVIRONMENT') == 'github-hosted'):
        parser.error('macOS runtime smoke checks require a disposable GitHub-hosted runner')
    runtime, work = args.runtime.resolve(), args.work.resolve()
    if work == runtime or work.is_relative_to(runtime):
        parser.error('Work directory must be outside the installed runtime')
    work.mkdir(parents=True, exist_ok=False)
    fixture_root = pathlib.Path(__file__).resolve().parents[2] / 'tests' / 'localization'
    for fixture_name in ('default_view_labels.py', 'hud_display_fixture.py'):
        (work / fixture_name).write_text((fixture_root / fixture_name).read_text(encoding='utf-8'), encoding='utf-8')
    content = runtime / 'Contents' if runtime.suffix == '.app' else runtime
    windows_root = os.environ.get('SystemRoot', '') if os.name == 'nt' else ''
    if os.name == 'nt' and not windows_root:
        raise RuntimeError('Windows SystemRoot is not set')
    bindir = content / ('MacOS' if runtime.suffix == '.app' else 'bin')
    suffix = '.exe' if os.name == 'nt' else ''
    names = {name: bindir / (name + suffix) for name in ('rv', 'rvio', 'py-interp')}
    if not names['rv'].is_file() and runtime.suffix == '.app':
        names['rv'] = bindir / 'RV'
    for path in names.values():
        if not path.is_file(): raise FileNotFoundError(path)
    steps = []
    report = {'runtime': str(runtime), 'requiredGui': args.require_gui, 'steps': steps,
              'startedAtUtc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
              'graphicsCapability': 'Measured only by the reported GUI checks; playback performance is not assessed.'}

    def invoke(name, binary, arguments, marker='', language='', offscreen=False):
        folder = work / name
        folder.mkdir()
        environment = os.environ.copy()
        for key in ('PYTHONHOME', 'PYTHONPATH', 'PYTHONSTARTUP', 'QT_PLUGIN_PATH',
                    'QT_QPA_PLATFORM_PLUGIN_PATH', 'QT_QPA_PLATFORM', 'OCIO', 'RV_INIT',
                    'RV_PYINIT', 'RVIO_INIT', 'RV_UI_LANGUAGE', 'MU_MODULE_PATH',
                    'QML_IMPORT_PATH', 'QML2_IMPORT_PATH', 'RV_ALLOW_SITE_PYTHONHOME',
                    'RV_PYTHONPATH_APPEND_ONLY', 'TWK_APP_SUPPORT_PATH', 'TWK_PROFILE_PLUGIN_PATH',
                    'TWK_FB_PLUGIN_PATH', 'TWK_MOVIE_PLUGIN_PATH', 'TWK_NODE_PLUGIN_PATH',
                    'TWK_OUTPUT_PLUGIN_PATH', 'TWK_MEDIA_LIBRARY_PLUGIN_PATH', 'OIIO_LIBRARY_PATH',
                    'DYLD_LIBRARY_PATH', 'DYLD_FRAMEWORK_PATH', 'DYLD_FALLBACK_LIBRARY_PATH',
                    'DYLD_FALLBACK_FRAMEWORK_PATH'):
            environment.pop(key, None)
        for key, subdir in (('APPDATA', 'appdata'), ('LOCALAPPDATA', 'localappdata'),
                            ('TEMP', 'temp'), ('TMP', 'temp'), ('RV_SUPPORT_PATH', 'support'),
                            ('RV_CRASH_DUMPS_DIR', 'crashes')):
            destination = folder / subdir
            destination.mkdir(exist_ok=True)
            environment[key] = str(destination)
        scripts = content / 'Resources' / 'English.lproj' if runtime.suffix == '.app' else content / 'scripts' / 'rv'
        for key, filename in (('RV_INIT', 'rvrc.mu'), ('RV_PYINIT', 'rvrc.py'), ('RVIO_INIT', 'rviorc.mu')):
            environment[key] = str(scripts / filename)
        environment.update(PYTHONNOUSERSITE='1', PYTHONDONTWRITEBYTECODE='1', RV_NO_CONSOLE_REDIRECT='1',
                           ORV_RUNTIME=str(runtime), ORV_INPUT=str(work / 'input.png'),
                           ORV_EXR=str(work / 'converted.exr'), ORV_ROUNDTRIP=str(work / 'roundtrip.png'),
                           ORV_GUI_REPORT=str(folder / 'gui.json'),
                           ORV_DEFAULT_VIEW_FIXTURE=str(work / 'default_view_labels.py'),
                           ORV_HUD_FIXTURE=str(work / 'hud_display_fixture.py'),
                           ORV_DEFAULT_VIEW_PREFS=str(work / ('prefs-' + language)))
        if language: environment['RV_UI_LANGUAGE'] = language
        if offscreen: environment['QT_QPA_PLATFORM'] = 'offscreen'
        # Never modify the parent or system PATH, or HOME. Exclude compiler/Qt installs.
        environment['PATH'] = os.pathsep.join([str(bindir)] + (
            [str(pathlib.Path(windows_root) / 'System32'), windows_root]
            if os.name == 'nt' else ['/usr/bin', '/bin', '/usr/sbin', '/sbin']))
        started = time.monotonic()
        process = subprocess.Popen([str(binary), *arguments], cwd=work, env=environment,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   start_new_session=(os.name != 'nt'))
        timed_out = False
        try:
            stdout, stderr = process.communicate(timeout=60)
        except subprocess.TimeoutExpired:
            timed_out = True
            if os.name == 'nt':
                subprocess.run([str(pathlib.Path(windows_root) / 'System32' / 'taskkill.exe'),
                                '/PID', str(process.pid), '/T', '/F'], capture_output=True, timeout=10)
            else:
                os.killpg(process.pid, signal.SIGKILL)
            stdout, stderr = process.communicate(timeout=10)
        stdout_text, stderr_text = (value.decode('utf-8', errors='replace') for value in (stdout, stderr))
        (folder / 'stdout.log').write_text(stdout_text, encoding='utf-8')
        (folder / 'stderr.log').write_text(stderr_text, encoding='utf-8')
        gui_language = language if sys.platform == 'darwin' and name in ('gui-en', 'gui-zh_CN') else None
        gui_details, gui_error = None, None
        if gui_language is not None:
            try:
                gui_details = json.loads((folder / 'gui.json').read_text(encoding='utf-8'))
            except (OSError, ValueError) as error:
                gui_error = type(error).__name__ + ': ' + str(error)[:500]
        passed = step_passed(process.returncode, timed_out, stdout_text, marker,
                             gui_language=gui_language, gui_details=gui_details)
        result = {'name': name, 'status': 'passed' if passed else 'failed', 'exitCode': process.returncode,
                  'pid': process.pid,
                  'timedOut': timed_out, 'seconds': round(time.monotonic()-started, 3),
                  'stdout': stdout_text, 'stderr': stderr_text}
        if gui_language is not None:
            result.update({key: gui_details.get(key) if isinstance(gui_details, dict) else None
                           for key in ('requestClose', 'closeAccepted', 'sessionClosed')})
            if gui_error:
                result['guiReportError'] = gui_error
        steps.append(result)
        print(f"{result['status']}: {name} (exit={process.returncode})", flush=True)
        if not passed:
            # Keep CI failures diagnosable without publishing runtime log directories.
            print(stdout_text[-8000:], flush=True)
            print(stderr_text[-8000:], file=sys.stderr, flush=True)
        return passed

    try:
        if not invoke('rv-version', names['rv'], ['-noPrefs', '-prefsPath', str(work / 'version-prefs'), '-version']):
            return 1
        if not invoke('python-ocio', names['py-interp'], ['-B', '-s', '-c', PYTHON_SMOKE], 'PYTHON_OCIO_OK', offscreen=True):
            return 1
        if not invoke('png-to-exr', names['rvio'], ['-noSequence', '-rthreads', '1', '-wthreads', '1',
                      '-insrgb', str(work / 'input.png'), '-outhalf', '-o', str(work / 'converted.exr')]):
            return 1
        if not invoke('exr-to-png', names['rvio'], ['-noSequence', '-rthreads', '1', '-wthreads', '1',
                      str(work / 'converted.exr'), '-outsrgb', '-outformat', '8', 'int', '-o', str(work / 'roundtrip.png')]):
            return 1
        if not invoke('dimensions', names['py-interp'], ['-B', '-s', '-c', DIMENSION_SMOKE], 'ROUNDTRIP_OK', offscreen=True):
            return 1
        gui_ok = True
        scripts = content / 'Resources' / 'English.lproj' if runtime.suffix == '.app' else content / 'scripts' / 'rv'
        for language in ('en', 'zh_CN'):
            gui_ok &= invoke('gui-' + language, names['rv'], ['-noPrefs', '-prefsPath', str(work / ('prefs-' + language)),
                '-init', str(scripts / 'rvrc.mu'), '-progressiveSourceLoading', '0',
                '-noSequence', str(work / 'input.png'), '-pyeval', GUI_SMOKE], 'GUI_LANGUAGE_OK', language=language)
        report['guiPassed'] = gui_ok
        aac_ok = invoke('aac-mp4', pathlib.Path(sys.executable), [
            '-B', str(pathlib.Path(__file__).resolve().with_name('aac_runtime_smoke.py')),
            '--runtime', str(runtime),
            '--fixture', str(pathlib.Path(__file__).resolve().parents[2] / 'tests/media/h264-aac-stereo.mp4'),
            '--work', str(work / 'aac-mp4' / 'native')], 'AAC_MEDIA_SMOKE_OK')
        report['aacMediaPassed'] = aac_ok
        return int(not aac_ok or (args.require_gui and not gui_ok))
    finally:
        report['finishedAtUtc'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        (work / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


if __name__ == '__main__':
    sys.exit(main())
