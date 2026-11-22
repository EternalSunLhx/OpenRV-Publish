"""Opt-in tests of an already extracted package; never builds or publishes it."""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

from aac_runtime_smoke import FIXTURE_SHA256

CASES = ('rotation-playback', 'preferences-owner-close')
MARKER = 'POST_PACKAGE_CASE_OK'
GL_PROVIDER_SHA256 = 'b04de4541863bc7d8879040a78889c4849c1b1da2784c4630f734c146c2998ce'
GL_PROVIDER_SIZE = 20639888

GUI_CASE = r'''
import json, os, pathlib, time, traceback
from PySide6 import QtCore, QtGui, QtWidgets
from rv import commands
app = QtWidgets.QApplication.instance()
case = os.environ['ORV_POST_CASE']
state = {'finished': False, 'closing': False}
proof = {'case': case, 'language': os.environ['RV_UI_LANGUAGE'], 'status': 'running',
         'requestClose': False, 'closeAccepted': False, 'sessionClosed': False}
deadline = time.monotonic() + 30

def write_report():
    with open(os.environ['ORV_POST_REPORT'], 'w', encoding='utf-8') as stream:
        json.dump(proof, stream, ensure_ascii=False)

def fail(error):
    if state['finished']:
        return
    state['finished'] = True
    proof.update(status='failed', error=str(error)[:1000])
    write_report()
    traceback.print_exc()
    QtCore.QCoreApplication.exit(1)

def later(callback, delay=0):
    def guarded():
        if state['finished']:
            return
        try:
            assert time.monotonic() < deadline, 'Post-package GUI case exceeded its deadline'
            callback()
        except BaseException as error:
            fail(error)
    QtCore.QTimer.singleShot(delay, guarded)

def sessions():
    return [widget for widget in app.topLevelWidgets()
            if widget.objectName().startswith('rv-session') and widget.isVisible()]

def menu_action(path):
    # Traverse actual menu bars, excluding the duplicate right-click popup.
    found = {}
    for bar in app.allWidgets():
        if not isinstance(bar, QtWidgets.QMenuBar):
            continue
        current = [bar]
        for depth, component in enumerate(path):
            selected = []
            for menu in current:
                if isinstance(menu, QtWidgets.QMenu):
                    assert list(menu.property('rvSourceMenuPath') or []) == list(path[:depth])
                    menu.aboutToShow.emit()
                for action in menu.actions():
                    if action.property('rvSourceText') == component:
                        if depth == len(path) - 1:
                            found[id(action)] = action
                        elif action.menu() is not None:
                            selected.append(action.menu())
            current = selected
    assert len(found) == 1, ('Expected one native menu action', path, len(found))
    action = next(iter(found.values()))
    assert action.isEnabled() and action.menuRole() == QtGui.QAction.NoRole, path
    return action

def finish(window):
    assert not state['closing'] and sessions() == [window]
    assert app.quitOnLastWindowClosed()
    assert window.testAttribute(QtCore.Qt.WA_DeleteOnClose) and window.testAttribute(QtCore.Qt.WA_QuitOnClose)
    state['closing'] = True
    proof['status'] = 'closing'
    write_report()
    def destroyed(*unused):
        try:
            assert proof['requestClose'] and proof['closeAccepted'], 'Session was destroyed before its owned close completed'
            proof.update(sessionClosed=True, status='passed')
            write_report()
            print('POST_PACKAGE_CASE_OK', flush=True)
            state['finished'] = True
        except BaseException as error:
            fail(error)
    def close():
        proof['requestClose'] = True
        assert window.close(), 'Owned session rejected normal close'
        proof['closeAccepted'] = True
    window.destroyed.connect(destroyed)
    later(close)

def rotation_playback(window):
    commands.stop()
    prop = '#RVTransform2D.transform.rotate'
    before = commands.getFloatProperty(prop)
    assert before == [0.0], ('Fixture must start without rotation', before)
    action = menu_action(('Image', 'Rotation', '180'))
    action.trigger()
    assert commands.getFloatProperty(prop) == [180.0], 'Actual menu action did not rotate the source by 180 degrees'
    assert sessions() == [window], 'Rotation changed the owned session'
    proof['rotation'] = {'menuPath': 'Image/Rotation/180', 'initialAngle': 0, 'angle': 180,
                         'sessionAlive': False, 'playbackObserved': False, 'sampledFrames': []}
    commands.setPlayMode(commands.PlayLoop)
    commands.setFrame(commands.frameStart())
    commands.play()
    started = time.monotonic()
    def sample():
        assert sessions() == [window], 'Session closed during playback after rotation'
        assert commands.getFloatProperty(prop) == [180.0]
        assert commands.isPlaying(), 'RV stopped instead of continuing playback'
        proof['rotation']['sampledFrames'].append(commands.frame())
        commands.redraw()
        if time.monotonic() - started < 2.0:
            later(sample, 200)
            return
        assert len(set(proof['rotation']['sampledFrames'])) >= 3, 'No sustained playback frame progression'
        commands.stop()
        assert not commands.isPlaying()
        proof['rotation'].update(sessionAlive=True, playbackObserved=True, playbackSeconds=round(time.monotonic() - started, 3))
        finish(window)
    later(sample, 200)

def preferences_owner_close(window):
    commands.stop()
    actions = [action for action in app.findChildren(QtGui.QAction)
               if action.menuRole() == QtGui.QAction.PreferencesRole]
    assert len(actions) == 1 and actions[0].isEnabled(), 'Expected the native Preferences action'
    actions[0].trigger()
    dialogs = [widget for widget in app.topLevelWidgets()
               if widget.metaObject().className().split('::')[-1] == 'RvPreferences' and widget.isVisible()]
    assert len(dialogs) == 1 and dialogs[0].parent() == window, 'Preferences must belong to session A'
    prefs = dialogs[0]
    observed = {'ownerDestroyed': False, 'preferencesDestroyed': False}
    proof['preferences'] = observed
    window.destroyed.connect(lambda *unused: observed.update(ownerDestroyed=True))
    prefs.destroyed.connect(lambda *unused: observed.update(preferencesDestroyed=True))
    # Closing the dialog hides it; the owning session must perform destruction.
    assert prefs.close()
    assert not observed['preferencesDestroyed'], 'Preferences unexpectedly deleted itself on hide'
    menu_action(('File', 'New Session')).trigger()
    def close_owner():
        values = sessions()
        if len(values) == 1 and values[0] == window:
            later(close_owner, 100)
            return
        assert len(values) == 2 and window in values, 'New Session did not create the separate session B'
        survivor = next(value for value in values if value != window)
        assert window.close(), 'Session A rejected close'
        def await_destruction():
            if not all(observed.values()):
                later(await_destruction, 50)
                return
            assert sessions() == [survivor]
            # This final close exercised the stale application preference cache
            # in build35. No explicit dialog deletion or factory workaround.
            finish(survivor)
        later(await_destruction)
    later(close_owner)

def start():
    values = sessions()
    sources = commands.nodesOfType('RVFileSource')
    if len(values) != 1 or len(sources) != 1:
        later(start, 100)
        return
    assert app.property('rvInterfaceLanguage') == proof['language']
    movies = commands.getStringProperty(sources[0] + '.media.movie')
    assert len(movies) == 1 and pathlib.Path(movies[0]).resolve() == pathlib.Path(os.environ['ORV_POST_MEDIA']).resolve()
    proof['mp4SourceVerified'] = True
    # The native menu can finish loading after the source/window is available.
    if not any(action.property('rvSourceText') == 'Image' for bar in app.allWidgets()
               if isinstance(bar, QtWidgets.QMenuBar) for action in bar.actions()):
        later(start, 100)
        return
    if case == 'rotation-playback':
        rotation_playback(values[0])
    elif case == 'preferences-owner-close':
        preferences_owner_close(values[0])
    else:
        raise ValueError('Unknown post-package GUI case')
later(start, 200)
'''


