"""Offline source-run, artifact cohort and publish-only recovery boundaries."""
from __future__ import annotations

import contextlib
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
try:
    import resume_release as resume
finally:
    sys.path.remove(str(SCRIPTS))

SOURCE_SHA = 'a' * 40
CONTROLLER_SHA = 'b' * 40
RUN_ID = 12345
START = '2026-11-21T09:00:00Z'
END = '2026-11-21T10:00:00Z'


def source_fixture():
    run = {'id': RUN_ID, 'path': resume.WORKFLOW, 'head_sha': SOURCE_SHA,
           'event': 'workflow_dispatch', 'head_branch': 'main', 'status': 'completed',
           'conclusion': 'failure', 'run_attempt': 1, 'run_number': 7,
           'repository': {'id': 42, 'full_name': resume.TARGET},
           'head_repository': {'id': 42, 'full_name': resume.TARGET}}
    jobs, artifacts = [], []
    owners = {resume.INPUT: (resume.PREFLIGHT, 'Upload immutable build input and translation reports'),
              **resume.PACKAGES}
    for index, (name, (job, upload)) in enumerate(owners.items()):
        jobs.append({'id': 500 + index, 'name': job, 'run_id': RUN_ID, 'head_sha': SOURCE_SHA,
                     'status': 'completed', 'conclusion': 'success', 'started_at': START, 'completed_at': END,
                     'steps': [{'name': upload, 'status': 'completed', 'conclusion': 'success'}]})
        artifacts.append({'id': 100 + index, 'name': name, 'expired': False,
                          'size_in_bytes': 1024, 'digest': 'sha256:' + str(index) * 64,
                          'created_at': '2026-11-21T09:30:00Z',
                          'workflow_run': {'id': RUN_ID, 'head_sha': SOURCE_SHA, 'head_branch': 'main',
                                           'repository_id': 42, 'head_repository_id': 42}})
    jobs.append({'id': 600, 'name': resume.TAG_JOB, 'run_id': RUN_ID, 'head_sha': SOURCE_SHA,
                 'status': 'completed', 'conclusion': 'success', 'started_at': START, 'completed_at': END})
    return run, jobs, artifacts


def set_field(value, path, replacement):
    parts = path.split('.')
    for part in parts[:-1]:
        value = value[part]
    value[parts[-1]] = replacement


def evidence_fixture():
    return resume.validate_source(*source_fixture(), RUN_ID)


def packages_fixture(root, evidence):
    directory = root / 'packages'
    for artifact, (platform, architecture) in zip(resume.PACKAGES,
            (('Windows', 'x64'), ('macOS', 'x86_64'), ('macOS', 'arm64'))):
        folder = directory / artifact
        folder.mkdir(parents=True)
        base = f"OpenRV-4.1.0-build.{evidence['build_number']}-{platform}-{architecture}"
        names = ([base + '-Setup.exe', base + '-Portable.zip'] if platform == 'Windows'
                 else [base + '.dmg', base + '.zip'])
        for name in names:
            (folder / name).write_bytes(('original package ' + name).encode())
        manifest = {'repository': resume.TARGET,
                    'runUrl': f'https://github.com/{resume.TARGET}/actions/runs/{RUN_ID}',
                    'localizationSnapshot': SOURCE_SHA, 'buildNumber': evidence['build_number'],
                    'platform': platform, 'architecture': architecture, 'files': names}
        manifest_name = base + '.build.json'
        (folder / manifest_name).write_text(json.dumps(manifest), encoding='utf-8')
        (folder / (base + '.sha256.txt')).write_text(''.join(
            hashlib.sha256((folder / name).read_bytes()).hexdigest() + '  ' + name + '\n'
            for name in [*names, manifest_name]), encoding='ascii')
    return directory


