"""Offline official-tag dispatch and recovery contracts. All live HTTP is disabled."""
from __future__ import annotations

import contextlib
import copy
from datetime import timedelta
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
import urllib.error

SCRIPT = Path(__file__).resolve().parents[1] / 'dispatch.py'
spec = importlib.util.spec_from_file_location('openrv_main_dispatch', SCRIPT)
dispatch = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = dispatch
spec.loader.exec_module(dispatch)

SOURCE, COMMIT, TREE, ANNOTATED, OTHER = ('a' * 40, 'b' * 40, 'c' * 40, 'd' * 40, 'e' * 40)
PUBLICATION = 'f' * 40
TAG = 'v4.0.2'
PUBLISHED = '2026-11-18T08:00:00Z'
WORKFLOW_ID = 42
HISTORY = f'actions/workflows/{WORKFLOW_ID}/runs?event=workflow_dispatch&branch=main&per_page=100&page='
MAIN_REF = 'git/ref/heads/main'
TAG_REF = 'git/ref/tags/' + TAG


def reference(name, sha, kind='commit'):
    return {'ref': 'refs/' + name, 'object': {'type': kind, 'sha': sha}}


def run_record(**changes):
    run = {'id': 123, 'run_number': 17, 'event': 'workflow_dispatch', 'head_branch': 'main', 'head_sha': SOURCE,
           'workflow_id': WORKFLOW_ID, 'path': '.github/workflows/localized-release.yml',
           'repository': {'full_name': dispatch.TARGET}, 'display_title': 'OpenRV v4.0.2 build 17',
           'created_at': '2026-11-18T08:01:00Z', 'status': 'completed', 'conclusion': 'success',
           'html_url': 'https://github.com/' + dispatch.TARGET + '/actions/runs/123'}
    run.update(changes)
    return run


class FakeApi:
    def __init__(self, *, annotated=False, mirrored=True, runs=(), tag=TAG):
        self.tag = tag
        obj = ANNOTATED if annotated else COMMIT
        kind = 'tag' if annotated else 'commit'
        self.calls = []
        self.dispatch_status = 204
        self.routes = {
            (dispatch.TARGET, MAIN_REF): reference('heads/main', SOURCE),
            (dispatch.UPSTREAM, 'releases/latest'): {'id': 456, 'tag_name': tag, 'draft': False, 'prerelease': False,
                'html_url': 'https://github.com/' + dispatch.UPSTREAM + '/releases/tag/' + tag, 'published_at': PUBLISHED},
            (dispatch.UPSTREAM, 'git/ref/tags/' + tag): reference('tags/' + tag, obj, kind),
            (dispatch.UPSTREAM, 'git/commits/' + COMMIT): {'sha': COMMIT, 'tree': {'sha': TREE}},
            (dispatch.TARGET, 'git/ref/tags/' + tag): reference('tags/' + tag, PUBLICATION, 'tag') if mirrored else (404, None),
            (dispatch.TARGET, 'git/tags/' + PUBLICATION): {
                'sha': PUBLICATION, 'tag': tag, 'object': {'type': 'commit', 'sha': SOURCE},
                'message': dispatch.publication_tags().publication_tag_message(
                    {'release_id': 456, 'tag': tag, 'object_sha': obj,
                     'commit_sha': COMMIT, 'published_at': PUBLISHED})},
            (dispatch.TARGET, 'actions/workflows/localized-release.yml'):
                {'id': WORKFLOW_ID, 'path': '.github/workflows/localized-release.yml', 'state': 'active'},
            (dispatch.TARGET, HISTORY + '1'): {'workflow_runs': list(runs)},
        }
        if annotated:
            self.routes[(dispatch.UPSTREAM, 'git/tags/' + ANNOTATED)] = {
                'sha': ANNOTATED, 'tag': tag, 'object': {'type': 'commit', 'sha': COMMIT}}

    @property
    def writes(self):
        return [call for call in self.calls if call[0] != 'GET']

    def request(self, method, repository, suffix, data=None):
        self.calls.append((method, repository, suffix, copy.deepcopy(data)))
        if method == 'POST':
            assert (repository, suffix, data) == (dispatch.TARGET, 'actions/workflows/localized-release.yml/dispatches',
                {'ref': 'main', 'inputs': {'official_tag': self.tag}}), 'Unexpected mock mutation'
            return self.dispatch_status, None
        assert method == 'GET' and data is None
        assert (repository, suffix) in self.routes, ('Unexpected mock read', repository, suffix)
        value = self.routes[(repository, suffix)]
        if isinstance(value, list):
            value = value.pop(0) if len(value) > 1 else value[0]
        return copy.deepcopy(value) if isinstance(value, tuple) else (200, copy.deepcopy(value))


