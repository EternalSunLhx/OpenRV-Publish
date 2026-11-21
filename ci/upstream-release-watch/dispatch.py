"""Read-only official release monitoring; the only write is a main workflow dispatch."""
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from functools import cache
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import urllib.error
import urllib.parse
import urllib.request

UPSTREAM = 'AcademySoftwareFoundation/OpenRV'
TARGET = 'EternalSunLhx/OpenRV-Publish'
WORKFLOW = 'localized-release.yml'
TAG = re.compile(r'v(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)')
SHA = re.compile(r'[0-9a-f]{40}')
MAX_PAGES = 5


@cache
def publication_tags():
    path = Path(__file__).resolve().parents[1] / 'localized-release/publication_tags.py'
    spec = importlib.util.spec_from_file_location('openrv_watch_publication_tags', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def source_snapshot_is_valid(repo, candidate, snapshot):
    return publication_tags().is_light_snapshot(repo, candidate, snapshot)


def official_record(release):
    return {'release_id': release.release_id, 'tag': release.tag,
            'object_sha': release.tag_object, 'commit_sha': release.commit,
            'published_at': release.published_at}


def checked_sha(value):
    if not isinstance(value, str) or not SHA.fullmatch(value):
        raise ValueError('Invalid immutable commit/object identity')
    return value


def timestamp(value):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?Z', value):
        raise ValueError('Expected a UTC publication/run timestamp')
    return datetime.fromisoformat(value[:-1] + '+00:00')


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Api:
    def __init__(self, token):
        self.token = token
        self.opener = urllib.request.build_opener(NoRedirect())

    def request(self, method, repository, suffix, data=None):
        if not all(isinstance(value, str) for value in (method, repository, suffix)):
            raise ValueError('Expected an approved API method, repository and suffix')
        official_get = (suffix == 'releases/latest' or re.fullmatch('git/ref/tags/' + TAG.pattern, suffix)
                        or re.fullmatch(r'git/(?:tags|commits)/[0-9a-f]{40}', suffix))
        target_get = re.fullmatch(
            r'git/ref/heads/main|actions/workflows/localized-release\.yml|'
            r'actions/workflows/[1-9][0-9]*/runs\?event=workflow_dispatch&branch=main&per_page=100&page=[1-5]', suffix)
        target_get = (target_get or re.fullmatch('git/ref/tags/' + TAG.pattern, suffix)
                      or re.fullmatch(r'git/tags/[0-9a-f]{40}', suffix))
        dispatch = method == 'POST' and repository == TARGET and suffix == 'actions/workflows/' + WORKFLOW + '/dispatches'
        if not ((method == 'GET' and data is None and ((repository == UPSTREAM and official_get)
                or (repository == TARGET and target_get))) or dispatch):
            raise ValueError('Only approved reads and a main official-tag dispatch are permitted')
        if dispatch and (not isinstance(data, dict) or set(data) != {'ref', 'inputs'}
                or data['ref'] != 'main' or not isinstance(data['inputs'], dict)
                or set(data['inputs']) != {'official_tag'} or not isinstance(data['inputs']['official_tag'], str)
                or not TAG.fullmatch(data['inputs']['official_tag'])):
            raise ValueError('Dispatch must select main and one official release tag')
        payload = None if data is None else json.dumps(data).encode('utf-8')
        request = urllib.request.Request('https://api.github.com/repos/' + repository + '/' + suffix,
            data=payload, method=method, headers={
                'Accept': 'application/vnd.github+json', 'Authorization': 'Bearer ' + self.token,
                'X-GitHub-Api-Version': '2022-11-28', 'User-Agent': 'OpenRV-Publish-release-watch'})
        try:
            with self.opener.open(request, timeout=30) as response:
                raw = response.read(4 * 1024 * 1024 + 1)
                if len(raw) > 4 * 1024 * 1024:
                    raise ValueError('GitHub metadata response exceeded the size limit')
                return response.status, json.loads(raw) if raw else None
        except urllib.error.HTTPError as error:
            # Never include response bodies, redirect locations or credentials in errors.
            return error.code, None


def require(api, repository, suffix):
    status, value = api.request('GET', repository, suffix)
    if status != 200 or not isinstance(value, dict):
        raise RuntimeError('GitHub metadata request failed (HTTP ' + str(status) + ')')
    return value


@dataclass(frozen=True)
class Release:
    release_id: int
    tag: str
    tag_object: str
    commit: str
    tree: str
    published_at: str


def snapshot(api):
    release = require(api, UPSTREAM, 'releases/latest')
    tag = release.get('tag_name')
    if (type(release.get('id')) is not int or release['id'] <= 0
            or release.get('draft') is not False or release.get('prerelease') is not False
            or not isinstance(tag, str) or not TAG.fullmatch(tag)
            or release.get('html_url') != 'https://github.com/' + UPSTREAM + '/releases/tag/' + tag):
        raise ValueError('Official latest release is not a canonical stable release')
    timestamp(release.get('published_at'))
    reference = require(api, UPSTREAM, 'git/ref/tags/' + tag)
    if reference.get('ref') != 'refs/tags/' + tag:
        raise ValueError('Official tag ref does not match its release')
    obj = reference.get('object', {})
    tag_object = checked_sha(obj.get('sha'))
    seen = set()
    while obj.get('type') == 'tag':
        identity = checked_sha(obj.get('sha'))
        if identity in seen or len(seen) >= 8:
            raise ValueError('Unexpected annotated tag chain')
        seen.add(identity)
        annotated = require(api, UPSTREAM, 'git/tags/' + identity)
        if annotated.get('sha') != identity:
            raise ValueError('Annotated tag object changed')
        obj = annotated.get('object', {})
    if obj.get('type') != 'commit':
        raise ValueError('Official tag must resolve to a commit')
    commit = checked_sha(obj.get('sha'))
    commit_data = require(api, UPSTREAM, 'git/commits/' + commit)
    if commit_data.get('sha') != commit:
        raise ValueError('Official commit identity differs')
    tree = checked_sha(commit_data.get('tree', {}).get('sha'))
    return Release(release['id'], tag, tag_object, commit, tree, release['published_at'])


def current_main(api, expected):
    reference = require(api, TARGET, 'git/ref/heads/main')
    if (reference.get('ref') != 'refs/heads/main'
            or reference.get('object', {}).get('type') != 'commit'
            or reference['object'].get('sha') != checked_sha(expected)):
        raise ValueError('Maintenance main moved; the next watch run will use its new snapshot')


def mirrored_tag(api, release, source_checker):
    """Bind upstream identity in an own annotation without importing upstream history."""
    publication = publication_tags().read_publication_tag(api, official_record(release))
    if publication is None:
        return False
    if source_checker(publication['commit']) is not True:
        raise ValueError('Publication tag does not anchor a schema-2 maintenance ancestor')
    return True


def previous_run(api, release, source_checker):
    """Deduplicate official releases across maintenance-only main commits."""
    workflow = require(api, TARGET, 'actions/workflows/' + WORKFLOW)
    if (type(workflow.get('id')) is not int or workflow['id'] <= 0 or workflow.get('state') != 'active'
            or workflow.get('path') != '.github/workflows/' + WORKFLOW):
        raise ValueError('Unexpected release workflow identity or state')
    title = re.compile(r'OpenRV ' + re.escape(release.tag) + r' build ([1-9][0-9]*)')
    for page in range(1, MAX_PAGES + 1):
        result = require(api, TARGET, f"actions/workflows/{workflow['id']}/runs?event=workflow_dispatch&branch=main&per_page=100&page={page}")
        runs = result.get('workflow_runs')
        if not isinstance(runs, list) or len(runs) > 100:
            raise ValueError('Invalid workflow run history')
        older = False
        for run in runs:
            if not isinstance(run, dict):
                raise ValueError('Invalid workflow run entry')
            created = timestamp(run.get('created_at'))
            match = title.fullmatch(run['display_title']) if isinstance(run.get('display_title'), str) else None
            older |= created < timestamp(release.published_at)
            if (type(run.get('id')) is int and run['id'] > 0 and run.get('event') == 'workflow_dispatch'
                    and run.get('head_branch') == 'main' and isinstance(run.get('head_sha'), str)
                    and SHA.fullmatch(run['head_sha'])
                    and type(run.get('workflow_id')) is int and run['workflow_id'] == workflow['id']
                    and run.get('path') == '.github/workflows/' + WORKFLOW
                    and run.get('repository', {}).get('full_name') == TARGET
                    and match and type(run.get('run_number')) is int and run['run_number'] > 0
                    and match.group(1) == str(run['run_number'])
                    and created >= timestamp(release.published_at)
                    and run.get('html_url') == 'https://github.com/' + TARGET + '/actions/runs/' + str(run['id'])
                    and source_checker(run['head_sha']) is True):
                return run
        if len(runs) < 100 or older:
            return None
    raise ValueError('Recent workflow history exceeded the bounded recovery window; no duplicate was dispatched')


def check_and_dispatch(api, source_sha, *, probe=False, source_checker=None):
    checked_sha(source_sha)
    if source_checker is None:
        source_checker = lambda candidate: source_snapshot_is_valid(Path.cwd(), candidate, source_sha)
    if source_checker(source_sha) is not True:
        raise ValueError('Controller source must be a schema-2 lightweight maintenance snapshot')
    current_main(api, source_sha)
    release = snapshot(api)
    mirrored = mirrored_tag(api, release, source_checker)
    existing = previous_run(api, release, source_checker)
    if existing:
        state = existing.get('status')
        if state not in ('queued', 'in_progress', 'completed', 'waiting', 'requested', 'pending'):
            raise ValueError('Existing build status is not recognized; review its source identity')
        if state == 'completed' and existing.get('conclusion') == 'success' and not mirrored:
            raise ValueError('A successful build has no immutable publication tag; review its source identity')
        return {'needs_build': False, 'dispatched': False, 'release': asdict(release),
                'run_id': existing['id'], 'run_url': existing['html_url'],
                'existing_run_source_sha': existing['head_sha'],
                'tag_mirror_verified': mirrored, 'existing_run_status': state,
                'existing_run_conclusion': existing.get('conclusion')}
    if probe:
        return {'needs_build': True, 'dispatched': False, 'release': asdict(release), 'tag_mirror_verified': mirrored}
    current_main(api, source_sha)
    if snapshot(api) != release:
        raise ValueError('Official latest release or tag changed before dispatch')
    mirrored_before_dispatch = mirrored_tag(api, release, source_checker)
    if mirrored and not mirrored_before_dispatch:
        raise ValueError('Publication tag disappeared before dispatch; review its source identity')
    status, _ = api.request('POST', TARGET, 'actions/workflows/' + WORKFLOW + '/dispatches',
                           {'ref': 'main', 'inputs': {'official_tag': release.tag}})
    if status != 204:
        raise RuntimeError('Official release build dispatch was not accepted (HTTP ' + str(status) + ')')
    return {'needs_build': True, 'dispatched': True, 'release': asdict(release),
            'tag_mirror_verified': mirrored_before_dispatch}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--repo', type=Path, default=Path.cwd())
    parser.add_argument('--probe', action='store_true')
    args = parser.parse_args()
    if (os.environ.get('GITHUB_ACTIONS') != 'true' or os.environ.get('GITHUB_REPOSITORY') != TARGET
            or os.environ.get('GITHUB_REF') != 'refs/heads/main'
            or os.environ.get('GITHUB_EVENT_NAME') not in ('schedule', 'workflow_dispatch')):
        parser.error('Official release monitoring runs only in this repository on main')
    source_sha = checked_sha(os.environ.get('GITHUB_SHA'))
    actual = subprocess.check_output(['git', '-C', str(args.repo.resolve()), 'rev-parse', 'HEAD'], text=True).strip()
    if actual != source_sha:
        raise ValueError('Checked-out maintenance source is not the workflow snapshot')
    token = os.environ.get('GH_TOKEN', '')
    if not token:
        raise ValueError('A job-scoped GitHub token is required')
    result = check_and_dispatch(Api(token), source_sha, probe=args.probe,
        source_checker=lambda candidate: source_snapshot_is_valid(args.repo.resolve(), candidate, source_sha))
    print(json.dumps(result, indent=2))
    if output := os.environ.get('GITHUB_OUTPUT'):
        with open(output, 'a', encoding='utf-8') as stream:
            stream.write('needs_build=' + str(result['needs_build']).lower() + '\n')
    if summary := os.environ.get('GITHUB_STEP_SUMMARY'):
        with open(summary, 'a', encoding='utf-8') as stream:
            stream.write('Official **' + result['release']['tag'] + '**; current controller snapshot `' + source_sha + '`.\n\n')
            if result.get('run_url'):
                stream.write('Existing build: ' + result['run_url'] + '; build source snapshot `'
                             + result['existing_run_source_sha'] + '`.\n')
                stream.write('To rebuild maintenance changes for the same official tag, manually dispatch '
                             'the formal packaging workflow `' + WORKFLOW + '`.')
            else:
                stream.write('Build dispatched on main.' if result['dispatched'] else 'Read-only check: a new build is needed.')
            if result.get('run_id') and not result['tag_mirror_verified']:
                stream.write('\nBuild retained without dispatching again; its official source identity has no publication tag yet. Failed or cancelled builds require a manual retry.\n')
            stream.write('\nNo candidate branches, state branches or custom edition tags are created.\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
