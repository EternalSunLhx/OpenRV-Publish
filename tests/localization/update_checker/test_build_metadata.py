"""Exercise the actual CMake metadata function using temporary local Git repositories."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[3]
MODULE = ROOT / 'overlay/src/lib/app/RvCommon/RvLocalizedBuild.cmake'
if not MODULE.is_file():
    MODULE = ROOT / 'src/lib/app/RvCommon/RvLocalizedBuild.cmake'
CMAKE = os.environ.get('OPENRV_TEST_CMAKE') or shutil.which('cmake')


@unittest.skipUnless(CMAKE and shutil.which('git'), 'CMake and Git are required')
class BuildMetadataTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='openrv-update-metadata-')
        self.root = Path(self.temporary.name).resolve()
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        self.hooks = self.root / 'empty-hooks'
        self.hooks.mkdir()
        self.git('init', '-b', 'main')
        self.commit('first')
        self.driver = self.root / 'driver.cmake'
        self.driver.write_text('''set(RV_GIT_COMMIT_SHORT_HASH deadbeef CACHE STRING "stale fixture")
include("${MODULE}")
RV_CONFIGURE_LOCALIZED_BUILD("${REPO}" "${OUT}" "4.1.0")
''', encoding='utf-8')

    def tearDown(self):
        # This directory was created exclusively by this fixture; no repository
        # or path supplied by a candidate checkout is eligible for cleanup.
        assert self.repo.resolve().is_relative_to(self.root)
        self.temporary.cleanup()

    def git(self, *arguments):
        return subprocess.check_output(['git', '-c', 'core.hooksPath=' + str(self.hooks),
                                        '-c', 'init.templateDir=' + str(self.hooks),
                                        '-c', 'user.name=OpenRV fixture', '-c', 'user.email=openrv-test@invalid.example',
                                        '-C', str(self.repo), *arguments], text=True, encoding='utf-8', stderr=subprocess.DEVNULL).strip()

    def commit(self, text):
        (self.repo / 'marker').write_text(text, encoding='utf-8')
        self.git('add', 'marker')
        self.git('commit', '-m', text)
        return self.git('rev-parse', 'HEAD')

    def configure(self, tag=''):
        environment = os.environ.copy()
        for name in tuple(environment):
            if name in ('GH_TOKEN', 'GITHUB_TOKEN', 'GIT_ASKPASS', 'SSH_ASKPASS') or name.startswith('GIT_CONFIG_'):
                environment.pop(name, None)
        environment['ORV_RELEASE_TAG'] = tag
        output = self.root / 'generated'
        result = subprocess.run([str(CMAKE), '-DMODULE=' + str(MODULE), '-DREPO=' + str(self.repo),
                                 '-DOUT=' + str(output), '-P', str(self.driver)], env=environment,
                                text=True, encoding='utf-8', capture_output=True, timeout=20)
        header = output / 'RvLocalizedBuild.h'
        return result, header.read_text(encoding='utf-8') if header.exists() else ''

    def test_development_metadata_uses_fresh_head(self):
        result, header = self.configure()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('4.1.0-dev.' + self.git('rev-parse', 'HEAD')[:8], header)
        self.assertNotIn('deadbeef', header)
        self.assertIn('#define RV_LOCALIZED_RELEASE_TAG ""', header)
        self.assertIn('#define RV_LOCALIZED_BUILD_NUMBER 0ULL', header)

    def test_lightweight_and_annotated_tags_at_exact_head(self):
        for edition, annotated in ((2, False), (3, True)):
            tag = f'v4.1.0-zh.{edition}'
            if annotated:
                self.git('tag', '-a', tag, '-m', 'fixture tag')
            else:
                self.git('tag', tag)
            result, header = self.configure(tag)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(f'#define RV_LOCALIZED_VERSION "4.1.0-zh.{edition}"', header)
            self.assertIn(f'#define RV_LOCALIZED_RELEASE_TAG "{tag}"', header)

    def test_tag_for_another_core_version_is_rejected(self):
        self.git('tag', 'v4.2.0-zh.1')
        result, unused = self.configure('v4.2.0-zh.1')
        self.assertNotEqual(result.returncode, 0)

    def test_tag_pointing_to_another_commit_is_rejected(self):
        self.git('tag', 'v4.1.0-zh.1')
        self.commit('second')
        result, unused = self.configure('v4.1.0-zh.1')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('does not identify the exact checkout', result.stderr)

    def test_missing_tag_is_not_a_released_build(self):
        result, unused = self.configure('v4.1.0-zh.1')
        self.assertNotEqual(result.returncode, 0)

    def test_official_tag_needs_workspace_provenance_even_when_native_version_matches(self):
        self.git('tag', 'v4.1.0')
        with mock.patch.dict(os.environ, {'ORV_BUILD_NUMBER': '17'}):
            result, unused = self.configure('v4.1.0')
        self.assertNotEqual(result.returncode, 0)

    def test_invalid_or_overflowing_editions_are_rejected(self):
        for tag in ('v4.1.0-zh.0', 'v4.1.0-zh.01', 'v4.1.0-zh.18446744073709551616', 'v4.1.0-zh.1\n'):
            result, unused = self.configure(tag)
            self.assertNotEqual(result.returncode, 0, tag)


if __name__ == '__main__':
    unittest.main()
