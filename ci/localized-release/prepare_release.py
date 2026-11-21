"""Resolve a release commit, lint it, and optionally create one immutable tag ref."""
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import json
import os
import pathlib
import re
import subprocess
import sys

from official_release import release_identity

API_VERSION = '2026-03-10'
TAG_PATTERN = re.compile(r'v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)-zh\.([1-9][0-9]*)')
# Keep standalone-repository products newer than the previously published build 28.
PRODUCT_BUILD_NUMBER_OFFSET = 28


def checked_sha(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r'[0-9a-f]{40}', value):
        raise ValueError('Expected a full lowercase Git SHA-1')
    return value


def checked_tag(value: str, version: str | None = None) -> str:
    if len(value) > 100 or not TAG_PATTERN.fullmatch(value):
        raise ValueError('Release tag must have the form vX.Y.Z-zh.N (N > 0)')
    if version is not None and not value.startswith('v' + version + '-zh.'):
        raise ValueError(f'Release tag numeric base must match CMakeLists.txt ({version})')
    return value


def git(repo: pathlib.Path, *arguments: str) -> str:
    environment = os.environ.copy()
    if os.name == 'nt':
        environment['MSYS'] = (environment.get('MSYS', '') + ' noglob').strip()
    return subprocess.check_output(['git', '-C', str(repo), *arguments],
                                   text=True, encoding='utf-8', timeout=120, env=environment).strip()


def base_version(repo: pathlib.Path) -> str:
    text = (repo / 'CMakeLists.txt').read_text(encoding='utf-8')
    parts = []
    for name in ('RV_MAJOR_VERSION', 'RV_MINOR_VERSION', 'RV_REVISION_NUMBER'):
        match = re.search(r'SET\(' + name + r'\s+"(\d+)"', text)
        if not match:
            raise ValueError(f'Cannot read {name} from CMakeLists.txt')
        parts.append(match.group(1))
    return '.'.join(parts)


def validate_checkout(repo: pathlib.Path, source_sha: str, tag: str = '', tag_object: str = '', *, lint: bool = False) -> None:
    checked_sha(source_sha)
    if git(repo, 'rev-parse', '--verify', 'HEAD') != source_sha:
        raise ValueError('Checkout HEAD differs from the resolved immutable source SHA')
    if tag:
        checked_tag(tag)
        release_identity(repo, tag, base_version(repo), refresh=True)
        if tag_object:
            checked_sha(tag_object)
            if git(repo, 'rev-parse', '--verify', 'refs/tags/' + tag) != tag_object:
                raise ValueError('Local release tag object changed after preparation')
            if git(repo, 'rev-parse', '--verify', 'refs/tags/' + tag + '^{commit}') != source_sha:
                raise ValueError('Local release tag does not identify the resolved source commit')
    if lint:
        checker = repo / 'tools' / 'localization' / 'check_translations.py'
        if not checker.is_file():
            raise FileNotFoundError('Selected source lacks tools/localization/check_translations.py')
        environment = os.environ.copy()
        for key in ('GH_TOKEN', 'GITHUB_TOKEN'):
            environment.pop(key, None)
        environment['PYTHONDONTWRITEBYTECODE'] = '1'
        subprocess.run([sys.executable, str(checker)], cwd=repo, env=environment, check=True, timeout=120)


class ApiError(RuntimeError):
    def __init__(self, status: int, message: str):
        self.status = status
        super().__init__(f'GitHub API HTTP {status}: {message}')


def parse_response(stdout: str, returncode: int) -> tuple[int, dict]:
    parts = re.split(r'\r?\n\r?\n', stdout, maxsplit=1)
    if len(parts) != 2:
        raise RuntimeError('gh api did not return an HTTP status/header block; no tag mutation may be inferred')
    match = re.fullmatch(r'HTTP/\S+\s+(\d{3})(?:\s+.*)?', parts[0].splitlines()[0])
    if not match:
        raise RuntimeError('Unrecognized gh api HTTP status line')
    status = int(match.group(1))
    try:
        body = json.loads(parts[1])
    except json.JSONDecodeError:
        raise ApiError(status, 'Response body is not valid JSON')
    if not isinstance(body, dict):
        raise ApiError(status, 'Expected an exact object response')
    if returncode and 200 <= status < 300:
        raise ApiError(status, 'gh api failed despite a success HTTP status')
    return status, body


