"""Exercise the native chooser and persistence only on disposable hosted Mac runners.

macOS NativeFormat uses CFPreferences, so this does not claim private INI isolation.
It borrows only the built app's language key on an ephemeral GitHub-hosted runner,
snapshots that key before mutation, and restores and verifies it in fresh processes.
Ordinary local machines are rejected before a directory or process is created.
"""
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

from language_persistence import GUI_CHECK as WINDOWS_GUI_CHECK

KEY = 'General/interfaceLanguage'
HOST_MARKERS = {'GITHUB_ACTIONS': 'true', 'RUNNER_OS': 'macOS', 'RUNNER_ENVIRONMENT': 'github-hosted'}


def require_hosted_macos(platform=None, environment=None):
    platform = sys.platform if platform is None else platform
    environment = os.environ if environment is None else environment
    if platform != 'darwin' or any(environment.get(key) != value for key, value in HOST_MARKERS.items()):
        raise RuntimeError('Native preference mutation is allowed only on a disposable GitHub-hosted macOS runner')


# This guard is repeated inside every embedded process, before QSettings access.
EMBEDDED_COMMON = r'''
import json, os, pathlib, re, sys
from PySide6 import QtCore
assert sys.platform == 'darwin'
assert os.environ.get('GITHUB_ACTIONS') == 'true'
assert os.environ.get('RUNNER_OS') == 'macOS'
assert os.environ.get('RUNNER_ENVIRONMENT') == 'github-hosted'
KEY = 'General/interfaceLanguage'
config = json.loads(pathlib.Path(os.environ['ORV_MAC_PERSIST_CONFIG']).read_text(encoding='utf-8'))

def explicit_settings(identity):
    for key in ('organization', 'application'):
        assert isinstance(identity.get(key), str) and re.fullmatch(r'[A-Za-z0-9_.-]+', identity[key]), identity
    settings = QtCore.QSettings(QtCore.QSettings.NativeFormat, QtCore.QSettings.UserScope,
                               identity['organization'], identity['application'])
    settings.setFallbacksEnabled(False)
    settings.sync()
    assert settings.status() == QtCore.QSettings.NoError, settings.status()
    filename = pathlib.Path(settings.fileName()).resolve()
    preferences = pathlib.Path.home() / 'Library' / 'Preferences'
    assert filename.is_relative_to(preferences.resolve()) and filename.suffix == '.plist', str(filename)
    if identity.get('settingsFile'):
        assert str(filename) == identity['settingsFile'], (str(filename), identity['settingsFile'])
    return settings, str(filename)
'''

PROBE = EMBEDDED_COMMON + r'''
import traceback
from PySide6 import QtWidgets
app = QtWidgets.QApplication.instance()

def probe():
    try:
        identity = {'organization': app.organizationName(), 'application': app.applicationName()}
        settings, filename = explicit_settings(identity)
        present = settings.contains(KEY)
        value = settings.value(KEY) if present else None
        assert not present or isinstance(value, str), 'Original language preference is not a lossless JSON string'
        snapshot = dict(identity, schemaVersion=1, settingsFile=filename, present=present, value=value)
        # Exclusive creation and fsync precede the ONLY explicit preference mutation.
        with open(config['snapshot'], 'x', encoding='utf-8') as stream:
            json.dump(snapshot, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        settings.setValue(KEY, 'system')
        settings.sync()
        assert settings.status() == QtCore.QSettings.NoError, settings.status()
        print('MAC_PREFERENCE_SNAPSHOT_OK', flush=True)
        app.exit(0)
    except BaseException:
        traceback.print_exc()
        app.exit(1)

QtCore.QTimer.singleShot(200, probe)
QtCore.QTimer.singleShot(20000, lambda: app.exit(1))
'''

