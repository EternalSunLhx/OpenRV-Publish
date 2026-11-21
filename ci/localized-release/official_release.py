"""Validate a release/native-version difference against exact source composition."""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import re
import sys

TAG = re.compile(r'v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)-zh\.([1-9][0-9]*)')
PLAN = 'ci/upstream-release-watch/candidate.json'


def release_identity(repo: Path, tag: str, native_version: str, *, refresh: bool = False) -> dict:
    if isinstance(tag, str) and re.fullmatch(r'v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)', tag):
        from official_workspace import build_number, verify_materialized
        number = build_number(os.environ.get('ORV_BUILD_NUMBER', ''))
        plan = verify_materialized(repo, expected_build=number)
        if plan['official']['tag'] != tag or plan['native_version'] != native_version:
            raise ValueError('Official workspace provenance does not authorize this tag/native version')
        identity = {'version': tag[1:], 'nativeVersion': native_version, 'buildNumber': number,
                'displayVersion': tag[1:] + '+build.' + str(number),
                'upstreamRelease': plan['official'], 'upstreamSourceTree': plan['official_tree'],
                'patchedSourceTree': plan['patched_tree'], 'composedSourceTree': plan['composed_tree'],
                'localizationSnapshot': plan['localization_snapshot'],
                'localizationPatchBase': plan['localization_base'],
                'localizationPatchSha256': plan['feature_patch_sha256'],
                'generatedLocalizationPatchSha256': plan['generated_patch_sha256'],
                'omittedUpstreamComponents': plan['omitted_upstream_components']}
        if plan.get('schema') == 4:
            identity.update(publicationCommit=plan['publication_commit'], sourcePlanSchema=4,
                            localizationPatchKind='explicit-patches-and-owned-resources')
        return identity
    match = TAG.fullmatch(tag) if isinstance(tag, str) else None
    if not match:
        raise ValueError('Release tag must have the form vX.Y.Z-zh.N')
    release_version = '.'.join(match.groups()[:3])
    path = repo / PLAN
    if not path.exists():
        if release_version != native_version:
            raise ValueError('Release tag numeric base must match CMakeLists.txt without verified official provenance')
        return {'version': native_version, 'nativeVersion': native_version}
    # Load this CI implementation, never a path supplied by candidate metadata.
    watch_path = Path(__file__).resolve().parents[1] / 'upstream-release-watch/watch.py'
    spec = importlib.util.spec_from_file_location('openrv_release_identity_watch', watch_path)
    watch = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = watch
    spec.loader.exec_module(watch)
    plan = json.loads(watch.safe_file(repo, PLAN).read_text(encoding='utf-8'))
    if (plan.get('schema') != 2 or plan.get('kind') != 'official-release-localization'
            or plan.get('release_tag') != tag or plan.get('upstream_native_version') != native_version):
        raise ValueError('Official release provenance does not authorize this tag/native version')
    upstream = plan.get('upstream', {})
    if not isinstance(upstream, dict) or set(upstream) != {'release_id', 'tag', 'object_sha', 'commit_sha', 'published_at'}:
        raise ValueError('Official source snapshot is incomplete')
    release = watch.Release(**upstream)
    watch.version(release.tag)
    watch.checked_sha(release.object_sha)
    watch.checked_sha(release.commit_sha)
    if 'v' + release_version != release.tag:
        raise ValueError('Localized tag differs from its pinned official release')
    # The upstream commit is intentionally not a parent of the candidate. A
    # fresh checkout may therefore need these public objects for reconstruction.
    have_object = watch.git_bytes(repo, 'cat-file', '-e', release.object_sha, check=False).returncode == 0
    have_commit = watch.git_bytes(repo, 'cat-file', '-e', release.commit_sha + '^{commit}', check=False).returncode == 0
    if refresh or not (have_object and have_commit):
        watch.fetch_official_release(repo, release)
    if watch.git(repo, 'rev-parse', release.object_sha + '^{commit}') != release.commit_sha:
        raise ValueError('Pinned official tag object does not peel to its recorded commit')
    watch.verify_candidate(repo, plan, release, watch.checked_sha(plan.get('base_sha')))
    if watch.base_version(repo) != native_version:
        raise ValueError('Native build version differs from the verified official source')
    return {'version': release_version, 'nativeVersion': native_version,
            'upstreamRelease': upstream, 'officialSourceTree': plan['upstream_tree_sha'],
            'localizationSnapshot': plan['base_sha'], 'localizationPatchBase': plan['localization_base_sha'],
            'localizationPatchSha256': plan['localization_patch_sha256'],
            'omittedUpstreamComponents': plan['omitted_upstream_components'],
            'composedSourceCommit': plan['composed_commit_sha'], 'composedSourceTree': plan['composed_tree_sha']}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', type=Path, required=True)
    parser.add_argument('--tag', required=True)
    parser.add_argument('--native-version', required=True)
    parser.add_argument('--version-only', action='store_true')
    args = parser.parse_args()
    identity = release_identity(args.repo.resolve(), args.tag, args.native_version)
    print(identity['version'] if args.version_only else json.dumps(identity, sort_keys=True))
