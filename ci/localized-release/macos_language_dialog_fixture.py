"""Real Cocoa dialog-mechanism probe; never certifies product or preferences."""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
from pathlib import Path
import platform
import signal
import subprocess
import sys
import time
import traceback

HOST = {'GITHUB_ACTIONS': 'true', 'RUNNER_OS': 'macOS', 'RUNNER_ENVIRONMENT': 'github-hosted'}
TITLE = 'Restart required / \u9700\u8981\u91cd\u65b0\u542f\u52a8'
MESSAGE = ('Language preference saved. Save your work, then restart OpenRV to apply it.\n'
           '\u754c\u9762\u8bed\u8a00\u8bbe\u7f6e\u5df2\u4fdd\u5b58\u3002\u8bf7\u5148\u4fdd\u5b58\u5de5\u4f5c\uff0c\u518d\u91cd\u65b0\u542f\u52a8 OpenRV \u4f7f\u5176\u751f\u6548\u3002')
OVERRIDE = ('\n\nRV_UI_LANGUAGE overrides this preference until it is removed.\n'
            '\u5f53\u524d RV_UI_LANGUAGE \u73af\u5883\u53d8\u91cf\u4f18\u5148\u4e8e\u6b64\u8bbe\u7f6e\uff1b\u79fb\u9664\u8be5\u53d8\u91cf\u540e\u624d\u4f1a\u4f7f\u7528\u4fdd\u5b58\u7684\u9009\u62e9\u3002')
MESSAGE_SHA = {'normal': '2f1ede3228216392e18abe25d6b0c87ffa65eaa4754e79a1766bce8a1ae39c0c',
               'override': '2721c4dc7b80f7fa9b6ea5b348962eee07c59b16ede93bd5d25f1642f258ad0f'}
CASES = ('old-normal', 'old-override', 'fixed-normal', 'fixed-override')


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def require_host(arch):
    require(sys.platform == 'darwin' and platform.machine() == arch
            and all(os.environ.get(key) == value for key, value in HOST.items()),
            'This probe requires an actual native GitHub-hosted macOS runner')


def extract_helpers(windows_path, mac_path):
    """Only compile the two actual helpers; no RV, QSettings or GUI code runs."""
    windows_raw, mac_raw = windows_path.read_bytes(), mac_path.read_bytes()
    windows_tree, mac_tree = ast.parse(windows_raw), ast.parse(mac_raw)

    def literals(tree):
        values = {}
        for node in tree.body:
            if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                try:
                    values[node.targets[0].id] = ast.literal_eval(node.value)
                except (ValueError, TypeError):
                    pass
        return values

    old_program = literals(windows_tree)['GUI_CHECK']
    values = literals(mac_tree)
    values['WINDOWS_GUI_CHECK'] = old_program
    generator = [node for node in mac_tree.body if isinstance(node, ast.FunctionDef) and node.name == 'gui_check_source']
    require(len(generator) == 1, 'Expected the real Mac GUI source generator')
    namespace = dict(values)
    exec(compile(ast.Module(body=generator, type_ignores=[]), str(mac_path), 'exec'), namespace)
    fixed_program = namespace['gui_check_source']()
    result = {}
    identities = {'windowsSourceSha256': digest(windows_raw), 'macSourceSha256': digest(mac_raw)}
    for name, source in (('old', old_program), ('fixed', fixed_program)):
        tree = ast.parse(source)
        wanted = ('belongs_to_language_menu', 'close_owned_restart_note')
        functions = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in wanted]
        require(len(functions) == 2 and {node.name for node in functions} == set(wanted),
                'Expected exactly the actual ownership and dialog-close helpers')
        require(not any(isinstance(node, ast.Name) and node.id in
                        {'QSettings', 'hosted_settings', 'isolated_settings', 'commands'}
                        for function in functions for node in ast.walk(function)),
                'The extracted dialog helpers must not access preferences or RV')
        module = ast.Module(body=functions, type_ignores=[])
        result[name] = compile(module, str(mac_path if name == 'fixed' else windows_path), 'exec')
        identities[name + 'HelperAstSha256'] = digest(ast.dump(module, include_attributes=False).encode())
        identities[name + 'GeneratedSourceSha256'] = digest(source.encode())
    return result, identities