class NoNetworkTest(unittest.TestCase):
    def setUp(self):
        blocked = mock.patch.object(dispatch.urllib.request.OpenerDirector, 'open',
            side_effect=AssertionError('Live HTTP is disabled in dispatch tests'))
        blocked.start()
        self.addCleanup(blocked.stop)
        ancestry = mock.patch.object(dispatch, 'source_snapshot_is_valid', return_value=True)
        ancestry.start()
        self.addCleanup(ancestry.stop)

    def rejects_without_write(self, api, exception=(ValueError, RuntimeError)):
        with self.assertRaises(exception):
            dispatch.check_and_dispatch(api, SOURCE)
        self.assertEqual(api.writes, [])


class SnapshotAndDispatchTests(NoNetworkTest):
    def test_stable_first_dispatch_only_posts_main_with_official_tag(self):
        api = FakeApi(mirrored=False)
        result = dispatch.check_and_dispatch(api, SOURCE)
        self.assertTrue(result['needs_build'])
        self.assertTrue(result['dispatched'])
        self.assertFalse(result['tag_mirror_verified'])
        self.assertEqual(result['release']['commit'], COMMIT)
        self.assertEqual(len(api.writes), 1)
        self.assertEqual(api.writes[0], ('POST', dispatch.TARGET, 'actions/workflows/localized-release.yml/dispatches',
            {'ref': 'main', 'inputs': {'official_tag': TAG}}))
        self.assertEqual(sum(call[2] == 'releases/latest' for call in api.calls), 2)
        self.assertEqual(sum(call[1:3] == (dispatch.TARGET, TAG_REF) for call in api.calls), 2)

    def test_probe_is_read_only_and_does_not_request_second_snapshot(self):
        api = FakeApi(mirrored=False)
        result = dispatch.check_and_dispatch(api, SOURCE, probe=True)
        self.assertTrue(result['needs_build'])
        self.assertFalse(result['dispatched'])
        self.assertEqual(api.writes, [])
        self.assertEqual(sum(call[2] == 'releases/latest' for call in api.calls), 1)

    def test_annotated_original_object_is_preserved_and_mirrored(self):
        api = FakeApi(annotated=True)
        result = dispatch.check_and_dispatch(api, SOURCE, probe=True)
        self.assertEqual(result['release']['tag_object'], ANNOTATED)
        self.assertEqual(result['release']['commit'], COMMIT)
        self.assertTrue(result['tag_mirror_verified'])
        api.routes[(dispatch.TARGET, TAG_REF)] = reference('tags/' + TAG, COMMIT)
        self.rejects_without_write(api)

    def test_annotated_cycle_or_changed_object_is_rejected(self):
        for mode in ('cycle', 'changed-sha'):
            api = FakeApi(annotated=True)
            api.routes[(dispatch.UPSTREAM, 'git/tags/' + ANNOTATED)] = {'sha': ANNOTATED if mode == 'cycle' else OTHER,
                'object': {'type': 'tag', 'sha': ANNOTATED}}
            with self.subTest(mode=mode):
                self.rejects_without_write(api)

    def test_annotated_depth_is_bounded(self):
        api = FakeApi(annotated=True)
        identities = [f'{index:040x}' for index in range(1, 10)]
        api.routes[(dispatch.UPSTREAM, TAG_REF)] = reference('tags/' + TAG, identities[0], 'tag')
        for index, identity in enumerate(identities):
            api.routes[(dispatch.UPSTREAM, 'git/tags/' + identity)] = {'sha': identity,
                'object': {'type': 'tag', 'sha': identities[min(index + 1, 8)]}}
        self.rejects_without_write(api)

    def test_unstable_or_noncanonical_latest_release_never_dispatches(self):
        changes = ({'draft': True}, {'prerelease': True}, {'tag_name': 'v4.0.2-zh.1'}, {'tag_name': 'v04.0.2'},
                   {'tag_name': 'v4.0.2\n'}, {'id': True}, {'id': 0}, {'html_url': 'https://github.com/foreign/repo/releases/tag/' + TAG},
                   {'published_at': '2026-11-18Z'}, {'published_at': 'not a timestamp'})
        for change in changes:
            api = FakeApi()
            api.routes[(dispatch.UPSTREAM, 'releases/latest')].update(change)
            with self.subTest(change=change):
                self.rejects_without_write(api)

    def test_tag_ref_commit_and_tree_identities_are_checked(self):
        for mode in ('wrong-ref', 'tree-object', 'commit-sha', 'tree-sha'):
            api = FakeApi()
            if mode == 'wrong-ref':
                api.routes[(dispatch.UPSTREAM, TAG_REF)]['ref'] = 'refs/tags/v4.0.3'
            elif mode == 'tree-object':
                api.routes[(dispatch.UPSTREAM, TAG_REF)]['object']['type'] = 'tree'
            elif mode == 'commit-sha':
                api.routes[(dispatch.UPSTREAM, 'git/commits/' + COMMIT)]['sha'] = OTHER
            else:
                api.routes[(dispatch.UPSTREAM, 'git/commits/' + COMMIT)]['tree']['sha'] = 'not-a-sha'
            with self.subTest(mode=mode):
                self.rejects_without_write(api)

    def test_main_change_before_second_snapshot_is_rejected(self):
        api = FakeApi()
        api.routes[(dispatch.TARGET, MAIN_REF)] = [reference('heads/main', SOURCE), reference('heads/main', OTHER)]
        self.rejects_without_write(api)

    def test_official_tag_retarget_between_snapshots_is_rejected(self):
        api = FakeApi()
        api.routes[(dispatch.UPSTREAM, TAG_REF)] = [reference('tags/' + TAG, COMMIT), reference('tags/' + TAG, OTHER)]
        api.routes[(dispatch.UPSTREAM, 'git/commits/' + OTHER)] = {'sha': OTHER, 'tree': {'sha': TREE}}
        self.rejects_without_write(api)

    def test_latest_release_or_tree_change_between_snapshots_is_rejected(self):
        for mode in ('release-id', 'tree'):
            api = FakeApi()
            if mode == 'release-id':
                first = api.routes[(dispatch.UPSTREAM, 'releases/latest')]
                api.routes[(dispatch.UPSTREAM, 'releases/latest')] = [first, {**first, 'id': first['id'] + 1}]
            else:
                api.routes[(dispatch.UPSTREAM, 'git/commits/' + COMMIT)] = [
                    {'sha': COMMIT, 'tree': {'sha': TREE}}, {'sha': COMMIT, 'tree': {'sha': OTHER}}]
            with self.subTest(mode=mode):
                self.rejects_without_write(api)

    def test_existing_mirror_catches_retarget_across_watch_runs(self):
        api = FakeApi(runs=[run_record()])
        api.routes[(dispatch.UPSTREAM, TAG_REF)] = reference('tags/' + TAG, OTHER)
        api.routes[(dispatch.UPSTREAM, 'git/commits/' + OTHER)] = {'sha': OTHER, 'tree': {'sha': TREE}}
        self.rejects_without_write(api)
        self.assertFalse(any(call[2].startswith('actions/workflows/') for call in api.calls))

    def test_mirror_change_or_disappearance_before_dispatch_is_rejected(self):
        for final in ((404, None), reference('tags/' + TAG, OTHER), reference('tags/' + TAG, COMMIT, 'tag')):
            api = FakeApi()
            api.routes[(dispatch.TARGET, TAG_REF)] = [reference('tags/' + TAG, PUBLICATION, 'tag'), final]
            api.routes[(dispatch.TARGET, 'git/tags/' + COMMIT)] = (404, None)
            with self.subTest(final=final):
                self.rejects_without_write(api)

    def test_mirror_api_error_does_not_mean_missing(self):
        for status in (301, 403, 429, 500):
            api = FakeApi()
            api.routes[(dispatch.TARGET, TAG_REF)] = (status, None)
            with self.subTest(status=status):
                self.rejects_without_write(api)

    def test_dispatch_rejection_is_reported_without_other_writes(self):
        api = FakeApi()
        api.dispatch_status = 422
        with self.assertRaisesRegex(RuntimeError, 'not accepted'):
            dispatch.check_and_dispatch(api, SOURCE)
        self.assertEqual(len(api.writes), 1)


