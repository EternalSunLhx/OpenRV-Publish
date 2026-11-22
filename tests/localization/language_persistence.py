"""Exercise the real language chooser across three isolated, visible RV launches.

Run with a host Python, not RV's embedded interpreter. The installed runtime is
read-only. A new caller-selected work directory owns all preferences and logs.
This test deliberately skips non-Windows hosts: native macOS QSettings requires
separate isolation verification before it is safe to exercise persistence.
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time


GUI_CHECK = r'''
import json, os, pathlib, time, traceback
from PySide6 import QtCore, QtWidgets
from rv import commands

config = json.loads(pathlib.Path(os.environ['ORV_LANGUAGE_PERSIST_CONFIG']).read_text(encoding='utf-8'))
app = QtWidgets.QApplication.instance()
deadline = time.monotonic() + 30
state = {'finished': False, 'restartNote': None}
result = {'stage': config['stage'], 'expectedLanguage': config['expectedLanguage']}
expected_names = {'rvInterfaceLanguage_system', 'rvInterfaceLanguage_en', 'rvInterfaceLanguage_zh_CN'}

def finish(code, error=None):
    if state['finished']:
        return
    state['finished'] = True
    if state.get('closeTimer'):
        state['closeTimer'].stop()
    result['status'] = 'passed' if code == 0 else 'failed'
    if error:
        result['error'] = error
    pathlib.Path(config['childReport']).write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print('LANGUAGE_PERSISTENCE_STAGE_OK' if code == 0 else 'LANGUAGE_PERSISTENCE_STAGE_FAILED', flush=True)
    app.exit(code)

def verify_language(window, actions):
    actual = app.property('rvInterfaceLanguage')
    expected = config['expectedLanguage']
    assert actual == expected, ('app language', expected, actual)
    expected_file = '\u6587\u4ef6' if expected == 'zh_CN' else 'File'
    texts = [action.text() for action in actions]
    assert texts and all(text.replace('&', '') == expected_file for text in texts), texts
    assert QtCore.QCoreApplication.translate('RvMenu', 'File') == expected_file
    assert window.isVisible(), 'Session window unexpectedly closed'
    return texts

def isolated_settings():
    assert os.name == 'nt', 'Persistence is enabled only with Windows INI isolation'
    assert QtCore.QSettings.defaultFormat() == QtCore.QSettings.IniFormat
    settings = QtCore.QSettings(QtCore.QSettings.defaultFormat(), QtCore.QSettings.UserScope,
                               QtCore.QCoreApplication.organizationName(), QtCore.QCoreApplication.applicationName())
    settings.setFallbacksEnabled(False)
    filename = pathlib.Path(settings.fileName()).resolve()
    private_prefs = pathlib.Path(config['prefsPath']).resolve()
    private_work = pathlib.Path(config['workPath']).resolve()
    assert filename.is_relative_to(private_prefs) and filename.is_relative_to(private_work), str(filename)
    assert filename.suffix.lower() == '.ini', str(filename)
    settings.sync()
    assert settings.status() == QtCore.QSettings.NoError, settings.status()
    result['settingsFile'] = str(filename)
    result['settingsIsolationVerifiedBeforeAction'] = True
    return settings

def save_screenshot(window):
    target = config.get('screenshot')
    if target:
        # This captures only RV's own widget, never the desktop or another app.
        pixmap = window.grab()
        assert not pixmap.isNull() and pixmap.save(target, 'PNG'), target
        result['screenshot'] = target

def check_after_action():
    try:
        window, actions, menu = state['window'], state['actions'], state['menu']
        result['afterActionFileMenuTexts'] = verify_language(window, actions)
        assert commands.viewNode() == state['viewNode'], 'Language selection changed the current session view'
        assert state['actionTriggeredCount'] == state['groupTriggeredCount'] == 1, 'Language action-group forwarding is broken'
        assert state['restartNote'], 'The native restart-required message was not observed'
        assert state['restartNote']['closedByTest'], 'Restart note was not closed by the owned-dialog timer'
        settings = isolated_settings()
        saved = settings.value('General/interfaceLanguage', 'system')
        assert saved == config['selectLanguage'], ('saved preference', saved)
        assert any(action.objectName() == 'rvInterfaceLanguage_' + saved and action.isChecked() for action in menu.actions())
        result.update(savedPreference=saved, restartNote=state['restartNote'], sessionStillVisible=True,
                      runningLanguageUnchanged=True, viewNode=commands.viewNode())
        save_screenshot(window)
        finish(0)
    except BaseException:
        finish(1, traceback.format_exc())

def belongs_to_language_menu(dialog, menu):
    parent = dialog.parent()
    while parent is not None:
        if parent == menu:
            return True
        parent = parent.parent()
    return False

def close_owned_restart_note():
    try:
        for widget in app.topLevelWidgets():
            if not isinstance(widget, QtWidgets.QMessageBox) or not widget.isVisible():
                continue
            if widget.windowTitle() != 'Restart required / \u9700\u8981\u91cd\u65b0\u542f\u52a8':
                continue
            if not belongs_to_language_menu(widget, state['menu']):
                continue
            text = widget.text()
            assert 'restart OpenRV' in text and '\u91cd\u65b0\u542f\u52a8 OpenRV' in text, text
            state['restartNote'] = {'title': widget.windowTitle(), 'text': text, 'closedByTest': True}
            state['closeTimer'].stop()
            widget.accept()
            return
    except BaseException:
        finish(1, traceback.format_exc())

def check_ready():
    if state['finished']:
        return
    try:
        commands.redraw()
        candidates = []
        for window in app.topLevelWidgets():
            if not window.isVisible():
                continue
            actions = [action for bar in window.findChildren(QtWidgets.QMenuBar) for action in bar.actions()
                       if action.property('rvSourceText') == 'File']
            menus = [menu for menu in window.findChildren(QtWidgets.QMenu)
                     if menu.objectName() == 'rvInterfaceLanguageMenu']
            if actions and menus:
                candidates.append((window, actions, menus[0]))
        if not candidates:
            if time.monotonic() < deadline:
                QtCore.QTimer.singleShot(200, check_ready)
                return
            raise RuntimeError('No visible RV session with File and Language menus within 30 seconds')
        window, actions, menu = candidates[0]
        result['beforeActionFileMenuTexts'] = verify_language(window, actions)
        menu_actions = menu.actions()
        assert {action.objectName() for action in menu_actions} == expected_names
        assert all(action.isCheckable() for action in menu_actions)
        assert {action.data() for action in menu_actions} == {'system', 'en', 'zh_CN'}
        result['languageActionNames'] = sorted(expected_names)
        result['actualLanguage'] = app.property('rvInterfaceLanguage')
        result['environmentOverride'] = app.property('rvInterfaceLanguageOverride')
        assert result['environmentOverride'] == config.get('overrideLanguage')
        settings = isolated_settings()  # Must pass BEFORE triggering any preference mutation.
        before = settings.value('General/interfaceLanguage', 'system')
        assert before == config['expectedPreference'], ('initial preference', before)
        result['preferenceBeforeAction'] = before
        # Exercise the native rebuild path repeatedly. The persistent Language
        # menu must retain both QAction -> QActionGroup forwarding and its save
        # callback after ordinary RV menus are disconnected and replaced.
        for unused in range(3):
            assert QtCore.QMetaObject.invokeMethod(window, 'buildMenu', QtCore.Qt.DirectConnection)
            current_menus = [item for item in window.findChildren(QtWidgets.QMenu)
                             if item.objectName() == 'rvInterfaceLanguageMenu']
            assert len(current_menus) == 1 and current_menus[0] == menu, 'Persistent language menu was duplicated or replaced'
        result['nativeMenuRebuilds'] = 3
        actions = [action for bar in window.findChildren(QtWidgets.QMenuBar) for action in bar.actions()
                   if action.property('rvSourceText') == 'File']
        result['afterRebuildFileMenuTexts'] = verify_language(window, actions)
        state.update(window=window, actions=actions, menu=menu, viewNode=commands.viewNode())
        target = config.get('selectLanguage')
        if target:
            assert target != before, 'This stage must exercise a real preference change'
            action = next(action for action in menu_actions if action.objectName() == 'rvInterfaceLanguage_' + target)
            group = action.actionGroup()
            result['targetActionBefore'] = {'enabled': action.isEnabled(), 'checkable': action.isCheckable(),
                                           'checked': action.isChecked(), 'hasActionGroup': group is not None}
            assert action.isEnabled(), 'The language action is disabled'
            state['actionTriggeredCount'] = 0
            state['groupTriggeredCount'] = 0
            result.update(actionTriggeredCount=0, groupTriggeredCount=0)
            def action_triggered(*unused):
                state['actionTriggeredCount'] += 1
                result['actionTriggeredCount'] = state['actionTriggeredCount']
            def group_triggered(*unused):
                state['groupTriggeredCount'] += 1
                result['groupTriggeredCount'] = state['groupTriggeredCount']
            action.triggered.connect(action_triggered)
            if group is not None:
                group.triggered.connect(group_triggered)
            timer = QtCore.QTimer(app)
            timer.setInterval(40)
            timer.timeout.connect(close_owned_restart_note)
            state['closeTimer'] = timer
            timer.start()
            action.trigger()  # Native QAction callback opens its own nested QMessageBox loop.
            result['targetActionCheckedAfterTrigger'] = action.isChecked()
            # Observe that the application and session survive after the native callback returns.
            QtCore.QTimer.singleShot(500, check_after_action)
        else:
            result.update(savedPreference=before, sessionStillVisible=True, viewNode=commands.viewNode())
            save_screenshot(window)
            finish(0)
    except BaseException:
        finish(1, traceback.format_exc())

def watchdog():
    if not state['finished']:
        finish(1, 'GUI persistence stage exceeded its 35-second watchdog')

QtCore.QTimer.singleShot(35000, watchdog)
QtCore.QTimer.singleShot(200, check_ready)
'''


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime', required=True, type=Path)
    parser.add_argument('--work', required=True, type=Path, help='New unique directory outside the installed runtime')
    parser.add_argument('--timeout', type=float, default=45, help='Per-launch timeout in seconds (40-120)')
    parser.add_argument('--screenshot', type=Path, help='Optional new PNG from the final RV widget grab')
    args = parser.parse_args()
    runtime, work = args.runtime.resolve(), args.work.resolve()
    if work == runtime or work.is_relative_to(runtime):
        parser.error('--work must be outside the installed runtime')
    if not 40 <= args.timeout <= 120:
        parser.error('--timeout must be between 40 and 120 seconds')
    if work.exists():
        parser.error('--work must be a new unique directory')
    screenshot = args.screenshot.resolve() if args.screenshot else None
    if screenshot and (screenshot == runtime or screenshot.is_relative_to(runtime) or screenshot.exists()):
        parser.error('--screenshot must be a new file outside the installed runtime')
    if screenshot and (screenshot.suffix.lower() != '.png' or not screenshot.parent.is_dir()):
        parser.error('--screenshot requires an existing parent directory and a .png filename')
    work.mkdir(parents=True, exist_ok=False)
    report = {'schemaVersion': 1, 'status': 'failed', 'runtime': str(runtime), 'work': str(work),
              'platform': sys.platform, 'launches': [], 'startedAtUtc': datetime.datetime.now(datetime.timezone.utc).isoformat()}
    report_path = work / 'report.json'
    try:
        if os.name != 'nt':
            report.update(status='skipped', reason='Windows INI preference isolation required; native macOS preferences were not touched.')
            return 0
        bindir = runtime / 'bin'
        executable = bindir / 'rv.exe'
        scripts = runtime / 'scripts' / 'rv'
        for required in (executable, scripts / 'rvrc.mu', scripts / 'rvrc.py'):
            if not required.is_file():
                raise FileNotFoundError(required)
        report['rvSha256'] = hashlib.sha256(executable.read_bytes()).hexdigest()
        windows_root = Path(os.environ['SystemRoot']).resolve()
        prefs = work / 'prefs'
        prefs.mkdir()
        shared = {}
        for name in ('appdata', 'localappdata', 'support'):
            shared[name] = work / name
            shared[name].mkdir()
        report['prefsPath'] = str(prefs)
        child_script = work / 'gui-check.py'
        child_script.write_text(GUI_CHECK, encoding='utf-8')
        stages = [
            {'stage': '01-chinese-select-english', 'expectedLanguage': 'zh_CN', 'expectedPreference': 'system',
             'overrideLanguage': 'zh_CN', 'selectLanguage': 'en'},
            {'stage': '02-english-select-chinese', 'expectedLanguage': 'en', 'expectedPreference': 'en',
             'selectLanguage': 'zh_CN'},
            {'stage': '03-chinese-persisted', 'expectedLanguage': 'zh_CN', 'expectedPreference': 'zh_CN'},
        ]
        for stage in stages:
            folder = work / stage['stage']
            folder.mkdir()
            config = dict(stage, workPath=str(work), prefsPath=str(prefs), childReport=str(folder / 'gui.json'))
            if screenshot and not stage.get('selectLanguage'):
                config['screenshot'] = str(screenshot)
            config_path = folder / 'config.json'
            config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding='utf-8')
            environment = os.environ.copy()
            for key in ('PYTHONHOME', 'PYTHONPATH', 'PYTHONSTARTUP', 'QT_PLUGIN_PATH', 'QT_QPA_PLATFORM_PLUGIN_PATH',
                        'QT_QPA_PLATFORM', 'QT_QPA_PLATFORMTHEME', 'OCIO', 'RV_INIT', 'RV_PYINIT', 'RVIO_INIT', 'RV_UI_LANGUAGE',
                        'RV_PREFS_OVERRIDE_PATH', 'RV_PREFS_CLOBBER_PATH', 'MU_MODULE_PATH', 'QML_IMPORT_PATH', 'QML2_IMPORT_PATH',
                        'RV_ALLOW_SITE_PYTHONHOME', 'RV_PYTHONPATH_APPEND_ONLY', 'TWK_APP_SUPPORT_PATH', 'TWK_PROFILE_PLUGIN_PATH',
                        'TWK_FB_PLUGIN_PATH', 'TWK_MOVIE_PLUGIN_PATH', 'TWK_NODE_PLUGIN_PATH', 'TWK_OUTPUT_PLUGIN_PATH',
                        'TWK_MEDIA_LIBRARY_PLUGIN_PATH', 'OIIO_LIBRARY_PATH'):
                environment.pop(key, None)
            temporary, crashes = folder / 'temp', folder / 'crashes'
            temporary.mkdir()
            crashes.mkdir()
            environment.update(APPDATA=str(shared['appdata']), LOCALAPPDATA=str(shared['localappdata']),
                               RV_DISABLE_AUTOMATIC_UPDATE_CHECK='1',
                               TEMP=str(temporary), TMP=str(temporary), RV_SUPPORT_PATH=str(shared['support']),
                               RV_CRASH_DUMPS_DIR=str(crashes), RV_INIT=str(scripts / 'rvrc.mu'), RV_PYINIT=str(scripts / 'rvrc.py'),
                               PYTHONNOUSERSITE='1', PYTHONDONTWRITEBYTECODE='1', RV_NO_CONSOLE_REDIRECT='1',
                               ORV_LANGUAGE_PERSIST_CONFIG=str(config_path))
            if stage.get('overrideLanguage'):
                environment['RV_UI_LANGUAGE'] = stage['overrideLanguage']
            environment['PATH'] = os.pathsep.join((str(bindir), str(windows_root / 'System32'), str(windows_root)))
            expression = f"exec(compile(open({str(child_script)!r}, encoding='utf-8').read(), {str(child_script)!r}, 'exec'))"
            command = [str(executable), '-prefsPath', str(prefs), '-init', str(scripts / 'rvrc.mu'), '-pyeval', expression]
            started = time.monotonic()
            # No hidden/offscreen flags: the real RV session must become visible.
            process = subprocess.Popen(command, cwd=work, env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            timed_out = False
            timeout_cleanup = None
            try:
                stdout, stderr = process.communicate(timeout=args.timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                # Only this Popen's still-running process tree is eligible for timeout cleanup.
                if process.poll() is None:
                    try:
                        killed = subprocess.run([str(windows_root / 'System32' / 'taskkill.exe'), '/PID', str(process.pid), '/T', '/F'],
                                                capture_output=True, timeout=10)
                        timeout_cleanup = killed.returncode
                    except (subprocess.TimeoutExpired, OSError) as error:
                        timeout_cleanup = type(error).__name__
                try:
                    stdout, stderr = process.communicate(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()  # Exact Popen process handle, never an image-name/global kill.
                    stdout, stderr = process.communicate(timeout=10)
            stdout_text, stderr_text = (value.decode('utf-8', errors='replace') for value in (stdout, stderr))
            (folder / 'stdout.log').write_text(stdout_text, encoding='utf-8')
            (folder / 'stderr.log').write_text(stderr_text, encoding='utf-8')
            child_report = Path(config['childReport'])
            details = json.loads(child_report.read_text(encoding='utf-8')) if child_report.is_file() else None
            passed = (not timed_out and process.returncode == 0 and 'LANGUAGE_PERSISTENCE_STAGE_OK' in stdout_text
                      and details is not None and details.get('status') == 'passed')
            result = {'stage': stage['stage'], 'status': 'passed' if passed else 'failed', 'exitCode': process.returncode,
                      'timedOut': timed_out, 'seconds': round(time.monotonic() - started, 3), 'details': details,
                      'stdoutLog': str(folder / 'stdout.log'), 'stderrLog': str(folder / 'stderr.log')}
            if timed_out:
                result['ownedTimeoutCleanupExitCode'] = timeout_cleanup
            report['launches'].append(result)
            print(f"{result['status']}: {stage['stage']} (exit={process.returncode})", flush=True)
            if not passed:
                return 1
        if hashlib.sha256(executable.read_bytes()).hexdigest() != report['rvSha256']:
            raise RuntimeError('RV executable changed during the three-launch test')
        report['status'] = 'passed'
        return 0
    except BaseException as error:
        report['error'] = f'{type(error).__name__}: {error}'
        return 1
    finally:
        report['finishedAtUtc'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        print(json.dumps({'status': report['status'], 'report': str(report_path)}, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    sys.exit(main())
