"""Real local Git compositions from patches and owned files, without shared ancestry."""
import copy
import json
import os
from pathlib import Path
import unittest
from unittest import mock

from lightweight_fixture import lightweight_fixture, prepare_fixture
import official_workspace as workspace


class LightweightWorkspaceTests(unittest.TestCase):
    def commit_config(self, fixture, configuration):
        fixture['write'](fixture['trusted'], workspace.watcher().LOCALIZATION_BASE_FILE,
                         json.dumps(configuration, indent=2) + '\n')
        fixture['git'](fixture['trusted'], 'add', '.')
        fixture['git'](fixture['trusted'], 'commit', '-m', 'explicit fixture bundle revision')
        fixture['snapshot'] = fixture['git'](fixture['trusted'], 'rev-parse', 'HEAD')

    def test_unrelated_owned_history_composes_exact_source_with_private_official_ref(self):
        with lightweight_fixture(annotated=True) as f:
            plan, digest, output = prepare_fixture(f)
            source = f['root'] / 'source'
            self.assertEqual((plan['schema'], plan['kind']), (4, 'official-tag-patch-workspace'))
            self.assertEqual(plan['publication_commit'], f['snapshot'])
            self.assertEqual(f['git'](source, 'rev-parse', 'HEAD'), f['release'].commit_sha)
            self.assertEqual(f['git'](source, 'rev-parse', 'refs/tags/v4.0.3'), f['snapshot'])
            self.assertEqual(f['git'](source, 'rev-parse', 'refs/openrv-upstream/tags/v4.0.3'), f['release'].object_sha)
            self.assertEqual((source / 'caption.txt').read_text(), 'Localized caption\n')
            self.assertEqual((source / 'app.cpp').read_text(), '// stable upstream behavior\n')
            self.assertFalse((source / 'dev-only.cpp').exists())
            self.assertFalse((f['trusted'] / 'CMakeLists.txt').exists())
            self.assertEqual(f['git'](f['trusted'], 'rev-parse', 'HEAD'), f['snapshot'])
            self.assertEqual(workspace.watcher().git_bytes(source, 'merge-base', '--is-ancestor',
                             f['patch_base'], f['snapshot'], check=False).returncode, 1)
            workspace.verify_materialized(source, plan, b'', expected_snapshot=f['snapshot'], expected_build=29)
            self.assertEqual(workspace.read_input(output, digest)[0], plan)

    def test_owned_binary_and_executable_mode_come_from_committed_blobs(self):
        with lightweight_fixture() as f:
            plan, _, _ = prepare_fixture(f)
            source = f['root'] / 'source'
            self.assertEqual((source / 'resources/binary.dat').read_bytes(), b'\0\xff\x80\nowned\0')
            entry = f['git'](source, 'ls-tree', plan['patched_tree'], '--', 'tools/run-fixture.sh')
            self.assertTrue(entry.startswith('100755 blob '), entry)
            self.assertEqual(f['git'](source, 'rev-parse', plan['patched_tree'] + ':.github/workflows'), plan['workflow_tree'])

    def test_dirty_owned_worktree_is_never_used_as_composition_input(self):
        with lightweight_fixture() as f:
            f['write'](f['trusted'], 'overlay/feature.cpp', '// uncommitted wrong input\n')
            prepare_fixture(f)
            self.assertEqual((f['root'] / 'source/feature.cpp').read_text(), '// our owned feature\n')

    def test_committed_input_digest_change_fails_without_manifest_refresh(self):
        for path in ('patches/core.patch', 'overlay/feature.cpp'):
            with self.subTest(path=path), lightweight_fixture() as f:
                f['write'](f['trusted'], path, 'Unreviewed replacement\n')
                f['git'](f['trusted'], 'add', path)
                f['git'](f['trusted'], 'commit', '-m', 'mismatched declared bytes')
                f['snapshot'] = f['git'](f['trusted'], 'rev-parse', 'HEAD')
                with self.assertRaisesRegex(ValueError, 'digest'):
                    prepare_fixture(f)

    def test_new_owned_file_collision_and_unsafe_maps_fail_closed(self):
        with lightweight_fixture() as f:
            config = copy.deepcopy(f['config'])
            config['owned_files'][1]['destination'] = 'caption.txt'
            self.commit_config(f, config)
            with self.assertRaisesRegex(ValueError, 'collides'):
                prepare_fixture(f)
        for change in ({'destination': '../escape'}, {'destination': '.git/config'},
                       {'source': 'ci/upstream-release-watch/localization-base.json'},
                       {'mode': '120000'}, {'replace': 1}):
            with self.subTest(change=change), lightweight_fixture() as f:
                config = copy.deepcopy(f['config'])
                config['owned_files'][1].update(change)
                self.commit_config(f, config)
                with self.assertRaises(ValueError):
                    prepare_fixture(f)

    def test_optional_component_is_wholly_omitted_or_wholly_applied(self):
        for state in ('absent', 'present', 'partial'):
            with self.subTest(state=state), lightweight_fixture(optional=state) as f:
                if state == 'partial':
                    with self.assertRaisesRegex(RuntimeError, 'conflicts'):
                        prepare_fixture(f)
                    continue
                plan, _, _ = prepare_fixture(f)
                component = f['root'] / 'source/src/plugins/rv-packages/annotate_beta'
                if state == 'absent':
                    self.assertEqual(plan['omitted_upstream_components'], ['src/plugins/rv-packages/annotate_beta'])
                    self.assertFalse(component.exists())
                else:
                    self.assertEqual(plan['omitted_upstream_components'], [])
                    self.assertEqual((component / 'caption.py').read_text(), 'translated optional caption\n')
                    self.assertTrue((component / 'marker.py').is_file())

    def test_existing_owned_publication_anchor_survives_new_controller_revision(self):
        with lightweight_fixture() as f:
            anchor = f['snapshot']
            f['write'](f['trusted'], 'README.md', 'New maintenance documentation\n')
            f['git'](f['trusted'], 'add', '.')
            f['git'](f['trusted'], 'commit', '-m', 'new controller revision')
            f['snapshot'] = f['git'](f['trusted'], 'rev-parse', 'HEAD')
            plan, _, _ = prepare_fixture(f, publication_commit=anchor)
            self.assertEqual(plan['publication_commit'], anchor)
            self.assertEqual(plan['localization_snapshot'], f['snapshot'])
            self.assertNotEqual(anchor, f['snapshot'])
            altered = {**plan, 'publication_commit': f['release'].commit_sha}
            with self.assertRaisesRegex(ValueError, 'owned maintenance history'):
                workspace.verify_composition(f['root'] / 'source', altered, b'')
            with self.assertRaisesRegex(ValueError, 'explicit publication commit'):
                workspace.prepare_workspace(f['trusted'], f['root'] / 'missing-anchor', f['root'] / 'other-input',
                                            f['release'], 30, checker=lambda *args: None)

    def test_schema3_cannot_reinterpret_a_lightweight_controller(self):
        with lightweight_fixture() as f:
            plan, _, _ = prepare_fixture(f)
            altered = {**plan, 'schema': 3, 'kind': 'official-tag-workspace'}
            with self.assertRaisesRegex(ValueError, 'schema does not match'):
                workspace.verify_composition(f['root'] / 'source', altered, b'')


