"""Watch official stable releases; prepare isolated candidates and dispatch the gated build."""
from __future__ import annotations

import argparse
import base64
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from urllib.error import HTTPError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

UPSTREAM = 'AcademySoftwareFoundation/OpenRV'
TARGET = 'EternalSunLhx/OpenRV-Publish'
API_VERSION = '2026-03-10'
WORKFLOW = 'localized-release.yml'
STATE_BRANCH = 'automation/upstream-release-state'
STATE_FILE = 'ci/upstream-release-watch/state.json'
PLAN_FILE = 'ci/upstream-release-watch/candidate.json'
LOCALIZATION_BASE_FILE = 'ci/upstream-release-watch/localization-base.json'
OFFICIAL_TAG = re.compile(r'v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)')
SHA = re.compile(r'[0-9a-f]{40}')
SHA256 = re.compile(r'[0-9a-f]{64}')
PREFERENCES_PATCH = 'patches/openrv-preferences-lifetime.patch'
PREFERENCES_CONDITION = 'preferences-lifetime-v1'
PREFERENCES_PATHS = frozenset((
    'src/lib/app/RvCommon/RvApplication.cpp',
    'src/lib/app/RvCommon/RvCommon/RvApplication.h',
    'src/lib/app/RvCommon/RvDocument.cpp',
))


def checked_sha(value: str) -> str:
    if not isinstance(value, str) or not SHA.fullmatch(value):
        raise ValueError('Expected a full Git SHA-1')
    return value


def version(tag: str) -> tuple[int, int, int]:
    match = OFFICIAL_TAG.fullmatch(tag) if isinstance(tag, str) else None
    if not match:
        raise ValueError('Official release tag must be vMAJOR.MINOR.PATCH')
    return tuple(map(int, match.groups()))


def clean_environment() -> dict:
    environment = os.environ.copy()
    for key in tuple(environment):
        if key in ('GH_TOKEN', 'GITHUB_TOKEN', 'GIT_ASKPASS', 'SSH_ASKPASS') or key.startswith('GIT_CONFIG_'):
            environment.pop(key, None)
    environment.update(PYTHONDONTWRITEBYTECODE='1', GIT_TERMINAL_PROMPT='0')
    return environment


def git_environment() -> dict:
    environment = clean_environment()
    if os.name == 'nt':
        # MSYS Git expands unquoted braces in a native parent's argv otherwise.
        # Keep the setting local to Git children; other MSYS tools are unchanged.
        environment['MSYS'] = (environment.get('MSYS', '') + ' noglob').strip()
    return environment


def git(repo: Path, *arguments: str, write: bool = False) -> str:
    environment = git_environment()
    if write:
        token = os.environ['GH_TOKEN']
        authorization = base64.b64encode(('x-access-token:' + token).encode()).decode()
        environment.update(GIT_CONFIG_COUNT='1', GIT_CONFIG_KEY_0='http.https://github.com/.extraheader',
                           GIT_CONFIG_VALUE_0='AUTHORIZATION: basic ' + authorization)
    process = subprocess.run(['git', '-C', str(repo), *arguments], env=environment,
                             text=True, encoding='utf-8', capture_output=True, timeout=180)
    if process.returncode:
        raise RuntimeError('Git operation failed: ' + ' '.join(arguments[:2]) + '\n' + process.stderr.strip())
    return process.stdout.strip()


def git_bytes(repo: Path, *arguments: str, data: bytes | None = None,
              index: Path | None = None, check: bool = True) -> subprocess.CompletedProcess:
    """Use Git object/index plumbing without text stripping or candidate execution."""
    environment = git_environment()
    if index is not None:
        environment['GIT_INDEX_FILE'] = str(index)
    result = subprocess.run(['git', '-C', str(repo), *arguments], env=environment,
                            input=data, capture_output=True, timeout=180)
    if check and result.returncode:
        raise RuntimeError('Git object/index operation failed: ' + ' '.join(arguments[:2]))
    return result


def safe_file(repo: Path, relative: str) -> Path:
    path = repo / relative
    if not path.resolve().is_relative_to(repo.resolve()):
        raise ValueError('Candidate metadata escapes its checkout')
    current = path
    while current != repo:
        if current.is_symlink():
            raise ValueError('Candidate metadata cannot use symbolic links')
        current = current.parent
    return path


def localization_configuration(repo: Path, snapshot: str) -> dict:
    """Read the patch boundary from the immutable trusted Git snapshot."""
    checked_sha(snapshot)
    config = json.loads(git(repo, 'show', snapshot + ':' + LOCALIZATION_BASE_FILE))
    if config.get('schema') not in (1, 2) or config.get('repository') != UPSTREAM:
        raise ValueError('Unsupported localization patch boundary')
    patch_base = checked_sha(config.get('source_base_sha'))
    if config['schema'] == 1:
        git(repo, 'merge-base', '--is-ancestor', patch_base, snapshot)
    components = config.get('optional_upstream_components', [])
    if (not isinstance(components, list) or len(components) > 16
            or any(not isinstance(path, str) or not re.fullmatch(r'src/plugins/rv-packages/[A-Za-z0-9_-]+', path)
                   for path in components) or len(set(components)) != len(components)):
        raise ValueError('Optional upstream components must be explicit package directories')
    if config['schema'] == 2:
        validate_bundle_configuration(config)
    return config


def bundle_path(value: str) -> str:
    if (not isinstance(value, str) or len(value) > 512 or not re.fullmatch(r'[A-Za-z0-9_./-]+', value)
            or value.startswith('/') or any(part in ('', '.', '..', '.git') for part in value.split('/'))
            or value.split('/')[0] in ('_release_work', 'dist')):
        raise ValueError('Bundle paths must be explicit safe repository-relative paths')
    return value