def case_passed(exit_code, timed_out, stdout, details, case, language):
    if (type(exit_code) is not int or exit_code != 0 or timed_out or MARKER not in stdout
            or not isinstance(details, dict) or details.get('case') != case or details.get('language') != language
            or details.get('status') != 'passed'
            or any(details.get(key) is not True for key in ('mp4SourceVerified', 'requestClose', 'closeAccepted', 'sessionClosed'))):
        return False
    if case == 'rotation-playback':
        proof = details.get('rotation')
        return (isinstance(proof, dict) and proof.get('menuPath') == 'Image/Rotation/180'
                and type(proof.get('initialAngle')) is int and proof['initialAngle'] == 0
                and type(proof.get('angle')) is int and proof['angle'] == 180
                and proof.get('sessionAlive') is True and proof.get('playbackObserved') is True
                and type(proof.get('playbackSeconds')) in (int, float) and 2 <= proof['playbackSeconds'] <= 30
                and isinstance(proof.get('sampledFrames'), list)
                and len(proof['sampledFrames']) >= 3 and all(type(frame) is int for frame in proof['sampledFrames'])
                and len(set(proof['sampledFrames'])) >= 3)
    if case == 'preferences-owner-close':
        proof = details.get('preferences')
        return isinstance(proof, dict) and all(proof.get(key) is True for key in ('ownerDestroyed', 'preferencesDestroyed'))
    return False


