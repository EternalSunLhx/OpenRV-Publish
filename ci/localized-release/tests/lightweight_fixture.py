"""Strictly local, unrelated Git histories for the publish-only source contract."""
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest import mock

CI = Path(__file__).resolve().parents[1]
ROOT = CI.parents[1]
sys.path.insert(0, str(CI))
import official_workspace as workspace


@contextmanager
def lightweight_fixture(*, include_metadata=False, optional='absent', annotated=False):
    w = workspace.watcher()
    with tempfile.TemporaryDirectory(prefix='openrv-light-fixture-') as directory:
        root = Path(directory)
        upstream, trusted = root / 'upstream', root / 'trusted'

        def git(repo, *arguments):
            return subprocess.check_output(['git', '-C', str(repo), *arguments], env=w.git_environment(),
                                           text=True, encoding='utf-8', stderr=subprocess.DEVNULL).strip()

        def write(repo, path, data):
            target = repo / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data.encode() if isinstance(data, str) else data)

        for repo in (upstream, trusted):
            repo.mkdir()
            git(repo, 'init', '-b', 'main')
            git(repo, 'config', 'user.name', 'Local Fixture')
            git(repo, 'config', 'user.email', 'fixture@example.invalid')
            git(repo, 'config', 'core.autocrlf', 'false')
        write(upstream, 'CMakeLists.txt', 'SET(RV_MAJOR_VERSION "4")\nSET(RV_MINOR_VERSION "0")\nSET(RV_REVISION_NUMBER "0")\n')
        write(upstream, 'app.cpp', '// stable upstream behavior\n')
        write(upstream, 'caption.txt', 'Original caption\n')
        write(upstream, '.github/workflows/upstream.yml', 'upstream workflow\n')
        component = 'src/plugins/rv-packages/annotate_beta'
        write(upstream, component + '/caption.py', 'original optional caption\n')
        write(upstream, component + '/marker.py', '# complete component\n')
        git(upstream, 'add', '.')
        git(upstream, 'commit', '-m', 'official common base')
        common = git(upstream, 'rev-parse', 'HEAD')
        write(upstream, 'app.cpp', '// development-only behavior\n')
        write(upstream, 'dev-only.cpp', '// unreleased source\n')
        git(upstream, 'add', '.')
        git(upstream, 'commit', '-m', 'official patch boundary')
        patch_base = git(upstream, 'rev-parse', 'HEAD')
        write(upstream, 'caption.txt', 'Localized caption\n')
        core = subprocess.check_output(['git', '-C', str(upstream), 'diff', '--binary', '--full-index', '--', 'caption.txt'], env=w.git_environment())
        write(upstream, component + '/caption.py', 'translated optional caption\n')
        optional_patch = subprocess.check_output(['git', '-C', str(upstream), 'diff', '--binary', '--full-index', '--', component], env=w.git_environment())
        git(upstream, 'reset', '--hard', 'HEAD')
        git(upstream, 'checkout', '-b', 'stable', common)
        if optional == 'absent':
            git(upstream, 'rm', '-r', component)
        elif optional == 'partial':
            git(upstream, 'rm', component + '/caption.py')
        write(upstream, 'stable-only.txt', 'Published source\n')
        git(upstream, 'add', '.')
        git(upstream, 'commit', '-m', 'published stable release')
        official = git(upstream, 'rev-parse', 'HEAD')
        if annotated:
            git(upstream, 'tag', '-a', 'v4.0.3', '-m', 'Official annotated fixture')
        else:
            git(upstream, 'tag', 'v4.0.3')
        tag_object = git(upstream, 'rev-parse', 'refs/tags/v4.0.3')

        write(trusted, 'patches/core.patch', core)
        write(trusted, 'patches/optional.patch', optional_patch)
        owned = []

        def own(source, destination, data, *, replace=False, mode='100644'):
            write(trusted, source, data)
            owned.append({'source': source, 'destination': destination, 'mode': mode,
                          'sha256': hashlib.sha256((trusted / source).read_bytes()).hexdigest(), 'replace': replace})

        own('.github/workflows/localized.yml', '.github/workflows/localized.yml', 'trusted controller\n', replace=True)
        own('overlay/feature.cpp', 'feature.cpp', '// our owned feature\n')
        own('overlay/binary.dat', 'resources/binary.dat', b'\0\xff\x80\nowned\0')
        own('tools/run-fixture.sh', 'tools/run-fixture.sh', '#!/bin/sh\nexit 0\n', mode='100755')
        if include_metadata:
            for path in ('ci/upstream-release-watch/watch.py', 'ci/localized-release/official_workspace.py',
                         'ci/localized-release/official_release.py'):
                own(path, path, (ROOT / path).read_bytes(), replace=True)
            for path in ('src/lib/app/RvCommon/RvLocalizedBuild.cmake',
                         'src/lib/app/RvCommon/RvCommon/RvLocalizedBuild.h.in'):
                source = 'overlay/' + path
                own(source, path, (ROOT / source).read_bytes())
        configuration = {'schema': 2, 'repository': w.UPSTREAM, 'source_base_sha': patch_base,
            'optional_upstream_components': [component],
            'patches': [{'path': 'patches/core.patch', 'sha256': hashlib.sha256(core).hexdigest(), 'optional_component': None},
                        {'path': 'patches/optional.patch', 'sha256': hashlib.sha256(optional_patch).hexdigest(), 'optional_component': component}],
            'owned_files': owned}
        write(trusted, w.LOCALIZATION_BASE_FILE, json.dumps(configuration, indent=2) + '\n')
        git(trusted, 'add', '.')
        git(trusted, 'update-index', '--chmod=+x', 'tools/run-fixture.sh')
        git(trusted, 'commit', '-m', 'owned lightweight controller; no upstream parent')
        snapshot = git(trusted, 'rev-parse', 'HEAD')
        git(trusted, 'tag', 'v4.0.3')  # Remote/publication name deliberately identifies our commit.
        release = w.Release(456, 'v4.0.3', tag_object, official, '2026-08-20T17:15:15Z')
        original_git = w.git

        def local_git(repo, *arguments, **keywords):
            arguments = tuple(str(value) for value in arguments)
            public = 'https://github.com/' + w.UPSTREAM + '.git'
            if public in arguments:
                arguments = tuple(str(upstream) if value == public else value for value in arguments)
            return original_git(repo, *arguments, **keywords)

        with mock.patch.object(w, 'git', side_effect=local_git):
            yield {'root': root, 'trusted': trusted, 'upstream': upstream, 'patch_base': patch_base,
                   'snapshot': snapshot, 'release': release, 'config': configuration, 'git': git, 'write': write}


def prepare_fixture(fixture, *, number=29, publication_commit=None, name='source', checker=lambda *args: None):
    output = fixture['root'] / (name + '-input')
    plan, digest = workspace.prepare_workspace(fixture['trusted'], fixture['root'] / name, output,
        fixture['release'], number, checker=checker,
        publication_commit=publication_commit or fixture['snapshot'])
    return plan, digest, output