class SourceEvidenceTests(unittest.TestCase):
    def test_accepts_one_original_completed_cohort_and_derives_product_number(self):
        evidence = evidence_fixture()
        self.assertEqual((evidence['source_run_id'], evidence['source_sha'], evidence['build_number']),
                         (RUN_ID, SOURCE_SHA, 35))
        self.assertEqual(set(evidence['artifacts']), {resume.INPUT, *resume.PACKAGES})
        self.assertEqual(len({item['id'] for item in evidence['artifacts'].values()}), 4)

    def test_rejects_wrong_repository_fork_run_attempt_head_or_workflow(self):
        mutations = [('repository.full_name', 'other/project'), ('head_repository.full_name', 'other/project'),
                     ('repository.id', True), ('head_repository.id', 43), ('id', RUN_ID + 1),
                     ('path', '.github/workflows/diagnostic.yml'), ('event', 'pull_request'),
                     ('head_branch', 'feature'), ('status', 'in_progress'), ('conclusion', 'cancelled'),
                     ('run_attempt', 2), ('run_attempt', True), ('head_sha', 'c' * 40),
                     ('head_sha', 'not-a-sha'), ('run_number', 0), ('run_number', True)]
        for path, replacement in mutations:
            with self.subTest(field=path, replacement=replacement):
                run, jobs, artifacts = source_fixture()
                set_field(run, path, replacement)
                with self.assertRaises(ValueError):
                    resume.validate_source(run, jobs, artifacts, RUN_ID)
        with self.assertRaises(ValueError):
            resume.validate_source(*source_fixture(), RUN_ID + 1)

    def test_every_required_job_and_upload_must_be_unique_and_successful(self):
        for position in range(5):
            for field, replacement in [('run_id', RUN_ID + 1), ('head_sha', 'c' * 40),
                                       ('status', 'queued'), ('conclusion', 'failure')]:
                with self.subTest(position=position, field=field):
                    run, jobs, artifacts = source_fixture()
                    jobs[position][field] = replacement
                    with self.assertRaises(ValueError):
                        resume.validate_source(run, jobs, artifacts, RUN_ID)
        for change in ('missing', 'duplicate', 'upload-failed', 'upload-duplicate', 'upload-missing'):
            with self.subTest(change=change):
                run, jobs, artifacts = source_fixture()
                if change == 'missing': jobs.pop(0)
                elif change == 'duplicate': jobs.append(copy.deepcopy(jobs[0]))
                elif change == 'upload-failed': jobs[0]['steps'][0]['conclusion'] = 'failure'
                elif change == 'upload-duplicate': jobs[0]['steps'] *= 2
                else: jobs[0]['steps'] = []
                with self.assertRaises(ValueError):
                    resume.validate_source(run, jobs, artifacts, RUN_ID)

    def test_artifact_origin_expiry_digest_size_time_and_id_collisions(self):
        mutations = [('expired', True), ('digest', ''), ('digest', 'sha256:' + 'z' * 64),
                     ('workflow_run.id', RUN_ID + 1), ('workflow_run.head_sha', 'c' * 40),
                     ('workflow_run.head_branch', 'feature'), ('workflow_run.repository_id', 99),
                     ('workflow_run.head_repository_id', 99), ('size_in_bytes', 0), ('id', True),
                     ('created_at', '2026-11-20T09:30:00Z')]
        for path, replacement in mutations:
            with self.subTest(field=path, replacement=replacement):
                run, jobs, artifacts = source_fixture()
                set_field(artifacts[0], path, replacement)
                with self.assertRaises(ValueError):
                    resume.validate_source(run, jobs, artifacts, RUN_ID)
        for change in ('missing', 'duplicate-name', 'same-id', 'oversized-input', 'oversized-package'):
            with self.subTest(change=change):
                run, jobs, artifacts = source_fixture()
                if change == 'missing': artifacts.pop(0)
                elif change == 'duplicate-name': artifacts.append(copy.deepcopy(artifacts[0]))
                elif change == 'same-id': artifacts[1]['id'] = artifacts[0]['id']
                elif change == 'oversized-input': artifacts[0]['size_in_bytes'] = 16 * 1024**2 + 1
                else: artifacts[1]['size_in_bytes'] = 8 * 1024**3 + 1
                with self.assertRaises(ValueError):
                    resume.validate_source(run, jobs, artifacts, RUN_ID)

    def test_source_evidence_reads_complete_pages_not_only_the_first_page(self):
        run, jobs, artifacts = source_fixture()
        first_jobs = [{'name': 'unrelated-' + str(index)} for index in range(100)]
        calls = []
        responses = {f'actions/runs/{RUN_ID}': run,
                     f'actions/runs/{RUN_ID}/jobs?per_page=100&page=1': {'jobs': first_jobs},
                     f'actions/runs/{RUN_ID}/jobs?per_page=100&page=2': {'jobs': jobs},
                     f'actions/runs/{RUN_ID}/artifacts?per_page=100&page=1': {'artifacts': artifacts}}
        def read(path):
            calls.append(path)
            return responses[path]
        self.assertEqual(resume.source_evidence(RUN_ID, read), evidence_fixture())
        self.assertEqual(set(calls), set(responses))

    def test_invalid_or_unbounded_pages_fail_before_artifact_selection(self):
        run, _, _ = source_fixture()
        for batch in (None, [{}] * 101):
            with self.subTest(batch_size=None if batch is None else len(batch)), self.assertRaises(ValueError):
                resume.source_evidence(RUN_ID, lambda path: run if path.endswith(str(RUN_ID)) else {'jobs': batch})
        calls = []
        def endless(path):
            calls.append(path)
            return run if path.endswith(str(RUN_ID)) else {'jobs': [{}] * 100}
        with self.assertRaisesRegex(ValueError, 'pagination'):
            resume.source_evidence(RUN_ID, endless)
        self.assertEqual(len(calls), 101)