def validate_bundle_configuration(config: dict) -> None:
    if set(config) != {'schema', 'repository', 'source_base_sha', 'optional_upstream_components', 'patches', 'owned_files'}:
        raise ValueError('Unknown or missing lightweight bundle configuration fields')
    patches, owned = config.get('patches'), config.get('owned_files')
    if not isinstance(patches, list) or not 1 <= len(patches) <= 16:
        raise ValueError('A lightweight bundle needs an explicit bounded patch list')
    patch_paths, optional, conditional = set(), set(), False
    for entry in patches:
        keys = {'path', 'sha256', 'optional_component'}
        if not isinstance(entry, dict) or set(entry) not in (keys, keys | {'condition'}):
            raise ValueError('Invalid declared patch record')
        path = bundle_path(entry['path'])
        if not path.startswith('patches/') or path in patch_paths or not SHA256.fullmatch(str(entry['sha256'])):
            raise ValueError('Invalid or duplicate declared patch identity')
        patch_paths.add(path)
        component = entry['optional_component']
        if 'condition' in entry:
            if (path != PREFERENCES_PATCH or entry['condition'] != PREFERENCES_CONDITION
                    or component is not None or entry is not patches[-1]):
                raise ValueError('Conditional patch must be the final reviewed preferences-lifetime patch')
            conditional = True
        elif path == PREFERENCES_PATCH:
            raise ValueError('Preferences-lifetime patch requires its reviewed condition')
        if component is not None:
            if component not in config.get('optional_upstream_components', []):
                raise ValueError('A patch may omit only an explicitly reviewed optional component')
            optional.add(component)
    if optional != set(config.get('optional_upstream_components', [])):
        raise ValueError('Optional component inventory must exactly match its whole-component patches')
    if not isinstance(owned, list) or not 1 <= len(owned) <= 4096:
        raise ValueError('A lightweight bundle needs an explicit bounded owned-file map')
    destinations = set()
    for entry in owned:
        if not isinstance(entry, dict) or set(entry) != {'source', 'destination', 'mode', 'sha256', 'replace'}:
            raise ValueError('Invalid owned-file record')
        source, destination = bundle_path(entry['source']), bundle_path(entry['destination'])
        if (source == LOCALIZATION_BASE_FILE or source in patch_paths or destination in destinations
                or entry['mode'] not in ('100644', '100755') or not SHA256.fullmatch(str(entry['sha256']))
                or type(entry['replace']) is not bool):
            raise ValueError('Invalid, recursive, or duplicate owned-file identity')
        if conditional and destination in PREFERENCES_PATHS:
            raise ValueError('Owned overlays cannot replace conditionally verified preferences source')
        destinations.add(destination)


def validate_conditional_patch_decisions(records) -> None:
    if not isinstance(records, list) or len(records) > 1:
        raise ValueError('Invalid conditional maintenance patch provenance')
    for record in records:
        if (not isinstance(record, dict)
                or set(record) != {'path', 'sha256', 'condition', 'input_tree', 'output_tree', 'decision', 'reason'}
                or record['path'] != PREFERENCES_PATCH or record['condition'] != PREFERENCES_CONDITION
                or not isinstance(record['sha256'], str) or not SHA256.fullmatch(record['sha256'])
                or (record['decision'], record['reason']) not in (
                    ('applied', 'forward-apply-check'), ('already-fixed', 'reverse-apply-check'))):
            raise ValueError('Invalid conditional maintenance patch decision')
        checked_sha(record['input_tree']); checked_sha(record['output_tree'])
        if (record['input_tree'] == record['output_tree']) != (record['decision'] == 'already-fixed'):
            raise ValueError('Conditional patch tree identities do not match its decision')


def apply_preferences_patch(repo: Path, index: Path, record: dict, data: bytes) -> dict:
    """Skip only a complete reverse-applicable fix; partial/unknown forms require review."""
    paths = git_bytes(repo, 'apply', '--numstat', '-z', '-', data=data).stdout
    changed = [item.split(b'\t', 2)[-1].decode('utf-8') for item in paths.split(b'\0') if item]
    if len(changed) != len(PREFERENCES_PATHS) or set(changed) != PREFERENCES_PATHS:
        raise ValueError('Preferences-lifetime patch must change exactly its three reviewed source paths')
    before = checked_sha(git_bytes(repo, 'write-tree', index=index).stdout.decode().strip())
    reverse = git_bytes(repo, 'apply', '--cached', '--reverse', '--check', '--whitespace=nowarn', '-',
                        data=data, index=index, check=False)
    if reverse.returncode == 0:
        decision, reason = 'already-fixed', 'reverse-apply-check'
    else:
        forward = git_bytes(repo, 'apply', '--cached', '--check', '--whitespace=nowarn', '-',
                            data=data, index=index, check=False)
        if forward.returncode:
            raise RuntimeError('Preferences-lifetime patch is partial or conflicts with upstream; '
                               'review the official source instead of guessing an equivalent fix')
        # Deliberately no --3way: it could silently complete a partial upstream
        # fix, which this versioned, exact source contract does not authorize.
        git_bytes(repo, 'apply', '--cached', '--whitespace=nowarn', '-', data=data, index=index)
        if git_bytes(repo, 'apply', '--cached', '--reverse', '--check', '--whitespace=nowarn', '-',
                     data=data, index=index, check=False).returncode:
            raise RuntimeError('Applied preferences-lifetime patch failed its exact reverse verification')
        decision, reason = 'applied', 'forward-apply-check'
    after = checked_sha(git_bytes(repo, 'write-tree', index=index).stdout.decode().strip())
    result = {key: record[key] for key in ('path', 'sha256', 'condition')}
    result.update(input_tree=before, output_tree=after, decision=decision, reason=reason)
    validate_conditional_patch_decisions([result])
    return result