class GitHubApi:
    def __init__(self, repository: str, *, read_only: bool = True):
        if (not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository)
                or any(part in ('.', '..') for part in repository.split('/'))):
            raise ValueError('Invalid GITHUB_REPOSITORY')
        self.prefix = 'repos/' + repository + '/git/'
        self.read_only = read_only

    def request(self, method: str, suffix: str, payload: dict | None = None) -> tuple[int, dict]:
        # No arbitrary response URL, ref update, deletion, or force endpoint is accepted.
        allowed_get = re.fullmatch(r'ref/tags/(v[0-9.]+-zh\.[0-9]+)|tags/[0-9a-f]{40}', suffix)
        if method == 'GET' and allowed_get:
            pass
        elif method == 'POST' and suffix == 'refs' and not self.read_only:
            if not payload or set(payload) != {'ref', 'sha'} or not payload['ref'].startswith('refs/tags/'):
                raise ValueError('Only a complete lightweight release-tag creation payload is allowed')
            checked_tag(payload['ref'].removeprefix('refs/tags/'))
            checked_sha(payload['sha'])
        else:
            raise ValueError('Only fixed read endpoints and authorized create-ref POST are supported')
        command = ['gh', 'api', '--hostname', 'github.com', '--include', '--method', method,
                   '-H', 'Accept: application/vnd.github+json',
                   '-H', 'X-GitHub-Api-Version: ' + API_VERSION, self.prefix + suffix]
        if payload is not None:
            command.extend(['--input', '-'])
        process = subprocess.run(command, input=json.dumps(payload) if payload is not None else None,
                                 text=True, encoding='utf-8', capture_output=True, timeout=45)
        return parse_response(process.stdout, process.returncode)


@dataclass(frozen=True)
class TagState:
    tag: str
    object_sha: str
    commit_sha: str


def resolve_tag(api: GitHubApi, tag: str) -> TagState | None:
    checked_tag(tag)
    status, body = api.request('GET', 'ref/tags/' + tag)
    if status == 404:
        return None
    if status != 200:
        raise ApiError(status, str(body.get('message', 'Cannot read exact tag reference')))
    if body.get('ref') != 'refs/tags/' + tag:
        raise ValueError('API returned a different reference')
    current = body.get('object', {})
    original_sha = checked_sha(current.get('sha'))
    seen = set()
    for _ in range(8):
        sha = checked_sha(current.get('sha'))
        kind = current.get('type')
        if kind == 'commit':
            return TagState(tag, original_sha, sha)
        if kind != 'tag' or sha in seen:
            raise ValueError('Release tag must peel to a commit without cycles')
        seen.add(sha)
        status, body = api.request('GET', 'tags/' + sha)
        if status != 200:
            raise ApiError(status, str(body.get('message', 'Cannot read annotated tag object')))
        if body.get('sha') != sha:
            raise ValueError('API returned a different annotated tag object')
        current = body.get('object', {})
    raise ValueError('Annotated tag nesting exceeds the bounded resolver depth')


def fetch_exact_tag(repo: pathlib.Path, state: TagState) -> None:
    # Full ref name prevents a same-named branch from being fetched instead.
    git(repo, 'fetch', '--no-tags', 'origin', 'refs/tags/' + state.tag)
    if git(repo, 'rev-parse', '--verify', 'FETCH_HEAD') != state.object_sha:
        raise ValueError('Fetched exact tag object differs from the API snapshot')
    if git(repo, 'rev-parse', '--verify', 'FETCH_HEAD^{commit}') != state.commit_sha:
        raise ValueError('Fetched exact tag commit differs from the API snapshot')


