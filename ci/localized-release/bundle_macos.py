"""Collect native non-system dependency closure without changing original libraries."""
from __future__ import annotations

from contextlib import contextmanager
import os
import pathlib
import plistlib
import re
import shutil
import stat
import subprocess

from release_common import run, sha256


def _resource_manifest(path: pathlib.Path, root: pathlib.Path, *, reject_links=False) -> dict:
    root = root.resolve(strict=True)
    entries = {}

    def visit(current, relative, ancestors):
        info = current.lstat()
        redirected = stat.S_ISLNK(info.st_mode) or bool(
            getattr(info, 'st_file_attributes', 0) & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0))
        if reject_links and redirected:
            raise RuntimeError(f'Redirected bundle resource: {current}')
        resolved = current.resolve(strict=True)
        if not resolved.is_relative_to(root):
            raise RuntimeError(f'Resource escapes its allowed root: {current}')
        if resolved.is_file():
            entries[relative] = ('file', sha256(resolved))
        elif resolved.is_dir():
            if resolved in ancestors:
                raise RuntimeError(f'Directory cycle in resource: {current}')
            entries[relative] = ('directory', None)
            for child in sorted(resolved.iterdir()):
                visit(child, relative + '/' + child.name, ancestors | {resolved})
        else:
            raise RuntimeError(f'Non-regular bundle resource: {current}')

    visit(path, '.', frozenset())
    return entries


def _copy_bundled_resource(source: pathlib.Path, destination: pathlib.Path,
                           app: pathlib.Path, source_root: pathlib.Path) -> None:
    # Resources are immutable snapshots: repeat copies must match complete bytes/types.
    # Reusing an identical tree avoids overwriting Homebrew's read-only copied files.
    app = app.resolve(strict=True)
    destination = destination.absolute()
    if not destination.is_relative_to(app) or '..' in destination.relative_to(app).parts:
        raise RuntimeError(f'Resource destination escapes the app: {destination}')
    current = app
    for part in destination.relative_to(app).parts:
        current /= part
        try:
            info = current.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or (getattr(info, 'st_file_attributes', 0)
                & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0)):
            raise RuntimeError(f'Redirected bundle resource destination: {current}')
        if not current.resolve().is_relative_to(app):
            raise RuntimeError(f'Resource destination escapes the app: {current}')
        if current != destination and not stat.S_ISDIR(info.st_mode):
            raise RuntimeError(f'Resource collision (non-directory parent): {current}')
    expected = _resource_manifest(source, source_root)
    if destination.exists():
        if _resource_manifest(destination, app, reject_links=True) != expected:
            raise RuntimeError(f'Resource collision (different or partial content): {destination}')
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source.is_dir():
        shutil.copytree(source, destination)
    else:
        shutil.copy2(source, destination)
    if _resource_manifest(destination, app, reject_links=True) != expected:
        raise RuntimeError(f'Copied resource differs from its source: {destination}')


def _relocated_install_id(provider: pathlib.Path, ids: list[str], contents: pathlib.Path,
                          library_ids: dict[pathlib.Path, list[str]]) -> str | None:
    """Choose an ID without load-command growth; the caller supplies real app IDs."""
    if not any(identity.startswith('/') and not identity.startswith(('/System/', '/usr/lib/')) for identity in ids):
        return None
    limit = min(len(identity.encode('utf-8')) for identity in ids)
    relative_ids = {identity for identity in ids if not identity.startswith('/')}
    if relative_ids:
        if len(relative_ids) != 1:
            raise RuntimeError(f'Ambiguous mixed-architecture library IDs: {provider}')
        identity = next(iter(relative_ids))
        if len(identity.encode('utf-8')) > limit:
            raise RuntimeError(f'Mixed-architecture library ID would grow: {provider}')
    else:
        # Keep the unique app-relative identity when it fits. A shorter
        # basename is safe only when it does not collide with another ID.
        candidates = ('@rpath/' + provider.relative_to(contents).as_posix(), '@rpath/' + provider.name)
        identity = next((candidate for candidate in candidates
                         if len(candidate.encode('utf-8')) <= limit
                         and not any(candidate in other_ids for other, other_ids in library_ids.items()
                                     if other != provider)), None)
        if identity is None:
            raise RuntimeError(f'No non-growing, unique library ID for {provider}: '
                               f'{[value[:256] for value in ids[:4]]}')
    return identity