@lru_cache(maxsize=4096)
def snapshot_blob(repo: Path, snapshot: str, path: str) -> tuple[str, str, bytes]:
    path = bundle_path(path)
    entries = git_bytes(repo, 'ls-tree', '-z', snapshot, '--', path).stdout.split(b'\0')
    entries = [entry for entry in entries if entry]
    if len(entries) != 1:
        raise ValueError('Bundle input is not one committed blob: ' + path)
    header, actual = entries[0].split(b'\t', 1)
    mode, kind, blob = header.decode('ascii').split()
    if actual.decode('utf-8') != path or kind != 'blob' or mode not in ('100644', '100755'):
        raise ValueError('Bundle input must be a regular committed file: ' + path)
    return mode, checked_sha(blob), git_bytes(repo, 'cat-file', 'blob', blob).stdout


def verified_bundle_inputs(repo: Path, snapshot: str, config: dict):
    patches, owned = [], []
    for record in config['patches']:
        mode, blob, data = snapshot_blob(repo, snapshot, record['path'])
        if hashlib.sha256(data).hexdigest() != record['sha256']:
            raise ValueError('Declared patch digest differs from its immutable snapshot: ' + record['path'])
        patches.append((record, data))
    for record in config['owned_files']:
        mode, blob, data = snapshot_blob(repo, snapshot, record['source'])
        if mode != record['mode'] or hashlib.sha256(data).hexdigest() != record['sha256']:
            raise ValueError('Owned file mode or digest differs from its immutable snapshot: ' + record['source'])
        owned.append((record, blob))
    return patches, owned


def compose_bundle_tree(repo: Path, release: Release, patch_base: str, snapshot: str, config: dict,
                        conditional_decisions: list[dict] | None = None):
    if git_bytes(repo, 'cat-file', '-e', patch_base + '^{commit}', check=False).returncode:
        git(repo, 'fetch', '--no-tags', 'https://github.com/' + UPSTREAM + '.git', patch_base)
        if git(repo, 'rev-parse', 'FETCH_HEAD^{commit}') != patch_base:
            raise ValueError('Upstream patch-base fetch returned a different commit')
    patches, owned = verified_bundle_inputs(repo, snapshot, config)
    native_version = cmake_version(git(repo, 'show', release.commit_sha + ':CMakeLists.txt'))
    omitted = omitted_components(repo, patch_base, snapshot, release.commit_sha)
    git_directory = (repo / git(repo, 'rev-parse', '--path-format=relative', '--git-dir')).resolve()
    with tempfile.TemporaryDirectory(prefix='openrv-bundle-index-', dir=git_directory) as temporary:
        index = Path(temporary) / 'index'
        git_bytes(repo, 'read-tree', release.commit_sha, index=index)
        for record, data in patches:
            if 'condition' in record:
                decision = apply_preferences_patch(repo, index, record, data)
                if conditional_decisions is not None:
                    conditional_decisions.append(decision)
                continue
            component = record['optional_component']
            if component is not None:
                paths = git_bytes(repo, 'apply', '--numstat', '-z', '-', data=data).stdout
                changed = [item.split(b'\t', 2)[-1].decode('utf-8') for item in paths.split(b'\0') if item]
                if not changed or any(not path.startswith(component + '/') for path in changed):
                    raise ValueError('Optional patch changes files outside its reviewed whole component')
                if component in omitted:
                    continue
            if git_bytes(repo, 'apply', '--cached', '--3way', '--whitespace=nowarn', '-',
                         data=data, index=index, check=False).returncode:
                raise RuntimeError('Declared localization patch conflicts with the official release: ' + record['path'])
        for record, blob in owned:
            destination = record['destination']
            if any(destination.startswith(component + '/') for component in omitted):
                continue
            existing = git_bytes(repo, 'ls-files', '-z', '--', destination, index=index).stdout
            if existing and not record['replace']:
                raise ValueError('Owned new file collides with official source: ' + destination)
            entry = f"{record['mode']} {blob}\t{destination}\0".encode('utf-8')
            git_bytes(repo, 'update-index', '-z', '--index-info', data=entry, index=index)
        # CI authority always remains the exact maintenance snapshot workflow tree.
        existing = git_bytes(repo, 'ls-files', '-z', '--', '.github/workflows', index=index).stdout
        if existing:
            git_bytes(repo, 'update-index', '-z', '--force-remove', '--stdin', data=existing, index=index)
        workflows = git_bytes(repo, 'ls-tree', '-r', '-z', '--full-tree', snapshot, '--', '.github/workflows').stdout
        if not workflows:
            raise ValueError('Trusted lightweight snapshot has no workflow tree')
        git_bytes(repo, 'update-index', '-z', '--index-info', data=workflows, index=index)
        tree = checked_sha(git_bytes(repo, 'write-tree', index=index).stdout.decode().strip())
    if cmake_version(git(repo, 'show', tree + ':CMakeLists.txt')) != native_version:
        raise ValueError('Localization patch changed the official native source version')
    raw_config = snapshot_blob(repo, snapshot, LOCALIZATION_BASE_FILE)[2]
    return tree, hashlib.sha256(raw_config).hexdigest(), omitted


def localization_source(repo: Path, snapshot: str) -> str:
    return localization_configuration(repo, snapshot)['source_base_sha']


def omitted_components(repo: Path, patch_base: str, snapshot: str, official: str) -> list[str]:
    """Do not introduce an unreleased optional component into an older release.

    Only explicitly reviewed whole components qualify. Partial files/trees and
    ordinary missing files remain hard conflicts; no failed patch hunk is dropped.
    """
    omitted = []
    for path in sorted(localization_configuration(repo, snapshot).get('optional_upstream_components', [])):
        if git(repo, 'cat-file', '-t', patch_base + ':' + path) != 'tree':
            raise ValueError('Optional component is not an upstream source directory: ' + path)
        if not git(repo, 'ls-tree', official, '--', path):
            omitted.append(path)
    return omitted


