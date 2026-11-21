"""Offline public-format and release-transaction tests; no GitHub writes."""
from dataclasses import asdict, replace
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import official_workspace as workspace
import prepare_release
import publication_tags
import public_assets


def marker_text(record):
    return '<!-- openrv-publication: ' + json.dumps(record, sort_keys=True, separators=(',', ':')) + ' -->'


class ReleaseServer:
    def __init__(self, owner, *, exists=False, draft=True, records=(), marker=None):
        self.owner, self.exists = owner, exists
        self.release = {'id': getattr(owner, 'release_id', 123), 'tag_name': owner.release.tag, 'draft': draft, 'prerelease': False,
                        'html_url': f'https://github.com/{workspace.TARGET}/releases/tag/'
                                    + ('untagged-' + 'a' * 20 if draft else owner.release.tag),
                        'name': owner.title, 'body': owner.pending if marker is None
                        else owner.pending.rstrip() + '\n\n' + marker_text(marker) + '\n'}
        self.assets = {record['name']: copy.deepcopy(record) for record in records}
        self.events, self.commands = [], []
        self.asset_reads = self.source_reads = self.tag_reads = 0
        self.fail_upload_after = self.fail_delete_after = 0
        self.fail_edit_once = False
        self.final_asset_change = self.post_publish_asset_change = None
        self.source_changed = self.tag_changed = self.keep_temporary_asset_urls = False
        self.latest = self.release_pages = self.release_read_change = None

    def request(self, method, repository, suffix, payload=None):
        assert (method, repository, payload) == ('GET', workspace.TARGET, None)
        if suffix.startswith('releases?per_page=100&page='):
            self.events.append('list-releases'); page = int(suffix.rsplit('=', 1)[1])
            if self.release_pages is not None: return 200, copy.deepcopy(self.release_pages.get(page, []))
            return 200, [copy.deepcopy(self.release)] if self.exists and page == 1 else []
        if suffix == f'releases/{self.release["id"]}':
            self.events.append('read-release'); record = copy.deepcopy(self.release)
            if self.release_read_change: self.release_read_change(record)
            return (200, record) if self.exists else (404, {})
        if suffix == f'releases/{self.release["id"]}/assets?per_page=100&page=1':
            self.events.append('read-assets'); self.asset_reads += 1
            records = copy.deepcopy(list(self.assets.values()))
            if self.asset_reads >= 2 and self.final_asset_change: self.final_asset_change(records)
            if not self.release['draft'] and self.post_publish_asset_change: self.post_publish_asset_change(records)
            return 200, records
        if suffix == 'releases/latest':
            self.events.append('read-latest')
            if self.latest: return 200, copy.deepcopy(self.latest)
            return (200, copy.deepcopy(self.release)) if self.exists and not self.release['draft'] else (404, {})
        raise AssertionError(suffix)

    def official_release(self, api, tag):
        self.events.append('verify-upstream'); self.source_reads += 1
        return replace(self.owner.release, commit_sha='b' * 40) if self.source_changed and self.source_reads > 1 else self.owner.release

    def verify_publication_tag(self, api, release, publication_commit):
        self.events.append('verify-tag'); self.tag_reads += 1
        assert release == self.owner.release and publication_commit == 'c' * 40
        if self.tag_changed and self.tag_reads > 1: raise ValueError('Publication tag changed before release publication')
        return {'object': {'sha': 'd' * 40, 'type': 'tag'}}

    def gh(self, command, *, check, timeout, **kwargs):
        self.commands.append(command)
        assert check is True and '--clobber' not in command
        if command[:2] == ['gh', 'api']:
            assert command[command.index('--method') + 1] == 'DELETE' and timeout == 120
            assert command[-1].startswith(f'repos/{workspace.TARGET}/releases/assets/')
            asset_id = int(command[-1].rsplit('/', 1)[1])
            matches = [name for name, record in self.assets.items() if record['id'] == asset_id]
            assert len(matches) == 1
            self.events.append('delete-old'); del self.assets[matches[0]]
            if self.fail_delete_after:
                self.fail_delete_after -= 1
                if not self.fail_delete_after: raise subprocess.CalledProcessError(1, command)
            return subprocess.CompletedProcess(command, 0, '')
        assert command[:2] == ['gh', 'release'] and timeout == 1800
        operation = command[2]
        if operation == 'create':
            self.events.append('create-draft')
            assert '--draft' in command and '--verify-tag' in command and '--latest=false' in command and not self.exists
            self.exists = True
            self.release['body'] = Path(command[command.index('--notes-file') + 1]).read_text(encoding='utf-8')
            assert public_assets.MARKER not in self.release['body']
        elif operation == 'upload':
            self.events.append('upload'); assert command[4:6] == ['--repo', workspace.TARGET]
            for path in command[6:]:
                name = Path(path).name
                assert name not in self.assets and '-build.' not in name and not name.endswith('.build.json')
                self.assets[name] = self.owner.record(name, Path(path).read_bytes())
                if self.release['draft']:
                    pending = self.release['html_url'].rsplit('/', 1)[1]
                    self.assets[name]['browser_download_url'] = f'https://github.com/{workspace.TARGET}/releases/download/{pending}/{name}'
                if self.fail_upload_after:
                    self.fail_upload_after -= 1
                    if not self.fail_upload_after: raise subprocess.CalledProcessError(1, command)
        elif operation == 'edit':
            if self.fail_edit_once:
                self.fail_edit_once = False
                raise subprocess.CalledProcessError(1, command)
            self.events.append('publish-marker')
            assert '--draft=false' in command and '--prerelease=false' in command and '--verify-tag' in command
            self.release.update(draft=False, prerelease=False, name=command[command.index('--title') + 1],
                                body=Path(command[command.index('--notes-file') + 1]).read_text(encoding='utf-8'),
                                html_url=f'https://github.com/{workspace.TARGET}/releases/tag/{self.owner.release.tag}')
            if not self.keep_temporary_asset_urls:
                for name, record in self.assets.items():
                    record['browser_download_url'] = f'https://github.com/{workspace.TARGET}/releases/download/{self.owner.release.tag}/{name}'
        else: raise AssertionError(operation)
        return subprocess.CompletedProcess(command, 0)


class PublishReleaseTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name); self.assets = self.root / 'original'; self.assets.mkdir()
        self.repo = self.root / 'source'; self.updater = self.repo / 'src/lib/app/RvCommon/RvUpdateChecker.cpp'
        self.updater.parent.mkdir(parents=True); self.updater.write_text('// new openrv-publication: parser fixture\n')
        tag = getattr(self, 'fixture_tag', 'v4.0.2'); number = getattr(self, 'fixture_build', 29)
        base = f'OpenRV-{tag[1:]}-build.{number}'; self.original_names = []; groups = []
        for platform, suffixes in (('Windows-x64', ('-Setup.exe', '-Portable.zip')),
                                   ('macOS-x86_64', ('.dmg', '.zip')), ('macOS-arm64', ('.dmg', '.zip'))):
            files = [base + '-' + platform + suffix for suffix in (*suffixes, '.build.json')]
            groups.append((base + '-' + platform + '.sha256.txt', files))
        groups.append((base + '-source.sha256.txt', [base + '-source.provenance.json', base + '-generated-localization.patch.zip']))
        for checksum, names in groups:
            for name in names: (self.assets / name).write_bytes(('Original validated fixture ' + name).encode())
            self.original_names.extend([*names, checksum]); self.write_checksum(checksum, names)
        notes = f'[Windows](https://github.com/{workspace.TARGET}/releases/download/{tag}/{base}-Windows-x64-Setup.exe)\n'
        (self.assets / 'RELEASE-NOTES.md').write_text(notes, encoding='utf-8')
        self.title = 'OpenRV ' + tag + ' 中英文版'
        self.release = workspace.watcher().Release(456, tag, 'a' * 40, 'a' * 40, '2026-08-20T17:15:15Z')
        self.plan = {'official': asdict(self.release), 'build_number': number, 'publication_commit': 'c' * 40}
        self.public = self.root / 'public-proof'
        self.projection = public_assets.project_assets(self.assets, self.public, self.release.tag, number)
        self.names = sorted(self.projection['expected']); self.notes = self.projection['notes'].read_text(encoding='utf-8')
        self.pending = self.projection['pending_notes'].read_text(encoding='utf-8')

    def write_checksum(self, checksum, names):
        (self.assets / checksum).write_text(''.join(hashlib.sha256((self.assets / name).read_bytes()).hexdigest()
                                                   + '  ' + name + '\n' for name in names), encoding='ascii')

    def record(self, name, data=None):
        if data is None: data = ((self.public if name in self.names else self.assets) / name).read_bytes()
        return {'id': 1000 + int(hashlib.sha256(name.encode()).hexdigest()[:10], 16), 'name': name,
                'state': 'uploaded', 'size': len(data), 'digest': 'sha256:' + hashlib.sha256(data).hexdigest(),
                'browser_download_url': f'https://github.com/{workspace.TARGET}/releases/download/{self.release.tag}/{name}'}

    def publish(self, server, *, replace_legacy_v410=False):
        with mock.patch.object(workspace, 'official_release', side_effect=server.official_release), \
                mock.patch.object(publication_tags, 'verify_publication_tag', side_effect=server.verify_publication_tag), \
                mock.patch.object(prepare_release.subprocess, 'run', side_effect=server.gh):
            return prepare_release.publish_assets(self.repo, self.assets, self.plan, server,
                                                  replace_legacy_v410=replace_legacy_v410)

    def current_server(self):
        return ReleaseServer(self, exists=True, draft=False, records=[self.record(name) for name in self.names], marker=self.projection['marker'])

    def older_server(self):
        records = [self.record(name, b'older validated build ' + (self.public / name).read_bytes()) for name in self.names]
        marker = copy.deepcopy(self.projection['marker']); marker['buildNumber'] = '28'
        marker['assets'] = {record['name']: record['digest'] for record in records}
        return ReleaseServer(self, exists=True, draft=False, records=records, marker=marker)

    def test_projection_preserves_eight_payloads_and_publishes_only_twelve_clean_files(self):
        self.assertEqual(len(self.names), 12)
        self.assertFalse(any('-build.' in name or name.endswith('.build.json') for name in self.names))
        unchanged = {name: self.projection['mapping'][name] for name in self.original_names if not name.endswith(('.build.json', '.sha256.txt'))}
        self.assertEqual(len(unchanged), 8)
        for old, new in unchanged.items(): self.assertEqual((self.assets / old).read_bytes(), (self.public / new).read_bytes())
        for checksum in self.public.glob('*.sha256.txt'):
            lines = checksum.read_text().splitlines(); self.assertEqual(len(lines), 2)
            for line in lines:
                digest, name = line.split('  ')
                self.assertEqual(digest, hashlib.sha256((self.public / name).read_bytes()).hexdigest())
                self.assertFalse(name.endswith('.build.json'))
        self.assertEqual(public_assets.parse_publication(self.notes, self.release.tag), self.projection['marker'])
        self.assertEqual(self.notes.count(public_assets.MARKER), 1); self.assertNotIn('build.29', self.notes)
        self.assertEqual(len(list(self.assets.iterdir())), 16)

    def test_new_release_uploads_twelve_then_commits_marker_after_remote_verification(self):
        server = ReleaseServer(self); result = self.publish(server)
        self.assertEqual([command[2] for command in server.commands], ['create', 'upload', 'edit'])
        self.assertEqual(len(server.assets), 12)
        publication = server.events.index('publish-marker')
        self.assertIn('read-assets', server.events[server.events.index('upload') + 1:publication])
        self.assertEqual(server.events[publication - 2:publication], ['verify-upstream', 'verify-tag'])
        self.assertEqual(server.release['body'], self.notes)
        self.assertEqual((result['status'], result['publishedBuildNumber']), ('published', 29))

    def test_legacy_product_and_existing_historical_release_are_never_migrated(self):
        self.updater.write_text('// original updater without new naming support\n'); server = ReleaseServer(self)
        with self.assertRaisesRegex(ValueError, 'historical product'): self.publish(server)
        self.assertEqual(server.events, [])
        self.updater.write_text('// openrv-publication: compatible parser\n')
        server = ReleaseServer(self, exists=True, draft=False, records=[self.record(name) for name in self.original_names])
        before = copy.deepcopy(server.assets), copy.deepcopy(server.release)
        with self.assertRaisesRegex(ValueError, 'Historical releases'): self.publish(server)
        self.assertEqual(server.commands, []); self.assertEqual((server.assets, server.release), before)

    def test_partial_first_upload_resumes_only_missing_files_without_an_early_marker(self):
        server = ReleaseServer(self); server.fail_upload_after = 3
        with self.assertRaises(subprocess.CalledProcessError): self.publish(server)
        self.assertTrue(server.release['draft']); self.assertNotIn(public_assets.MARKER, server.release['body'])
        retained = copy.deepcopy(server.assets); server.commands.clear(); self.publish(server)
        uploaded = {Path(path).name for command in server.commands if command[2] == 'upload' for path in command[6:]}
        self.assertEqual(uploaded, set(self.names) - set(retained)); self.assertEqual(len(server.assets), 12)

    def test_exact_resume_is_idempotent_and_older_build_does_not_mutate(self):
        server = self.current_server(); self.publish(server); self.assertEqual(server.commands, [])
        marker = copy.deepcopy(self.projection['marker']); marker['buildNumber'] = '30'
        server = ReleaseServer(self, exists=True, draft=False, records=[self.record(name) for name in self.names], marker=marker)
        before = copy.deepcopy(server.release); result = self.publish(server)
        self.assertEqual((result['status'], result['requestedBuildNumber'], result['publishedBuildNumber']), ('skipped_newer', 29, 30))
        self.assertEqual(server.commands, []); self.assertEqual(server.release, before)

    def test_same_build_with_different_bytes_is_rejected_before_writes(self):
        server = self.current_server(); binary = 'OpenRV-4.0.2-build.29-Windows-x64-Setup.exe'
        (self.assets / binary).write_bytes(b'different purported build 29')
        files = [binary, binary.replace('-Setup.exe', '-Portable.zip'), binary.replace('-Setup.exe', '.build.json')]
        self.write_checksum(binary.replace('-Setup.exe', '.sha256.txt'), files)
        with self.assertRaisesRegex(ValueError, 'same build number'): self.publish(server)
        self.assertEqual(server.commands, [])

    def test_newer_unpublished_build_cannot_be_reported_as_published(self):
        for state in ('draft', 'prerelease'):
            server = self.current_server()
            record = copy.deepcopy(self.projection['marker']); record['buildNumber'] = '30'
            server.release['body'] = marker_text(record); server.release[state] = True
            with self.subTest(state=state), self.assertRaisesRegex(ValueError, 'not a published stable release'):
                self.publish(server)
            self.assertEqual(server.commands, [])

    def test_higher_build_replaces_only_authenticated_ids_and_commits_marker_last(self):
        server = self.older_server(); old_ids = {record['id'] for record in server.assets.values()}; self.publish(server)
        deletes = [command for command in server.commands if command[:2] == ['gh', 'api']]
        self.assertEqual({int(command[-1].rsplit('/', 1)[1]) for command in deletes}, old_ids); self.assertEqual(len(deletes), 12)
        self.assertGreater(server.events.index('publish-marker'), server.events.index('upload'))
        self.assertEqual(public_assets.parse_publication(server.release['body'], self.release.tag)['buildNumber'], '29')

    def test_interrupted_higher_build_resumes_without_replacing_correct_new_files(self):
        for interruption in ('delete', 'upload'):
            with self.subTest(interruption=interruption):
                server = self.older_server()
                if interruption == 'delete': server.fail_delete_after = 2
                else: server.fail_upload_after = 3
                with self.assertRaises(subprocess.CalledProcessError): self.publish(server)
                self.assertEqual(public_assets.parse_publication(server.release['body'], self.release.tag)['buildNumber'], '28')
                correct = {name for name, record in server.assets.items() if record['digest'] == self.projection['expected'][name]['digest']}
                server.commands.clear(); self.publish(server)
                uploaded = {Path(path).name for command in server.commands if command[:2] == ['gh', 'release']
                            and command[2] == 'upload' for path in command[6:]}
                self.assertEqual(uploaded, set(self.names) - correct); self.assertEqual(server.release['body'], self.notes)

    def test_invalid_existing_old_asset_prevents_every_replacement(self):
        for field, value in [('digest', 'sha256:' + '0' * 64), ('size', 0), ('state', 'new'), ('browser_download_url', 'https://foreign.invalid/asset')]:
            server = self.older_server(); server.assets[self.names[-1]][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError): self.publish(server)
            self.assertEqual(server.commands, [])

    def test_remote_loss_corruption_or_extra_asset_prevents_final_marker(self):
        for mutation in (lambda rows: rows.pop(), lambda rows: rows[0].update(digest='sha256:' + '0' * 64),
                         lambda rows: rows.append({**rows[0], 'id': 123456789, 'name': 'unexpected.txt'})):
            server = ReleaseServer(self); server.final_asset_change = mutation
            with self.subTest(mutation=mutation), self.assertRaises(ValueError): self.publish(server)
            self.assertTrue(server.release['draft']); self.assertNotIn(public_assets.MARKER, server.release['body'])

    def test_post_publish_still_requires_canonical_urls_and_matching_hashes(self):
        for mode in ('url', 'digest'):
            server = ReleaseServer(self)
            if mode == 'url': server.keep_temporary_asset_urls = True
            else: server.post_publish_asset_change = lambda rows: rows[0].update(digest='sha256:' + '0' * 64)
            with self.subTest(mode=mode), self.assertRaises(ValueError): self.publish(server)
            self.assertFalse(server.release['draft'])

    def test_draft_discovery_uses_paginated_listing_then_bound_id(self):
        server = ReleaseServer(self, exists=True)
        server.release_pages = {1: [{'id': 1000 + index, 'tag_name': f'v9.0.{index}'} for index in range(100)], 2: [server.release]}
        self.publish(server); self.assertEqual(server.events.count('list-releases'), 2); self.assertNotIn('create-draft', server.events)
        for rows in ([server.release, server.release], [server.release, {**server.release, 'id': 124}], [{**server.release, 'id': True}], [{**server.release, 'draft': 1}]):
            bad = ReleaseServer(self, exists=True); bad.release_pages = {1: rows}
            with self.subTest(rows=rows), self.assertRaises(ValueError): self.publish(bad)
            self.assertEqual(bad.commands, [])

    def test_release_identity_or_source_change_is_never_silently_accepted(self):
        for changes in ({'id': 124}, {'tag_name': 'v4.0.3'}, {'draft': None}, {'prerelease': 1}, {'html_url': 'https://foreign.invalid/release'}):
            server = ReleaseServer(self, exists=True); server.release_read_change = lambda record, changes=changes: record.update(changes)
            with self.subTest(changes=changes), self.assertRaises(ValueError): self.publish(server)
            self.assertEqual(server.commands, [])
        for field in ('source_changed', 'tag_changed'):
            server = ReleaseServer(self); setattr(server, field, True)
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, 'changed before release publication'): self.publish(server)
            self.assertNotIn(public_assets.MARKER, server.release['body'])

    def test_old_tag_cannot_replace_a_newer_latest_release(self):
        server = ReleaseServer(self)
        server.latest = {**server.release, 'id': 124, 'tag_name': 'v4.1.0', 'draft': False,
                         'html_url': f'https://github.com/{workspace.TARGET}/releases/tag/v4.1.0'}
        self.publish(server); self.assertIn('--latest=false', server.commands[-1])

    def test_incomplete_or_changed_local_cohort_is_rejected_before_github_reads(self):
        server = ReleaseServer(self); (self.assets / self.original_names[0]).write_bytes(b'changed after assembly')
        with self.assertRaisesRegex(ValueError, 'checksum changed'): self.publish(server)
        self.assertEqual(server.events, []); (self.assets / self.original_names[0]).unlink()
        with self.assertRaisesRegex(ValueError, 'exact fifteen assets'): self.publish(server)
        self.assertEqual(server.events, [])

    def test_marker_rejects_duplicate_wrong_type_tag_and_incomplete_inventory(self):
        valid = self.projection['marker']; invalid = []
        for key, value in [('schema', True), ('tag', 'v9.0.0'), ('buildNumber', 29), ('buildNumber', '0'),
                           ('buildNumber', '01'), ('buildNumber', '18446744073709551616'), ('assets', {})]:
            record = copy.deepcopy(valid); record[key] = value; invalid.append(marker_text(record))
        invalid += [marker_text(valid) * 2, '<!-- openrv-publication: {bad} -->',
                    marker_text(valid).replace('"schema":1', '"schema":1,"schema":1'),
                    marker_text(valid).replace('"schema":1', '"schema":1,\n'),
                    'inline ' + marker_text(valid), marker_text(valid) + ' trailing',
                    marker_text(valid).replace('openrv-publication', 'OPENRV-PUBLICATION'),
                    '<!-- OPENRV-PUBLICATION malformed -->',
                    marker_text(valid) + '\nOPENRV-PUBLICATION',
                    marker_text(valid).replace(': {', ': {' + ' ' * 16384)]
        for body in invalid:
            with self.subTest(body=body[:60]), self.assertRaises(ValueError): public_assets.parse_publication(body, self.release.tag)
        self.assertIsNone(public_assets.parse_publication('Legacy description', self.release.tag))
        for body in (marker_text(valid), 'Description\n' + marker_text(valid) + '\r\n',
                     marker_text(valid).replace('"schema":1', '"schema":1.0')):
            self.assertEqual(public_assets.parse_publication(body, self.release.tag), valid)

    def test_preflight_preserves_legacy_release_and_validates_clean_cohort_before_build(self):
        api = mock.Mock(); api.request.return_value = (404, {})
        public_assets.guard_published_release(api, self.release.tag)
        api.request.assert_called_once_with('GET', workspace.TARGET, 'releases/tags/v4.0.2')
        server = self.current_server(); record = {**server.release, 'assets': list(server.assets.values())}
        api.request.return_value = (200, record); public_assets.guard_published_release(api, self.release.tag)
        for mutation in (lambda value: value.update(body='Historical description'), lambda value: value['assets'].pop(),
                         lambda value: value['assets'][0].update(digest='sha256:' + '0' * 64), lambda value: value.update(draft=True)):
            changed = copy.deepcopy(record); mutation(changed); api.request.return_value = (200, changed)
            with self.subTest(mutation=mutation), self.assertRaises(ValueError): public_assets.guard_published_release(api, self.release.tag)


