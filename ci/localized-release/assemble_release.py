"""Validate three-platform assets and prepare concise download instructions."""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import zipfile
from urllib.parse import quote

from release_common import sha256, source_metadata, validate_release_tag, write_checksums


def write_localization_archive(path: pathlib.Path, delta: bytes) -> None:
    # A ZIP transports an empty patch without requiring a zero-byte release asset.
    entry = zipfile.ZipInfo('generated-localization.patch', date_time=(1980, 1, 1, 0, 0, 0))
    entry.create_system = 3
    entry.external_attr = 0o100644 << 16
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr(entry, delta, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)


def release_notes(source: dict, records: list[dict], tag: str) -> str:
    """Use verified package names and platform requirements in download links."""
    from official_workspace import TARGET

    download = f'https://github.com/{TARGET}/releases/download/{quote(tag, safe="")}/'

    def link(label: str, name: str) -> str:
        return f'[{label}]({download}{quote(name, safe="")})'

    rows = []
    mac_signatures = []
    order = {('Windows', 'x64'): 0, ('macOS', 'x86_64'): 1, ('macOS', 'arm64'): 2}
    for record in sorted(records, key=lambda value: order[(value['platform'], value['architecture'])]):
        platform, architecture = record['platform'], record['architecture']
        files = {pathlib.Path(name).suffix: name for name in record['files']}
        checksum = f"OpenRV-{source['releaseVersion']}-{platform}-{architecture}.sha256.txt"
        if platform == 'Windows':
            label = 'Windows x64'
            downloads = link('安装版 EXE', files['.exe']) + ' · ' + link('便携版 ZIP', files['.zip'])
            requirement = '64 位 Windows'
        else:
            label = 'macOS Intel' if architecture == 'x86_64' else 'macOS Apple Silicon'
            downloads = link('DMG', files['.dmg']) + ' · ' + link('ZIP', files['.zip'])
            minimum = record.get('minimumMacOS')
            requirement = f'macOS {minimum} 及以上' if minimum else '最低系统版本未记录'
            signature = ('临时签名（ad-hoc）' if record.get('signature') == 'ad-hoc'
                         else '开发者签名' if record.get('codeSigned') is True else '签名状态未记录')
            notarization = ('已通过 Apple 公证' if record.get('notarized') is True
                            else '未经过 Apple 公证' if record.get('notarized') is False else '公证状态未记录')
            mac_signatures.append((label, signature + '，' + notarization))
        rows.append(f'| {label} | {downloads} | {requirement} | {link("SHA-256", checksum)} |')
    table = '\n'.join(rows)
    if len({status for _, status in mac_signatures}) == 1:
        signing = 'macOS 软件包使用' + mac_signatures[0][1] + '。'
    else:
        signing = '；'.join(label + '：' + status for label, status in mac_signatures) + '。'
    return f'''OpenRV {tag} 中英文版

基于官方 OpenRV 发布源码的社区版本，支持简体中文和 English。

| 平台 | 下载 | 系统要求 | 文件校验 |
| --- | --- | --- | --- |
{table}

- Windows 安装版为所有用户安装，需要管理员权限；安装后可在视频文件的“打开方式”中选择 OpenRV，不会更改现有默认播放器。便携版解压后即可使用。
- macOS 请按处理器类型下载，将 RV.app 拖入“应用程序”。
- 在“Language / 语言”菜单选择简体中文或 English，重启软件后生效。

Windows 安装包未使用发布者证书签名。{signing}系统可能显示信任提示。
'''