def paths(args, child):
    repo = args.repo.resolve(strict=True)
    temporary = Path(os.environ['RUNNER_TEMP']).resolve(strict=True)
    require(repo == Path(os.environ['GITHUB_WORKSPACE']).resolve(strict=True),
            'Use this immutable Actions checkout')
    bindings = args.bindings.resolve(strict=True)
    work, output = args.work.resolve(), args.output.resolve()
    require(bindings.is_relative_to(temporary) and bindings.is_dir(), 'Bindings must be in the private runner temp directory')
    require(work != temporary and work.is_relative_to(temporary), 'Work must be private runner temp storage')
    require(output != repo / '_release_work' and output.is_relative_to(repo / '_release_work'),
            'Output must be a private release-work directory')
    if child:
        require(work.is_dir() and output.is_dir(), 'Parent-owned work and output directories are missing')
    else:
        require(not work.exists() and not output.exists(), 'Use fresh private work and output directories')
    return repo, bindings, work, output


def run_child(args):
    require_host(args.arch)
    repo, bindings, work, output = paths(args, True)
    case = args.child_case
    fixed, override = case.startswith('fixed-'), case.endswith('-override')
    helper_codes, identities = extract_helpers(repo / 'tests/localization/language_persistence.py',
                                               repo / 'tests/localization/macos_language_persistence.py')
    report_path = output / (case + '.json')
    report = {'check': 'native-macos-language-dialog-fixture', 'case': case, 'status': 'running',
              'architecture': args.arch, 'productRuntimeVerified': False, 'persistenceVerified': False,
              'helperIdentities': identities, 'actionTriggeredCount': 0, 'groupTriggeredCount': 0}
    state = {'finished': False, 'restartNote': None, 'observed': False, 'safetyClicked': False,
             'callbackReturned': False, 'triggerReturned': False}

    def save(phase):
        report['phase'] = phase
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        print('MAC_DIALOG_CHECKPOINT ' + json.dumps({'case': case, 'phase': phase}, sort_keys=True), flush=True)

    save('before-qt-import')
    sys.path.insert(0, str(bindings))
    import PySide6
    import shiboken6
    from PySide6 import QtCore, QtGui, QtWidgets
    require(Path(PySide6.__file__).resolve().is_relative_to(bindings)
            and Path(shiboken6.__file__).resolve().is_relative_to(bindings),
            'Qt bindings escaped the isolated official wheel target')
    require(PySide6.__version__ == shiboken6.__version__ == QtCore.qVersion() == '6.5.3',
            'Expected actual Qt/PySide/shiboken 6.5.3')
    require(not QtCore.QCoreApplication.testAttribute(QtCore.Qt.AA_DontUseNativeDialogs),
            'Native dialogs must not be disabled')
    app = QtWidgets.QApplication(['openrv-native-dialog-fixture'])
    require(app.platformName() == 'cocoa', 'The actual Cocoa platform is required')
    report.update(qtVersion=QtCore.qVersion(), pysideVersion=PySide6.__version__,
                  platformPlugin=app.platformName(), nativeDialogsDisabled=False)
    window = QtWidgets.QMainWindow()
    window.setObjectName('openrv-owned-dialog-fixture')
    window.setWindowTitle('OpenRV native dialog mechanism fixture')
    window.resize(420, 240)
    menu = QtWidgets.QMenu('Language / \u8bed\u8a00', window)
    window.menuBar().addMenu(menu)
    group = QtGui.QActionGroup(menu)
    group.setExclusive(True)
    action = menu.addAction('Diagnostic language selection')
    action.setCheckable(True)
    group.addAction(action)
    message = MESSAGE + (OVERRIDE if override else '')
    require(digest(message.encode()) == MESSAGE_SHA['override' if override else 'normal'],
            'Dialog text differs from the actual product source contract')
    report.update(expectedMessageSha256=digest(message.encode()), messageUtf8Bytes=len(message.encode()),
                  overrideMessageCase=override)
    config = {'overrideLanguage': 'zh_CN'} if override else {}
    observer = QtCore.QTimer(app)
    observer.setInterval(40)
    state.update(menu=menu, closeTimer=observer)

    def owned_dialog():
        found = []
        for widget in app.topLevelWidgets():
            if not isinstance(widget, QtWidgets.QMessageBox) or not widget.isVisible():
                continue
            parent = widget.parent()
            while parent is not None and parent != menu:
                parent = parent.parent()
            if parent != menu or widget.text() != message or widget.icon() != QtWidgets.QMessageBox.Information:
                continue
            button = widget.button(QtWidgets.QMessageBox.Ok)
            if widget.standardButtons() == QtWidgets.QMessageBox.Ok and button is not None and button.isEnabled() and widget.buttons() == [button]:
                found.append((widget, button))
        require(len(found) <= 1, 'More than one strictly owned restart notice exists')
        return found[0] if found else None

    def safety_cleanup(reason):
        owned = owned_dialog()
        require(owned is not None, 'Independent safety cleanup found no strictly owned Information/OK dialog')
        dialog, button = owned
        require(dialog.isVisible() and button.isEnabled(), 'Owned OK button is unavailable')
        report['safetyCleanupReason'] = reason
        state['safetyClicked'] = True
        button.click()
        report['safetyCleanupClickedRealOk'] = True
        save('safety-cleanup-clicked-ok')

    def cleanup_expected_negative():
        try:
            safety_cleanup('expected-old-matcher-failure')
        except BaseException:
            finish(1, traceback.format_exc())

    def finish(code, error=None):
        if state['finished']:
            return
        state['finished'] = True
        observer.stop()
        report.update(status='passed' if code == 0 else 'failed',
                      callbackReturned=state['callbackReturned'], triggerReturned=state['triggerReturned'])
        if error:
            report['error'] = str(error)[:2500]
        save('finished')
        if code and owned_dialog() is not None:
            try:
                safety_cleanup('failed-probe-cleanup-only')
            except BaseException as cleanup_error:
                report['cleanupError'] = str(cleanup_error)[:1000]
                save('failed-cleanup')
        QtCore.QTimer.singleShot(0, lambda: app.exit(code))

    namespace = {'QtWidgets': QtWidgets, 'app': app, 'state': state, 'config': config,
                 'traceback': traceback, 'finish': finish}
    exec(helper_codes['fixed' if fixed else 'old'], namespace)
    close_actual_helper = namespace['close_owned_restart_note']

    def check_complete():
        try:
            require(state['observed'] and state['callbackReturned'] and state['triggerReturned'],
                    'The actual native callback/trigger did not return')
            require(report['actionTriggeredCount'] == report['groupTriggeredCount'] == 1,
                    'QAction/QActionGroup forwarding did not run exactly once')
            require(window.isVisible() and window.windowHandle() is not None and window.windowHandle().isExposed(),
                    'The owned native main window did not remain visible')
            if fixed:
                note = state['restartNote']
                require(note and note['closedByTest'] is True and note['text'] == message,
                        'The actual fixed helper did not close its exact owned message')
                require(not state['safetyClicked'], 'A safety cleanup cannot substitute for the fixed helper')
                report['fixedHelperClosedRealOk'] = True
            else:
                require(report.get('oldHelperRejectedIgnoredTitle') is True and state['safetyClicked']
                        and state['restartNote'] is None, 'The original title matcher did not reproduce its expected failure')
                report['expectedOldFailureReproduced'] = True
            report['mainWindowStillVisibleAndExposed'] = True
            finish(0)
        except BaseException:
            finish(1, traceback.format_exc())

    def observe():
        if state['finished'] or state['observed']:
            return
        try:
            owned = owned_dialog()
            if owned is None:
                return
            dialog, button = owned
            native_proxy = dialog.testAttribute(QtCore.Qt.WA_DontShowOnScreen)
            require(native_proxy, 'The default Cocoa native dialog proxy was not used')
            require(dialog.windowTitle() != TITLE, 'The old macOS title mismatch did not actually occur')
            report.update(actualTitle=dialog.windowTitle(), titleIgnored=True, nativeDialogProxyObserved=True,
                          actualTextSha256=digest(dialog.text().encode()),
                          actualIcon=str(dialog.icon()), actualStandardButtons=str(dialog.standardButtons()),
                          actualDefaultButton=None if dialog.defaultButton() is None else dialog.defaultButton().text())
            state['observed'] = True
            save('actual-native-dialog-observed')
            close_actual_helper()
            if state['finished']:
                return
            if fixed:
                require(state['restartNote'] and state['restartNote']['closedByTest'] is True,
                        'The fixed helper did not accept this actual native title/body/owner')
                save('actual-fixed-helper-returned')
            else:
                require(state['restartNote'] is None and dialog.isVisible() and not state['callbackReturned'],
                        'The original title matcher unexpectedly closed the native message')
                report['oldHelperRejectedIgnoredTitle'] = True
                observer.stop()
                save('old-helper-did-not-close')
                QtCore.QTimer.singleShot(100, cleanup_expected_negative)
        except BaseException:
            finish(1, traceback.format_exc())

    def on_group(triggered_action):
        try:
            require(triggered_action == action, 'An unrelated QAction reached the diagnostic callback')
            report['groupTriggeredCount'] += 1
            save('before-production-information-call')
            QtWidgets.QMessageBox.information(menu, TITLE, message)
            state['callbackReturned'] = True
            save('native-information-callback-returned')
            QtCore.QTimer.singleShot(80, check_complete)
        except BaseException:
            finish(1, traceback.format_exc())

    def on_action(*unused):
        report['actionTriggeredCount'] += 1

    visible_deadline = time.monotonic() + 5

    def trigger():
        try:
            visible = window.isVisible() and window.windowHandle() is not None and window.windowHandle().isExposed()
            if not visible and time.monotonic() < visible_deadline:
                QtCore.QTimer.singleShot(100, trigger)
                return
            require(visible, 'The real Cocoa window was not visible/exposed before the action')
            report['initialWindowVisibleAndExposed'] = True
            action.trigger()
            state['triggerReturned'] = True
        except BaseException:
            finish(1, traceback.format_exc())

    group.triggered.connect(on_group)
    action.triggered.connect(on_action)
    observer.timeout.connect(observe)
    observer.start()
    window.show()
    QtCore.QTimer.singleShot(600, trigger)
    QtCore.QTimer.singleShot(18000, lambda: finish(1, 'Native dialog probe exceeded its 18-second watchdog'))
    save('event-loop-starting')
    exit_code = app.exec()
    report['eventLoopExitCode'] = exit_code
    require(state['finished'] and report['status'] == 'passed' and exit_code == 0,
            report.get('error', 'Native dialog process did not finish successfully'))
    save('normal-exit')
    return 0


