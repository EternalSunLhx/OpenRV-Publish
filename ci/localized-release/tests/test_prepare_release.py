"""No-network safety checks for first-release tag creation and immutable source planning."""
from contextlib import ExitStack
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import prepare_release as release

TAG = 'v4.1.0-zh.1'
CURRENT = '1' * 40
OLDER = '2' * 40
ANNOTATED = 'a' * 40


def ref(commit=CURRENT, *, kind='commit', tag=TAG):
    return {'ref': 'refs/tags/' + tag, 'object': {'type': kind, 'sha': commit}}


class FakeApi:
    def __init__(self, value=None):
        self.ref = value
        self.objects = {}
        self.calls = []
        self.read_status = None
        self.post_status = 201
        self.race_ref = None

    def request(self, method, suffix, payload=None):
        self.calls.append((method, suffix, payload))
        if method == 'GET' and suffix == 'ref/tags/' + TAG:
            if self.read_status:
                return self.read_status, {'message': 'Denied'}
            return (200, self.ref) if self.ref is not None else (404, {'message': 'Not Found'})
        if method == 'GET' and suffix.startswith('tags/'):
            body = self.objects.get(suffix.removeprefix('tags/'))
            return (200, body) if body else (404, {'message': 'Not Found'})
        if method == 'POST' and suffix == 'refs':
            if self.post_status == 201:
                self.ref = ref(payload['sha'])
                return 201, self.ref
            self.ref = self.race_ref
            return self.post_status, {'message': 'Validation failed'}
        raise AssertionError((method, suffix, payload))


class FormatAndApiTests(unittest.TestCase):
    def test_tag_format_rejects_other_refs_and_invalid_versions(self):
        for value in ('refs/tags/' + TAG, 'v4.1.0-zh.0', 'v4.1.0-zh.01', 'v4.1.0-zh.1\n',
                      '../main', 'v4.1.0;echo bad-zh.1', 'v04.1.0-zh.1'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                release.checked_tag(value)
        with self.assertRaises(ValueError):
            release.checked_tag('v4.2.0-zh.1', '4.1.0')

    def test_http_404_is_explicit_and_not_a_network_failure(self):
        for line_ending in ('\n', '\r\n'):
            stdout = line_ending.join(('HTTP/2.0 404 Not Found', 'Content-Type: application/json', '', '{"message":"Not Found"}'))
            self.assertEqual(release.parse_response(stdout, 1)[0], 404)
        with self.assertRaises(RuntimeError):
            release.parse_response('', 1)
        with self.assertRaises(release.ApiError):
            release.parse_response('HTTP/2.0 200 OK\n\n{}', 1)

    def test_get_403_is_not_a_missing_tag(self):
        api = FakeApi(); api.read_status = 403
        with self.assertRaises(release.ApiError):
            release.resolve_tag(api, TAG)
        self.assertFalse(any(call[0] == 'POST' for call in api.calls))

    def test_lightweight_and_annotated_tags_resolve_to_commit(self):
        self.assertEqual(release.resolve_tag(FakeApi(ref()), TAG).commit_sha, CURRENT)
        api = FakeApi(ref(ANNOTATED, kind='tag'))
        api.objects[ANNOTATED] = {'sha': ANNOTATED, 'object': {'type': 'commit', 'sha': OLDER}}
        state = release.resolve_tag(api, TAG)
        self.assertEqual((state.object_sha, state.commit_sha), (ANNOTATED, OLDER))

    def test_tag_cycles_and_non_commit_targets_are_rejected(self):
        api = FakeApi(ref(ANNOTATED, kind='tag'))
        api.objects[ANNOTATED] = {'sha': ANNOTATED, 'object': {'type': 'tag', 'sha': ANNOTATED}}
        with self.assertRaises(ValueError):
            release.resolve_tag(api, TAG)
        with self.assertRaises(ValueError):
            release.resolve_tag(FakeApi(ref(CURRENT, kind='tree')), TAG)

    def test_annotated_tag_depth_is_bounded(self):
        chain = [f'{number:040x}' for number in range(100,109)]
        api = FakeApi(ref(chain[0], kind='tag'))
        for index, sha in enumerate(chain):
            api.objects[sha] = {'sha': sha, 'object': {'type': 'tag', 'sha': chain[index+1]} if index+1 < len(chain)
                                else {'type': 'commit', 'sha': CURRENT}}
        with self.assertRaisesRegex(ValueError, 'depth'):
            release.resolve_tag(api, TAG)

    def test_only_exact_reference_is_accepted(self):
        with self.assertRaises(ValueError):
            release.resolve_tag(FakeApi(ref(tag='v4.1.0-zh.10')), TAG)

    def test_api_cannot_update_delete_force_or_follow_response_urls(self):
        api = release.GitHubApi('owner/repo', read_only=False)
        with mock.patch.object(release.subprocess, 'run') as process:
            for method, suffix, payload in (
                ('PATCH', 'refs/tags/' + TAG, {'sha': CURRENT}),
                ('DELETE', 'refs/tags/' + TAG, None),
                ('POST', 'refs', {'ref': 'refs/tags/' + TAG, 'sha': CURRENT, 'force': True}),
                ('GET', 'https://example.invalid/tag', None)):
                with self.subTest(method=method), self.assertRaises(ValueError):
                    api.request(method, suffix, payload)
            process.assert_not_called()

    def test_read_only_api_cannot_create(self):
        with self.assertRaises(ValueError):
            release.GitHubApi('owner/repo').request('POST', 'refs', {'ref': 'refs/tags/' + TAG, 'sha': CURRENT})

    def test_create_uses_fixed_endpoint_and_json_stdin(self):
        response = 'HTTP/2.0 201 Created\nContent-Type: application/json\n\n' + json.dumps(ref())
        with mock.patch.object(release.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, response, '')) as process:
            api = release.GitHubApi('owner/repo', read_only=False)
            api.request('POST', 'refs', {'ref': 'refs/tags/' + TAG, 'sha': CURRENT})
            arguments = process.call_args.args[0]
            self.assertIn('repos/owner/repo/git/refs', arguments)
            self.assertIn('X-GitHub-Api-Version: ' + release.API_VERSION, arguments)
            self.assertEqual(arguments[-2:], ['--input', '-'])
            self.assertEqual(json.loads(process.call_args.kwargs['input']), {'ref': 'refs/tags/' + TAG, 'sha': CURRENT})


