"""Keep bounded, owned runtime evidence after Mac packaging, including failures.

No preferences, configurations, application bundles or raw crash dumps are copied.
Apple crash stacks require both the recorded child PID and exact executable path.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
from pathlib import Path
import sys

JSON_LIMIT = 512 * 1024
LOG_LIMIT = 64 * 1024
IPS_LIMIT = 4 * 1024 * 1024
TOTAL_LIMIT = 6 * 1024 * 1024
INDEX_RESERVE = 64 * 1024
PERSISTENCE_STAGES = ('00-snapshot', '01-chinese-select-english', '02-english-select-chinese',
                      '03-chinese-persisted', 'recovery-restore', 'recovery-verify')
SMOKE_STAGES = ('rv-version', 'python-ocio', 'png-to-exr', 'exr-to-png', 'dimensions',
                'gui-en', 'gui-zh_CN', 'aac-mp4')


def safe_file(root: Path, path: Path) -> bool:
    """Reject lexical escape, symlinks at every level, and non-regular files."""
    try:
        relative = path.relative_to(root)
        if '..' in relative.parts or root.is_symlink():
            return False
        current = root
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                return False
        return path.is_file() and path.resolve().is_relative_to(root.resolve())
    except (OSError, ValueError):
        return False


def read_json(root: Path, path: Path, limit: int = JSON_LIMIT):
    if not safe_file(root, path):
        return None
    try:
        with path.open('rb') as stream:
            data = stream.read(limit + 1)
        return json.loads(data) if len(data) <= limit else None
    except (OSError, ValueError, UnicodeError):
        return None


def crash_stack(data: str, executable: Path, pids: set[int]):
    """Read Apple's two JSON objects; never infer an anonymized procPath.

    Schema: developer.apple.com/documentation/xcode/interpreting-the-json-format-of-a-crash-report
    """
    decoder = json.JSONDecoder()
    try:
        first, end = decoder.raw_decode(data.lstrip())
        remainder = data.lstrip()[end:].strip()
        report = json.loads(remainder) if remainder else first
    except ValueError:
        return None
    if (not isinstance(report, dict) or type(report.get('pid')) is not int
            or report['pid'] not in pids or report.get('procPath') != str(executable)):
        return None
    threads, images = report.get('threads', []), report.get('usedImages', [])
    index = report.get('faultingThread')
    if (not isinstance(threads, list) or not isinstance(images, list)
            or type(index) is not int or not 0 <= index < len(threads)
            or not isinstance(threads[index], dict)):
        return None
    thread = threads[index]
    frames = thread.get('frames', [])
    if not isinstance(frames, list):
        return None
    selected_frames = []
    selected_images = {}
    for frame in frames[:256]:
        if not isinstance(frame, dict):
            continue
        selected_frames.append({key: frame[key] for key in ('imageIndex', 'imageOffset', 'symbol', 'symbolLocation') if key in frame})
        image_index = frame.get('imageIndex')
        if type(image_index) is int and 0 <= image_index < len(images) and isinstance(images[image_index], dict):
            selected_images[str(image_index)] = {key: images[image_index][key]
                                                for key in ('name', 'path', 'uuid', 'base', 'size', 'arch')
                                                if key in images[image_index]}
    return {'pid': report['pid'], 'procPath': report['procPath'], 'captureTime': report.get('captureTime'),
            'exception': {key: report.get('exception', {}).get(key) for key in ('type', 'signal', 'codes')}
            if isinstance(report.get('exception'), dict) else {},
            'termination': {key: report.get('termination', {}).get(key) for key in ('namespace', 'code', 'indicator')}
            if isinstance(report.get('termination'), dict) else {},
            'faultingThread': index, 'frames': selected_frames, 'images': selected_images,
            'framesTruncated': len(frames) > 256}


def collect(repo: Path, arch: str, diagnostic_dir: Path) -> Path:
    if arch not in ('x86_64', 'arm64'):
        raise ValueError('Unsupported architecture')
    repo = repo.resolve()
    work = repo / '_release_work'
    if work.is_symlink():
        raise ValueError('Runtime work directory must not be a symlink')
    destination = work / ('macos-runtime-evidence-' + arch)
    destination.mkdir(parents=True, exist_ok=False)
    index = {'schemaVersion': 1, 'architecture': arch, 'files': [], 'omitted': [],
             'crashStacks': 0, 'totalBytes': 0,
             'scope': 'Explicit runtime reports and log tails; exact executable/PID crash stacks only.'}

    def save(relative: Path, data: bytes, truncated=False):
        if index['totalBytes'] + len(data) > TOTAL_LIMIT - INDEX_RESERVE:
            index['omitted'].append({'path': relative.as_posix(), 'reason': 'total byte limit'})
            return False
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        index['totalBytes'] += len(data)
        index['files'].append({'path': relative.as_posix(), 'bytes': len(data), 'truncated': truncated})
        return True

    # Never recursively copy these directories: they also contain preference snapshots,
    # generated scripts, environment configuration and decoded media fixtures.
    for kind, stages in (('smoke', SMOKE_STAGES), ('persistence', PERSISTENCE_STAGES)):
        folder = work / (kind + '-' + arch)
        names = [Path('report.json')]
        names += [Path(stage) / name for stage in stages
                  for name in ('gui.json', 'progress.json', 'stdout.log', 'stderr.log')]
        for name in names:
            source = folder / name
            relative = Path(folder.name) / name
            if not safe_file(repo, source):
                continue
            limit = LOG_LIMIT if source.suffix == '.log' else JSON_LIMIT
            size = source.stat().st_size
            if source.suffix == '.json' and size > limit:
                index['omitted'].append({'path': relative.as_posix(), 'reason': 'JSON byte limit'})
                continue
            with source.open('rb') as stream:
                if size > limit:
                    stream.seek(-limit, 2)
                payload = stream.read(limit)
            save(relative, payload, size > limit)

    persistence = read_json(repo, work / ('persistence-' + arch) / 'report.json')
    launches = persistence.get('launches', []) if isinstance(persistence, dict) else []
    if not isinstance(launches, list):
        launches = []
    pids = {launch['pid'] for launch in launches
            if isinstance(launch, dict) and type(launch.get('pid')) is int and launch['pid'] > 0}
    try:
        started = datetime.datetime.fromisoformat(persistence['startedAtUtc']).timestamp()
        finished = datetime.datetime.fromisoformat(persistence['finishedAtUtc']).timestamp()
    except (TypeError, KeyError, ValueError):
        started, finished = 0, 0
    executable = repo / '_install_release/RV.app/Contents/MacOS/rv'
    if not safe_file(repo, executable):
        executable = executable.with_name('RV')
    if pids and started > 0 and finished >= started and safe_file(repo, executable):
        # Resolve only the already-owned executable. Untrusted report paths are compared
        # literally; placeholder/anonymized paths and previous runs cannot match.
        executable = executable.resolve()
        if diagnostic_dir.is_dir() and not any(path.is_symlink() for path in (diagnostic_dir, *diagnostic_dir.parents)):
            candidates = sorted(diagnostic_dir.glob('*.ips'), key=lambda path: path.name)
            for source in candidates:
                if not source.name.lower().startswith('rv-') and not source.name.lower().startswith('rv_'):
                    continue
                if not safe_file(diagnostic_dir, source):
                    continue
                stat = source.stat()
                if stat.st_size > IPS_LIMIT or not started - 2 <= stat.st_mtime <= finished + 60:
                    continue
                with source.open('rb') as stream:
                    raw = stream.read(IPS_LIMIT + 1)
                if len(raw) > IPS_LIMIT:
                    continue
                stack = crash_stack(raw.decode('utf-8', errors='replace'), executable, pids)
                if stack is None:
                    continue
                payload = (json.dumps(stack, indent=2, ensure_ascii=False) + '\n').encode('utf-8')
                if len(payload) > JSON_LIMIT:
                    continue
                if save(Path('crash-stacks') / (str(stack['pid']) + '-' + str(index['crashStacks']) + '.json'), payload):
                    index['crashStacks'] += 1
                if index['crashStacks'] == 3:
                    break
    index_bytes = (json.dumps(index, indent=2) + '\n').encode('utf-8')
    if len(index_bytes) > INDEX_RESERVE:
        raise RuntimeError('Evidence index exceeded reserved byte limit')
    (destination / 'index.json').write_bytes(index_bytes)
    return destination


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--arch', required=True, choices=('x86_64', 'arm64'))
    args = parser.parse_args()
    if (sys.platform != 'darwin' or any(os.environ.get(key) != value for key, value in
            {'GITHUB_ACTIONS': 'true', 'RUNNER_OS': 'macOS', 'RUNNER_ENVIRONMENT': 'github-hosted'}.items())):
        parser.error('Runtime evidence collection is limited to ephemeral GitHub-hosted macOS runners')
    repo = Path.cwd().resolve()
    if repo != Path(os.environ['GITHUB_WORKSPACE']).resolve():
        parser.error('Run from this job checkout')
    output = collect(repo, args.arch, Path.home() / 'Library/Logs/DiagnosticReports')
    print('Preserved bounded runtime evidence: ' + str(output))


if __name__ == '__main__':
    main()
