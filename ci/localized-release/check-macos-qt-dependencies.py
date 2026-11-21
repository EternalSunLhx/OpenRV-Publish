"""Read-only Qt SDK dependency gate before cold native CI compilation."""
from __future__ import annotations

import argparse
from importlib.metadata import version
import json
import os
from pathlib import Path
import platform
import subprocess
import sys

from qt_runtime_profile import MACOS_EXCLUDED_QT_PLUGINS, excluded_macos_qt_plugin

MACHO_MAGICS = {bytes.fromhex(value) for value in (
    'feedface', 'cefaedfe', 'feedfacf', 'cffaedfe',
    'cafebabe', 'bebafeca', 'cafebabf', 'bfbafeca')}


def is_macho(path: Path) -> bool:
    # Match the final packager's magic/Java check; static archives are not runtime images.
    if path.is_symlink() or not path.is_file() or path.suffix == '.a':
        return False
    with path.open('rb') as stream:
        header = stream.read(8)
    if header[:4] == bytes.fromhex('cafebabe') and not 1 <= int.from_bytes(header[4:8], 'big') <= 16:
        return False
    return header[:4] in MACHO_MAGICS


def deployed_tool_candidates(qt: Path) -> list[Path]:
    # PySide 6.5.3's build_scripts tool lists and pyside-tools/CMakeLists.txt copy
    # precisely these SDK tools. Do not sweep unrelated SDK development utilities.
    qt = qt.resolve(strict=True)
    tools = set()

    def owned(path: Path) -> Path:
        resolved = path.resolve(strict=True)
        if not resolved.is_relative_to(qt):
            raise RuntimeError(f'Deployed Qt tool escapes the SDK: {path}')
        return resolved

    for directory, names in (
            ('bin', ('lupdate', 'lrelease', 'qmllint', 'qmlformat', 'qmlls')),
            ('libexec', ('uic', 'rcc', 'qmltyperegistrar', 'qmlimportscanner',
                         'qmlcachegen', 'QtWebEngineProcess'))):
        for name in names:
            path = qt / directory / name
            if not path.exists() and not path.is_symlink():
                continue  # Upstream copies optional tools only when present.
            resolved = owned(path)
            if not resolved.is_file():
                raise RuntimeError(f'Expected a regular deployed Qt tool: {path}')
            tools.add(resolved)
    # These complete .apps, including their native helpers, are copied by PySide.
    for name in ('Assistant.app', 'Designer.app', 'Linguist.app'):
        path = qt / 'bin' / name
        if not path.exists() and not path.is_symlink():
            continue
        app = owned(path)
        if not app.is_dir():
            raise RuntimeError(f'Expected a deployed Qt tool app: {path}')
        pending, seen = [(app, frozenset())], set()
        while pending:
            directory, ancestors = pending.pop()
            if directory in ancestors:
                raise RuntimeError(f'Directory cycle in deployed Qt tool app: {directory}')
            if directory in seen:
                continue
            seen.add(directory)
            for child in directory.iterdir():
                resolved = owned(child)
                if resolved.is_dir():
                    pending.append((resolved, ancestors | {directory}))
                elif resolved.is_file():
                    tools.add(resolved)
    return sorted(tools)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--qt', type=Path, required=True)
    parser.add_argument('--arch', choices=('x86_64', 'arm64'), required=True)
    args = parser.parse_args()
    if (sys.platform != 'darwin' or platform.machine() != args.arch
            or os.environ.get('GITHUB_ACTIONS') != 'true'
            or os.environ.get('RUNNER_ENVIRONMENT') != 'github-hosted'
            or os.environ.get('RUNNER_OS') != 'macOS'):
        parser.error('Use a native disposable GitHub-hosted macOS runner')
    if version('delocate') != '0.13.0':
        raise RuntimeError('The Qt SDK gate requires pinned Delocate 0.13.0')
    from delocate.libsana import get_dependencies

    qt = args.qt.resolve(strict=True)
    roots = [qt / name for name in ('lib', 'plugins', 'qml')]
    if any(not root.is_dir() for root in roots):
        raise RuntimeError('Expected Qt SDK lib, plugins and qml directories')
    tool_seeds = [path for path in deployed_tool_candidates(qt) if is_macho(path)]
    pending = sorted({path.resolve() for root in roots for path in root.rglob('*')
                      if is_macho(path) and not excluded_macos_qt_plugin(path, qt / 'plugins')}
                     | set(tool_seeds))
    if not pending:
        raise RuntimeError('No native Qt SDK libraries or plugins were found')
    seen, errors, edges = set(), [], 0
    keys = ('DYLD_LIBRARY_PATH', 'DYLD_FALLBACK_LIBRARY_PATH')
    previous = {key: os.environ.get(key) for key in keys}
    try:
        os.environ.pop('DYLD_LIBRARY_PATH', None)
        os.environ['DYLD_FALLBACK_LIBRARY_PATH'] = str(qt / 'lib')
        while pending and len(errors) < 20:
            consumer = pending.pop()
            if consumer in seen:
                continue
            seen.add(consumer)
            architectures = subprocess.check_output(
                ['lipo', '-archs', str(consumer)], text=True, timeout=30).split()
            if args.arch not in architectures:
                errors.append(f'Wrong architecture {architectures}: {consumer.relative_to(qt)}')
                continue
            # SDK helper executables use their real directory, never Python's or a fake app.
            for provider, install_name in get_dependencies(str(consumer), executable_path=str(consumer.parent)):
                edges += 1
                if provider is None:
                    errors.append(f'Unresolved {install_name} in {consumer.relative_to(qt)}')
                elif not provider.startswith(('/System/', '/usr/lib/')):
                    dependency = Path(provider).resolve()
                    if not dependency.is_relative_to(qt):
                        errors.append(f'Dependency outside Qt SDK/system: {provider} in {consumer.relative_to(qt)}')
                    elif excluded_macos_qt_plugin(dependency, qt / 'plugins'):
                        errors.append(f'Excluded SQL driver required by {consumer.relative_to(qt)}: {install_name}')
                    elif not is_macho(dependency):
                        errors.append(f'Missing/non-Mach-O provider {provider} in {consumer.relative_to(qt)}')
                    elif dependency not in seen:
                        pending.append(dependency)
                if len(errors) >= 20:
                    break
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    if errors:
        raise RuntimeError('Qt SDK dependency closure failed (first 20 errors):\n' + '\n'.join(errors))
    print(json.dumps({'status': 'passed', 'check': 'qt-sdk-dependency-closure',
                      'qtRoot': str(qt), 'architecture': args.arch, 'delocate': '0.13.0',
                      'machOFiles': len(seen), 'dependencyEdges': edges,
                      'toolSeedCount': len(tool_seeds),
                      'toolSeeds': [path.relative_to(qt).as_posix() for path in tool_seeds],
                      'excludedQtPlugins': list(MACOS_EXCLUDED_QT_PLUGINS),
                      'sdkModified': False, 'appRuntimeVerified': False}, sort_keys=True))


if __name__ == '__main__':
    main()