def _qt_framework_snapshot(framework: pathlib.Path, app: pathlib.Path, is_macho) -> dict:
    """Describe real framework payloads, allowing only app-internal aliases."""
    app = app.resolve(strict=True)
    snapshot = {}
    image_ids = {}
    hashes = {}

    def visit(path, relative, ancestors):
        if '_CodeSignature' in relative.parts:
            return  # Every retained code container is signed again after relocation.
        resolved = path.resolve(strict=True)
        if not resolved.is_relative_to(app):
            raise RuntimeError(f'Qt framework payload escapes the app: {path}')
        if resolved.is_dir():
            if resolved in ancestors:
                raise RuntimeError(f'Qt framework directory cycle: {path}')
            snapshot[relative.as_posix()] = ('directory',)
            for child in sorted(resolved.iterdir()):
                visit(child, relative / child.name, ancestors | {resolved})
        elif resolved.is_file():
            if is_macho(resolved):
                if resolved not in image_ids:
                    arches = set(run('lipo', '-archs', str(resolved)).split())
                    ids = re.findall(r'^UUID: ([0-9A-Fa-f-]+) \(([^)]+)\)',
                                     run('dwarfdump', '--uuid', str(resolved)), re.MULTILINE)
                    if not arches or len(ids) != len(arches) or {arch for _, arch in ids} != arches:
                        raise RuntimeError(f'Incomplete per-architecture Qt UUIDs: {resolved}')
                    image_ids[resolved] = tuple(sorted((arch, uuid.upper()) for uuid, arch in ids))
                snapshot[relative.as_posix()] = ('image', image_ids[resolved])
            else:
                if resolved not in hashes:
                    hashes[resolved] = sha256(resolved)
                snapshot[relative.as_posix()] = ('resource', hashes[resolved])
        else:
            raise RuntimeError(f'Non-regular Qt framework payload: {path}')

    visit(framework, pathlib.Path('.'), frozenset())
    return snapshot


def _qt_framework_info(framework: pathlib.Path, app: pathlib.Path) -> dict:
    info = framework / 'Resources' / 'Info.plist'
    if not info.resolve(strict=True).is_relative_to(app):
        raise RuntimeError(f'Qt framework metadata escapes the app: {info}')
    with info.open('rb') as stream:
        values = plistlib.load(stream)
    keys = ('CFBundleIdentifier', 'CFBundleExecutable', 'CFBundleVersion', 'CFBundleShortVersionString')
    if any(not isinstance(values.get(key), str) or not values[key] for key in keys):
        raise RuntimeError(f'Incomplete Qt framework identity: {info}')
    return {key: values[key] for key in keys}