class PackageStagingTests(unittest.TestCase):
    def test_staging_keeps_original_package_manifest_and_checksum_bytes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            evidence = evidence_fixture()
            packages = packages_fixture(root, evidence)
            original = {file.name: file.read_bytes() for folder in packages.iterdir() for file in folder.iterdir()}
            assets = root / 'assets'
            resume.stage_packages(packages, assets, evidence)
            self.assertEqual({file.name: file.read_bytes() for file in assets.iterdir()}, original)
            for checksum in assets.glob('*.sha256.txt'):
                for line in checksum.read_text().splitlines():
                    expected, name = line.split('  ')
                    self.assertEqual(hashlib.sha256((assets / name).read_bytes()).hexdigest(), expected)

    def test_manifest_must_belong_to_original_run_snapshot_and_build(self):
        for field, replacement in [('repository', 'other/project'), ('runUrl', 'https://example.org/run'),
                                   ('localizationSnapshot', CONTROLLER_SHA), ('buildNumber', 36)]:
            with self.subTest(field=field), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                evidence = evidence_fixture()
                packages = packages_fixture(root, evidence)
                manifest = next(packages.rglob('*.build.json'))
                record = json.loads(manifest.read_text())
                record[field] = replacement
                manifest.write_text(json.dumps(record))
                with self.assertRaisesRegex(ValueError, 'original source run'):
                    resume.stage_packages(packages, root / 'assets', evidence)

    def test_rejects_extra_files_collisions_and_linked_artifact_directory(self):
        for case in ('extra', 'collision', 'linked-directory', 'linked-file'):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                evidence = evidence_fixture()
                packages = packages_fixture(root, evidence)
                folders = [packages / name for name in resume.PACKAGES]
                if case == 'extra':
                    (folders[0] / 'unexpected.txt').write_text('not a release file')
                elif case == 'collision':
                    original_name = next(folders[0].glob('*.sha256.txt')).name
                    replaced = next(folders[1].glob('*.dmg'))
                    (folders[1] / original_name).write_bytes(replaced.read_bytes())
                    replaced.unlink()
                link = folders[0] if case == 'linked-directory' else next(folders[0].glob('*.exe'))
                original_is_symlink = Path.is_symlink
                # Windows test accounts may lack symlink privileges; exercise the
                # same refusal branch without requiring elevation to create one.
                with mock.patch.object(Path, 'is_symlink', lambda path:
                        (case.startswith('linked-') and path == link) or original_is_symlink(path)):
                    with self.assertRaises(ValueError):
                        resume.stage_packages(packages, root / 'assets', evidence)