class RunRecoveryTests(NoNetworkTest):
    def test_matching_completed_or_pending_run_is_idempotent(self):
        for status in ('queued', 'in_progress', 'completed'):
            api = FakeApi(runs=[run_record(status=status, conclusion='failure' if status == 'completed' else None)])
            with self.subTest(status=status):
                result = dispatch.check_and_dispatch(api, SOURCE)
                self.assertFalse(result['needs_build'])
                self.assertFalse(result['dispatched'])
                self.assertEqual(result['run_id'], 123)
                self.assertTrue(result['tag_mirror_verified'])
                self.assertEqual(api.writes, [])

    def test_new_main_does_not_rebuild_an_already_dispatched_official_tag(self):
        cases = [(status, None) for status in ('queued', 'in_progress', 'waiting', 'requested', 'pending')]
        cases += [('completed', conclusion) for conclusion in ('success', 'failure', 'cancelled')]
        for status, conclusion in cases:
            api = FakeApi(runs=[run_record(status=status, conclusion=conclusion)])
            api.routes[(dispatch.TARGET, MAIN_REF)] = reference('heads/main', OTHER)
            with self.subTest(status=status, conclusion=conclusion):
                result = dispatch.check_and_dispatch(api, OTHER)
                self.assertFalse(result['needs_build'])
                self.assertFalse(result['dispatched'])
                self.assertEqual(result['run_id'], 123)
                self.assertEqual(result['existing_run_status'], status)
                self.assertEqual(result['existing_run_source_sha'], SOURCE)
                self.assertEqual(api.writes, [])

    def test_new_official_tag_dispatches_despite_previous_tag_run(self):
        api = FakeApi(tag='v4.0.3', mirrored=False, runs=[run_record()])
        result = dispatch.check_and_dispatch(api, SOURCE)
        self.assertTrue(result['needs_build'])
        self.assertTrue(result['dispatched'])
        self.assertEqual(result['release']['tag'], 'v4.0.3')
        self.assertEqual(api.writes, [('POST', dispatch.TARGET,
            'actions/workflows/localized-release.yml/dispatches',
            {'ref': 'main', 'inputs': {'official_tag': 'v4.0.3'}})])
        api.routes[(dispatch.TARGET, HISTORY + '1')] = {'workflow_runs': [
            run_record(display_title='OpenRV v4.0.3 build 17', status='queued', conclusion=None)]}
        recovered = dispatch.check_and_dispatch(api, SOURCE)
        self.assertFalse(recovered['needs_build'])
        self.assertFalse(recovered['dispatched'])
        self.assertEqual(len(api.writes), 1)

    def test_new_main_cannot_bypass_completed_run_mirror_checks(self):
        for mirror in ((404, None), reference('tags/' + TAG, OTHER)):
            api = FakeApi(runs=[run_record(conclusion='success')])
            api.routes[(dispatch.TARGET, MAIN_REF)] = reference('heads/main', OTHER)
            api.routes[(dispatch.TARGET, TAG_REF)] = mirror
            with self.subTest(mirror=mirror), self.assertRaises(ValueError):
                dispatch.check_and_dispatch(api, OTHER)
            self.assertEqual(api.writes, [])

    def test_failed_or_cancelled_lightweight_run_without_tag_waits_for_manual_retry(self):
        for conclusion in ('failure', 'cancelled'):
            api = FakeApi(mirrored=False, runs=[run_record(conclusion=conclusion)])
            result = dispatch.check_and_dispatch(api, SOURCE)
            self.assertFalse(result['needs_build'])
            self.assertFalse(result['tag_mirror_verified'])
            self.assertEqual(result['existing_run_conclusion'], conclusion)
            self.assertEqual(api.writes, [])

    def test_cancelled_full_source_migration_run_does_not_suppress_first_lightweight_build(self):
        old = 'f8b950e7f5f818475e46fcb40ae1e04becfa0289'
        api = FakeApi(mirrored=False, runs=[run_record(id=44850508247, head_sha=old, conclusion='cancelled',
            html_url='https://github.com/' + dispatch.TARGET + '/actions/runs/44850508247')])
        result = dispatch.check_and_dispatch(api, SOURCE, source_checker=lambda candidate: candidate == SOURCE)
        self.assertTrue(result['dispatched'])
        self.assertNotIn('run_id', result)
        self.assertEqual(len(api.writes), 1)

    def test_publication_anchor_requires_valid_lightweight_ancestry(self):
        api = FakeApi()
        api.routes[(dispatch.TARGET, 'git/tags/' + PUBLICATION)]['object']['sha'] = OTHER
        with self.assertRaisesRegex(ValueError, 'schema-2 maintenance ancestor'):
            dispatch.check_and_dispatch(api, SOURCE, source_checker=lambda candidate: candidate == SOURCE)
        self.assertEqual(api.writes, [])

    def test_old_schema_controller_fails_before_http(self):
        api = FakeApi()
        with self.assertRaisesRegex(ValueError, 'schema-2 lightweight'):
            dispatch.check_and_dispatch(api, SOURCE, source_checker=lambda candidate: False)
        self.assertEqual(api.calls, [])

    def test_pending_without_mirror_is_retained_without_claiming_original_identity(self):
        api = FakeApi(mirrored=False, runs=[run_record(status='in_progress', conclusion=None)])
        result = dispatch.check_and_dispatch(api, SOURCE)
        self.assertFalse(result['needs_build'])
        self.assertFalse(result['tag_mirror_verified'])
        self.assertEqual(result['existing_run_status'], 'in_progress')
        self.assertEqual(api.writes, [])

    def test_completed_without_mirror_requires_review_and_never_redispatches(self):
        api = FakeApi(mirrored=False, runs=[run_record()])
        self.rejects_without_write(api)

    def test_unknown_matching_run_status_is_not_silently_recovered(self):
        self.rejects_without_write(FakeApi(runs=[run_record(status='unknown-status')]))

    def test_malformed_sha_foreign_repo_and_mismatched_run_fields_are_not_recovered(self):
        changes = ({'head_sha': None}, {'head_sha': 123}, {'head_sha': 'not-a-sha'},
                   {'head_sha': 'a' * 39}, {'head_sha': 'A' * 40}, {'head_sha': SOURCE + '\n'},
                   {'head_branch': 'candidate/old'}, {'event': 'push'}, {'workflow_id': WORKFLOW_ID + 1},
                   {'repository': {'full_name': 'foreign/repo'}}, {'path': '.github/workflows/other.yml'},
                   {'created_at': '2026-11-18T07:59:59Z'}, {'html_url': 'https://github.com/foreign/repo/actions/runs/123'},
                   {'display_title': 'OpenRV v4.0.2-zh.1 build 17'}, {'display_title': 'OpenRV v4.0.2 build 017'},
                   {'display_title': 'OpenRV v4.0.2 build 18'}, {'run_number': 18}, {'run_number': True},
                   {'display_title': None}, {'id': True})
        for change in changes:
            api = FakeApi(runs=[run_record(**change)])
            with self.subTest(change=change):
                result = dispatch.check_and_dispatch(api, SOURCE)
                self.assertTrue(result['dispatched'])
                self.assertNotIn('run_id', result)
                self.assertEqual(len(api.writes), 1)

    def test_workflow_id_boolean_cannot_equal_integer_one(self):
        api = FakeApi(runs=[])
        api.routes[(dispatch.TARGET, 'actions/workflows/localized-release.yml')]['id'] = 1
        api.routes[(dispatch.TARGET, 'actions/workflows/1/runs?event=workflow_dispatch&branch=main&per_page=100&page=1')] = {
            'workflow_runs': [run_record(workflow_id=True)]}
        self.assertTrue(dispatch.check_and_dispatch(api, SOURCE)['dispatched'])

    def test_workflow_identity_and_history_shape_are_checked(self):
        for change in ({'id': True}, {'state': 'disabled_manually'}, {'path': '.github/workflows/other.yml'}):
            api = FakeApi()
            api.routes[(dispatch.TARGET, 'actions/workflows/localized-release.yml')].update(change)
            with self.subTest(change=change):
                self.rejects_without_write(api)
        for runs in (None, {}, [None], [run_record()] * 101):
            api = FakeApi()
            api.routes[(dispatch.TARGET, HISTORY + '1')] = {'workflow_runs': runs}
            with self.subTest(runs_type=type(runs).__name__):
                self.rejects_without_write(api)

    def test_second_page_recovery_is_read_only(self):
        api = FakeApi()
        api.routes[(dispatch.TARGET, HISTORY + '1')] = {'workflow_runs': [run_record(display_title='unrelated')] * 100}
        api.routes[(dispatch.TARGET, HISTORY + '2')] = {'workflow_runs': [run_record()]}
        result = dispatch.check_and_dispatch(api, SOURCE)
        self.assertEqual(result['run_id'], 123)
        self.assertEqual(api.writes, [])

    def test_recovery_window_overflow_fails_closed(self):
        api = FakeApi()
        for page in range(1, 6):
            api.routes[(dispatch.TARGET, HISTORY + str(page))] = {'workflow_runs': [run_record(display_title='unrelated')] * 100}
        self.rejects_without_write(api)
        self.assertEqual(sum('/runs?' in call[2] for call in api.calls), 5)

    def test_history_before_publication_stops_pagination(self):
        api = FakeApi()
        api.routes[(dispatch.TARGET, HISTORY + '1')] = {'workflow_runs':
            [run_record(display_title='unrelated')] * 99 + [run_record(created_at='2026-11-18T07:00:00Z')]}
        self.assertTrue(dispatch.check_and_dispatch(api, SOURCE)['dispatched'])
        self.assertEqual(sum('/runs?' in call[2] for call in api.calls), 1)