class PlanningAndCreationTests(unittest.TestCase):
    def setUp(self):
        self.environment = mock.patch.dict(os.environ, {'GITHUB_ACTIONS': 'true', 'GITHUB_EVENT_NAME': 'workflow_dispatch',
                                                       'GITHUB_SHA': CURRENT, 'ORV_CREATE_TAG': 'true'})
        self.environment.start(); self.addCleanup(self.environment.stop)
        self.api = FakeApi()
        self.head = CURRENT
        self.git_calls = []
        self.fetched_object = self.fetched_commit = ''
        self.git_patch = mock.patch.object(release, 'git', side_effect=self.git)
        self.git_patch.start(); self.addCleanup(self.git_patch.stop)
        self.validation = mock.patch.object(release, 'validate_checkout')
        self.validate = self.validation.start(); self.addCleanup(self.validation.stop)
        self.version_patch = mock.patch.object(release, 'base_version', return_value='4.1.0')
        self.version_patch.start(); self.addCleanup(self.version_patch.stop)
        self.repo = pathlib.Path('unused-fixture-repo')

    def git(self, repo, *arguments):
        self.git_calls.append(arguments)
        if arguments == ('rev-parse', '--verify', 'HEAD'):
            return self.head
        if arguments == ('rev-parse', '--verify', CURRENT + '^{commit}'):
            return CURRENT
        if arguments == ('rev-parse', '--verify', ANNOTATED + '^{commit}'):
            return CURRENT
        if arguments[:3] == ('fetch', '--no-tags', 'origin'):
            self.assertEqual(arguments[-1], 'refs/tags/' + TAG)
            self.fetched_object = self.api.ref['object']['sha']
            current = self.api.ref['object']
            while current['type'] == 'tag':
                current = self.api.objects[current['sha']]['object']
            self.fetched_commit = current['sha']
            return ''
        if arguments == ('rev-parse', '--verify', 'FETCH_HEAD'):
            return self.fetched_object
        if arguments == ('rev-parse', '--verify', 'FETCH_HEAD^{commit}'):
            return self.fetched_commit
        if arguments[:2] == ('checkout', '--detach'):
            self.head = arguments[-1]
            return ''
        raise AssertionError(arguments)

    def test_new_tag_plan_only_reads_and_lints_trigger_commit(self):
        plan = release.plan_release(self.repo, self.api, TAG, True)
        self.assertEqual(plan['source_sha'], CURRENT)
        self.assertEqual(plan['tag_mode'], 'create')
        self.assertTrue(all(call[0] == 'GET' for call in self.api.calls))
        self.validate.assert_called_once_with(self.repo, CURRENT, TAG, lint=True)

    def test_default_existing_tag_cannot_move_to_new_trigger_commit(self):
        self.api.ref = ref(OLDER)
        with self.assertRaises(ValueError):
            release.plan_release(self.repo, self.api, TAG, True)
        self.assertFalse(any(call[0] == 'POST' for call in self.api.calls))

    def test_explicit_existing_mode_resolves_and_lints_that_exact_commit(self):
        self.api.ref = ref(OLDER)
        plan = release.plan_release(self.repo, self.api, TAG, False)
        self.assertEqual((plan['source_sha'], plan['tag_mode']), (OLDER, 'existing'))
        self.validate.assert_called_once_with(self.repo, OLDER, TAG, lint=True)
        self.assertIn(('checkout', '--detach', OLDER), self.git_calls)

    def test_missing_tag_with_creation_disabled_fails(self):
        with self.assertRaises(ValueError):
            release.plan_release(self.repo, self.api, TAG, False)

    def test_blank_input_is_artifacts_only_without_api_calls(self):
        plan = release.plan_release(self.repo, self.api, '', True)
        self.assertEqual((plan['source_sha'], plan['tag_mode'], plan['release_tag']), (CURRENT, 'branch', ''))
        self.assertEqual(self.api.calls, [])

    def test_tag_push_verifies_instead_of_creating(self):
        os.environ['GITHUB_EVENT_NAME'] = 'push'
        self.api.ref = ref()
        self.assertEqual(release.plan_release(self.repo, self.api, TAG, True)['tag_mode'], 'push')
        self.assertTrue(all(call[0] == 'GET' for call in self.api.calls))

    def test_annotated_tag_push_uses_peeled_trigger_commit(self):
        os.environ['GITHUB_EVENT_NAME'] = 'push'
        os.environ['GITHUB_SHA'] = ANNOTATED
        self.api.ref = ref(ANNOTATED, kind='tag')
        self.api.objects[ANNOTATED] = {'sha': ANNOTATED, 'object': {'type': 'commit', 'sha': CURRENT}}
        plan = release.plan_release(self.repo, self.api, TAG, False)
        self.assertEqual((plan['source_sha'], plan['tag_object_sha'], plan['tag_mode']), (CURRENT, ANNOTATED, 'push'))

    def test_failed_lint_never_creates_a_tag(self):
        self.validate.side_effect = ValueError('Catalog did not pass')
        with self.assertRaises(ValueError):
            release.plan_release(self.repo, self.api, TAG, True)
        self.assertFalse(any(call[0] == 'POST' for call in self.api.calls))

    def test_new_tag_is_created_only_at_immutable_trigger_sha(self):
        result = release.apply_release(self.repo, self.api, TAG, CURRENT, 'create')
        self.assertEqual((result['tag_created'], result['tag_object_sha']), ('true', CURRENT))
        posts = [call for call in self.api.calls if call[0] == 'POST']
        self.assertEqual(posts, [('POST', 'refs', {'ref': 'refs/tags/' + TAG, 'sha': CURRENT})])

    def test_cannot_create_at_a_different_source_commit(self):
        with self.assertRaises(ValueError):
            release.apply_release(self.repo, self.api, TAG, OLDER, 'create')
        self.assertFalse(any(call[0] == 'POST' for call in self.api.calls))

    def test_cannot_create_outside_authorized_dispatch(self):
        os.environ['ORV_CREATE_TAG'] = 'false'
        with self.assertRaises(ValueError):
            release.apply_release(self.repo, self.api, TAG, CURRENT, 'create')
        self.assertFalse(any(call[0] == 'POST' for call in self.api.calls))

    def test_same_existing_tag_is_idempotent_without_post(self):
        self.api.ref = ref()
        result = release.apply_release(self.repo, self.api, TAG, CURRENT, 'create', CURRENT)
        self.assertEqual(result['tag_created'], 'false')
        self.assertTrue(all(call[0] == 'GET' for call in self.api.calls))

    def test_422_race_is_accepted_only_when_same_ref_commit_now_exists(self):
        self.api.post_status = 422; self.api.race_ref = ref()
        self.assertEqual(release.apply_release(self.repo, self.api, TAG, CURRENT, 'create')['tag_created'], 'false')
        self.api = FakeApi(); self.api.post_status = 422
        with self.assertRaises(ValueError):
            release.apply_release(self.repo, self.api, TAG, CURRENT, 'create')
        self.api = FakeApi(); self.api.post_status = 422; self.api.race_ref = ref(OLDER)
        with self.assertRaises(ValueError):
            release.apply_release(self.repo, self.api, TAG, CURRENT, 'create')

    def test_preexisting_tag_deleted_after_plan_is_not_recreated(self):
        with self.assertRaises(ValueError):
            release.apply_release(self.repo, self.api, TAG, CURRENT, 'create', CURRENT)
        self.assertFalse(any(call[0] == 'POST' for call in self.api.calls))

    def test_external_tag_move_is_rejected_before_release(self):
        self.api.ref = ref(OLDER)
        with self.assertRaises(ValueError):
            release.verify_remote(self.api, TAG, CURRENT, CURRENT)
        self.assertTrue(all(call[0] == 'GET' for call in self.api.calls))

    def test_same_commit_but_replaced_annotated_object_is_rejected(self):
        self.api.ref = ref(ANNOTATED, kind='tag')
        self.api.objects[ANNOTATED] = {'sha': ANNOTATED, 'object': {'type': 'commit', 'sha': CURRENT}}
        with self.assertRaises(ValueError):
            release.verify_remote(self.api, TAG, CURRENT, CURRENT)

    def test_branch_apply_makes_no_api_request(self):
        self.assertEqual(release.apply_release(self.repo, self.api, '', CURRENT, 'branch')['release_tag'], '')
        self.assertEqual(self.api.calls, [])


