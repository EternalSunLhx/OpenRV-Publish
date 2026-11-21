"""Own annotation publication with real, isolated maintenance ancestry and no network."""
from __future__ import annotations

import copy
from dataclasses import make_dataclass
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import publication_tags as tags

OFFICIAL = {'release_id': 456, 'tag': 'v4.1.0', 'object_sha': 'a' * 40,
            'commit_sha': 'b' * 40, 'published_at': '2026-11-20T18:15:05Z'}
CREATED, CONCURRENT = 'c' * 40, 'd' * 40


def annotation(commit, identity=CREATED, release=OFFICIAL):
    return {'sha': identity, 'tag': release['tag'], 'message': tags.publication_tag_message(release),
            'object': {'type': 'commit', 'sha': commit}}


class FakeApi:
    def __init__(self, commit=None):
        self.calls = []
        self.objects = {}
        self.reference = None
        self.race = None
        self.annotation_status = 201
        self.ref_status = 201
        self.change_created = None
        if commit:
            self.set_tag(annotation(commit))

    def set_tag(self, value):
        self.objects[value['sha']] = copy.deepcopy(value)
        self.reference = {'ref': 'refs/tags/' + OFFICIAL['tag'],
                          'object': {'type': 'tag', 'sha': value['sha']}}

    @property
    def writes(self):
        return [call for call in self.calls if call[0] != 'GET']

    def request(self, method, repository, suffix, payload=None):
        self.calls.append((method, repository, suffix, copy.deepcopy(payload)))
        assert repository == tags.TARGET
        if method == 'GET' and suffix == 'git/ref/tags/' + OFFICIAL['tag']:
            return (200, copy.deepcopy(self.reference)) if self.reference else (404, None)
        if method == 'GET' and suffix.startswith('git/tags/'):
            result = self.objects.get(suffix.removeprefix('git/tags/'))
            return (200, copy.deepcopy(result)) if result else (404, None)
        if method == 'POST' and suffix == 'git/tags':
            assert set(payload) == {'tag', 'message', 'object', 'type'}
            value = {'sha': CREATED, 'tag': payload['tag'], 'message': payload['message'],
                     'object': {'type': payload['type'], 'sha': payload['object']}}
            if self.change_created:
                self.change_created(value)
            self.objects[CREATED] = copy.deepcopy(value)
            return self.annotation_status, value
        if method == 'POST' and suffix == 'git/refs':
            assert set(payload) == {'ref', 'sha'}
            if self.race is not None:
                self.set_tag(self.race)
                return 422, None
            if self.ref_status == 201:
                self.set_tag(self.objects[payload['sha']])
            return self.ref_status, None
        raise AssertionError('Unexpected API operation: ' + method + ' ' + suffix)


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.repo = Path(self.temporary.name)
        self.git('init', '-b', 'main')
        self.git('config', 'user.name', 'Offline fixture')
        self.git('config', 'user.email', 'fixture@example.invalid')
        self.config = self.repo / tags.CONFIG_PATH
        self.config.parent.mkdir(parents=True)
        self.old = self.commit_config(1)
        self.first = self.commit_config(2)
        (self.repo / 'own-maintenance.txt').write_text('second maintenance snapshot\n')
        self.git('add', '.')
        self.git('commit', '-m', 'Second lightweight maintenance snapshot')
        self.current = self.git('rev-parse', 'HEAD')
        self.plan = {'official': dict(OFFICIAL), 'localization_snapshot': self.current,
                     'publication_commit': self.current}
        network = mock.patch('urllib.request.urlopen', side_effect=AssertionError('Live HTTP is forbidden'))
        network.start()
        self.addCleanup(network.stop)

    def git(self, *arguments):
        return subprocess.check_output(['git', '-C', str(self.repo), *arguments],
                                       stderr=subprocess.DEVNULL, text=True).strip()

    def commit_config(self, schema):
        self.config.write_text(json.dumps({'schema': schema, 'repository': tags.UPSTREAM}) + '\n')
        self.git('add', '.')
        self.git('commit', '-m', 'Schema ' + str(schema))
        return self.git('rev-parse', 'HEAD')

    def test_canonical_message_supports_five_field_release_dataclass(self):
        release_type = make_dataclass('Release', [(name, type(value)) for name, value in OFFICIAL.items()])
        message = tags.publication_tag_message(release_type(**OFFICIAL))
        decoded = json.loads(message)
        self.assertEqual(decoded, {'schema': 1, 'kind': 'openrv-publication', 'official': OFFICIAL})
        self.assertEqual(message, json.dumps(decoded, sort_keys=True, separators=(',', ':'), ensure_ascii=True) + '\n')
        for changes in ({'release_id': True}, {'tag': 'v04.1.0'}, {'commit_sha': 'B' * 40},
                        {'published_at': '2026-11-20Z'}, {'extra': 'not accepted'}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                tags.publication_tag_message({**OFFICIAL, **changes})

    def test_missing_tag_anchors_to_current_lightweight_snapshot_without_writes(self):
        api = FakeApi()
        self.assertEqual(tags.resolve_publication_commit(self.repo, self.current, api, OFFICIAL), self.current)
        self.assertEqual(api.writes, [])

    def test_existing_own_ancestor_is_reused_across_maintenance_commits(self):
        api = FakeApi(self.first)
        self.assertEqual(tags.resolve_publication_commit(self.repo, self.current, api, OFFICIAL), self.first)
        self.assertEqual(tags.verify_publication_tag(api, OFFICIAL, self.first), api.reference)
        self.assertEqual(api.writes, [])

    def test_old_full_source_schema_and_nonancestor_are_rejected(self):
        self.assertFalse(tags.is_light_snapshot(self.repo, self.old, self.current))
        self.assertTrue(tags.is_light_snapshot(self.repo, self.first, self.current))
        self.assertFalse(tags.is_light_snapshot(self.repo, self.current, self.first))
        for anchor in (self.old, 'e' * 40):
            with self.subTest(anchor=anchor), self.assertRaisesRegex(ValueError, 'schema-2 ancestor'):
                tags.resolve_publication_commit(self.repo, self.current, FakeApi(anchor), OFFICIAL)
        with self.assertRaisesRegex(ValueError, 'Current maintenance snapshot'):
            tags.resolve_publication_commit(self.repo, self.old, FakeApi(), OFFICIAL)

    def test_wrong_config_repository_is_not_a_lightweight_source(self):
        self.config.write_text(json.dumps({'schema': 2, 'repository': 'foreign/repo'}))
        self.git('add', '.'); self.git('commit', '-m', 'Wrong config')
        current = self.git('rev-parse', 'HEAD')
        self.assertFalse(tags.is_light_snapshot(self.repo, current, current))

    def test_every_official_identity_field_is_immutably_bound(self):
        for change in ({'release_id': 457}, {'object_sha': 'e' * 40}, {'commit_sha': 'f' * 40},
                       {'published_at': '2026-11-20T18:15:06Z'}):
            api = FakeApi(self.current)
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, 'annotation differs'):
                tags.verify_publication_tag(api, {**OFFICIAL, **change}, self.current)
            self.assertEqual(api.writes, [])
        api = FakeApi(self.current)
        api.objects[CREATED]['tag'] = 'v4.1.1'
        with self.assertRaises(ValueError):
            tags.verify_publication_tag(api, OFFICIAL, self.current)

    def test_noncanonical_or_chained_annotations_and_lightweight_refs_are_rejected(self):
        for mode in ('whitespace', 'extra-field', 'chain', 'different-object-sha', 'lightweight-ref'):
            api = FakeApi(self.current)
            value = api.objects[CREATED]
            if mode == 'whitespace': value['message'] = value['message'].strip()
            elif mode == 'extra-field': value['message'] = value['message'].replace('"schema":1', '"schema":1,"other":0')
            elif mode == 'chain': value['object']['type'] = 'tag'
            elif mode == 'different-object-sha': value['sha'] = CONCURRENT
            else: api.reference['object']['type'] = 'commit'
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                tags.verify_publication_tag(api, OFFICIAL, self.current)

    def test_creation_posts_only_own_annotation_and_ref_and_never_pushes_upstream_objects(self):
        api = FakeApi()
        original_run = subprocess.run
        with mock.patch.object(tags.subprocess, 'run', wraps=original_run) as commands:
            self.assertTrue(tags.sync_publication_tag(self.repo, self.plan, api))
        self.assertTrue(all(call.args[0][3] in ('merge-base', 'show') for call in commands.call_args_list))
        self.assertEqual(api.writes, [
            ('POST', tags.TARGET, 'git/tags', {'tag': OFFICIAL['tag'],
                'message': tags.publication_tag_message(OFFICIAL), 'object': self.current, 'type': 'commit'}),
            ('POST', tags.TARGET, 'git/refs', {'ref': 'refs/tags/' + OFFICIAL['tag'], 'sha': CREATED})])
        for official_sha in (OFFICIAL['object_sha'], OFFICIAL['commit_sha']):
            with self.assertRaises(subprocess.CalledProcessError):
                self.git('cat-file', '-e', official_sha)

    def test_existing_tag_is_idempotent_and_immutable(self):
        api = FakeApi(self.current)
        self.assertFalse(tags.sync_publication_tag(self.repo, self.plan, api))
        self.assertEqual(api.writes, [])
        api = FakeApi(self.first)
        with self.assertRaisesRegex(ValueError, 'different maintenance commit'):
            tags.sync_publication_tag(self.repo, self.plan, api)
        self.assertEqual(api.writes, [])

    def test_concurrent_exact_creation_is_reused_but_conflicting_anchor_is_rejected(self):
        api = FakeApi(); api.race = annotation(self.current, CONCURRENT)
        self.assertFalse(tags.sync_publication_tag(self.repo, self.plan, api))
        self.assertEqual(api.reference['object']['sha'], CONCURRENT)
        api = FakeApi(); api.race = annotation(self.first, CONCURRENT)
        with self.assertRaisesRegex(ValueError, 'different maintenance commit'):
            tags.sync_publication_tag(self.repo, self.plan, api)
        self.assertEqual({call[0] for call in api.writes}, {'POST'})

    def test_concurrent_official_record_change_is_rejected(self):
        api = FakeApi(); api.race = annotation(self.current, CONCURRENT, {**OFFICIAL, 'release_id': 457})
        with self.assertRaisesRegex(ValueError, 'annotation differs'):
            tags.sync_publication_tag(self.repo, self.plan, api)

    def test_disappeared_existing_anchor_is_not_recreated_at_a_new_snapshot(self):
        api = FakeApi()
        with self.assertRaisesRegex(ValueError, 'disappeared'):
            tags.sync_publication_tag(self.repo, {**self.plan, 'publication_commit': self.first}, api)
        self.assertEqual(api.writes, [])

    def test_wrong_created_annotation_is_rejected_before_ref_creation(self):
        api = FakeApi(); api.change_created = lambda value: value['object'].update(sha=self.first)
        with self.assertRaisesRegex(ValueError, 'different maintenance anchor'):
            tags.sync_publication_tag(self.repo, self.plan, api)
        self.assertEqual(len(api.writes), 1)

    def test_api_failures_never_fall_back_to_git_push_or_ref_update(self):
        for stage in ('annotation', 'ref'):
            api = FakeApi()
            if stage == 'annotation': api.annotation_status = 403
            else: api.ref_status = 403
            with self.subTest(stage=stage), self.assertRaises(RuntimeError):
                tags.sync_publication_tag(self.repo, self.plan, api)
            self.assertTrue(all(call[0] == 'POST' for call in api.writes))


if __name__ == '__main__':
    unittest.main()