def localization_patch(repo: Path, patch_base: str, snapshot: str, omitted: list[str] | None = None) -> bytes:
    # Read committed blobs, never a dirty working tree or an external diff driver.
    return git_bytes(repo, 'diff', '--binary', '--full-index', '--no-ext-diff', '--no-textconv',
                     patch_base, snapshot, '--', '.', ':(exclude).github/workflows',
                     *(':(exclude)' + path for path in (omitted or []))).stdout


def compose_source_tree(repo: Path, release: Release, patch_base: str,
                        snapshot: str, *, conditional_decisions: list[dict] | None = None) -> tuple[str, str, list[str]]:
    """Build official-tag + localization tree in an owned temporary index.

    The real candidate index/worktree and human translation commits are untouched.
    Workflow blobs always come from the trusted snapshot. No newer upstream-main
    commits become parents or contribute source outside our explicit feature diff.
    """
    config = localization_configuration(repo, snapshot)
    if checked_sha(config['source_base_sha']) != checked_sha(patch_base):
        raise ValueError('Composition patch base differs from the immutable configuration')
    if config['schema'] == 2:
        return compose_bundle_tree(repo, release, patch_base, snapshot, config, conditional_decisions)
    native_version = cmake_version(git(repo, 'show', release.commit_sha + ':CMakeLists.txt'))
    omitted = omitted_components(repo, patch_base, snapshot, release.commit_sha)
    patch = localization_patch(repo, patch_base, snapshot, omitted)
    # MSYS Git prints POSIX absolute paths which native Python cannot interpret.
    git_directory = (repo / git(repo, 'rev-parse', '--path-format=relative', '--git-dir')).resolve()
    with tempfile.TemporaryDirectory(prefix='openrv-release-index-', dir=git_directory) as temporary:
        index = Path(temporary) / 'index'
        git_bytes(repo, 'read-tree', release.commit_sha, index=index)
        if patch:
            result = git_bytes(repo, 'apply', '--cached', '--3way', '--whitespace=nowarn', '-',
                               data=patch, index=index, check=False)
            if result.returncode:
                conflicts = git_bytes(repo, 'diff', '--cached', '--name-only', '--diff-filter=U',
                                      index=index).stdout.decode('utf-8', errors='replace').strip()
                if not conflicts:
                    errors = [line for line in result.stderr.decode('utf-8', errors='replace').splitlines()
                              if line.startswith('error:')]
                    changed = git_bytes(repo, 'diff', '--name-only', '-z', patch_base, snapshot,
                                        '--', '.', ':(exclude).github/workflows',
                                        *(':(exclude)' + path for path in omitted)).stdout
                    conflicts = '\n'.join(path for raw in changed.split(b'\0') if raw
                                          and (path := raw.decode('utf-8', errors='replace'))
                                          and any(path in line for line in errors))
                raise RuntimeError('Localization patch conflicts with the official release; main was not changed.'
                                   + ('\n' + conflicts if conflicts else ' Review the patch boundary and source changes.'))
        existing = git_bytes(repo, 'ls-files', '-z', '--', '.github/workflows', index=index).stdout
        if existing:
            git_bytes(repo, 'update-index', '-z', '--force-remove', '--stdin', data=existing, index=index)
        trusted = git_bytes(repo, 'ls-tree', '-r', '-z', '--full-tree', snapshot, '--', '.github/workflows').stdout
        if not trusted:
            raise ValueError('Trusted localization snapshot has no workflow tree')
        git_bytes(repo, 'update-index', '-z', '--index-info', data=trusted, index=index)
        tree = checked_sha(git_bytes(repo, 'write-tree', index=index).stdout.decode('ascii').strip())
    if cmake_version(git(repo, 'show', tree + ':CMakeLists.txt')) != native_version:
        raise ValueError('Localization patch changed the official native source version')
    return tree, hashlib.sha256(patch).hexdigest(), omitted


def fetch_official_release(repo: Path, release: Release) -> str:
    git(repo, 'fetch', '--no-tags', 'https://github.com/' + UPSTREAM + '.git', 'refs/tags/' + release.tag)
    if (git(repo, 'rev-parse', 'FETCH_HEAD') != release.object_sha
            or git(repo, 'rev-parse', 'FETCH_HEAD^{commit}') != release.commit_sha):
        raise ValueError('Official tag changed during fetch')
    return checked_sha(git(repo, 'rev-parse', release.commit_sha + '^{tree}'))


def remote_candidate_head(repo: Path, branch: str) -> str:
    ref = 'refs/heads/' + branch
    output = git(repo, 'ls-remote', '--heads', 'origin', ref)
    if not output:
        return ''
    lines = output.splitlines()
    if len(lines) != 1 or len(parts := lines[0].split('\t')) != 2 or parts[1] != ref:
        raise ValueError('Exact candidate branch lookup returned an unexpected ref')
    return checked_sha(parts[0])