def _coalesce_qt_frameworks(app: pathlib.Path, is_macho) -> list[dict]:
    """Keep PySide's lookup paths while loading one verified copy of each Qt image."""
    app = app.resolve(strict=True)
    canonical_root = app / 'Contents' / 'Frameworks'
    standalone_root = app / 'Contents/lib/python3.11/site-packages/PySide6/Qt/lib'

    def redirected(path):
        try:
            info = path.lstat()
        except FileNotFoundError:
            return False
        return stat.S_ISLNK(info.st_mode) or bool(getattr(info, 'st_file_attributes', 0)
                & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0))

    for root in (canonical_root, standalone_root):
        current = app
        for part in root.relative_to(app).parts:
            current /= part
            if redirected(current) or (current.exists() and not current.resolve().is_relative_to(app)):
                raise RuntimeError(f'Redirected Qt framework root: {current}')
    if not standalone_root.exists():
        return []
    if not standalone_root.is_dir() or not canonical_root.is_dir():
        raise RuntimeError('Expected app-owned canonical and PySide Qt framework directories')
    plans, records = [], []
    # Validate every pair before replacing any duplicate. A matching version alone
    # does not establish binary identity or preserve WebEngine's helper/resources.
    for duplicate in sorted(standalone_root.glob('Qt*.framework')):
        canonical = canonical_root / duplicate.name
        if redirected(canonical) or not canonical.is_dir():
            raise RuntimeError(f'Missing or redirected canonical Qt framework: {canonical}')
        if duplicate.is_symlink():
            if (pathlib.Path(os.readlink(duplicate)).is_absolute()
                    or duplicate.resolve(strict=True) != canonical.resolve(strict=True)):
                raise RuntimeError(f'Unexpected PySide Qt framework alias: {duplicate}')
            continue
        if redirected(duplicate):
            raise RuntimeError(f'Redirected PySide Qt framework: {duplicate}')
        if not duplicate.is_dir():
            raise RuntimeError(f'Non-directory PySide Qt framework: {duplicate}')
        identity = _qt_framework_info(duplicate, app)
        if identity != _qt_framework_info(canonical, app):
            raise RuntimeError(f'Qt framework version/identity collision: {duplicate.name}')
        original = _qt_framework_snapshot(duplicate, app, is_macho)
        retained = _qt_framework_snapshot(canonical, app, is_macho)
        for relative, value in original.items():
            if retained.get(relative) != value:
                raise RuntimeError(f'Qt framework payload collision: {duplicate.name}/{relative}')
        image_count = sum(value[0] == 'image' for value in original.values())
        if not image_count:
            raise RuntimeError(f'Qt framework contains no verified native image: {duplicate}')
        plans.append((duplicate, canonical))
        records.append({'kind': 'qt-framework-alias', 'source': duplicate.relative_to(app).as_posix(),
                        'destination': canonical.relative_to(app).as_posix(),
                        'identity': identity, 'verifiedImagePaths': image_count,
                        'verifiedImages': {relative: dict(value[1]) for relative, value in original.items()
                                           if value[0] == 'image'},
                        'verifiedResourcePaths': sum(value[0] == 'resource' for value in original.values())})
    for duplicate, canonical in plans:
        # Both roots and every payload were checked above; SDK/build libraries
        # are untouched. Relative aliases also cover explicit PySide dlopen paths.
        shutil.rmtree(duplicate)
        duplicate.symlink_to(os.path.relpath(canonical, duplicate.parent), target_is_directory=True)
        if duplicate.resolve(strict=True) != canonical.resolve(strict=True):
            raise RuntimeError(f'Qt framework alias did not retain its canonical target: {duplicate}')
    return records


def _analysis_library_paths(app: pathlib.Path, qt: pathlib.Path) -> tuple[pathlib.Path, ...]:
    app = app.resolve()
    contents = app / 'Contents'
    packages = contents / 'lib' / 'python3.11' / 'site-packages'
    # Standalone PySide installs these sibling libraries and its bundled Qt frameworks.
    # These exact app-owned directories supplement, rather than replace, Mach-O runpaths.
    candidates = (contents / 'lib', contents / 'Frameworks', packages / 'shiboken6',
                  packages / 'PySide6', packages / 'PySide6' / 'Qt' / 'lib')
    paths = []
    for candidate in candidates:
        resolved = candidate.resolve()
        if not resolved.is_relative_to(app):
            raise RuntimeError(f'Analysis library directory escapes the app: {candidate}')
        if resolved.is_dir() and resolved not in paths:
            paths.append(resolved)
    return (*paths, (qt / 'lib').resolve())


@contextmanager
def _analysis_environment(app: pathlib.Path, qt: pathlib.Path):
    paths = _analysis_library_paths(app, qt)
    keys = ('DYLD_LIBRARY_PATH', 'DYLD_FALLBACK_LIBRARY_PATH')
    old = {key: os.environ.get(key) for key in keys}
    os.environ.pop('DYLD_LIBRARY_PATH', None)
    os.environ['DYLD_FALLBACK_LIBRARY_PATH'] = os.pathsep.join(map(str, paths))
    try:
        yield
    finally:
        for key, value in old.items():
            if value is None: os.environ.pop(key, None)
            else: os.environ[key] = value


