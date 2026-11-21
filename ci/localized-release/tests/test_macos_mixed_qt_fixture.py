"""Offline negative/positive report contracts; native Cocoa remains a CI proof."""
from __future__ import annotations

import copy
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[1] / 'macos_mixed_qt_fixture.py'
spec = importlib.util.spec_from_file_location('mixed_qt_fixture', SCRIPT)
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)


def report(app, embedded=True):
    return {'check': 'native-mixed-qt-python-fixture', 'productRuntimeVerified': False,
            'phase': 'finished', 'status': 'passed', 'embedded': embedded, 'qtVersion': '6.5.3',
            'platformPlugin': 'cocoa', 'cppAppPointer': '0x123abc', 'pythonAppPointer': '0x123abc',
            'cppArguments': ['actual-host'], 'pythonArguments': ['actual-host'], 'pythonInstancePresent': True,
            'sameApplicationInstance': True, 'pythonQtVersion': '6.5.3', 'pysideVersion': '6.5.3',
            'pythonDefaultProfileInitialized': True, 'cppDefaultProfileInitialized': True,
            'applicationDestroyedBeforePythonFinalization': True, 'pythonFinalizedNormally': True,
            'loadFinished': True, 'javascriptSentinel': True, 'visibleReadbackMatched': True,
            'widgetVisible': True, 'windowExposed': True, 'timedOut': False,
            'pixel': [32, 80, 160, 255], 'imageWidth': 128, 'imageHeight': 128,
            'loadedQtImages': [{'framework': name, 'realPath': str(app / ('Contents/Frameworks/' + name + '.framework/Versions/A/' + name))}
                               for name in sorted(fixture.REQUIRED_FRAMEWORKS)]}


def record(value):
    return {'timedOut': False, 'exitCode': 0, 'checkpoints': [value]}


