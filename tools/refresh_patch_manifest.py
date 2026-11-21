"""Hash staged release resources into the reviewed schema-2 composition manifest.

Stage intended edits first, run this tool, then stage the refreshed manifest.
Only Git-index blobs are read; unstaged edits and checkout line endings are ignored.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess

CONFIG = 'ci/upstream-release-watch/localization-base.json'
UPSTREAM = 'AcademySoftwareFoundation/OpenRV'
DEFAULT_BASE = 'b6f33435afdbafaf4a94d412e0400eef32b16976'
OPTIONAL = 'src/plugins/rv-packages/annotate_beta'
PATCHES = (('patches/openrv-core.patch', None), ('patches/annotate-beta.patch', OPTIONAL))
CATALOG = 'localization/i18n_zh.ts'
CATALOG_TARGET = 'src/lib/app/RvCommon/qrc/translations/i18n_zh.ts'
RUNTIME_PREFIXES = ('ci/', 'tools/', 'tests/', '.github/actions/')


def index_blobs(repo: Path) -> dict[str, tuple[str, bytes]]:
    listing = subprocess.check_output(['git', '-C', str(repo), 'ls-files', '--stage', '-z'])
    records = []
    for entry in listing.split(b'\0'):
        if not entry:
            continue
        header, raw_path = entry.split(b'\t', 1)
        mode, identity, stage = header.decode('ascii').split()
        path = raw_path.decode('utf-8')
        if stage != '0' or mode not in ('100644', '100755') or not re.fullmatch(r'[0-9a-f]{40}', identity):
            raise ValueError('Stage only regular, conflict-free owned files: ' + path)
        records.append((path, mode, identity))
    if not records:
        raise ValueError('Stage intended files with git add before refreshing the manifest')
    raw = subprocess.check_output(['git', '-C', str(repo), 'cat-file', '--batch'],
                                  input=''.join(identity + '\n' for _, _, identity in records).encode('ascii'))
    result, offset = {}, 0
    for path, mode, identity in records:
        end = raw.index(b'\n', offset)
        actual, kind, size = raw[offset:end].decode('ascii').split()
        if actual != identity or kind != 'blob':
            raise ValueError('Git-index object identity changed while reading ' + path)
        length = int(size)
        content = raw[end + 1:end + 1 + length]
        if len(content) != length or raw[end + 1 + length:end + 2 + length] != b'\n':
            raise ValueError('Truncated Git-index blob for ' + path)
        offset = end + 2 + length
        result[path] = mode, content
    if offset != len(raw):
        raise ValueError('Unexpected Git-index batch response')
    return result


def build_manifest(repo: Path) -> bytes:
    blobs = index_blobs(repo)
    previous = json.loads(blobs[CONFIG][1]) if CONFIG in blobs else {}
    base = previous.get('source_base_sha', DEFAULT_BASE)
    if not isinstance(base, str) or not re.fullmatch(r'[0-9a-f]{40}', base):
        raise ValueError('The staged upstream source base must be a full Git commit identity')
    patches = []
    for path, component in PATCHES:
        if path not in blobs:
            raise ValueError('Missing staged reviewed patch: ' + path)
        patches.append({'path': path, 'sha256': hashlib.sha256(blobs[path][1]).hexdigest(),
                        'optional_component': component})
    owned = []
    for source in sorted(blobs):
        if source == CONFIG:
            continue  # A manifest cannot contain a digest of itself.
        mode, content = blobs[source]
        replace = False
        if source.startswith('overlay/'):
            destination = source.removeprefix('overlay/')
        elif source == CATALOG:
            destination, replace = CATALOG_TARGET, True
        elif source.startswith(RUNTIME_PREFIXES):
            destination = source
        else:
            continue
        owned.append({'source': source, 'destination': destination, 'mode': mode,
                      'sha256': hashlib.sha256(content).hexdigest(), 'replace': replace})
    if CATALOG not in {entry['source'] for entry in owned} or not any(entry['source'].startswith('overlay/') for entry in owned):
        raise ValueError('Stage the complete translation catalog and native owned overlay')
    manifest = {'schema': 2, 'repository': UPSTREAM, 'source_base_sha': base,
                'optional_upstream_components': [OPTIONAL], 'patches': patches, 'owned_files': owned}
    return (json.dumps(manifest, ensure_ascii=False, indent=2) + '\n').encode('utf-8')


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--check', action='store_true', help='Check the staged manifest without writing files')
    args = parser.parse_args()
    repo = args.repo.resolve()
    expected = build_manifest(repo)
    if args.check:
        recorded = index_blobs(repo).get(CONFIG)
        if recorded is None or recorded[1] != expected:
            raise ValueError('Staged resource manifest is stale; refresh and git add the manifest')
        print('Staged patch/resource manifest is current.')
    else:
        (repo / CONFIG).write_bytes(expected)
        print('Refreshed ' + CONFIG + '; stage this file before committing.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
