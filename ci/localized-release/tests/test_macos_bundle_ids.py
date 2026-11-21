"""Compile real tiny Mach-O fixtures on Darwin; never substitute fake native bytes."""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'ci/localized-release'))
from bundle_macos import bundle_dependencies, _relocated_install_id
from release_common import sha256

SPEC = importlib.util.spec_from_file_location(
    'macos_qt_dependencies', ROOT / 'ci/localized-release/check-macos-qt-dependencies.py')
CHECKER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CHECKER)


class InstallIdChoiceTests(unittest.TestCase):
    """Pure string/path policy only; these cases do not validate native Mach-O files."""
    def setUp(self):
        self.contents = Path('owned/Contents')
        self.provider = self.contents / 'lib/python3.11/site-packages/deep/libfixture.so'

    def choose(self, ids, others=None):
        registry = {self.provider: list(ids), **(others or {})}
        before = {path: list(values) for path, values in registry.items()}
        result = _relocated_install_id(self.provider, ids, self.contents, registry)
        self.assertEqual(registry, before)
        return result

    def test_all_relative_or_system_slice_ids_are_left_untouched(self):
        self.assertIsNone(self.choose(['@rpath/a.so', '@loader_path/b.so', '/usr/lib/libSystem.B.dylib']))
        self.assertIsNone(self.choose([]))

    def test_mixed_reuses_consistent_relative_but_rejects_utf8_byte_growth(self):
        self.assertEqual(self.choose(['@rpath/x.so', '/long/original/build/path/libfixture.so']), '@rpath/x.so')
        relative = '@rpath/é.so'
        absolute = '/' + 'a' * (len(relative) - 1)
        self.assertEqual(len(relative), len(absolute))
        self.assertGreater(len(relative.encode('utf-8')), len(absolute.encode('utf-8')))
        with self.assertRaisesRegex(RuntimeError, 'would grow'):
            self.choose([relative, absolute])

    def test_ambiguous_mixed_slice_ids_are_rejected(self):
        with self.assertRaisesRegex(RuntimeError, 'Ambiguous'):
            self.choose(['@rpath/a.so', '@rpath/b.so', '/original/build/libfixture.so'])

    def test_short_unique_basename_fallback_does_not_grow_id(self):
        original = '/build/libfixture.so'
        expected = '@rpath/libfixture.so'
        self.assertEqual(len(original.encode('utf-8')), len(expected.encode('utf-8')))
        self.assertEqual(self.choose([original]), expected)

    def test_generated_ids_fail_closed_on_collisions_or_insufficient_space(self):
        canonical = '@rpath/' + self.provider.relative_to(self.contents).as_posix()
        short = '@rpath/' + self.provider.name
        others = {self.contents / 'other/a.so': [canonical], self.contents / 'other/b.so': [short]}
        with self.assertRaisesRegex(RuntimeError, 'unique'):
            self.choose(['/' + 'a' * 200], others)
        with self.assertRaisesRegex(RuntimeError, 'non-growing'):
            self.choose(['/x'])