def plan_release(repo: pathlib.Path, api: GitHubApi, tag: str, create_tag: bool) -> dict:
    event = os.environ.get('GITHUB_EVENT_NAME')
    trigger_sha = checked_sha(os.environ.get('GITHUB_SHA', ''))
    trigger_commit = checked_sha(git(repo, 'rev-parse', '--verify', trigger_sha + '^{commit}'))
    if git(repo, 'rev-parse', '--verify', 'HEAD') != trigger_commit:
        raise ValueError('Preflight checkout is not the immutable workflow trigger commit')
    if event not in ('workflow_dispatch', 'push'):
        raise ValueError('Only manual dispatch and version-tag push are supported')
    source_sha, mode, tag_object = trigger_commit, 'branch', ''
    if tag:
        checked_tag(tag)
        state = resolve_tag(api, tag)
        if event == 'push':
            if state is None or state.commit_sha != trigger_commit:
                raise ValueError('Pushed tag no longer identifies this workflow trigger commit')
            mode = 'push'
        elif create_tag:
            # New/manual tag creation is pinned to github.sha, not a re-read branch head.
            if trigger_sha != trigger_commit:
                raise ValueError('Creating a tag requires a commit SHA workflow source')
            if state is not None and state.commit_sha != trigger_commit:
                raise ValueError('Existing tag points elsewhere; it will not be moved. Clear create_tag only to rebuild that existing tag.')
            mode = 'create'
        else:
            if state is None:
                raise ValueError('Tag does not exist and create_tag is disabled')
            source_sha, mode = state.commit_sha, 'existing'
        if state is not None:
            fetch_exact_tag(repo, state)
            tag_object = state.object_sha
        if source_sha != trigger_commit:
            git(repo, 'checkout', '--detach', source_sha)
    elif event == 'push':
        raise ValueError('A tag-push run requires a release tag')
    validate_checkout(repo, source_sha, tag, lint=True)
    return {'source_sha': source_sha, 'release_tag': tag, 'tag_mode': mode,
            'tag_object_sha': tag_object, 'tag_created': 'false'}


def verify_remote(api: GitHubApi, tag: str, source_sha: str, expected_object: str = '') -> TagState:
    state = resolve_tag(api, tag)
    if state is None or state.commit_sha != checked_sha(source_sha):
        raise ValueError('Remote release tag is missing or no longer points to the prepared source commit')
    if expected_object and state.object_sha != checked_sha(expected_object):
        raise ValueError('Remote release tag object changed after preparation')
    return state


def apply_release(repo: pathlib.Path, api: GitHubApi, tag: str, source_sha: str, mode: str, expected_object: str = '') -> dict:
    validate_checkout(repo, source_sha, tag)
    if mode == 'branch':
        if tag or expected_object:
            raise ValueError('Artifacts-only mode cannot create or resolve a release tag')
        return {'source_sha': source_sha, 'release_tag': '', 'tag_object_sha': '', 'tag_created': 'false'}
    checked_tag(tag)
    release_identity(repo, tag, base_version(repo), refresh=True)
    created = False
    state = resolve_tag(api, tag)
    if mode == 'create':
        if (os.environ.get('GITHUB_ACTIONS') != 'true' or os.environ.get('GITHUB_EVENT_NAME') != 'workflow_dispatch'
                or os.environ.get('ORV_CREATE_TAG') != 'true' or source_sha != checked_sha(os.environ.get('GITHUB_SHA', ''))):
            raise ValueError('Tag creation is allowed only for an authorized dispatch at its immutable github.sha')
        if state is None:
            if expected_object:
                raise ValueError('Existing tag disappeared after preflight; it will not be recreated silently')
            status, body = api.request('POST', 'refs', {'ref': 'refs/tags/' + tag, 'sha': source_sha})
            if status == 201:
                created_object = body.get('object', {})
                if (body.get('ref') != 'refs/tags/' + tag or not isinstance(created_object, dict)
                        or created_object.get('type') != 'commit' or created_object.get('sha') != source_sha):
                    raise ValueError('Created ref response does not identify the requested lightweight tag commit')
                created = True
            elif status != 422:
                raise ApiError(status, str(body.get('message', 'Cannot create release tag')))
            # 422 can mean validation/spam too. Accept a race only after exact re-resolution.
            state = verify_remote(api, tag, source_sha)
        else:
            state = verify_remote(api, tag, source_sha, expected_object)
    elif mode in ('existing', 'push'):
        state = verify_remote(api, tag, source_sha, expected_object)
    else:
        raise ValueError('Unsupported release preparation mode')
    return {'source_sha': source_sha, 'release_tag': tag,
            'tag_object_sha': state.object_sha, 'tag_created': str(created).lower()}


