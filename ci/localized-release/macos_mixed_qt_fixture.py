"""Native mechanism fixture for C++/PySide Qt ownership, not product acceptance."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import platform
import plistlib
import re
import shutil
import stat
import subprocess
import sys
import sysconfig

OLD_SNAPSHOT = '457b9665d6b4871dac244a9c674a0acd1d3cc054'
OLD_BUNDLER_FIXTURE = 'ci/localized-release/fixtures/legacy_bundle_macos.py'
OLD_BUNDLER_BYTES = 15768
OLD_BUNDLER_SHA256 = '1349ac3788fc2533bf34d57ebc0a02ccc833408ffba689033f38ac32abf77b1a'
QT_VERSION = '6.5.3'
REQUIRED_FRAMEWORKS = {'QtCore', 'QtGui', 'QtWidgets', 'QtWebEngineCore'}
# Exact v6.5.3 build_scripts.PYSIDE_UNIX_BUNDLED_TOOLS and macOS bundle names.
WHEEL_TOOL_APPS = {'Assistant.app': 'Assistant', 'Designer.app': 'Designer', 'Linguist.app': 'Linguist'}
MAGICS = {bytes.fromhex(value) for value in ('feedface', 'cefaedfe', 'feedfacf', 'cffaedfe',
                                            'cafebabe', 'bebafeca', 'cafebabf', 'bfbafeca')}
_cloner = None


def require(condition, message):
    if not condition:
        raise ValueError(message)


def run(*command, cwd=None):
    return subprocess.check_output(command, cwd=cwd, text=True, stderr=subprocess.PIPE, timeout=60).strip()


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def legacy_bundler_bytes(repo):
    """Read the reviewed negative fixture without importing upstream Git history."""
    repo = repo.resolve(strict=True)
    path = repo / OLD_BUNDLER_FIXTURE
    require(path.resolve(strict=True).is_relative_to(repo) and not path.is_symlink() and path.is_file(),
            'Legacy bundler fixture must be a regular file inside the trusted checkout')
    raw = path.read_bytes()
    require(len(raw) == OLD_BUNDLER_BYTES and hashlib.sha256(raw).hexdigest() == OLD_BUNDLER_SHA256,
            'Legacy bundler fixture differs from its reviewed original bytes')
    return raw


def is_macho(path):
    if path.is_symlink() or not path.is_file() or path.suffix == '.a':
        return False
    with path.open('rb') as stream:
        header = stream.read(8)
    if header[:4] == bytes.fromhex('cafebabe') and not 1 <= int.from_bytes(header[4:8], 'big') <= 16:
        return False
    return header[:4] in MAGICS


def load_module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def clean_runtime_environment(home):
    environment = os.environ.copy()
    removed = []
    for key in tuple(environment):
        if key.startswith('DYLD_') or key.startswith(('QT_', 'QML_')) or key in (
                'PYTHONHOME', 'PYTHONPATH', 'VIRTUAL_ENV', 'QTWEBENGINE_CHROMIUM_FLAGS', 'QTWEBENGINE_DISABLE_SANDBOX'):
            removed.append(key)
            environment.pop(key)
    environment.update(HOME=str(home), CFFIXED_USER_HOME=str(home),
                       XDG_CACHE_HOME=str(home / 'cache'), XDG_CONFIG_HOME=str(home / 'config'),
                       PYTHONNOUSERSITE='1', PYTHONDONTWRITEBYTECODE='1')
    return environment, sorted(removed)


def image_providers(report, app):
    images = report.get('loadedQtImages')
    require(isinstance(images, list), 'Native checkpoint has no dyld image evidence')
    providers = {}
    for image in images:
        require(isinstance(image, dict) and isinstance(image.get('framework'), str)
                and isinstance(image.get('realPath'), str) and image['realPath'], 'Invalid dyld provider identity')
        path = Path(image['realPath']).resolve()
        require(path.is_absolute() and path.is_relative_to(app), 'Loaded Qt provider escapes the mini.app')
        providers.setdefault(image['framework'], set()).add(str(path))
    require(REQUIRED_FRAMEWORKS.issubset(providers), 'The required Qt frameworks were not really loaded')
    return providers


def validate_positive(record, app, embedded):
    require(record.get('timedOut') is False and type(record.get('exitCode')) is int and record['exitCode'] == 0,
            'Native process did not exit normally with zero')
    reports = record.get('checkpoints', [])
    require(bool(reports) and reports[-1].get('phase') == 'finished', 'Native process did not reach its final checkpoint')
    final = reports[-1]
    require(final.get('status') == 'passed' and final.get('qtVersion') == QT_VERSION
            and final.get('platformPlugin') == 'cocoa' and final.get('embedded') is embedded,
            'Native final identity or Cocoa mode is incorrect')
    for field in ('loadFinished', 'javascriptSentinel', 'visibleReadbackMatched', 'widgetVisible', 'windowExposed'):
        require(final.get(field) is True, 'Visible WebEngine requirement failed: ' + field)
    require(final.get('timedOut') is False, 'Native WebEngine event loop timed out')
    require(all(type(final.get(name)) is int and final[name] > 0 for name in ('imageWidth', 'imageHeight')),
            'Native visible WebEngine image is empty')
    pixel = final.get('pixel')
    require(isinstance(pixel, list) and len(pixel) == 4 and all(type(value) is int for value in pixel)
            and all(abs(value - expected) <= 5 for value, expected in zip(pixel[:3], (32, 80, 160)))
            and 250 <= pixel[3] <= 255, 'Actual visible WebEngine pixel does not match the local page')
    providers = image_providers(final, app)
    require(all(len(paths) == 1 for paths in providers.values()), 'Multiple physical Qt providers remain loaded')
    if embedded:
        require(final.get('sameApplicationInstance') is True and final.get('pythonInstancePresent') is True
                and isinstance(final.get('cppAppPointer'), str)
                and re.fullmatch(r'0x[1-9a-f][0-9a-f]*', final['cppAppPointer']) is not None
                and final.get('pythonAppPointer') == final.get('cppAppPointer')
                and isinstance(final.get('cppArguments'), list) and bool(final['cppArguments'])
                and all(isinstance(value, str) for value in final['cppArguments']) and bool(final['cppArguments'][0])
                and final.get('pythonArguments') == final['cppArguments']
                and final.get('pythonQtVersion') == QT_VERSION and final.get('pysideVersion') == QT_VERSION
                and final.get('pythonDefaultProfileInitialized') is True
                and final.get('applicationDestroyedBeforePythonFinalization') is True
                and final.get('pythonFinalizedNormally') is True,
                'Embedded PySide does not share the C++ QApplication/arguments/WebEngine initialization')
    else:
        require(final.get('cppDefaultProfileInitialized') is True, 'Pure C++ WebEngine baseline did not initialize')
    return final


def validate_negative(record, app):
    require(record.get('timedOut') is False and type(record.get('exitCode')) is int and record['exitCode'] != 0,
            'Old actual bundler did not reproduce a real process failure')
    observed = [report for report in record.get('checkpoints', [])
                if report.get('phase') == 'python-application-observed']
    require(len(observed) == 1, 'Negative process did not preserve its pre-WebEngine singleton checkpoint')
    report = observed[0]
    split = report.get('pythonInstancePresent') is False or report.get('pythonAppPointer') != report.get('cppAppPointer')
    require(split and report.get('sameApplicationInstance') is False
            and report.get('pythonArguments') == [] and bool(report.get('cppArguments')),
            'Negative failure does not reproduce the observed singleton/empty-arguments mechanism')
    providers = image_providers(report, app)
    require(any(len(paths) > 1 for paths in providers.values()), 'Negative process has no actual dual-provider evidence')
    return report


def launch(app, output, name, embedded):
    home = output / (name + '-home')
    home.mkdir()
    environment, removed = clean_runtime_environment(home)
    command = [str(app / 'Contents/MacOS/openrv-mixed-qt')]
    if embedded:
        command += ['--embedded', '--python-home', str(app / 'Contents/lib/python-runtime'),
                    '--packages', str(app / 'Contents/lib/python3.11/site-packages')]
    record = {'timedOut': False, 'exitCode': None, 'checkpoints': [], 'removedOverrides': removed}
    stdout, stderr = '', ''
    try:
        process = subprocess.run(command, cwd=home, env=environment, capture_output=True,
            text=True, encoding='utf-8', errors='replace', timeout=60)
        stdout, stderr = process.stdout, process.stderr
        record['exitCode'] = process.returncode
    except subprocess.TimeoutExpired as error:
        record['timedOut'] = True
        stdout = error.stdout.decode('utf-8', errors='replace') if isinstance(error.stdout, bytes) else (error.stdout or '')
        stderr = error.stderr.decode('utf-8', errors='replace') if isinstance(error.stderr, bytes) else (error.stderr or '')
    (output / (name + '.stdout.ndjson')).write_text(stdout, encoding='utf-8')
    (output / (name + '.stderr.log')).write_text(stderr, encoding='utf-8')
    for line in stdout.splitlines():
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict) and value.get('check') == 'native-mixed-qt-python-fixture':
            require(value.get('productRuntimeVerified') is False, 'Probe must not claim product acceptance')
            record['checkpoints'].append(value)
    return record


def console_proof(summary):
    """Bounded actual evidence; full checkpoints/resources stay in artifacts."""
    def text(value, limit=128):
        return value[:limit] if isinstance(value, str) else None

    def process(record):
        if not isinstance(record, dict):
            return None
        reports = [value for value in record.get('checkpoints', []) if isinstance(value, dict)]
        final = reports[-1] if reports else {}
        providers = {name: set() for name in sorted(REQUIRED_FRAMEWORKS)}
        for image in final.get('loadedQtImages', []):
            if isinstance(image, dict) and image.get('framework') in providers and isinstance(image.get('realPath'), str):
                providers[image['framework']].add(image['realPath'])
        cpp_arguments, python_arguments = final.get('cppArguments'), final.get('pythonArguments')
        return {
            'exitCode': record.get('exitCode'), 'timedOut': record.get('timedOut'),
            'phase': text(final.get('phase'), 80), 'nativeStatus': text(final.get('status'), 32),
            'platformPlugin': text(final.get('platformPlugin'), 32),
            'cppAppPointer': text(final.get('cppAppPointer'), 64),
            'pythonAppPointer': text(final.get('pythonAppPointer'), 64),
            'sameApplicationInstance': final.get('sameApplicationInstance'),
            'pythonInstancePresent': final.get('pythonInstancePresent'),
            'cppArgc': len(cpp_arguments) if isinstance(cpp_arguments, list) else None,
            'pythonArgc': len(python_arguments) if isinstance(python_arguments, list) else None,
            'argvEqual': cpp_arguments == python_arguments if isinstance(cpp_arguments, list)
                and isinstance(python_arguments, list) else None,
            'cppArgv0': text(cpp_arguments[0], 256) if isinstance(cpp_arguments, list) and cpp_arguments else None,
            'requiredQtPhysicalProviderCounts': {name: len(paths) for name, paths in providers.items()},
            **{field: final.get(field) if type(final.get(field)) is bool else None for field in (
                'cppDefaultProfileInitialized', 'pythonDefaultProfileInitialized', 'loadFinished',
                'javascriptSentinel', 'visibleReadbackMatched', 'widgetVisible', 'windowExposed',
                'applicationDestroyedBeforePythonFinalization', 'pythonFinalizedNormally')},
        }

    cases = {}
    for name in ('negative', 'positive'):
        case = summary.get('cases', {}).get(name, {})
        if case:
            cases[name] = {key: case.get(key) for key in (
                'bundlingStatus', 'strictSignatureVerified', 'cppBaselineAccepted', 'embeddedContractAccepted',
                'expectedFailureReproduced')}
            cases[name].update(cppBaseline=process(case.get('cppBaseline')), embedded=process(case.get('embedded')))
    archive = summary.get('archiveProof', {})
    preparation = summary.get('fixturePreparation', {})
    development = preparation.get('pythonDevelopmentIsolation', {})
    excluded = development.get('excludedDevelopmentInputs', [])
    latest_provider = preparation.get('pythonDependencyAnalysis', {}).get('latestProvider', {})
    def input_summary(value):
        if not isinstance(value, dict):
            return None
        return {**{name: text(value.get(name), limit) for name, limit in (
                    ('path', 384), ('sourcePath', 384), ('consumerRelativePath', 256),
                    ('header32Hex', 64), ('sha256', 64), ('errorType', 64), ('error', 512))},
                **{name: {'exitCode': value.get(name, {}).get('exitCode'),
                          'output': text(value.get(name, {}).get('output'), 384)}
                   for name in ('file', 'otoolHeader')}}
    result = {
        'check': text(summary.get('check')), 'status': text(summary.get('status'), 32),
        'phase': text(summary.get('phase'), 80), 'errorType': text(summary.get('errorType'), 64),
        'productRuntimeVerified': False, 'architecture': text(summary.get('architecture'), 16),
        'controllerSourceSnapshot': text(summary.get('controllerSourceSnapshot'), 40),
        'negativeSourceSnapshot': text(summary.get('negativeSourceSnapshot'), 40),
        'bundlerSha256': {name: text(summary.get('bundlerSha256', {}).get(name), 64)
                          for name in ('negative', 'positive')},
        'cases': cases,
        'archives': {**{key: archive.get(key) for key in ('phase', 'zipResourceManifestMatched',
            'zipStrictSignatureVerified', 'zipEmbeddedAccepted', 'readOnlyMountVerified',
            'dmgResourceManifestMatched', 'dmgStrictSignatureVerified', 'dmgEmbeddedAccepted')},
            'zipEmbedded': process(archive.get('zipRestored')), 'readonlyDmgEmbedded': process(archive.get('dmgMounted'))},
        'sdkAndPythonSourceBytesUnchanged': summary.get('sdkAndPythonSourceBytesUnchanged'),
        'pythonDevelopmentIsolation': {'excludedCount': len(excluded),
            'firstTwoInputs': [input_summary(value) for value in excluded[:2]],
            'verifiedAliasCount': len(development.get('verifiedFrameworkLibraryAliases', []))},
        'nativeConsumerFailure': input_summary(preparation.get('pythonDependencyAnalysis', {}).get('nativeConsumerFailure')),
        'latestPythonProvider': {name: text(latest_provider.get(name), limit) for name, limit in (
            ('consumerRelativePath', 256), ('loadIdentity', 384), ('reportedProvider', 384),
            ('resolvedProvider', 384), ('frameworkCandidate', 384), ('decision', 80),
            ('status', 32), ('errorType', 64), ('error', 512))},
        'excludedWheelToolApps': [value['appName'] for value in preparation.get('wheelBindings', {}).get('toolAppExclusions', [])
                                 if value.get('excludedFromRuntime') is True],
        'error': text(summary.get('error'), 1024),
    }
    encoded = json.dumps(result, sort_keys=True, ensure_ascii=True, allow_nan=False)
    require(len(encoded.encode('utf-8')) <= 12 * 1024, 'Console proof exceeded its explicit 12 KiB bound')
    return encoded


def copy_tree(source, destination):
    require(_cloner is not None, 'Private clone policy has not been initialized')
    return _cloner.copy(source, destination, directory=True)


class FixtureCloner:
    def __init__(self, root, sources):
        self.root = root.resolve(strict=True)
        self.sources = tuple(path.resolve(strict=True) for path in sources) + (self.root,)
        self.records = []

    def copy(self, source, destination, *, directory=False):
        source = source.resolve(strict=True)
        destination = destination.absolute()
        require(any(source.is_relative_to(root) for root in self.sources), 'Clone source is outside known SDK/fixture roots: ' + str(source))
        require(destination.is_relative_to(self.root) and destination.parent.resolve(strict=True).is_relative_to(self.root)
                and not destination.exists() and not destination.is_symlink(), 'Clone destination must be fresh and inside the private fixture')
        require(source.is_dir() if directory else source.is_file(), 'Clone source type differs from the requested copy')
        command = ['/bin/cp', '-cpR' if directory else '-cp', str(source), str(destination)]
        subprocess.run(command, check=True, timeout=180)
        source_info, target_info = source.stat(), destination.stat()
        require((source_info.st_dev, source_info.st_ino) != (target_info.st_dev, target_info.st_ino),
                'Clone unexpectedly reused the source inode')
        record = {'source': str(source), 'destination': str(destination), 'mechanism': 'APFS-copy-on-write-clone',
                  'sourceDevice': source_info.st_dev, 'sourceInode': source_info.st_ino,
                  'destinationDevice': target_info.st_dev, 'destinationInode': target_info.st_ino}
        record['metadata'] = self.metadata(source, destination)
        critical = []
        candidates = [Path()] if not directory else [Path('Versions/A/QtCore'),
            Path('Contents/Frameworks/QtCore.framework/Versions/A/QtCore')]
        for relative in candidates:
            original = source / relative if directory else source
            copied = destination / relative if directory else destination
            if not original.is_file():
                continue
            first, second = original.stat(), copied.stat()
            require((first.st_dev, first.st_ino) != (second.st_dev, second.st_ino), 'Critical image clone reused its source inode')
            require(digest(original) == digest(copied), 'Critical image clone changed source bytes')
            critical.append({'relativePath': str(relative), 'sha256': digest(original),
                'sourceDevice': first.st_dev, 'sourceInode': first.st_ino,
                'destinationDevice': second.st_dev, 'destinationInode': second.st_ino,
                'metadata': self.metadata(original, copied)})
        record['criticalImages'] = critical
        self.records.append(record)
        return record

    @staticmethod
    def metadata(source, destination):
        mode = stat.S_IMODE(source.stat().st_mode)
        require(mode == stat.S_IMODE(destination.stat().st_mode), 'Clone changed source mode bits')
        attributes = {}
        if hasattr(os, 'listxattr'):
            def values(path):
                return {name: hashlib.sha256(os.getxattr(path, name)).hexdigest()
                        for name in os.listxattr(path)}
            attributes = values(source)
            require(attributes == values(destination), 'Clone changed SDK extended attributes')
        return {'mode': mode, 'extendedAttributeSha256': attributes,
                'extendedAttributesVerified': hasattr(os, 'listxattr')}

    def remove_private(self, path):
        path = path.absolute()
        require(path.is_relative_to(self.root) and path.parent.resolve(strict=True).is_relative_to(self.root),
                'Private cleanup target escapes fixture root')
        if path.is_symlink() or path.is_file(): path.unlink()
        elif path.is_dir(): shutil.rmtree(path)


def tree_manifest(app):
    """Exact post-sign data/modes/link targets; do not export large file maps."""
    app = app.resolve(strict=True)
    entries = {}
    def visit(directory):
        for path in sorted(directory.iterdir()):
            relative = path.relative_to(app).as_posix()
            info = path.lstat()
            if path.is_symlink():
                require(path.resolve(strict=True).is_relative_to(app), 'Archive fixture alias escapes the app')
                entries[relative] = ['symlink', os.readlink(path)]
            elif path.is_dir():
                entries[relative] = ['directory', stat.S_IMODE(info.st_mode)]
                visit(path)
            elif path.is_file():
                entries[relative] = ['file', stat.S_IMODE(info.st_mode), digest(path)]
            else:
                raise ValueError('Unsupported app resource in archive fixture: ' + relative)
    visit(app)
    return entries


def archive_proof(app, work, output, records):
    from sign_macos import verify_app
    expected = tree_manifest(app)
    aliases = {name: value[1] for name, value in expected.items()
               if name.startswith('Contents/lib/python3.11/site-packages/PySide6/Qt/lib/')
               and name.endswith('.framework') and value[0] == 'symlink'}
    require(bool(aliases), 'Positive fixture has no actual standalone Qt framework aliases')
    manifest_sha256 = hashlib.sha256(json.dumps(expected, sort_keys=True).encode()).hexdigest()
    records.update(signedManifestSha256=manifest_sha256, manifestEntries=len(expected), frameworkAliases=aliases)
    def save(phase):
        records['phase'] = phase
        (output / 'archive-proof.json').write_text(json.dumps(records, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    save('signed-baseline')
    archive = work / 'mixed-qt-fixture.zip'
    subprocess.run(['ditto', '-c', '-k', '--sequesterRsrc', '--keepParent', str(app), str(archive)], check=True, timeout=300)
    restored = work / 'zip-restored'
    restored.mkdir()
    subprocess.run(['ditto', '-x', '-k', str(archive), str(restored)], check=True, timeout=300)
    restored_app = restored / app.name
    require(tree_manifest(restored_app) == expected, 'ZIP roundtrip changed signed bytes, modes, or relative aliases')
    records['zipResourceManifestMatched'] = True
    verify_app(restored_app)
    records['zipStrictSignatureVerified'] = True
    save('zip-resources-aliases-signature-verified')
    records['zipRestored'] = launch(restored_app, output, 'positive-zip-embedded', True)
    validate_positive(records['zipRestored'], restored_app.resolve(), True)
    records['zipEmbeddedAccepted'] = True
    save('zip-mixed-native-passed')
    staging = work / 'dmg-staging'
    staging.mkdir()
    copy_tree(app, staging / app.name)
    image, mount = work / 'mixed-qt-fixture.dmg', work / 'readonly-mount'
    mount.mkdir()
    subprocess.run(['hdiutil', 'create', '-volname', 'OpenRVMixedQtFixture', '-srcfolder', str(staging),
                    '-format', 'UDZO', '-fs', 'HFS+', str(image)], check=True, timeout=900)
    subprocess.run(['hdiutil', 'verify', str(image)], check=True, timeout=180)
    attached = False
    try:
        subprocess.run(['hdiutil', 'attach', '-readonly', '-nobrowse', '-mountpoint', str(mount), str(image)],
                       check=True, timeout=120)
        attached = True
        readonly = bool(os.statvfs(mount).f_flag & os.ST_RDONLY)
        require(readonly, 'DMG mount is not actually read-only')
        mounted_app = (mount / app.name).resolve(strict=True)
        require(tree_manifest(mounted_app) == expected, 'Read-only DMG changed signed bytes, modes, or relative aliases')
        records['dmgResourceManifestMatched'] = True
        verify_app(mounted_app)
        records['dmgStrictSignatureVerified'] = True
        records['readOnlyMountVerified'] = readonly
        save('readonly-dmg-resources-aliases-signature-verified')
        records['dmgMounted'] = launch(mounted_app, output, 'positive-dmg-embedded', True)
        validate_positive(records['dmgMounted'], mounted_app, True)
        records['dmgEmbeddedAccepted'] = True
    finally:
        if attached or (mount / app.name).exists():
            subprocess.run(['hdiutil', 'detach', str(mount)], check=True, timeout=120)
    save('completed')
    return records


def binding_inventory(bindings, home):
    """A separate wheel-only process discovers actual transitive imports.

    This is not an embedded-host or GUI check. Its Qt tree is never copied into
    the fixture; only the observed bindings and libpyside/shiboken are retained.
    """
    home.mkdir()
    environment, _ = clean_runtime_environment(home)
    code = """