@unittest.skipUnless(sys.platform == 'darwin', 'Requires real Apple Mach-O tools on macOS')
class MacBundleInstallIdTests(unittest.TestCase):
    def setUp(self):
        for tool in ('clang', 'cmake', 'lipo', 'otool', 'install_name_tool', 'brew'):
            self.assertIsNotNone(shutil.which(tool), f'Missing native fixture tool: {tool}')
        self.assertIn('QT_ROOT_DIR', os.environ, 'Use the actual installed Qt SDK')
        self.qt = Path(os.environ['QT_ROOT_DIR']).resolve(strict=True)
        self.assertTrue((self.qt / 'lib').is_dir())
        self.temporary = tempfile.TemporaryDirectory(prefix='openrv-native-id-fixture-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.repo = self.root / 'fixture-repo'
        self.repo.mkdir()

    def command(self, *arguments):
        result = subprocess.run(list(map(str, arguments)), text=True, capture_output=True, timeout=60)
        self.assertEqual(result.returncode, 0,
                         f'Native command failed: {arguments}\n{result.stderr[-4000:]}')
        return result.stdout

    def app(self, name):
        app = self.root / name / 'fixture.app'
        (app / 'Contents/lib').mkdir(parents=True)
        (app / 'Contents/MacOS').mkdir()
        return app

    def compile_library(self, directory, name, architectures, host_install_id=None, headerpad=True):
        directory.mkdir(parents=True)
        source = directory.parent / (directory.name + '.c')
        source.write_text('int fixture_answer(void) { return 42; }\n', encoding='utf-8')
        output = directory / name
        slices = []
        for architecture in architectures:
            target = output if len(architectures) == 1 else directory.parent / (architecture + '-' + name)
            identity = host_install_id if host_install_id and architecture == platform.machine() else output
            self.command('clang', '-arch', architecture, '-dynamiclib', source,
                         '-Wl,-headerpad_max_install_names' if headerpad else '-Wl,-headerpad,0',
                         '-install_name', identity, '-o', target)
            slices.append(target)
        if len(slices) > 1:
            self.command('lipo', '-create', *slices, '-output', output)
        return output

    def architectures(self, binary):
        return set(self.command('lipo', '-archs', binary).split())

    def assert_relocated(self, binary, app, architectures, expected_id=None):
        self.assertEqual(self.architectures(binary), set(architectures))
        for architecture in architectures:
            ids = [line.strip() for line in self.command('otool', '-arch', architecture, '-D', binary).splitlines()
                   if line.strip() and not line.rstrip().endswith(':')]
            if ids:
                self.assertEqual(ids, [expected_id or '@rpath/' + binary.relative_to(app / 'Contents').as_posix()])
            # Keep the production absolute-path gate, including the library's own ID.
            for line in self.command('otool', '-arch', architecture, '-L', binary).splitlines()[1:]:
                dependency = line.strip().split(' (', 1)[0]
                self.assertFalse(dependency.startswith('/')
                                 and not dependency.startswith(('/System/', '/usr/lib/')),
                                 f'Non-relocatable dependency {dependency} in {binary}')

    @staticmethod
    def native_hashes(app):
        return {path.relative_to(app).as_posix(): sha256(path)
                for path in app.rglob('*') if CHECKER.is_macho(path)}

    def assert_second_run_unchanged(self, app):
        before = self.native_hashes(app)
        bundle_dependencies(app, self.repo, self.qt, CHECKER.is_macho)
        self.assertEqual(self.native_hashes(app), before)

    def test_unreferenced_thin_universal_and_mixed_ids_keep_all_architectures(self):
        for name, architectures, mixed in (('thin', (platform.machine(),), False),
                                           ('universal', ('x86_64', 'arm64'), False),
                                           ('mixed-universal', ('x86_64', 'arm64'), True),
                                           ('mixed-short-universal', ('x86_64', 'arm64'), True)):
            with self.subTest(kind=name):
                app = self.app(name)
                short = name == 'mixed-short-universal'
                host_id = ('@rpath/x.so' if short else '@rpath/lib/liborphan.dylib') if mixed else None
                original = self.compile_library(self.repo / name / 'lib', 'liborphan.1.dylib',
                                                architectures, host_install_id=host_id, headerpad=not short)
                alias = original.parent / 'liborphan.dylib'
                alias.symlink_to(original.name)
                source_sha = sha256(original)
                # Upstream stages whole lib directories, following the alias into another
                # regular file, but only normalizes TARGET_FILE_NAME's identity.
                self.command('cmake', '-E', 'copy_directory', original.parent, app / 'Contents/lib')
                versioned = app / 'Contents/lib' / original.name
                unreferenced = app / 'Contents/lib' / alias.name
                self.assertFalse(unreferenced.is_symlink())
                if not short:
                    self.command('install_name_tool', '-id', '@rpath/' + original.name, versioned)
                versioned_sha = sha256(versioned)
                self.assertIn(str(original), self.command('otool', '-arch', 'all', '-L', unreferenced))
                if mixed:
                    self.assertIn(host_id, self.command('otool', '-arch', platform.machine(), '-D', unreferenced))
                    other_arch = next(arch for arch in architectures if arch != platform.machine())
                    self.assertIn(str(original), self.command('otool', '-arch', other_arch, '-D', unreferenced))
                self.assertEqual(bundle_dependencies(app, self.repo, self.qt, CHECKER.is_macho), [])
                self.assert_relocated(versioned, app, architectures, host_id if short else '@rpath/' + original.name)
                if not short:
                    self.assertEqual(sha256(versioned), versioned_sha)
                self.assert_relocated(unreferenced, app, architectures, host_id)
                self.assertEqual(sha256(original), source_sha)
                self.assert_second_run_unchanged(app)
                self.assertEqual(sha256(original), source_sha)

    def test_short_relative_id_without_headerpad_is_preserved_in_deep_package(self):
        architecture = platform.machine()
        app = self.app('short-relative')
        relative_id = '@rpath/_rust.cpython-311-darwin.so'
        original = self.compile_library(self.repo / 'short/lib', '_rust.cpython-311-darwin.so',
                                        (architecture,), host_install_id=relative_id, headerpad=False)
        source_sha = sha256(original)
        binary = app / 'Contents/lib/python3.11/site-packages/cryptography/hazmat/bindings' / original.name
        binary.parent.mkdir(parents=True)
        shutil.copy2(original, binary)
        before = sha256(binary)
        legacy = self.repo / 'legacy-growth.so'
        shutil.copy2(original, legacy)
        legacy_id = '@rpath/' + binary.relative_to(app / 'Contents').as_posix()
        result = subprocess.run(['install_name_tool', '-id', legacy_id, str(legacy)],
                                capture_output=True, text=True, timeout=60)
        if result.returncode:
            self.assertRegex(result.stderr.lower(), 'headerpad|load commands|loadcommands')
            print('Real legacy ID-growth probe: rejected by available header space', flush=True)
        else:
            self.assertNotEqual(sha256(legacy), source_sha)
            print('Real legacy ID-growth probe: linker alignment permitted growth; byte preservation still required',
                  flush=True)
        self.assertEqual(bundle_dependencies(app, self.repo, self.qt, CHECKER.is_macho), [])
        self.assertEqual(sha256(binary), before)
        self.assert_relocated(binary, app, (architecture,), relative_id)
        self.assertEqual(sha256(original), source_sha)
        self.assert_second_run_unchanged(app)

        # A real short absolute ID at a deep location must use the non-growing
        # basename fallback, not the longer Contents-relative spelling.
        fallback_app = self.app('short-absolute')
        absolute_id = '/build/libfixture.so'  # Linker metadata only; no /build path is created.
        short_id = '@rpath/libfixture.so'
        self.assertEqual(len(absolute_id.encode('utf-8')), len(short_id.encode('utf-8')))
        fallback_original = self.compile_library(self.repo / 'absolute/lib', 'libfixture.so',
                                                 (architecture,), host_install_id=absolute_id, headerpad=False)
        fallback_source_sha = sha256(fallback_original)
        self.assertIn(absolute_id, self.command('otool', '-D', fallback_original))
        fallback_binary = fallback_app / 'Contents/lib/python3.11/site-packages/fallback/deep/libfixture.so'
        fallback_binary.parent.mkdir(parents=True)
        shutil.copy2(fallback_original, fallback_binary)
        self.assertEqual(bundle_dependencies(fallback_app, self.repo, self.qt, CHECKER.is_macho), [])
        self.assert_relocated(fallback_binary, fallback_app, (architecture,), short_id)
        self.assertNotEqual(sha256(fallback_binary), fallback_source_sha)
        self.assertEqual(sha256(fallback_original), fallback_source_sha)
        self.assert_second_run_unchanged(fallback_app)
        self.assertEqual(sha256(fallback_original), fallback_source_sha)

    def test_unreferenced_executable_real_load_dependency_is_collected(self):
        architecture = platform.machine()
        app = self.app('consumer')
        original = self.compile_library(self.repo / 'external/lib', 'libfixture.1.dylib', (architecture,))
        source_sha = sha256(original)
        source = self.repo / 'orphan-tool.c'
        source.write_text('extern int fixture_answer(void); int main(void) { return fixture_answer(); }\n',
                          encoding='utf-8')
        executable = app / 'Contents/MacOS/orphan-tool'
        self.command('clang', '-arch', architecture, source, original,
                     '-Wl,-headerpad_max_install_names', '-o', executable)
        self.assertIn(str(original), self.command('otool', '-L', executable))
        records = bundle_dependencies(app, self.repo, self.qt, CHECKER.is_macho)
        copied = app / 'Contents/lib/thirdparty' / original.name
        self.assertTrue(copied.is_file())
        self.assertTrue(any(record['source'] == str(original) for record in records))
        self.assertIn('@loader_path/../lib/thirdparty/' + original.name,
                      self.command('otool', '-L', executable))
        for binary in (executable, copied):
            self.assert_relocated(binary, app, (architecture,))
        self.assertEqual(sha256(original), source_sha)
        self.assert_second_run_unchanged(app)
        self.assertEqual(sha256(original), source_sha)


if __name__ == '__main__':
    unittest.main()