class CheckoutLintTests(unittest.TestCase):
    def test_checker_runs_read_only_without_github_mutation_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = pathlib.Path(directory)
            (repo / 'CMakeLists.txt').write_text('SET(RV_MAJOR_VERSION "4")\nSET(RV_MINOR_VERSION "1")\nSET(RV_REVISION_NUMBER "0")\n')
            checker = repo / 'tools/localization/check_translations.py'
            checker.parent.mkdir(parents=True); checker.write_text('# fixture')
            with mock.patch.object(release, 'git', return_value=CURRENT), mock.patch.object(release.subprocess, 'run') as process:
                with mock.patch.dict(os.environ, {'GH_TOKEN': 'never-forward', 'GITHUB_TOKEN': 'never-forward'}):
                    release.validate_checkout(repo, CURRENT, TAG, lint=True)
                self.assertEqual(process.call_args.args[0], [sys.executable, str(checker)])
                environment = process.call_args.kwargs['env']
                self.assertNotIn('GH_TOKEN', environment)
                self.assertNotIn('GITHUB_TOKEN', environment)
                self.assertEqual(environment['PYTHONDONTWRITEBYTECODE'], '1')
                process.reset_mock()
                with self.assertRaises(ValueError):
                    release.validate_checkout(repo, CURRENT, 'v4.2.0-zh.1', lint=True)
                process.assert_not_called()