import importlib, json, pathlib, sys
root = pathlib.Path(sys.argv[1]).resolve()
sys.path.insert(0, str(root))
import PySide6
from PySide6 import QtCore
for name in ('QtWidgets', 'QtWebEngineCore', 'QtWebEngineWidgets'):
    importlib.import_module('PySide6.' + name)
files = []
for name, module in tuple(sys.modules.items()):
    path = getattr(module, '__file__', '') or ''
    if name.startswith('PySide6.') and path.endswith('.so'):
        resolved = pathlib.Path(path).resolve()
        if not resolved.is_relative_to(root):
            raise RuntimeError('Binding imported outside isolated wheel cache')
        files.append(resolved.relative_to(root).as_posix())
print(json.dumps({'pysideVersion': PySide6.__version__, 'qtVersion': QtCore.qVersion(),
                  'nativeBindingFiles': sorted(set(files))}))
"""
    process = subprocess.run([sys.executable, '-I', '-c', code, str(bindings)], cwd=home, env=environment,
        capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=60)
    require(process.returncode == 0, 'Isolated official wheel binding import inventory failed: ' + process.stderr[-2000:])
    inventory = json.loads(process.stdout)
    require(inventory.get('pysideVersion') == QT_VERSION and inventory.get('qtVersion') == QT_VERSION,
            'Official wheel inventory version differs from the SDK version')
    files = inventory.get('nativeBindingFiles')
    require(isinstance(files, list) and bool(files) and all(isinstance(name, str)
            and name.startswith('PySide6/') and '..' not in Path(name).parts for name in files), 'Invalid binding import inventory')
    return inventory


def python_home():
    prefix = Path(sys.base_prefix).resolve(strict=True)
    require((prefix / 'lib/python3.11/os.py').is_file(), 'Runner Python 3.11 stdlib is missing')
    return prefix


def native_input_evidence(path):
    """Classify an exact input without allowing a diagnostic error to hide it."""
    evidence = {'path': str(path), 'resolvedPath': str(path.resolve()),
                'symlinkTarget': os.readlink(path) if path.is_symlink() else None}
    try:
        with path.open('rb') as stream:
            evidence['header32Hex'] = stream.read(32).hex()
        evidence['sha256'] = digest(path)
    except OSError as error:
        evidence['readError'] = type(error).__name__ + ': ' + str(error)[:512]
    for name, command in (('file', ['file', '-Lb', str(path)]), ('otoolHeader', ['otool', '-hv', str(path)])):
        try:
            result = subprocess.run(command, capture_output=True, text=True,
                encoding='utf-8', errors='replace', timeout=30)
            raw = result.stdout + '\n' + result.stderr
            evidence[name] = {'exitCode': result.returncode, 'output': raw[:4096],
                              'outputCharacters': len(raw), 'truncated': len(raw) > 4096}
        except (OSError, subprocess.SubprocessError) as error:
            evidence[name] = {'diagnosticError': type(error).__name__ + ': ' + str(error)[:512]}
    return evidence


def python_runtime_layout(prefix):
    stdlib = (prefix / 'lib/python3.11').resolve(strict=True)
    configured = sysconfig.get_config_var('LIBPL')
    require(isinstance(configured, str) and configured and Path(configured).is_absolute(),
            'Runner sysconfig.LIBPL is not an absolute Python development directory')
    libpl = Path(configured).resolve(strict=True)
    require(stdlib.is_dir() and libpl.is_dir() and libpl.parent == stdlib
            and re.fullmatch(r'config-3\.11-.+', libpl.name) is not None,
            'Runner sysconfig.LIBPL is not the verified direct Python 3.11 stdlib config directory')
    return stdlib, libpl


def isolate_python_development(app, stdlib, libpl, evidence):
    """Keep config text, isolate only exact LIBPL .o/.a inputs outside mini.app."""
    owned = app / 'Contents/lib/python-runtime/lib/python3.11' / libpl.name
    quarantine = app.parent / 'python-development-inputs'
    require(owned.is_dir() and not owned.is_symlink() and owned.resolve().is_relative_to(app),
            'Owned Python LIBPL is not a private real directory')
    require(not quarantine.exists() and not quarantine.is_symlink(), 'Development isolation must be fresh')
    quarantine.mkdir()
    evidence.update(sourceStdlib=str(stdlib), verifiedSourceLibpl=str(libpl), ownedLibpl=str(owned),
                    isolationRoot=str(quarantine), excludedDevelopmentInputs=[])
    for source in sorted(libpl.rglob('*')):
        if source.suffix not in ('.o', '.a'):
            continue
        require(source.is_file() and source.resolve(strict=True).is_relative_to(stdlib.parent.parent),
                'Python development input escapes its verified interpreter prefix')
        relative = source.relative_to(libpl)
        target = quarantine / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        identity = native_input_evidence(source)
        identity.update(reason='verified-sysconfig-LIBPL-development-object-or-archive',
                        sourceRelativePath=relative.as_posix(), isolationPath=str(target))
        evidence['excludedDevelopmentInputs'].append(identity)
        identity['cloneProof'] = _cloner.copy(source, target)
        require(digest(target) == identity['sha256'], 'Isolated development input bytes differ from source')
        _cloner.remove_private(owned / relative)
        identity['excludedFromRuntime'] = True
    evidence['retainedConfigFiles'] = sorted(path.relative_to(owned).as_posix()
        for path in owned.rglob('*') if path.is_file() and path.suffix not in ('.o', '.a'))
    require(not any(path.suffix in ('.o', '.a') for path in owned.rglob('*')),
            'A Python development object remains in the runtime LIBPL')


def bind_python_development_aliases(app, libpl, library, owned_library, evidence):
    """Repair only verified framework-style LIBPL aliases to this interpreter."""
    records = []
    evidence['verifiedFrameworkLibraryAliases'] = records
    require(owned_library.is_file() and owned_library.resolve().is_relative_to(app),
            'Owned interpreter library is missing or outside the app')
    for source in sorted(libpl.rglob('*.dylib')):
        if not source.is_symlink():
            continue
        identity = {'sourcePath': str(source), 'sourceLinkTarget': os.readlink(source),
                    'sourceResolvedPath': str(source.resolve()), 'status': 'unverified'}
        records.append(identity)
        require(source.parent == libpl and re.fullmatch(r'libpython3\.11[a-z]*\.dylib', source.name) is not None
                and source.resolve(strict=True) == library,
                'Unexpected Python development framework alias: ' + str(source))
        target = app / 'Contents/lib/python-runtime/lib/python3.11' / libpl.name / source.name
        require(target.is_symlink() and os.readlink(target) == identity['sourceLinkTarget'],
                'Owned Python framework alias differs from the verified source')
        relative = os.path.relpath(owned_library, target.parent)
        identity.update(sourceLibrarySha256=digest(source), ownedPath=str(target), ownedLinkTarget=relative)
        _cloner.remove_private(target)
        target.symlink_to(relative)
        require(target.resolve(strict=True) == owned_library.resolve(strict=True),
                'Rebound Python alias does not resolve to the actual owned interpreter')
        identity['status'] = 'bound-to-actual-owned-interpreter'


def enumerate_python_dependencies(consumer, app, evidence, source=None):
    from delocate.libsana import get_dependencies
    try:
        # Eagerly enumerate so generator errors also preserve the exact input.
        return list(get_dependencies(str(consumer), executable_path=str(app / 'Contents/MacOS')))
    except Exception as error:
        failure = native_input_evidence(consumer)
        failure.update(consumerRelativePath=consumer.relative_to(app).as_posix(),
                       sourcePath=str(source) if source is not None else None,
                       errorType=type(error).__name__, error=str(error)[:2048])
        evidence['nativeConsumerFailure'] = failure
        evidence['status'] = 'failed'
        raise RuntimeError('Python runtime dependency analysis failed for ' + str(consumer)
                           + ': ' + type(error).__name__ + ': ' + str(error)) from error


def prepare_python(app, prefix, library, evidence):
    destination = app / 'Contents/lib/python-runtime'
    stdlib, libpl = python_runtime_layout(prefix)
    # Only runtime stdlib/lib-dynload and the actual CMake interpreter library;
    # do not import prefix/lib's development libraries and framework aliases.
    destination.mkdir()
    (destination / 'lib').mkdir()
    copy_tree(stdlib, destination / 'lib/python3.11')
    evidence['pythonDevelopmentIsolation'] = {}
    isolate_python_development(app, stdlib, libpl, evidence['pythonDevelopmentIsolation'])
    site = destination / 'lib/python3.11/site-packages'
    if site.exists() or site.is_symlink():
        _cloner.remove_private(site)
    site.mkdir()
    owned_library = destination / 'lib' / library.name
    if owned_library.exists() or owned_library.is_symlink():
        _cloner.remove_private(owned_library)
    python_clone = _cloner.copy(library, owned_library)
    bind_python_development_aliases(app, libpl, library, owned_library,
                                   evidence['pythonDevelopmentIsolation'])
    executable = app / 'Contents/MacOS/openrv-mixed-qt'
    # FindPython's framework link may use its absolute resolved path or the
    # version's Python path. Require the real executable load command to match.
    changed = False
    for line in run('otool', '-L', str(executable)).splitlines()[1:]:
        dependency = line.strip().split(' (', 1)[0]
        if dependency == str(library) or ((dependency.startswith(str(prefix) + '/') or dependency.startswith('@rpath/'))
                                         and Path(dependency).name == library.name):
            relative = os.path.relpath(owned_library, executable.parent).replace(os.sep, '/')
            subprocess.run(['install_name_tool', '-change', dependency, '@loader_path/' + relative,
                            str(executable)], check=True, timeout=30)
            changed = True
    require(changed, 'Tiny executable Python load command was not bound to the owned runtime copy')
    return {'sourceHome': str(prefix), 'ownedHome': str(destination), 'version': platform.python_version(),
            'sourceLibrary': str(library), 'ownedLibrary': str(owned_library), 'librarySha256': digest(library),
            'libraryCloneProof': python_clone}


def python_dependency_copy_policy(source, prefix):
    require(source.is_relative_to(prefix), 'Python runtime provider is outside the verified interpreter prefix: ' + str(source))
    framework = next((parent for parent in source.parents if parent.suffix == '.framework'), None)
    if framework is not None and framework.is_relative_to(prefix):
        return 'copy-prefix-contained-framework', framework
    # The interpreter prefix can itself be Versions/3.11 inside Python.framework.
    # Its outer framework is not a runtime provider or a permitted copy root.
    require(framework is None or (framework.name == 'Python.framework' and prefix.is_relative_to(framework)),
            'Unexpected outer framework around the verified Python interpreter prefix')
    return ('copy-provider-file-inside-outer-python-framework' if framework else 'copy-prefix-provider-file'), framework


def own_python_dependencies(app, prefix, library, owned_library, qt, evidence):
    """Pre-copy the interpreter's real closure; never expand production roots."""
    destination = app / 'Contents/lib/python-runtime'
    brew = Path(run('brew', '--prefix')).resolve()
    mapping = {library.resolve(): owned_library}
    for target in destination.rglob('*'):
        source = prefix / target.relative_to(destination)
        if is_macho(target) and source.is_file():
            mapping.setdefault(source.resolve(), target)
    pending = sorted(path for path in destination.rglob('*') if is_macho(path))
    records, seen = [], set()
    evidence.update(status='running', inspectedConsumers=0, ownedExternalDependencies=records, providerDecisions=[])
    previous = os.environ.get('DYLD_FALLBACK_LIBRARY_PATH')
    os.environ['DYLD_FALLBACK_LIBRARY_PATH'] = os.pathsep.join((str(prefix / 'lib'), str(library.parent), str(qt / 'lib')))
    try:
        while pending:
            consumer = pending.pop()
            if consumer in seen:
                continue
            seen.add(consumer)
            evidence['inspectedConsumers'] += 1
            original = next((origin for origin, target in mapping.items() if target == consumer), None)
            for provider, identity in enumerate_python_dependencies(consumer, app, evidence, original):
                item = {'consumerRelativePath': consumer.relative_to(app).as_posix(), 'loadIdentity': identity,
                        'reportedProvider': provider, 'resolvedProvider': None, 'frameworkCandidate': None,
                        'decision': 'resolve-provider', 'status': 'processing'}
                evidence['providerDecisions'].append(item)
                evidence['latestProvider'] = item
                try:
                    require(provider is not None, 'Cannot resolve copied Python dependency: ' + identity)
                    if provider.startswith(('/System/', '/usr/lib/')):
                        item.update(decision='system-provider', status='completed')
                        continue
                    source = Path(provider).resolve(strict=True)
                    framework = next((parent for parent in source.parents if parent.suffix == '.framework'), None)
                    item.update(resolvedProvider=str(source), frameworkCandidate=str(framework) if framework else None)
                    if source.is_relative_to(app):
                        pending.append(source)
                        item.update(decision='existing-app-provider', status='completed')
                        continue
                    item['decision'] = 'verify-interpreter-provider-version'
                    require(framework is None or framework.name != 'Python.framework'
                            or source.is_relative_to(prefix) or source == library.resolve(),
                            'Python framework provider is outside the verified interpreter version: ' + str(source))
                    if source.is_relative_to(qt) or source.is_relative_to(brew):
                        item.update(decision='unchanged-production-qt-or-brew-provider', status='completed')
                        continue  # The unchanged production bundler owns these roots.
                    target = mapping.get(source)
                    if target is None:
                        item['decision'] = 'verify-new-provider-prefix'
                        decision, framework = python_dependency_copy_policy(source, prefix)
                        item['decision'] = decision
                        if decision == 'copy-prefix-contained-framework':
                            root = app / 'Contents/Frameworks' / framework.name
                            require(not root.exists(), 'Python dependency framework collides with an existing provider')
                            copy_tree(framework, root)
                            for member in root.rglob('*'):
                                if is_macho(member):
                                    mapping[(framework / member.relative_to(root)).resolve()] = member
                                    pending.append(member)
                            target = root / source.relative_to(framework)
                        else:
                            target = app / 'Contents/lib/python-runtime-extra' / digest(source)[:16] / source.name
                            target.parent.mkdir(parents=True, exist_ok=True)
                            _cloner.copy(source, target)
                            mapping[source] = target
                            pending.append(target)
                        records.append({'source': str(source), 'destination': target.relative_to(app).as_posix(),
                                        'sourceSha256': digest(source)})
                    else:
                        item['decision'] = 'reuse-verified-owned-provider'
                    require(target.is_file() and target.resolve().is_relative_to(app), 'Owned Python dependency is invalid')
                    relative = os.path.relpath(target, consumer.parent).replace(os.sep, '/')
                    subprocess.run(['install_name_tool', '-change', identity, '@loader_path/' + relative,
                                    str(consumer)], check=True, timeout=30)
                    item.update(status='completed', ownedProvider=target.relative_to(app).as_posix())
                except Exception as error:
                    item.update(status='failed', errorType=type(error).__name__, error=str(error)[:2048])
                    evidence['status'] = 'failed'
                    raise RuntimeError('Python provider relocation failed for ' + str(consumer)
                                       + ' load ' + str(identity) + ' provider ' + str(provider)
                                       + ': ' + str(error)) from error
    finally:
        if previous is None: os.environ.pop('DYLD_FALLBACK_LIBRARY_PATH', None)
        else: os.environ['DYLD_FALLBACK_LIBRARY_PATH'] = previous
    evidence['status'] = 'passed'
    return records