class PublicationApiBoundaryTests(unittest.TestCase):
    def test_only_owned_publication_object_and_tag_shapes_can_write(self):
        api = workspace.OfficialApi(write=True)
        tag = {'tag': 'v4.1.0', 'message': '{"kind":"fixture"}', 'object': 'a' * 40, 'type': 'commit'}
        reference = {'ref': 'refs/tags/v4.1.0', 'sha': 'b' * 40}
        response = mock.MagicMock(); response.status = 201; response.read.return_value = b'{}'
        response.__enter__.return_value = response
        with mock.patch.object(workspace, 'urlopen', return_value=response) as send:
            for suffix, payload in (('git/tags', tag), ('git/refs', reference)):
                api.request('POST', workspace.TARGET, suffix, payload)
                request = send.call_args.args[0]
                self.assertEqual(request.method, 'POST')
                self.assertEqual(json.loads(request.data), payload)
                self.assertEqual(request.full_url, f'https://api.github.com/repos/{workspace.TARGET}/{suffix}')
        invalid = [
            ('POST', workspace.UPSTREAM, 'git/tags', tag),
            ('POST', workspace.TARGET, 'git/tags', {**tag, 'type': 'tag'}),
            ('POST', workspace.TARGET, 'git/tags', {**tag, 'tagger': {}}),
            ('POST', workspace.TARGET, 'git/refs', {**reference, 'ref': 'refs/heads/main'}),
            ('POST', workspace.TARGET, 'git/refs', {**reference, 'force': True}),
            ('PATCH', workspace.TARGET, 'git/refs/tags/v4.1.0', reference),
            ('DELETE', workspace.TARGET, 'git/refs/tags/v4.1.0', None)]
        with mock.patch.object(workspace, 'urlopen') as send:
            for method, repository, suffix, payload in invalid:
                with self.subTest(suffix=suffix, payload=payload), self.assertRaises(ValueError):
                    api.request(method, repository, suffix, payload)
            with self.assertRaises(ValueError):
                workspace.OfficialApi().request('POST', workspace.TARGET, 'git/tags', tag)
            send.assert_not_called()


if __name__ == '__main__':
    unittest.main()