def output_values(values: dict) -> None:
    path = os.environ.get('GITHUB_OUTPUT')
    if not path:
        raise RuntimeError('GITHUB_OUTPUT must be supplied by Actions')
    with open(path, 'a', encoding='utf-8') as stream:
        for name, value in values.items():
            if '\n' in value or '\r' in value:
                raise ValueError('Output values must be single-line')
            stream.write(name + '=' + value + '\n')
    print(json.dumps(values, indent=2))


def legacy_main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('phase', choices=('plan', 'apply', 'verify-local', 'verify-remote'))
    parser.add_argument('--repo', type=pathlib.Path, default=pathlib.Path.cwd())
    parser.add_argument('--tag', default='')
    parser.add_argument('--source-sha', default='')
    parser.add_argument('--tag-object-sha', default='')
    parser.add_argument('--mode', choices=('branch', 'create', 'existing', 'push'))
    parser.add_argument('--create-tag', choices=('true', 'false'), default='false')
    args = parser.parse_args()
    repo = args.repo.resolve()
    if args.phase == 'verify-local':
        validate_checkout(repo, args.source_sha, args.tag, args.tag_object_sha)
        return
    api = GitHubApi(os.environ.get('GITHUB_REPOSITORY', ''), read_only=args.phase != 'apply' or args.mode != 'create')
    if args.phase == 'plan':
        output_values(plan_release(repo, api, args.tag, args.create_tag == 'true'))
    elif args.phase == 'apply':
        if not os.environ.get('GITHUB_OUTPUT'):
            raise RuntimeError('Actions output path is required before tag creation')
        output_values(apply_release(repo, api, args.tag, args.source_sha, args.mode, args.tag_object_sha))
    else:
        validate_checkout(repo, args.source_sha, args.tag, args.tag_object_sha)
        print(json.dumps(asdict(verify_remote(api, args.tag, args.source_sha, args.tag_object_sha)), indent=2))


def product_build_number(workflow_run_number) -> int:
    from official_workspace import build_number
    return build_number(build_number(workflow_run_number) + PRODUCT_BUILD_NUMBER_OFFSET)


def trusted_context(repo: pathlib.Path) -> tuple[str, int]:
    from official_workspace import TARGET, build_number
    if (os.environ.get('GITHUB_ACTIONS') != 'true' or os.environ.get('GITHUB_REPOSITORY') != TARGET
            or os.environ.get('GITHUB_EVENT_NAME') != 'workflow_dispatch'
            or os.environ.get('GITHUB_REF') != 'refs/heads/main'):
        raise ValueError('Official release jobs must run by dispatch from maintenance main')
    number = product_build_number(os.environ.get('GITHUB_RUN_NUMBER', ''))
    if 'ORV_BUILD_NUMBER' in os.environ and build_number(os.environ['ORV_BUILD_NUMBER']) != number:
        raise ValueError('Product build number differs from this workflow run plus the fixed migration offset')
    return checked_sha(os.environ.get('GITHUB_SHA', '')), number


