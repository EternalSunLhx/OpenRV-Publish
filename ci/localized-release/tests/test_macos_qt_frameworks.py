"""Real filesystem safety tests; native identity is exercised only on Darwin."""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'ci/localized-release'))
from bundle_macos import _coalesce_qt_frameworks, _qt_framework_snapshot
from release_common import sha256


class QtFrameworkFilesystemTests(unittest.TestCase):
    """These ordinary files do not substitute for Mach-O binaries or runtime tests."""
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='openrv-qt-frameworks-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.app = self.root / 'RV.app'
        self.canonical = self.app / 'Contents/Frameworks'
        self.standalone = self.app / 'Contents/lib/python3.11/site-packages/PySide6/Qt/lib'
        self.canonical.mkdir(parents=True)
        self.standalone.mkdir(parents=True)

    @staticmethod
    def ordinary_file(path):
        return False

    def framework(self, root, name='QtFixture.framework'):
        path = root / name
        resources = path / 'Resources'
        resources.mkdir(parents=True)
        with (resources / 'Info.plist').open('wb') as stream:
            plistlib.dump({'CFBundleIdentifier': 'org.qt-project.QtFixture',
                          'CFBundleExecutable': 'QtFixture', 'CFBundleVersion': '6.5.3',
                          'CFBundleShortVersionString': '6.5.3'}, stream)
        (resources / 'data.pak').write_bytes(b'actual ordinary resource bytes\x00\x01')
        return path

    def directory_alias(self, link, target):
        try:
            link.symlink_to(target, target_is_directory=True)
        except OSError:
            if os.name != 'nt':
                raise
            environment = os.environ.copy()
            environment.update(ORV_TEST_ALIAS=str(link), ORV_TEST_TARGET=str(target))
            subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command',
                            'New-Item -ItemType Junction -Path $env:ORV_TEST_ALIAS '
                            '-Target $env:ORV_TEST_TARGET -ErrorAction Stop | Out-Null'],
                           env=environment, check=True, capture_output=True, timeout=20,
                           creationflags=subprocess.CREATE_NO_WINDOW)

    def test_resource_subset_allows_canonical_extras_and_ignores_old_code_signature(self):
        duplicate = self.framework(self.standalone)
        canonical = self.framework(self.canonical)
        (canonical / 'Headers').mkdir()
        (canonical / 'Headers/extra.h').write_text('sdk header', encoding='utf-8')
        for framework, value in ((duplicate, b'old-signature'), (canonical, b'other-signature')):
            (framework / '_CodeSignature').mkdir()
            (framework / '_CodeSignature/CodeResources').write_bytes(value)
        original = _qt_framework_snapshot(duplicate, self.app, self.ordinary_file)
        retained = _qt_framework_snapshot(canonical, self.app, self.ordinary_file)
        self.assertTrue(all(retained.get(name) == value for name, value in original.items()))
        self.assertFalse(any('_CodeSignature' in name for name in original))
        self.assertEqual((duplicate / 'Resources/data.pak').read_bytes(),
                         b'actual ordinary resource bytes\x00\x01')

    def test_different_resource_bytes_fail_without_replacing_any_directory(self):
        duplicate = self.framework(self.standalone)
        canonical = self.framework(self.canonical)
        (canonical / 'Resources/data.pak').write_bytes(b'different')
        with self.assertRaisesRegex(RuntimeError, 'payload collision'):
            _coalesce_qt_frameworks(self.app, self.ordinary_file)
        self.assertFalse(duplicate.is_symlink())
        self.assertEqual((duplicate / 'Resources/data.pak').read_bytes(),
                         b'actual ordinary resource bytes\x00\x01')

    def test_version_mismatch_and_missing_native_image_fail_closed(self):
        duplicate = self.framework(self.standalone)
        canonical = self.framework(self.canonical)
        info = canonical / 'Resources/Info.plist'
        values = plistlib.loads(info.read_bytes())
        values['CFBundleVersion'] = 'different'
        info.write_bytes(plistlib.dumps(values))
        with self.assertRaisesRegex(RuntimeError, 'version/identity collision'):
            _coalesce_qt_frameworks(self.app, self.ordinary_file)
        shutil.copy2(duplicate / 'Resources/Info.plist', info)
        with self.assertRaisesRegex(RuntimeError, 'no verified native image'):
            _coalesce_qt_frameworks(self.app, self.ordinary_file)
        self.assertFalse(duplicate.is_symlink())

    def test_missing_canonical_framework_and_redirected_root_are_rejected(self):
        duplicate = self.framework(self.standalone)
        with self.assertRaisesRegex(RuntimeError, 'canonical Qt framework'):
            _coalesce_qt_frameworks(self.app, self.ordinary_file)
        shutil.rmtree(self.standalone)
        foreign = self.root / 'foreign'
        foreign.mkdir()
        self.directory_alias(self.standalone, foreign)
        try:
            with self.assertRaisesRegex(RuntimeError, 'Redirected Qt framework root'):
                _coalesce_qt_frameworks(self.app, self.ordinary_file)
            self.assertEqual(list(foreign.iterdir()), [])
        finally:
            if self.standalone.is_symlink():
                self.standalone.unlink()
            else:
                self.standalone.rmdir()  # Windows junction, never recurse into its target.

    def test_internal_alias_resources_checked_but_escape_and_cycle_rejected(self):
        framework = self.framework(self.standalone)
        self.directory_alias(framework / 'Alias', framework / 'Resources')
        manifest = _qt_framework_snapshot(framework, self.app, self.ordinary_file)
        self.assertEqual(manifest['Alias/data.pak'], manifest['Resources/data.pak'])
        alias = framework / 'Alias'
        if alias.is_symlink(): alias.unlink()
        else: alias.rmdir()
        foreign = self.root / 'foreign-payload'
        foreign.mkdir()
        self.directory_alias(alias, foreign)
        try:
            with self.assertRaisesRegex(RuntimeError, 'escapes the app'):
                _qt_framework_snapshot(framework, self.app, self.ordinary_file)
        finally:
            if alias.is_symlink(): alias.unlink()
            else: alias.rmdir()
        self.directory_alias(alias, framework)
        try:
            with self.assertRaisesRegex(RuntimeError, 'directory cycle'):
                _qt_framework_snapshot(framework, self.app, self.ordinary_file)
        finally:
            if alias.is_symlink(): alias.unlink()
            else: alias.rmdir()

    def test_unrelated_package_tree_is_not_a_coalescing_source(self):
        unrelated = self.app / 'Contents/lib/python3.11/site-packages/unrelated/Qt/lib'
        unrelated.mkdir(parents=True)
        framework = self.framework(unrelated)
        self.assertEqual(_coalesce_qt_frameworks(self.app, self.ordinary_file), [])
        self.assertTrue((framework / 'Resources/data.pak').is_file())

    def test_existing_absolute_alias_is_rejected_without_changing_canonical(self):
        canonical = self.framework(self.canonical)
        original = (canonical / 'Resources/data.pak').read_bytes()
        alias = self.standalone / canonical.name
        self.directory_alias(alias, canonical)
        try:
            # The Windows junction fallback is also rejected as a redirected root;
            # POSIX exercises the absolute-symlink portability guard directly.
            with self.assertRaisesRegex(RuntimeError, 'framework alias|Redirected PySide'):
                _coalesce_qt_frameworks(self.app, self.ordinary_file)
            self.assertEqual((canonical / 'Resources/data.pak').read_bytes(), original)
        finally:
            if alias.is_symlink(): alias.unlink()
            else: alias.rmdir()


