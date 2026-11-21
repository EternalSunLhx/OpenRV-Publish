"""Shared, dependency-free metadata and release validation helpers."""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import re
import subprocess
from datetime import datetime, timezone

from official_release import release_identity


def run(*args: str, **kwargs) -> str:
    if os.name == 'nt' and pathlib.Path(args[0]).name.lower() in ('git', 'git.exe'):
        supplied_environment = kwargs.get('env')
        environment = (os.environ if supplied_environment is None else supplied_environment).copy()
        environment['MSYS'] = (environment.get('MSYS', '') + ' noglob').strip()
        kwargs['env'] = environment
    return subprocess.check_output(args, text=True, **kwargs).strip()


def source_metadata(repo: pathlib.Path) -> dict:
    text = (repo / "CMakeLists.txt").read_text(encoding="utf-8")
    version = ".".join(re.search(r'SET\(' + name + r'\s+"(\d+)"', text).group(1)
                       for name in ("RV_MAJOR_VERSION", "RV_MINOR_VERSION", "RV_REVISION_NUMBER"))
    commit = run("git", "-C", str(repo), "rev-parse", "HEAD")
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("Expected a full Git commit SHA")
    tag = os.environ.get('ORV_RELEASE_TAG', '')
    identity = release_identity(repo, tag, version) if tag else {'version': version, 'nativeVersion': version}
    release_version = (identity['version'] + '-build.' + str(identity['buildNumber'])
                       if 'buildNumber' in identity else tag[1:] if tag else version + '-dev.' + commit[:8])
    patch_record = repo / '_release_work' / 'source-patches.json'
    return {
        "product": "OpenRV", "edition": "English / Simplified Chinese community build",
        **identity, "sourceCommit": commit, "shortCommit": commit[:8],
        "releaseVersion": release_version, "releaseTag": tag,
        "ciBuildSystemPatches": json.loads(patch_record.read_text(encoding='utf-8'))['patches'] if patch_record.is_file() else [],
        "repository": os.environ.get("GITHUB_REPOSITORY", ""),
        "runUrl": (f"https://github.com/{os.environ['GITHUB_REPOSITORY']}/actions/runs/"
                   f"{os.environ['GITHUB_RUN_ID']}" if os.environ.get("GITHUB_RUN_ID") else ""),
        "builtAtUtc": datetime.now(timezone.utc).isoformat(),
        "configuration": "Release", "vfxPlatform": "CY2025", "qtVersion": "6.5.3",
        "languages": ["en", "zh_CN"], "codeSigned": False, "notarized": False,
        "submodules": run("git", "-C", str(repo), "submodule", "status", "--recursive").splitlines(),
    }


def sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def write_json(path: pathlib.Path, value: dict) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_checksums(output: pathlib.Path, names: list[str], filename: str) -> None:
    if any(pathlib.Path(name).name != name for name in names):
        raise ValueError("Only immediate output files may be published")
    (output / filename).write_text("".join(f"{sha256(output / name)}  {name}\n" for name in names),
                                   encoding="ascii")


def copy_licenses(repo: pathlib.Path, destination: pathlib.Path) -> None:
    import shutil
    destination.mkdir(parents=True, exist_ok=True)
    for name in ("LICENSE", "Notice.txt", "THIRD-PARTY.md"):
        shutil.copy2(repo / name, destination / name)


def copy_dependency_licenses(build: pathlib.Path, qt: pathlib.Path, destination: pathlib.Path) -> None:
    import shutil
    roots = [(path.name, path / 'src') for path in sorted(build.glob('RV_DEPS_*')) if path.is_dir()]
    roots.append(('Qt', qt))
    for name, root in roots:
        if not root.is_dir():
            continue
        for source in root.iterdir():
            if re.match(r'(?i)^(licen[cs]es?|copying|notice|copyright)(?:[._-]|$)', source.name):
                target = destination / name / source.name
                target.parent.mkdir(parents=True, exist_ok=True)
                if source.is_file():
                    shutil.copy2(source, target)
                elif source.name.lower() in ('licenses', 'license', 'licences'):
                    shutil.copytree(source, target, dirs_exist_ok=True)
    # The Qt binary archives do not always include license text directories.
    bundled = pathlib.Path(__file__).parent / 'licenses' / 'Qt-6.5.3'
    sources = json.loads((bundled / 'SOURCES.json').read_text(encoding='utf-8-sig'))
    qt_destination = destination / 'Qt-6.5.3'
    qt_destination.mkdir(parents=True, exist_ok=True)
    for entry in sources['files']:
        if pathlib.Path(entry['name']).name != entry['name'] or sha256(bundled / entry['name']) != entry['sha256']:
            raise ValueError('Vendored Qt license text does not match its source digest')
        shutil.copy2(bundled / entry['name'], qt_destination / entry['name'])
    shutil.copy2(bundled / 'SOURCES.json', qt_destination / 'SOURCES.json')


def validate_release_tag(repo: pathlib.Path, tag: str) -> None:
    text = (repo / 'CMakeLists.txt').read_text(encoding='utf-8')
    native_version = '.'.join(re.search(r'SET\(' + name + r'\s+"(\d+)"', text).group(1)
                              for name in ('RV_MAJOR_VERSION', 'RV_MINOR_VERSION', 'RV_REVISION_NUMBER'))
    version = release_identity(repo, tag, native_version)['version']
    if not (tag == 'v' + version or re.fullmatch(r"v" + re.escape(version) + r"-zh\.[1-9][0-9]*", tag)):
        raise ValueError('Release tag differs from verified source provenance')
    source_ref = ('refs/openrv-upstream/tags/' if tag == 'v' + version else 'refs/tags/') + tag
    if run("git", "-C", str(repo), "rev-list", "--max-count=1", source_ref) != run(
            "git", "-C", str(repo), "rev-parse", "HEAD"):
        raise ValueError("Release source reference does not identify this checkout")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=pathlib.Path, default=pathlib.Path.cwd())
    parser.add_argument("--tag", default="")
    args = parser.parse_args()
    if args.tag:
        validate_release_tag(args.repo, args.tag)
    print(json.dumps(source_metadata(args.repo), ensure_ascii=False, indent=2))
