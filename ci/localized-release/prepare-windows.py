"""Make the CI-only CMake hook explicit and record the exact build-system patch."""
from __future__ import annotations

import pathlib

from release_common import sha256, write_json

repo = pathlib.Path.cwd().resolve()
path = repo / 'cmake' / 'dependencies' / 'CMakeLists.txt'
marker = '# OpenRV localized release dependency hooks'
original = path.read_text(encoding='utf-8')
if marker in original:
    raise RuntimeError('Expected a fresh checkout; the CI-only hook is already injected')
original_hash = sha256(path)
path.write_text(original + '\n' + marker + '\ninclude("${PROJECT_SOURCE_DIR}/ci/localized-release/dependency-hooks.cmake")\n',
                encoding='utf-8', newline='\n')
work = repo / '_release_work'
work.mkdir(exist_ok=True)
write_json(work / 'source-patches.json', {'patches': [{
    'file': str(path.relative_to(repo).as_posix()), 'purpose': 'Add CI-only dependency fix steps in their target directory',
    'originalSha256': original_hash, 'patchedSha256': sha256(path),
    'implementation': 'ci/localized-release/prepare-windows.py + dependency-hooks.cmake + fix_dependency.py'}]})
