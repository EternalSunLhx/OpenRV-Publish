"""Prepare app-owned resource aliases and sign nested macOS code inside-out."""
from __future__ import annotations

import os
from pathlib import Path
import plistlib
import subprocess

BUNDLE_SUFFIXES = {'.app', '.framework', '.xpc', '.bundle'}
# TN2206 Table 3 plus the two aliases in Apple's V2 nested-code rules.
CODE_ROOTS = ('MacOS', 'Frameworks', 'SharedFrameworks', 'PlugIns', 'Plug-ins',
              'XPCServices', 'Helpers', 'Library/Automator', 'Library/Spotlight',
              'Library/LoginItems', 'Library/LaunchServices')
CONTENTS_METADATA = {'Info.plist', 'PkgInfo', 'version.plist', 'embedded.provisionprofile'}


def _is_bundle(path: Path) -> bool:
    return path.is_dir() and (path.suffix in BUNDLE_SUFFIXES or
                              (path / 'Contents/Info.plist').is_file())


def _owned(app: Path, path: Path, *, strict=True) -> Path:
    resolved = path.resolve(strict=strict)
    if not resolved.is_relative_to(app):
        raise RuntimeError(f'Bundle path escapes the app: {path}')
    return resolved


def _containers(app: Path) -> list[Path]:
    return [app, *sorted(path for path in app.rglob('*')
                         if not path.is_symlink() and _is_bundle(path))]


def _validate_links(app: Path) -> None:
    for path in app.rglob('*'):
        if path.is_symlink():
            _owned(app, path)


def _safe_destination(app: Path, path: Path) -> None:
    if not path.is_relative_to(app) or '..' in path.relative_to(app).parts:
        raise RuntimeError(f'Resource destination escapes the app: {path}')
    _owned(app, path, strict=False)
    current = path
    while current != app:
        if current.is_symlink():
            raise RuntimeError(f'Redirected resource destination: {current}')
        if current.exists() and current != path and not current.is_dir():
            raise RuntimeError(f'Non-directory resource parent: {current}')
        current = current.parent
    if path.exists():
        raise RuntimeError(f'Resource destination collision: {path}')


def relocate_non_macho(app: Path, is_macho, protected=()) -> list[dict]:
    """Follow TN2206: resources outside code roots, with relative legacy-path aliases."""
    app = app.resolve(strict=True)
    _validate_links(app)
    protected = set(protected)
    moves = []

    def pure_data(directory):
        for child in directory.rglob('*'):
            if child.is_symlink() or _is_bundle(child) or is_macho(child):
                return False
        return True

    def plan(source, contents, resources, owner):
        destination = resources / source.relative_to(contents)
        _safe_destination(owner, destination)
        moves.append((source, destination, owner))

    def collect(directory, contents, resources, owner):
        for source in sorted(directory.iterdir()):
            if source.is_symlink() or source.resolve() in protected:
                continue
            _owned(owner, source)
            if source.is_dir():
                if _is_bundle(source):
                    continue  # A nested bundle handles its own code roots.
                if not pure_data(source):
                    collect(source, contents, resources, owner)
                    continue
            elif not source.is_file():
                raise RuntimeError(f'Unsupported non-code bundle entry: {source}')
            elif is_macho(source):
                continue
            plan(source, contents, resources, owner)

    for container in _containers(app):
        contents = container / 'Contents'
        if not contents.is_dir():
            continue
        _owned(container, contents)
        resources = contents / 'Resources/scripts'
        for relative in CODE_ROOTS:
            code_root = contents / relative
            if code_root.is_dir():
                _owned(container, code_root)
                if not code_root.is_symlink():
                    collect(code_root, contents, resources, container)
        # Apple's V2 rules also treat direct Contents files as nested code,
        # except these bundle metadata files. Do not recurse into other dirs.
        for source in sorted(contents.iterdir()):
            if (source.name not in CONTENTS_METADATA and source.is_file() and
                    not source.is_symlink() and source.resolve() not in protected and not is_macho(source)):
                _owned(container, source)
                plan(source, contents, resources, container)
    destinations = [destination for _, destination, _ in moves]
    if len(set(destinations)) != len(destinations):
        raise RuntimeError('Colliding resource relocation targets')
    records = []
    for source, destination, owner in moves:
        destination.parent.mkdir(parents=True, exist_ok=True)
        kind = 'directory' if source.is_dir() else 'file'
        source.rename(destination)  # Same app/volume: preserve bytes and permissions.
        source.symlink_to(os.path.relpath(destination, source.parent), target_is_directory=kind == 'directory')
        _owned(owner, source)
        records.append({'path': source.relative_to(app).as_posix(),
                        'resource': destination.relative_to(app).as_posix(), 'kind': kind})
    return records


def _signing_units(app: Path, is_macho) -> tuple[set[Path], set[Path]]:
    units, mains = set(), set()
    for container in _containers(app):
        versions = container / 'Versions'
        if container.suffix == '.framework' and versions.is_dir():
            entries = [(version, version / 'Resources/Info.plist', version)
                       for version in versions.iterdir() if version.is_dir() and not version.is_symlink()]
        elif container.suffix == '.framework':
            entries = [(container, container / 'Resources/Info.plist', container)]
        else:
            entries = [(container, container / 'Contents/Info.plist', container / 'Contents/MacOS')]
            if not entries[0][1].is_file() and (container / 'Info.plist').is_file():
                entries = [(container, container / 'Info.plist', container)]
        for unit, info_path, executable_dir in entries:
            _owned(container, unit)
            if not info_path.is_file():
                raise RuntimeError(f'Missing bundle Info.plist: {info_path}')
            _owned(container, info_path)
            with info_path.open('rb') as stream:
                info = plistlib.load(stream)
            executable = info.get('CFBundleExecutable')
            if not executable:
                if container.suffix == '.bundle' and not any(is_macho(path) for path in container.rglob('*')):
                    continue  # Resource-only bundles are sealed as data by their parent.
                raise RuntimeError(f'Missing bundle CFBundleExecutable: {info_path}')
            if not isinstance(executable, str) or Path(executable).name != executable or executable in ('.', '..'):
                raise RuntimeError(f'Invalid CFBundleExecutable: {info_path}')
            main = _owned(container, executable_dir / executable)
            if not main.is_file():
                raise RuntimeError(f'Missing bundle main executable: {main}')
            mains.add(main)
            units.add(unit)
    return units, mains


def verify_app(app: Path) -> None:
    subprocess.run(['codesign', '--verify', '--deep', '--strict', '--verbose=2', str(app)], check=True)


def sign_app(app: Path, is_macho) -> list[dict]:
    app = app.resolve(strict=True)
    _validate_links(app)
    units, mains = _signing_units(app, is_macho)
    records = relocate_non_macho(app, is_macho, mains)
    binaries = {_owned(app, path) for path in app.rglob('*') if is_macho(path)}
    # Signing a CFBundleExecutable can implicitly sign its bundle. Leave every
    # bundle main for its enclosing signing unit, after all of that unit's children.
    targets = (binaries - mains) | units
    for target in sorted(targets, key=lambda path: (len(path.parts), str(path)), reverse=True):
        subprocess.run(['codesign', '--force', '--sign', '-', '--timestamp=none', str(target)], check=True)
    verify_app(app)
    return records