def validate_case(record, details, case, identities, arch):
    require(record['timedOut'] is False and type(record['exitCode']) is int and record['exitCode'] == 0,
            'Native child failed or timed out: ' + case)
    require(isinstance(details, dict) and details.get('check') == 'native-macos-language-dialog-fixture'
            and details.get('case') == case and details.get('status') == 'passed'
            and details.get('architecture') == arch
            and details.get('productRuntimeVerified') is False and details.get('persistenceVerified') is False
            and details.get('helperIdentities') == identities, 'Native report identity differs: ' + case)
    require(details.get('qtVersion') == details.get('pysideVersion') == '6.5.3'
            and details.get('platformPlugin') == 'cocoa' and details.get('nativeDialogsDisabled') is False,
            'The actual default Cocoa/Qt path was not used')
    for key in ('initialWindowVisibleAndExposed', 'mainWindowStillVisibleAndExposed', 'titleIgnored',
                'nativeDialogProxyObserved', 'callbackReturned', 'triggerReturned'):
        require(details.get(key) is True, 'Native requirement failed: ' + key)
    require(details.get('actionTriggeredCount') == details.get('groupTriggeredCount') == 1
            and details.get('eventLoopExitCode') == 0, 'Native signal/exit evidence failed')
    expected = MESSAGE_SHA['override' if case.endswith('-override') else 'normal']
    require(details.get('actualTextSha256') == details.get('expectedMessageSha256') == expected,
            'The actual displayed product body differs')
    if case.startswith('old-'):
        require(details.get('expectedOldFailureReproduced') is True
                and details.get('oldHelperRejectedIgnoredTitle') is True
                and details.get('safetyCleanupClickedRealOk') is True,
                'The old native title-matcher failure was not reproduced')
    else:
        require(details.get('fixedHelperClosedRealOk') is True and details.get('safetyCleanupClickedRealOk') is not True,
                'Only the actual fixed helper can satisfy a positive case')


