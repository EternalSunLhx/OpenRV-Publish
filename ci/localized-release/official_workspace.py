"""Compose an official tag plus an immutable maintenance-main patch in local workspaces."""
from __future__ import annotations

from dataclasses import asdict
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from urllib.error import HTTPError
from urllib.request import Request, urlopen

UPSTREAM = 'AcademySoftwareFoundation/OpenRV'
TARGET = 'EternalSunLhx/OpenRV-Publish'
PLAN = '_release_work/official-build-plan.json'
DELTA = '_release_work/generated-localization.patch'
GENERATED_PATHS = frozenset(('src/lib/app/RvCommon/RvCommon/RvMenuPaths.h',
                             'src/lib/app/RvCommon/qrc/translations/i18n_zh.ts'))
OFFICIAL_TAG = re.compile(r'v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)')
SHA256 = re.compile(r'[0-9a-f]{64}')


def watcher():
    name = 'openrv_official_workspace_watch'
    if name not in sys.modules:
        path = Path(__file__).resolve().parents[1] / 'upstream-release-watch/watch.py'
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


def checked_tag(tag: str) -> str:
    if not isinstance(tag, str) or not OFFICIAL_TAG.fullmatch(tag):
        raise ValueError('Only official vMAJOR.MINOR.PATCH tags are supported')
    return tag


def build_number(value) -> int:
    if isinstance(value, bool) or not re.fullmatch(r'[1-9][0-9]*', str(value)) or int(value) > 18446744073709551615:
        raise ValueError('Build number must be a positive unsigned 64-bit integer')
    return int(value)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()




class OfficialApi:
    """Fixed reads plus narrowly shaped publication tag objects and references."""
    def __init__(self, *, write=False):
        self.write = write

    def request(self, method: str, repository: str, suffix: str, payload=None):
        if repository not in (UPSTREAM, TARGET):
            raise ValueError('Unknown repository')
        reads = (r'releases/latest|releases\?per_page=100&page=[1-9][0-9]*|releases/tags/v[0-9.]+|git/ref/tags/v[0-9.]+|git/tags/[0-9a-f]{40}|'
                 r'releases/tags/v[0-9.]+|releases/[1-9][0-9]*|releases/[1-9][0-9]*/assets(?:\?[^\s]+)?')
        if method == 'GET' and payload is None and re.fullmatch(reads, suffix):
            pass
        elif method == 'POST' and repository == TARGET and self.write and suffix == 'git/tags':
            if (not isinstance(payload, dict) or set(payload) != {'tag', 'message', 'object', 'type'}
                    or payload['type'] != 'commit' or not isinstance(payload['message'], str)
                    or not 0 < len(payload['message'].encode('utf-8')) <= 16384):
                raise ValueError('Only a bounded annotated publication tag at an owned commit may be created')
            checked_tag(payload['tag']); watcher().checked_sha(payload['object'])
        elif method == 'POST' and repository == TARGET and self.write and suffix == 'git/refs':
            if not isinstance(payload, dict) or set(payload) != {'ref', 'sha'} or not isinstance(payload['ref'], str):
                raise ValueError('Only a publication tag reference may be created')
            if not payload['ref'].startswith('refs/tags/'):
                raise ValueError('Branch writes and updates are forbidden')
            checked_tag(payload['ref'].removeprefix('refs/tags/')); watcher().checked_sha(payload['sha'])
        else:
            raise ValueError('Only fixed reads and authorized publication tag creation are permitted')
        body = json.dumps(payload).encode() if payload is not None else None
        headers = {'Accept': 'application/vnd.github+json', 'X-GitHub-Api-Version': watcher().API_VERSION,
                   'User-Agent': 'OpenRV-official-workspace', 'Content-Type': 'application/json'}
        if os.environ.get('GH_TOKEN'):
            headers['Authorization'] = 'Bearer ' + os.environ['GH_TOKEN']
        request = Request('https://api.github.com/repos/' + repository + '/' + suffix,
                          data=body, headers=headers, method=method)
        try:
            response = urlopen(request, timeout=45)
        except HTTPError as error:
            response = error
        with response:
            raw = response.read()
            return response.status, json.loads(raw) if raw else None


