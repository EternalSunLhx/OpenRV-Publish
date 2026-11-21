"""Offline release-state tests; all GitHub reads and gh writes are simulated."""
from dataclasses import asdict, replace
import copy
import hashlib
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


class ReleaseServer:
    def __init__(self, owner, *, exists=False, draft=True, records=()):
        self.owner = owner
        self.exists = exists
        self.release = {'id': 123, 'tag_name': owner.release.tag, 'draft': draft, 'prerelease': False,
                        'html_url': f'https://github.com/{workspace.TARGET}/releases/tag/{owner.release.tag}',
                        'name': owner.title, 'body': owner.notes}
        self.assets = {record['name']: copy.deepcopy(record) for record in records}
        self.events = []
        self.commands = []
        self.asset_reads = 0
        self.source_reads = 0
        self.tag_reads = 0
        self.uploads = 0
        self.fail_upload = 0
        self.final_asset_change = None
        self.source_changed = False
        self.tag_changed = False
        self.latest = None

    def request(self, method, repository, suffix, payload=None):
        if (method, repository, payload) != ('GET', workspace.TARGET, None):
            raise AssertionError('Only fixed repository GETs may use OfficialApi')
        if suffix == 'releases/tags/' + self.owner.release.tag:
            self.events.append('read-release')
            return (200, copy.deepcopy(self.release)) if self.exists else (404, {})
        if suffix == 'releases/123/assets?per_page=100&page=1':
            self.events.append('read-assets')
            self.asset_reads += 1
            records = copy.deepcopy(list(self.assets.values()))
            if self.asset_reads >= 2 and self.final_asset_change:
                self.final_asset_change(records)
            return 200, records
        if suffix == 'releases/latest':
            self.events.append('read-latest')
            if self.latest:
                return 200, copy.deepcopy(self.latest)
            if self.exists and self.release['draft'] is False:
                return 200, copy.deepcopy(self.release)
            return 404, {}
        raise AssertionError(suffix)

    def official_release(self, api, tag):
        self.events.append('verify-upstream')
        self.source_reads += 1
        if self.source_changed and self.source_reads > 1:
            return replace(self.owner.release, commit_sha='b' * 40)
        return self.owner.release

    def verify_publication_tag(self, api, release, publication_commit):
        self.events.append('verify-tag')
        self.tag_reads += 1
        assert release == self.owner.release and publication_commit == 'c' * 40
        if self.tag_changed and self.tag_reads > 1:
            raise ValueError('Publication tag changed before release publication')
        return {'object': {'sha': 'd' * 40, 'type': 'tag'}}

    def gh(self, command, *, check, timeout):
        self.commands.append(command)
        assert command[:2] == ['gh', 'release'] and check is True and timeout == 1800
        operation = command[2]
        if operation == 'create':
            self.events.append('create-draft')
            assert '--draft' in command and '--verify-tag' in command and '--latest=false' in command
            assert not any(str(self.owner.assets / name) in command for name in self.owner.names)
            self.exists = True
        elif operation == 'upload':
            self.uploads += 1
            paths = command[6:]
            assert paths and '--clobber' not in command
            manifests = all(path.endswith('.build.json') for path in paths)
            assert manifests or all(not path.endswith('.build.json') for path in paths)
            self.events.append('upload-manifests' if manifests else 'upload-other')
            for path in paths:
                name = Path(path).name
                assert name not in self.assets
                self.assets[name] = self.owner.record(name)
                if self.fail_upload == self.uploads:
                    raise subprocess.CalledProcessError(1, command)
        elif operation == 'edit':
            self.events.append('publish')
            assert '--draft=false' in command and '--prerelease=false' in command and '--verify-tag' in command
            assert command[command.index('--tag') + 1] == self.owner.release.tag
            self.release.update(draft=False, prerelease=False,
                                name=command[command.index('--title') + 1], body=self.owner.notes)
        else:
            raise AssertionError(operation)


class PublishReleaseTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.assets = Path(temporary.name)
        base = 'OpenRV-4.0.2-build.29'
        self.names = [base + '-source.provenance.json', base + '-generated-localization.patch.zip', base + '-source.sha256.txt']
        for platform, suffixes in (
                ('Windows-x64', ('-Setup.exe', '-Portable.zip', '.build.json', '.sha256.txt')),
                ('macOS-x86_64', ('.dmg', '.zip', '.build.json', '.sha256.txt')),
                ('macOS-arm64', ('.dmg', '.zip', '.build.json', '.sha256.txt'))):
            self.names.extend(base + '-' + platform + suffix for suffix in suffixes)
        for name in self.names:
            (self.assets / name).write_bytes(('Offline fixture ' + name).encode())
        self.notes = 'Simple download links and install instructions.\n'
        (self.assets / 'RELEASE-NOTES.md').write_text(self.notes, encoding='utf-8')
        self.title = 'OpenRV v4.0.2 中英文版'
        self.release = workspace.watcher().Release(456, 'v4.0.2', 'a' * 40, 'a' * 40, '2026-08-20T17:15:15Z')
        self.plan = {'official': asdict(self.release), 'build_number': 29, 'publication_commit': 'c' * 40}

    def record(self, name):
        data = (self.assets / name).read_bytes()
        return {'name': name, 'state': 'uploaded', 'size': len(data),
                'digest': 'sha256:' + hashlib.sha256(data).hexdigest(),
                'browser_download_url': f'https://github.com/{workspace.TARGET}/releases/download/v4.0.2/{name}'}

    def publish(self, server):
        with mock.patch.object(workspace, 'official_release', side_effect=server.official_release), \
                mock.patch.object(publication_tags, 'verify_publication_tag', side_effect=server.verify_publication_tag), \
                mock.patch.object(prepare_release.subprocess, 'run', side_effect=server.gh):
            prepare_release.publish_assets(self.assets, self.assets, self.plan, server)

    def test_new_release_orders_draft_uploads_remote_verification_then_publication(self):
        server = ReleaseServer(self)
        self.publish(server)
        self.assertEqual([command[2] for command in server.commands], ['create', 'upload', 'upload', 'edit'])
        events = server.events
        self.assertLess(events.index('create-draft'), events.index('upload-other'))
        self.assertLess(events.index('upload-other'), events.index('upload-manifests'))
        last_read = max(index for index, event in enumerate(events) if event == 'read-assets')
        self.assertGreater(last_read, events.index('upload-manifests'))
        self.assertGreater(events.index('publish'), last_read)
        self.assertEqual(events[events.index('publish') - 2:events.index('publish')], ['verify-upstream', 'verify-tag'])
        self.assertEqual(len(server.assets), 15)
        self.assertIn('--latest=true', server.commands[-1])
        self.assertFalse(server.release['draft'])
        self.assertFalse(server.release['prerelease'])
        self.assertEqual(server.release['name'], self.title)
        self.assertEqual(server.release['body'], self.notes)

    def test_interrupted_upload_stays_draft_and_resumes_only_missing_files(self):
        server = ReleaseServer(self)
        server.fail_upload = 1
        with self.assertRaises(subprocess.CalledProcessError):
            self.publish(server)
        self.assertTrue(server.release['draft'])
        self.assertNotIn('publish', server.events)
        retained = set(server.assets)
        server.fail_upload = 0
        server.commands.clear()
        self.publish(server)
        uploaded = {Path(path).name for command in server.commands if command[2] == 'upload' for path in command[6:]}
        self.assertEqual(uploaded, set(self.names) - retained)
        self.assertNotIn('create', [command[2] for command in server.commands])

    def test_remote_post_upload_corruption_or_missing_file_never_publishes(self):
        for changes in ({'digest': 'sha256:' + '0' * 64}, {'digest': None}, {'size': 0},
                        {'state': 'new'}, {'browser_download_url': 'https://foreign.invalid/asset'}, None):
            server = ReleaseServer(self)
            server.final_asset_change = lambda records, changes=changes: records.pop() if changes is None else records[0].update(changes)
            with self.subTest(changes=changes), self.assertRaisesRegex(ValueError, 'not be overwritten or published'):
                self.publish(server)
            self.assertTrue(server.release['draft'])
            self.assertNotIn('publish', server.events)

    def test_conflicting_existing_assets_are_never_overwritten(self):
        for changes in ({'digest': None}, {'digest': 'sha256:' + '0' * 64}, {'size': 0},
                        {'state': 'new'}, {'browser_download_url': 'https://foreign.invalid/asset'}):
            server = ReleaseServer(self, exists=True, records=[{**self.record(self.names[0]), **changes}])
            with self.subTest(changes=changes), self.assertRaisesRegex(ValueError, 'not be overwritten'):
                self.publish(server)
            self.assertEqual(server.commands, [])

    def test_completed_exact_resume_has_no_upload_or_release_edit(self):
        server = ReleaseServer(self, exists=True, draft=False, records=[self.record(name) for name in self.names])
        self.publish(server)
        self.assertEqual(server.commands, [])
        self.assertEqual(server.asset_reads, 2)
        self.assertEqual(server.source_reads, 2)

    def test_older_build_keeps_existing_stable_notes_and_latest_state(self):
        name = 'OpenRV-4.0.2-build.30-macOS-arm64.build.json'
        higher = {**self.record(self.names[0]), 'name': name,
                  'browser_download_url': f'https://github.com/{workspace.TARGET}/releases/download/v4.0.2/{name}'}
        server = ReleaseServer(self, exists=True, draft=False, records=[higher])
        server.release['body'] = 'Newer build download links must remain.'
        self.publish(server)
        self.assertTrue(server.commands)
        self.assertTrue(all(command[2] == 'upload' for command in server.commands))
        self.assertEqual(server.release['body'], 'Newer build download links must remain.')
        self.assertNotIn('read-latest', server.events)

    def test_manual_old_tag_cannot_replace_a_newer_latest_release(self):
        workflow = (Path(__file__).resolve().parents[3] / '.github/workflows/localized-release.yml').read_text(encoding='utf-8')
        publisher = workflow.split('\n  draft:', 1)[1]
        self.assertIn('group: openrv-official-release-upload\n', publisher)
        self.assertNotIn('group: official-release-upload-${{', publisher)
        server = ReleaseServer(self)
        server.latest = {**server.release, 'id': 124, 'tag_name': 'v4.1.0', 'draft': False,
                         'html_url': f'https://github.com/{workspace.TARGET}/releases/tag/v4.1.0'}
        self.publish(server)
        self.assertIn('--latest=false', server.commands[-1])

    def test_upstream_or_mirror_change_after_upload_prevents_publication(self):
        for field in ('source_changed', 'tag_changed'):
            server = ReleaseServer(self)
            setattr(server, field, True)
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, 'changed before release publication'):
                self.publish(server)
            self.assertEqual(len(server.assets), 15)
            self.assertNotIn('publish', server.events)
            self.assertTrue(server.release['draft'])

    def test_current_latest_identity_error_is_not_treated_as_no_release(self):
        server = ReleaseServer(self)
        server.latest = {**server.release, 'id': 124, 'draft': False}
        with self.assertRaisesRegex(ValueError, 'latest stable release'):
            self.publish(server)
        self.assertNotIn('publish', server.events)

    def test_incomplete_local_cohort_is_rejected_before_creating_a_release(self):
        (self.assets / self.names[0]).unlink()
        server = ReleaseServer(self)
        with self.assertRaisesRegex(ValueError, 'exact fifteen assets'):
            self.publish(server)
        self.assertEqual(server.commands, [])


if __name__ == '__main__':
    unittest.main()
