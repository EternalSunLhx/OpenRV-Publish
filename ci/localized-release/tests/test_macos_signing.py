"""Filesystem layout cases and genuine Darwin signing/archive integration fixtures."""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import plistlib
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'ci/localized-release'))
from sign_macos import relocate_non_macho, sign_app, verify_app
from release_common import sha256

SPEC = importlib.util.spec_from_file_location(
    'macos_qt_dependencies', ROOT / 'ci/localized-release/check-macos-qt-dependencies.py')
CHECKER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CHECKER)

CODE_ROOT_FIXTURES = ('MacOS', 'Frameworks', 'SharedFrameworks', 'PlugIns', 'Plug-ins',
                      'XPCServices', 'Helpers', 'Library/Automator', 'Library/Spotlight',
                      'Library/LoginItems', 'Library/LaunchServices')


class MacResourceLayoutTests(unittest.TestCase):
    """Ordinary filesystem data only; these are not native code-signing proofs."""
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='openrv-resource-layout-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.app = self.root / 'owned-stage'
        self.macos = self.app / 'Contents/MacOS'
        self.macos.mkdir(parents=True)

    @unittest.skipUnless(os.name == 'posix', 'Requires real POSIX resource aliases')
    def test_scripts_and_pure_data_directories_keep_bytes_modes_and_old_paths(self):
        script = self.macos / 'wrapper.sh'
        script.write_bytes(b'#!/bin/sh\nexit 0\n')
        script.chmod(0o755)
        module = self.macos / 'deploy_lib/module.py'
        module.parent.mkdir()
        module.write_bytes(b'value = 42\n')
        module.chmod(0o644)
        before = {path: (path.read_bytes(), stat.S_IMODE(path.stat().st_mode)) for path in (script, module)}
        records = relocate_non_macho(self.app, CHECKER.is_macho)
        self.assertEqual(len(records), 2)
        self.assertTrue(script.is_symlink())
        self.assertTrue(module.parent.is_symlink())
        self.assertTrue(all(not os.path.isabs(os.readlink(path)) for path in (script, module.parent)))
        self.assertEqual(before, {path: (path.read_bytes(), stat.S_IMODE(path.stat().st_mode)) for path in before})
        self.assertEqual(relocate_non_macho(self.app, CHECKER.is_macho), [])

    def test_destination_collision_is_rejected_before_any_move(self):
        script = self.macos / 'wrapper.sh'
        script.write_bytes(b'#!/bin/sh\nexit 0\n')
        plugin = self.app / 'Contents/PlugIns/Python/plugin.py'
        plugin.parent.mkdir(parents=True)
        plugin.write_bytes(b'print("preserve plugin")\n')
        target = self.app / 'Contents/Resources/scripts/PlugIns/Python'
        target.parent.mkdir(parents=True)
        target.write_bytes(b'preserve existing resource')
        with self.assertRaisesRegex(RuntimeError, 'collision'):
            relocate_non_macho(self.app, CHECKER.is_macho)
        self.assertFalse(script.is_symlink())
        self.assertFalse(plugin.is_symlink())
        self.assertEqual(script.read_bytes(), b'#!/bin/sh\nexit 0\n')
        self.assertEqual(target.read_bytes(), b'preserve existing resource')

    @unittest.skipUnless(os.name == 'posix', 'Requires real POSIX resource aliases')
    def test_code_roots_namespaces_metadata_and_nested_owners(self):
        expected = {}
        for relative in CODE_ROOT_FIXTURES:
            source = self.app / 'Contents' / relative / 'same-name.txt'
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_bytes(relative.encode('ascii'))
            expected[source] = source.read_bytes()
        direct = self.app / 'Contents/direct-data.txt'
        direct.write_bytes(b'direct resource')
        expected[direct] = direct.read_bytes()
        metadata = {self.app / 'Contents' / name: name.encode('ascii') for name in
                    ('Info.plist', 'PkgInfo', 'version.plist', 'embedded.provisionprofile')}
        for path, data in metadata.items():
            path.write_bytes(data)
        unscanned = self.app / 'Contents/lib/keep.txt'
        unscanned.parent.mkdir()
        unscanned.write_bytes(b'not a code root')
        plugin = self.app / 'Contents/PlugIns/Custom.plugin'
        plugin_info = plugin / 'Contents/Info.plist'
        plugin_info.parent.mkdir(parents=True)
        plugin_info.write_bytes(b'ordinary filesystem bundle-boundary fixture')
        plugin_data = plugin / 'Contents/PlugIns/Mu/same-name.txt'
        plugin_data.parent.mkdir(parents=True)
        plugin_data.write_bytes(b'nested owner')
        relocate_non_macho(self.app, CHECKER.is_macho)
        for path, data in expected.items():
            self.assertEqual(path.read_bytes(), data)
            self.assertEqual(path.resolve(), self.app / 'Contents/Resources/scripts' /
                             path.relative_to(self.app / 'Contents'))
        self.assertEqual(plugin_data.read_bytes(), b'nested owner')
        self.assertTrue(plugin_data.resolve().is_relative_to(plugin / 'Contents/Resources'))
        self.assertFalse(plugin.is_symlink())
        self.assertFalse(plugin_info.is_symlink())
        for path, data in metadata.items():
            self.assertFalse(path.is_symlink())
            self.assertEqual(path.read_bytes(), data)
        self.assertFalse(unscanned.is_symlink())
        self.assertEqual(relocate_non_macho(self.app, CHECKER.is_macho), [])

    @unittest.skipUnless(os.name == 'posix', 'Requires real POSIX resource aliases')
    def test_external_alias_is_rejected_before_moving_app_data(self):
        foreign = self.root / 'foreign'
        foreign.mkdir()
        (self.macos / 'escape').symlink_to(foreign, target_is_directory=True)
        with self.assertRaisesRegex(RuntimeError, 'escapes'):
            relocate_non_macho(self.app, CHECKER.is_macho)
        self.assertEqual(list(foreign.iterdir()), [])
        (self.macos / 'escape').unlink()
        child = self.app / 'Contents/Helpers/Child.app'
        (child / 'Contents/MacOS').mkdir(parents=True)
        script = child / 'Contents/MacOS/helper.sh'
        script.write_bytes(b'#!/bin/sh\nexit 0\n')
        resources = self.app / 'Contents/Resources'
        resources.mkdir()
        (child / 'Contents/Resources').symlink_to(resources, target_is_directory=True)
        with self.assertRaisesRegex(RuntimeError, 'escapes'):
            relocate_non_macho(self.app, CHECKER.is_macho)
        self.assertFalse(script.is_symlink())
        self.assertEqual(list(resources.iterdir()), [])


