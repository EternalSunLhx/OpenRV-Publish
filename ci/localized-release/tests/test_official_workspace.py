"""Real offline compositions: official HEAD, immutable main patch and numbered rebuilds."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
import json
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
import zipfile
from unittest import mock

import test_release_metadata as fixtures
import official_workspace as workspace
from official_release import release_identity
from release_common import source_metadata, validate_release_tag, write_checksums
import prepare_release
from assemble_release import assemble, write_localization_archive


class OfficialWorkspaceTests(unittest.TestCase):
    @contextmanager
    def fixture(self, **options):
        harness = fixtures.fixtures.GitIntegrationTests()
        with harness.fixture(native_version=(4, 0, 0), optional_component='absent', include_metadata=True, **options) as f:
            trusted = f['trusted']
            implementation = Path(__file__).resolve().parents[1]
            target = trusted / 'ci/localized-release/official_workspace.py'
            target.write_bytes((implementation / 'official_workspace.py').read_bytes())
            harness.git(trusted, 'add', '.')
            harness.git(trusted, 'commit', '-m', 'official workspace fixture tools')
            f['snapshot'] = harness.git(trusted, 'rev-parse', 'HEAD')
            with mock.patch.dict(os.environ, {'ORV_BUILD_PLAN_SHA256': ''}):
                yield f

    def prepare(self, f, number=17, checker=lambda *args: None):
        output = f['root'] / ('input-' + str(number))
        plan, plan_hash = workspace.prepare_workspace(f['trusted'], f['root'] / ('source-' + str(number)),
                                                     output, f['release'], number, checker=checker)
        return plan, plan_hash, output

    def materialize(self, f, plan, plan_hash, output, name='matrix'):
        repo = f['root'] / name
        subprocess.run(['git', 'clone', '--no-hardlinks', str(f['trusted']), str(repo)], check=True, capture_output=True)
        workspace.materialize(repo, output, plan_hash, f['snapshot'], plan['build_number'])
        return repo

    def test_official_head_and_tag_remain_original_while_tree_contains_only_stable_plus_features(self):
        with self.fixture() as f:
            plan, plan_hash, output = self.prepare(f)
            repo = self.materialize(f, plan, plan_hash, output)
            w = workspace.watcher()
            self.assertEqual(w.git(repo, 'rev-parse', 'HEAD'), f['release'].commit_sha)
            self.assertEqual(w.git(repo, 'rev-parse', 'refs/tags/v4.0.3'), f['release'].object_sha)
            self.assertEqual(w.git(repo, 'rev-parse', 'refs/heads/main'), f['snapshot'])
            self.assertEqual(w.git(repo, 'for-each-ref', '--format=%(refname)', 'refs/heads'), 'refs/heads/main')
            self.assertTrue((repo / 'stable-only.cpp').is_file())
            self.assertFalse((repo / 'dev-only.cpp').exists())
            self.assertTrue((repo / 'language.cpp').is_file())
            self.assertEqual(w.base_version(repo), '4.0.0')
            self.assertEqual(w.git(repo, 'write-tree'), plan['patched_tree'])
            self.assertEqual(w.git(repo, 'rev-parse', plan['patched_tree'] + ':.github/workflows'),
                             w.git(repo, 'rev-parse', f['snapshot'] + ':.github/workflows'))
            self.assertEqual(plan['omitted_upstream_components'], ['src/plugins/rv-packages/annotate_beta'])
            with mock.patch.dict(os.environ, {'ORV_RELEASE_TAG': 'v4.0.3', 'ORV_BUILD_NUMBER': '17'}):
                metadata = source_metadata(repo); validate_release_tag(repo, 'v4.0.3')
                self.assertEqual((metadata['version'], metadata['nativeVersion'], metadata['releaseVersion']),
                                 ('4.0.3', '4.0.0', '4.0.3-build.17'))
                self.assertEqual(metadata['patchedSourceTree'], plan['patched_tree'])

    def test_generated_localization_delta_is_bound_and_replayed_on_every_matrix(self):
        with self.fixture() as f:
            def checker(trusted, repo, output):
                target = repo / sorted(workspace.GENERATED_PATHS)[0]
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text('// generated menu paths\n')
                workspace.watcher().git(repo, 'add', '-N', str(target.relative_to(repo)))
            plan, plan_hash, output = self.prepare(f, checker=checker)
            self.assertNotEqual(plan['patched_tree'], plan['composed_tree'])
            repo = self.materialize(f, plan, plan_hash, output)
            self.assertEqual((repo / sorted(workspace.GENERATED_PATHS)[0]).read_text(), '// generated menu paths\n')
            (output / 'generated-localization.patch').write_bytes(b'tampered')
            with self.assertRaisesRegex(ValueError, 'delta differs'):
                workspace.read_input(output, plan_hash)

    def test_unexpected_generated_source_changes_are_rejected(self):
        with self.fixture() as f:
            def checker(trusted, repo, output):
                (repo / 'app.cpp').write_text('// unexpected generated source change\n')
            with self.assertRaisesRegex(ValueError, 'unexpected source'):
                self.prepare(f, checker=checker)

    def test_rebuild_changes_only_internal_number_and_rejects_snapshot_or_number_races(self):
        with self.fixture() as f:
            plan, plan_hash, output = self.prepare(f)
            second, _, _ = self.prepare(f, 18)
            self.assertEqual(plan['patched_tree'], second['patched_tree'])
            self.assertEqual(plan['official'], second['official'])
            self.assertEqual(second['build_number'], 18)
            for parameters in ({'expected_snapshot': f['patch_base']}, {'expected_build': 18}):
                with self.assertRaises(ValueError):
                    workspace.verify_composition(f['trusted'], plan, b'', **parameters)
            repo = self.materialize(f, plan, plan_hash, output)
            with mock.patch.dict(os.environ, {'ORV_BUILD_NUMBER': '18'}), self.assertRaisesRegex(ValueError, 'build number'):
                release_identity(repo, 'v4.0.3', '4.0.0')

    def test_annotated_official_tag_is_preserved_as_its_original_object(self):
        with self.fixture() as f:
            w = workspace.watcher()
            w.git(f['upstream'], '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
                  'tag', '-a', '-f', 'v4.0.3', f['release'].commit_sha, '-m', 'official annotated fixture')
            f['release'] = replace(f['release'], object_sha=w.git(f['upstream'], 'rev-parse', 'refs/tags/v4.0.3'))
            plan, plan_hash, output = self.prepare(f)
            repo = self.materialize(f, plan, plan_hash, output)
            self.assertNotEqual(f['release'].object_sha, f['release'].commit_sha)
            self.assertEqual(w.git(repo, 'rev-parse', 'refs/tags/v4.0.3'), f['release'].object_sha)

    def test_official_retag_and_conflicting_owned_patch_stop_without_main_writes(self):
        with self.fixture() as f:
            plan, _, _ = self.prepare(f)
            workspace.watcher().git(f['upstream'], 'update-ref', 'refs/tags/v4.0.3', f['patch_base'])
            with self.assertRaisesRegex(ValueError, 'tag changed'):
                workspace.verify_composition(f['trusted'], plan, b'', refresh=True)
        with self.fixture(conflict=True) as f:
            with self.assertRaises(RuntimeError):
                self.prepare(f)
            self.assertEqual(workspace.watcher().git(f['trusted'], 'rev-parse', 'HEAD'), f['snapshot'])


    def test_source_tampering_after_materialization_is_rejected(self):
        with self.fixture() as f:
            plan, plan_hash, output = self.prepare(f)
            repo = self.materialize(f, plan, plan_hash, output)
            (repo / 'language.cpp').write_text('// changed after verification\n')
            with self.assertRaisesRegex(ValueError, 'Working source differs.*language.cpp'):
                workspace.verify_materialized(repo)

    def test_assembly_requires_same_build_and_keeps_required_aac_gate(self):
        with self.fixture() as f:
            plan, plan_hash, output = self.prepare(f)
            repo = self.materialize(f, plan, plan_hash, output)
            assets = f['root'] / 'assets'; assets.mkdir()
            with mock.patch.dict(os.environ, {'ORV_RELEASE_TAG': 'v4.0.3', 'ORV_BUILD_NUMBER': '17'}):
                source = source_metadata(repo)
                for platform, arch in (('Windows', 'x64'), ('macOS', 'x86_64'), ('macOS', 'arm64')):
                    base = f"OpenRV-{source['releaseVersion']}-{platform}-{arch}"
                    files = ([base + '-Setup.exe', base + '-Portable.zip'] if platform == 'Windows'
                             else [base + '.zip', base + '.dmg'])
                    for name in files:
                        (assets / name).write_bytes(b'Offline package shape fixture only\n')
                    manifest = {**source, 'platform': platform, 'architecture': arch, 'files': files,
                                'guiPassed': True,
                                'smoke': [{'name': 'aac-mp4', 'status': 'passed'}] + [
                                    {'name': 'gui-' + language, 'status': 'passed', 'exitCode': 0,
                                     'timedOut': False, 'requestClose': True, 'closeAccepted': True,
                                     'sessionClosed': True} for language in ('en', 'zh_CN')],
                                'installerSmoke': {'status': 'passed', 'removed': True},
                                'languagePersistence': {'status': 'passed',
                                    'preferenceRecovery': {'restore': 'passed', 'verify': 'passed'},
                                    'launches': [{'status': 'passed', 'hostedRunnerPreferencesVerifiedBeforeAction': True,
                                                  'nativeMenuRebuilds': 3, 'requestClose': True,
                                                  'closeAccepted': True, 'sessionClosed': True}] * 3}}
                    name = base + '.build.json'
                    (assets / name).write_text(json.dumps(manifest))
                    write_checksums(assets, files + [name], base + '.sha256.txt')
                windows = assets / 'OpenRV-4.0.3-build.17-Windows-x64.build.json'
                correct = windows.read_bytes()
                manifest = json.loads(correct); manifest['buildNumber'] = 18
                windows.write_text(json.dumps(manifest))
                with self.assertRaisesRegex(ValueError, 'buildNumber'):
                    assemble(repo, assets, 'v4.0.3')
                manifest = json.loads(correct); manifest['smoke'] = []
                windows.write_text(json.dumps(manifest))
                with self.assertRaisesRegex(ValueError, 'AAC MP4'):
                    assemble(repo, assets, 'v4.0.3')
                windows.write_bytes(correct)
                mac = assets / 'OpenRV-4.0.3-build.17-macOS-arm64.build.json'
                correct_mac = mac.read_bytes()
                for change in ({'exitCode': -10}, {'timedOut': True}, {'sessionClosed': False},
                               {'closeAccepted': 1}, {'requestClose': None}, {'name': 'gui-en'}):
                    with self.subTest(gui_shutdown=change):
                        manifest = json.loads(correct_mac)
                        manifest['smoke'][2].update(change)
                        mac.write_text(json.dumps(manifest))
                        with self.assertRaisesRegex(ValueError, 'Mac GUI checks'):
                            assemble(repo, assets, 'v4.0.3')
                for field in ('requestClose', 'closeAccepted', 'sessionClosed'):
                    with self.subTest(missing_lifecycle=field):
                        manifest = json.loads(correct_mac)
                        manifest['languagePersistence']['launches'][2].pop(field)
                        mac.write_text(json.dumps(manifest))
                        with self.assertRaisesRegex(ValueError, 'Mac native chooser persistence'):
                            assemble(repo, assets, 'v4.0.3')
                mac.write_bytes(correct_mac)
                assemble(repo, assets, 'v4.0.3')
            self.assertEqual((assets / 'OpenRV-4.0.3-build.17-source.provenance.json').read_bytes(),
                             (output / 'build-plan.json').read_bytes())
            patch_zip = assets / 'OpenRV-4.0.3-build.17-generated-localization.patch.zip'
            with zipfile.ZipFile(patch_zip) as archive:
                self.assertEqual(archive.read('generated-localization.patch'), b'')
            self.assertGreater(patch_zip.stat().st_size, 0)
            self.assertIn(patch_zip.name, (assets / 'OpenRV-4.0.3-build.17-source.sha256.txt').read_text())

    def test_new_public_cli_retires_legacy_tag_and_branch_modes(self):
        for arguments in (['plan', '--tag', 'v4.1.0-zh.1'], ['apply', '--mode', 'branch']):
            with mock.patch('sys.argv', ['prepare_release.py'] + arguments), self.assertRaises(SystemExit) as error:
                prepare_release.main()
            self.assertEqual(error.exception.code, 2)
        api = workspace.OfficialApi(write=True)
        for method, suffix, payload in (('POST', 'git/refs', {'ref': 'refs/heads/localized/candidate', 'sha': 'a' * 40}),
                                        ('POST', 'git/refs', {'ref': 'refs/tags/v4.0.2-zh.1', 'sha': 'a' * 40}),
                                        ('PATCH', 'git/refs/tags/v4.0.2', {'sha': 'a' * 40})):
            with self.assertRaises(ValueError):
                api.request(method, workspace.TARGET, suffix, payload)

    @unittest.skipUnless(fixtures.CMAKE, 'CMake required')
    def test_native_cmake_official_metadata_requires_exact_provenance_and_positive_number(self):
        with self.fixture() as f:
            plan, plan_hash, output = self.prepare(f)
            repo = self.materialize(f, plan, plan_hash, output)
            driver = f['root'] / 'metadata.cmake'
            driver.write_text('include("${REPO}/src/lib/app/RvCommon/RvLocalizedBuild.cmake")\n'
                              'RV_CONFIGURE_LOCALIZED_BUILD("${REPO}" "${OUT}" "4.0.0")\n')
            command = [str(fixtures.CMAKE), '-DREPO=' + str(repo), '-DOUT=' + str(f['root'] / 'metadata'), '-P', str(driver)]
            env = workspace.watcher().clean_environment(); env.update(ORV_RELEASE_TAG='v4.0.3', ORV_BUILD_NUMBER='17')
            result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=90)
            self.assertEqual(result.returncode, 0, result.stderr)
            header = (f['root'] / 'metadata/RvLocalizedBuild.h').read_text()
            self.assertIn('#define RV_LOCALIZED_VERSION "4.0.3+build.17"', header)
            self.assertIn('#define RV_LOCALIZED_BUILD_NUMBER 17ULL', header)
            for number in ('0', '01', '18446744073709551616', '18'):
                env['ORV_BUILD_NUMBER'] = number
                result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=90)
                self.assertNotEqual(result.returncode, 0, number)


class LocalizationArchiveTests(unittest.TestCase):
    def test_empty_and_nonempty_deltas_are_deterministic_and_restore_exact_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            first, second = Path(directory) / 'first.zip', Path(directory) / 'second.zip'
            for delta in (b'', b'diff fixture\r\nwith trailing bytes \n\xff\0'):
                with self.subTest(size=len(delta)):
                    write_localization_archive(first, delta)
                    write_localization_archive(second, delta)
                    self.assertEqual(first.read_bytes(), second.read_bytes())
                    self.assertGreater(first.stat().st_size, 0)
                    with zipfile.ZipFile(first) as archive:
                        self.assertEqual(archive.namelist(), ['generated-localization.patch'])
                        restored = archive.read('generated-localization.patch')
                        self.assertEqual(restored, delta)
                        self.assertEqual(hashlib.sha256(restored).hexdigest(), workspace.digest(delta))
                        self.assertEqual(archive.getinfo('generated-localization.patch').date_time, (1980, 1, 1, 0, 0, 0))




class NativeWorkflowContractTests(unittest.TestCase):
    def test_native_builds_use_preflight_but_publishing_still_requires_tag_mirror(self):
        workflow = (Path(__file__).resolve().parents[3] / '.github/workflows/localized-release.yml').read_text(encoding='utf-8')
        windows = workflow.split('\n  windows:', 1)[1].split('\n  macos:', 1)[0]
        macos = workflow.split('\n  macos:', 1)[1].split('\n  draft:', 1)[0]
        publisher = workflow.split('\n  draft:', 1)[1]
        for native in (windows, macos):
            self.assertIn('needs: [preflight]', native)
            self.assertIn('prepare_release.py materialize', native)
            self.assertNotIn('needs.prepare', native)
        self.assertIn('needs: [preflight, prepare, windows, macos]', publisher)



if __name__ == '__main__':
    unittest.main()