class Api:
    def request(self, method: str, repository: str, suffix: str, payload: dict | None = None) -> tuple[int, object]:
        if repository not in (UPSTREAM, TARGET):
            raise ValueError('Unknown repository')
        if method == 'GET':
            allowed = (r'releases/latest|releases/[1-9][0-9]*|git/ref/(?:tags/v[0-9.]+|heads/automation/upstream-release-state)|'
                       r'git/tags/[0-9a-f]{40}|contents/ci/upstream-release-watch/state\.json\?ref=automation%2Fupstream-release-state|'
                       r'actions/workflows/localized-release\.yml(?:/runs\?[^\s]+)?|actions/runs/[1-9][0-9]*')
            if not re.fullmatch(allowed, suffix):
                raise ValueError('Unsupported API read endpoint')
        elif repository != TARGET or not (
                (method == 'POST' and suffix in ('git/refs', 'actions/workflows/' + WORKFLOW + '/dispatches'))
                or (method == 'PUT' and suffix == 'contents/' + STATE_FILE)):
            raise ValueError('Only candidate dispatch and dedicated state writes are allowed')
        if method == 'POST' and suffix == 'git/refs':
            if set(payload or {}) != {'ref', 'sha'} or payload['ref'] != 'refs/heads/' + STATE_BRANCH:
                raise ValueError('Only the dedicated state branch may be created through this endpoint')
            checked_sha(payload['sha'])
        if method == 'PUT' and (payload or {}).get('branch') != STATE_BRANCH:
            raise ValueError('State writes must target the dedicated branch')
        if method == 'POST' and suffix.endswith('/dispatches'):
            inputs = (payload or {}).get('inputs', {})
            if (set(payload or {}) != {'ref', 'inputs'}
                    or not re.fullmatch(r'automation/upstream-release-[1-9][0-9]*', payload['ref'])
                    or set(inputs) != {'release_tag', 'create_tag', 'require_gui_smoke'}
                    or not re.fullmatch(r'v(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)-zh\.[1-9][0-9]*', inputs.get('release_tag', ''))
                    or inputs['create_tag'] is not True or inputs['require_gui_smoke'] is not True):
                raise ValueError('Only the watcher candidate with all release gates may be dispatched')
        data = json.dumps(payload).encode() if payload is not None else None
        request = Request('https://api.github.com/repos/' + repository + '/' + suffix, data=data, method=method,
                          headers={'Accept': 'application/vnd.github+json', 'X-GitHub-Api-Version': API_VERSION,
                                   'Authorization': 'Bearer ' + os.environ['GH_TOKEN'],
                                   'Content-Type': 'application/json', 'User-Agent': 'OpenRV-upstream-release-watch'})
        try:
            response = urlopen(request, timeout=45)
        except HTTPError as error:
            response = error
        with response:
            raw = response.read()
            body = json.loads(raw) if raw else None
            return response.status, body


def require(status: int, expected: int, body: object) -> object:
    if status != expected:
        raise RuntimeError(f'GitHub API expected HTTP {expected}, received {status}: {body}')
    return body


@dataclass(frozen=True)
class Release:
    release_id: int
    tag: str
    object_sha: str
    commit_sha: str
    published_at: str