MAC_SETTINGS = r'''def hosted_settings():
    assert os.environ.get('GITHUB_ACTIONS') == 'true'
    assert os.environ.get('RUNNER_OS') == 'macOS'
    assert os.environ.get('RUNNER_ENVIRONMENT') == 'github-hosted'
    assert sys.platform == 'darwin'
    identity = config['settingsIdentity']
    assert app.organizationName() == identity['organization']
    assert app.applicationName() == identity['application']
    settings = QtCore.QSettings(QtCore.QSettings.NativeFormat, QtCore.QSettings.UserScope,
                               identity['organization'], identity['application'])
    settings.setFallbacksEnabled(False)
    settings.sync()
    assert settings.status() == QtCore.QSettings.NoError, settings.status()
    filename = pathlib.Path(settings.fileName()).resolve()
    assert str(filename) == identity['settingsFile'], (str(filename), identity['settingsFile'])
    assert filename.is_relative_to((pathlib.Path.home() / 'Library' / 'Preferences').resolve())
    result['settingsFile'] = str(filename)
    result['hostedRunnerPreferencesVerifiedBeforeAction'] = True
    result['settingsIsolationVerifiedBeforeAction'] = False
    return settings
'''


MAC_RESTART_NOTE = r'''def close_owned_restart_note():
    try:
        expected = ('Language preference saved. Save your work, then restart OpenRV to apply it.\n'
                    '\u754c\u9762\u8bed\u8a00\u8bbe\u7f6e\u5df2\u4fdd\u5b58\u3002\u8bf7\u5148\u4fdd\u5b58\u5de5\u4f5c\uff0c\u518d\u91cd\u65b0\u542f\u52a8 OpenRV \u4f7f\u5176\u751f\u6548\u3002')
        if config.get('overrideLanguage'):
            expected += ('\n\nRV_UI_LANGUAGE overrides this preference until it is removed.\n'
                         '\u5f53\u524d RV_UI_LANGUAGE \u73af\u5883\u53d8\u91cf\u4f18\u5148\u4e8e\u6b64\u8bbe\u7f6e\uff1b\u79fb\u9664\u8be5\u53d8\u91cf\u540e\u624d\u4f1a\u4f7f\u7528\u4fdd\u5b58\u7684\u9009\u62e9\u3002')
        for widget in app.topLevelWidgets():
            if not isinstance(widget, QtWidgets.QMessageBox) or not widget.isVisible():
                continue
            if not belongs_to_language_menu(widget, state['menu']):
                continue
            if widget.text() != expected or widget.icon() != QtWidgets.QMessageBox.Information:
                continue
            if widget.standardButtons() != QtWidgets.QMessageBox.Ok:
                continue
            button = widget.button(QtWidgets.QMessageBox.Ok)
            if button is None or not button.isEnabled():
                continue
            if widget.buttons() != [button]:
                continue
            # QMessageBox ignores setWindowTitle on macOS. Match the owned,
            # exact native message and click its real OK button instead.
            note = {'title': widget.windowTitle(), 'text': widget.text(), 'closedByTest': False}
            state['closeTimer'].stop()
            button.click()
            note['closedByTest'] = True
            state['restartNote'] = note
            return
    except BaseException:
        finish(1, traceback.format_exc())
'''

MAC_CHECKPOINTS = r'''def checkpoint(phase):
    entry = {'stage': config['stage'], 'phase': phase, 'time': round(time.time(), 3)}
    path = pathlib.Path(config['progressReport'])
    temporary = path.with_name(path.name + '.tmp')
    try:
        temporary.write_text(json.dumps(entry, ensure_ascii=False) + '\n', encoding='utf-8')
        os.replace(temporary, path)
    except OSError:
        # Progress is diagnostic only; the original final report remains required.
        pass
    print('MAC_LANGUAGE_PERSISTENCE_CHECKPOINT ' + json.dumps(entry, ensure_ascii=False), flush=True)

checkpoint('boot')
'''


