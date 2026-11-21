"""Offline watcher tests: no GitHub writes, no dispatch and no source build."""
import base64
from contextlib import contextmanager
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import subprocess
import unittest
from unittest import mock

SPEC = importlib.util.spec_from_file_location('openrv_watch', Path(__file__).resolve().parents[1] / 'watch.py')
watch = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = watch
SPEC.loader.exec_module(watch)

BASE = '1' * 40
HEAD = '2' * 40
OBJECT = 'a' * 40
BLOB = 'b' * 40
BRANCH = 'automation/upstream-release-456'
RELEASE = watch.Release(456, 'v4.1.0', OBJECT, HEAD, '2026-11-18T00:00:00Z')
BASELINE = dict(watch.asdict(watch.Release(123, 'v4.0.2', BASE, BASE, '2026-08-20T17:15:15Z')), schema=1)


class FakeApi:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, *arguments):
        self.calls.append(arguments)
        if not self.responses:
            raise AssertionError('Unexpected API operation: ' + repr(arguments))
        return self.responses.pop(0)


def workflow():
    return {'id': 17, 'state': 'active', 'path': '.github/workflows/localized-release.yml'}


def run(run_id=99, sha=HEAD):
    return {'id': run_id, 'head_sha': sha, 'head_branch': BRANCH, 'event': 'workflow_dispatch',
            'workflow_id': 17, 'path': '.github/workflows/localized-release.yml',
            'html_url': f'https://github.com/{watch.TARGET}/actions/runs/{run_id}'}


