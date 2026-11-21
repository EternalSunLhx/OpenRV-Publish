"""Actual CMake/Git metadata checks with unrelated publish-only/upstream histories."""
from pathlib import Path
import os
import shutil
import subprocess
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import official_workspace as workspace
import release_common
from lightweight_fixture import lightweight_fixture, prepare_fixture

CMAKE = os.environ.get('OPENRV_TEST_CMAKE') or shutil.which('cmake')


def materialized_fixture(fixture):
    plan, digest, inputs = prepare_fixture(fixture)
    repo = fixture['root'] / 'matrix'
    subprocess.run(['git', 'clone', '--no-hardlinks', str(fixture['trusted']), str(repo)],
                   env=workspace.watcher().git_environment(), check=True, capture_output=True, timeout=60)
    workspace.materialize(repo, inputs, digest, fixture['snapshot'], 29)
    return repo, plan


@unittest.skipUnless(CMAKE and shutil.which('git'), 'Native CMake and Git are required')
class NativeSourceReferenceTests(unittest.TestCase):
    def configure(self, fixture, repo):
        root = fixture['root']
        driver = root / 'source-metadata.cmake'
        driver.write_text('include("${REPO}/src/lib/app/RvCommon/RvLocalizedBuild.cmake")\n'
                          'RV_CONFIGURE_LOCALIZED_BUILD("${REPO}" "${OUT}" "4.0.0")\n', encoding='utf-8')
        environment = workspace.watcher().clean_environment()
        environment.update(ORV_RELEASE_TAG='v4.0.3', ORV_BUILD_NUMBER='29')
        environment['PATH'] = str(Path(sys.executable).parent) + os.pathsep + environment.get('PATH', '')
        output = root / 'generated-source-metadata'
        result = subprocess.run([str(CMAKE), '-DREPO=' + str(repo), '-DOUT=' + str(output),
                                 '-P', str(driver)], env=environment, text=True, encoding='utf-8',
                                capture_output=True, timeout=90)
        header = output / 'RvLocalizedBuild.h'
        return result, header.read_text(encoding='utf-8') if header.is_file() else ''

    def test_actual_cmake_and_packager_use_private_upstream_ref_not_owned_publication_tag(self):
        for annotated in (False, True):
            with self.subTest(annotated=annotated), lightweight_fixture(include_metadata=True, annotated=annotated) as f:
                repo, plan = materialized_fixture(f)
                git = f['git']
                private = 'refs/openrv-upstream/tags/v4.0.3'
                self.assertEqual(plan['schema'], 4)
                self.assertEqual(git(repo, 'rev-parse', 'refs/tags/v4.0.3'), f['snapshot'])
                self.assertEqual(git(repo, 'rev-parse', private), f['release'].object_sha)
                self.assertEqual(git(repo, 'rev-parse', private + '^{commit}'), f['release'].commit_sha)
                self.assertNotEqual(f['snapshot'], git(repo, 'rev-parse', 'HEAD'))
                result, header = self.configure(f, repo)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn('#define RV_LOCALIZED_VERSION "4.0.3+build.29"', header)
                self.assertIn('#define RV_LOCALIZED_NATIVE_VERSION "4.0.0"', header)
                self.assertIn('#define RV_LOCALIZED_BUILD_NUMBER 29ULL', header)
                with mock.patch.dict(os.environ, {'ORV_RELEASE_TAG': 'v4.0.3', 'ORV_BUILD_NUMBER': '29'}):
                    release_common.validate_release_tag(repo, 'v4.0.3')
                git(repo, 'update-ref', '-d', private)
                result, unused = self.configure(f, repo)
                # The source verifier can restore a missing private alias only
                # from the already verified original object in the bound plan.
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(git(repo, 'rev-parse', private), f['release'].object_sha)
                self.assertEqual(git(repo, 'rev-parse', 'refs/tags/v4.0.3'), f['snapshot'])
                with mock.patch.dict(os.environ, {'ORV_RELEASE_TAG': 'v4.0.3', 'ORV_BUILD_NUMBER': '29'}):
                    release_common.validate_release_tag(repo, 'v4.0.3')

    def test_wrong_private_ref_is_not_accepted_even_if_publication_tag_exists(self):
        with lightweight_fixture(include_metadata=True) as f:
            repo, unused = materialized_fixture(f)
            f['git'](repo, 'update-ref', 'refs/openrv-upstream/tags/v4.0.3', f['snapshot'])
            result, unused = self.configure(f, repo)
            self.assertNotEqual(result.returncode, 0)
            with mock.patch.dict(os.environ, {'ORV_RELEASE_TAG': 'v4.0.3', 'ORV_BUILD_NUMBER': '29'}), \
                    self.assertRaises((ValueError, RuntimeError, subprocess.CalledProcessError)):
                release_common.validate_release_tag(repo, 'v4.0.3')


if __name__ == '__main__':
    unittest.main()
