"""Real filesystem/environment boundaries only; no Mach-O/native runtime claims."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'ci/localized-release'))
from bundle_macos import _analysis_environment, _analysis_library_paths


class MacBundleAnalysisTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='openrv-mac-analysis-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.app = self.root / 'RV.app'
        contents = self.app / 'Contents'
        packages = contents / 'lib/python3.11/site-packages'
        self.expected = (contents / 'lib', contents / 'Frameworks', packages / 'shiboken6',
                         packages / 'PySide6', packages / 'PySide6/Qt/lib')
        for directory in self.expected:
            directory.mkdir(parents=True, exist_ok=True)
        self.qt = self.root / 'Qt'
        (self.qt / 'lib').mkdir(parents=True)

    def test_exact_package_roots_before_sdk_without_recursive_search(self):
        unrelated = self.app / 'Contents/lib/python3.11/site-packages/unrelated/lib'
        unrelated.mkdir(parents=True)
        self.assertEqual(_analysis_library_paths(self.app, self.qt),
                         (*self.expected, self.qt / 'lib'))
        self.assertNotIn(unrelated, _analysis_library_paths(self.app, self.qt))

    def test_absent_package_directory_is_not_an_analysis_path(self):
        self.expected[2].rmdir()
        self.assertEqual(_analysis_library_paths(self.app, self.qt),
                         (*self.expected[:2], *self.expected[3:], self.qt / 'lib'))

    def test_analysis_environment_restored_on_success_and_failure(self):
        keys = ('DYLD_LIBRARY_PATH', 'DYLD_FALLBACK_LIBRARY_PATH')
        expected_path = os.pathsep.join(map(str, (*self.expected, self.qt / 'lib')))
        for previous in (None, '', 'previous-value'):
            for fail in (False, True):
                with self.subTest(previous=previous, failure=fail), patch.dict(os.environ):
                    for key in keys:
                        if previous is None:
                            os.environ.pop(key, None)
                        else:
                            os.environ[key] = previous
                    try:
                        with _analysis_environment(self.app, self.qt):
                            self.assertNotIn(keys[0], os.environ)
                            self.assertEqual(os.environ[keys[1]], expected_path)
                            if fail:
                                raise ValueError('analysis stopped')
                    except ValueError:
                        self.assertTrue(fail)
                    for key in keys:
                        self.assertEqual(os.environ.get(key), previous)
                        self.assertEqual(key in os.environ, previous is not None)

    def test_redirected_package_root_rejected_before_environment_change(self):
        link = self.expected[2]
        link.rmdir()
        foreign = self.root / 'foreign-package'
        foreign.mkdir()
        try:
            link.symlink_to(foreign, target_is_directory=True)
        except OSError:
            if os.name != 'nt':
                raise
            environment = os.environ.copy()
            environment.update(ORV_ANALYSIS_TEST_LINK=str(link), ORV_ANALYSIS_TEST_TARGET=str(foreign))
            subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command',
                            'New-Item -ItemType Junction -Path $env:ORV_ANALYSIS_TEST_LINK '
                            '-Target $env:ORV_ANALYSIS_TEST_TARGET -ErrorAction Stop | Out-Null'],
                           env=environment, capture_output=True, check=True, timeout=20,
                           creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            old = dict(os.environ)
            with self.assertRaisesRegex(RuntimeError, 'escapes the app'):
                with _analysis_environment(self.app, self.qt):
                    self.fail('redirected package root reached analysis')
            self.assertEqual(dict(os.environ), old)
            self.assertTrue(foreign.is_dir())
        finally:
            if link.is_symlink():
                link.unlink()
            else:
                link.rmdir()  # Remove only the owned junction, never its target.


if __name__ == '__main__':
    unittest.main()