class PublishExistingTests(unittest.TestCase):
    @contextlib.contextmanager
    def fixture(self, failure=None):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / 'recovery'
            repo = Path(temporary) / 'controller'
            root.mkdir(); repo.mkdir()
            evidence = evidence_fixture()
            packages_fixture(root, evidence)
            plan = {'localization_snapshot': SOURCE_SHA, 'build_number': 35, 'official': {'tag': 'v4.1.0'}}
            raw = json.dumps(plan).encode()
            current = {'GITHUB_ACTIONS': 'true', 'GITHUB_REPOSITORY': resume.TARGET,
                       'GITHUB_SHA': CONTROLLER_SHA, 'GITHUB_RUN_ID': '98765', 'GITHUB_RUN_NUMBER': '3',
                       'GITHUB_STEP_SUMMARY': str(root / 'summary.md')}
            def command(args, **kwargs):
                if args[:2] == ['git', 'clone']:
                    source = root / 'source'
                    script = source / 'ci/localized-release/assemble_release.py'
                    script.parent.mkdir(parents=True)
                    script.write_text('# immutable original assembler\n')
                if args[0] == sys.executable and failure == 'assembler':
                    raise subprocess.CalledProcessError(1, args, stderr='original checksum/native gate rejected cohort')
                return subprocess.CompletedProcess(args, 0)
            with mock.patch.dict(os.environ, current, clear=True), \
                    mock.patch.object(resume, 'source_evidence', return_value=evidence) as source_check, \
                    mock.patch.object(resume, 'read_input', return_value=(plan, b'', raw)) as input_check, \
                    mock.patch.object(resume, 'materialize') as materialize, \
                    mock.patch.object(resume, 'verify_materialized') as verify, \
                    mock.patch.object(resume, 'publish_assets') as publish, \
                    mock.patch.object(resume.subprocess, 'run', side_effect=command) as run:
                publish.return_value = {'status': 'published', 'requestedBuildNumber': 35,
                                        'publishedBuildNumber': 35, 'releaseTag': 'v4.1.0',
                                        'releaseId': 123, 'assetCount': 12}
                yield {'root': root, 'repo': repo, 'evidence': evidence, 'plan': plan, 'raw': raw, 'current': current,
                       'source_check': source_check, 'input_check': input_check, 'materialize': materialize,
                       'verify': verify, 'publish': publish, 'run': run}

    def test_original_assembler_and_product_identity_preserve_controller_github_identity(self):
        with self.fixture() as f:
            before = dict(os.environ)
            resume.publish_existing(f['repo'], f['root'], f['evidence'])
            source = f['root'] / 'source'
            f['materialize'].assert_called_once_with(source, f['root'] / 'input', resume.digest(f['raw']), SOURCE_SHA, 35)
            assembler = [call for call in f['run'].call_args_list if call.args[0][0] == sys.executable]
            self.assertEqual(len(assembler), 1)
            self.assertEqual(assembler[0].args[0][1], str(source / 'ci/localized-release/assemble_release.py'))
            self.assertEqual(assembler[0].kwargs['cwd'], source)
            environment = assembler[0].kwargs['env']
            for key, value in before.items():
                if key.startswith('GITHUB_'):
                    self.assertEqual(environment[key], value)
            self.assertEqual((environment['ORV_RELEASE_TAG'], environment['ORV_BUILD_NUMBER']), ('v4.1.0', '35'))
            self.assertEqual(environment['ORV_BUILD_PLAN_SHA256'], resume.digest(f['raw']))
            self.assertEqual(dict(os.environ), before)
            f['verify'].assert_called_once_with(source, f['plan'], b'', expected_snapshot=SOURCE_SHA, expected_build=35)
            f['publish'].assert_called_once()
            self.assertEqual(f['publish'].call_args.args[:3], (source, f['root'] / 'assets', f['plan']))
            self.assertEqual(f['publish'].call_args.kwargs, {'replace_legacy_v410': False})
            receipt = json.loads((f['root'] / 'publication-receipt.json').read_text())
            self.assertEqual((receipt['controller_sha'], receipt['publication_run_id']), (CONTROLLER_SHA, '98765'))
            self.assertFalse(receipt['native_rebuild'])

    def test_legacy_migration_requires_explicit_recovery_opt_in(self):
        with self.fixture() as f:
            resume.publish_existing(f['repo'], f['root'], f['evidence'], replace_legacy_v410=True)
            self.assertEqual(f['publish'].call_args.kwargs, {'replace_legacy_v410': True})
            receipt = json.loads((f['root'] / 'publication-receipt.json').read_text())
            self.assertTrue(receipt['replace_legacy_v410'])

    def test_retaining_newer_publication_does_not_claim_requested_build_was_published(self):
        with self.fixture() as f:
            f['publish'].return_value.update(status='skipped_newer', publishedBuildNumber=36)
            resume.publish_existing(f['repo'], f['root'], f['evidence'])
            receipt = json.loads((f['root'] / 'publication-receipt.json').read_text())
            self.assertEqual(receipt['status'], 'skipped_newer')
            self.assertEqual(receipt['publication']['publishedBuildNumber'], 36)
            summary = (f['root'] / 'summary.md').read_text()
            self.assertIn('requested build 35 was not published', summary)
            self.assertNotIn('Published v4.1.0', summary)

    def test_original_assembler_failure_never_calls_fixed_publisher(self):
        with self.fixture(failure='assembler') as f, self.assertRaises(subprocess.CalledProcessError):
            try:
                resume.publish_existing(f['repo'], f['root'], f['evidence'])
            finally:
                f['publish'].assert_not_called()
                f['verify'].assert_not_called()
                self.assertFalse((f['root'] / 'publication-receipt.json').exists())

    def test_changed_evidence_plan_materialization_or_final_gate_stops_publication(self):
        for gate in ('source-evidence', 'input', 'plan-snapshot', 'plan-build', 'materialize', 'final-source'):
            with self.subTest(gate=gate), self.fixture() as f:
                if gate == 'source-evidence':
                    changed = copy.deepcopy(f['evidence']); changed['artifacts'][resume.INPUT]['digest'] = 'sha256:' + 'f' * 64
                    f['source_check'].return_value = changed
                elif gate == 'input': f['input_check'].side_effect = ValueError('bad input plan/delta')
                elif gate == 'plan-snapshot': f['plan']['localization_snapshot'] = CONTROLLER_SHA
                elif gate == 'plan-build': f['plan']['build_number'] = 36
                elif gate == 'materialize': f['materialize'].side_effect = ValueError('composition mismatch')
                else: f['verify'].side_effect = ValueError('source changed after assembly')
                with self.assertRaises(ValueError):
                    resume.publish_existing(f['repo'], f['root'], f['evidence'])
                f['publish'].assert_not_called()
                self.assertFalse((f['root'] / 'publication-receipt.json').exists())


