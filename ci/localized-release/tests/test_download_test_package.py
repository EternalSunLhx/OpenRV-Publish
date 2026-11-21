"""Exercise published package identity, byte verification and archive boundaries."""
from __future__ import annotations
import hashlib
import io
import json
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest import mock
import zipfile
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import download_test_package as package
from public_assets import public_names

TAG = 'v4.1.0'


def fixture(clean):
    prefix = 'OpenRV-4.1.0'
    names = public_names(TAG)
    if not clean:
        names = {name.replace(prefix, prefix + '-build.35', 1) for name in names}
        names.update(prefix + '-build.35-' + item + '.build.json' for item in package.PLATFORMS)
    assets = [{'id': index + 1, 'name': name, 'size': 3, 'digest': 'sha256:' + 'a' * 64,
               'state': 'uploaded', 'browser_download_url': f'https://github.com/{package.TARGET}/releases/download/{TAG}/{name}'}
              for index, name in enumerate(sorted(names))]
    marker = {'schema': 1, 'tag': TAG, 'buildNumber': '36', 'assets': {a['name']: a['digest'] for a in assets}}
    return {'id': 123, 'tag_name': TAG, 'draft': False, 'prerelease': False,
            'html_url': f'https://github.com/{package.TARGET}/releases/tag/{TAG}',
            'assets': assets, 'body': '<!-- openrv-publication: ' + json.dumps(marker) + ' -->' if clean else 'Old release'}


class SelectionTests(unittest.TestCase):
    def test_selects_zip_for_every_platform_in_both_complete_layouts(self):
        for clean in (True, False):
            for platform in package.PLATFORMS:
                with self.subTest(clean=clean, platform=platform):
                    chosen = package.select_package(fixture(clean), TAG, platform)
                    self.assertTrue(chosen['asset']['name'].endswith('-Portable.zip' if platform == 'Windows-x64' else '.zip'))
                    self.assertEqual(chosen['buildNumber'], 36 if clean else 35)
                    self.assertEqual(chosen['layout'], 'public-12' if clean else 'legacy-15')

    def test_partial_mixed_wrong_hash_or_identity_never_selects_a_runtime(self):
        for case in ('missing', 'mixed', 'digest', 'url', 'draft', 'foreign-tag', 'duplicate', 'bad-marker'):
            with self.subTest(case=case):
                data = fixture(True)
                if case == 'missing': data['assets'].pop()
                elif case == 'mixed': data['assets'].append(fixture(False)['assets'][0])
                elif case == 'digest': data['assets'][0]['digest'] = 'sha256:' + 'b' * 64
                elif case == 'url': data['assets'][0]['browser_download_url'] = 'https://example.org/package.zip'
                elif case == 'draft': data['draft'] = True
                elif case == 'foreign-tag': data['tag_name'] = 'v4.2.0'
                elif case == 'duplicate': data['assets'].append(data['assets'][0])
                else: data['body'] += '\n<!-- OPENRV-PUBLICATION broken -->'
                with self.assertRaises(ValueError):
                    package.select_package(data, TAG, 'macOS-x86_64')

    def test_legacy_multiple_builds_or_unknown_assets_are_rejected(self):
        for extra in ('OpenRV-4.1.0-build.36-Windows-x64-Setup.exe', 'unknown.zip'):
            data = fixture(False)
            record = dict(data['assets'][0], id=999, name=extra,
                          browser_download_url=f'https://github.com/{package.TARGET}/releases/download/{TAG}/{extra}')
            data['assets'].append(record)
            with self.assertRaises(ValueError):
                package.select_package(data, TAG, 'Windows-x64')


class Response(io.BytesIO):
    def geturl(self):
        return 'https://release-assets.githubusercontent.com/verified-fixture'


class DownloadTests(unittest.TestCase):
    def test_streamed_content_is_hash_checked_without_forwarding_api_credentials(self):
        raw = b'owned package fixture'
        asset = {'browser_download_url': 'https://github.com/owner/repo/package.zip',
                 'size': len(raw), 'digest': 'sha256:' + hashlib.sha256(raw).hexdigest()}
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(package.os.environ, {'GH_TOKEN': 'fixture-only'}):
            destination = Path(directory) / 'package.zip'
            def opener(request, **unused):
                self.assertIsNone(request.get_header('Authorization'))
                return Response(raw)
            package.download_package(asset, destination, opener)
            self.assertEqual(destination.read_bytes(), raw)

    def test_truncated_overlong_or_corrupt_download_fails(self):
        raw = b'owned package fixture'
        asset = {'browser_download_url': 'https://github.com/owner/repo/package.zip',
                 'size': len(raw), 'digest': 'sha256:' + hashlib.sha256(raw).hexdigest()}
        for received in (raw[:-1], raw + b'x', b'x' * len(raw)):
            with self.subTest(received=received), tempfile.TemporaryDirectory() as directory:
                with self.assertRaises(ValueError):
                    package.download_package(asset, Path(directory) / 'package.zip', lambda *a, **k: Response(received))


class ArchiveTests(unittest.TestCase):
    def archive(self, root, members):
        path = root / 'fixture.zip'
        with zipfile.ZipFile(path, 'w') as archive:
            for name, payload, symlink in members:
                info = zipfile.ZipInfo(name)
                info.create_system = 3
                info.external_attr = ((stat.S_IFLNK if symlink else stat.S_IFREG) | 0o755) << 16
                archive.writestr(info, payload)
        return path

    def test_regular_windows_zip_extracts_one_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = self.archive(root, [('OpenRV-test/bin/rv.exe', b'MZ-fixture', False)])
            runtime = package.unpack_package(path, root / 'unpacked', 'Windows-x64')
            self.assertEqual((runtime / 'bin/rv.exe').read_bytes(), b'MZ-fixture')

    def test_mac_framework_internal_alias_is_allowed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.archive(Path(directory), [
                ('OpenRV.app/Contents/Frameworks/QtCore.framework/Versions/A/QtCore', b'fixture', False),
                ('OpenRV.app/Contents/Frameworks/QtCore.framework/Versions/Current', b'A', True)])
            self.assertEqual(package.check_zip(path, 'macOS-x86_64'), 2)

    def test_traversal_external_links_and_writes_through_aliases_are_rejected(self):
        cases = [
            [('../escaped', b'x', False)],
            [('/absolute', b'x', False)],
            [('C:/escaped', b'x', False)],
            [('OpenRV.app/link', b'../../outside', True)],
            [('OpenRV.app/link', b'/Applications', True)],
            [('OpenRV.app/link', b'dir', True), ('OpenRV.app/link/file', b'x', False)],
            [('OpenRV.app/a', b'b', True), ('OpenRV.app/b', b'a', True)],
        ]
        for members in cases:
            with self.subTest(members=members), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                path = self.archive(root, members)
                with self.assertRaises(ValueError):
                    package.unpack_package(path, root / 'unpacked', 'macOS-x86_64')
                self.assertFalse((root / 'unpacked').exists())


if __name__ == '__main__':
    unittest.main()
