"""Use the real MSYS Git/native CMake boundary before Windows dependency builds."""
from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import test_release_metadata as metadata_fixtures
import prepare_release
import release_common
import test_official_workspace as official_fixtures
import official_workspace
from lightweight_fixture import lightweight_fixture
from test_native_source_refs import materialized_fixture

ROOT = Path(__file__).resolve().parents[3]
watch = metadata_fixtures.fixtures.watch


@unittest.skipUnless(os.name == 'nt', 'MSYS Git metadata regression requires Windows')
class MsysGitMetadataTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        msys = os.environ.get('OPENRV_TEST_MSYS_GIT', '')
        cmake = os.environ.get('OPENRV_TEST_CMAKE') or os.environ.get('OPENRV_CMAKE') or shutil.which('cmake')
        if not msys or not Path(msys).is_file() or not cmake or not Path(cmake).is_file():
            message = 'Set OPENRV_TEST_MSYS_GIT and OPENRV_TEST_CMAKE to real Windows executables'
            if os.environ.get('OPENRV_REQUIRE_MSYS_GIT_TESTS') == '1':
                raise RuntimeError(message)
            raise unittest.SkipTest(message)
        cls.msys_git, cls.cmake = Path(msys).resolve(), Path(cmake).resolve()

    def setUp(self):
        environment = os.environ.copy()
        # Reproduce the production failure without a global noglob workaround.
        environment.pop('MSYS', None)
        native_python = Path(environment.get('OPENRV_BUILD_PYTHON', sys.executable)).parent
        environment['PATH'] = os.pathsep.join((str(native_python), str(self.cmake.parent),
                                              str(self.msys_git.parent), environment.get('PATH', '')))
        patch = mock.patch.dict(os.environ, environment, clear=True)
        patch.start(); self.addCleanup(patch.stop)
        self.assertNotIn('MSYS', os.environ)

    def git(self, repo, *arguments):
        return prepare_release.git(repo, *arguments)

    @contextmanager
    def repository(self):
        with tempfile.TemporaryDirectory(prefix='openrv-msys-git-') as directory:
            root = Path(directory); repo = root / 'repo'; repo.mkdir()
            hooks = root / 'empty-hooks'; hooks.mkdir()
            self.git(repo, 'init', '-b', 'main')
            self.git(repo, 'config', 'core.hooksPath', str(hooks))
            self.git(repo, 'config', 'user.name', 'MSYS metadata fixture')
            self.git(repo, 'config', 'user.email', 'msys-metadata@example.invalid')
            (repo / 'marker').write_text('first fixture commit\n')
            self.git(repo, 'add', '.')
            self.git(repo, 'commit', '-m', 'private fixture')
            self.git(repo, 'tag', 'v4.1.0-zh.1')
            self.git(repo, 'tag', '-a', 'v4.1.0-zh.2', '-m', 'private annotated fixture')
            yield root, repo, self.git(repo, 'rev-parse', 'HEAD')

    def configure(self, root, repo, tag, core='4.1.0', module=None):
        driver = root / 'driver.cmake'
        if module is None:
            module = ROOT / 'overlay/src/lib/app/RvCommon/RvLocalizedBuild.cmake'
            if not module.is_file():
                module = ROOT / 'src/lib/app/RvCommon/RvLocalizedBuild.cmake'
        driver.write_text('find_program(SELECTED_GIT git NO_CACHE REQUIRED)\n'
                          'include("${MODULE}")\n'
                          'RV_CONFIGURE_LOCALIZED_BUILD("${REPO}" "${OUT}" "${CORE}")\n'
                          'file(WRITE "${OUT}/selected-git.txt" "${SELECTED_GIT}")\n')
        output = root / ('generated-' + tag)
        environment = os.environ.copy(); environment['ORV_RELEASE_TAG'] = tag
        result = subprocess.run([str(self.cmake), '-DMODULE=' + str(module), '-DREPO=' + str(repo),
                                 '-DOUT=' + str(output), '-DCORE=' + core, '-P', str(driver)],
                                env=environment, capture_output=True, text=True, timeout=90)
        if result.returncode == 0:
            self.assertEqual(Path((output / 'selected-git.txt').read_text()).resolve(), self.msys_git)
        return result, (output / 'RvLocalizedBuild.h').read_text() if (output / 'RvLocalizedBuild.h').exists() else ''

    def test_actual_cmake_resolves_lightweight_and_annotated_tags_with_msys_git(self):
        with self.repository() as (root, repo, unused):
            for tag in ('v4.1.0-zh.1', 'v4.1.0-zh.2'):
                with self.subTest(tag=tag):
                    result, header = self.configure(root, repo, tag)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn('#define RV_LOCALIZED_RELEASE_TAG "' + tag + '"', header)
        self.assertNotIn('MSYS', os.environ)

    def test_actual_cmake_still_rejects_a_tag_for_another_commit(self):
        with self.repository() as (root, repo, unused):
            (repo / 'marker').write_text('second fixture commit\n')
            self.git(repo, 'add', '.')
            self.git(repo, 'commit', '-m', 'move private HEAD only')
            result, unused = self.configure(root, repo, 'v4.1.0-zh.1')
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('does not identify the exact checkout', result.stderr)
        self.assertNotIn('MSYS', os.environ)

    def test_git_helpers_preserve_brace_revisions_without_changing_parent_environment(self):
        with self.repository() as (root, repo, head):
            for tag in ('v4.1.0-zh.1', 'v4.1.0-zh.2'):
                revision = 'refs/tags/' + tag + '^{commit}'
                self.assertEqual(watch.git(repo, 'rev-parse', revision), head)
                self.assertEqual(watch.git_bytes(repo, 'rev-parse', revision).stdout.decode().strip(), head)
                self.assertEqual(prepare_release.git(repo, 'rev-parse', revision), head)
                child_input = os.environ.copy()
                self.assertEqual(release_common.run('git', '-C', str(repo), 'rev-parse', revision, env=child_input), head)
                self.assertNotIn('MSYS', child_input)
        self.assertNotIn('MSYS', os.environ)

    def test_native_checkout_stale_crlf_is_reexported_and_feature_deletions_stay_deleted(self):
        native_git = Path(os.environ.get('OPENRV_TEST_NATIVE_GIT', 'C:/Program Files/Git/cmd/git.exe'))
        self.assertTrue(native_git.is_file(), 'Native Git is required for the checkout/MSYS boundary fixture')
        def native(repo, *arguments):
            return subprocess.check_output([str(native_git), '-C', str(repo), *arguments],
                                           text=True, encoding='utf-8', stderr=subprocess.DEVNULL).strip()
        with self.repository() as (root, source, unused):
            self.git(source, 'config', 'core.autocrlf', 'false')
            (source / 'marker').write_bytes(b'first fixture commit\n')
            (source / 'deleted-by-feature.txt').write_bytes(b'official source removed by our feature\n')
            self.git(source, 'add', '.')
            self.git(source, 'commit', '-m', 'official release fixture')
            official = self.git(source, 'rev-parse', 'HEAD')
            self.git(source, 'tag', 'v4.0.2')
            (source / 'deleted-by-feature.txt').unlink()
            (source / 'feature.txt').write_bytes(b'localized feature\n')
            self.git(source, 'add', '.')
            self.git(source, 'commit', '-m', 'maintenance feature fixture')
            tree = self.git(source, 'rev-parse', 'HEAD^{tree}')
            clone = root / 'native-clone'
            subprocess.run([str(native_git), 'clone', '--no-hardlinks', '--config', 'core.autocrlf=true',
                            str(source), str(clone)], check=True, capture_output=True)
            expected = subprocess.check_output([str(native_git), '-C', str(source), 'show', official + ':marker'])
            self.assertIn(b'\r\n', (clone / 'marker').read_bytes())
            # Match an established checkout rather than Git's racy same-timestamp
            # fast path, which can accidentally rewrite the stale file in a tiny fixture.
            os.utime(clone / 'marker', (1600000000, 1600000000))
            native(clone, 'update-index', '--refresh')
            # This is the old production sequence: --force still skips matching stat entries.
            native(clone, 'config', 'core.autocrlf', 'false')
            native(clone, 'read-tree', '--reset', '-u', official)
            native(clone, 'checkout', '--detach', official)
            native(clone, 'read-tree', '--reset', '-u', tree)
            native(clone, 'checkout-index', '--all', '--force')
            self.assertNotEqual((clone / 'marker').read_bytes(), expected)
            self.assertIn('marker', official_workspace.working_source_changes(clone, tree))
            release = watch.Release(456, 'v4.0.2', official, official, '2026-08-20T17:15:15Z')
            with mock.patch.dict(os.environ, {'PATH': str(native_git.parent) + os.pathsep + os.environ['PATH']}):
                official_workspace.set_workspace_tree(clone, release, tree)
            self.assertEqual((clone / 'marker').read_bytes(), expected)
            self.assertFalse((clone / 'deleted-by-feature.txt').exists())
            self.assertEqual(self.git(clone, 'rev-parse', 'HEAD'), official)
            self.assertEqual(self.git(clone, 'write-tree'), tree)
            self.assertEqual(official_workspace.working_source_changes(clone, tree), set())

    def test_native_submodule_checkout_uses_lf_and_msys_still_detects_tracked_edits(self):
        native_git = Path(os.environ.get('OPENRV_TEST_NATIVE_GIT', 'C:/Program Files/Git/cmd/git.exe'))
        self.assertTrue(native_git.is_file())
        def native(repo, *arguments):
            return subprocess.run([str(native_git), '-C', str(repo), *arguments], check=True,
                                  capture_output=True, timeout=60).stdout.decode().strip()
        with self.repository() as (root, parent, unused):
            child = root / 'child'; child.mkdir()
            native(child, 'init', '-b', 'main')
            native(child, 'config', 'core.autocrlf', 'false')
            native(child, 'config', 'user.name', 'Fixture'); native(child, 'config', 'user.email', 'fixture@example.invalid')
            expected = b'actual tracked submodule source\n'
            (child / 'source.cpp').write_bytes(expected)
            native(child, 'add', '.'); native(child, 'commit', '-m', 'child source')
            native(parent, '-c', 'protocol.file.allow=always', 'submodule', 'add', str(child), 'dependency')
            native(parent, 'commit', '-am', 'official submodule fixture')
            clone = root / 'with-submodule'
            subprocess.run([str(native_git), 'clone', '--no-hardlinks', '--config', 'core.autocrlf=false',
                            str(parent), str(clone)], check=True, capture_output=True)
            native(clone, '-c', 'protocol.file.allow=always', '-c', 'core.autocrlf=false',
                   'submodule', 'update', '--init', '--recursive')
            self.assertEqual((clone / 'dependency/source.cpp').read_bytes(), expected)
            tree = self.git(clone, 'rev-parse', 'HEAD^{tree}')
            self.assertEqual(official_workspace.working_source_changes(clone, tree), set())
            (clone / 'dependency/source.cpp').write_bytes(b'arbitrary tracked submodule edit\n')
            self.assertEqual(official_workspace.working_source_changes(clone, tree), {'dependency'})

    def test_actual_cmake_and_metadata_verify_official_release_native_version_difference(self):
        harness = official_fixtures.OfficialWorkspaceTests()
        with harness.fixture() as fixture:
            # Model the real official 4.0.2 tag retaining native CMake 4.0.0.
            upstream = fixture['release']
            self.git(fixture['upstream'], 'tag', 'v4.0.2', upstream.commit_sha)
            fixture['release'] = watch.Release(**{**watch.asdict(upstream), 'tag': 'v4.0.2'})
            plan, plan_hash, output = harness.prepare(fixture)
            repo = harness.materialize(fixture, plan, plan_hash, output)
            tag = 'v4.0.2'
            module = repo / 'src/lib/app/RvCommon/RvLocalizedBuild.cmake'
            with mock.patch.dict(os.environ, {'ORV_BUILD_NUMBER': '17'}):
                result, header = self.configure(fixture['root'], repo, tag, '4.0.0', module)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('#define RV_LOCALIZED_PATCH 2', header)
            self.assertIn('#define RV_LOCALIZED_NATIVE_VERSION "4.0.0"', header)
            self.assertIn('#define RV_LOCALIZED_VERSION "4.0.2+build.17"', header)
            self.assertIn('#define RV_LOCALIZED_BUILD_NUMBER 17ULL', header)
            with mock.patch.dict(os.environ, {'ORV_RELEASE_TAG': tag, 'ORV_BUILD_NUMBER': '17'}):
                identity = release_common.source_metadata(repo)
                release_common.validate_release_tag(repo, tag)
            self.assertEqual((identity['version'], identity['nativeVersion']), ('4.0.2', '4.0.0'))
            self.assertEqual(watch.git(repo, 'rev-parse', 'HEAD'), fixture['release'].commit_sha)
            official_workspace.verify_materialized(repo, expected_build=17)
        self.assertNotIn('MSYS', os.environ)

    def test_actual_msys_cmake_separates_owned_tag_and_private_annotated_upstream_ref(self):
        with lightweight_fixture(include_metadata=True, annotated=True) as fixture:
            repo, plan = materialized_fixture(fixture)
            self.assertEqual(plan['schema'], 4)
            self.assertEqual(self.git(repo, 'rev-parse', 'refs/tags/v4.0.3'), fixture['snapshot'])
            self.assertNotEqual(fixture['snapshot'], self.git(repo, 'rev-parse', 'HEAD'))
            module = repo / 'src/lib/app/RvCommon/RvLocalizedBuild.cmake'
            with mock.patch.dict(os.environ, {'ORV_BUILD_NUMBER': '29'}):
                result, header = self.configure(fixture['root'], repo, 'v4.0.3', '4.0.0', module)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('#define RV_LOCALIZED_VERSION "4.0.3+build.29"', header)
            with mock.patch.dict(os.environ, {'ORV_RELEASE_TAG': 'v4.0.3', 'ORV_BUILD_NUMBER': '29'}):
                release_common.validate_release_tag(repo, 'v4.0.3')
            self.git(repo, 'update-ref', 'refs/openrv-upstream/tags/v4.0.3', fixture['snapshot'])
            with mock.patch.dict(os.environ, {'ORV_BUILD_NUMBER': '29'}):
                result, unused = self.configure(fixture['root'], repo, 'v4.0.3', '4.0.0', module)
            self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('MSYS', os.environ)


if __name__ == '__main__':
    unittest.main()