def snapshot(api: Api) -> Release:
    status, body = api.request('GET', UPSTREAM, 'releases/latest')
    body = require(status, 200, body)
    if (not isinstance(body, dict) or type(body.get('id')) is not int or body['id'] <= 0
            or body.get('draft') is not False or body.get('prerelease') is not False):
        raise ValueError('Latest official release is not a stable published release')
    tag = body.get('tag_name')
    version(tag)
    published = body.get('published_at')
    if not isinstance(published, str) or not re.fullmatch(r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ', published):
        raise ValueError('Official release has no valid publication timestamp')
    status, ref = api.request('GET', UPSTREAM, 'git/ref/tags/' + tag)
    ref = require(status, 200, ref)
    if ref.get('ref') != 'refs/tags/' + tag:
        raise ValueError('Official exact-tag lookup returned another ref')
    current = ref.get('object', {})
    original = checked_sha(current.get('sha'))
    seen = set()
    for _ in range(8):
        sha = checked_sha(current.get('sha'))
        if current.get('type') == 'commit':
            return Release(body['id'], tag, original, sha, published)
        if current.get('type') != 'tag' or sha in seen:
            raise ValueError('Official tag does not peel to a commit')
        seen.add(sha)
        status, annotated = api.request('GET', UPSTREAM, 'git/tags/' + sha)
        annotated = require(status, 200, annotated)
        if annotated.get('sha') != sha:
            raise ValueError('Official annotated tag object differs')
        current = annotated.get('object', {})
    raise ValueError('Official annotated tag nesting is excessive')


def same_release(record: dict, release: Release) -> bool:
    return all(record.get(key) == value for key, value in asdict(release).items())


def load_state(api: Api, baseline: dict) -> tuple[dict, str]:
    suffix = 'contents/' + STATE_FILE + '?ref=' + quote(STATE_BRANCH, safe='')
    status, body = api.request('GET', TARGET, suffix)
    if status == 404:
        branch_status, branch = api.request('GET', TARGET, 'git/ref/heads/' + STATE_BRANCH)
        empty_sha = ''
        if branch_status != 404:
            require(branch_status, 200, branch)
            if branch.get('ref') != 'refs/heads/' + STATE_BRANCH or branch.get('object', {}).get('type') != 'commit':
                raise ValueError('Unexpected empty state branch')
            empty_sha = checked_sha(branch['object']['sha'])
        # An interrupted first state save may have created the branch but not its
        # file. save_state accepts it only at this candidate's exact main base.
        return {'schema': 1, 'baseline': baseline, 'handled': {}, 'pending': {}, '_empty_branch_sha': empty_sha}, ''
    body = require(status, 200, body)
    if body.get('type') != 'file' or body.get('encoding') != 'base64':
        raise ValueError('Unexpected state content response')
    state = json.loads(base64.b64decode(body['content'], validate=False))
    if (state.get('schema') != 1 or state.get('baseline') != baseline
            or not isinstance(state.get('handled'), dict) or not isinstance(state.get('pending'), dict)):
        raise ValueError('State schema or initial baseline differs; manual review is required')
    return state, checked_sha(body.get('sha'))


def needs_build(state: dict, release: Release) -> bool:
    records = [state['baseline'], *state['handled'].values()]
    for record in records:
        if record.get('release_id') == release.release_id:
            if not same_release(record, release):
                raise ValueError('A known official release ID/tag/commit was changed; refusing silent retargeting')
            return False
    for record in state.get('pending', {}).values():
        if record.get('release_id') == release.release_id and not same_release(record, release):
            raise ValueError('A pending official release snapshot changed; manual review is required')
    if version(release.tag) <= max(version(record['tag']) for record in records):
        raise ValueError('New official release ID does not advance the known stable version')
    return True


def cmake_version(text: str) -> str:
    return '.'.join(re.search(r'SET\(' + key + r'\s+"(\d+)"', text).group(1)
                    for key in ('RV_MAJOR_VERSION', 'RV_MINOR_VERSION', 'RV_REVISION_NUMBER'))


def base_version(repo: Path) -> str:
    return cmake_version((repo / 'CMakeLists.txt').read_text(encoding='utf-8'))


def next_edition(tags: str, official_tag: str) -> str:
    pattern = re.compile(re.escape(official_tag) + r'-zh\.([1-9][0-9]*)')
    numbers = [int(match.group(1)) for line in tags.splitlines()
               if len(parts := line.split('\t')) == 2
               and (match := pattern.fullmatch(parts[1].removeprefix('refs/tags/')))]
    return official_tag + '-zh.' + str(max(numbers, default=0) + 1)


def verify_candidate(repo: Path, plan: dict, release: Release, trusted_snapshot: str) -> str:
    if (plan.get('schema') != 2 or plan.get('kind') != 'official-release-localization'
            or not same_release(plan.get('upstream', {}), release)):
        raise ValueError('Existing candidate metadata requires review; expected an official-release tree composition')
    base = checked_sha(plan.get('base_sha'))
    checked_sha(trusted_snapshot)
    if git_bytes(repo, 'merge-base', '--is-ancestor', base, trusted_snapshot, check=False).returncode:
        raise ValueError('Candidate localization snapshot is not part of the trusted controller history')
    if not re.fullmatch(re.escape(release.tag) + r'-zh\.[1-9][0-9]*', plan.get('release_tag', '')):
        raise ValueError('Existing candidate has an invalid localized tag')
    patch_base = localization_source(repo, base)
    if checked_sha(plan.get('localization_base_sha')) != patch_base:
        raise ValueError('Candidate localization patch boundary differs from its trusted snapshot')
    if checked_sha(plan.get('upstream_tree_sha')) != git(repo, 'rev-parse', release.commit_sha + '^{tree}'):
        raise ValueError('Candidate official source tree differs from the pinned release')
    native_version = cmake_version(git(repo, 'show', release.commit_sha + ':CMakeLists.txt'))
    if plan.get('upstream_native_version') != native_version:
        raise ValueError('Candidate native version differs from the pinned official source')
    expected_tree, expected_patch, omitted = compose_source_tree(repo, release, patch_base, base)
    if (checked_sha(plan.get('composed_tree_sha')) != expected_tree
            or not isinstance(plan.get('localization_patch_sha256'), str)
            or not SHA256.fullmatch(plan['localization_patch_sha256'])
            or plan['localization_patch_sha256'] != expected_patch
            or plan.get('omitted_upstream_components') != omitted):
        raise ValueError('Candidate source composition or feature patch digest differs')
    composed = checked_sha(plan.get('composed_commit_sha'))
    if git(repo, 'rev-list', '--parents', '-n', '1', composed).split() != [composed, base]:
        raise ValueError('Composed source commit must have exactly the trusted snapshot as its parent')
    if git(repo, 'rev-parse', composed + '^{tree}') != expected_tree:
        raise ValueError('Composed source commit does not contain the verified official-release tree')
    git(repo, 'merge-base', '--is-ancestor', composed, 'HEAD')
    if git(repo, 'rev-parse', 'HEAD:.github/workflows') != git(repo, 'rev-parse', base + ':.github/workflows'):
        raise ValueError('Candidate workflow tree differs from its trusted main snapshot')
    safe_file(repo, 'CMakeLists.txt')
    if base_version(repo) != native_version:
        raise ValueError('Candidate CMake version differs from the official native source version')
    return checked_sha(git(repo, 'rev-parse', 'HEAD'))


def static_checks(trusted: Path, repo: Path) -> None:
    environment = clean_environment()
    lupdate = Path(os.environ.get('QT_LUPDATE_PATH', '/usr/lib/qt6/bin/lupdate'))
    if not lupdate.is_file():
        raise ValueError('Qt lupdate is required for accurate native source/context extraction')
    failed = []
    for script in ('update_menu_paths.py', 'sync_sources.py', 'check_translations.py'):
        path = trusted / 'tools/localization' / script
        if not path.is_file():
            raise ValueError('Candidate lacks the localization check: ' + script)
        arguments = [sys.executable, str(path), '--repo', str(repo)]
        if script == 'sync_sources.py':
            arguments += ['--output-dir', str(repo / 'ci/upstream-release-watch/translations'),
                          '--lupdate', str(lupdate), '--apply']
        if script == 'check_translations.py':
            arguments += ['--report', str(repo / 'ci/upstream-release-watch/catalog-check.json')]
        result = subprocess.run(arguments, cwd=trusted, env=environment, check=False, timeout=180)
        if result.returncode:
            failed.append(script)
    git(repo, 'diff', '--check')
    if failed:
        raise ValueError('Candidate localization checks failed: ' + ', '.join(failed))


def commit(repo: Path, message: str) -> None:
    git(repo, '-c', 'user.name=OpenRV localization automation', '-c',
        'user.email=41898282+github-actions[bot]@users.noreply.github.com', 'commit', '-m', message)


def prepare_candidate(source: Path, destination: Path, release: Release) -> tuple[str, str, str, str, str]:
    source, destination = source.resolve(), destination.resolve()
    if destination.exists():
        raise ValueError('Candidate workspace must be new and outside the trusted checkout')
    if destination == source or destination.is_relative_to(source):
        raise ValueError('Candidate workspace must be outside the trusted checkout')
    # Snapshot exactly the controller checkout, even if remote main advances.
    snapshot_sha = checked_sha(git(source, 'rev-parse', 'HEAD'))
    localization_source(source, snapshot_sha)
    subprocess.run(['git', 'clone', '--no-checkout', 'https://github.com/' + TARGET + '.git', str(destination)],
                   env=clean_environment(), check=True, timeout=180)
    git(destination, 'config', 'user.name', 'OpenRV localization automation')
    git(destination, 'config', 'user.email', '41898282+github-actions[bot]@users.noreply.github.com')
    git(destination, 'config', 'core.autocrlf', 'false')
    git(destination, 'fetch', '--no-tags', 'origin', snapshot_sha)
    if git(destination, 'rev-parse', 'FETCH_HEAD^{commit}') != snapshot_sha:
        raise ValueError('Trusted localization snapshot changed during fetch')
    upstream_tree = fetch_official_release(destination, release)
    branch = 'automation/upstream-release-' + str(release.release_id)
    expected = remote_candidate_head(destination, branch)
    if expected:
        git(destination, 'fetch', '--no-tags', 'origin', 'refs/heads/' + branch)
        if git(destination, 'rev-parse', 'FETCH_HEAD') != expected:
            raise ValueError('Candidate branch moved during fetch')
        git(destination, 'checkout', '--detach', expected)
        plan = json.loads(safe_file(destination, PLAN_FILE).read_text(encoding='utf-8'))
        verify_candidate(destination, plan, release, snapshot_sha)
    else:
        base = snapshot_sha
        patch_base = localization_source(destination, base)
        tree, patch_digest, omitted = compose_source_tree(destination, release, patch_base, base)
        # The source tree comes from the official release, but its single parent
        # is our trusted controller snapshot. The new commit introduces no workflow
        # changes and does not carry newer development-main source into this tree.
        composed = checked_sha(git(destination, 'commit-tree', tree, '-p', base, '-m',
                                   'Apply localization snapshot to official ' + release.tag))
        git(destination, 'checkout', '--detach', composed)
        plan = {'schema': 2, 'kind': 'official-release-localization',
                'upstream': asdict(release), 'upstream_tree_sha': upstream_tree, 'base_sha': base,
                'upstream_native_version': cmake_version(git(destination, 'show', release.commit_sha + ':CMakeLists.txt')),
                'omitted_upstream_components': omitted,
                'localization_base_sha': patch_base, 'localization_patch_sha256': patch_digest,
                'composed_tree_sha': tree, 'composed_commit_sha': composed,
                'release_tag': next_edition(git(destination, 'ls-remote', '--tags', 'origin'), release.tag)}
        path = safe_file(destination, PLAN_FILE)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(plan, indent=2) + '\n', encoding='utf-8')
        # Run only trusted default-branch tools; candidate source is data, never a script.
        updater = source / 'tools/localization/update_menu_paths.py'
        if not updater.is_file():
            raise ValueError('Trusted controller lacks the static menu path generator')
    # Preserve a clean, version-validated candidate even when new captions need translation.
    check_error = None
    try:
        static_checks(source, destination)
    except (ValueError, RuntimeError, subprocess.SubprocessError) as error:
        check_error = error
    if git(destination, 'status', '--porcelain'):
        git(destination, 'add', '--all')
        commit(destination, 'Refresh official ' + release.tag + ' localized candidate and checks')
    candidate_sha = verify_candidate(destination, plan, release, snapshot_sha)
    git(destination, 'fetch', '--no-tags', 'origin', 'refs/heads/main')
    if git(destination, 'rev-parse', 'FETCH_HEAD:.github/workflows') != git(destination, 'rev-parse', 'HEAD:.github/workflows'):
        raise ValueError('Main workflow definitions changed; refresh the candidate before any dispatch')
    if remote_candidate_head(destination, branch) != expected:
        raise ValueError('Candidate branch moved during preparation; refusing to overwrite human changes')
    if not expected or candidate_sha != expected:
        git(destination, 'push', 'origin', 'HEAD:refs/heads/' + branch, write=True)
    if check_error:
        print('Candidate localization/source check details: ' + str(check_error), flush=True)
    reason = 'Localization/source checks failed; review the manual reports and workflow logs.' if check_error else ''
    return branch, candidate_sha, plan['release_tag'], plan['base_sha'], reason