def publish_assets(repo: pathlib.Path, assets: pathlib.Path, plan: dict, api) -> None:
    """Publish only after the complete cohort is uploaded and independently verified."""
    from official_workspace import TARGET, build_number, checked_tag, release_from_plan, official_release
    from publication_tags import verify_publication_tag
    from release_common import sha256
    release = release_from_plan(plan)
    number = build_number(plan['build_number'])

    def verify_source():
        if official_release(api, release.tag) != release:
            raise ValueError('Official release changed before release publication')
        verify_publication_tag(api, release, checked_sha(plan['publication_commit']))

    def validate_release(record, expected_id=None):
        if (not isinstance(record, dict) or record.get('tag_name') != release.tag
                or type(record.get('id')) is not int or record['id'] <= 0
                or type(record.get('draft')) is not bool or type(record.get('prerelease')) is not bool
                or (expected_id is not None and record['id'] != expected_id)):
            raise ValueError('Cannot verify the same release identity and publication state')
        prefix = f'https://github.com/{TARGET}/releases/tag/'
        allowed = re.escape(prefix + release.tag)
        if record['draft']:
            allowed += '|' + re.escape(prefix) + r'untagged-[0-9a-f]{20,64}'
        if not isinstance(record.get('html_url'), str) or not re.fullmatch(allowed, record['html_url']):
            raise ValueError('Release URL does not identify this repository and publication state')
        return record

    def read_release(expected_id):
        status, record = api.request('GET', TARGET, f'releases/{expected_id}')
        if status != 200:
            raise ValueError('Cannot read the bound release identity')
        return validate_release(record, expected_id)

    def discover_release():
        # The by-tag REST endpoint finds published releases only. Authenticated
        # listings include drafts; identify one exact pending tag, then bind ID.
        found, seen = [], set()
        for page in range(1, 101):
            status, batch = api.request('GET', TARGET, f'releases?per_page=100&page={page}')
            if status != 200 or not isinstance(batch, list) or len(batch) > 100:
                raise ValueError('Cannot inspect published and draft releases')
            for record in batch:
                if (not isinstance(record, dict) or type(record.get('id')) is not int or record['id'] <= 0
                        or not isinstance(record.get('tag_name'), str) or record['id'] in seen):
                    raise ValueError('Release listing has invalid or duplicate identities')
                seen.add(record['id'])
                if record['tag_name'] == release.tag:
                    found.append(validate_release(record)['id'])
            if len(batch) < 100:
                if len(found) > 1:
                    raise ValueError('Multiple releases identify the requested tag; refusing ambiguous publication')
                return read_release(found[0]) if found else None
        raise ValueError('Release discovery pagination exceeded its safety bound')

    def read_assets(release_id):
        result = {}
        for page in range(1, 101):
            status, batch = api.request('GET', TARGET, f'releases/{release_id}/assets?per_page=100&page={page}')
            if status != 200 or not isinstance(batch, list) or len(batch) > 100:
                raise ValueError('Cannot inspect existing release assets')
            for record in batch:
                name = record.get('name') if isinstance(record, dict) else None
                if not isinstance(name, str) or name in result:
                    raise ValueError('Existing release has invalid or duplicate asset names')
                result[name] = record
            if len(batch) < 100:
                return result
        raise ValueError('Existing release asset pagination exceeded its safety bound')

    verify_source()
    names = sorted(path.name for path in assets.glob('OpenRV-*'))
    if not names or any((assets / name).is_symlink() or not (assets / name).is_file() for name in names):
        raise ValueError('Release assets must be immediate regular files')
    base = f'OpenRV-{release.tag[1:]}-build.{number}'
    required = {base + '-source.provenance.json', base + '-generated-localization.patch.zip', base + '-source.sha256.txt'}
    for platform, suffixes in (
            ('Windows-x64', ('-Setup.exe', '-Portable.zip', '.build.json', '.sha256.txt')),
            ('macOS-x86_64', ('.dmg', '.zip', '.build.json', '.sha256.txt')),
            ('macOS-arm64', ('.dmg', '.zip', '.build.json', '.sha256.txt'))):
        required.update(base + '-' + platform + suffix for suffix in suffixes)
    if set(names) != required:
        raise ValueError('Publication requires the exact fifteen assets of the verified build')
    notes = assets / 'RELEASE-NOTES.md'
    if notes.is_symlink() or not notes.is_file():
        raise ValueError('Verified release notes must be an immediate regular file')
    notes_text = notes.read_text(encoding='utf-8')
    title = 'OpenRV ' + release.tag + ' 中英文版'
    expected = {name: ((assets / name).stat().st_size, 'sha256:' + sha256(assets / name)) for name in names}

    def verify_asset(name, record, release_record):
        allowed_urls = {f'https://github.com/{TARGET}/releases/download/{release.tag}/{name}'}
        if release_record['draft']:
            # A draft's temporary download tag is bound to its validated HTML
            # URL and ID. It must become the canonical tag URL after publishing.
            pending_tag = release_record['html_url'].rsplit('/', 1)[1]
            allowed_urls.add(f'https://github.com/{TARGET}/releases/download/{pending_tag}/{name}')
        if (not isinstance(record, dict) or record.get('state') != 'uploaded'
                or type(record.get('size')) is not int or record['size'] != expected[name][0]
                or record.get('digest') != expected[name][1]
                or record.get('browser_download_url') not in allowed_urls):
            raise ValueError('Asset lacks exact verified content; it will not be overwritten or published: ' + name)

    existing = discover_release()
    command = ['gh', 'release']
    if existing is None:
        subprocess.run(command + ['create', release.tag, '--repo', TARGET, '--verify-tag', '--draft',
                                  '--latest=false', '--title', title, '--notes-file', str(notes)], check=True, timeout=1800)
        existing = discover_release()
        if existing is None or existing['draft'] is not True:
            raise ValueError('A new release must remain a draft until asset verification finishes')
    release_id = existing['id']
    existing_assets = read_assets(release_id)
    verified = set()
    for name in set(names).intersection(existing_assets):
        verify_asset(name, existing_assets[name], existing)
        verified.add(name)
    # Manifests are uploaded last so readers only discover a verified completed set.
    missing = [name for name in names if name not in verified]
    batches = [[name for name in missing if not name.endswith('.build.json')],
               [name for name in missing if name.endswith('.build.json')]]
    for batch in batches:
        if not batch:
            continue
        subprocess.run(command + ['upload', release.tag, '--repo', TARGET]
                       + [str(assets / name) for name in batch], check=True, timeout=1800)

    current = read_release(release_id)
    uploaded = read_assets(release_id)
    for name in names:
        verify_asset(name, uploaded.get(name), current)
    higher_manifest = re.compile(re.escape('OpenRV-' + release.tag[1:])
                                 + r'-build\.([1-9][0-9]*)-(?:Windows-x64|macOS-(?:arm64|x86_64))\.build\.json')
    if any(record.get('state') == 'uploaded' and (match := higher_manifest.fullmatch(name))
           and build_number(match.group(1)) > number for name, record in uploaded.items()):
        verify_source()
        if current['draft'] is False and current['prerelease'] is False:
            return  # Retain the stable release's newer download links and Latest state.
        raise ValueError('A newer build exists in this draft; publish its verified cohort instead')

    status, latest = api.request('GET', TARGET, 'releases/latest')
    if status == 404:
        make_latest = True
    elif status == 200 and isinstance(latest, dict):
        latest_tag = checked_tag(latest.get('tag_name'))
        if (type(latest.get('id')) is not int or latest['id'] <= 0 or latest.get('draft') is not False
                or latest.get('prerelease') is not False
                or latest.get('html_url') != f'https://github.com/{TARGET}/releases/tag/{latest_tag}'
                or (latest_tag == release.tag and latest['id'] != release_id)):
            raise ValueError('Cannot verify the current latest stable release')
        make_latest = tuple(map(int, release.tag[1:].split('.'))) >= tuple(map(int, latest_tag[1:].split('.')))
    else:
        raise ValueError('Cannot inspect the current latest stable release')
    verify_source()
    if (current['draft'] is False and current['prerelease'] is False and current.get('name') == title
            and current.get('body') == notes_text and (not make_latest or (latest or {}).get('id') == release_id)):
        return  # An exact completed resume needs no uploads or release edits.
    subprocess.run(command + ['edit', release.tag, '--repo', TARGET, '--tag', release.tag, '--verify-tag', '--draft=false',
                              '--prerelease=false', '--latest=' + str(make_latest).lower(),
                              '--title', title, '--notes-file', str(notes)], check=True, timeout=1800)
    published = read_release(release_id)
    if (published['draft'] is not False or published['prerelease'] is not False
            or published.get('name') != title or published.get('body') != notes_text):
        raise ValueError('Release publication state or notes could not be verified')
    published_assets = read_assets(release_id)
    for name in names:
        verify_asset(name, published_assets.get(name), published)