def gui_check_source():
    # Reuse all native menu/action/session assertions without changing Windows isolation.
    start = WINDOWS_GUI_CHECK.index('def isolated_settings():\n')
    end = WINDOWS_GUI_CHECK.index('\ndef save_screenshot(', start)
    source = WINDOWS_GUI_CHECK[:start] + MAC_SETTINGS + WINDOWS_GUI_CHECK[end:]
    source = source.replace('isolated_settings()', 'hosted_settings()')
    source = source.replace('import json, os, pathlib, time, traceback', 'import json, os, pathlib, sys, time, traceback')
    start = source.index('def close_owned_restart_note():\n')
    end = source.index('\ndef check_ready():', start)
    source = source[:start] + MAC_RESTART_NOTE + source[end:]
    start = source.index('\ndef finish(')
    source = source[:start] + '\n' + MAC_CHECKPOINTS + source[start:]
    # Cocoa's menu bar is parentless (RvApplication::m_macMenuBar), while the
    # session/rebuild target is the visible RvDocument. Do not use window children.
    replacements = [
        ("            if not window.isVisible():\n", "            if not window.isVisible() or not window.objectName().startswith('rv-session'):\n"),
        ("for bar in window.findChildren(QtWidgets.QMenuBar)",
         "for bar in app.allWidgets() if isinstance(bar, QtWidgets.QMenuBar)"),
        ("menus = [menu for menu in window.findChildren(QtWidgets.QMenu)\n",
         "menus = [menu for menu in app.allWidgets() if isinstance(menu, QtWidgets.QMenu)\n"),
        ("current_menus = [item for item in window.findChildren(QtWidgets.QMenu)\n",
         "current_menus = [item for item in app.allWidgets() if isinstance(item, QtWidgets.QMenu)\n"),
        ("    state['finished'] = True\n",
         "    state['finished'] = True\n    checkpoint('finish')\n"),
        ("        result['preferenceBeforeAction'] = before\n",
         "        result['preferenceBeforeAction'] = before\n        checkpoint('ready')\n"),
        ("            action.trigger()  # Native QAction callback opens its own nested QMessageBox loop.\n",
         "            checkpoint('before-action')\n            action.trigger()  # Native QAction callback opens its own nested QMessageBox loop.\n"),
        ("            result['targetActionCheckedAfterTrigger'] = action.isChecked()\n",
         "            result['targetActionCheckedAfterTrigger'] = action.isChecked()\n            checkpoint('after-action')\n"),
    ]
    for before, after in replacements:
        expected = 2 if before.startswith('for bar in ') else 1
        if source.count(before) != expected:
            raise RuntimeError('Shared chooser test changed; review the Mac menu adaptation')
        source = source.replace(before, after)
    return source


def partial_progress(path, stage):
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
        if (isinstance(value, dict) and set(value) == {'stage', 'phase', 'time'}
                and value['stage'] == stage and value['phase'] in
                {'boot', 'ready', 'before-action', 'after-action', 'finish'}
                and type(value['time']) in (int, float)):
            return value
    except (OSError, ValueError):
        pass
    return None