def run_parent(args):
    require_host(args.arch)
    repo, bindings, work, output = paths(args, False)
    work.mkdir(parents=True)
    output.mkdir(parents=True)
    identities = {}
    summary = {'check': 'macos-language-dialog-mechanism-fixture', 'status': 'failed',
               'architecture': args.arch, 'productRuntimeVerified': False, 'persistenceVerified': False,
               'helperIdentities': identities, 'cases': {}}
    try:
        snapshot = subprocess.check_output(['git', '-C', str(repo), 'rev-parse', 'HEAD'],
                                           text=True, timeout=20).strip()
        require(snapshot == os.environ.get('GITHUB_SHA') and len(snapshot) == 40,
                'The native diagnostic checkout differs from its immutable workflow source')
        summary['controllerSourceSnapshot'] = snapshot
        helper_codes, identities = extract_helpers(repo / 'tests/localization/language_persistence.py',
                                                   repo / 'tests/localization/macos_language_persistence.py')
        del helper_codes
        summary['helperIdentities'] = identities
        for case in CASES:
            home = work / case
            home.mkdir()
            environment = os.environ.copy()
            for key in tuple(environment):
                if key.startswith(('DYLD_', 'QT_', 'QML_')) or key in (
                        'PYTHONHOME', 'PYTHONPATH', 'VIRTUAL_ENV', 'RV_UI_LANGUAGE', 'GH_TOKEN', 'GITHUB_TOKEN'):
                    environment.pop(key)
            environment.update(HOME=str(home), CFFIXED_USER_HOME=str(home),
                               PYTHONNOUSERSITE='1', PYTHONDONTWRITEBYTECODE='1')
            command = [sys.executable, '-I', '-B', str(Path(__file__).resolve()),
                       '--repo', str(repo), '--bindings', str(bindings), '--work', str(work),
                       '--output', str(output), '--arch', args.arch, '--child-case', case]
            record = {'timedOut': False, 'exitCode': None}
            summary['cases'][case] = record
            started = time.monotonic()
            process = subprocess.Popen(command, cwd=home, env=environment, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, start_new_session=True)
            stdout, stderr = b'', b''
            try:
                stdout, stderr = process.communicate(timeout=40)
            except subprocess.TimeoutExpired:
                record['timedOut'] = True
                if process.poll() is None:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                stdout, stderr = process.communicate(timeout=10)
            finally:
                if process.poll() is None:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.communicate(timeout=10)
            record.update(exitCode=process.returncode, seconds=round(time.monotonic() - started, 3))
            (output / (case + '.stdout.log')).write_bytes(stdout)
            (output / (case + '.stderr.log')).write_bytes(stderr)
            child = output / (case + '.json')
            details = json.loads(child.read_text(encoding='utf-8')) if child.is_file() else None
            record['details'] = details
            if process.returncode or record['timedOut']:
                record['stderrTail'] = stderr.decode('utf-8', errors='replace')[-512:]
            validate_case(record, details, case, identities, args.arch)
        summary['status'] = 'passed'
    except Exception as error:
        summary['error'] = type(error).__name__ + ': ' + str(error)
    (output / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    compact = {key: summary[key] for key in ('check', 'status', 'architecture', 'productRuntimeVerified', 'persistenceVerified')}
    compact['controllerSourceSnapshot'] = summary.get('controllerSourceSnapshot')
    compact['helperIdentities'] = identities
    compact['cases'] = {case: {'exitCode': value['exitCode'], 'timedOut': value['timedOut'], 'stderrTail': value.get('stderrTail'),
        'evidence': {key: ((value.get('details') or {}).get(key)[:300]
                           if key == 'error' and isinstance((value.get('details') or {}).get(key), str)
                           else (value.get('details') or {}).get(key)) for key in (
            'status', 'phase', 'actualTitle', 'titleIgnored', 'nativeDialogProxyObserved', 'actualTextSha256',
            'actionTriggeredCount', 'groupTriggeredCount', 'callbackReturned', 'triggerReturned',
            'expectedOldFailureReproduced', 'fixedHelperClosedRealOk', 'safetyCleanupClickedRealOk',
            'mainWindowStillVisibleAndExposed', 'eventLoopExitCode', 'error')}} for case, value in summary['cases'].items()}
    compact['error'] = summary.get('error', '')[:512] or None
    encoded = json.dumps(compact, ensure_ascii=True, sort_keys=True)
    require(len(encoded.encode()) <= 12 * 1024, 'Native dialog console proof exceeded 12 KiB')
    print('MACOS_DIALOG_PROBE_SUMMARY ' + encoded, flush=True)
    return 0 if summary['status'] == 'passed' else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', type=Path, required=True)
    parser.add_argument('--bindings', type=Path, required=True)
    parser.add_argument('--work', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--arch', choices=('arm64', 'x86_64'), required=True)
    parser.add_argument('--child-case', choices=CASES, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    require_host(args.arch)  # Before directories, processes, Qt or preferences.
    return run_child(args) if args.child_case else run_parent(args)


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception:
        traceback.print_exc()
        raise SystemExit(1)