def main() -> None:
    from official_workspace import (OfficialApi, checked_tag as official_tag, digest, materialize,
                                    official_release, prepare_workspace, read_input,
                                    verify_composition, verify_materialized)
    from publication_tags import resolve_publication_commit, sync_publication_tag, verify_publication_tag
    parser = argparse.ArgumentParser(description='Build only published official tags using maintenance-main patches')
    parser.add_argument('phase', choices=('preflight', 'sync-tag', 'materialize', 'verify', 'verify-remote', 'publish'))
    parser.add_argument('--repo', type=pathlib.Path, default=pathlib.Path.cwd())
    parser.add_argument('--official-tag', default='')
    parser.add_argument('--workspace', type=pathlib.Path)
    parser.add_argument('--output', type=pathlib.Path, default=pathlib.Path('_release_work/preflight'))
    parser.add_argument('--input', type=pathlib.Path, default=pathlib.Path('_release_work/build-input'))
    parser.add_argument('--plan-sha256', default='')
    parser.add_argument('--assets', type=pathlib.Path)
    args = parser.parse_args(); repo = args.repo.resolve()
    snapshot, number = trusted_context(repo)
    if args.phase != 'preflight' and 'ORV_BUILD_NUMBER' not in os.environ:
        raise ValueError('The preflight product build number is required in ORV_BUILD_NUMBER')
    if args.phase == 'preflight':
        if git(repo, 'rev-parse', 'HEAD') != snapshot or git(repo, 'status', '--porcelain'):
            raise ValueError('Preflight requires a clean immutable maintenance-main checkout')
        if args.workspace is None:
            raise ValueError('Fresh external preflight workspace is required')
        api = OfficialApi()
        release = official_release(api, args.official_tag)
        anchor = resolve_publication_commit(repo, snapshot, api, release)
        plan, plan_hash = prepare_workspace(repo, args.workspace, args.output, release, number,
                                            publication_commit=anchor)
        output_values({'source_sha': snapshot, 'official_sha': release.commit_sha,
                       'release_tag': release.tag, 'plan_sha256': plan_hash, 'build_number': str(number)})
        return
    if not args.plan_sha256:
        raise ValueError('The immutable preflight plan SHA256 is required')
    plan, delta, raw = read_input(args.input.resolve(), args.plan_sha256)
    verify_composition(repo, plan, delta, expected_snapshot=snapshot, expected_build=number)
    if args.phase == 'materialize':
        if pathlib.Path(os.environ.get('GITHUB_WORKSPACE', '')).resolve() != repo:
            raise ValueError('Only the disposable Actions workspace may be materialized')
        materialize(repo, args.input.resolve(), args.plan_sha256, snapshot, number)
    elif args.phase == 'sync-tag':
        if git(repo, 'rev-parse', 'HEAD') != snapshot:
            raise ValueError('Tag sync must use the immutable maintenance-main checkout')
        from official_workspace import release_from_plan
        api = OfficialApi(write=True)
        release = release_from_plan(plan)
        if official_release(api, release.tag) != release:
            raise ValueError('Official source identity changed before recording the publication tag')
        print(json.dumps({'tagCreated': sync_publication_tag(repo, plan, api),
                          'tag': plan['official']['tag']}))
    elif args.phase == 'verify-remote':
        from official_workspace import release_from_plan
        release = release_from_plan(plan)
        api = OfficialApi()
        if official_release(api, release.tag) != release:
            raise ValueError('Official source identity changed after preflight')
        verify_publication_tag(api, release, checked_sha(plan['publication_commit']))
    else:
        verify_materialized(repo, plan, delta, expected_snapshot=snapshot, expected_build=number)
        if args.phase == 'publish':
            if args.assets is None:
                raise ValueError('Verified release assets are required')
            from assemble_release import assemble
            assemble(repo, args.assets.resolve(), plan['official']['tag'])
            publish_assets(repo, args.assets.resolve(), plan, OfficialApi())


if __name__ == '__main__':
    main()
