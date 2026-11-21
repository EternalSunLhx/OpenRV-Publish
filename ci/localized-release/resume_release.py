"""Publish an authenticated existing native cohort without rebuilding the application."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

from official_workspace import (TARGET, OfficialApi, digest, materialize,
                                read_input, verify_materialized, watcher)
from prepare_release import product_build_number, publish_assets

WORKFLOW = '.github/workflows/localized-release.yml'
INPUT = 'official-build-input'
PACKAGES = {
    'localized-release-Windows-x64': ('Windows x64 Release', 'Upload Windows release files'),
    'localized-release-macOS-x86_64': ('macOS x86_64 Release', 'Upload macOS release files'),
    'localized-release-macOS-arm64': ('macOS arm64 Release', 'Upload macOS release files'),
}
PREFLIGHT = 'Compose official source and check manual translations'
TAG_JOB = 'Record the publication tag and official source identity'


def api_get(suffix):
    if not re.fullmatch(r'actions/runs/[1-9][0-9]*(?:/(?:jobs|artifacts)\?per_page=100&page=[1-9][0-9]*)?', suffix):
        raise ValueError('Only fixed source-run reads are permitted')
    result = subprocess.run(['gh', 'api', '--hostname', 'github.com', f'repos/{TARGET}/{suffix}'],
                            check=True, capture_output=True, text=True, timeout=60)
    return json.loads(result.stdout)


def positive(value):
    if type(value) is not int or value <= 0:
        raise ValueError('A positive numeric GitHub identity is required')
    return value


def validate_source(run, jobs, artifacts, run_id):
    """Bind jobs and immutable artifact IDs to one original dispatch, never a fork."""
    run_id = positive(run_id)
    sha = watcher().checked_sha(run.get('head_sha'))
    repository = run.get('repository', {})
    repository_id = positive(repository.get('id'))
    head_repository = run.get('head_repository', {})
    if (run.get('id') != run_id or run.get('path') != WORKFLOW
            or run.get('event') != 'workflow_dispatch' or run.get('head_branch') != 'main'
            or run.get('status') != 'completed' or run.get('conclusion') not in ('success', 'failure')
            or positive(run.get('run_attempt')) != 1
            or repository.get('full_name') != TARGET or head_repository.get('full_name') != TARGET
            or head_repository.get('id') != repository_id):
        raise ValueError('Recovery requires a completed original main dispatch from this repository')
    number = product_build_number(positive(run.get('run_number')))
    required_jobs = {PREFLIGHT, TAG_JOB} | {item[0] for item in PACKAGES.values()}
    selected_jobs = {}
    for name in required_jobs:
        matches = [job for job in jobs if job.get('name') == name]
        if len(matches) != 1:
            raise ValueError('Each original preflight, tag and native job must be unique')
        job = matches[0]
        if (job.get('run_id') != run_id or job.get('head_sha') != sha
                or job.get('status') != 'completed' or job.get('conclusion') != 'success'):
            raise ValueError('All original preflight, tag and native jobs must have succeeded')
        selected_jobs[name] = job
    selected = {}
    owners = {INPUT: (PREFLIGHT, 'Upload immutable build input and translation reports'), **PACKAGES}
    for name, (job_name, upload_name) in owners.items():
        matches = [artifact for artifact in artifacts if artifact.get('name') == name]
        if len(matches) != 1:
            raise ValueError('Exactly one original artifact is required for ' + name)
        artifact = matches[0]
        origin = artifact.get('workflow_run', {})
        upload = [step for step in selected_jobs[job_name].get('steps', []) if step.get('name') == upload_name]
        if (len(upload) != 1 or upload[0].get('conclusion') != 'success'
                or artifact.get('expired') is not False
                or origin.get('id') != run_id or origin.get('head_sha') != sha
                or origin.get('head_branch') != 'main'
                or origin.get('repository_id') != repository_id
                or origin.get('head_repository_id') != repository_id
                or not re.fullmatch(r'sha256:[0-9a-f]{64}', artifact.get('digest', ''))):
            raise ValueError('Artifact lacks its original successful upload and digest: ' + name)
        created = artifact.get('created_at', '')
        if not (selected_jobs[job_name].get('started_at', 'z') <= created
                <= selected_jobs[job_name].get('completed_at', '')):
            raise ValueError('Artifact was not created during the authenticated source job')
        size = positive(artifact.get('size_in_bytes'))
        if size > (16 * 1024 * 1024 if name == INPUT else 8 * 1024**3):
            raise ValueError('Artifact exceeds the expected release size bound')
        selected[name] = {'id': positive(artifact.get('id')), 'digest': artifact['digest'], 'size': size}
    if len({value['id'] for value in selected.values()}) != len(selected):
        raise ValueError('Artifact identities must be distinct')
    return {'source_run_id': run_id, 'source_sha': sha, 'build_number': number, 'artifacts': selected}


def source_evidence(run_id, read=api_get):
    run = read(f'actions/runs/{run_id}')
    collections = {}
    for kind in ('jobs', 'artifacts'):
        values = []
        for page in range(1, 101):
            response = read(f'actions/runs/{run_id}/{kind}?per_page=100&page={page}')
            batch = response.get(kind)
            if not isinstance(batch, list) or len(batch) > 100:
                raise ValueError('Invalid GitHub source evidence page')
            values.extend(batch)
            if len(batch) < 100:
                break
        else:
            raise ValueError('GitHub source evidence exceeded its pagination bound')
        collections[kind] = values
    return validate_source(run, collections['jobs'], collections['artifacts'], run_id)


def controller_context(repo):
    if (os.environ.get('GITHUB_ACTIONS') != 'true' or os.environ.get('GITHUB_REPOSITORY') != TARGET
            or os.environ.get('GITHUB_EVENT_NAME') != 'workflow_dispatch'
            or os.environ.get('GITHUB_REF') != 'refs/heads/main'):
        raise ValueError('Recovery must be dispatched from the owned maintenance main')
    sha = watcher().checked_sha(os.environ.get('GITHUB_SHA', ''))
    if watcher().git(repo, 'rev-parse', 'HEAD') != sha or watcher().git(repo, 'status', '--porcelain'):
        raise ValueError('Recovery controller must be a clean immutable checkout')
    return sha


def stage_packages(directory, assets, evidence):
    assets.mkdir(exist_ok=False)
    run_url = f"https://github.com/{TARGET}/actions/runs/{evidence['source_run_id']}"
    for artifact_name in PACKAGES:
        folder = directory / artifact_name
        if folder.is_symlink() or not folder.is_dir():
            raise ValueError('Each native artifact must have its own regular directory')
        files = list(folder.iterdir())
        if len(files) != 4 or any(path.is_symlink() or not path.is_file() for path in files):
            raise ValueError('Each original platform artifact must contain four immediate regular files')
        manifests = [path for path in files if path.name.endswith('.build.json')]
        if len(manifests) != 1:
            raise ValueError('Each platform must contain exactly one original manifest')
        manifest = json.loads(manifests[0].read_text(encoding='utf-8-sig'))
        if (manifest.get('repository') != TARGET or manifest.get('runUrl') != run_url
                or manifest.get('localizationSnapshot') != evidence['source_sha']
                or manifest.get('buildNumber') != evidence['build_number']):
            raise ValueError('Platform manifest does not belong to the original source run')
        for path in files:
            target = assets / path.name
            if target.exists():
                raise ValueError('Platform artifacts contain colliding file names')
            shutil.copyfile(path, target)


def publish_existing(repo, root, evidence):
    if source_evidence(evidence['source_run_id']) != evidence:
        raise ValueError('Original source-run evidence changed during download')
    source = root / 'source'
    inputs = root / 'input'
    assets = root / 'assets'
    plan, delta, raw = read_input(inputs)
    plan_hash = digest(raw)
    if plan['localization_snapshot'] != evidence['source_sha'] or plan['build_number'] != evidence['build_number']:
        raise ValueError('Original input plan does not belong to the authenticated build')
    subprocess.run(['git', 'merge-base', '--is-ancestor', evidence['source_sha'], 'HEAD'], cwd=repo, check=True)
    subprocess.run(['git', 'clone', '--no-hardlinks', str(repo), str(source)], check=True, capture_output=True)
    subprocess.run(['git', 'checkout', '--detach', evidence['source_sha']], cwd=source, check=True, capture_output=True)
    materialize(source, inputs, plan_hash, evidence['source_sha'], evidence['build_number'])
    stage_packages(root / 'packages', assets, evidence)
    environment = os.environ.copy()
    environment.update(ORV_RELEASE_TAG=plan['official']['tag'], ORV_BUILD_NUMBER=str(evidence['build_number']),
                       ORV_BUILD_PLAN_SHA256=plan_hash)
    # Execute the original assembler in its own process; never copy controller fixes into product sources.
    subprocess.run([sys.executable, str(source / 'ci/localized-release/assemble_release.py'),
                    '--repo', str(source), '--assets', str(assets), '--tag', plan['official']['tag']],
                   cwd=source, env=environment, check=True, timeout=1200)
    verify_materialized(source, plan, delta, expected_snapshot=evidence['source_sha'],
                        expected_build=evidence['build_number'])
    publish_assets(source, assets, plan, OfficialApi())
    receipt = {**evidence, 'controller_sha': os.environ['GITHUB_SHA'],
               'publication_run_id': os.environ['GITHUB_RUN_ID'], 'plan_sha256': plan_hash,
               'release_tag': plan['official']['tag'], 'native_rebuild': False, 'status': 'published'}
    (root / 'publication-receipt.json').write_text(json.dumps(receipt, indent=2) + '\n', encoding='utf-8')
    with open(os.environ['GITHUB_STEP_SUMMARY'], 'a', encoding='utf-8') as summary:
        summary.write(f"Published {plan['official']['tag']} using unchanged packages from source run "
                      f"{evidence['source_run_id']} ({evidence['source_sha']}). No native rebuild.\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('phase', choices=('discover', 'publish'))
    parser.add_argument('--source-run', required=True, type=int)
    args = parser.parse_args()
    repo = Path.cwd().resolve()
    controller_context(repo)
    root = Path(os.environ['RUNNER_TEMP']).resolve() / 'openrv-publication-recovery'
    if args.phase == 'discover':
        evidence = source_evidence(positive(args.source_run))
        root.mkdir(exist_ok=False)
        (root / 'source-evidence.json').write_text(json.dumps(evidence, indent=2) + '\n', encoding='utf-8')
        with open(os.environ['GITHUB_OUTPUT'], 'a', encoding='utf-8') as output:
            output.write(f"input_id={evidence['artifacts'][INPUT]['id']}\n")
            output.write('package_ids=' + ','.join(str(evidence['artifacts'][name]['id']) for name in PACKAGES) + '\n')
    else:
        evidence = json.loads((root / 'source-evidence.json').read_text(encoding='utf-8'))
        if evidence['source_run_id'] != positive(args.source_run):
            raise ValueError('Source run changed between recovery steps')
        publish_existing(repo, root, evidence)


if __name__ == '__main__':
    main()
