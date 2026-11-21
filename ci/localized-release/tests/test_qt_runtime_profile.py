"""Exercise only profile file boundaries; these are not native SDK/runtime checks."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'ci/localized-release'))
from qt_runtime_profile import (MACOS_EXCLUDED_QT_PLUGINS, WINDOWS_EXCLUDED_QT_PLUGINS,
                                MACOS_PLUGIN_ROOTS, WINDOWS_PLUGIN_ROOTS,
                                apply_macos_qt_runtime_profile, apply_windows_qt_runtime_profile)


class QtRuntimeProfileTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='openrv-qt-profile-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.app = self.root / 'owned-stage'
        self.plugins = self.app / 'Contents/PlugIns/Qt'
        self.plugins.mkdir(parents=True)

    def drivers(self, root, names=MACOS_EXCLUDED_QT_PLUGINS):
        result = {}
        for relative in names:
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            data = ('file-boundary test data: ' + relative).encode()
            path.write_bytes(data)
            result[path] = data
        return result

    def test_exact_driver_removal_preserves_sdk_sqlite_qtsql_and_nmea(self):
        removed = self.drivers(self.plugins)
        pyside_plugins = self.app / MACOS_PLUGIN_ROOTS[1]
        removed.update(self.drivers(pyside_plugins))
        sdk_files = self.drivers(self.root / 'vendor-sdk/plugins')
        retained = [self.plugins / 'sqldrivers/libqsqlite.dylib',
                    self.plugins / 'sqldrivers/libqsqlpsql.dylib.backup',
                    self.plugins / 'position/libqtposition_nmea.dylib',
                    pyside_plugins / 'sqldrivers/libqsqlite.dylib',
                    self.app / 'Contents/Frameworks/QtSql.framework/Versions/A/QtSql']
        for path in retained:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'preserved test data')
        records = apply_macos_qt_runtime_profile(self.app)
        self.assertEqual(records, [{'path': path.relative_to(self.app).as_posix(),
                                    'originalSha256': hashlib.sha256(data).hexdigest()}
                                   for path, data in removed.items()])
        self.assertTrue(all(not path.exists() for path in removed))
        self.assertTrue(all(path.read_bytes() == b'preserved test data' for path in retained))
        self.assertTrue(all(path.read_bytes() == data for path, data in sdk_files.items()))
        self.assertEqual(apply_macos_qt_runtime_profile(self.app), [])

    def test_non_regular_driver_is_rejected_before_removing_any_target(self):
        originals = self.drivers(self.plugins)
        (self.app / MACOS_PLUGIN_ROOTS[1] / MACOS_EXCLUDED_QT_PLUGINS[1]).mkdir(parents=True)
        with self.assertRaisesRegex(ValueError, 'non-regular'):
            apply_macos_qt_runtime_profile(self.app)
        self.assertTrue(all(path.read_bytes() == data for path, data in originals.items()))

    def test_windows_removes_only_both_postgresql_copies(self):
        runtime = self.root / 'windows-stage'
        removed = {}
        retained = []
        for relative_root in WINDOWS_PLUGIN_ROOTS:
            plugins = runtime / relative_root
            removed.update(self.drivers(plugins, WINDOWS_EXCLUDED_QT_PLUGINS))
            for name in ('qsqlodbc.dll', 'qsqlite.dll', 'qsqlpsql.dll.backup'):
                path = plugins / 'sqldrivers' / name
                path.write_bytes(b'preserved driver data')
                retained.append(path)
        elsewhere = self.drivers(runtime / 'unrelated/plugins', WINDOWS_EXCLUDED_QT_PLUGINS)
        records = apply_windows_qt_runtime_profile(runtime)
        self.assertEqual(records, [{'path': path.relative_to(runtime).as_posix(),
                                    'originalSha256': hashlib.sha256(data).hexdigest()}
                                   for path, data in removed.items()])
        self.assertTrue(all(not path.exists() for path in removed))
        self.assertTrue(all(path.read_bytes() == b'preserved driver data' for path in retained))
        self.assertTrue(all(path.read_bytes() == data for path, data in elsewhere.items()))

    def test_unexpected_standard_layout_rejected_before_any_deletion(self):
        cases = ((apply_macos_qt_runtime_profile, MACOS_PLUGIN_ROOTS, MACOS_EXCLUDED_QT_PLUGINS,
                  'Contents/lib/python3.11/site-packages/PySide6/plugins'),
                 (apply_windows_qt_runtime_profile, WINDOWS_PLUGIN_ROOTS, WINDOWS_EXCLUDED_QT_PLUGINS,
                  'lib/site-packages/PySide6/Qt/plugins'))
        for index, (apply, roots, names, alternate) in enumerate(cases):
            with self.subTest(platform=index):
                runtime = self.root / ('alternate-' + str(index))
                originals = self.drivers(runtime / roots[0], names)
                originals.update(self.drivers(runtime / alternate, names))
                with self.assertRaisesRegex(ValueError, 'unexpected standard layout'):
                    apply(runtime)
                self.assertTrue(all(path.read_bytes() == data for path, data in originals.items()))

    def test_redirected_parent_cannot_delete_external_files(self):
        foreign = self.root / 'foreign'
        originals = self.drivers(foreign)
        link = self.plugins / 'sqldrivers'
        target = foreign / 'sqldrivers'
        try:
            link.symlink_to(target, target_is_directory=True)
        except OSError:
            if os.name != 'nt':
                raise
            environment = os.environ.copy()
            environment.update(ORV_PROFILE_TEST_LINK=str(link), ORV_PROFILE_TEST_TARGET=str(target))
            subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command',
                            'New-Item -ItemType Junction -Path $env:ORV_PROFILE_TEST_LINK '
                            '-Target $env:ORV_PROFILE_TEST_TARGET -ErrorAction Stop | Out-Null'],
                           env=environment, capture_output=True, check=True, timeout=20,
                           creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            with self.assertRaisesRegex(ValueError, 'redirected'):
                apply_macos_qt_runtime_profile(self.app)
            self.assertTrue(all(path.read_bytes() == data for path, data in originals.items()))
        finally:
            if link.is_symlink():
                link.unlink()
            else:
                link.rmdir()  # Remove only the owned junction, never its target.


if __name__ == '__main__':
    unittest.main()