class MixedQtContracts(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.app = self.root / 'actual mini.app'

    def test_legacy_negative_bundler_uses_frozen_file_without_git_history(self):
        # This temporary directory deliberately has no .git or upstream objects.
        repo = self.root / 'lightweight checkout'
        target = repo / fixture.OLD_BUNDLER_FIXTURE
        target.parent.mkdir(parents=True)
        original = SCRIPT.parent / 'fixtures/legacy_bundle_macos.py'
        target.write_bytes(original.read_bytes())
        self.assertEqual(fixture.legacy_bundler_bytes(repo), original.read_bytes())
        self.assertEqual(fixture.digest(target), fixture.OLD_BUNDLER_SHA256)
        raw = target.read_bytes()
        target.write_bytes(b'X' + raw[1:])
        with self.assertRaisesRegex(ValueError, 'reviewed original bytes'):
            fixture.legacy_bundler_bytes(repo)
        target.write_bytes(raw + b'\n')
        with self.assertRaisesRegex(ValueError, 'reviewed original bytes'):
            fixture.legacy_bundler_bytes(repo)

    def rejects_positive(self, value):
        with self.assertRaises(ValueError):
            fixture.validate_positive(value, self.app, True)

    def negative(self):
        value = report(self.app)
        value.update(phase='python-application-observed', status='failed', pythonInstancePresent=False,
                     pythonAppPointer=None, pythonArguments=[], sameApplicationInstance=False)
        value['loadedQtImages'].append({'framework': 'QtCore', 'realPath': str(self.app /
            'Contents/lib/python3.11/site-packages/PySide6/Qt/lib/QtCore.framework/Versions/A/QtCore')})
        return {'timedOut': False, 'exitCode': -6, 'checkpoints': [value]}

    def test_actual_singleton_arguments_visible_webengine_and_normal_exit_contract(self):
        value = record(report(self.app))
        self.assertEqual(fixture.validate_positive(value, self.app, True)['status'], 'passed')
        baseline = record(report(self.app, False))
        self.assertEqual(fixture.validate_positive(baseline, self.app, False)['status'], 'passed')

    def test_negative_requires_real_failure_split_empty_argv_and_loaded_duplicates(self):
        self.assertFalse(fixture.validate_negative(self.negative(), self.app)['sameApplicationInstance'])
        for change in ({'exitCode': 0}, {'timedOut': True}, {'exitCode': None}):
            value = self.negative()
            value.update(change)
            with self.subTest(change=change), self.assertRaises(ValueError):
                fixture.validate_negative(value, self.app)
        for change in ({'pythonArguments': ['different']}, {'sameApplicationInstance': True},
                       {'phase': 'finished'}, {'loadedQtImages': report(self.app)['loadedQtImages']}):
            value = self.negative()
            value['checkpoints'][0].update(change)
            with self.subTest(change=change), self.assertRaises(ValueError):
                fixture.validate_negative(value, self.app)

    def test_warning_text_or_collection_completion_cannot_replace_native_evidence(self):
        self.rejects_positive({'collectionStatus': 'completed', 'stderr': 'duplicate class warning'})
        with self.assertRaises(ValueError):
            fixture.validate_negative({'exitCode': -6, 'timedOut': False, 'checkpoints': []}, self.app)

    def test_console_proof_preserves_actual_exits_and_bounded_evidence_not_large_payloads(self):
        summary = {'check': 'mixed-qt-bundling-mechanism-fixture', 'status': 'passed',
            'controllerSourceSnapshot': 'a' * 40, 'negativeSourceSnapshot': fixture.OLD_SNAPSHOT,
            'bundlerSha256': {'negative': 'b' * 64, 'positive': 'c' * 64},
            'cases': {'negative': {'cppBaseline': record(report(self.app, False)), 'embedded': self.negative(),
                                  'expectedFailureReproduced': True},
                      'positive': {'cppBaseline': record(report(self.app, False)), 'embedded': record(report(self.app)),
                                   'strictSignatureVerified': True, 'embeddedContractAccepted': True}},
            'archiveProof': {'phase': 'completed', 'zipResourceManifestMatched': True,
                'zipStrictSignatureVerified': True, 'zipEmbeddedAccepted': True,
                'readOnlyMountVerified': True, 'dmgResourceManifestMatched': True,
                'dmgStrictSignatureVerified': True, 'dmgEmbeddedAccepted': True,
                'zipRestored': record(report(self.app)), 'dmgMounted': record(report(self.app))},
            'fixtureInputs': {'hugeUnrelatedPayload': 'do-not-print' * 100000},
            'error': 'x' * 100000}
        encoded = fixture.console_proof(summary)
        self.assertLessEqual(len(encoded.encode()), 12 * 1024)
        self.assertNotIn('do-not-print', encoded)
        value = json.loads(encoded)
        self.assertEqual(value['controllerSourceSnapshot'], 'a' * 40)
        self.assertEqual(value['bundlerSha256']['positive'], 'c' * 64)
        self.assertEqual(value['cases']['negative']['embedded']['exitCode'], -6)
        self.assertFalse(value['cases']['negative']['embedded']['argvEqual'])
        self.assertEqual(value['cases']['negative']['embedded']['requiredQtPhysicalProviderCounts']['QtCore'], 2)
        self.assertTrue(value['cases']['positive']['embedded']['sameApplicationInstance'])
        self.assertTrue(value['cases']['positive']['embedded']['argvEqual'])
        self.assertTrue(value['archives']['readOnlyMountVerified'])
        self.assertTrue(value['archives']['readonlyDmgEmbedded']['visibleReadbackMatched'])
        self.assertNotIn('loadedQtImages', encoded)

    def test_console_proof_never_invents_acceptance_for_unrun_or_partial_steps(self):
        value = json.loads(fixture.console_proof({'status': 'failed', 'cases': {
            'positive': {'embedded': {'timedOut': True, 'exitCode': None, 'checkpoints': []}}},
            'archiveProof': {'phase': 'zip-resources-aliases-signature-verified', 'zipStrictSignatureVerified': True}}))
        self.assertTrue(value['cases']['positive']['embedded']['timedOut'])
        self.assertIsNone(value['cases']['positive']['strictSignatureVerified'])
        self.assertIsNone(value['archives']['zipEmbeddedAccepted'])
        self.assertIsNone(value['archives']['readOnlyMountVerified'])
        self.assertIsNone(value['archives']['readonlyDmgEmbedded'])

    def test_singleton_pointer_arguments_versions_and_profile_all_must_agree(self):
        for change in ({'pythonAppPointer': '0x456'}, {'cppAppPointer': None, 'pythonAppPointer': None},
                       {'pythonInstancePresent': False}, {'cppArguments': [], 'pythonArguments': []},
                       {'pythonArguments': ['other']}, {'pysideVersion': '6.6.0'},
                       {'pythonDefaultProfileInitialized': False}, {'sameApplicationInstance': False}):
            value = record(report(self.app))
            value['checkpoints'][0].update(change)
            with self.subTest(change=change):
                self.rejects_positive(value)

    def test_wrong_application_python_cleanup_order_cannot_be_accepted(self):
        for change in ({'applicationDestroyedBeforePythonFinalization': False}, {'pythonFinalizedNormally': False}):
            value = record(report(self.app))
            value['checkpoints'][0].update(change)
            with self.subTest(change=change):
                self.rejects_positive(value)

    def test_archive_manifest_preserves_link_targets_modes_and_exact_resource_bytes(self):
        self.app.mkdir()
        framework = self.app / 'canonical framework'
        framework.mkdir()
        resource = framework / 'actual resource'
        resource.write_bytes(b'preserve exact resource bytes\n')
        link = self.app / 'relative alias'
        try:
            link.symlink_to('canonical framework', target_is_directory=True)
        except OSError:
            self.skipTest('Creating real relative symlinks is unavailable')
        first = fixture.tree_manifest(self.app)
        self.assertEqual(first['relative alias'], ['symlink', 'canonical framework'])
        self.assertEqual(first['canonical framework/actual resource'][2], fixture.digest(resource))
        resource.write_bytes(b'changed bytes\n')
        self.assertNotEqual(first, fixture.tree_manifest(self.app))
        link.unlink()
        link.symlink_to('../outside', target_is_directory=True)
        (self.root / 'outside').mkdir()
        with self.assertRaises(ValueError):
            fixture.tree_manifest(self.app)

    def test_every_visible_cocoa_requirement_is_required(self):
        for change in ({'platformPlugin': 'offscreen'}, {'platformPlugin': 'minimal'},
                       {'loadFinished': False}, {'javascriptSentinel': False}, {'visibleReadbackMatched': False},
                       {'widgetVisible': False}, {'windowExposed': False}, {'timedOut': True},
                       {'pixel': [0, 0, 0, 0]}, {'imageWidth': 0}, {'status': 'collection-completed'}):
            value = record(report(self.app))
            value['checkpoints'][0].update(change)
            with self.subTest(change=change):
                self.rejects_positive(value)

    def test_external_or_duplicate_physical_qt_provider_is_rejected(self):
        value = record(report(self.app))
        value['checkpoints'][0]['loadedQtImages'][0]['realPath'] = str(self.root / 'SDK/QtCore')
        self.rejects_positive(value)
        self.rejects_positive(record({**report(self.app), 'loadedQtImages': []}))
        value = record(report(self.app))
        value['checkpoints'][0]['loadedQtImages'].append({'framework': 'QtCore',
            'realPath': str(self.app / 'Contents/second-copy/QtCore')})
        self.rejects_positive(value)

    def test_failed_crashed_or_timed_out_child_cannot_pass_final_claim(self):
        for change in ({'exitCode': -6}, {'exitCode': 5}, {'timedOut': True}, {'exitCode': False}):
            value = record(report(self.app))
            value.update(change)
            with self.subTest(change=change):
                self.rejects_positive(value)

    def test_host_launch_removes_all_loader_and_fake_gui_overrides(self):
        secrets = {'DYLD_LIBRARY_PATH': '/external/sdk', 'DYLD_FRAMEWORK_PATH': '/external/frameworks',
                   'DYLD_FALLBACK_LIBRARY_PATH': '/external/fallback', 'QT_QPA_PLATFORM': 'offscreen',
                   'QT_QUICK_BACKEND': 'software', 'QT_OPENGL': 'software', 'QML_IMPORT_PATH': '/external',
                   'PYTHONPATH': '/external', 'PYTHONHOME': '/external', 'VIRTUAL_ENV': '/external',
                   'QTWEBENGINE_DISABLE_SANDBOX': '1', 'QTWEBENGINE_CHROMIUM_FLAGS': '--disable-gpu'}
        with mock.patch.dict(os.environ, secrets):
            environment, removed = fixture.clean_runtime_environment(self.root / 'isolated home')
        for key in secrets:
            self.assertNotIn(key, environment)
            self.assertIn(key, removed)
        self.assertEqual(environment['HOME'], str(self.root / 'isolated home'))

    def test_launch_preserves_partial_checkpoint_and_actual_abort_status(self):
        value = self.negative()['checkpoints'][0]
        raw = json.dumps(value) + '\n'
        process = subprocess.CompletedProcess([], -6, raw, 'Argument list is empty')
        with mock.patch.object(fixture.subprocess, 'run', return_value=process) as run:
            result = fixture.launch(self.app, self.root, 'abort', True)
        self.assertEqual(result['exitCode'], -6)
        self.assertEqual(result['checkpoints'], [value])
        self.assertEqual((self.root / 'abort.stderr.log').read_text(), 'Argument list is empty')
        self.assertEqual(run.call_args.kwargs['timeout'], 60)
        self.assertNotIn('DYLD_LIBRARY_PATH', run.call_args.kwargs['env'])

    def test_launch_timeout_does_not_turn_partial_pass_into_success(self):
        error = subprocess.TimeoutExpired([], 60, output=(json.dumps(report(self.app)) + '\n').encode(), stderr=b'hung')
        with mock.patch.object(fixture.subprocess, 'run', side_effect=error):
            result = fixture.launch(self.app, self.root, 'timeout', True)
        self.assertTrue(result['timedOut'])
        self.assertEqual(result['checkpoints'][-1]['status'], 'passed')
        self.rejects_positive(result)

    def test_wheel_inventory_is_real_separate_process_and_not_a_mixed_gui_pass(self):
        inventory = {'pysideVersion': '6.5.3', 'qtVersion': '6.5.3',
                     'nativeBindingFiles': ['PySide6/QtCore.abi3.so', 'PySide6/QtWebEngineWidgets.abi3.so']}
        process = subprocess.CompletedProcess([], 0, json.dumps(inventory), '')
        with mock.patch.object(fixture.subprocess, 'run', return_value=process) as run:
            result = fixture.binding_inventory(self.root, self.root / 'wheel import home')
        self.assertEqual(result, inventory)
        self.assertIn('-I', run.call_args.args[0])
        self.assertNotIn('status', result)
        bad = copy.deepcopy(inventory)
        bad['nativeBindingFiles'] = ['PySide6/../../external.so']
        with mock.patch.object(fixture.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, json.dumps(bad), '')):
            with self.assertRaises(ValueError):
                fixture.binding_inventory(self.root, self.root / 'bad inventory home')

    def test_clone_boundary_rejects_unknown_sources_and_nonprivate_or_existing_destinations_before_cp(self):
        sdk, private = self.root / 'sdk', self.root / 'private'
        sdk.mkdir(); private.mkdir()
        source = sdk / 'native image'
        source.write_bytes(b'real fixture source bytes')
        unknown = self.root / 'not-an-sdk'
        unknown.write_bytes(b'not authorized for clone')
        existing = private / 'existing'
        existing.write_bytes(b'preserve evidence')
        cloner = fixture.FixtureCloner(private, (sdk,))
        for origin, target in ((unknown, private / 'target'), (source, self.root / 'outside'), (source, existing)):
            with self.subTest(origin=origin, target=target), mock.patch.object(fixture.subprocess, 'run') as command:
                with self.assertRaises(ValueError):
                    cloner.copy(origin, target)
                command.assert_not_called()

    def test_clone_check_rejects_actual_reused_inode_and_records_independent_copy_identity(self):
        sdk, private = self.root / 'sdk', self.root / 'private'
        sdk.mkdir(); private.mkdir()
        source = sdk / 'native image'
        source.write_bytes(b'original SDK image bytes')
        cloner = fixture.FixtureCloner(private, (sdk,))
        target = private / 'hardlink'
        with mock.patch.object(fixture.subprocess, 'run', side_effect=lambda *args, **kwargs: os.link(source, target)):
            with self.assertRaisesRegex(ValueError, 'reused the source inode'):
                cloner.copy(source, target)
        # This exercises the checker using real filesystem copies, not APFS or
        # Mach-O simulation. Native CI must still execute the actual cp -c.
        target = private / 'independent'
        with mock.patch.object(fixture.subprocess, 'run', side_effect=lambda *args, **kwargs: shutil.copy2(source, target)):
            proof = cloner.copy(source, target)
        self.assertNotEqual((proof['sourceDevice'], proof['sourceInode']),
                            (proof['destinationDevice'], proof['destinationInode']))
        target.write_bytes(b'owned relocation bytes')
        self.assertEqual(source.read_bytes(), b'original SDK image bytes')

    @staticmethod
    def copy_backend(command, **kwargs):
        # Real FS copies exercise the boundary/identity checks. Native CI is
        # still required for actual APFS cloning and file/otool classification.
        if command[0] == '/bin/cp':
            source, target = map(Path, command[-2:])
            if source.is_dir():
                shutil.copytree(source, target, symlinks=True)
            else:
                shutil.copy2(source, target)
        return subprocess.CompletedProcess(command, 0, 'offline classification fixture\n', '')

    def test_runtime_seed_keeps_stdlib_native_extensions_and_config_text_but_isolates_development_inputs(self):
        prefix, private = self.root / 'python-source', self.root / 'private'
        stdlib = prefix / 'lib/python3.11'
        config = stdlib / 'config-3.11-darwin'
        config.mkdir(parents=True)
        (stdlib / 'lib-dynload').mkdir()
        (stdlib / 'site-packages').mkdir()
        (stdlib / 'os.py').write_text('ordinary stdlib source')
        (stdlib / '_sysconfigdata__darwin_darwin.py').write_text('runtime build variables')
        (stdlib / 'lib-dynload/actual-runtime.so').write_bytes(b'preserve native runtime extension')
        (stdlib / 'site-packages/unrelated-package').write_text('exclude runner packages')
        (config / 'Makefile').write_text('retain sysconfig text')
        (config / 'config.c').write_text('retain ordinary config source')
        (config / 'python.o').write_bytes(bytes.fromhex('cafebabe00000002') + b'offline development object')
        (config / 'libpython3.11.a').write_bytes(b'!<arch>\nordinary development archive')
        (prefix / 'lib/unrelated-development-library').write_bytes(b'never seed all prefix/lib')
        library = prefix / 'Python'
        library.write_bytes(b'actual interpreter input fixture')
        app = private / 'seed.app'
        (app / 'Contents/lib').mkdir(parents=True)
        cloner = fixture.FixtureCloner(private, (prefix,))
        evidence = {}
        with mock.patch.object(fixture, '_cloner', cloner), \
             mock.patch.object(fixture.sysconfig, 'get_config_var', return_value=str(config)), \
             mock.patch.object(fixture.subprocess, 'run', side_effect=self.copy_backend), \
             mock.patch.object(fixture, 'run', return_value='host:\n' + str(library) + ' (compatibility version 3.11.0)'):
            result = fixture.prepare_python(app, prefix, library, evidence)
        owned_stdlib = app / 'Contents/lib/python-runtime/lib/python3.11'
        self.assertEqual((owned_stdlib / 'config-3.11-darwin/Makefile').read_text(), 'retain sysconfig text')
        self.assertTrue((owned_stdlib / '_sysconfigdata__darwin_darwin.py').is_file())
        self.assertEqual((owned_stdlib / 'lib-dynload/actual-runtime.so').read_bytes(), b'preserve native runtime extension')
        self.assertEqual(list((owned_stdlib / 'site-packages').iterdir()), [])
        self.assertFalse((owned_stdlib.parent / 'unrelated-development-library').exists())
        self.assertEqual(Path(result['ownedLibrary']).read_bytes(), library.read_bytes())
        excluded = evidence['pythonDevelopmentIsolation']['excludedDevelopmentInputs']
        self.assertEqual({Path(value['path']).name for value in excluded}, {'python.o', 'libpython3.11.a'})
        for value in excluded:
            self.assertTrue(value['excludedFromRuntime'])
            self.assertFalse(Path(value['isolationPath']).is_relative_to(app))
            self.assertEqual(fixture.digest(Path(value['isolationPath'])), value['sha256'])
            self.assertTrue(value['header32Hex'])
            self.assertIn('exitCode', value['file'])
            self.assertIn('exitCode', value['otoolHeader'])
            self.assertTrue(Path(value['path']).is_file())
            self.assertFalse((owned_stdlib / 'config-3.11-darwin' / Path(value['path']).name).exists())

    def test_sysconfig_libpl_must_be_exact_real_direct_python311_config(self):
        prefix = self.root / 'python-source'
        stdlib = prefix / 'lib/python3.11'
        valid = stdlib / 'config-3.11-darwin'
        valid.mkdir(parents=True)
        wrong_version = stdlib / 'config-3.12-darwin'
        wrong_version.mkdir()
        nested = valid / 'config-3.11-nested'
        nested.mkdir()
        outside = self.root / 'config-3.11-outside'
        outside.mkdir()
        for value in (None, 'relative/config-3.11-darwin', str(wrong_version), str(nested), str(outside)):
            with self.subTest(value=value), mock.patch.object(fixture.sysconfig, 'get_config_var', return_value=value):
                with self.assertRaises(ValueError):
                    fixture.python_runtime_layout(prefix)
        with mock.patch.object(fixture.sysconfig, 'get_config_var', return_value=str(valid)):
            self.assertEqual(fixture.python_runtime_layout(prefix), (stdlib, valid))

    def test_exact_framework_development_alias_rebinds_to_owned_actual_library_and_unknown_alias_fails(self):
        prefix, private = self.root / 'python-source', self.root / 'private'
        libpl = prefix / 'lib/python3.11/config-3.11-darwin'
        libpl.mkdir(parents=True)
        library = prefix / 'Python'
        library.write_bytes(b'actual framework interpreter input')
        source_alias = libpl / 'libpython3.11.dylib'
        try:
            source_alias.symlink_to('../../../Python')
        except OSError:
            self.skipTest('Creating real framework-style symlinks is unavailable')
        app = private / 'seed.app'
        owned = app / 'Contents/lib/python-runtime/lib/Python'
        owned.parent.mkdir(parents=True)
        owned.write_bytes(library.read_bytes())
        destination = owned.parent / 'python3.11' / libpl.name / source_alias.name
        destination.parent.mkdir(parents=True)
        destination.symlink_to('../../../Python')
        evidence = {}
        cloner = fixture.FixtureCloner(private, (prefix,))
        with mock.patch.object(fixture, '_cloner', cloner):
            fixture.bind_python_development_aliases(app, libpl, library, owned, evidence)
            self.assertEqual(destination.resolve(strict=True), owned)
            self.assertFalse(Path(os.readlink(destination)).is_absolute())
            self.assertEqual(evidence['verifiedFrameworkLibraryAliases'][0]['sourceLibrarySha256'], fixture.digest(library))
            unexpected = libpl / 'libanother.dylib'
            unexpected.symlink_to('../../../Python')
            with self.assertRaisesRegex(ValueError, 'Unexpected Python development framework alias'):
                fixture.bind_python_development_aliases(app, libpl, library, owned, {})
        self.assertEqual(os.readlink(source_alias), '../../../Python')

    def test_delocate_custom_exception_retains_exact_consumer_header_and_classification_then_fails(self):
        self.app.mkdir()
        consumer = self.app / 'actual-runtime-input.so'
        consumer.write_bytes(bytes.fromhex('cafebabe00000002') + b'exact unresolved input')
        class ExternalInstallNameError(Exception):
            pass
        external = types.ModuleType('delocate.libsana')
        def dependencies(*args, **kwargs):
            yield '/usr/lib/libSystem.B.dylib', '/usr/lib/libSystem.B.dylib'
            raise ExternalInstallNameError('LLVM bit-code classification from real third party type')
        external.get_dependencies = dependencies
        evidence = {}
        with mock.patch.dict(sys.modules, {'delocate.libsana': external}), \
             mock.patch.object(fixture.subprocess, 'run', side_effect=self.copy_backend):
            with self.assertRaisesRegex(RuntimeError, 'actual-runtime-input.so'):
                fixture.enumerate_python_dependencies(consumer, self.app, evidence, self.root / 'source-input')
        failure = evidence['nativeConsumerFailure']
        self.assertEqual(evidence['status'], 'failed')
        self.assertEqual(failure['path'], str(consumer))
        self.assertEqual(failure['consumerRelativePath'], consumer.name)
        self.assertEqual(failure['errorType'], 'ExternalInstallNameError')
        self.assertEqual(failure['sha256'], fixture.digest(consumer))
        self.assertEqual(failure['header32Hex'], consumer.read_bytes()[:32].hex())
        self.assertEqual(failure['file']['exitCode'], 0)
        self.assertIn('offline classification fixture', failure['otoolHeader']['output'])

    def test_cli_unexpected_preparation_exception_writes_failed_summary_and_exit_one(self):
        class ExternalInstallNameError(Exception):
            pass
        output = self.root / 'failure-proof'
        arguments = ['fixture', '--qt', str(self.root), '--bindings', str(self.root),
            '--work', str(self.root / 'private'), '--output', str(output), '--arch', 'arm64']
        # Interrupt before any macOS tool runs; this specifically tests the CLI
        # exception boundary rather than treating an offline probe as native.
        with mock.patch.object(fixture.sys, 'argv', arguments), \
             mock.patch.object(fixture, 'require', side_effect=[ExternalInstallNameError('unexpected third-party failure'), None]), \
             contextlib.redirect_stdout(io.StringIO()) as console:
            result = fixture.main()
        summary = json.loads((output / 'summary.json').read_text())
        self.assertEqual(result, 1)
        self.assertEqual(summary['status'], 'failed')
        self.assertEqual(summary['errorType'], 'ExternalInstallNameError')
        self.assertEqual(summary['phase'], 'initializing')
        self.assertFalse(summary['productRuntimeVerified'])
        self.assertEqual(json.loads(console.getvalue())['errorType'], 'ExternalInstallNameError')

    def test_provider_policy_keeps_inner_framework_but_never_copies_outer_python_framework(self):
        framework = self.root / 'Python.framework'
        prefix = framework / 'Versions/3.11'
        source = prefix / 'lib/libruntime.dylib'
        source.parent.mkdir(parents=True)
        source.write_bytes(b'actual scanned provider fixture')
        self.assertEqual(fixture.python_dependency_copy_policy(source, prefix),
                         ('copy-provider-file-inside-outer-python-framework', framework))
        inner = prefix / 'lib/Native.framework/Versions/A/Native'
        inner.parent.mkdir(parents=True)
        inner.write_bytes(b'inner native framework fixture')
        self.assertEqual(fixture.python_dependency_copy_policy(inner, prefix),
                         ('copy-prefix-contained-framework', prefix / 'lib/Native.framework'))
        other = framework / 'Versions/3.12/Python'
        other.parent.mkdir(parents=True)
        other.write_bytes(b'unknown interpreter version')
        with self.assertRaisesRegex(ValueError, 'outside the verified interpreter prefix'):
            fixture.python_dependency_copy_policy(other, prefix)

    def provider_graph(self, *, external=False):
        prefix = self.root / 'Python.framework/Versions/3.11'
        source_consumer = prefix / 'lib/python3.11/lib-dynload/consumer.so'
        source_consumer.parent.mkdir(parents=True)
        source_consumer.write_bytes(b'native consumer fixture')
        library = prefix / 'Python'
        library.write_bytes(b'actual interpreter fixture')
        source = prefix / 'lib/libruntime.dylib'
        if external:
            source = self.root / 'Python.framework/Versions/3.12/Python'
            source.parent.mkdir(parents=True)
        source.write_bytes(b'exact runtime provider fixture')
        private = self.root / 'private'
        app = private / 'seed.app'
        runtime = app / 'Contents/lib/python-runtime'
        consumer = runtime / 'lib/python3.11/lib-dynload/consumer.so'
        consumer.parent.mkdir(parents=True)
        consumer.write_bytes(source_consumer.read_bytes())
        owned_library = runtime / 'lib/Python'
        owned_library.write_bytes(library.read_bytes())
        brew, qt = self.root / 'brew', self.root / 'qt'
        brew.mkdir(); qt.mkdir()
        return prefix, library, source, app, consumer, owned_library, brew, qt

    def test_scanned_prefix_provider_is_copied_as_single_file_then_recursively_analysed_and_recorded(self):
        prefix, library, source, app, consumer, owned_library, brew, qt = self.provider_graph()
        cloner = fixture.FixtureCloner(app.parent, (prefix,))
        evidence, inspected = {}, []
        def dependencies(current, *args):
            inspected.append(current)
            return [(str(source), '/actual/install/id/libruntime.dylib')] if current == consumer else []
        with mock.patch.object(fixture, '_cloner', cloner), \
             mock.patch.object(fixture, 'run', return_value=str(brew)), \
             mock.patch.object(fixture, 'is_macho', side_effect=lambda path: path.is_file() and not path.is_symlink()), \
             mock.patch.object(fixture, 'enumerate_python_dependencies', side_effect=dependencies), \
             mock.patch.object(fixture.subprocess, 'run', side_effect=self.copy_backend) as commands:
            records = fixture.own_python_dependencies(app, prefix, library, owned_library, qt, evidence)
        self.assertEqual(evidence['status'], 'passed')
        self.assertEqual(len(records), 1)
        target = app / records[0]['destination']
        self.assertEqual(target.read_bytes(), source.read_bytes())
        self.assertIn(target, inspected)
        self.assertNotEqual(target.stat().st_ino, source.stat().st_ino)
        self.assertFalse((app / 'Contents/Frameworks/Python.framework').exists())
        latest = evidence['latestProvider']
        self.assertEqual(latest['resolvedProvider'], str(source))
        self.assertEqual(latest['frameworkCandidate'], str(self.root / 'Python.framework'))
        self.assertEqual(latest['consumerRelativePath'], consumer.relative_to(app).as_posix())
        self.assertEqual(latest['decision'], 'copy-provider-file-inside-outer-python-framework')
        self.assertEqual(latest['status'], 'completed')
        changes = [call.args[0] for call in commands.call_args_list if call.args[0][0] == 'install_name_tool']
        self.assertEqual(changes[0][2], '/actual/install/id/libruntime.dylib')
        self.assertTrue(changes[0][3].startswith('@loader_path/'))

    def test_other_python_version_fails_before_copy_with_exact_provider_context_in_summary(self):
        prefix, library, source, app, consumer, owned_library, brew, qt = self.provider_graph(external=True)
        evidence = {}
        def dependencies(current, *args):
            return [(str(source), '/actual/other/python/id')] if current == consumer else []
        with mock.patch.object(fixture, 'run', return_value=str(brew)), \
             mock.patch.object(fixture, 'is_macho', side_effect=lambda path: path.is_file() and not path.is_symlink()), \
             mock.patch.object(fixture, 'enumerate_python_dependencies', side_effect=dependencies), \
             mock.patch.object(fixture.subprocess, 'run') as command:
            with self.assertRaisesRegex(RuntimeError, 'outside the verified interpreter version'):
                fixture.own_python_dependencies(app, prefix, library, owned_library, qt, evidence)
        command.assert_not_called()
        latest = evidence['latestProvider']
        self.assertEqual(evidence['status'], 'failed')
        self.assertEqual(latest['loadIdentity'], '/actual/other/python/id')
        self.assertEqual(latest['resolvedProvider'], str(source))
        self.assertEqual(latest['status'], 'failed')
        self.assertEqual(latest['decision'], 'verify-interpreter-provider-version')
        encoded = fixture.console_proof({'status': 'failed', 'fixturePreparation': {'pythonDependencyAnalysis': evidence}})
        self.assertLessEqual(len(encoded.encode()), 12 * 1024)
        proof = json.loads(encoded)['latestPythonProvider']
        self.assertEqual(proof['loadIdentity'], '/actual/other/python/id')
        self.assertEqual(proof['consumerRelativePath'], consumer.relative_to(app).as_posix())
        self.assertEqual(proof['resolvedProvider'], str(source))
        self.assertEqual(proof['decision'], 'verify-interpreter-provider-version')

    def wheel_fixture(self):
        bindings, packages = self.root / 'official-wheel-cache', self.root / 'owned-packages'
        pyside = bindings / 'PySide6'
        pyside.mkdir(parents=True)
        (bindings / 'shiboken6').mkdir()
        (bindings / 'shiboken6/__init__.py').write_text('ordinary shiboken module')
        (pyside / '__init__.py').write_text('ordinary pyside module')
        # Structural copy-filter inputs only; these bytes do not certify a
        # loadable binary or replace the actual Mac Cocoa/WebEngine fixture.
        marker = bytes.fromhex('cffaedfe') + b'offline native-classification fixture'
        (pyside / 'QtCore.abi3.so').write_bytes(marker)
        (pyside / 'UnusedBinding.abi3.so').write_bytes(marker)
        (pyside / 'libpyside6.abi3.6.5.dylib').write_bytes(marker)
        for app_name, executable in {**fixture.WHEEL_TOOL_APPS, 'Unknown.app': 'Unknown',
                                     'QtWebEngineProcess.app': 'QtWebEngineProcess'}.items():
            app = pyside / app_name
            (app / 'Contents/MacOS').mkdir(parents=True)
            (app / 'Contents/Resources').mkdir()
            (app / 'Contents/MacOS' / executable).write_bytes(marker)
            (app / 'Contents/Resources/resource').write_text('tool resource must not survive without its bundle')
            with (app / 'Contents/Info.plist').open('wb') as stream:
                plistlib.dump({'CFBundleExecutable': executable, 'CFBundleIdentifier': 'org.qt.' + executable}, stream)
        packages.mkdir()
        return bindings, packages, {'nativeBindingFiles': ['PySide6/QtCore.abi3.so']}

    def test_official_wheel_tools_are_excluded_whole_with_audit_and_source_unchanged(self):
        bindings, packages, inventory = self.wheel_fixture()
        pyside = bindings / 'PySide6'
        before = fixture.tree_manifest(bindings)
        sdk_helper = self.root / 'actual-sdk/QtWebEngineCore.framework/Helpers/QtWebEngineProcess.app/Contents/MacOS/QtWebEngineProcess'
        sdk_helper.parent.mkdir(parents=True)
        sdk_helper.write_bytes(b'actual SDK helper source is outside wheel-copy scope')
        evidence = {}
        fixture.copy_wheel_bindings(bindings, packages, inventory, evidence)
        self.assertEqual(fixture.tree_manifest(bindings), before)
        self.assertEqual({value['appName'] for value in evidence['toolAppExclusions']}, set(fixture.WHEEL_TOOL_APPS))
        for value in evidence['toolAppExclusions']:
            self.assertTrue(value['excludedFromRuntime'])
            self.assertTrue(value['sourceBytesUnchanged'])
            self.assertEqual(value['observedBindingMembers'], [])
            self.assertFalse((packages / 'PySide6' / value['appName']).exists())
            self.assertTrue((pyside / value['appName'] / 'Contents/Info.plist').is_file())
            self.assertEqual(fixture.digest(Path(value['executablePath'])), value['executableSha256'])
        self.assertTrue((packages / 'PySide6/QtCore.abi3.so').is_file())
        self.assertTrue((packages / 'PySide6/libpyside6.abi3.6.5.dylib').is_file())
        self.assertFalse((packages / 'PySide6/UnusedBinding.abi3.so').exists())
        # The exact whitelist does not turn into a generic .app exclusion.
        self.assertTrue((packages / 'PySide6/Unknown.app/Contents/Info.plist').is_file())
        self.assertTrue((packages / 'PySide6/QtWebEngineProcess.app/Contents/Info.plist').is_file())
        self.assertEqual(sdk_helper.read_bytes(), b'actual SDK helper source is outside wheel-copy scope')
        proof = json.loads(fixture.console_proof({'fixturePreparation': {'wheelBindings': evidence}}))
        self.assertEqual(set(proof['excludedWheelToolApps']), set(fixture.WHEEL_TOOL_APPS))

    def test_inventory_member_or_invalid_tool_identity_prevents_whole_app_exclusion(self):
        bindings, packages, inventory = self.wheel_fixture()
        inventory['nativeBindingFiles'].append('PySide6/Assistant.app/Contents/MacOS/Assistant')
        with self.assertRaisesRegex(ValueError, 'contains an observed binding'):
            fixture.copy_wheel_bindings(bindings, packages, inventory, {})
        self.assertEqual(list(packages.iterdir()), [])
        inventory['nativeBindingFiles'].pop()
        assistant = bindings / 'PySide6/Assistant.app'
        with (assistant / 'Contents/Info.plist').open('wb') as stream:
            plistlib.dump({'CFBundleExecutable': 'UnexpectedTool'}, stream)
        with self.assertRaisesRegex(ValueError, 'Unexpected official wheel tool executable'):
            fixture.copy_wheel_bindings(bindings, packages, inventory, {})
        self.assertEqual(list(packages.iterdir()), [])


if __name__ == '__main__':
    unittest.main()