def bundle_dependencies(app: pathlib.Path, repo: pathlib.Path, qt: pathlib.Path, is_macho) -> list[dict]:
    # Delocate supplies its tested per-architecture @rpath/@loader_path resolver.
    # Our copy layer retains complete frameworks (including their resources).
    from delocate.libsana import get_dependencies, walk_directory
    app, repo, qt = (path.resolve() for path in (app, repo, qt))
    brew = pathlib.Path(run('brew', '--prefix')).resolve()
    allowed_roots = (repo, qt, brew)
    contents = app / 'Contents'
    private_libs = contents / 'lib' / 'thirdparty'
    private_libs.mkdir(parents=True, exist_ok=True)
    frameworks = contents / 'Frameworks'
    frameworks.mkdir(exist_ok=True)
    records = _coalesce_qt_frameworks(app, is_macho)
    recorded, frameworks_copied = set(), {}
    source_destinations, destination_hashes = {}, {}

    def system(path: str) -> bool:
        return path.startswith(('/System/', '/usr/lib/'))

    def validate_source(path: pathlib.Path) -> None:
        if not any(path.is_relative_to(root) for root in allowed_roots):
            raise RuntimeError(f'External dependency is outside the known build/Qt/Homebrew roots: {path}')

    def framework_root(path: pathlib.Path):
        return next((parent for parent in path.parents if parent.suffix == '.framework'), None)

    def uuids(path: pathlib.Path):
        return {tuple(line.split()[1:3]) for line in run('dwarfdump', '--uuid', str(path)).splitlines() if line.startswith('UUID:')}

    def licenses_for(path: pathlib.Path) -> None:
        # Homebrew keeps each installed formula's actual license/doc files in its keg.
        keg = next((parent for parent in path.parents if (parent / 'INSTALL_RECEIPT.json').is_file()), None)
        if keg is None:
            return  # Qt/source dependencies are handled by copy_dependency_licenses.
        destination = contents / 'Resources' / 'licenses' / 'Homebrew' / keg.parent.name
        for source in keg.iterdir():
            if source.is_file() and re.match(r'(?i)^(licen[cs]es?|copying|notice|copyright)(?:[._-]|$)', source.name):
                _copy_bundled_resource(source, destination / source.name, app, keg)
        docs = keg / 'share' / 'doc'
        if docs.is_dir():
            _copy_bundled_resource(docs, destination / 'doc', app, keg)
        receipt = keg / 'INSTALL_RECEIPT.json'
        _copy_bundled_resource(receipt, destination / 'INSTALL_RECEIPT.json', app, keg)

    def local_copy(source: pathlib.Path) -> pathlib.Path:
        if source.is_relative_to(app):
            return source
        if source in source_destinations:
            return source_destinations[source]
        validate_source(source)
        framework = framework_root(source)
        if framework:
            destination_root = frameworks / framework.name
            if framework not in frameworks_copied:
                target = destination_root / source.relative_to(framework)
                if destination_root.exists():
                    # Install-name rewrites invalidate byte hashes; LC_UUID identifies the same build.
                    if not target.is_file() or not uuids(source) or uuids(source) != uuids(target):
                        raise RuntimeError(f'Framework name collision: {framework} -> {destination_root}')
                else:
                    subprocess.run(['ditto', str(framework), str(destination_root)], check=True)
                frameworks_copied[framework] = destination_root
            destination = destination_root / source.relative_to(framework)
        else:
            destination = private_libs / source.name
            if destination.exists():
                if destination_hashes.get(destination, sha256(destination)) != sha256(source):
                    raise RuntimeError(f'Different external libraries share a basename: {source.name}')
            else:
                shutil.copy2(source, destination)
            destination_hashes[destination] = sha256(source)
            # Preserve Tcl/Tk script resources alongside the executable's ../lib lookup.
            if source.name.startswith(('libtcl', 'libtk')):
                for data in source.parent.iterdir():
                    if data.is_dir() and re.fullmatch(r'(?:tcl|tk)8\.[0-9]+', data.name):
                        source_root = next(root for root in allowed_roots if source.is_relative_to(root))
                        _copy_bundled_resource(data, contents / 'lib' / data.name, app, source_root)
        if source not in recorded:
            records.append({'source': str(source), 'originalSha256': sha256(source),
                            'destination': destination.relative_to(app).as_posix()})
            recorded.add(source)
            licenses_for(source)
        source_destinations[source] = destination.resolve()
        return source_destinations[source]

    def inspect():
        # Resolve inherited app runpaths during analysis. Final links become loader-relative,
        # and the runtime smoke runs without these analysis-only environment overrides.
        with _analysis_environment(app, qt):
            should_inspect = lambda path: not system(path) and is_macho(pathlib.Path(path))
            executable_path = str(contents / 'MacOS')
            graph = {}
            # Delocate 0.13's tree wrapper omits executable_path in its second pass.
            # Use its public primitives and pass the app's executable directory in both.
            for consumer in walk_directory(str(app), filt_func=should_inspect, executable_path=executable_path):
                for provider, install_name in get_dependencies(consumer, executable_path=executable_path, filt_func=should_inspect):
                    if provider is None:
                        raise RuntimeError(f'Unresolved native dependency {install_name} in {consumer}')
                    if not system(provider):
                        graph.setdefault(provider, {})[consumer] = install_name
            return graph

    for _ in range(5):
        graph = inspect()
        external = [pathlib.Path(name).resolve() for name in graph
                    if not pathlib.Path(name).resolve().is_relative_to(app)]
        # Validate before copying anything from a previously unrecognized source.
        for source in external:
            validate_source(source)
        mapping = {pathlib.Path(name).resolve(): local_copy(pathlib.Path(name).resolve()) for name in graph}
        for provider_name, consumers in graph.items():
            provider = mapping[pathlib.Path(provider_name).resolve()]
            for consumer_name, original_name in consumers.items():
                source_consumer = pathlib.Path(consumer_name).resolve()
                consumer = local_copy(source_consumer)
                relative = os.path.relpath(provider, consumer.parent).replace(os.sep, '/')
                new_name = '@loader_path/' + relative
                if original_name != new_name:
                    subprocess.run(['install_name_tool', '-change', original_name, new_name, str(consumer)], check=True)
        # LC_ID_DYLIB is excluded from Delocate's dependency graph. Inspect every
        # app image, but preserve already-relocatable IDs byte-for-byte: extensions
        # may have short IDs and no room to grow their load commands.
        library_ids = {
            provider: [line.strip() for line in run('otool', '-arch', 'all', '-D', str(provider)).splitlines()
                       if line.strip() and not line.rstrip().endswith(':')]
            for provider in sorted({path.resolve() for path in app.rglob('*') if is_macho(path)})}
        for provider, ids in library_ids.items():
            identity = _relocated_install_id(provider, ids, contents, library_ids)
            if identity is None:
                continue
            subprocess.run(['install_name_tool', '-id', identity, str(provider)], check=True)
            library_ids[provider] = [identity] * len(ids)
        # Re-scan complete copied frameworks, including any newly introduced helpers.
        if not external:
            break
    else:
        raise RuntimeError('Native dependency collection did not converge in five passes')
    final_graph = inspect()
    if any(not pathlib.Path(name).resolve().is_relative_to(app) for name in final_graph):
        raise RuntimeError('The final native dependency closure still references an external library')
    for link in app.rglob('*'):
        if not link.is_symlink() or link.resolve().is_relative_to(app):
            continue
        original = link.resolve()
        target = source_destinations.get(original)
        if target is None:
            for source_root, destination_root in frameworks_copied.items():
                if original.is_relative_to(source_root):
                    target = destination_root / original.relative_to(source_root)
                    break
        if target is None or not link.parent.resolve().is_relative_to(app):
            raise RuntimeError(f'Bundle symlink escapes the app: {link} -> {original}')
        # Replace only the app-owned symlink, keeping the original dependency untouched.
        link.unlink()
        link.symlink_to(os.path.relpath(target, link.parent))
    return records