class ReleaseTests(unittest.TestCase):
    def test_versions_and_strict_tags(self):
        self.assertEqual(watch.version('v4.1.0'), (4, 1, 0))
        for value in ('4.1.0', 'v04.1.0', 'v4.1.0-rc.1', 'v4.1.0; echo bad', 'v4.1.0\n'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                watch.version(value)

    def test_baseline_does_not_build_old_official_release(self):
        baseline_release = watch.Release(123, 'v4.0.2', BASE, BASE, BASELINE['published_at'])
        state = {'baseline': BASELINE, 'handled': {}, 'pending': {}}
        self.assertFalse(watch.needs_build(state, baseline_release))
        self.assertTrue(watch.needs_build(state, RELEASE))

    def test_known_id_changed_or_same_version_republished_is_rejected(self):
        state = {'baseline': BASELINE, 'handled': {}, 'pending': {}}
        for release in (watch.Release(123, 'v4.0.2', HEAD, HEAD, BASELINE['published_at']),
                        watch.Release(124, 'v4.0.2', BASE, BASE, BASELINE['published_at'])):
            with self.subTest(release=release), self.assertRaises(ValueError):
                watch.needs_build(state, release)

    def test_pending_does_not_count_as_handled(self):
        state = {'baseline': BASELINE, 'handled': {}, 'pending': {'456': watch.asdict(RELEASE)}}
        self.assertTrue(watch.needs_build(state, RELEASE))
        state['pending']['456']['commit_sha'] = BASE
        with self.assertRaises(ValueError):
            watch.needs_build(state, RELEASE)

    def test_handled_is_idempotent(self):
        state = {'baseline': BASELINE, 'handled': {'456': watch.asdict(RELEASE)}, 'pending': {}}
        self.assertFalse(watch.needs_build(state, RELEASE))

    def test_edition_allocation_ignores_peels_and_other_versions(self):
        tags = f'{BASE}\trefs/tags/v4.1.0-zh.1\n{HEAD}\trefs/tags/v4.1.0-zh.1^{{}}\n{BASE}\trefs/tags/v4.0.2-zh.99'
        self.assertEqual(watch.next_edition(tags, 'v4.1.0'), 'v4.1.0-zh.2')

    def test_snapshot_peels_annotated_exact_tag(self):
        api = FakeApi([(200, {'id': 456, 'tag_name': RELEASE.tag, 'draft': False, 'prerelease': False, 'published_at': RELEASE.published_at}),
                       (200, {'ref': 'refs/tags/' + RELEASE.tag, 'object': {'type': 'tag', 'sha': OBJECT}}),
                       (200, {'sha': OBJECT, 'object': {'type': 'commit', 'sha': HEAD}})])
        self.assertEqual(watch.snapshot(api), RELEASE)
        self.assertEqual(api.calls[-1], ('GET', watch.UPSTREAM, 'git/tags/' + OBJECT))

    def test_snapshot_rejects_prerelease_and_wrong_ref(self):
        latest = {'id': 456, 'tag_name': RELEASE.tag, 'draft': False, 'prerelease': True, 'published_at': RELEASE.published_at}
        with self.assertRaises(ValueError):
            watch.snapshot(FakeApi([(200, latest)]))
        latest['prerelease'] = False
        with self.assertRaises(ValueError):
            watch.snapshot(FakeApi([(200, latest), (200, {'ref': 'refs/tags/v4.1.1'})]))


class StateTests(unittest.TestCase):
    def test_no_state_is_baselined_not_handled(self):
        state, blob = watch.load_state(FakeApi([(404, {}), (404, {})]), BASELINE)
        self.assertEqual(blob, '')
        self.assertEqual(state['baseline'], BASELINE)
        self.assertEqual((state['handled'], state['pending']), ({}, {}))

    def test_partial_first_state_branch_is_recoverable_only_at_same_base(self):
        api = FakeApi([(404, {}), (200, {'ref': 'refs/heads/' + watch.STATE_BRANCH,
                                      'object': {'type': 'commit', 'sha': BASE}})])
        state, blob = watch.load_state(api, BASELINE)
        api.responses = [(201, {'content': {'sha': BLOB}})]
        self.assertEqual(watch.save_state(api, state, blob, BASE), BLOB)
        self.assertEqual(api.calls[-1][0], 'PUT')
        state['_empty_branch_sha'] = BASE
        with self.assertRaises(ValueError):
            watch.save_state(FakeApi([]), state, '', HEAD)

    def test_state_updates_use_blob_sha_and_fixed_branch(self):
        state = {'schema': 1, 'baseline': BASELINE, 'handled': {}, 'pending': {}}
        api = FakeApi([(200, {'content': {'sha': HEAD}})])
        self.assertEqual(watch.save_state(api, state, BLOB, BASE), HEAD)
        method, repository, suffix, payload = api.calls[0]
        self.assertEqual((method, repository, suffix), ('PUT', watch.TARGET, 'contents/' + watch.STATE_FILE))
        self.assertEqual((payload['sha'], payload['branch']), (BLOB, watch.STATE_BRANCH))
        self.assertEqual(json.loads(base64.b64decode(payload['content'])), state)

    def test_state_compare_and_swap_failure_is_not_ignored(self):
        with self.assertRaises(RuntimeError):
            watch.save_state(FakeApi([(409, {'message': 'sha differs'})]), {'handled': {}}, BLOB, BASE)


class DispatchTests(unittest.TestCase):
    def test_dispatch_requires_confirmed_matching_run(self):
        api = FakeApi([(200, workflow()), (200, {'workflow_runs': []}),
                       (200, {'workflow_run_id': 99}), (200, run())])
        self.assertEqual(watch.dispatch(api, BRANCH, HEAD, 'v4.1.0-zh.2')[0], 99)
        posts = [call for call in api.calls if call[0] == 'POST']
        self.assertEqual(posts[0][3]['inputs'], {'release_tag': 'v4.1.0-zh.2', 'create_tag': True, 'require_gui_smoke': True})

    def test_run_from_previous_dispatch_is_recovered_without_reposting(self):
        api = FakeApi([(200, workflow()), (200, {'workflow_runs': [run()]}), (200, run())])
        watch.dispatch(api, BRANCH, HEAD, 'v4.1.0-zh.2')
        self.assertTrue(all(call[0] == 'GET' for call in api.calls))

    def test_204_or_wrong_head_cannot_be_marked_confirmed(self):
        for response in ((204, None), (403, {'message': 'Denied'})):
            with self.subTest(response=response), self.assertRaises(RuntimeError):
                watch.dispatch(FakeApi([(200, workflow()), (200, {'workflow_runs': []}), response]), BRANCH, HEAD, 'v4.1.0-zh.2')
        with self.assertRaises(ValueError):
            watch.checked_run(FakeApi([(200, run(sha=BASE))]), 99, BRANCH, HEAD, 17)

    def test_failed_previous_build_is_not_automatically_retried(self):
        previous = dict(run(), status='completed', conclusion='failure')
        api = FakeApi([(200, workflow()), (200, {'workflow_runs': [previous]}), (200, previous)])
        self.assertEqual(watch.dispatch(api, BRANCH, HEAD, 'v4.1.0-zh.2')[0], 99)
        self.assertFalse(any(call[0] == 'POST' for call in api.calls))

    def test_api_has_no_arbitrary_write_or_force_endpoint(self):
        for method, repository, suffix, payload in (
                ('PATCH', watch.TARGET, 'git/refs/heads/main', {'sha': HEAD, 'force': True}),
                ('DELETE', watch.TARGET, 'git/refs/tags/v4.1.0', None),
                ('POST', watch.UPSTREAM, 'git/refs', {'ref': 'refs/heads/main', 'sha': HEAD}),
                ('POST', watch.TARGET, 'git/refs', {'ref': 'refs/heads/main', 'sha': HEAD}),
                ('PUT', watch.TARGET, 'contents/' + watch.STATE_FILE, {'branch': 'main'}),
                ('POST', watch.TARGET, 'actions/workflows/localized-release.yml/dispatches',
                 {'ref': 'main', 'inputs': {'release_tag': 'v4.1.0-zh.2', 'create_tag': True, 'require_gui_smoke': True}})):
            with self.subTest(method=method, suffix=suffix), self.assertRaises(ValueError), mock.patch.object(watch, 'urlopen') as request:
                watch.Api().request(method, repository, suffix, payload)
            request.assert_not_called()


class LocalSafetyTests(unittest.TestCase):
    def test_branch_move_after_checks_is_rejected(self):
        with mock.patch.object(watch, 'git', return_value=BASE + '\trefs/heads/' + BRANCH):
            with self.assertRaises(ValueError):
                watch.verify_branch_head(Path('fixture'), BRANCH, HEAD)

    def test_candidate_tools_cannot_inherit_write_credentials(self):
        with mock.patch.dict(os.environ, {'GH_TOKEN': 'secret', 'GITHUB_TOKEN': 'secret',
                                        'GIT_CONFIG_COUNT': '1', 'GIT_CONFIG_VALUE_0': 'secret'}):
            environment = watch.clean_environment()
        self.assertFalse(any(key in environment for key in ('GH_TOKEN', 'GITHUB_TOKEN', 'GIT_CONFIG_COUNT', 'GIT_CONFIG_VALUE_0')))

    def test_static_checker_uses_trusted_checkout_and_candidate_as_data(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); trusted = root / 'trusted'; candidate = root / 'candidate'
            tools = trusted / 'tools/localization'; tools.mkdir(parents=True)
            for script in ('update_menu_paths.py', 'sync_sources.py', 'check_translations.py'):
                (tools / script).write_text('# trusted fixture')
            lupdate = root / 'lupdate'; lupdate.write_text('# fixture')
            with mock.patch.object(watch.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)) as process, mock.patch.object(watch, 'git'), mock.patch.dict(os.environ, {'QT_LUPDATE_PATH': str(lupdate)}):
                watch.static_checks(trusted, candidate)
            self.assertEqual(process.call_args_list[0].args[0][1], str(tools / 'update_menu_paths.py'))
            self.assertIn(str(candidate), process.call_args_list[0].args[0])
            self.assertEqual(process.call_args_list[1].args[0][1], str(tools / 'sync_sources.py'))
            self.assertIn('--apply', process.call_args_list[1].args[0])

    def test_failed_source_sync_still_generates_catalog_check_report(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); tools = root / 'tools/localization'; tools.mkdir(parents=True)
            for script in ('update_menu_paths.py', 'sync_sources.py', 'check_translations.py'):
                (tools / script).write_text('# trusted fixture')
            lupdate = root / 'lupdate'; lupdate.write_text('# fixture')
            with mock.patch.object(watch.subprocess, 'run', side_effect=[subprocess.CompletedProcess([], 0), subprocess.CompletedProcess([], 1), subprocess.CompletedProcess([], 1)]) as process, mock.patch.object(watch, 'git'), mock.patch.dict(os.environ, {'QT_LUPDATE_PATH': str(lupdate)}):
                with self.assertRaises(ValueError):
                    watch.static_checks(root, root / 'candidate')
            self.assertEqual(process.call_count, 3)
            self.assertIn('--report', process.call_args.args[0])


