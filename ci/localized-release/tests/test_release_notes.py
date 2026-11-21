"""Download notes keep internal build provenance out of the visible release copy."""
from __future__ import annotations

from pathlib import Path
import re
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from assemble_release import release_notes


class ReleaseNotesTests(unittest.TestCase):
    def fixture(self):
        source = {'releaseVersion': '4.1.0-build.29', 'displayVersion': '4.1.0 build 29',
                  'sourceCommit': 'a' * 40, 'buildNumber': 29}
        records = []
        for platform, architecture, minimum in (('macOS', 'arm64', '15.3'),
                                                 ('Windows', 'x64', None),
                                                 ('macOS', 'x86_64', '13.7')):
            base = f"OpenRV-{source['releaseVersion']}-{platform}-{architecture}"
            record = {'platform': platform, 'architecture': architecture,
                      'files': ([base + '-Portable.zip', base + '-Setup.exe'] if platform == 'Windows'
                                else [base + '.zip', base + '.dmg'])}
            if platform == 'macOS':
                record.update(minimumMacOS=minimum, signature='ad-hoc', codeSigned=False, notarized=False)
            records.append(record)
        return source, records

    def test_all_downloads_use_the_actual_complete_package_names_and_official_tag(self):
        source, records = self.fixture()
        notes = release_notes(source, records, 'v4.1.0')
        links = re.findall(r'\]\(([^)]+)\)', notes)
        prefix = 'https://github.com/EternalSunLhx/OpenRV-Publish/releases/download/v4.1.0/'
        names = {name for record in records for name in record['files']}
        names.update(f"OpenRV-{source['releaseVersion']}-{record['platform']}-{record['architecture']}.sha256.txt"
                     for record in records)
        self.assertEqual(len(links), 10)
        self.assertEqual(set(links), {prefix + name for name in names} | {'https://support.apple.com/zh-cn/102445'})
        self.assertLess(notes.index('| Windows x64'), notes.index('| macOS Intel'))
        self.assertLess(notes.index('| macOS Intel'), notes.index('| macOS Apple Silicon'))

    def test_visible_copy_uses_official_version_and_actual_mac_requirements(self):
        source, records = self.fixture()
        notes = release_notes(source, records, 'v4.1.0')
        visible = re.sub(r'\]\([^)]*\)', ']', notes)
        self.assertTrue(visible.startswith('OpenRV v4.1.0 中英文版\n'))
        self.assertNotIn('build', visible.lower())
        self.assertNotIn(source['sourceCommit'], notes)
        for private_detail in ('Runtime validation', 'provenance', 'generated-localization', 'CFPreferences'):
            self.assertNotIn(private_detail, notes)
        self.assertRegex(notes, r'\| macOS Intel \|[^\n]+\| macOS 13\.7 及以上 \|')
        self.assertRegex(notes, r'\| macOS Apple Silicon \|[^\n]+\| macOS 15\.3 及以上 \|')
        for instruction in ('所有用户', '管理员权限', '打开方式', '不会更改现有默认播放器',
                            'Language / 语言', 'English', '重启软件', 'ad-hoc', '未经过 Apple 公证',
                            'OpenRV.app', '隐私与安全性', '仍要打开'):
            self.assertIn(instruction, notes)

    def test_missing_requirements_and_different_signatures_are_not_misrepresented(self):
        source, records = self.fixture()
        apple = records[0]
        apple.pop('minimumMacOS')
        apple.update(signature='Developer ID', codeSigned=True, notarized=True)
        notes = release_notes(source, records, 'v4.1.0')
        self.assertRegex(notes, r'\| macOS Apple Silicon \|[^\n]+\| 最低系统版本未记录 \|')
        self.assertIn('macOS Intel：临时签名（ad-hoc），未经过 Apple 公证', notes)
        self.assertIn('macOS Apple Silicon：开发者签名，已通过 Apple 公证', notes)
        self.assertNotIn('macOS 15.3', notes)


if __name__ == '__main__':
    unittest.main()
