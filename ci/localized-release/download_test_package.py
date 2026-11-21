"""Download and unpack one verified published ZIP for an independent hosted test."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import sys
from urllib.request import Request, urlopen
import zipfile

from official_workspace import TARGET, checked_tag, build_number
from public_assets import PLATFORMS, parse_publication, public_names

MAX_PACKAGE = 4 * 1024**3


def select_package(release, tag, platform):
    tag = checked_tag(tag)
    if platform not in PLATFORMS:
        raise ValueError('Unsupported test platform')
    if (not isinstance(release, dict) or release.get('tag_name') != tag
            or type(release.get('id')) is not int or release['id'] <= 0
            or release.get('draft') is not False or release.get('prerelease') is not False
            or release.get('html_url') != f'https://github.com/{TARGET}/releases/tag/{tag}'
            or not isinstance(release.get('assets'), list)):
        raise ValueError('An existing published stable release is required')
    marker = parse_publication(release.get('body'), tag)
    records, identities = {}, set()
    for asset in release['assets']:
        if not isinstance(asset, dict):
            raise ValueError('Invalid release asset')
        name = asset.get('name')
        if (not isinstance(name, str) or '/' in name or '\\' in name or name in records
                or type(asset.get('id')) is not int or asset['id'] <= 0 or asset['id'] in identities
                or asset.get('state') != 'uploaded' or type(asset.get('size')) is not int
                or not 0 < asset['size'] <= MAX_PACKAGE or not isinstance(asset.get('digest'), str)
                or not re.fullmatch(r'sha256:[0-9a-f]{64}', asset['digest'])
                or asset.get('browser_download_url') != f'https://github.com/{TARGET}/releases/download/{tag}/{name}'):
            raise ValueError('Release asset identity, content or download URL is invalid')
        records[name] = asset
        identities.add(asset['id'])
    prefix = 'OpenRV-' + tag[1:]
    if marker is not None:
        expected = public_names(tag)
        if set(records) != expected or any(records[name]['digest'] != marker['assets'][name] for name in expected):
            raise ValueError('Clean publication is incomplete or changing; retry after publication finishes')
        number = build_number(marker['buildNumber'])
        layout = 'public-12'
    else:
        pattern = re.compile(re.escape(prefix) + r'-build\.([1-9][0-9]*)-')
        numbers = set()
        for name in records:
            match = pattern.match(name)
            if not match:
                raise ValueError('Legacy publication contains mixed or unrecognized files')
            numbers.add(build_number(match.group(1)))
        if len(numbers) != 1:
            raise ValueError('Exactly one complete legacy build cohort is required')
        number = numbers.pop()
        legacy_prefix = prefix + '-build.' + str(number)
        expected = {name.replace(prefix, legacy_prefix, 1) for name in public_names(tag)}
        expected.update(legacy_prefix + '-' + item + '.build.json' for item in PLATFORMS)
        if set(records) != expected:
            raise ValueError('Legacy publication requires its original fifteen files')
        prefix, layout = legacy_prefix, 'legacy-15'
    suffix = '-Portable.zip' if platform == 'Windows-x64' else '.zip'
    selected = records[prefix + '-' + platform + suffix]
    return {'releaseId': release['id'], 'tag': tag, 'buildNumber': number, 'layout': layout,
            'platform': platform, 'asset': {key: selected[key] for key in
                                         ('id', 'name', 'size', 'digest', 'browser_download_url')}}


def read_release(tag):
    headers = {'Accept': 'application/vnd.github+json', 'User-Agent': 'OpenRV-independent-package-test',
               'X-GitHub-Api-Version': '2026-03-10'}
    if os.environ.get('GH_TOKEN'):
        headers['Authorization'] = 'Bearer ' + os.environ['GH_TOKEN']
    url = f'https://api.github.com/repos/{TARGET}/releases/tags/{checked_tag(tag)}'
    with urlopen(Request(url, headers=headers), timeout=60) as response:
        raw = response.read(2 * 1024 * 1024 + 1)
    if len(raw) > 2 * 1024 * 1024:
        raise ValueError('Release metadata exceeds supported bounds')
    return json.loads(raw)


def download_package(asset, destination, opener=urlopen):
    request = Request(asset['browser_download_url'], headers={'User-Agent': 'OpenRV-independent-package-test'})
    digest, total = hashlib.sha256(), 0
    with opener(request, timeout=120) as response, destination.open('xb') as output:
        if not response.geturl().startswith('https://'):
            raise ValueError('Package transfer must use HTTPS')
        while block := response.read(1024 * 1024):
            total += len(block)
            if total > asset['size'] or total > MAX_PACKAGE:
                raise ValueError('Downloaded package exceeds its authenticated size')
            digest.update(block)
            output.write(block)
    if total != asset['size'] or 'sha256:' + digest.hexdigest() != asset['digest']:
        raise ValueError('Downloaded package does not match its published size and SHA-256')


def check_zip(path, platform):
    """Validate every destination and link before any extraction is permitted."""
    names, links, expanded = set(), {}, 0
    with zipfile.ZipFile(path) as archive:
        members = archive.infolist()
        if len(members) > 100000:
            raise ValueError('Too many archive entries')
        for item in members:
            name = item.filename.rstrip('/')
            parts = PurePosixPath(name).parts
            if (not parts or name.startswith('/') or '\\' in name or ':' in name
                    or any(part in ('.', '..') for part in parts)
                    or '/'.join(parts) != name or name in names or item.flag_bits & 1):
                raise ValueError('Unsafe, duplicate or encrypted ZIP entry')
            names.add(name)
            expanded += item.file_size
            if expanded > 20 * 1024**3:
                raise ValueError('Expanded package exceeds the test limit')
            mode = item.external_attr >> 16
            kind = stat.S_IFMT(mode)
            if kind not in (0, stat.S_IFREG, stat.S_IFDIR, stat.S_IFLNK):
                raise ValueError('Unsupported special file in ZIP')
            if kind == stat.S_IFLNK:
                if platform == 'Windows-x64' or item.file_size > 4096:
                    raise ValueError('Unexpected package symlink')
                target = archive.read(item).decode('utf-8')
                if not target or target.startswith('/') or '\\' in target or ':' in target:
                    raise ValueError('Archive symlink must have a relative target')
                resolved = list(parts[:-1])
                for part in target.split('/'):
                    if part == '..':
                        if not resolved:
                            raise ValueError('Archive symlink escapes its extraction root')
                        resolved.pop()
                    elif part not in ('', '.'):
                        resolved.append(part)
                if not resolved or resolved[0] != parts[0]:
                    raise ValueError('Archive symlink escapes its application bundle')
                links[name] = '/'.join(resolved)
        # ditto may follow directory symlinks while extracting later members.
        for name in names:
            parents = PurePosixPath(name).parents
            if any(str(parent) in links for parent in parents if str(parent) != '.'):
                raise ValueError('Archive writes through a symlink parent')
        for source, target in links.items():
            seen = {source}
            while target in links:
                if target in seen:
                    raise ValueError('Archive contains a symlink cycle')
                seen.add(target)
                target = links[target]
    return len(names)


def unpack_package(path, destination, platform):
    check_zip(path, platform)
    destination.mkdir(exist_ok=False)
    if platform == 'Windows-x64':
        with zipfile.ZipFile(path) as archive:
            archive.extractall(destination)
        candidates = [item for item in destination.iterdir()
                      if item.is_dir() and not item.is_symlink() and (item / 'bin/rv.exe').is_file()]
    else:
        subprocess.run(['ditto', '-x', '-k', str(path), str(destination)], check=True, timeout=600)
        candidates = [destination / name for name in ('RV.app', 'OpenRV.app') if (destination / name).is_dir()]
    if len(candidates) != 1 or candidates[0].is_symlink():
        raise ValueError('Expected exactly one packaged OpenRV runtime')
    runtime = candidates[0].resolve()
    if not runtime.is_relative_to(destination.resolve()):
        raise ValueError('Packaged runtime escaped the extraction root')
    if platform != 'Windows-x64':
        subprocess.run(['codesign', '--verify', '--deep', '--strict', str(runtime)], check=True, timeout=180)
    return runtime


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tag', required=True)
    parser.add_argument('--platform', choices=tuple(PLATFORMS), required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if (os.environ.get('GITHUB_ACTIONS') != 'true' or os.environ.get('RUNNER_ENVIRONMENT') != 'github-hosted'
            or os.environ.get('GITHUB_REPOSITORY') != TARGET):
        raise ValueError('This downloader is restricted to this repository hosted test runners')
    root = Path(os.environ['RUNNER_TEMP']).resolve()
    output = args.output.resolve()
    if output == root or not output.is_relative_to(root) or output.exists():
        raise ValueError('Choose a fresh diagnostic folder beneath RUNNER_TEMP')
    output.mkdir(parents=True)
    selection = select_package(read_release(args.tag), args.tag, args.platform)
    archive = output / selection['asset']['name']
    download_package(selection['asset'], archive)
    if select_package(read_release(args.tag), args.tag, args.platform) != selection:
        raise ValueError('Published package changed during download; rerun against the completed publication')
    runtime = unpack_package(archive, output / 'unpacked', args.platform)
    report = {**selection, 'runtime': str(runtime), 'sha256Verified': True, 'nativeBuild': False,
              'controllerCommit': os.environ.get('GITHUB_SHA'), 'status': 'prepared'}
    (output / 'package-identity.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    with open(os.environ['GITHUB_OUTPUT'], 'a', encoding='utf-8') as stream:
        stream.write('runtime=' + str(runtime) + '\n')
    print(f"Verified {selection['asset']['name']} from {args.tag}; ready for independent tests.")


if __name__ == '__main__':
    main()