def tag_snapshot(api, repository: str, tag: str):
    checked_tag(tag)
    status, ref = api.request('GET', repository, 'git/ref/tags/' + tag)
    if status == 404:
        return None
    watcher().require(status, 200, ref)
    if ref.get('ref') != 'refs/tags/' + tag:
        raise ValueError('Exact official tag lookup returned another ref')
    obj = ref.get('object', {})
    original = watcher().checked_sha(obj.get('sha'))
    seen = set()
    for _ in range(8):
        sha = watcher().checked_sha(obj.get('sha'))
        if obj.get('type') == 'commit':
            return original, sha
        if obj.get('type') != 'tag' or sha in seen:
            raise ValueError('Official tag must peel to a commit')
        seen.add(sha)
        status, body = api.request('GET', repository, 'git/tags/' + sha)
        watcher().require(status, 200, body)
        if body.get('sha') != sha:
            raise ValueError('Annotated tag object changed')
        obj = body.get('object', {})
    raise ValueError('Annotated tag nesting exceeds the bounded resolver')


def official_release(api, tag=''):
    suffix = 'releases/tags/' + checked_tag(tag) if tag else 'releases/latest'
    status, body = api.request('GET', UPSTREAM, suffix)
    watcher().require(status, 200, body)
    if (type(body.get('id')) is not int or body['id'] <= 0 or body.get('draft') is not False
            or body.get('prerelease') is not False):
        raise ValueError('Only published official stable releases may be packaged')
    actual = checked_tag(body.get('tag_name'))
    if tag and tag != actual:
        raise ValueError('Official release lookup returned another tag')
    published = body.get('published_at')
    if not isinstance(published, str) or not re.fullmatch(r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ', published):
        raise ValueError('Official release publication timestamp is invalid')
    resolved = tag_snapshot(api, UPSTREAM, actual)
    if resolved is None:
        raise ValueError('Official stable release has no exact tag')
    return watcher().Release(body['id'], actual, *resolved, published)


def ensure_local_tag(repo: Path, release, *, lightweight=False) -> None:
    w = watcher(); tag = checked_tag(release.tag)
    refs = ['refs/openrv-upstream/tags/' + tag]
    if not lightweight:
        refs.append('refs/tags/' + tag)  # Compatibility for isolated historical schema-3 fixtures only.
    for ref in refs:
        result = w.git_bytes(repo, 'rev-parse', '--verify', ref, check=False)
        if result.returncode:
            w.git(repo, 'update-ref', ref, release.object_sha, '0' * 40)
        elif result.stdout.decode().strip() != release.object_sha:
            raise ValueError('Existing local upstream source ref differs from its original object')
        if w.git(repo, 'rev-parse', ref + '^{commit}') != release.commit_sha:
            raise ValueError('Local upstream source ref does not identify its original commit')


def validate_plan(plan: dict) -> None:
    w = watcher()
    kinds = {3: 'official-tag-workspace', 4: 'official-tag-patch-workspace'}
    if plan.get('schema') not in kinds or plan.get('kind') != kinds[plan['schema']] or plan.get('repository') != TARGET:
        raise ValueError('Unsupported official workspace provenance')
    release_from_plan(plan)
    for field in ('localization_snapshot', 'localization_base', 'official_tree', 'composed_tree', 'patched_tree', 'workflow_tree'):
        w.checked_sha(plan.get(field))
    for field in ('feature_patch_sha256', 'generated_patch_sha256'):
        if not isinstance(plan.get(field), str) or not SHA256.fullmatch(plan[field]):
            raise ValueError('Invalid source patch digest')
    build_number(plan.get('build_number'))
    if plan['schema'] == 4:
        w.checked_sha(plan.get('publication_commit'))
    if not re.fullmatch(r'(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)', plan.get('native_version', '')):
        raise ValueError('Invalid native source version')
    if not isinstance(plan.get('omitted_upstream_components'), list):
        raise ValueError('Missing optional-component provenance')


def release_from_plan(plan):
    record = plan.get('official', {})
    if not isinstance(record, dict) or set(record) != {'release_id', 'tag', 'object_sha', 'commit_sha', 'published_at'}:
        raise ValueError('Official source snapshot is incomplete')
    checked_tag(record.get('tag'))
    watcher().checked_sha(record.get('object_sha')); watcher().checked_sha(record.get('commit_sha'))
    if type(record.get('release_id')) is not int or record['release_id'] <= 0:
        raise ValueError('Invalid official release identity')
    return watcher().Release(**record)


def generated_tree(repo: Path, base_tree: str, patch: bytes) -> str:
    w = watcher()
    git_dir = (repo / w.git(repo, 'rev-parse', '--path-format=relative', '--git-dir')).resolve()
    with tempfile.TemporaryDirectory(prefix='openrv-generated-index-', dir=git_dir) as directory:
        index = Path(directory) / 'index'
        w.git_bytes(repo, 'read-tree', base_tree, index=index)
        if patch:
            w.git_bytes(repo, 'apply', '--cached', '--3way', '--whitespace=nowarn', '-', data=patch, index=index)
        result = w.checked_sha(w.git_bytes(repo, 'write-tree', index=index).stdout.decode().strip())
    changed = w.git_bytes(repo, 'diff', '--name-only', '-z', base_tree, result).stdout
    paths = {value.decode('utf-8') for value in changed.split(b'\0') if value}
    if not paths.issubset(GENERATED_PATHS):
        raise ValueError('Generated localization patch modifies unexpected source paths')
    return result


def verify_composition(repo: Path, plan: dict, delta: bytes, *, expected_snapshot=None, expected_build=None, refresh=False):
    validate_plan(plan); w = watcher(); release = release_from_plan(plan)
    if expected_snapshot and plan['localization_snapshot'] != w.checked_sha(expected_snapshot):
        raise ValueError('Plan differs from the immutable maintenance-main workflow snapshot')
    if expected_build is not None and plan['build_number'] != build_number(expected_build):
        raise ValueError('Plan build number differs from this workflow run')
    if digest(delta) != plan['generated_patch_sha256']:
        raise ValueError('Generated source patch digest differs from the immutable plan')
    if (refresh or w.git_bytes(repo, 'cat-file', '-e', release.commit_sha + '^{commit}', check=False).returncode
            or w.git_bytes(repo, 'cat-file', '-e', release.object_sha, check=False).returncode):
        w.fetch_official_release(repo, release)
    if w.git(repo, 'rev-parse', release.object_sha + '^{commit}') != release.commit_sha:
        raise ValueError('Official tag object does not peel to its recorded commit')
    if w.git(repo, 'rev-parse', release.commit_sha + '^{tree}') != plan['official_tree']:
        raise ValueError('Official source tree differs from the recorded tag')
    boundary = w.localization_source(repo, plan['localization_snapshot'])
    if boundary != plan['localization_base']:
        raise ValueError('Localization patch boundary differs from its immutable snapshot')
    config = w.localization_configuration(repo, plan['localization_snapshot'])
    if (plan['schema'], config['schema']) not in ((3, 1), (4, 2)):
        raise ValueError('Source plan schema does not match its committed bundle configuration')
    if plan['schema'] == 4:
        anchor = plan['publication_commit']
        if w.git_bytes(repo, 'merge-base', '--is-ancestor', anchor, plan['localization_snapshot'], check=False).returncode:
            raise ValueError('Publication anchor is not in the owned maintenance history')
        anchor_config = w.localization_configuration(repo, anchor)
        if anchor_config['schema'] != 2:
            raise ValueError('Publication anchor is not an owned lightweight maintenance commit')
        w.verified_bundle_inputs(repo, anchor, anchor_config)
    tree, patch_hash, omitted = w.compose_source_tree(repo, release, boundary, plan['localization_snapshot'])
    if (tree != plan['composed_tree'] or patch_hash != plan['feature_patch_sha256']
            or omitted != plan['omitted_upstream_components']):
        raise ValueError('Official source plus maintenance patch composition differs')
    final_tree = generated_tree(repo, tree, delta)
    if final_tree != plan['patched_tree']:
        raise ValueError('Patched source tree differs from its immutable provenance')
    if w.cmake_version(w.git(repo, 'show', final_tree + ':CMakeLists.txt')) != plan['native_version']:
        raise ValueError('Patched source changed the original native version')
    workflow = w.git(repo, 'rev-parse', plan['localization_snapshot'] + ':.github/workflows')
    if workflow != plan['workflow_tree'] or w.git(repo, 'rev-parse', final_tree + ':.github/workflows') != workflow:
        raise ValueError('Patched workspace workflow tree differs from trusted maintenance main')
    return release


def set_workspace_tree(repo: Path, release, tree: str, *, lightweight=False) -> None:
    """Only call for a freshly created clone or a verified disposable CI checkout."""
    w = watcher(); ensure_local_tag(repo, release, lightweight=lightweight)
    w.git(repo, 'config', 'core.autocrlf', 'false')
    w.git(repo, 'read-tree', '--reset', '-u', release.commit_sha)
    w.git(repo, 'checkout', '--detach', release.commit_sha)
    # Apply deletions while the old index still identifies tracked paths.
    w.git(repo, 'read-tree', '--reset', '-u', tree)
    # Clear cached stat data from an earlier autocrlf checkout. Even forced
    # checkout-index can skip apparently unchanged files and retain CRLF bytes.
    # This workspace is disposable; every tracked blob must be exported anew.
    w.git(repo, 'read-tree', '--empty')
    w.git(repo, 'read-tree', '--reset', '-u', tree)


def working_source_changes(repo: Path, tree: str) -> set[str]:
    """Hash files against a fresh index rather than trusting another Git's stat cache."""
    w = watcher()
    git_dir = (repo / w.git(repo, 'rev-parse', '--path-format=relative', '--git-dir')).resolve()
    with tempfile.TemporaryDirectory(prefix='openrv-working-index-', dir=git_dir) as directory:
        index = Path(directory) / 'index'
        w.git_bytes(repo, 'read-tree', tree, index=index)
        paths = w.git_bytes(repo, 'diff', '--no-ext-diff', '--no-textconv', '--name-only', '-z', index=index).stdout
    return {path.decode('utf-8') for path in paths.split(b'\0') if path}


def source_checks(trusted: Path, repo: Path, output: Path) -> None:
    w = watcher(); lupdate = Path(os.environ.get('QT_LUPDATE_PATH', '/usr/lib/qt6/bin/lupdate'))
    if not lupdate.is_file():
        raise ValueError('Qt lupdate is required for official source extraction')
    failed = []
    for script in ('update_menu_paths.py', 'sync_sources.py', 'check_translations.py'):
        path = trusted / 'tools/localization' / script
        if not path.is_file():
            raise ValueError('Trusted main is missing a localization checker')
        command = [sys.executable, str(path), '--repo', str(repo)]
        if script == 'sync_sources.py':
            command += ['--output-dir', str(output / 'translations'), '--lupdate', str(lupdate), '--apply']
        if script == 'check_translations.py':
            command += ['--report', str(output / 'catalog-check.json')]
        if subprocess.run(command, cwd=trusted, env=w.clean_environment(), timeout=180).returncode:
            failed.append(script)
    for relative in GENERATED_PATHS:
        target = w.safe_file(repo, relative)
        if target.is_file():
            target.write_bytes(target.read_bytes().replace(b'\r\n', b'\n'))
    w.git(repo, 'diff', '--check')
    if failed:
        raise ValueError('Official source needs manual translation in main: ' + ', '.join(failed))


def prepare_workspace(trusted: Path, destination: Path, output: Path, release, number, *, checker=source_checks,
                      publication_commit=None):
    trusted, destination, output = trusted.resolve(), destination.resolve(), output.resolve()
    if destination.exists() or destination == trusted or destination.is_relative_to(trusted):
        raise ValueError('Preflight workspace must be fresh and outside the trusted checkout')
    output.mkdir(parents=True, exist_ok=True)
    w = watcher(); snapshot = w.checked_sha(w.git(trusted, 'rev-parse', 'HEAD'))
    boundary = w.localization_source(trusted, snapshot)
    config = w.localization_configuration(trusted, snapshot)
    lightweight = config['schema'] == 2
    if lightweight and publication_commit is None:
        raise ValueError('A lightweight source plan requires its explicit publication commit')
    subprocess.run(['git', 'clone', '--no-checkout', '--no-hardlinks', str(trusted), str(destination)],
                   env=w.git_environment(), check=True, capture_output=True, timeout=180)
    official_tree = w.fetch_official_release(destination, release)
    tree, feature_hash, omitted = w.compose_source_tree(destination, release, boundary, snapshot)
    set_workspace_tree(destination, release, tree, lightweight=lightweight)
    checker(trusted, destination, output)
    changed = w.git_bytes(destination, 'diff', '--name-only', '-z').stdout
    if not {p.decode() for p in changed.split(b'\0') if p}.issubset(GENERATED_PATHS):
        raise ValueError('Trusted localization tools changed unexpected source files')
    delta = w.git_bytes(destination, 'diff', '--binary', '--full-index', '--no-ext-diff', '--no-textconv').stdout
    final_tree = generated_tree(destination, tree, delta)
    plan = {'schema': 3, 'kind': 'official-tag-workspace', 'repository': TARGET,
            'official': asdict(release), 'official_tree': official_tree,
            'localization_snapshot': snapshot, 'localization_base': boundary,
            'feature_patch_sha256': feature_hash, 'omitted_upstream_components': omitted,
            'composed_tree': tree, 'patched_tree': final_tree,
            'generated_patch_sha256': digest(delta), 'build_number': build_number(number),
            'native_version': w.cmake_version(w.git(destination, 'show', release.commit_sha + ':CMakeLists.txt')),
            'workflow_tree': w.git(destination, 'rev-parse', snapshot + ':.github/workflows')}
    if lightweight:
        plan.update(schema=4, kind='official-tag-patch-workspace',
                    publication_commit=w.checked_sha(publication_commit))
    verify_composition(destination, plan, delta, expected_snapshot=snapshot, expected_build=number)
    (output / 'generated-localization.patch').write_bytes(delta)
    encoded = (json.dumps(plan, indent=2, sort_keys=True) + '\n').encode()
    (output / 'build-plan.json').write_bytes(encoded)
    return plan, digest(encoded)


def read_input(directory: Path, expected_digest=''):
    directory = directory.resolve()
    plan_path = watcher().safe_file(directory, 'build-plan.json')
    delta_path = watcher().safe_file(directory, 'generated-localization.patch')
    raw = plan_path.read_bytes()
    if expected_digest and (not SHA256.fullmatch(expected_digest) or digest(raw) != expected_digest):
        raise ValueError('Build input plan differs from the trusted preflight output')
    plan = json.loads(raw); validate_plan(plan)
    delta = delta_path.read_bytes()
    if digest(delta) != plan['generated_patch_sha256']:
        raise ValueError('Build input source delta differs from its plan')
    return plan, delta, raw


def materialize(repo: Path, input_dir: Path, expected_digest: str, expected_snapshot: str, expected_build):
    repo = repo.resolve(); w = watcher()
    plan, delta, raw = read_input(input_dir, expected_digest)
    if w.git(repo, 'rev-parse', 'HEAD') != w.checked_sha(expected_snapshot) or w.git(repo, 'status', '--porcelain'):
        raise ValueError('Materialization requires a clean disposable maintenance-main checkout')
    release = verify_composition(repo, plan, delta, expected_snapshot=expected_snapshot,
                                 expected_build=expected_build, refresh=True)
    set_workspace_tree(repo, release, plan['patched_tree'], lightweight=plan['schema'] == 4)
    metadata = w.safe_file(repo, PLAN); metadata.parent.mkdir(parents=True, exist_ok=True)
    metadata.write_bytes(raw); w.safe_file(repo, DELTA).write_bytes(delta)
    verify_materialized(repo, plan, delta, expected_snapshot=expected_snapshot, expected_build=expected_build)
    return plan


def verify_materialized(repo: Path, plan=None, delta=None, *, expected_snapshot=None, expected_build=None):
    w = watcher(); repo = repo.resolve()
    if plan is None:
        path = w.safe_file(repo, PLAN)
        raw = path.read_bytes()
        expected = os.environ.get('ORV_BUILD_PLAN_SHA256', '')
        if expected and digest(raw) != expected:
            raise ValueError('Workspace plan does not match preflight SHA256')
        plan = json.loads(raw); delta = w.safe_file(repo, DELTA).read_bytes()
    release = verify_composition(repo, plan, delta, expected_snapshot=expected_snapshot, expected_build=expected_build)
    ensure_local_tag(repo, release, lightweight=plan['schema'] == 4)
    if w.git(repo, 'rev-parse', 'HEAD') != release.commit_sha:
        raise ValueError('Workspace HEAD must remain the original official tag commit')
    if w.git(repo, 'write-tree') != plan['patched_tree']:
        raise ValueError('Workspace index differs from the verified patched source tree')
    if w.base_version(repo) != plan['native_version']:
        raise ValueError('Native CMake version differs from official source provenance')
    changes = working_source_changes(repo, plan['patched_tree'])
    if changes:
        # This is the one reviewed Windows CI hook; dependency edits made later
        # inside build directories are recorded by the existing packager.
        hook = 'cmake/dependencies/CMakeLists.txt'
        if changes != {hook}:
            paths = json.dumps(sorted(changes)[:12], ensure_ascii=True)[:2400]
            raise ValueError(f'Working source differs from the verified patched tree ({len(changes)} paths): {paths}')
        original = w.git_bytes(repo, 'show', plan['patched_tree'] + ':' + hook).stdout
        expected = original + b'\n# OpenRV localized release dependency hooks\ninclude("${PROJECT_SOURCE_DIR}/ci/localized-release/dependency-hooks.cmake")\n'
        if w.safe_file(repo, hook).read_bytes() != expected:
            raise ValueError('Working source dependency hook differs from the reviewed CI patch')
    return plan