@unittest.skipUnless(sys.platform == 'darwin', 'Requires actual Qt frameworks and Apple Mach-O tools')
class NativeQtFrameworkTests(unittest.TestCase):
    setUp = QtFrameworkFilesystemTests.setUp

    def test_actual_sdk_framework_coalesces_without_changing_sdk_or_canonical_bytes(self):
        for tool in ('ditto', 'lipo', 'dwarfdump'):
            self.assertIsNotNone(shutil.which(tool), f'Missing actual framework tool: {tool}')
        self.assertIn('QT_ROOT_DIR', os.environ)
        qt = Path(os.environ['QT_ROOT_DIR']).resolve(strict=True)
        sources = [qt / 'lib' / name for name in ('QtCore.framework', 'QtGui.framework')]
        before = {source: sha256(source / 'Versions/A' / source.stem) for source in sources}
        for source in sources:
            for root in (self.canonical, self.standalone):
                subprocess.run(['ditto', str(source), str(root / source.name)], check=True, timeout=90)
        spec = importlib.util.spec_from_file_location(
            'macos_qt_checker_for_framework_test', ROOT / 'ci/localized-release/check-macos-qt-dependencies.py')
        checker = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(checker)
        # A late conflict must not partially consolidate the earlier real QtCore pair.
        info = self.standalone / 'QtGui.framework/Resources/Info.plist'
        original_info = info.read_bytes()
        values = plistlib.loads(original_info)
        values['CFBundleVersion'] = 'collision'
        info.write_bytes(plistlib.dumps(values))
        with self.assertRaisesRegex(RuntimeError, 'version/identity collision'):
            _coalesce_qt_frameworks(self.app, checker.is_macho)
        self.assertFalse((self.standalone / 'QtCore.framework').is_symlink())
        info.write_bytes(original_info)
        # Keep QtCore's real metadata/resources, but substitute another actual SDK
        # image to prove different native UUIDs cannot be consolidated by version.
        duplicate_core = self.standalone / 'QtCore.framework/Versions/A/QtCore'
        shutil.copy2(self.canonical / 'QtGui.framework/Versions/A/QtGui', duplicate_core)
        with self.assertRaisesRegex(RuntimeError, 'payload collision'):
            _coalesce_qt_frameworks(self.app, checker.is_macho)
        self.assertTrue(all(not (self.standalone / source.name).is_symlink() for source in sources))
        shutil.copy2(self.canonical / 'QtCore.framework/Versions/A/QtCore', duplicate_core)
        records = _coalesce_qt_frameworks(self.app, checker.is_macho)
        for source in sources:
            duplicate = self.standalone / source.name
            canonical = self.canonical / source.name
            self.assertTrue(duplicate.is_symlink())
            self.assertFalse(Path(os.readlink(duplicate)).is_absolute())
            self.assertEqual(duplicate.resolve(strict=True), canonical.resolve(strict=True))
            self.assertEqual(sha256(canonical / 'Versions/A' / source.stem), before[source])
            self.assertEqual(sha256(source / 'Versions/A' / source.stem), before[source])
        self.assertEqual(len(records), 2)
        self.assertTrue(all(record['verifiedImagePaths'] > 0 for record in records))
        self.assertTrue(all(record['verifiedImages'] for record in records))
        self.assertEqual(_coalesce_qt_frameworks(self.app, checker.is_macho), [])


if __name__ == '__main__':
    unittest.main()