@unittest.skipUnless(sys.platform == 'darwin', 'Requires real Apple signing and disk-image tools')
class MacNativeSigningTests(unittest.TestCase):
    def setUp(self):
        for tool in ('clang', 'codesign', 'ditto', 'hdiutil', 'lipo', '/usr/bin/xattr'):
            self.assertIsNotNone(shutil.which(tool), f'Missing native signing tool: {tool}')
        self.temporary = tempfile.TemporaryDirectory(prefix='openrv-native-signing-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.originals = self.root / 'originals'
        self.originals.mkdir()

    def command(self, *arguments, env=None):
        result = subprocess.run(list(map(str, arguments)), capture_output=True, text=True,
                                timeout=60, env=env)
        self.assertEqual(result.returncode, 0,
                         f'Native signing command failed: {arguments}\n{result.stderr[-6000:]}')
        return result.stdout

    def compile(self, name, *, framework=False, handler=False, bundle=False):
        source = self.originals / (name + '.c')
        text = ('#include <stdio.h>\nint main(int argc, char **argv) { '
                'for(int i=1; i<argc; i++) fprintf(stderr,"arg:%s\\n",argv[i]); return 0; }\n'
                if handler else 'int main(void) { return 0; }\n')
        if framework or bundle:
            text = 'int fixture_framework_value(void) { return 42; }\n'
        source.write_text(text, encoding='utf-8')
        binary = self.originals / name
        arguments = ['clang', source, '-Wl,-headerpad_max_install_names', '-o', binary]
        if framework:
            arguments += ['-arch', 'x86_64', '-arch', 'arm64', '-dynamiclib',
                          '-install_name', '@rpath/Tiny.framework/Versions/A/Tiny']
        elif bundle:
            arguments += ['-bundle']
        self.command(*arguments)
        return binary

    def signature_identity(self, path, architecture):
        result = subprocess.run(['codesign', '-d', '-r-', '--verbose=4', '-a', architecture, str(path)],
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr[-6000:])
        lines = (result.stdout + '\n' + result.stderr).splitlines()
        hashes = [line.strip() for line in lines if line.strip().startswith('CDHash=')]
        requirements = [line.strip().lstrip('# ') for line in lines if 'designated =>' in line]
        self.assertEqual(len(hashes), 1, lines)
        self.assertEqual(len(requirements), 1, lines)
        self.assertIn('cdhash', requirements[0])  # Retain the default ad-hoc identity.
        print(f'Native framework identity {path.parent.name}/{path.name} ({architecture}): '
              f'{hashes[0]}; {requirements[0]}', flush=True)
        return hashes[0], requirements[0]

    @staticmethod
    def info(path, executable=None, *, framework=False, version='1', bundle=False):
        data = {'CFBundleIdentifier': 'org.openrv.fixture.' + (executable or 'resources'),
                'CFBundleName': executable or 'Resources', 'CFBundleVersion': version,
                'CFBundleShortVersionString': '1.0',
                'CFBundlePackageType': 'FMWK' if framework else ('APPL' if executable and not bundle else 'BNDL')}
        if executable:
            data['CFBundleExecutable'] = executable
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('wb') as stream:
            plistlib.dump(data, stream)

    def assert_wrapper(self, app, phase):
        script = app / 'Contents/MacOS/crashpad_handler_macos.sh'
        self.assertTrue(script.is_symlink())
        self.assertFalse(os.path.isabs(os.readlink(script)))
        resource = script.resolve(strict=True)
        self.assertTrue(resource.is_relative_to(app / 'Contents/Resources'))
        attributes = self.command('/usr/bin/xattr', resource).splitlines()
        self.assertFalse(any(name.startswith('com.apple.cs.') for name in attributes))
        environment = os.environ.copy()
        log_dir = self.root / ('wrapper-log-' + phase)
        environment['RV_CRASH_DUMPS_DIR'] = str(log_dir)
        self.command(script, '--fixture', 'argument with spaces', env=environment)
        self.assertEqual((log_dir / 'crashpad_handler.log').read_text(encoding='utf-8'),
                         'arg:--fixture\narg:argument with spaces\n')
        verify_app(app)  # Executing the old entry point must not mutate the signed app.

    def assert_plugin_resources(self, app, expected, phase):
        for relative, (data, mode, owner_relative) in expected.items():
            original = app / relative
            owner = app / owner_relative
            resource = original.resolve(strict=True)
            self.assertTrue(resource.is_relative_to(owner / 'Contents/Resources'))
            self.assertEqual(original.read_bytes(), data)
            self.assertEqual(stat.S_IMODE(original.stat().st_mode), mode)
            attributes = self.command('/usr/bin/xattr', resource).splitlines()
            self.assertFalse(any(name.startswith('com.apple.cs.') for name in attributes))
        python_root = app / 'Contents/PlugIns/Python'
        self.assertFalse(python_root.is_symlink())  # The native neighbor stays in place.
        native = python_root / 'native_neighbor.dylib'
        self.assertFalse(native.is_symlink())
        self.assertTrue(CHECKER.is_macho(native))
        self.command('codesign', '--verify', '--strict', '--verbose=2', native)
        output = self.command(sys.executable, '-B', python_root / 'plugin.py', 'argument with spaces')
        self.assertEqual(output, 'plugin-argv:argument with spaces\n')
        verify_app(app)  # Reading Mu and executing Python must not modify the app.
        print(f'Native code-root resources ({phase}): {len(expected)} byte/mode/xattr checks; '
              'legacy Python entry and native neighbor verified', flush=True)

    def test_inside_out_signing_scripts_nested_bundles_and_archive_roundtrips(self):
        app = self.root / 'Fixture.app'
        macos = app / 'Contents/MacOS'
        macos.mkdir(parents=True)
        main = self.compile('Fixture')
        handler = self.compile('crashpad_handler', handler=True)
        shutil.copy2(main, macos / 'Fixture')
        shutil.copy2(handler, macos / 'crashpad_handler')
        self.info(app / 'Contents/Info.plist', 'Fixture')
        template = ROOT / 'src/bin/apps/rv/crashpad_handler_macos.sh.in'
        wrapper_bytes = template.read_bytes().replace(b'@RV_INTERNAL_ORGANIZATION_NAME@', b'Fixture')
        wrapper = macos / 'crashpad_handler_macos.sh'
        wrapper.write_bytes(wrapper_bytes)
        wrapper.chmod(0o755)
        self.command('codesign', '--force', '--sign', '-', '--timestamp=none', macos / 'crashpad_handler')
        legacy = subprocess.run(['codesign', '--force', '--sign', '-', '--timestamp=none', str(macos / 'Fixture')],
                                capture_output=True, text=True, timeout=60)
        self.assertNotEqual(legacy.returncode, 0)
        self.assertIn('crashpad_handler_macos.sh', legacy.stderr)

        child = app / 'Contents/Helpers/Child.app'
        child_main = self.compile('Child')
        self.info(child / 'Contents/Info.plist', 'Child')
        (child / 'Contents/MacOS').mkdir()
        shutil.copy2(child_main, child / 'Contents/MacOS/Child')
        (child / 'Contents/MacOS/helper.sh').write_bytes(b'#!/bin/sh\nexit 0\n')
        (child / 'Contents/MacOS/helper.sh').chmod(0o755)
        data = macos / 'deploy_lib/module.py'
        data.parent.mkdir()
        data.write_bytes(b'fixture_value = 42\n')

        framework = app / 'Contents/Frameworks/Tiny.framework'
        tiny = self.compile('Tiny', framework=True)
        expected_archs = set(self.command('lipo', '-archs', tiny).split())
        for version in ('A', 'B'):
            version_dir = framework / 'Versions' / version
            # Default ad-hoc DRs use cdhash, so mirrored versions must have the
            # same complete signed content to satisfy the Current version's DR.
            self.info(version_dir / 'Resources/Info.plist', 'Tiny', framework=True)
            shutil.copy2(tiny, version_dir / 'Tiny')
        (framework / 'Versions/Current').symlink_to('A', target_is_directory=True)
        (framework / 'Tiny').symlink_to('Versions/Current/Tiny')
        (framework / 'Resources').symlink_to('Versions/Current/Resources', target_is_directory=True)
        resource_bundle = app / 'Contents/Resources/Data.bundle'
        self.info(resource_bundle / 'Info.plist')
        (resource_bundle / 'data.txt').write_bytes(b'sealed resource-only bundle')

        # Reproduce the actual Mu deployment and a mixed Python/native plug-in
        # directory. Exercise every documented code root without moving native code.
        expected_resources = {}

        def data_file(relative, data, owner_relative=''):
            path = app / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            expected_resources[relative] = (data, stat.S_IMODE(path.stat().st_mode), owner_relative)

        for relative in CODE_ROOT_FIXTURES:
            data_file('Contents/' + relative + '/same-name.txt', relative.encode('ascii'))
        data_file('Contents/direct-data.txt', b'direct resource')
        mu_source = ROOT / 'src/lib/app/mu_rvui/timeline.mu'
        data_file('Contents/PlugIns/Mu/timeline.mu', mu_source.read_bytes())
        data_file('Contents/PlugIns/Python/plugin.py',
                  b'from pathlib import Path\nimport sys\n'
                  b'assert (Path(__file__).parent / "native_neighbor.dylib").is_file()\n'
                  b'print("plugin-argv:" + sys.argv[1])\n')
        shutil.copy2(tiny, app / 'Contents/PlugIns/Python/native_neighbor.dylib')
        data_file('Contents/Helpers/Child.app/Contents/PlugIns/Mu/timeline.mu', mu_source.read_bytes(),
                  'Contents/Helpers/Child.app')
        native_plugin = app / 'Contents/PlugIns/Custom.plugin'
        plugin_binary = self.compile('NativePlugin', bundle=True)
        self.info(native_plugin / 'Contents/Info.plist', 'NativePlugin', bundle=True)
        (native_plugin / 'Contents/MacOS').mkdir()
        shutil.copy2(plugin_binary, native_plugin / 'Contents/MacOS/NativePlugin')
        data_file('Contents/PlugIns/Custom.plugin/Contents/PlugIns/Python/plugin.py', b'value = 42\n',
                  'Contents/PlugIns/Custom.plugin')

        source_hashes = {path: sha256(path) for path in (main, handler, child_main, tiny, plugin_binary, mu_source)}
        records = sign_app(app, CHECKER.is_macho)
        self.assertTrue(any(record['path'] == 'Contents/MacOS/crashpad_handler_macos.sh' for record in records))
        self.assertEqual(wrapper.read_bytes(), wrapper_bytes)
        self.assertEqual(stat.S_IMODE(wrapper.stat().st_mode), 0o755)
        self.assertTrue(data.parent.is_symlink())
        child_script = child / 'Contents/MacOS/helper.sh'
        self.assertTrue(child_script.is_symlink())
        self.assertFalse(os.path.isabs(os.readlink(child_script)))
        self.assertTrue(child_script.resolve().is_relative_to(child / 'Contents/Resources'))
        self.assert_wrapper(app, 'original')
        self.assert_plugin_resources(app, expected_resources, 'original')
        self.assertFalse(native_plugin.is_symlink())
        identities = []
        for version in ('A', 'B'):
            version_dir = framework / 'Versions' / version
            self.command('codesign', '--verify', '--strict', '--verbose=2', version_dir)
            self.assertEqual(set(self.command('lipo', '-archs', version_dir / 'Tiny').split()), expected_archs)
            identities.append({arch: self.signature_identity(version_dir, arch)
                               for arch in sorted(expected_archs)})
        self.assertEqual(identities[0], identities[1])
        self.assertEqual(source_hashes, {path: sha256(path) for path in source_hashes})

        archive = self.root / 'fixture.zip'
        self.command('ditto', '-c', '-k', '--sequesterRsrc', '--keepParent', app, archive)
        extracted = self.root / 'extracted'
        self.command('ditto', '-x', '-k', archive, extracted)
        self.assert_wrapper(extracted / app.name, 'zip')
        self.assert_plugin_resources(extracted / app.name, expected_resources, 'zip')

        staging = self.root / 'dmg-staging'
        staging.mkdir()
        self.command('ditto', app, staging / app.name)
        (staging / 'Applications').symlink_to('/Applications', target_is_directory=True)
        image = self.root / 'fixture.dmg'
        self.command('hdiutil', 'create', '-volname', 'OpenRV-Fixture', '-srcfolder', staging,
                     '-ov', '-format', 'UDZO', image)
        self.command('hdiutil', 'verify', image)
        mount = self.root / 'mounted-image'
        mount.mkdir()
        attached = False
        try:
            self.command('hdiutil', 'attach', '-readonly', '-nobrowse', '-mountpoint', mount, image)
            attached = True
            self.assert_wrapper(mount / app.name, 'dmg')
            self.assert_plugin_resources(mount / app.name, expected_resources, 'dmg')
        finally:
            if attached:
                self.command('hdiutil', 'detach', mount)

    def test_different_ad_hoc_framework_versions_remain_strictly_rejected(self):
        app = self.root / 'DifferentVersions.app'
        macos = app / 'Contents/MacOS'
        macos.mkdir(parents=True)
        main = self.compile('DifferentVersions')
        shutil.copy2(main, macos / main.name)
        self.info(app / 'Contents/Info.plist', main.name)
        framework = app / 'Contents/Frameworks/Tiny.framework'
        tiny = self.compile('Tiny', framework=True)
        architectures = set(self.command('lipo', '-archs', tiny).split())
        for version in ('A', 'B'):
            version_dir = framework / 'Versions' / version
            self.info(version_dir / 'Resources/Info.plist', 'Tiny', framework=True,
                      version='1' if version == 'A' else '2')
            shutil.copy2(tiny, version_dir / 'Tiny')
        (framework / 'Versions/Current').symlink_to('A', target_is_directory=True)
        (framework / 'Tiny').symlink_to('Versions/Current/Tiny')
        (framework / 'Resources').symlink_to('Versions/Current/Resources', target_is_directory=True)
        source_hashes = {path: sha256(path) for path in (main, tiny)}
        with self.assertRaises(subprocess.CalledProcessError) as raised:
            sign_app(app, CHECKER.is_macho)
        self.assertIn('--verify', raised.exception.cmd)
        identities = []
        for version in ('A', 'B'):
            version_dir = framework / 'Versions' / version
            # Both versions are signed and individually valid; rejecting the
            # parent must not be mistaken for an unsigned or omitted version.
            self.command('codesign', '--verify', '--strict', '--verbose=2', version_dir)
            identities.append({arch: self.signature_identity(version_dir, arch)
                               for arch in sorted(architectures)})
        self.assertNotEqual(identities[0], identities[1])
        parent = subprocess.run(['codesign', '--verify', '--deep', '--strict', '--verbose=2', str(app)],
                                capture_output=True, text=True, timeout=60)
        self.assertNotEqual(parent.returncode, 0)
        self.assertIn('embedded framework contains modified or invalid version', parent.stderr)
        self.assertEqual(source_hashes, {path: sha256(path) for path in source_hashes})
        print('Native distinct-version framework: each version valid; parent strict verification rejected',
              flush=True)


if __name__ == '__main__':
    unittest.main()
