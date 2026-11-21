"""Check built-in view labels, stable action data and custom names in a real RV window.

Windows uses a private INI preference directory. macOS runs only on a disposable
GitHub-hosted runner; ordinary local macOS execution is refused before any write.
The graph mutations affect only the test's unsaved, otherwise empty session.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys


def check_default_view_labels():
    """Check the caller's already visible, isolated RV session and restore its graph."""
    import os
    from pathlib import Path
    from PySide6 import QtCore, QtGui, QtWidgets
    from rv import commands

    app = QtWidgets.QApplication.instance()
    report = {'status': 'failed', 'language': app.property('rvInterfaceLanguage')}
    assert report['language'] in ('en', 'zh_CN')
    assert any(widget.isVisible() for widget in app.topLevelWidgets()), 'No visible RV window'
    if os.name == 'nt':
        settings = QtCore.QSettings(QtCore.QSettings.defaultFormat(), QtCore.QSettings.UserScope,
                                   app.organizationName(), app.applicationName())
        filename = Path(settings.fileName()).resolve()
        assert QtCore.QSettings.defaultFormat() == QtCore.QSettings.IniFormat
        assert filename.is_relative_to(Path(os.environ['ORV_DEFAULT_VIEW_PREFS']).resolve()), str(filename)
        report['privatePreferenceDirectoryVerified'] = True
    else:
        assert os.environ.get('GITHUB_ACTIONS') == 'true' and os.environ.get('RUNNER_OS') == 'macOS'
        assert os.environ.get('RUNNER_ENVIRONMENT') == 'github-hosted'
        report['disposableHostedMacVerified'] = True
    toolbar = next((widget for widget in app.allWidgets()
                    if isinstance(widget, QtWidgets.QToolBar) and widget.objectName() == 'topToolBar'), None)
    assert toolbar is not None, 'The native view toolbar did not initialize'
    menu = toolbar.findChild(QtWidgets.QMenu, 'rvViewSelectorMenu')
    header = toolbar.findChild(QtGui.QAction, 'rvViewSelectorAction')
    assert menu is not None and header is not None, 'Stable view-selector objects are missing'
    defaults = {'defaultLayout': 'Default Layout', 'defaultSequence': 'Default Sequence', 'defaultStack': 'Default Stack'}
    chinese = {'defaultLayout': '默认布局', 'defaultSequence': '默认序列', 'defaultStack': '默认堆叠'}
    def actions():
        assert QtCore.QMetaObject.invokeMethod(toolbar, 'viewMenuUpdate', QtCore.Qt.DirectConnection)
        return {action.data(): action for action in menu.actions()}
    original_view = commands.viewNode()
    original_names = {node: commands.getStringProperty(node + '.ui.name') for node in defaults}
    custom_node = None
    try:
        assert all(original_names[node] == [label] for node, label in defaults.items()), original_names
        expected = chinese if report['language'] == 'zh_CN' else defaults
        report['defaultViews'] = []
        for node, label in expected.items():
            by_node = actions()
            assert by_node[node].text() == label, (node, label, by_node[node].text())
            assert commands.getStringProperty(node + '.ui.name') == [defaults[node]], 'Display translation changed graph data'
            by_node[node].trigger()
            assert commands.viewNode() == node, 'Translated action selected a different node'
            by_node = actions()
            assert by_node[node].isChecked() and header.text().strip() == label
            report['defaultViews'].append({'node': node, 'display': label, 'actionData': by_node[node].data()})
        custom_name = 'User view / 自定义'
        commands.setStringProperty('defaultSequence.ui.name', [custom_name])
        commands.setViewNode('defaultSequence')
        assert actions()['defaultSequence'].text() == custom_name and header.text().strip() == custom_name
        commands.setStringProperty('defaultLayout.ui.name', ['Default Sequence'])
        assert actions()['defaultLayout'].text() == 'Default Sequence'
        custom_node = commands.newNode('RVSequenceGroup', 'defaultLabelRegressionView')
        if not commands.propertyExists(custom_node + '.ui.name'):
            commands.newProperty(custom_node + '.ui.name', commands.StringType, 1)
        commands.setStringProperty(custom_node + '.ui.name', ['Default Sequence'])
        assert actions()[custom_node].text() == 'Default Sequence'
        actions()[custom_node].trigger()
        actions()
        assert commands.viewNode() == custom_node and header.text().strip() == 'Default Sequence'
        report['customNamesPreserved'] = True
        report['internalNamesPreserved'] = True
    finally:
        commands.setViewNode(original_view)
        for node, value in original_names.items():
            commands.setStringProperty(node + '.ui.name', value)
        if custom_node:
            commands.deleteNode(custom_node)
        assert QtCore.QMetaObject.invokeMethod(toolbar, 'viewMenuUpdate', QtCore.Qt.DirectConnection)
    report['status'] = 'passed'
    return report


GUI_CHECK = r'''
import json, os, pathlib, runpy, time, traceback
from PySide6 import QtCore, QtWidgets
app = QtWidgets.QApplication.instance()
deadline = time.monotonic() + 25
def check():
    toolbar = next((widget for widget in app.allWidgets()
                    if isinstance(widget, QtWidgets.QToolBar) and widget.objectName() == 'topToolBar'), None)
    if toolbar is None and time.monotonic() < deadline:
        QtCore.QTimer.singleShot(200, check)
        return
    try:
        fixture = runpy.run_path(os.environ['ORV_DEFAULT_VIEW_FIXTURE'])
        report = fixture['check_default_view_labels']()
        code = 0
    except BaseException:
        report = {'status': 'failed', 'language': os.environ['RV_UI_LANGUAGE'], 'error': traceback.format_exc()}
        code = 1
    pathlib.Path(os.environ['ORV_DEFAULT_VIEW_REPORT']).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print('DEFAULT_VIEW_LABELS_OK' if code == 0 else 'DEFAULT_VIEW_LABELS_FAILED', flush=True)
    app.exit(code)
QtCore.QTimer.singleShot(300, check)
'''