class PublicationApiScopeTests(unittest.TestCase):
    def test_authenticated_release_listing_uses_the_fixed_repository(self):
        workspace = sys.modules[resume.OfficialApi.__module__]
        response = mock.MagicMock()
        response.status = 200
        response.read.return_value = b'[]'
        response.__enter__.return_value = response
        with mock.patch.dict(os.environ, {'GH_TOKEN': 'fixture-token-not-a-credential'}), \
                mock.patch.object(workspace, 'urlopen', return_value=response) as request:
            status, records = resume.OfficialApi().request(
                'GET', resume.TARGET, 'releases?per_page=100&page=1')
        self.assertEqual((status, records), (200, []))
        sent = request.call_args.args[0]
        self.assertEqual(sent.full_url,
                         f'https://api.github.com/repos/{resume.TARGET}/releases?per_page=100&page=1')
        self.assertEqual(sent.get_method(), 'GET')
        self.assertEqual(sent.get_header('Authorization'), 'Bearer fixture-token-not-a-credential')

    def test_listing_does_not_enable_patch_or_outside_repository_requests(self):
        workspace = sys.modules[resume.OfficialApi.__module__]
        with mock.patch.object(workspace, 'urlopen') as request:
            for method, repository in (('PATCH', resume.TARGET), ('GET', 'other/project')):
                with self.subTest(method=method, repository=repository), self.assertRaises(ValueError):
                    resume.OfficialApi(write=True).request(method, repository, 'releases?per_page=100&page=1')
            request.assert_not_called()


if __name__ == '__main__':
    unittest.main()
