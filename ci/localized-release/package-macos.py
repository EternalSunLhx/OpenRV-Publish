"""Package a native installed RV.app. Ad-hoc signing is not Apple notarization."""
from __future__ import annotations

import argparse
import os
import pathlib
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile

from release_common import copy_dependency_licenses, copy_licenses, run, sha256, source_metadata, write_checksums, write_json
from bundle_macos import bundle_dependencies
from sign_macos import sign_app, verify_app
from qt_runtime_profile import apply_macos_qt_runtime_profile

MACHO_MAGICS = {bytes.fromhex(value) for value in ('feedface', 'cefaedfe', 'feedfacf', 'cffaedfe',
                                                  'cafebabe', 'bebafeca', 'cafebabf', 'bfbafeca')}


def update_bundle_metadata(app: pathlib.Path, metadata: dict, minimum_macos: str) -> None:
    """Write release/native identities before the final ad-hoc signature."""
    plist_path = app / 'Contents' / 'Info.plist'
    with plist_path.open('rb') as stream:
        info = plistlib.load(stream)
    info['CFBundleShortVersionString'] = metadata['version']
    info['CFBundleVersion'] = metadata['version']
    info['OpenRVReleaseVersion'] = metadata['releaseVersion']
    info['OpenRVNativeVersion'] = metadata['nativeVersion']
    if minimum_macos:
        existing = str(info.get('LSMinimumSystemVersion', '0.0'))
        if tuple(map(int, minimum_macos.split('.'))) > tuple(map(int, existing.split('.'))):
            info['LSMinimumSystemVersion'] = minimum_macos
    with plist_path.open('wb') as stream:
        plistlib.dump(info, stream, sort_keys=False)