class GitIntegrationTests(unittest.TestCase):
    """Real tag-tree compositions/pushes; all GitHub URLs map to local fixtures."""
    def git(self, repo, *arguments):
        return subprocess.check_output(['git', '-C', str(repo), *arguments], text=True, encoding='utf-8', stderr=subprocess.DEVNULL).strip()

    @contextmanager
    def fixture(self, conflict=False, native_version=(4, 0, 3), patch_version=False, binary=False,
                optional_component='', configure_optional=True, include_metadata=False):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); working = root / 'working'; working.mkdir()
            self.git(working, 'init', '-b', 'main')
            self.git(working, 'config', 'user.name', 'Fixture')
            self.git(working, 'config', 'user.email', 'fixture@example.invalid')
            self.git(working, 'config', 'core.autocrlf', 'false')
            def cmake(major, minor, revision):
                # Separate fields so a feature changing only the major version
                # applies cleanly and reaches the post-composition version gate.
                gap = '# common version field separator\n' * 10
                return (f'SET(RV_MAJOR_VERSION "{major}")\n' + gap
                        + f'SET(RV_MINOR_VERSION "{minor}")\n' + gap
                        + f'SET(RV_REVISION_NUMBER "{revision}")\n')
            (working / 'CMakeLists.txt').write_text(cmake(4, 0, 2))
            (working / 'app.cpp').write_text('// stable base behavior\n')
            (working / 'caption.txt').write_text('Original caption\n')
            optional = working / 'src/plugins/rv-packages/annotate_beta'
            if optional_component in ('present', 'partial'):
                optional.mkdir(parents=True)
                (optional / 'caption.py').write_text('original optional caption\n')
                (optional / 'marker.py').write_text('# complete upstream component marker\n')
            flows = working / '.github/workflows'; flows.mkdir(parents=True)
            (flows / 'ci.yml').write_text('base\n')
            self.git(working, 'add', '.')
            self.git(working, 'commit', '-m', 'common base')
            common = self.git(working, 'rev-parse', 'HEAD')
            # This is the newer development-main boundary from which our feature
            # diff is taken. Its development-only source must never enter 4.0.3.
            (working / 'CMakeLists.txt').write_text(cmake(4, 1, 0))
            (working / 'app.cpp').write_text('// unreleased development behavior\n')
            (working / 'dev-only.cpp').write_text('// not part of the stable release\n')
            if optional_component == 'absent':
                optional.mkdir(parents=True)
                (optional / 'caption.py').write_text('original optional caption\n')
                (optional / 'marker.py').write_text('# complete upstream component marker\n')
            self.git(working, 'add', '.')
            self.git(working, 'commit', '-m', 'official development main boundary')
            patch_base = self.git(working, 'rev-parse', 'HEAD')
            # Only these subsequent changes are our localization/interface patch.
            (flows / 'ci.yml').write_text('trusted fork pipeline\n')
            (flows / 'localized.yml').write_text('trusted localized controller\n')
            (working / 'language.cpp').write_text('// localized interface implementation\n')
            (working / 'caption.txt').write_text('Localized caption\n')
            control = working / watch.LOCALIZATION_BASE_FILE
            control.parent.mkdir(parents=True)
            configuration = {'schema': 1, 'repository': watch.UPSTREAM, 'source_base_sha': patch_base}
            if optional_component and configure_optional:
                configuration['optional_upstream_components'] = ['src/plugins/rv-packages/annotate_beta']
            control.write_text(json.dumps(configuration) + '\n')
            if optional_component:
                (optional / 'caption.py').write_text('translated optional caption\n')
            updater = working / 'tools/localization/update_menu_paths.py'
            updater.parent.mkdir(parents=True)
            updater.write_text('# only trusted fixture paths are inspected\n')
            if include_metadata:
                implementation = Path(__file__).resolve().parents[3]
                for relative in ('ci/upstream-release-watch/watch.py', 'ci/localized-release/official_release.py',
                                 'src/lib/app/RvCommon/RvLocalizedBuild.cmake',
                                 'src/lib/app/RvCommon/RvCommon/RvLocalizedBuild.h.in'):
                    target_file = working / relative
                    target_file.parent.mkdir(parents=True, exist_ok=True)
                    source_file = implementation / relative
                    if not source_file.is_file():
                        source_file = implementation / 'overlay' / relative
                    target_file.write_bytes(source_file.read_bytes())
            if patch_version:
                (working / 'CMakeLists.txt').write_text(cmake(5, 1, 0))
            if binary:
                (working / 'binary-resource.dat').write_bytes(b'\0\xff\x80\r\nresource\0\x01')
                (working / 'trailing-text.txt').write_bytes(b'caption with trailing spaces   \n\n')
            self.git(working, 'add', '.')
            self.git(working, 'commit', '-m', 'localized main snapshot')
            main_sha = self.git(working, 'rev-parse', 'HEAD')
            target = root / 'target.git'
            subprocess.run(['git', 'clone', '--bare', '--no-hardlinks', str(working), str(target)], check=True)
            self.git(working, 'checkout', '--detach', common)
            (working / 'CMakeLists.txt').write_text(cmake(*native_version))
            (working / 'app.cpp').write_text('// released stable behavior\n')
            (flows / 'ci.yml').write_text('official workflow update\n')
            (flows / 'new.yml').write_text('new upstream automation\n')
            (working / 'stable-only.cpp').write_text('// new official stable source\n')
            if conflict:
                (working / 'caption.txt').write_text('Different upstream caption\n')
            if optional_component == 'partial':
                (optional / 'caption.py').unlink()
            self.git(working, 'add', '.')
            self.git(working, 'commit', '-m', 'official release')
            upstream_sha = self.git(working, 'rev-parse', 'HEAD')
            self.git(working, 'tag', 'v4.0.3')
            upstream = root / 'upstream.git'
            subprocess.run(['git', 'clone', '--bare', '--no-hardlinks', str(working), str(upstream)], check=True)
            release = watch.Release(456, 'v4.0.3', upstream_sha, upstream_sha, RELEASE.published_at)
            trusted = root / 'trusted'
            subprocess.run(['git', 'clone', '--no-hardlinks', str(target), str(trusted)], check=True)
            self.git(trusted, 'config', 'user.name', 'Fixture')
            self.git(trusted, 'config', 'user.email', 'fixture@example.invalid')
            real_run = subprocess.run

            def local_only(command, *arguments, **kwargs):
                rewritten = [str(target) if value == 'https://github.com/' + watch.TARGET + '.git'
                             else str(upstream) if value == 'https://github.com/' + watch.UPSTREAM + '.git'
                             else value for value in command]
                if rewritten[:2] == ['git', 'clone']:
                    rewritten.insert(2, '--no-hardlinks')
                self.assertFalse(any(isinstance(value, str) and value.startswith('https://') for value in rewritten))
                return real_run(rewritten, *arguments, **kwargs)

            with mock.patch.object(watch.subprocess, 'run', side_effect=local_only), mock.patch.dict(os.environ, {'GH_TOKEN': 'local-fixture-only'}):
                yield {'root': root, 'working': working, 'target': target, 'upstream': upstream,
                       'trusted': trusted, 'release': release, 'base': main_sha, 'patch_base': patch_base}

    def prepare(self, fixture, name, check=None):
        with mock.patch.object(watch, 'static_checks', side_effect=check):
            return watch.prepare_candidate(fixture['trusted'], fixture['root'] / name, fixture['release'])

    def test_stable_tree_excludes_newer_main_and_preserves_human_translations(self):
        with self.fixture() as f:
            target, main_sha = f['target'], f['base']
            branch, candidate_sha, tag, base, reason = self.prepare(f, 'candidate1', ValueError('new caption needs translation'))
            self.assertTrue(reason)
            self.assertEqual((branch, tag, base), (BRANCH, 'v4.0.3-zh.1', main_sha))
            self.assertEqual(self.git(target, 'rev-parse', 'refs/heads/main'), main_sha)
            self.assertEqual(self.git(target, 'show', candidate_sha + ':.github/workflows/ci.yml'), 'trusted fork pipeline')
            self.assertEqual(self.git(target, 'show', candidate_sha + ':stable-only.cpp'), '// new official stable source')
            self.assertEqual(self.git(target, 'show', candidate_sha + ':app.cpp'), '// released stable behavior')
            self.assertEqual(self.git(target, 'show', candidate_sha + ':language.cpp'), '// localized interface implementation')
            self.assertFalse(self.git(target, 'ls-tree', '--name-only', candidate_sha, '--', 'dev-only.cpp'))
            self.assertEqual(watch.cmake_version(self.git(target, 'show', candidate_sha + ':CMakeLists.txt')), '4.0.3')
            plan = json.loads(self.git(target, 'show', candidate_sha + ':' + watch.PLAN_FILE))
            composed = plan['composed_commit_sha']
            self.assertEqual(self.git(target, 'rev-list', '--parents', '-n', '1', composed).split(), [composed, main_sha])
            self.assertEqual(self.git(target, 'rev-parse', composed + '^{tree}'), plan['composed_tree_sha'])
            self.assertFalse(self.git(target, 'diff', '--name-only', main_sha, candidate_sha, '--', '.github/workflows'))
            self.assertEqual(plan['upstream_tree_sha'], self.git(f['upstream'], 'rev-parse', f['release'].commit_sha + '^{tree}'))
            self.assertEqual(plan['localization_base_sha'], f['patch_base'])
            # A maintainer's edit lives on the candidate, not in main. Retrying
            # preserves it and never reconstructs/overwrites that branch.
            human = f['root'] / 'candidate1'
            (human / 'human-translation.txt').write_text('Manually completed translation\n')
            self.git(human, 'add', '.')
            self.git(human, 'commit', '-m', 'complete manual translation')
            translated_sha = self.git(human, 'rev-parse', 'HEAD')
            self.git(human, 'push', 'origin', 'HEAD:refs/heads/' + branch)
            repeated = self.prepare(f, 'candidate2')
            self.assertEqual(repeated[1], translated_sha)
            self.assertEqual(repeated[-1], '')
            self.assertEqual(self.git(target, 'show', translated_sha + ':human-translation.txt'), 'Manually completed translation')
            self.assertEqual(self.git(target, 'rev-parse', 'refs/heads/main'), main_sha)

    def test_binary_patch_and_trailing_newlines_are_preserved(self):
        with self.fixture(binary=True) as f:
            result = self.prepare(f, 'candidate')
            self.assertEqual(watch.git_bytes(f['target'], 'show', result[1] + ':binary-resource.dat').stdout,
                             b'\0\xff\x80\r\nresource\0\x01')
            self.assertEqual(watch.git_bytes(f['target'], 'show', result[1] + ':trailing-text.txt').stdout,
                             b'caption with trailing spaces   \n\n')

    def test_nonworkflow_patch_conflict_does_not_push_or_change_main(self):
        with self.fixture(conflict=True) as f:
            with self.assertRaisesRegex(RuntimeError, 'patch conflicts'):
                self.prepare(f, 'candidate')
            self.assertFalse(self.git(f['target'], 'ls-remote', '--heads', str(f['target']), 'refs/heads/' + BRANCH))
            self.assertEqual(self.git(f['target'], 'rev-parse', 'refs/heads/main'), f['base'])

    def test_patch_cannot_change_official_release_version(self):
        with self.fixture(patch_version=True) as f:
            with self.assertRaisesRegex(ValueError, 'changed the official native source version'):
                self.prepare(f, 'candidate')
            self.assertEqual(self.git(f['target'], 'rev-parse', 'refs/heads/main'), f['base'])

    def test_official_tag_can_keep_an_older_native_patch_version(self):
        with self.fixture(native_version=(4, 0, 0)) as f:
            result = self.prepare(f, 'candidate')
            plan = json.loads(self.git(f['target'], 'show', result[1] + ':' + watch.PLAN_FILE))
            self.assertEqual((result[2], plan['upstream_native_version']), ('v4.0.3-zh.1', '4.0.0'))
            self.assertEqual(watch.base_version(f['root'] / 'candidate'), '4.0.0')

    def test_reviewed_optional_component_is_omitted_only_when_wholly_absent(self):
        for component in ('absent', 'present', 'partial'):
            with self.subTest(component=component), self.fixture(optional_component=component) as f:
                if component == 'partial':
                    with self.assertRaisesRegex(RuntimeError, 'patch conflicts'):
                        self.prepare(f, 'candidate')
                    continue
                result = self.prepare(f, 'candidate')
                plan = json.loads(self.git(f['target'], 'show', result[1] + ':' + watch.PLAN_FILE))
                expected = ['src/plugins/rv-packages/annotate_beta'] if component == 'absent' else []
                self.assertEqual(plan['omitted_upstream_components'], expected)
                exists = self.git(f['target'], 'ls-tree', result[1], '--', 'src/plugins/rv-packages/annotate_beta')
                self.assertEqual(bool(exists), component == 'present')
                if exists:
                    self.assertEqual(self.git(f['target'], 'show', result[1] + ':src/plugins/rv-packages/annotate_beta/caption.py'),
                                     'translated optional caption')

    def test_missing_component_without_reviewed_configuration_stays_a_conflict(self):
        with self.fixture(optional_component='absent', configure_optional=False) as f:
            with self.assertRaisesRegex(RuntimeError, 'annotate_beta/caption.py'):
                self.prepare(f, 'candidate')

    def test_retargeted_official_tag_is_rejected(self):
        with self.fixture() as f:
            self.git(f['upstream'], 'update-ref', 'refs/tags/v4.0.3', f['patch_base'])
            with self.assertRaisesRegex(ValueError, 'tag changed'):
                self.prepare(f, 'candidate')

    def test_candidate_provenance_tampering_and_old_schema_are_rejected(self):
        for key, value in (('schema', 1), ('localization_patch_sha256', '0' * 64), ('composed_tree_sha', '0' * 40),
                           ('upstream_native_version', '4.0.0'), ('omitted_upstream_components', ['src/plugins/rv-packages/unreviewed'])):
            with self.subTest(key=key), self.fixture() as f:
                result = self.prepare(f, 'candidate1')
                candidate = f['root'] / 'candidate1'
                path = candidate / watch.PLAN_FILE
                plan = json.loads(path.read_text()); plan[key] = value
                path.write_text(json.dumps(plan) + '\n')
                self.git(candidate, 'add', '.')
                self.git(candidate, 'commit', '-m', 'alter provenance')
                altered = self.git(candidate, 'rev-parse', 'HEAD')
                self.git(candidate, 'push', 'origin', 'HEAD:refs/heads/' + result[0])
                with self.assertRaises(ValueError):
                    self.prepare(f, 'candidate2')
                self.assertEqual(self.git(f['target'], 'rev-parse', 'refs/heads/' + result[0]), altered)

    def test_branch_created_during_checks_is_not_overwritten(self):
        with self.fixture() as f:
            def race(*unused):
                self.git(f['target'], 'update-ref', 'refs/heads/' + BRANCH, f['base'])
            with self.assertRaisesRegex(ValueError, 'moved during preparation'):
                self.prepare(f, 'candidate', race)
            self.assertEqual(self.git(f['target'], 'rev-parse', 'refs/heads/' + BRANCH), f['base'])

    def test_controller_snapshot_is_immutable_when_remote_main_moves(self):
        with self.fixture() as f:
            changed = f['trusted']
            self.git(changed, 'checkout', '--detach', f['base'])
            (changed / 'later-feature.cpp').write_text('// after the workflow trigger\n')
            self.git(changed, 'add', '.')
            self.git(changed, 'commit', '-m', 'later main feature')
            later = self.git(changed, 'rev-parse', 'HEAD')
            self.git(changed, 'push', 'origin', 'HEAD:refs/heads/main')
            self.git(changed, 'checkout', '--detach', f['base'])
            result = self.prepare(f, 'candidate')
            self.assertEqual(result[3], f['base'])
            self.assertFalse(self.git(f['target'], 'ls-tree', '--name-only', result[1], '--', 'later-feature.cpp'))
            self.assertEqual(self.git(f['target'], 'rev-parse', 'refs/heads/main'), later)

    def test_later_main_workflow_change_stops_before_push(self):
        with self.fixture() as f:
            def move_workflows(*unused):
                changed = f['trusted']
                (changed / '.github/workflows/new-controller.yml').write_text('new controller\n')
                self.git(changed, 'add', '.')
                self.git(changed, 'commit', '-m', 'new controller workflow')
                self.git(changed, 'push', 'origin', 'HEAD:refs/heads/main')
            with self.assertRaisesRegex(ValueError, 'workflow definitions changed'):
                self.prepare(f, 'candidate', move_workflows)
            self.assertFalse(self.git(f['target'], 'ls-remote', '--heads', str(f['target']), 'refs/heads/' + BRANCH))


if __name__ == '__main__':
    unittest.main()
