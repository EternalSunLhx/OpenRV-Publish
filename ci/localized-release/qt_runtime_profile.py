"""Precise Qt runtime exclusions shared by SDK checks and native packaging."""
from __future__ import annotations

import hashlib
from pathlib import Path
import stat

# These optional database drivers require external client SDKs absent from this media build.
MACOS_EXCLUDED_QT_PLUGINS = (
    'sqldrivers/libqsqlodbc.dylib',
    'sqldrivers/libqsqlpsql.dylib',
)
WINDOWS_EXCLUDED_QT_PLUGINS = ('sqldrivers/qsqlpsql.dll',)
MACOS_PLUGIN_ROOTS = (
    'Contents/PlugIns/Qt',
    'Contents/lib/python3.11/site-packages/PySide6/Qt/plugins',
)
WINDOWS_PLUGIN_ROOTS = ('PlugIns/Qt', 'lib/site-packages/PySide6/plugins')


def excluded_macos_qt_plugin(path: Path, plugin_root: Path) -> bool:
    try:
        return path.relative_to(plugin_root).as_posix() in MACOS_EXCLUDED_QT_PLUGINS
    except ValueError:
        return False


def _plain_directory(path: Path) -> None:
    info = path.lstat()
    if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
            or getattr(info, 'st_file_attributes', 0) & 0x400):
        raise ValueError(f'Qt runtime profile refuses a redirected/non-directory parent: {path}')


def _plugin_root(runtime: Path, relative: str, *, required=False) -> Path | None:
    current = runtime
    for part in relative.split('/'):
        current /= part
        try:
            _plain_directory(current)
        except FileNotFoundError:
            if required:
                raise ValueError(f'Missing main Qt plugin directory: {current}') from None
            return None
    return current


def _apply_profile(runtime: Path, roots: tuple[str, ...], alternate: str,
                   excluded: tuple[str, ...]) -> list[dict]:
    runtime = runtime.absolute()
    _plain_directory(runtime)
    owner = runtime.resolve(strict=True)
    deployed = [_plugin_root(runtime, relative, required=index == 0)
                for index, relative in enumerate(roots)]
    unexpected = _plugin_root(runtime, alternate)
    if unexpected is not None:
        for relative in excluded:
            path = unexpected / relative
            if path.parent.exists() or path.parent.is_symlink():
                _plain_directory(path.parent)
            if path.exists() or path.is_symlink():
                raise ValueError(f'Excluded Qt driver in an unexpected standard layout: {path}')
    candidates = []
    for plugin_root in deployed:
        if plugin_root is None:
            continue
        for relative in excluded:
            path = plugin_root / relative
            if not path.parent.exists() and not path.parent.is_symlink():
                continue
            _plain_directory(path.parent)
            if not path.exists() and not path.is_symlink():
                continue
            info = path.lstat()
            if (not stat.S_ISREG(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400
                    or not path.resolve(strict=True).is_relative_to(owner)):
                raise ValueError(f'Qt runtime profile refuses a redirected/non-regular driver: {path}')
            with path.open('rb') as stream:
                original_hash = hashlib.file_digest(stream, 'sha256').hexdigest()
            candidates.append((path, {'path': path.relative_to(runtime).as_posix(),
                                      'originalSha256': original_hash}))
    # Validate every target before removing any file; never modify the vendor SDK.
    for path, record in candidates:
        path.unlink()
    for relative_root in (*roots, alternate):
        plugin_root = _plugin_root(runtime, relative_root)
        if plugin_root is not None and any((plugin_root / name).exists() or (plugin_root / name).is_symlink()
                                           for name in excluded):
            raise RuntimeError(f'Excluded Qt driver survived the runtime profile: {plugin_root}')
    return [record for path, record in candidates]


def apply_macos_qt_runtime_profile(app: Path) -> list[dict]:
    return _apply_profile(app, MACOS_PLUGIN_ROOTS,
                          'Contents/lib/python3.11/site-packages/PySide6/plugins',
                          MACOS_EXCLUDED_QT_PLUGINS)


def apply_windows_qt_runtime_profile(runtime: Path) -> list[dict]:
    return _apply_profile(runtime, WINDOWS_PLUGIN_ROOTS,
                          'lib/site-packages/PySide6/Qt/plugins', WINDOWS_EXCLUDED_QT_PLUGINS)