def assemble(repo: pathlib.Path, assets: pathlib.Path, tag: str) -> None:
    validate_release_tag(repo, tag)
    source = source_metadata(repo)
    expected_platforms = {('Windows', 'x64'), ('macOS', 'x86_64'), ('macOS', 'arm64')}
    manifests = sorted(assets.glob('OpenRV-*.build.json'))
    if len(manifests) != 3:
        raise ValueError('Exactly three native platform manifests are required')
    listed_files = set()
    platforms = set()
    records = []
    for path in manifests:
        manifest = json.loads(path.read_text(encoding='utf-8-sig'))
        identity = manifest['platform'], manifest['architecture']
        if identity not in expected_platforms or identity in platforms:
            raise ValueError(f'Unexpected/duplicate platform: {identity}')
        platforms.add(identity)
        if manifest['sourceCommit'] != source['sourceCommit'] or manifest['version'] != source['version']:
            raise ValueError('Platform builds do not identify the selected source commit/version')
        for key in ('nativeVersion', 'upstreamRelease', 'officialSourceTree', 'localizationSnapshot',
                    'localizationPatchBase', 'localizationPatchSha256', 'omittedUpstreamComponents',
                    'composedSourceCommit', 'composedSourceTree', 'buildNumber', 'displayVersion',
                    'upstreamSourceTree', 'patchedSourceTree', 'generatedLocalizationPatchSha256',
                    'publicationCommit', 'sourcePlanSchema', 'localizationPatchKind'):
            if manifest.get(key) != source.get(key):
                raise ValueError('Platform source/native-version provenance differs: ' + key)
        if manifest['releaseVersion'] != source['releaseVersion'] or manifest.get('releaseTag') != tag:
            raise ValueError('Platform release version does not match the requested tag')
        expected_ext = {'.exe', '.zip'} if identity[0] == 'Windows' else {'.dmg', '.zip'}
        binary_names = manifest['files']
        if len(binary_names) != 2 or {pathlib.Path(name).suffix for name in binary_names} != expected_ext:
            raise ValueError(f'Incomplete platform packages for {identity}')
        base = f"OpenRV-{source['releaseVersion']}-{identity[0]}-{identity[1]}"
        expected_packages = ({base + '-Setup.exe', base + '-Portable.zip'} if identity[0] == 'Windows'
                             else {base + '.dmg', base + '.zip'})
        if set(binary_names) != expected_packages or path.name != base + '.build.json':
            raise ValueError(f'Package names do not match the version/platform specification: {identity}')
        aac_steps = [step for step in manifest['smoke'] if step.get('name') == 'aac-mp4']
        if len(aac_steps) != 1 or aac_steps[0].get('status') != 'passed':
            raise ValueError(f'Required AAC MP4 audio/video decode did not pass on {identity}')
        if any(step['status'] != 'passed' for step in manifest['smoke'] if not step['name'].startswith('gui-')):
            raise ValueError(f'Required runtime smoke failed on {identity}')
        if identity[0] == 'Windows' and (manifest.get('installerSmoke', {}).get('status') != 'passed'
                                       or not manifest['installerSmoke'].get('removed')):
            raise ValueError('Windows installer install/uninstall lifecycle did not pass')
        if identity[0] == 'Windows' and manifest.get('languagePersistence', {}).get('status') != 'passed':
            raise ValueError('Windows native language chooser persistence did not pass')
        if identity[0] == 'macOS':
            persistence = manifest.get('languagePersistence', {})
            if (persistence.get('status') != 'passed' or len(persistence.get('launches', [])) != 3
                    or any(persistence.get('preferenceRecovery', {}).get(operation) != 'passed'
                           for operation in ('restore', 'verify'))
                    or any(launch.get('status') != 'passed'
                           or launch.get('hostedRunnerPreferencesVerifiedBeforeAction') is not True
                           or launch.get('nativeMenuRebuilds') != 3 for launch in persistence['launches'])):
                raise ValueError('Mac native chooser persistence or hosted preference restoration did not pass')
        checksum_name = path.name.removesuffix('.build.json') + '.sha256.txt'
        checksum_path = assets / checksum_name
        checksums = {}
        for line in checksum_path.read_text(encoding='ascii').splitlines():
            match = re.fullmatch(r'([0-9a-f]{64})  (OpenRV-[A-Za-z0-9_.-]+)', line)
            if not match or match.group(2) in checksums:
                raise ValueError(f'Invalid/duplicate checksum record: {line}')
            checksums[match.group(2)] = match.group(1)
        expected_names = set(binary_names + [path.name])
        if set(checksums) != expected_names:
            raise ValueError(f'Checksum manifest does not cover exact package set: {identity}')
        for name, expected_hash in checksums.items():
            file = assets / name
            if file.name != name or file.is_symlink() or not file.is_file() or sha256(file) != expected_hash:
                raise ValueError(f'Asset checksum/ownership check failed: {name}')
        listed_files.update(expected_names | {checksum_name})
        records.append(manifest)
    if platforms != expected_platforms:
        raise ValueError('All three platforms must pass before a release can be assembled')
    actual_files = {path.name for path in assets.iterdir()}
    if actual_files != listed_files:
        raise ValueError(f'Unexpected files in release assets: {actual_files - listed_files}')
    notes = release_notes(source, records, tag)
    if 'buildNumber' in source:
        from official_workspace import PLAN, DELTA, verify_materialized, watcher
        verify_materialized(repo, expected_build=source['buildNumber'])
        prefix = 'OpenRV-' + source['releaseVersion']
        proof = prefix + '-source.provenance.json'
        patch = prefix + '-generated-localization.patch.zip'
        (assets / proof).write_bytes(watcher().safe_file(repo, PLAN).read_bytes())
        write_localization_archive(assets / patch, watcher().safe_file(repo, DELTA).read_bytes())
        write_checksums(assets, [proof, patch], prefix + '-source.sha256.txt')
    (assets / 'RELEASE-NOTES.md').write_text(notes, encoding='utf-8')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--repo', type=pathlib.Path, default=pathlib.Path.cwd())
    parser.add_argument('--assets', type=pathlib.Path, required=True)
    parser.add_argument('--tag', required=True)
    args = parser.parse_args()
    assemble(args.repo.resolve(), args.assets.resolve(), args.tag)