def checked_run(api: Api, run_id: int, branch: str, sha: str, workflow_id: int) -> dict:
    if type(run_id) is not int or run_id <= 0:
        raise ValueError('Dispatch did not return a positive workflow run ID')
    status, run = api.request('GET', TARGET, 'actions/runs/' + str(run_id))
    run = require(status, 200, run)
    if (run.get('id') != run_id or run.get('head_sha') != sha or run.get('head_branch') != branch
            or run.get('event') != 'workflow_dispatch' or run.get('workflow_id') != workflow_id
            or run.get('path') != '.github/workflows/' + WORKFLOW):
        raise ValueError('Dispatched workflow identity or source SHA differs from the candidate')
    return run


def verify_branch_head(repo: Path, branch: str, sha: str) -> None:
    ref = 'refs/heads/' + branch
    if git(repo, 'ls-remote', '--heads', 'origin', ref) != checked_sha(sha) + '\t' + ref:
        raise ValueError('Candidate branch moved after its localization checks; no dispatch will be made')


def dispatch(api: Api, branch: str, sha: str, tag: str) -> tuple[int, str]:
    status, workflow = api.request('GET', TARGET, 'actions/workflows/' + WORKFLOW)
    workflow = require(status, 200, workflow)
    workflow_id = workflow.get('id')
    if type(workflow_id) is not int or workflow.get('state') != 'active' or workflow.get('path') != '.github/workflows/' + WORKFLOW:
        raise ValueError('Localized build workflow is missing, inactive or unexpected')
    # Recover a previously accepted dispatch when saving its state failed. This branch
    # namespace is reserved for the watcher; failed builds are not automatically retried.
    query = urlencode({'event': 'workflow_dispatch', 'branch': branch, 'per_page': 100})
    status, runs = api.request('GET', TARGET, 'actions/workflows/' + WORKFLOW + '/runs?' + query)
    runs = require(status, 200, runs)
    for run in runs.get('workflow_runs', []):
        if run.get('head_sha') == sha:
            confirmed = checked_run(api, run.get('id'), branch, sha, workflow_id)
            return confirmed['id'], confirmed['html_url']
    status, response = api.request('POST', TARGET, 'actions/workflows/' + WORKFLOW + '/dispatches',
                                   {'ref': branch, 'inputs': {'release_tag': tag, 'create_tag': True,
                                                             'require_gui_smoke': True}})
    response = require(status, 200, response)
    run_id = response.get('workflow_run_id')
    confirmed = checked_run(api, run_id, branch, sha, workflow_id)
    return run_id, confirmed['html_url']