class ApiBoundaryTests(NoNetworkTest):
    def api(self, raw=b'{}', status=200):
        response = mock.MagicMock()
        response.status = status
        response.read.return_value = raw
        response.__enter__.return_value = response
        api = dispatch.Api('fixture-token')
        api.opener = mock.Mock(open=mock.Mock(return_value=response))
        return api

    def test_only_main_official_tag_dispatch_can_write(self):
        api = self.api(raw=b'', status=204)
        status, body = api.request('POST', dispatch.TARGET, 'actions/workflows/localized-release.yml/dispatches',
            {'ref': 'main', 'inputs': {'official_tag': TAG}})
        self.assertEqual((status, body), (204, None))
        request = api.opener.open.call_args.args[0]
        self.assertEqual(request.full_url, 'https://api.github.com/repos/' + dispatch.TARGET + '/actions/workflows/localized-release.yml/dispatches')
        self.assertEqual(json.loads(request.data), {'ref': 'main', 'inputs': {'official_tag': TAG}})

    def test_other_api_mutations_and_reads_are_blocked_before_http(self):
        forbidden = [('POST', dispatch.TARGET, 'git/refs', {'ref': 'refs/heads/state', 'sha': SOURCE}),
                     ('PATCH', dispatch.TARGET, 'git/refs/heads/main', {'sha': OTHER}),
                     ('DELETE', dispatch.TARGET, 'git/refs/tags/' + TAG, None),
                     ('PUT', dispatch.TARGET, 'contents/state.json', {}),
                     ('POST', dispatch.UPSTREAM, 'actions/workflows/localized-release.yml/dispatches', {}),
                     ('GET', dispatch.TARGET, 'contents/state.json', None),
                     ('GET', 'foreign/repo', 'releases/latest', None),
                     ('GET', dispatch.TARGET, 'git/ref/tags/v4..2', None),
                     ('GET', dispatch.UPSTREAM, 'git/ref/tags/v04.0.2', None),
                     ('GET', dispatch.UPSTREAM, 'releases/latest', {})]
        for method, repository, suffix, payload in forbidden:
            api = self.api()
            with self.subTest(method=method, repository=repository, suffix=suffix):
                with self.assertRaises(ValueError):
                    api.request(method, repository, suffix, payload)
                api.opener.open.assert_not_called()

    def test_invalid_dispatch_payloads_are_blocked_before_http(self):
        payloads = [None, [], {'ref': SOURCE, 'inputs': {'official_tag': TAG}},
                    {'ref': 'refs/heads/main', 'inputs': {'official_tag': TAG}},
                    {'ref': 'main', 'inputs': {'official_tag': TAG}, 'extra': True},
                    {'ref': 'main', 'inputs': None}, {'ref': 'main', 'inputs': ['official_tag']},
                    {'ref': 'main', 'inputs': {'official_tag': TAG, 'release_tag': TAG + '-zh.1'}}]
        payloads += [{'ref': 'main', 'inputs': {'official_tag': value}} for value in (123, None, [], 'v04.0.2', 'v4.0.2-zh.1', TAG + '\n')]
        for payload in payloads:
            api = self.api()
            with self.subTest(payload=payload):
                with self.assertRaises(ValueError):
                    api.request('POST', dispatch.TARGET, 'actions/workflows/localized-release.yml/dispatches', payload)
                api.opener.open.assert_not_called()

    def test_mirror_get_is_read_only_and_bounded(self):
        api = self.api(raw=json.dumps(reference('tags/' + TAG, COMMIT)).encode())
        status, value = api.request('GET', dispatch.TARGET, TAG_REF)
        self.assertEqual(status, 200)
        self.assertEqual(value['object']['sha'], COMMIT)
        response = api.opener.open.return_value.__enter__.return_value
        response.read.assert_called_once_with(4 * 1024 * 1024 + 1)
        api = self.api(raw=b'x' * (4 * 1024 * 1024 + 1))
        with self.assertRaises(ValueError):
            api.request('GET', dispatch.UPSTREAM, 'releases/latest')

    def test_own_annotated_tag_object_read_is_allowed_without_write_permission(self):
        api = self.api(raw=b'{}')
        self.assertEqual(api.request('GET', dispatch.TARGET, 'git/tags/' + PUBLICATION), (200, {}))
        request = api.opener.open.call_args.args[0]
        self.assertEqual(request.full_url, 'https://api.github.com/repos/' + dispatch.TARGET + '/git/tags/' + PUBLICATION)

    def test_redirect_and_http_errors_do_not_expose_or_follow_response_data(self):
        api = self.api()
        api.opener.open.side_effect = urllib.error.HTTPError('https://api.github.com/private', 301, 'fixture',
            {'Location': 'https://foreign.invalid/fixture-token'}, io.BytesIO(b'fixture-token response body'))
        self.assertEqual(api.request('GET', dispatch.UPSTREAM, 'releases/latest'), (301, None))
        self.assertIsNone(dispatch.NoRedirect().redirect_request(None, None, 301, '', {}, 'https://foreign.invalid'))