RESTORE = EMBEDDED_COMMON + r'''
# py-interp's C++ entry point already owns a QApplication.
app = QtCore.QCoreApplication.instance()
assert app is not None, 'Bundled interpreter did not initialize Qt'
snapshot = json.loads(pathlib.Path(config['snapshot']).read_text(encoding='utf-8'))
settings, filename = explicit_settings(snapshot)
assert snapshot['schemaVersion'] == 1 and type(snapshot['present']) is bool
assert not snapshot['present'] or isinstance(snapshot['value'], str)
if config['operation'] == 'restore':
    if snapshot['present']:
        settings.setValue(KEY, snapshot['value'])
    else:
        settings.remove(KEY)
    settings.sync()
    assert settings.status() == QtCore.QSettings.NoError, settings.status()
elif config['operation'] != 'verify':
    raise ValueError('Unknown preference recovery operation')
settings.sync()
assert settings.contains(KEY) == snapshot['present']
assert not snapshot['present'] or settings.value(KEY) == snapshot['value']
print('MAC_PREFERENCE_' + config['operation'].upper() + '_OK', flush=True)
'''


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime', required=True, type=Path)
    parser.add_argument('--work', required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        require_hosted_macos()
    except RuntimeError as error:
        parser.error(str(error))  # Before mkdir, snapshot, QApplication or subprocess.
    runtime, work = args.runtime.resolve(), args.work.resolve()
    workspace = Path(os.environ['GITHUB_WORKSPACE']).resolve()
    runner_temp = Path(os.environ['RUNNER_TEMP']).resolve()
    if (runtime.suffix != '.app' or not runtime.is_relative_to(workspace)
            or work == runtime or work.is_relative_to(runtime)
            or not any(work.is_relative_to(root) for root in (workspace, runner_temp)) or work.exists()):
        parser.error('Use an installed app in this checkout and a new external work directory owned by this runner')
    content = runtime / 'Contents'
    bindir, scripts = content / 'MacOS', content / 'Resources/English.lproj'
    executable = bindir / 'rv'
    if not executable.is_file():
        executable = bindir / 'RV'
    interpreter = bindir / 'py-interp'
    for path in (executable, interpreter, scripts / 'rvrc.mu', scripts / 'rvrc.py'):
        if not path.is_file():
            parser.error('Incomplete app runtime: ' + str(path))
    work.mkdir(parents=True, exist_ok=False)
    snapshot_path = work / 'original-language-key.json'
    report = {'schemaVersion': 1, 'status': 'failed', 'preferenceScope': 'ephemeral hosted runner app CFPreferences domain',
              'platform': sys.platform, 'launches': [], 'preferenceRecovery': {},
              'rvSha256': hashlib.sha256(executable.read_bytes()).hexdigest(),
              'startedAtUtc': datetime.datetime.now(datetime.timezone.utc).isoformat()}
    running = []

    def invoke(name, code, config, gui=True, language=''):
        require_hosted_macos()
        folder = work / name
        folder.mkdir()
        config_path = folder / 'config.json'
        config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding='utf-8')
        script_path = folder / 'check.py'
        script_path.write_text(code, encoding='utf-8')
        environment = os.environ.copy()
        for key in ('PYTHONHOME', 'PYTHONPATH', 'PYTHONSTARTUP', 'QT_PLUGIN_PATH', 'QT_QPA_PLATFORM_PLUGIN_PATH',
                    'QT_QPA_PLATFORM', 'QT_QPA_PLATFORMTHEME', 'OCIO', 'RV_INIT', 'RV_PYINIT', 'RVIO_INIT', 'RV_UI_LANGUAGE',
                    'RV_PREFS_OVERRIDE_PATH', 'RV_PREFS_CLOBBER_PATH', 'MU_MODULE_PATH', 'QML_IMPORT_PATH', 'QML2_IMPORT_PATH',
                    'RV_ALLOW_SITE_PYTHONHOME', 'RV_PYTHONPATH_APPEND_ONLY', 'TWK_APP_SUPPORT_PATH', 'TWK_PROFILE_PLUGIN_PATH',
                    'TWK_FB_PLUGIN_PATH', 'TWK_MOVIE_PLUGIN_PATH', 'TWK_NODE_PLUGIN_PATH', 'TWK_OUTPUT_PLUGIN_PATH',
                    'TWK_MEDIA_LIBRARY_PLUGIN_PATH', 'OIIO_LIBRARY_PATH', 'DYLD_LIBRARY_PATH', 'DYLD_FRAMEWORK_PATH',
                    'DYLD_FALLBACK_LIBRARY_PATH', 'DYLD_FALLBACK_FRAMEWORK_PATH', 'CFFIXED_USER_HOME',
                    'GH_TOKEN', 'GITHUB_TOKEN'):
            environment.pop(key, None)
        for key, name_part in (('TEMP', 'temp'), ('TMP', 'temp'), ('RV_SUPPORT_PATH', 'support'), ('RV_CRASH_DUMPS_DIR', 'crashes')):
            destination = folder / name_part
            destination.mkdir(exist_ok=True)
            environment[key] = str(destination)
        environment.update(PYTHONNOUSERSITE='1', PYTHONDONTWRITEBYTECODE='1', RV_NO_CONSOLE_REDIRECT='1',
                           RV_INIT=str(scripts / 'rvrc.mu'), RV_PYINIT=str(scripts / 'rvrc.py'),
                           ORV_MAC_PERSIST_CONFIG=str(config_path), ORV_LANGUAGE_PERSIST_CONFIG=str(config_path))
        if language:
            environment['RV_UI_LANGUAGE'] = language
        # This is a child-only PATH. HOME and system/user settings are never redirected.
        environment['PATH'] = os.pathsep.join((str(bindir), '/usr/bin', '/bin', '/usr/sbin', '/sbin'))
        expression = f"exec(compile(open({str(script_path)!r}, encoding='utf-8').read(), {str(script_path)!r}, 'exec'))"
        command = ([str(executable), '-init', str(scripts / 'rvrc.mu'), '-pyeval', expression] if gui
                   else [str(interpreter), '-B', '-s', '-c', expression])
        started = time.monotonic()
        process = subprocess.Popen(command, cwd=work, env=environment, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, start_new_session=True)
        running.append(process)
        timed_out = False
        try:
            try:
                stdout, stderr = process.communicate(timeout=90 if gui else 25)
            except subprocess.TimeoutExpired:
                timed_out = True
                if process.poll() is None:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)  # Only this owned new process group.
                    except ProcessLookupError:
                        pass
                stdout, stderr = process.communicate(timeout=10)
        finally:
            # Also covers KeyboardInterrupt, pipe errors and recovery launches.
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.communicate(timeout=10)
            if process.poll() is not None:
                running.remove(process)
        stdout_text, stderr_text = (value.decode('utf-8', errors='replace') for value in (stdout, stderr))
        (folder / 'stdout.log').write_text(stdout_text, encoding='utf-8')
        (folder / 'stderr.log').write_text(stderr_text, encoding='utf-8')
        if timed_out or process.returncode:
            print(stdout_text[-4000:], flush=True)
            print(stderr_text[-4000:], file=sys.stderr, flush=True)
        result = {'stage': name, 'exitCode': process.returncode, 'timedOut': timed_out,
                  'seconds': round(time.monotonic() - started, 3)}
        return result, stdout_text

    try:
        result, stdout = invoke('00-snapshot', PROBE, {'snapshot': str(snapshot_path)}, language='en')
        report['initialization'] = result
        if result['timedOut'] or result['exitCode'] != 0 or 'MAC_PREFERENCE_SNAPSHOT_OK' not in stdout:
            raise RuntimeError('Could not snapshot and initialize the hosted app language key')
        identity = json.loads(snapshot_path.read_text(encoding='utf-8'))
        stages = [
            {'stage': '01-chinese-select-english', 'expectedLanguage': 'zh_CN', 'expectedPreference': 'system',
             'overrideLanguage': 'zh_CN', 'selectLanguage': 'en'},
            {'stage': '02-english-select-chinese', 'expectedLanguage': 'en', 'expectedPreference': 'en', 'selectLanguage': 'zh_CN'},
            {'stage': '03-chinese-persisted', 'expectedLanguage': 'zh_CN', 'expectedPreference': 'zh_CN'},
        ]
        for stage in stages:
            folder = work / stage['stage']
            config = dict(stage, workPath=str(work), settingsIdentity=identity,
                          childReport=str(folder / 'gui.json'), progressReport=str(folder / 'progress.json'))
            result, stdout = invoke(stage['stage'], gui_check_source(), config, language=stage.get('overrideLanguage', ''))
            child = folder / 'gui.json'
            try:
                details = json.loads(child.read_text(encoding='utf-8')) if child.is_file() else None
            except (OSError, ValueError) as error:
                details = None
                result['childReportError'] = type(error).__name__ + ': ' + str(error)[:500]
            passed = (not result['timedOut'] and result['exitCode'] == 0 and 'LANGUAGE_PERSISTENCE_STAGE_OK' in stdout
                      and isinstance(details, dict) and details.get('status') == 'passed'
                      and details.get('hostedRunnerPreferencesVerifiedBeforeAction') is True)
            result.update(status='passed' if passed else 'failed', details=details)
            report['launches'].append(result)
            print(result['status'] + ': ' + stage['stage'], flush=True)
            if not passed:
                progress = partial_progress(folder / 'progress.json', stage['stage'])
                diagnostic = {key: result[key] for key in ('stage', 'exitCode', 'timedOut', 'seconds')}
                diagnostic['partialPhase'] = progress['phase'] if progress else None
                print('MAC_LANGUAGE_PERSISTENCE_RESULT ' + json.dumps(diagnostic, ensure_ascii=False), flush=True)
                print(json.dumps(details, ensure_ascii=False)[-4000:], flush=True)
                raise RuntimeError('Native language persistence failed: ' + stage['stage'])
        report['status'] = 'passed'
    except BaseException as error:
        report['error'] = type(error).__name__ + ': ' + str(error)
    finally:
        # Finish every owned launch before restoration, including exceptions outside communicate.
        unsettled = []
        for process in running:
            try:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGKILL)
                process.communicate(timeout=10)
            except BaseException:
                unsettled.append(process.pid)
        # Probe writes this before mutation, even when it subsequently fails.
        if unsettled:
            report['status'] = 'failed'
            report['preferenceRecovery']['restore'] = {'status': 'failed', 'reason': 'Owned GUI process did not exit', 'ownedPids': unsettled}
        elif snapshot_path.exists():
            for operation in ('restore', 'verify'):
                try:
                    recovered, stdout = invoke('recovery-' + operation, RESTORE,
                                               {'snapshot': str(snapshot_path), 'operation': operation}, gui=False)
                    passed = (not recovered['timedOut'] and recovered['exitCode'] == 0
                              and 'MAC_PREFERENCE_' + operation.upper() + '_OK' in stdout)
                    recovered['status'] = 'passed' if passed else 'failed'
                    report['preferenceRecovery'][operation] = recovered
                    if not passed:
                        report['status'] = 'failed'
                except BaseException as error:
                    report['status'] = 'failed'
                    report['preferenceRecovery'][operation] = {'status': 'failed', 'error': type(error).__name__ + ': ' + str(error)}
                    if running:
                        # Do not run a read-back verifier concurrently with a still-live restore process.
                        report['preferenceRecovery']['verify'] = {'status': 'failed', 'reason': 'Owned recovery process did not exit'}
                        break
        else:
            report['preferenceRecovery']['restore'] = {'status': 'not-needed', 'reason': 'No preference snapshot/mutation was made'}
        if hashlib.sha256(executable.read_bytes()).hexdigest() != report['rvSha256']:
            report.update(status='failed', error='RV executable changed during persistence validation')
        report['finishedAtUtc'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        if report.get('error'):
            print(report['error'], file=sys.stderr, flush=True)
        if report['status'] != 'passed':
            recovery = {operation: {key: report['preferenceRecovery'].get(operation, {}).get(key)
                                    for key in ('status', 'exitCode', 'timedOut', 'seconds')}
                        for operation in ('restore', 'verify')}
            print('MAC_LANGUAGE_PREFERENCE_RECOVERY ' + json.dumps(recovery, ensure_ascii=False), flush=True)
        (work / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        print(json.dumps({'status': report['status'], 'report': str(work / 'report.json')}, ensure_ascii=False), flush=True)
    return 0 if report['status'] == 'passed' else 1


if __name__ == '__main__':
    sys.exit(main())
