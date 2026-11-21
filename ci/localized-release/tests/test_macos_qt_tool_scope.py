"""Filesystem-only deployment-scope tests; no synthetic Mach-O/native pass claims."""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'ci/localized-release'))
SPEC = importlib.util.spec_from_file_location(
    'macos_qt_dependencies', ROOT / 'ci/localized-release/check-macos-qt-dependencies.py')
CHECKER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CHECKER)


class MacQtToolScopeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='openrv-qt-tool-scope-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.qt = self.root / 'Qt'
        self.qt.mkdir()

    def files(self, paths):
        result = set()
        for relative in paths:
            path = self.qt / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'ordinary filesystem scope test data')
            result.add(path)
        return result

    def redirect(self, link, target):
        try:
            link.symlink_to(target, target_is_directory=True)
        except OSError:
            if os.name != 'nt':
                raise
            environment = os.environ.copy()
            environment.update(ORV_QT_TOOL_TEST_LINK=str(link), ORV_QT_TOOL_TEST_TARGET=str(target))
            subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command',
                            'New-Item -ItemType Junction -Path $env:ORV_QT_TOOL_TEST_LINK '
                            '-Target $env:ORV_QT_TOOL_TEST_TARGET -ErrorAction Stop | Out-Null'],
                           env=environment, capture_output=True, check=True, timeout=20,
                           creationflags=subprocess.CREATE_NO_WINDOW)

    @staticmethod
    def remove_redirect(link):
        if link.is_symlink():
            link.unlink()
        else:
            link.rmdir()  # Only remove the owned junction, never its target.

    def test_exact_copied_tools_and_whole_tool_apps_without_sdk_bin_sweep(self):
        expected = self.files([
            'bin/lupdate', 'bin/lrelease', 'bin/qmllint', 'bin/qmlformat', 'bin/qmlls',
            'libexec/uic', 'libexec/rcc', 'libexec/qmltyperegistrar',
            'libexec/qmlimportscanner', 'libexec/qmlcachegen', 'libexec/QtWebEngineProcess',
            'bin/Assistant.app/Contents/MacOS/Assistant',
            'bin/Assistant.app/Contents/Helpers/nested-helper',
            'bin/Designer.app/Contents/MacOS/Designer',
            'bin/Linguist.app/Contents/MacOS/Linguist'])
        unrelated = self.files(['bin/qtpaths', 'bin/other.app/Contents/MacOS/other',
                                'libexec/unrelated-developer-tool'])
        self.assertEqual(set(CHECKER.deployed_tool_candidates(self.qt)), expected)
        self.assertTrue(all(path.is_file() for path in expected | unrelated))

    def test_absent_optional_tools_are_not_invented(self):
        self.assertEqual(CHECKER.deployed_tool_candidates(self.qt), [])

    def test_sdk_internal_tool_app_alias_resolves_to_real_files(self):
        expected = self.files(['lib/actual-tool.app/Contents/MacOS/tool'])
        (self.qt / 'bin').mkdir()
        link = self.qt / 'bin/Assistant.app'
        self.redirect(link, self.qt / 'lib/actual-tool.app')
        try:
            self.assertEqual(set(CHECKER.deployed_tool_candidates(self.qt)), expected)
        finally:
            self.remove_redirect(link)

    def test_nested_internal_helper_aliases_are_followed_and_deduplicated(self):
        expected = self.files(['lib/actual-helpers/deep/helper'])
        app = self.qt / 'bin/Assistant.app'
        (app / 'Contents').mkdir(parents=True)
        links = [app / 'Contents/Helpers', app / 'Contents/OtherHelpers']
        try:
            for link in links:
                self.redirect(link, self.qt / 'lib/actual-helpers')
            self.assertEqual(CHECKER.deployed_tool_candidates(self.qt), sorted(expected))
        finally:
            for link in links:
                if link.exists() or link.is_symlink():
                    self.remove_redirect(link)

    def test_internal_directory_cycle_is_rejected(self):
        app = self.qt / 'bin/Assistant.app'
        (app / 'Contents').mkdir(parents=True)
        link = app / 'Contents/Loop'
        self.redirect(link, app)
        try:
            with self.assertRaisesRegex(RuntimeError, 'Directory cycle'):
                CHECKER.deployed_tool_candidates(self.qt)
        finally:
            self.remove_redirect(link)

    def test_external_tool_or_app_helper_redirect_is_rejected(self):
        foreign = self.root / 'foreign'
        foreign.mkdir()
        for name in ('qmlls', 'helper'):
            (foreign / name).write_bytes(b'unchanged foreign test data')
        for relative, target in (('bin', foreign),
                                 ('bin/Assistant.app/Contents/Helpers', foreign)):
            with self.subTest(relative=relative):
                link = self.qt / relative
                link.parent.mkdir(parents=True, exist_ok=True)
                self.redirect(link, target)
                try:
                    with self.assertRaisesRegex(RuntimeError, 'escapes the SDK'):
                        CHECKER.deployed_tool_candidates(self.qt)
                    self.assertEqual((foreign / 'qmlls').read_bytes(), b'unchanged foreign test data')
                finally:
                    self.remove_redirect(link)


if __name__ == '__main__':
    unittest.main()