def copy_wheel_bindings(bindings, packages, inventory, evidence):
    """Copy observed bindings; omit only verified root-level wheel tool apps."""
    selected = set(inventory['nativeBindingFiles'])
    pyside = bindings / 'PySide6'
    require(pyside.is_dir() and not pyside.is_symlink(), 'Official wheel PySide6 root must be a real directory')
    pyside = pyside.resolve(strict=True)
    exclusions = []
    evidence['toolAppExclusions'] = exclusions
    excluded_names = set()
    for app_name, executable_name in WHEEL_TOOL_APPS.items():
        tool = pyside / app_name
        if not tool.exists() and not tool.is_symlink():
            continue
        require(tool.is_dir() and not tool.is_symlink() and tool.resolve(strict=True).parent == pyside,
                'Official wheel tool app is not a real direct PySide6 child: ' + str(tool))
        prefix = 'PySide6/' + app_name + '/'
        require(not any(value.startswith(prefix) or value == prefix[:-1] for value in selected),
                'A wheel tool app contains an observed binding and cannot be excluded: ' + app_name)
        info = tool / 'Contents/Info.plist'
        require(info.is_file() and info.resolve(strict=True).is_relative_to(tool), 'Wheel tool Info.plist escapes its bundle')
        with info.open('rb') as stream:
            metadata = plistlib.load(stream)
        require(metadata.get('CFBundleExecutable') == executable_name, 'Unexpected official wheel tool executable identity')
        executable = tool / 'Contents/MacOS' / executable_name
        require(executable.is_file() and executable.resolve(strict=True).is_relative_to(tool) and is_macho(executable),
                'Official wheel tool has no real app-owned native executable: ' + str(executable))
        exclusions.append({'source': str(tool), 'appName': app_name,
            'reason': 'official-PySide-6.5.3-wheel-tool-not-in-observed-binding-inventory',
            'infoPath': str(info), 'infoSha256': digest(info),
            'executablePath': str(executable), 'executableSha256': digest(executable),
            'observedBindingMembers': [], 'excludedFromRuntime': False})
        excluded_names.add(app_name)
    for name in ('PySide6', 'shiboken6'):
        source = bindings / name
        require(source.is_dir(), 'Missing official wheel bindings: ' + name)
        def ignore(directory, names):
            excluded = {'Qt'} if Path(directory) == source else set()
            if name == 'PySide6':
                if Path(directory) == source:
                    excluded.update(excluded_names)
                for filename in names:
                    path = Path(directory) / filename
                    relative = path.relative_to(bindings).as_posix()
                    if is_macho(path) and relative not in selected and not re.fullmatch(r'libpyside6.*\.dylib', filename):
                        excluded.add(filename)
            return excluded
        shutil.copytree(source, packages / name, symlinks=True, ignore=ignore)
    for value in exclusions:
        require(not (packages / 'PySide6' / value['appName']).exists()
                and not (packages / 'PySide6' / value['appName']).is_symlink(),
                'A wheel-only tool app survived its whole-bundle exclusion')
        require(digest(Path(value['infoPath'])) == value['infoSha256']
                and digest(Path(value['executablePath'])) == value['executableSha256'],
                'The original wheel tool source changed during fixture construction')
        value.update(excludedFromRuntime=True, sourceBytesUnchanged=True)


