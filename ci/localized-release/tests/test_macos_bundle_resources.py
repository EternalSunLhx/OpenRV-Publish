"""Real resource files/permissions only; these are not native Mach-O/runtime tests."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'ci/localized-release'))
from bundle_macos import _copy_bundled_resource


class MacBundleResourceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='openrv-mac-resources-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.app = self.root / 'RV.app'
        self.app.mkdir()
        self.keg = self.root / 'vendor-keg'
        self.source = self.keg / 'lib/tk8.6'
        self.source.mkdir(parents=True)
        (self.source / 'tk.tcl').write_bytes(b'ordinary Tcl resource test data')
        self.destination = self.app / 'Contents/lib/tk8.6'

    def copy(self, source=None, destination=None):
        _copy_bundled_resource(source or self.source, destination or self.destination,
                               self.app, self.keg)

    def readonly_source(self):
        (self.source / 'tk.tcl').chmod(0o444)
        self.source.chmod(0o555)

    def redirect(self, link, target):
        try:
            link.symlink_to(target, target_is_directory=True)
        except OSError:
            if os.name != 'nt':
                raise
            environment = os.environ.copy()
            environment.update(ORV_RESOURCE_TEST_LINK=str(link), ORV_RESOURCE_TEST_TARGET=str(target))
            subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command',
                            'New-Item -ItemType Junction -Path $env:ORV_RESOURCE_TEST_LINK '
                            '-Target $env:ORV_RESOURCE_TEST_TARGET -ErrorAction Stop | Out-Null'],
                           env=environment, capture_output=True, check=True, timeout=20,
                           creationflags=subprocess.CREATE_NO_WINDOW)

    @staticmethod
    def remove_redirect(link):
        if link.is_symlink():
            link.unlink()
        else:
            link.rmdir()  # Remove only the owned junction, never its target.

    @unittest.skipUnless(os.name == 'posix' and os.geteuid() != 0,
                         'The legacy EACCES regression requires a non-root POSIX process')
    def test_legacy_merge_really_fails_on_copied_posix_readonly_files(self):
        self.readonly_source()
        legacy = self.root / 'legacy-copy'
        shutil.copytree(self.source, legacy)
        with self.assertRaises((shutil.Error, PermissionError)):
            shutil.copytree(self.source, legacy, dirs_exist_ok=True)
        self.copy()
        self.copy()

    def test_complete_readonly_tree_reused_without_changing_bytes_or_modes(self):
        self.readonly_source()
        self.copy()
        source_mode = stat.S_IMODE((self.source / 'tk.tcl').stat().st_mode)
        before = {path: (path.read_bytes(), stat.S_IMODE(path.stat().st_mode), path.stat().st_mtime_ns)
                  for path in (self.source / 'tk.tcl', self.destination / 'tk.tcl')}
        directory_modes = tuple(stat.S_IMODE(path.stat().st_mode)
                                for path in (self.source, self.destination))
        self.copy()  # libtcl and libtk encounter the same shared resource tree.
        self.assertEqual(source_mode, stat.S_IMODE((self.destination / 'tk.tcl').stat().st_mode))
        self.assertEqual(directory_modes, tuple(stat.S_IMODE(path.stat().st_mode)
                                                for path in (self.source, self.destination)))
        self.assertEqual(before, {path: (path.read_bytes(), stat.S_IMODE(path.stat().st_mode),
                                        path.stat().st_mtime_ns) for path in before})

    def test_readonly_license_and_receipt_reused_and_different_keg_rejected(self):
        for name in ('LICENSE', 'INSTALL_RECEIPT.json'):
            with self.subTest(name=name):
                source = self.keg / name
                source.write_bytes(b'identical license or receipt test bytes')
                source.chmod(0o444)
                destination = self.app / 'Contents/Resources/licenses/Homebrew/formula' / name
                self.copy(source, destination)
                self.copy(source, destination)
                alternate = self.root / 'other-keg'
                alternate.mkdir(exist_ok=True)
                (alternate / name).write_bytes(b'different keg bytes')
                with self.assertRaisesRegex(RuntimeError, 'Resource collision'):
                    _copy_bundled_resource(alternate / name, destination, self.app, alternate)
                self.assertEqual(destination.read_bytes(), source.read_bytes())
                self.assertEqual(stat.S_IMODE(destination.stat().st_mode),
                                 stat.S_IMODE(source.stat().st_mode))

    def test_partial_or_different_existing_tree_is_collision_without_overwrite(self):
        (self.source / 'second.tcl').write_bytes(b'second file')
        self.destination.mkdir(parents=True)
        target = self.destination / 'tk.tcl'
        target.write_bytes((self.source / 'tk.tcl').read_bytes())
        target.chmod(0o444)
        with self.assertRaisesRegex(RuntimeError, 'partial content'):
            self.copy()
        self.assertFalse((self.destination / 'second.tcl').exists())
        self.assertEqual(target.read_bytes(), (self.source / 'tk.tcl').read_bytes())

    def test_existing_wrong_type_is_collision(self):
        self.destination.parent.mkdir(parents=True)
        self.destination.write_bytes(b'preserved file instead of directory')
        with self.assertRaisesRegex(RuntimeError, 'Resource collision'):
            self.copy()
        self.assertEqual(self.destination.read_bytes(), b'preserved file instead of directory')

    def test_destination_parent_redirect_cannot_write_outside_app(self):
        foreign = self.root / 'foreign'
        foreign.mkdir()
        (self.app / 'Contents').mkdir()
        link = self.app / 'Contents/lib'
        self.redirect(link, foreign)
        try:
            with self.assertRaisesRegex(RuntimeError, 'Redirected'):
                self.copy()
            self.assertEqual(list(foreign.iterdir()), [])
        finally:
            self.remove_redirect(link)

    def test_source_escape_or_directory_cycle_is_rejected_before_copy(self):
        foreign = self.root / 'foreign'
        foreign.mkdir()
        (foreign / 'private.txt').write_bytes(b'must not be copied')
        for target in (foreign, self.source):
            with self.subTest(target=target):
                link = self.source / 'alias'
                self.redirect(link, target)
                try:
                    with self.assertRaisesRegex(RuntimeError, 'escapes|cycle'):
                        self.copy()
                    self.assertFalse(self.destination.exists())
                finally:
                    self.remove_redirect(link)


if __name__ == '__main__':
    unittest.main()