def save_state(api: Api, state: dict, blob_sha: str, base_sha: str) -> str:
    empty_branch_sha = state.pop('_empty_branch_sha', '')
    if not blob_sha:
        if empty_branch_sha:
            if empty_branch_sha != base_sha:
                raise ValueError('Empty state branch is not at the candidate main base; refusing to adopt it')
        else:
            status, body = api.request('POST', TARGET, 'git/refs', {'ref': 'refs/heads/' + STATE_BRANCH, 'sha': base_sha})
            if status != 201:
                raise RuntimeError('State branch creation failed; the confirmed dispatch can be recovered next run: ' + str(body))
    payload = {'message': 'Record confirmed official release build dispatch', 'branch': STATE_BRANCH,
               'content': base64.b64encode((json.dumps(state, indent=2) + '\n').encode()).decode()}
    if blob_sha:
        payload['sha'] = blob_sha
    status, body = api.request('PUT', TARGET, 'contents/' + STATE_FILE, payload)
    body = require(status, 200 if blob_sha else 201, body)
    return checked_sha(body.get('content', {}).get('sha'))


def summary(message: str) -> None:
    print(message, flush=True)
    path = os.environ.get('GITHUB_STEP_SUMMARY')
    if path:
        with open(path, 'a', encoding='utf-8') as stream:
            stream.write(message + '\n\n')


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', type=Path, required=True)
    parser.add_argument('--candidate-root', type=Path)
    parser.add_argument('--probe', action='store_true')
    args = parser.parse_args()
    if (os.environ.get('GITHUB_ACTIONS') != 'true' or os.environ.get('GITHUB_REPOSITORY') != TARGET
            or os.environ.get('GITHUB_REF') != 'refs/heads/main'
            or os.environ.get('GITHUB_EVENT_NAME') not in ('schedule', 'workflow_dispatch')):
        raise ValueError('Watcher runs only in its authorized repository schedule/manual workflow')
    if git(args.repo.resolve(), 'rev-parse', 'HEAD') != checked_sha(os.environ.get('GITHUB_SHA', '')):
        raise ValueError('Trusted tools checkout differs from the immutable workflow source')
    baseline = json.loads((Path(__file__).parent / 'baseline.json').read_text(encoding='utf-8'))
    api = Api()
    release = snapshot(api)
    state, blob_sha = load_state(api, baseline)
    needed = needs_build(state, release)
    if args.probe:
        output = os.environ.get('GITHUB_OUTPUT')
        if not output:
            raise ValueError('Probe output path must be supplied by Actions')
        with open(output, 'a', encoding='utf-8') as stream:
            stream.write('needs_build=' + str(needed).lower() + '\n')
        summary('Official ' + release.tag + (' needs candidate checks.' if needed else ' is already baselined or dispatched.'))
        return 0
    if not needed:
        summary('Official ' + release.tag + ' is already baselined or dispatched; no files or builds changed.')
        return 0
    if args.candidate_root is None:
        raise ValueError('A new external candidate workspace is required')
    for tool in ('update_menu_paths.py', 'sync_sources.py', 'check_translations.py'):
        if not (args.repo / 'tools/localization' / tool).is_file():
            raise ValueError('Trusted main is missing the required localization tool: ' + tool)
    branch, sha, tag, base, blocked = prepare_candidate(args.repo.resolve(), args.candidate_root.resolve(), release)
    pending = {**asdict(release), 'candidate_branch': branch, 'candidate_sha': sha,
               'release_tag': tag, 'status': 'needs_translation' if blocked else 'ready', 'reason': blocked}
    if state['pending'].get(str(release.release_id)) != pending:
        state['pending'][str(release.release_id)] = pending
        blob_sha = save_state(api, state, blob_sha, base)
    if blocked:
        summary('Manual translation is required: [missing messages](https://github.com/' + TARGET + '/blob/' + branch
                + '/ci/upstream-release-watch/translations/missing-translations.md) on candidate `' + branch + '`. '
                'Translate its Chinese catalog, commit to the same candidate branch, then run this watcher again from main.')
        raise RuntimeError('Candidate `' + branch + '` is pending manual translation; no build was dispatched. '
                           'See its ci/upstream-release-watch/translations/missing-translations.md and catalog-check.json reports.')
    if snapshot(api) != release:
        raise ValueError('Official latest release/tag changed while preparing; no dispatch was made')
    verify_branch_head(args.candidate_root.resolve(), branch, sha)
    run_id, run_url = dispatch(api, branch, sha, tag)
    state['handled'][str(release.release_id)] = {**asdict(release), 'candidate_branch': branch,
        'candidate_sha': sha, 'release_tag': tag, 'workflow_run_id': run_id,
        'workflow_run_url': run_url, 'confirmed_at': datetime.now(timezone.utc).isoformat()}
    state['pending'].pop(str(release.release_id), None)
    save_state(api, state, blob_sha, base)
    summary(f'Confirmed [{tag} build]({run_url}) from `{sha}` on `{branch}`. '
            'The existing three-platform checks create a draft only after success; main is unchanged.')
    return 0


if __name__ == '__main__':
    # Legacy candidate/state helpers remain importable for old offline fixtures.
    # The executable entry point uses main-only read/dispatch automation.
    from dispatch import main as dispatch_main
    sys.exit(dispatch_main())
