"""Project validated internal build artifacts into stable public download names."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil

from official_workspace import TARGET, build_number, checked_tag
from release_common import sha256

MARKER = 'openrv-publication:'
IDENTIFIER = MARKER.removesuffix(':')
PLATFORMS = {
    'Windows-x64': ('-Setup.exe', '-Portable.zip'),
    'macOS-x86_64': ('.dmg', '.zip'),
    'macOS-arm64': ('.dmg', '.zip'),
}
LEGACY_TAG = 'v4.1.0'
LEGACY_BUILD = 35
LEGACY_RELEASE_ID = 453727799


def legacy_migration_enabled():
    value = os.environ.get('ORV_REPLACE_LEGACY_V410', 'false')
    if value not in ('true', 'false'):
        raise ValueError('ORV_REPLACE_LEGACY_V410 must be explicitly true or false')
    return value == 'true'


def legacy_inventory(tag, number):
    """This is a one-time, reviewed migration, never a generic delete policy."""
    if tag != LEGACY_TAG or build_number(number) <= LEGACY_BUILD:
        raise ValueError('Legacy replacement requires v4.1.0 and a build newer than 35')
    path = Path(__file__).with_name('legacy-v4.1.0-build35-assets.json')
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 16384:
        raise ValueError('Missing or invalid reviewed legacy asset inventory')
    inventory = json.loads(path.read_text(encoding='utf-8'))
    if (not isinstance(inventory, dict) or set(inventory) != {'schema', 'repository', 'releaseId', 'tag', 'buildNumber', 'assets'}
            or type(inventory['schema']) is not int or inventory['schema'] != 1 or inventory['repository'] != TARGET
            or type(inventory['releaseId']) is not int or inventory['releaseId'] != LEGACY_RELEASE_ID
            or inventory['tag'] != LEGACY_TAG or type(inventory['buildNumber']) is not int
            or inventory['buildNumber'] != LEGACY_BUILD or not isinstance(inventory['assets'], list)):
        raise ValueError('Legacy inventory identity is not the reviewed v4.1.0 build35 release')
    prefix = 'OpenRV-4.1.0'
    names = {name.replace(prefix, prefix + '-build.35', 1) for name in public_names(LEGACY_TAG)}
    names.update(prefix + '-build.35-' + platform + '.build.json' for platform in PLATFORMS)
    records, identities = {}, set()
    for record in inventory['assets']:
        if (not isinstance(record, dict) or set(record) != {'id', 'name', 'size', 'digest'}
                or not isinstance(record['name'], str) or record['name'] not in names or record['name'] in records
                or type(record['id']) is not int or record['id'] <= 0 or record['id'] in identities
                or type(record['size']) is not int or not 0 < record['size'] <= 64 * 1024**3
                or not isinstance(record['digest'], str) or not re.fullmatch(r'sha256:[0-9a-f]{64}', record['digest'])):
            raise ValueError('Invalid or duplicate asset in reviewed legacy inventory')
        records[record['name']] = record
        identities.add(record['id'])
    if set(records) != names:
        raise ValueError('Legacy inventory must contain the exact fifteen build35 files')
    return records


def verify_legacy_release(release):
    if (release.get('id') != LEGACY_RELEASE_ID or release.get('tag_name') != LEGACY_TAG
            or release.get('draft') is not False or release.get('prerelease') is not False
            or release.get('html_url') != f'https://github.com/{TARGET}/releases/tag/{LEGACY_TAG}'):
        raise ValueError('Legacy migration is bound to the reviewed published release ID')


def verify_legacy_asset(name, record, expected):
    if (not isinstance(record, dict) or any(record.get(key) != expected[key] for key in ('id', 'name', 'size', 'digest'))
            or type(record.get('id')) is not int or type(record.get('size')) is not int
            or record.get('state') != 'uploaded'
            or record.get('browser_download_url') != f'https://github.com/{TARGET}/releases/download/{LEGACY_TAG}/{name}'):
        raise ValueError('Legacy asset differs from the reviewed ID and content inventory: ' + name)


def public_names(tag):
    prefix = 'OpenRV-' + checked_tag(tag)[1:]
    names = {prefix + '-source.provenance.json', prefix + '-generated-localization.patch.zip', prefix + '-source.sha256.txt'}
    for platform, suffixes in PLATFORMS.items():
        names.update(prefix + '-' + platform + suffix for suffix in (*suffixes, '.sha256.txt'))
    return names


def parse_publication(body, tag):
    """An absent legacy marker is distinct from a present malformed marker."""
    if body is None:
        return None
    if not isinstance(body, str):
        raise ValueError('Release body must be text')
    occurrences = body.lower().count(IDENTIFIER)
    if not occurrences:
        return None
    matches = re.findall(r'(?:\A|\n)<!-- openrv-publication: ([^\r\n]+) -->(?=\r?\n|\Z)', body)
    if occurrences != 1 or len(matches) != 1 or len(matches[0].encode('utf-8')) > 16384:
        raise ValueError('Release publication marker must occur exactly once on one line')
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('Duplicate publication metadata key')
            result[key] = value
        return result
    record = json.loads(matches[0], object_pairs_hook=unique)
    if (not isinstance(record, dict) or set(record) != {'schema', 'tag', 'buildNumber', 'assets'}
            or type(record['schema']) not in (int, float) or record['schema'] != 1 or record['tag'] != checked_tag(tag)
            or not isinstance(record['buildNumber'], str) or not isinstance(record['assets'], dict)
            or set(record['assets']) != public_names(tag)
            or any(not isinstance(value, str) or not re.fullmatch(r'sha256:[0-9a-f]{64}', value)
                   for value in record['assets'].values())):
        raise ValueError('Invalid publication metadata identity or twelve-asset inventory')
    build_number(record['buildNumber'])
    return record


def describe(path):
    size = path.stat().st_size
    if not 0 < size <= 64 * 1024**3:
        raise ValueError('Public asset size is outside supported bounds: ' + path.name)
    return {'size': size, 'digest': 'sha256:' + sha256(path)}


def guard_published_release(api, tag, *, number=None, replace_legacy_v410=False):
    """Refuse a historical target before spending hours on native builds."""
    tag = checked_tag(tag)
    if type(replace_legacy_v410) is not bool:
        raise ValueError('Legacy replacement permission must be an explicit boolean')
    legacy = legacy_inventory(tag, number) if replace_legacy_v410 else None
    status, release = api.request('GET', TARGET, 'releases/tags/' + tag)
    if status == 404:
        return
    if (status != 200 or not isinstance(release, dict) or release.get('tag_name') != tag
            or type(release.get('id')) is not int or release['id'] <= 0
            or release.get('draft') is not False or release.get('prerelease') is not False
            or release.get('html_url') != f'https://github.com/{TARGET}/releases/tag/{tag}'
            or not isinstance(release.get('assets'), list)):
        raise ValueError('Cannot verify the existing published release before building')
    marker = parse_publication(release.get('body'), tag)
    assets = release['assets']
    if not assets and marker is None:
        return
    if marker is None:
        if legacy is not None:
            verify_legacy_release(release)
            if (any(not isinstance(asset, dict) or not isinstance(asset.get('name'), str) for asset in assets)
                    or len(assets) != len(legacy) or {asset['name'] for asset in assets} != set(legacy)):
                raise ValueError('Legacy migration is already partial; resume publication from the original build run')
            for asset in assets:
                verify_legacy_asset(asset['name'], asset, legacy[asset['name']])
            return
        raise ValueError('Historical release assets are retained unchanged; select the next official version instead')
    names = set()
    for asset in assets:
        if not isinstance(asset, dict):
            raise ValueError('Invalid published asset before building')
        name = asset.get('name')
        if (not isinstance(name, str) or name not in marker['assets'] or name in names or asset.get('state') != 'uploaded'
                or type(asset.get('size')) is not int or not 0 < asset['size'] <= 64 * 1024**3
                or asset.get('digest') != marker['assets'][name]
                or asset.get('browser_download_url') != f'https://github.com/{TARGET}/releases/download/{tag}/{name}'):
            raise ValueError('Existing clean publication is incomplete or differs from its marker; recover it before rebuilding')
        names.add(name)
    if names != public_names(tag):
        raise ValueError('Existing clean publication is missing verified assets; recover it before rebuilding')


def project_assets(original: Path, destination: Path, tag: str, number):
    """Keep original fifteen files intact; rewrite only four public checksum files."""
    tag = checked_tag(tag)
    number = build_number(number)
    old_prefix = f'OpenRV-{tag[1:]}-build.{number}'
    new_prefix = 'OpenRV-' + tag[1:]
    required = {old_prefix + '-source.provenance.json', old_prefix + '-generated-localization.patch.zip',
                old_prefix + '-source.sha256.txt'}
    groups = {old_prefix + '-source.sha256.txt': [old_prefix + '-source.provenance.json',
                                                old_prefix + '-generated-localization.patch.zip']}
    for platform, suffixes in PLATFORMS.items():
        base = old_prefix + '-' + platform
        names = [base + suffix for suffix in (*suffixes, '.build.json')]
        required.update([*names, base + '.sha256.txt'])
        groups[base + '.sha256.txt'] = names
    files = list(original.iterdir())
    if (set(path.name for path in files) != required | {'RELEASE-NOTES.md'}
            or any(path.is_symlink() or not path.is_file() for path in files)):
        raise ValueError('Publication requires the exact fifteen assets and notes of the verified build')
    records = {name: describe(original / name) for name in required}
    for checksum, names in groups.items():
        expected_lines = {records[name]['digest'].removeprefix('sha256:') + '  ' + name for name in names}
        lines = (original / checksum).read_text(encoding='ascii').splitlines()
        if len(lines) != len(names) or set(lines) != expected_lines:
            raise ValueError('Original cohort checksum changed after assembly: ' + checksum)
    notes = (original / 'RELEASE-NOTES.md').read_text(encoding='utf-8')
    if IDENTIFIER in notes.lower():
        raise ValueError('Original assembly notes must not already contain publication metadata')
    destination.mkdir(exist_ok=False)
    mapping = {}
    expected = {}
    for old_name in sorted(required):
        if old_name.endswith(('.build.json', '.sha256.txt')):
            continue
        name = old_name.replace(old_prefix, new_prefix, 1)
        target = destination / name
        try:
            os.link(original / old_name, target)
        except OSError:
            shutil.copyfile(original / old_name, target)
        mapping[old_name] = name
        expected[name] = records[old_name]
    for old_checksum, old_names in groups.items():
        names = [mapping[name] for name in old_names if not name.endswith('.build.json')]
        checksum = old_checksum.replace(old_prefix, new_prefix, 1)
        target = destination / checksum
        target.write_text(''.join(expected[name]['digest'].removeprefix('sha256:') + '  ' + name + '\n'
                                  for name in names), encoding='ascii')
        expected[checksum] = describe(target)
        mapping[old_checksum] = checksum
    if set(expected) != public_names(tag):
        raise ValueError('Public projection did not produce the exact twelve files')
    notes = notes.replace(old_prefix, new_prefix)
    marker = {'schema': 1, 'tag': tag, 'buildNumber': str(number),
              'assets': {name: value['digest'] for name, value in sorted(expected.items())}}
    pending_notes = destination / 'PENDING-NOTES.md'
    pending_notes.write_text(notes, encoding='utf-8')
    public_notes = destination / 'PUBLIC-NOTES.md'
    public_notes.write_text(notes.rstrip() + '\n\n<!-- ' + MARKER + ' '
                            + json.dumps(marker, sort_keys=True, separators=(',', ':')) + ' -->\n', encoding='utf-8')
    return {'expected': expected, 'marker': marker, 'mapping': mapping,
            'notes': public_notes, 'pending_notes': pending_notes}
