"""Release/native-version separation with real, strictly local Git compositions."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

CI = Path(__file__).resolve().parents[1]
ROOT = CI.parents[1]
sys.path.insert(0, str(CI))
from official_release import release_identity
from release_common import source_metadata, validate_release_tag
import prepare_release

FIXTURE_SPEC = importlib.util.spec_from_file_location('release_metadata_git_fixture', ROOT / 'ci/upstream-release-watch/tests/test_watch.py')
fixtures = importlib.util.module_from_spec(FIXTURE_SPEC)
FIXTURE_SPEC.loader.exec_module(fixtures)
CMAKE = os.environ.get('OPENRV_TEST_CMAKE') or shutil.which('cmake')


class OfficialReleaseMetadataTests(unittest.TestCase):
    def fixture(self):
        return fixtures.GitIntegrationTests().fixture(native_version=(4, 0, 0), optional_component='absent', include_metadata=True)

    def candidate(self, fixture):
        result = fixtures.GitIntegrationTests().prepare(fixture, 'candidate')
        repo = fixture['root'] / 'candidate'
        fixtures.GitIntegrationTests().git(repo, 'tag', result[2])
        return repo, result[2]

    def test_real_composition_publishes_official_version_and_records_native_version(self):
        with self.fixture() as fixture:
            repo, tag = self.candidate(fixture)
            identity = release_identity(repo, tag, '4.0.0')
            self.assertEqual((identity['version'], identity['nativeVersion']), ('4.0.3', '4.0.0'))
            self.assertEqual(identity['upstreamRelease']['commit_sha'], fixture['release'].commit_sha)
            self.assertEqual(identity['omittedUpstreamComponents'], ['src/plugins/rv-packages/annotate_beta'])
            with mock.patch.dict(os.environ, {'ORV_RELEASE_TAG': tag}):
                metadata = source_metadata(repo)
                validate_release_tag(repo, tag)
            self.assertEqual((metadata['version'], metadata['nativeVersion'], metadata['releaseVersion']),
                             ('4.0.3', '4.0.0', '4.0.3-zh.1'))
            prepare_release.validate_checkout(repo, fixtures.watch.git(repo, 'rev-parse', 'HEAD'), tag)

    def test_mismatch_without_composition_remains_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            with self.assertRaisesRegex(ValueError, 'without verified official provenance'):
                release_identity(repo, 'v4.0.3-zh.1', '4.0.0')
            self.assertEqual(release_identity(repo, 'v4.0.0-zh.1', '4.0.0')['version'], '4.0.0')

    def test_wrong_tag_native_version_and_altered_patch_cannot_authorize_a_mismatch(self):
        with self.fixture() as fixture:
            repo, tag = self.candidate(fixture)
            for changed_tag, native in (('v4.0.4-zh.1', '4.0.0'), (tag, '4.0.1')):
                with self.subTest(tag=changed_tag, native=native), self.assertRaises(ValueError):
                    release_identity(repo, changed_tag, native)
            path = repo / fixtures.watch.PLAN_FILE
            plan = json.loads(path.read_text()); plan['localization_patch_sha256'] = '0' * 64
            path.write_text(json.dumps(plan) + '\n')
            with self.assertRaisesRegex(ValueError, 'composition or feature patch digest'):
                release_identity(repo, tag, '4.0.0')

    def test_preflight_refresh_rejects_an_upstream_tag_move(self):
        with self.fixture() as fixture:
            repo, tag = self.candidate(fixture)
            fixtures.GitIntegrationTests().git(fixture['upstream'], 'update-ref', 'refs/tags/v4.0.3', fixture['patch_base'])
            with self.assertRaisesRegex(ValueError, 'tag changed'):
                release_identity(repo, tag, '4.0.0', refresh=True)

    @unittest.skipUnless(CMAKE, 'CMake is required')
    def test_actual_cmake_generates_official_updater_version_and_rejects_tampering(self):
        with self.fixture() as fixture:
            repo, tag = self.candidate(fixture)
            driver = fixture['root'] / 'metadata.cmake'
            driver.write_text('include("${REPO}/src/lib/app/RvCommon/RvLocalizedBuild.cmake")\n'
                              'RV_CONFIGURE_LOCALIZED_BUILD("${REPO}" "${OUT}" "4.0.0")\n')
            output = fixture['root'] / 'generated'
            environment = fixtures.watch.clean_environment(); environment['ORV_RELEASE_TAG'] = tag
            command = [str(CMAKE), '-DREPO=' + str(repo), '-DOUT=' + str(output), '-P', str(driver)]
            result = subprocess.run(command, env=environment, capture_output=True, text=True, timeout=90)
            self.assertEqual(result.returncode, 0, result.stderr)
            header = (output / 'RvLocalizedBuild.h').read_text()
            self.assertIn('#define RV_LOCALIZED_MAJOR 4', header)
            self.assertIn('#define RV_LOCALIZED_MINOR 0', header)
            self.assertIn('#define RV_LOCALIZED_PATCH 3', header)
            self.assertIn('#define RV_LOCALIZED_NATIVE_VERSION "4.0.0"', header)
            self.assertIn('#define RV_LOCALIZED_VERSION "4.0.3-zh.1"', header)
            plan_path = repo / fixtures.watch.PLAN_FILE
            plan = json.loads(plan_path.read_text()); plan['composed_tree_sha'] = '0' * 40
            plan_path.write_text(json.dumps(plan) + '\n')
            result = subprocess.run(command, env=environment, capture_output=True, text=True, timeout=90)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('lacks verified official provenance', result.stderr)


class MacBundleVersionTests(unittest.TestCase):
    def test_bundle_records_official_and_native_versions_before_signing(self):
        spec = importlib.util.spec_from_file_location('release_metadata_macos_packager', CI / 'package-macos.py')
        package = importlib.util.module_from_spec(spec); spec.loader.exec_module(package)
        with tempfile.TemporaryDirectory() as directory:
            app = Path(directory) / 'RV.app'
            contents = app / 'Contents'; contents.mkdir(parents=True)
            path = contents / 'Info.plist'
            path.write_bytes(plistlib.dumps({'CFBundleShortVersionString': '4.0.0', 'LSMinimumSystemVersion': '14.0'}))
            package.update_bundle_metadata(app, {'version': '4.0.3', 'nativeVersion': '4.0.0', 'releaseVersion': '4.0.3-zh.2'}, '13.0')
            info = plistlib.loads(path.read_bytes())
            self.assertEqual((info['CFBundleShortVersionString'], info['CFBundleVersion']), ('4.0.3', '4.0.3'))
            self.assertEqual((info['OpenRVReleaseVersion'], info['OpenRVNativeVersion']), ('4.0.3-zh.2', '4.0.0'))
            self.assertEqual(info['LSMinimumSystemVersion'], '14.0')


if __name__ == '__main__':
    unittest.main()