def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime', type=Path, required=True)
    parser.add_argument('--work', type=Path, required=True)
    args = parser.parse_args()
    hosted_mac = (sys.platform == 'darwin' and os.environ.get('GITHUB_ACTIONS') == 'true'
                  and os.environ.get('RUNNER_OS') == 'macOS' and os.environ.get('RUNNER_ENVIRONMENT') == 'github-hosted')
    if os.name != 'nt' and not hosted_mac:
        parser.error('Only Windows with private INI preferences or disposable GitHub-hosted macOS is supported')
    runtime, work = args.runtime.resolve(), args.work.resolve()
    if work.exists() or work == runtime or work.is_relative_to(runtime):
        parser.error('--work must be a new directory outside the installed runtime')
    content = runtime / 'Contents' if hosted_mac else runtime
    executable = content / ('MacOS/RV' if hosted_mac else 'bin/rv.exe')
    scripts = content / ('Resources/English.lproj' if hosted_mac else 'scripts/rv')
    if not executable.is_file(): parser.error('Installed RV executable is missing')
    work.mkdir(parents=True)
    fixture = work / 'default-view-fixture.py'
    fixture.write_text(Path(__file__).read_text(encoding='utf-8'), encoding='utf-8')
    script = work / 'gui-check.py'
    script.write_text(GUI_CHECK, encoding='utf-8')
    report = {'status': 'failed', 'rvSha256': hashlib.sha256(executable.read_bytes()).hexdigest(), 'launches': []}
    try:
        for language in ('en', 'zh_CN'):
            folder = work / language
            folder.mkdir()
            environment = os.environ.copy()
            for key in ('PYTHONHOME', 'PYTHONPATH', 'PYTHONSTARTUP', 'QT_PLUGIN_PATH', 'QT_QPA_PLATFORM_PLUGIN_PATH',
                        'QT_QPA_PLATFORM', 'RV_UI_LANGUAGE', 'OCIO', 'RV_INIT', 'RV_PYINIT', 'RVIO_INIT',
                        'RV_PREFS_OVERRIDE_PATH', 'RV_PREFS_CLOBBER_PATH', 'MU_MODULE_PATH',
                        'DYLD_LIBRARY_PATH', 'DYLD_FRAMEWORK_PATH', 'DYLD_FALLBACK_LIBRARY_PATH', 'DYLD_FALLBACK_FRAMEWORK_PATH'):
                environment.pop(key, None)
            for key in ('APPDATA', 'LOCALAPPDATA', 'TEMP', 'TMP', 'RV_SUPPORT_PATH', 'RV_CRASH_DUMPS_DIR'):
                target = folder / key
                target.mkdir()
                environment[key] = str(target)
            prefs = folder / 'prefs'
            prefs.mkdir()
            environment.update(RV_UI_LANGUAGE=language, PYTHONNOUSERSITE='1', PYTHONDONTWRITEBYTECODE='1', RV_NO_CONSOLE_REDIRECT='1',
                               RV_INIT=str(scripts / 'rvrc.mu'), RV_PYINIT=str(scripts / 'rvrc.py'),
                               ORV_DEFAULT_VIEW_REPORT=str(folder / 'gui.json'), ORV_DEFAULT_VIEW_PREFS=str(prefs),
                               ORV_DEFAULT_VIEW_FIXTURE=str(fixture))
            windows_root = Path(os.environ['SystemRoot']) if os.name == 'nt' else None
            environment['PATH'] = os.pathsep.join([str(executable.parent)] +
                ([str(windows_root / 'System32'), str(windows_root)] if windows_root else ['/usr/bin', '/bin', '/usr/sbin', '/sbin']))
            expression = f"exec(compile(open({str(script)!r},encoding='utf-8').read(),{str(script)!r},'exec'))"
            process = subprocess.Popen([str(executable), '-noPrefs', '-prefsPath', str(prefs), '-init', str(scripts / 'rvrc.mu'),
                                        '-pyeval', expression], cwd=work, env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       start_new_session=hosted_mac)
            try:
                stdout, stderr = process.communicate(timeout=45)
            except subprocess.TimeoutExpired:
                if process.poll() is None:
                    if hosted_mac: os.killpg(process.pid, signal.SIGKILL)
                    else: subprocess.run([str(windows_root / 'System32/taskkill.exe'), '/PID', str(process.pid), '/T', '/F'], capture_output=True, timeout=10)
                stdout, stderr = process.communicate(timeout=10)
            (folder / 'stdout.log').write_bytes(stdout)
            (folder / 'stderr.log').write_bytes(stderr)
            details_path = folder / 'gui.json'
            details = json.loads(details_path.read_text(encoding='utf-8')) if details_path.is_file() else None
            passed = process.returncode == 0 and details and details.get('status') == 'passed' and b'DEFAULT_VIEW_LABELS_OK' in stdout
            report['launches'].append({'language': language, 'status': 'passed' if passed else 'failed', 'details': details, 'exitCode': process.returncode})
            print(('passed' if passed else 'failed') + ': default view labels ' + language, flush=True)
            if not passed: return 1
        if hashlib.sha256(executable.read_bytes()).hexdigest() != report['rvSha256']:
            raise RuntimeError('RV changed during the default-label checks')
        report['status'] = 'passed'
        return 0
    finally:
        (work / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


if __name__ == '__main__':
    raise SystemExit(main())