def prepare_seed(built_app, seed, qt, bindings, arch, python_library, preparation):
    copy_tree(built_app, seed)
    contents = seed / 'Contents'
    packages = contents / 'lib/python3.11/site-packages'
    packages.mkdir(parents=True)
    inventory_bindings = binding_inventory(bindings, seed.parent / 'wheel-import-home')
    preparation['wheelBindings'] = {}
    copy_wheel_bindings(bindings, packages, inventory_bindings, preparation['wheelBindings'])
    duplicate = packages / 'PySide6/Qt'
    require(not duplicate.exists(), 'Wheel-provided Qt must not enter the fixture')
    (duplicate / 'lib').mkdir(parents=True)
    (contents / 'Frameworks').mkdir(exist_ok=True)
    inventory = {}
    for framework in sorted((qt / 'lib').glob('Qt*.framework')):
        binary = framework / 'Versions/A' / framework.stem
        require(binary.is_file(), 'Expected canonical SDK framework binary: ' + str(framework))
        require(arch in run('lipo', '-archs', str(binary)).split(), 'SDK framework has wrong architecture')
        identity = {'sha256': digest(binary), 'uuids': run('dwarfdump', '--uuid', str(binary)).splitlines()}
        copy_tree(framework, contents / 'Frameworks' / framework.name)
        copy_tree(framework, duplicate / 'lib' / framework.name)
        require(digest(contents / 'Frameworks' / framework.name / 'Versions/A' / framework.stem)
                == digest(duplicate / 'lib' / framework.name / 'Versions/A' / framework.stem) == identity['sha256'],
                'Two fixture Qt roots must contain the same SDK bytes')
        inventory[framework.stem] = identity
    require(REQUIRED_FRAMEWORKS.issubset(inventory), 'SDK lacks a required WebEngine framework')
    # Copy only app runtime resources/plugins. Never merge the wheel Qt subtree.
    (contents / 'PlugIns').mkdir()
    copy_tree(qt / 'plugins', contents / 'PlugIns/Qt')
    copy_tree(qt / 'plugins', duplicate / 'plugins')
    for name in ('resources', 'translations'):
        if (qt / name).is_dir():
            copy_tree(qt / name, duplicate / name)
    (contents / 'Resources').mkdir(exist_ok=True)
    (contents / 'Resources/qt.conf').write_text('[Paths]\nPlugins=PlugIns/Qt\nLibraries=Frameworks\n', encoding='utf-8')
    python = prepare_python(seed, python_home(), python_library, preparation)
    preparation['pythonRuntime'] = python
    preparation['pythonDependencyAnalysis'] = {}
    python['ownedExternalDependencies'] = own_python_dependencies(
        seed, python_home(), python_library, Path(python['ownedLibrary']), qt, preparation['pythonDependencyAnalysis'])
    from qt_runtime_profile import apply_macos_qt_runtime_profile
    apply_macos_qt_runtime_profile(seed)
    return {'python': python, 'sdkFrameworks': inventory, 'bindings': {
        'kind': 'official-PyPI-6.5.3-wheel-bindings-only', 'productBindingsVerified': False,
        'isolatedImportInventory': inventory_bindings,
        'qtCoreBindingSha256': digest(next((packages / 'PySide6').glob('QtCore*.so')))}}