class EnvironmentAndTimestampTests(NoNetworkTest):
    @staticmethod
    def environment():
        return {'GITHUB_ACTIONS': 'true', 'GITHUB_REPOSITORY': dispatch.TARGET, 'GITHUB_REF': 'refs/heads/main',
                'GITHUB_EVENT_NAME': 'schedule', 'GITHUB_SHA': SOURCE, 'GH_TOKEN': 'fixture-token'}

    def test_timestamp_requires_complete_utc_iso_format(self):
        self.assertEqual(dispatch.timestamp(PUBLISHED).utcoffset(), timedelta(0))
        self.assertEqual(dispatch.timestamp('2026-11-18T08:00:00.123456Z').microsecond, 123456)
        for invalid in ('2026-11-18Z', '2026-11-18T08:00:00+00:00', '2026-11-18 08:00:00Z',
                        '2026-11-18T08:00:00z', '2026-99-18T08:00:00Z', PUBLISHED + '\n', None):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                dispatch.timestamp(invalid)

    def test_invalid_environment_is_rejected_before_git_or_http(self):
        for key, invalid in (('GITHUB_ACTIONS', 'True'), ('GITHUB_REPOSITORY', 'foreign/repo'),
                             ('GITHUB_REF', 'refs/tags/' + TAG), ('GITHUB_EVENT_NAME', 'push')):
            environment = self.environment()
            environment[key] = invalid
            with self.subTest(key=key), mock.patch.dict(os.environ, environment, clear=True), \
                    mock.patch.object(sys, 'argv', ['dispatch.py', '--probe']), \
                    mock.patch.object(dispatch.subprocess, 'check_output') as git, mock.patch.object(dispatch, 'Api') as api, \
                    contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    dispatch.main()
                git.assert_not_called()
                api.assert_not_called()

    def test_wrong_checkout_or_missing_token_is_rejected_before_api(self):
        for actual, token in ((OTHER, 'fixture-token'), (SOURCE, '')):
            environment = self.environment()
            environment['GH_TOKEN'] = token
            with self.subTest(actual=actual, token_present=bool(token)), mock.patch.dict(os.environ, environment, clear=True), \
                    mock.patch.object(sys, 'argv', ['dispatch.py', '--probe']), \
                    mock.patch.object(dispatch.subprocess, 'check_output', return_value=actual + '\n'), \
                    mock.patch.object(dispatch, 'Api') as api:
                with self.assertRaises(ValueError):
                    dispatch.main()
                api.assert_not_called()

    def test_cli_reused_build_summary_distinguishes_controller_and_build_snapshots(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            environment = dict(self.environment(), GITHUB_SHA=OTHER,
                GITHUB_OUTPUT=str(root / 'output'), GITHUB_STEP_SUMMARY=str(root / 'summary'))
            api = FakeApi(runs=[run_record(conclusion='failure')])
            api.routes[(dispatch.TARGET, MAIN_REF)] = reference('heads/main', OTHER)
            with mock.patch.dict(os.environ, environment, clear=True), \
                    mock.patch.object(sys, 'argv', ['dispatch.py', '--repo', str(root)]), \
                    mock.patch.object(dispatch.subprocess, 'check_output', return_value=OTHER + '\n'), \
                    mock.patch.object(dispatch, 'Api', return_value=api), contextlib.redirect_stdout(io.StringIO()) as stdout:
                self.assertEqual(dispatch.main(), 0)
            result = json.loads(stdout.getvalue())
            self.assertEqual(result['existing_run_source_sha'], SOURCE)
            self.assertEqual((root / 'output').read_text(encoding='utf-8'), 'needs_build=false\n')
            summary = (root / 'summary').read_text(encoding='utf-8')
            self.assertIn('current controller snapshot `' + OTHER + '`', summary)
            self.assertIn('build source snapshot `' + SOURCE + '`', summary)
            self.assertIn('manually dispatch the formal packaging workflow `localized-release.yml`', summary)
            self.assertEqual(api.writes, [])

    def test_cli_probe_writes_only_job_outputs_and_never_dispatches(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            environment = dict(self.environment(), GITHUB_OUTPUT=str(root / 'output'), GITHUB_STEP_SUMMARY=str(root / 'summary'))
            api = FakeApi(mirrored=False)
            with mock.patch.dict(os.environ, environment, clear=True), \
                    mock.patch.object(sys, 'argv', ['dispatch.py', '--repo', str(root), '--probe']), \
                    mock.patch.object(dispatch.subprocess, 'check_output', return_value=SOURCE + '\n'), \
                    mock.patch.object(dispatch, 'Api', return_value=api), contextlib.redirect_stdout(io.StringIO()) as stdout:
                self.assertEqual(dispatch.main(), 0)
            self.assertEqual((root / 'output').read_text(encoding='utf-8'), 'needs_build=true\n')
            self.assertIn('No candidate branches, state branches or custom edition tags', (root / 'summary').read_text(encoding='utf-8'))
            self.assertNotIn('fixture-token', stdout.getvalue())
            self.assertEqual(api.writes, [])


if __name__ == '__main__':
    unittest.main()