def is_macho(path: pathlib.Path) -> bool:
    if path.is_symlink() or not path.is_file():
        return False
    with path.open('rb') as stream:
        header = stream.read(8)
        if header[:4] == bytes.fromhex('cafebabe') and not 1 <= int.from_bytes(header[4:8], 'big') <= 16:
            return False  # Java .class files share the 32-bit universal Mach-O magic.
        return header[:4] in MACHO_MAGICS


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--arch', required=True, choices=('x86_64', 'arm64'))
    parser.add_argument('--repo', type=pathlib.Path, default=pathlib.Path.cwd())
    parser.add_argument('--app', type=pathlib.Path)
    args = parser.parse_args()
    if sys.platform != 'darwin':
        parser.error('macOS packaging requires native Apple command-line tools')
    repo = args.repo.resolve()
    app = (args.app or repo / '_install_release' / 'RV.app').resolve()
    output = repo / 'dist' / 'localized-release'
    output.mkdir(parents=True, exist_ok=True)
    work = repo / '_release_work'
    work.mkdir(exist_ok=True)
    if not (app / 'Contents' / 'Info.plist').is_file():
        raise FileNotFoundError(f'Installed app bundle is incomplete: {app}')
    metadata = source_metadata(repo)
    base = f"OpenRV-{metadata['releaseVersion']}-macOS-{args.arch}"
    resources = app / 'Contents' / 'Resources'
    # RvLanguage loads Qt's standard button/dialog translations relative to MacOS.
    # Upstream's macOS Qt framework copy does not include the SDK translations dir.
    qt_catalog = pathlib.Path(os.environ['QT_ROOT_DIR']) / 'translations' / 'qtbase_zh_CN.qm'
    if not qt_catalog.is_file():
        raise FileNotFoundError(f'Missing Qt Chinese base catalog: {qt_catalog}')
    qt_translations = app / 'Contents' / 'translations'
    qt_translations.mkdir(exist_ok=True)
    shutil.copy2(qt_catalog, qt_translations / qt_catalog.name)
    copy_licenses(repo, resources / 'licenses' / 'OpenRV')
    copy_dependency_licenses(repo / '_build_release', pathlib.Path(os.environ['QT_ROOT_DIR']), resources / 'licenses' / 'dependencies')
    metadata['excludedQtPlugins'] = apply_macos_qt_runtime_profile(app)
    external_libraries = bundle_dependencies(app, repo, pathlib.Path(os.environ['QT_ROOT_DIR']), is_macho)
    binaries = [path for path in app.rglob('*') if is_macho(path)]
    if not binaries:
        raise RuntimeError('No Mach-O binaries in the installed app')
    minimum_versions = []
    # Fail packaging if a compiled component depends on the build machine's Homebrew/Qt tree.
    for binary in binaries:
        architectures = run('lipo', '-archs', str(binary)).split()
        if args.arch not in architectures:
            raise RuntimeError(f'Wrong architecture {architectures}: {binary}')
        load_commands = run('otool', '-arch', args.arch, '-l', str(binary))
        minimum_versions.extend(re.findall(r'\bminos\s+(\d+(?:\.\d+){1,2})', load_commands))
        minimum_versions.extend(re.findall(r'cmd LC_VERSION_MIN_MACOSX\s+cmdsize \d+\s+version (\d+(?:\.\d+){1,2})', load_commands))
        for line in run('otool', '-arch', args.arch, '-L', str(binary)).splitlines()[1:]:
            dependency = line.strip().split(' (', 1)[0]
            if dependency.startswith('/') and not dependency.startswith(('/System/', '/usr/lib/')):
                raise RuntimeError(f'Non-relocatable dependency {dependency} in {binary}')
    minimum_macos = max(minimum_versions, key=lambda value: tuple(map(int,value.split('.')))) if minimum_versions else ''
    update_bundle_metadata(app, metadata, minimum_macos)
    metadata['relocatedNonMachO'] = sign_app(app, is_macho)
    smoke = work / ('smoke-' + args.arch)
    smoke_args = [sys.executable, str(repo / 'ci/localized-release/runtime_smoke.py'), '--runtime', str(app), '--work', str(smoke)]
    if os.environ.get('ORV_REQUIRE_GUI_SMOKE') == 'true':
        smoke_args.append('--require-gui')
    subprocess.run(smoke_args, check=True)
    import json
    smoke_report = json.loads((smoke / 'report.json').read_text(encoding='utf-8'))
    persistence_proof = {'status': 'skipped', 'reason': 'Artifacts-only diagnostic did not require visible GUI tests.'}
    if os.environ.get('ORV_REQUIRE_GUI_SMOKE') == 'true':
        persistence_dir = work / ('persistence-' + args.arch)
        subprocess.run([sys.executable, str(repo / 'tests/localization/macos_language_persistence.py'),
                        '--runtime', str(app), '--work', str(persistence_dir)], check=True)
        persistence = json.loads((persistence_dir / 'report.json').read_text(encoding='utf-8'))
        if (persistence.get('status') != 'passed' or len(persistence.get('launches', [])) != 3
                or any(persistence.get('preferenceRecovery', {}).get(operation, {}).get('status') != 'passed'
                       for operation in ('restore', 'verify'))):
            raise RuntimeError('Expected three successful Mac chooser launches and verified preference restoration')
        persistence_proof = {'status': persistence['status'], 'preferenceScope': persistence['preferenceScope'],
                             'preferenceRecovery': {operation: persistence['preferenceRecovery'][operation]['status']
                                                    for operation in ('restore', 'verify')},
                             'launches': [{key: launch['details'][key] for key in (
                                 'stage', 'status', 'actualLanguage', 'nativeMenuRebuilds',
                                 'hostedRunnerPreferencesVerifiedBeforeAction', 'savedPreference')}
                                 for launch in persistence['launches']]}
    # Detect app writes by runtime tests; do not hide them by re-signing.
    verify_app(app)
    # ditto preserves symlinks, executable bits and bundle metadata in the archive.
    zip_name, dmg_name = base + '.zip', base + '.dmg'
    if (output / zip_name).exists() or (output / dmg_name).exists():
        raise FileExistsError('Refusing to overwrite prior release files')
    subprocess.run(['ditto', '-c', '-k', '--sequesterRsrc', '--keepParent', str(app), str(output / zip_name)], check=True)
    with tempfile.TemporaryDirectory(prefix='dmg-', dir=work) as staging:
        staging_path = pathlib.Path(staging)
        subprocess.run(['ditto', str(app), str(staging_path / 'RV.app')], check=True)
        (staging_path / 'Applications').symlink_to('/Applications', target_is_directory=True)
        subprocess.run(['hdiutil', 'create', '-volname', 'OpenRV', '-srcfolder', staging,
                        '-ov', '-format', 'UDZO', str(output / dmg_name)], check=True)
    subprocess.run(['hdiutil', 'verify', str(output / dmg_name)], check=True)
    executable_hashes = {str(path.relative_to(app)): sha256(path) for path in binaries
                         if path.parent == app / 'Contents' / 'MacOS'}
    metadata.update(platform='macOS', architecture=args.arch, compiler=run('clang', '--version'),
                    xcode=run('xcodebuild', '-version'), sdk=run('xcrun', '--show-sdk-version'),
                    minimumMacOS=minimum_macos,
                    signature='ad-hoc', codeSigned=False, notarized=False, machoCount=len(binaries),
                    binarySha256=executable_hashes, files=[zip_name, dmg_name],
                    bundledExternalLibraries=external_libraries, dependencyResolver='delocate 0.13.0',
                    guiPassed=smoke_report.get('guiPassed', False),
                    languagePersistence=persistence_proof,
                    smoke=[{key: step[key] for key in ('name', 'status', 'exitCode', 'timedOut')}
                           for step in smoke_report['steps']])
    manifest_name = base + '.build.json'
    write_json(output / manifest_name, metadata)
    write_checksums(output, [zip_name, dmg_name, manifest_name], base + '.sha256.txt')


if __name__ == '__main__':
    main()