def build_probe(repo, qt, work, output, arch):
    build = work / 'build'
    require(not build.exists(), 'Tiny native build must be fresh')
    for name, command in (
        ('configure', ['cmake', '-S', str(repo / 'ci/localized-release/macos-mixed-qt-readiness'), '-B', str(build),
            '-DCMAKE_BUILD_TYPE=Release', '-DCMAKE_PREFIX_PATH=' + str(qt),
            '-DCMAKE_OSX_ARCHITECTURES=' + arch, '-DCMAKE_OSX_DEPLOYMENT_TARGET=15.0',
            '-DPython3_EXECUTABLE=' + sys.executable]),
        ('build', ['cmake', '--build', str(build), '--parallel', '2'])):
        process = subprocess.run(command, cwd=repo, capture_output=True, text=True,
            encoding='utf-8', errors='replace', timeout=180)
        (output / (name + '.log')).write_text(process.stdout + '\n' + process.stderr, encoding='utf-8')
        require(process.returncode == 0, 'Tiny mixed Qt native ' + name + ' failed')
    return build / 'openrv-mixed-qt.app', Path((build / 'python-library.txt').read_text().strip()).resolve(strict=True)


def main():
    global _cloner
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', type=Path, default=Path.cwd())
    parser.add_argument('--qt', type=Path, required=True)
    parser.add_argument('--bindings', type=Path, required=True)
    parser.add_argument('--work', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--arch', choices=('arm64', 'x86_64'), required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    summary = {'check': 'mixed-qt-bundling-mechanism-fixture', 'status': 'failed',
               'phase': 'initializing', 'productRuntimeVerified': False,
               'architecture': args.arch, 'negativeSourceSnapshot': OLD_SNAPSHOT}
    try:
        require(sys.platform == 'darwin' and platform.machine() == args.arch
                and os.environ.get('GITHUB_ACTIONS') == 'true', 'Use a real native macOS Actions runner')
        require(sys.version_info[:2] == (3, 11), 'This fixture requires the pinned runner Python 3.11')
        repo, qt, bindings = (path.resolve(strict=True) for path in (args.repo, args.qt, args.bindings))
        work = args.work.resolve()
        require(work.is_relative_to(repo) and not work.exists(), 'Fixture copies must be fresh and app-owned under the repository')
        work.mkdir(parents=True)
        require(re.search(r'QT_VERSION\s*=\s*6\.5\.3\b', (qt / 'mkspecs/qconfig.pri').read_text()) is not None,
                'Expected the same Qt 6.5.3 SDK')
        summary['controllerSourceSnapshot'] = run('git', '-C', str(repo), 'rev-parse', 'HEAD')
        if os.environ.get('GITHUB_SHA'):
            require(summary['controllerSourceSnapshot'] == os.environ['GITHUB_SHA'], 'Controller checkout differs from the run snapshot')
        old_bytes = legacy_bundler_bytes(repo)
        old_path = work / 'old_bundle_macos.py'
        old_path.write_bytes(old_bytes)
        new_path = repo / 'ci/localized-release/bundle_macos.py'
        summary['bundlerSha256'] = {'negative': digest(old_path), 'positive': digest(new_path)}
        summary['phase'] = 'native-host-build'
        built, python_library = build_probe(repo, qt, work, output, args.arch)
        _cloner = FixtureCloner(work, (qt, python_home(), python_library.parent))
        summary['cloneProof'] = _cloner.records
        summary['phase'] = 'prepare-runtime-fixture'
        summary['fixturePreparation'] = {}
        summary['fixtureInputs'] = prepare_seed(built, work / 'seed.app', qt, bindings, args.arch,
                                              python_library, summary['fixturePreparation'])
        from sign_macos import sign_app, verify_app
        outcomes = {}
        summary['cases'] = outcomes
        for name, module_path in (('negative', old_path), ('positive', new_path)):
            summary['phase'] = name + '-bundling-and-native-gates'
            app = work / (name + '.app')
            copy_tree(work / 'seed.app', app)
            module = load_module(module_path, 'mixed_qt_bundle_' + name)
            outcomes[name] = {'app': str(app), 'bundlingStatus': 'failed'}
            outcomes[name]['bundlingRecords'] = module.bundle_dependencies(app, repo, qt, is_macho)
            sign_app(app, is_macho)
            verify_app(app)
            outcomes[name]['strictSignatureVerified'] = True
            outcomes[name]['bundlingStatus'] = 'passed'
            outcomes[name]['cppBaseline'] = launch(app, output, name + '-cpp', False)
            validate_positive(outcomes[name]['cppBaseline'], app, False)
            outcomes[name]['cppBaselineAccepted'] = True
            outcomes[name]['embedded'] = launch(app, output, name + '-embedded', True)
            if name == 'negative':
                validate_negative(outcomes[name]['embedded'], app)
                outcomes[name]['expectedFailureReproduced'] = True
            else:
                validate_positive(outcomes[name]['embedded'], app, True)
                outcomes[name]['embeddedContractAccepted'] = True
                summary['archiveProof'] = {}
                summary['phase'] = 'positive-zip-and-readonly-dmg-gates'
                archive_proof(app, work, output, summary['archiveProof'])
        for name, identity in summary['fixtureInputs']['sdkFrameworks'].items():
            require(digest(qt / 'lib' / (name + '.framework') / 'Versions/A' / name) == identity['sha256'],
                    'Original SDK framework bytes changed during owned fixture relocation/signing')
        require(digest(python_library) == summary['fixtureInputs']['python']['librarySha256'],
                'Original runner Python library changed during owned fixture work')
        summary['sdkAndPythonSourceBytesUnchanged'] = True
        summary['status'] = 'passed'
        summary['phase'] = 'completed'
    except Exception as error:
        # A CLI failure boundary, never a dependency skip or a success fallback.
        summary['errorType'] = type(error).__name__
        summary['error'] = str(error)
    encoded = json.dumps(summary, indent=2, sort_keys=True, allow_nan=False) + '\n'
    (output / 'summary.json').write_text(encoded, encoding='utf-8', newline='\n')
    print(console_proof(summary), flush=True)
    return 0 if summary['status'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