def require_hosted_runner():
    runner_os = {'win32': 'Windows', 'darwin': 'macOS'}.get(sys.platform)
    if not runner_os or any(os.environ.get(key) != value for key, value in {
            'GITHUB_ACTIONS': 'true', 'RUNNER_ENVIRONMENT': 'github-hosted', 'RUNNER_OS': runner_os}.items()):
        raise ValueError('Post-package GUI tests run only on disposable GitHub-hosted Windows/macOS runners')


def windows_gl_settings(runtime):
    if sys.platform != 'win32':
        return {}
    source = os.environ.get('OPENRV_CI_GL_PROVIDER', '')
    if not source or not os.environ.get('RUNNER_TEMP'):
        raise ValueError('Windows hosted GUI tests require the scoped, verified software GL launcher')
    path = Path(source)
    root = Path(os.environ['RUNNER_TEMP']).resolve()
    if (path.is_symlink() or not path.is_file() or path.name != 'opengl32.dll'
            or not path.resolve().is_relative_to(root) or path.resolve().is_relative_to(runtime)
            or path.stat().st_size != GL_PROVIDER_SIZE
            or os.environ.get('OPENRV_CI_GL_SHA256') != GL_PROVIDER_SHA256
            or hashlib.sha256(path.read_bytes()).hexdigest() != GL_PROVIDER_SHA256):
        raise ValueError('Windows test GL provider differs from the pinned external fixture')
    return {'QT_OPENGL': 'desktop', 'QT_OPENGL_DLL': str(path.resolve())}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime', type=Path, required=True)
    parser.add_argument('--work', type=Path, required=True)
    parser.add_argument('--case', choices=('all', *CASES), default='all')
    parser.add_argument('--language', choices=('both', 'en', 'zh_CN'), default='both')
    args = parser.parse_args()
    require_hosted_runner()
    runtime, work = args.runtime.resolve(), args.work.resolve()
    if work == runtime or work.is_relative_to(runtime):
        raise ValueError('The new diagnostics directory must be outside the extracted runtime')
    gl_settings = windows_gl_settings(runtime)
    content = runtime / 'Contents' if runtime.suffix == '.app' else runtime
    bindir = content / ('MacOS' if runtime.suffix == '.app' else 'bin')
    candidates = [bindir / name for name in (('rv.exe',) if sys.platform == 'win32' else ('RV', 'rv'))]
    executable = next((path for path in candidates if path.is_file()), None)
    if executable is None:
        raise FileNotFoundError('Extracted package lacks the native RV executable')
    fixture = Path(__file__).resolve().parents[2] / 'tests/media/h264-aac-stereo.mp4'
    if hashlib.sha256(fixture.read_bytes()).hexdigest() != FIXTURE_SHA256:
        raise ValueError('The owned MP4 fixture differs from its reviewed digest')
    work.mkdir(parents=True, exist_ok=False)
    report = {'runtime': str(runtime), 'fixtureSha256': FIXTURE_SHA256, 'status': 'failed',
              'startedAtUtc': datetime.datetime.now(datetime.timezone.utc).isoformat(), 'cases': [],
              'graphicsProvider': 'pinned-hosted-software' if gl_settings else 'native-platform',
              'scope': 'Separate manual package tests; no build, publication, or playback-performance certification.'}
    cases = CASES if args.case == 'all' else (args.case,)
    languages = ('en', 'zh_CN') if args.language == 'both' else (args.language,)
    scripts = content / 'Resources/English.lproj' if runtime.suffix == '.app' else content / 'scripts/rv'
    try:
        for case in cases:
            for language in languages:
                folder = work / (case + '-' + language)
                folder.mkdir()
                environment = os.environ.copy()
                for key in list(environment):
                    if key.startswith(('RV_', 'RVIO_', 'TWK_', 'DYLD_', 'PYTHON', 'QT_', 'QML_')) or key in (
                            'OCIO', 'OIIO_LIBRARY_PATH', 'MU_MODULE_PATH', 'GH_TOKEN', 'GITHUB_TOKEN'):
                        environment.pop(key)
                for key in ('APPDATA', 'LOCALAPPDATA', 'TEMP', 'TMP', 'RV_SUPPORT_PATH', 'RV_CRASH_DUMPS_DIR'):
                    target = folder / key.lower(); target.mkdir()
                    environment[key] = str(target)
                environment.update(PYTHONNOUSERSITE='1', PYTHONDONTWRITEBYTECODE='1', RV_NO_CONSOLE_REDIRECT='1',
                                   RV_DISABLE_AUTOMATIC_UPDATE_CHECK='1',
                                   RV_INIT=str(scripts / 'rvrc.mu'), RV_PYINIT=str(scripts / 'rvrc.py'),
                                   RV_UI_LANGUAGE=language, ORV_POST_CASE=case,
                                   ORV_POST_MEDIA=str(fixture), ORV_POST_REPORT=str(folder / 'gui.json'))
                # Keep Qt and native GLEW on the launcher's same verified DLL.
                # Other inherited Qt settings and SDK paths remain excluded.
                environment.update(gl_settings)
                system = os.environ.get('SystemRoot', '')
                if sys.platform == 'win32' and not system:
                    raise ValueError('Windows SystemRoot is missing')
                environment['PATH'] = os.pathsep.join([str(bindir)] + (
                    [str(Path(system) / 'System32'), system] if sys.platform == 'win32'
                    else ['/usr/bin', '/bin', '/usr/sbin', '/sbin']))
                started = time.monotonic()
                started_utc = datetime.datetime.now(datetime.timezone.utc).isoformat()
                process = subprocess.Popen([str(executable), '-noPrefs', '-prefsPath', str(folder / 'prefs'),
                    '-init', str(scripts / 'rvrc.mu'), '-progressiveSourceLoading', '0', '-noSequence', str(fixture),
                    '-pyeval', GUI_CASE], cwd=work, env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    start_new_session=(sys.platform != 'win32'))
                timed_out = False
                try:
                    stdout, stderr = process.communicate(timeout=60)
                except subprocess.TimeoutExpired:
                    timed_out = True
                    if sys.platform == 'win32':
                        subprocess.run([str(Path(system) / 'System32/taskkill.exe'), '/PID', str(process.pid), '/T', '/F'],
                                       capture_output=True, timeout=10)
                    else:
                        os.killpg(process.pid, signal.SIGKILL)
                    stdout, stderr = process.communicate(timeout=10)
                stdout, stderr = (data.decode('utf-8', errors='replace')[-1024 * 1024:] for data in (stdout, stderr))
                (folder / 'stdout.log').write_text(stdout, encoding='utf-8')
                (folder / 'stderr.log').write_text(stderr, encoding='utf-8')
                details = None
                try:
                    details = json.loads((folder / 'gui.json').read_text(encoding='utf-8'))
                except (OSError, ValueError):
                    pass
                passed = case_passed(process.returncode, timed_out, stdout, details, case, language)
                result = {'case': case, 'language': language, 'status': 'passed' if passed else 'failed',
                          'pid': process.pid, 'exitCode': process.returncode, 'timedOut': timed_out,
                          'startedAtUtc': started_utc, 'finishedAtUtc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
                          'seconds': round(time.monotonic() - started, 3), 'details': details}
                report['cases'].append(result)
                print(json.dumps(result, ensure_ascii=False), flush=True)
        report['status'] = 'passed' if all(item['status'] == 'passed' for item in report['cases']) else 'failed'
        return int(report['status'] != 'passed')
    finally:
        report['finishedAtUtc'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        (work / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


if __name__ == '__main__':
    sys.exit(main())