class LegacyMigrationTests(unittest.TestCase):
    fixture_tag, fixture_build, release_id = 'v4.1.0', 36, 453727799
    write_checksum = PublishReleaseTests.write_checksum
    record = PublishReleaseTests.record
    publish = PublishReleaseTests.publish

    def setUp(self):
        PublishReleaseTests.setUp(self)
        self.legacy = public_assets.legacy_inventory(self.fixture_tag, self.fixture_build)

    def server(self):
        records = [{**record, 'state': 'uploaded',
                    'browser_download_url': f'https://github.com/{workspace.TARGET}/releases/download/v4.1.0/{name}'}
                   for name, record in self.legacy.items()]
        server = ReleaseServer(self, exists=True, draft=False, records=records)
        server.release['body'] = 'Original build35 release notes retained until publication finishes.\n'
        return server

    def guard(self, server, *, enabled=True):
        api = mock.Mock()
        api.request.return_value = (200, {**server.release, 'assets': list(server.assets.values())})
        public_assets.guard_published_release(api, self.fixture_tag, number=self.fixture_build,
                                              replace_legacy_v410=enabled)

    def test_migration_switch_is_opt_in_and_strict(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertFalse(public_assets.legacy_migration_enabled())
        for value, expected in [('true', True), ('false', False)]:
            with mock.patch.dict(os.environ, ORV_REPLACE_LEGACY_V410=value):
                self.assertIs(public_assets.legacy_migration_enabled(), expected)
        for value in ('', 'TRUE', '1', 'yes'):
            with mock.patch.dict(os.environ, ORV_REPLACE_LEGACY_V410=value), self.assertRaises(ValueError):
                public_assets.legacy_migration_enabled()
        server = self.server()
        with self.assertRaises(ValueError): self.guard(server, enabled=False)
        with self.assertRaises(ValueError): self.publish(server)
        self.assertEqual(server.commands, [])

    def test_fixed_inventory_and_explicit_higher_build_migration(self):
        self.assertEqual(len(self.legacy), 15)
        self.assertEqual(len({record['id'] for record in self.legacy.values()}), 15)
        server = self.server(); self.guard(server)
        result = self.publish(server, replace_legacy_v410=True)
        self.assertEqual(result['publishedBuildNumber'], 36)
        self.assertEqual(set(server.assets), set(self.names))
        self.assertLess(server.events.index('upload'), server.events.index('delete-old'))
        self.assertLess(server.events.index('delete-old'), server.events.index('publish-marker'))
        self.assertIn('read-assets', server.events[server.events.index('upload') + 1:server.events.index('delete-old')])
        deleted = {int(command[-1].rsplit('/', 1)[1]) for command in server.commands if command[:2] == ['gh', 'api']}
        self.assertEqual(deleted, {record['id'] for record in self.legacy.values()})
        self.assertEqual(public_assets.parse_publication(server.release['body'], self.fixture_tag)['buildNumber'], '36')
        server.commands.clear(); self.publish(server, replace_legacy_v410=True)
        self.assertEqual(server.commands, [])

    def test_any_changed_legacy_identity_or_hash_aborts_before_writes(self):
        for field, value in [('id', 999), ('size', 1), ('digest', 'sha256:' + '0' * 64),
                             ('state', 'new'), ('browser_download_url', 'https://foreign.invalid/asset')]:
            server = self.server(); server.assets[sorted(self.legacy)[-1]][field] = value
            with self.subTest(field=field):
                with self.assertRaises(ValueError): self.guard(server)
                with self.assertRaises(ValueError): self.publish(server, replace_legacy_v410=True)
                self.assertEqual(server.commands, [])
        for changes in ({'id': 999}, {'draft': True}, {'prerelease': True}):
            server = self.server(); server.release.update(changes)
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.publish(server, replace_legacy_v410=True)
            self.assertEqual(server.commands, [])
        absent = ReleaseServer(self)
        with self.assertRaises(ValueError): self.publish(absent, replace_legacy_v410=True)
        self.assertEqual(absent.commands, [])
        for tag, number in [('v4.0.2', 36), ('v4.2.0', 36), ('v4.1.0', 35), ('v4.1.0', 34)]:
            with self.subTest(tag=tag, number=number), self.assertRaises(ValueError): public_assets.legacy_inventory(tag, number)

    def test_failed_upload_preserves_every_original_file_and_can_resume(self):
        server = self.server(); original = copy.deepcopy(server.assets); body = server.release['body']
        server.fail_upload_after = 3
        with self.assertRaises(subprocess.CalledProcessError): self.publish(server, replace_legacy_v410=True)
        self.assertEqual({name: server.assets[name] for name in self.legacy}, original)
        self.assertEqual(server.release['body'], body); self.assertNotIn('delete-old', server.events)
        with self.assertRaisesRegex(ValueError, 'original build run'): self.guard(server)
        server.commands.clear(); self.publish(server, replace_legacy_v410=True)
        uploads = [command for command in server.commands if command[:3] == ['gh', 'release', 'upload']]
        self.assertEqual(len(uploads), 1); self.assertEqual(len(uploads[0][6:]), 9)

    def test_invalid_new_upload_never_deletes_old_assets(self):
        for mutate in (lambda rows: rows.pop(), lambda rows: next(record for record in rows if '-build.' not in record['name']).update(digest='sha256:' + '0' * 64)):
            server = self.server(); server.final_asset_change = mutate
            with self.subTest(mutate=mutate), self.assertRaises(ValueError): self.publish(server, replace_legacy_v410=True)
            self.assertTrue(set(self.legacy).issubset(server.assets)); self.assertNotIn('delete-old', server.events)
            self.assertNotIn(public_assets.MARKER, server.release['body'])

    def test_partial_cleanup_resumes_only_the_same_new_cohort(self):
        server = self.server(); server.fail_delete_after = 3
        with self.assertRaises(subprocess.CalledProcessError): self.publish(server, replace_legacy_v410=True)
        self.assertEqual(len(set(server.assets) & set(self.legacy)), 12)
        self.assertTrue(set(self.names).issubset(server.assets))
        with self.assertRaisesRegex(ValueError, 'original build run'): self.guard(server)
        before = copy.deepcopy(server.assets); server.commands.clear()
        clean_name = self.names[0]; server.assets[clean_name]['digest'] = 'sha256:' + '0' * 64
        with self.assertRaises(ValueError): self.publish(server, replace_legacy_v410=True)
        self.assertEqual(server.commands, []); server.assets = before
        self.publish(server, replace_legacy_v410=True)
        self.assertFalse(any(command[:3] == ['gh', 'release', 'upload'] for command in server.commands))
        self.assertEqual(len([command for command in server.commands if command[:2] == ['gh', 'api']]), 12)
        self.assertEqual(set(server.assets), set(self.names))

    def test_missing_old_and_missing_new_files_cannot_start_cleanup(self):
        server = self.server(); del server.assets[next(iter(self.legacy))]
        with self.assertRaisesRegex(ValueError, 'incomplete legacy cohort'): self.publish(server, replace_legacy_v410=True)
        self.assertEqual(server.commands, [])

    def test_final_marker_failure_resumes_without_uploading_or_deleting_again(self):
        server = self.server(); server.fail_edit_once = True
        with self.assertRaises(subprocess.CalledProcessError): self.publish(server, replace_legacy_v410=True)
        self.assertEqual(set(server.assets), set(self.names)); self.assertNotIn(public_assets.MARKER, server.release['body'])
        server.commands.clear(); self.publish(server, replace_legacy_v410=True)
        self.assertEqual([command[2] for command in server.commands], ['edit'])
        self.assertEqual(public_assets.parse_publication(server.release['body'], self.fixture_tag)['buildNumber'], '36')


if __name__ == '__main__':
    unittest.main()