class StandaloneBuildNumberTests(unittest.TestCase):
    def environment(self, *, run='1', product=None):
        import official_workspace as workspace
        values = {'GITHUB_ACTIONS': 'true', 'GITHUB_REPOSITORY': workspace.TARGET,
                  'GITHUB_EVENT_NAME': 'workflow_dispatch', 'GITHUB_REF': 'refs/heads/main',
                  'GITHUB_SHA': CURRENT, 'GITHUB_RUN_NUMBER': run,
                  'GITHUB_WORKSPACE': str(pathlib.Path.cwd())}
        if product is not None:
            values['ORV_BUILD_NUMBER'] = product
        return values

    def arguments(self, phase):
        return ['prepare_release.py', phase, '--repo', str(pathlib.Path.cwd()),
                '--workspace', str(pathlib.Path.cwd() / 'unused-preflight-fixture'),
                '--input', str(pathlib.Path.cwd() / 'unused-input-fixture'),
                '--plan-sha256', 'b' * 64, '--assets', str(pathlib.Path.cwd())]

    def test_first_new_build_metadata_is_newer_than_published_build_28(self):
        import official_workspace as workspace
        number = release.product_build_number('1')
        self.assertEqual(number, 29)
        plan = {'official': {'tag': 'v4.0.2'}, 'native_version': '4.0.0',
                **{key: CURRENT for key in ('official_tree', 'patched_tree', 'composed_tree',
                   'localization_snapshot', 'localization_base')},
                'feature_patch_sha256': 'b' * 64, 'generated_patch_sha256': 'c' * 64,
                'omitted_upstream_components': []}
        with mock.patch.dict(os.environ, self.environment(product=str(number)), clear=True), \
                mock.patch.object(workspace, 'verify_materialized', return_value=plan) as verify:
            identity = release.release_identity(pathlib.Path.cwd(), 'v4.0.2', '4.0.0')
        self.assertEqual(identity['displayVersion'], '4.0.2+build.29')
        self.assertGreater(identity['buildNumber'], 28)
        verify.assert_called_once_with(pathlib.Path.cwd(), expected_build=29)

    def test_arbitrary_raw_runs_and_unsigned_64_bit_boundary(self):
        import official_workspace as workspace
        maximum = 18446744073709551615
        for run, product in (('2', 30), ('29', 57), ('9007199254740993', 9007199254741021),
                             (str(maximum - 28), maximum)):
            with self.subTest(run=run):
                self.assertEqual(release.product_build_number(run), product)
        for run in (maximum - 27, maximum, maximum + 1):
            with self.subTest(overflow=run), self.assertRaises(ValueError):
                release.product_build_number(str(run))
        # Historical plans retain their existing generic product-number meaning.
        self.assertEqual(workspace.build_number(28), 28)
        self.assertEqual(workspace.build_number(17), 17)

    def test_invalid_run_numbers_are_not_offset_into_valid_builds(self):
        for run in ('', '0', '01', '-1', '1.0', '1\n', 'short', None, True):
            with self.subTest(run=run), self.assertRaises(ValueError):
                release.product_build_number(run)

    def test_preflight_emits_one_product_number_to_plan_and_job_output(self):
        import official_workspace as workspace
        official = SimpleNamespace(commit_sha=OLDER, tag='v4.0.2')
        with mock.patch.dict(os.environ, self.environment(), clear=True), \
                mock.patch.object(sys, 'argv', self.arguments('preflight')), \
                mock.patch.object(release, 'git', side_effect=lambda repo, *args: CURRENT if args[0] == 'rev-parse' else ''), \
                mock.patch.object(workspace, 'official_release', return_value=official), \
                mock.patch.object(workspace.OfficialApi, 'request', return_value=(404, {})) as read_target, \
                mock.patch('publication_tags.resolve_publication_commit', return_value=CURRENT), \
                mock.patch.object(workspace, 'prepare_workspace', return_value=({'build_number': 29}, 'b' * 64)) as prepare, \
                mock.patch.object(release, 'output_values') as output:
            release.main()
        self.assertEqual(prepare.call_args.args[-1], 29)
        read_target.assert_called_once_with('GET', workspace.TARGET, 'releases/tags/v4.0.2')
        self.assertEqual(prepare.call_args.kwargs['publication_commit'], CURRENT)
        self.assertEqual(output.call_args.args[0]['build_number'], '29')

    def test_every_consumer_verifies_preflight_product_number(self):
        import official_workspace as workspace
        plan = {'build_number': 29, 'official': {'tag': 'v4.0.2'}, 'publication_commit': CURRENT}
        official = SimpleNamespace(tag='v4.0.2', object_sha=OLDER, commit_sha=OLDER)
        values = {'read_input': (plan, b'', b'{}'), 'verify_composition': None,
                  'materialize': None, 'verify_materialized': None,
                  'official_release': official, 'release_from_plan': official}
        for phase in ('materialize', 'sync-tag', 'verify', 'verify-remote', 'publish'):
            with self.subTest(phase=phase), ExitStack() as stack:
                stack.enter_context(mock.patch.dict(os.environ, self.environment(product='29'), clear=True))
                stack.enter_context(mock.patch.object(sys, 'argv', self.arguments(phase)))
                stack.enter_context(mock.patch.object(release, 'git', return_value=CURRENT))
                stack.enter_context(mock.patch.object(release, 'publish_assets', return_value={'status': 'published'}))
                stack.enter_context(mock.patch('assemble_release.assemble'))
                stack.enter_context(mock.patch('publication_tags.sync_publication_tag', return_value=False))
                stack.enter_context(mock.patch('publication_tags.verify_publication_tag'))
                stack.enter_context(mock.patch('builtins.print'))
                calls = {name: stack.enter_context(mock.patch.object(workspace, name, return_value=value))
                         for name, value in values.items()}
                release.main()
                calls['verify_composition'].assert_called_once_with(
                    pathlib.Path.cwd(), plan, b'', expected_snapshot=CURRENT, expected_build=29)
                if phase == 'materialize':
                    self.assertEqual(calls['materialize'].call_args.args[-1], 29)
                if phase in ('verify', 'publish'):
                    self.assertEqual(calls['verify_materialized'].call_args.kwargs['expected_build'], 29)

    def test_wrong_or_missing_consumer_environment_fails_before_reading_plan(self):
        import official_workspace as workspace
        phases = ('preflight', 'materialize', 'sync-tag', 'verify', 'verify-remote', 'publish')
        for phase in phases:
            products = ('1', '28', '30', '', '0') + (() if phase == 'preflight' else (None,))
            for product in products:
                with self.subTest(phase=phase, product=product), \
                        mock.patch.dict(os.environ, self.environment(product=product), clear=True), \
                        mock.patch.object(sys, 'argv', self.arguments(phase)), \
                        mock.patch.object(workspace, 'read_input') as read, \
                        mock.patch.object(workspace, 'official_release') as official, self.assertRaises(ValueError):
                    release.main()
                read.assert_not_called()
                official.assert_not_called()

    def test_old_plan_number_cannot_authorize_first_new_repository_run(self):
        import official_workspace as workspace
        plan = {'build_number': 28, 'localization_snapshot': CURRENT}
        with mock.patch.object(workspace, 'validate_plan'), \
                mock.patch.object(workspace, 'release_from_plan', return_value=SimpleNamespace()), \
                self.assertRaisesRegex(ValueError, 'Plan build number'):
            workspace.verify_composition(pathlib.Path.cwd(), plan, b'', expected_snapshot=CURRENT,
                                         expected_build=release.product_build_number('1'))

    def test_workflow_keeps_raw_run_title_and_propagates_product_output(self):
        workflow = (pathlib.Path(__file__).resolve().parents[3] / '.github/workflows/localized-release.yml').read_text(encoding='utf-8')
        self.assertIn("run-name: OpenRV ${{ inputs.official_tag }} build ${{ github.run_number }}", workflow)
        self.assertNotIn('ORV_BUILD_NUMBER: ${{ github.run_number }}', workflow)
        preflight = workflow.split('\n  preflight:', 1)[1].split('\n  prepare:', 1)[0]
        self.assertIn('build_number: ${{ steps.plan.outputs.build_number }}', preflight)
        for job in ('prepare', 'windows', 'macos', 'draft'):
            block = re.split(r'\n  (?=\S)', workflow.split('\n  ' + job + ':', 1)[1], maxsplit=1)[0]
            # Job children are indented four spaces, so the next two-space job ends this block.
            self.assertIn('ORV_BUILD_NUMBER: ${{ needs.preflight.outputs.build_number }}', block)


if __name__ == '__main__':
    unittest.main()
