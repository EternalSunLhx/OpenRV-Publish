"""Read captured native diagnostics from one small, same-repository proof ZIP."""
from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import PurePosixPath
import re
import secrets
import selectors
import stat
import subprocess
import sys
import time
import zipfile

REPOSITORY = 'EternalSunLhx/OpenRV-Publish'
REPOSITORY_ID = 1466054290
MAX_ZIP = 1024 * 1024
MAX_MEMBER = 2 * 1024 * 1024
MAX_UNCOMPRESSED = 4 * 1024 * 1024
PROOF_NAMES = {'macos-mixed-qt-proof-x86_64': 'x86_64', 'macos-mixed-qt-proof-arm64': 'arm64'}
SELECTED = ('summary.json', 'negative-cpp.stderr.log', 'negative-cpp.stdout.ndjson')
TERMINAL_SEQUENCES = re.compile(
    r'(?:\x1b\]|\x9d)[\s\S]*?(?:\x07|\x1b\\|\x9c|\Z)'
    r'|(?:\x1b[PX^_]|[\x90\x98\x9e\x9f])[\s\S]*?(?:\x1b\\|\x9c|\Z)'
    r'|(?:\x1b\[|\x9b)[0-?]*[ -/]*[@-~]'
    r'|\x1b[ -/]*[0-~]|[\x00-\x08\x0b-\x1f\x7f-\x9f]')


def require(value, message):
    if not value:
        raise ValueError(message)


def numeric_id(value):
    require(isinstance(value, str) and re.fullmatch(r'[1-9][0-9]{0,19}', value) is not None,
            'Expected a positive numeric artifact ID of at most 20 digits')
    return int(value)


def unique_json(data):
    def pairs(values):
        result = {}
        for key, value in values:
            require(key not in result, 'Duplicate JSON identity field')
            result[key] = value
        return result
    return json.loads(data.decode('utf-8'), object_pairs_hook=pairs)


def gh_get(endpoint, maximum, timeout=45):
    """Limit bytes while streaming; discard gh debug/error output completely."""
    environment = os.environ.copy()
    environment.pop('GH_DEBUG', None)
    environment.pop('GH_FORCE_TTY', None)
    command = ['gh', 'api', '--hostname', 'github.com', endpoint]
    with subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL, env=environment) as process:
        parts, count = [], 0
        deadline = time.monotonic() + timeout
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                while True:
                    remaining = deadline - time.monotonic()
                    require(remaining > 0, 'GitHub read request exceeded its time limit')
                    require(selector.select(remaining), 'GitHub read request exceeded its time limit')
                    block = os.read(process.stdout.fileno(), min(65536, maximum + 1 - count))
                    if not block:
                        break
                    count += len(block)
                    require(count <= maximum, 'GitHub read response exceeded its byte limit')
                    parts.append(block)
            code = process.wait(timeout=max(0.01, deadline - time.monotonic()))
            require(code == 0, 'GitHub read request failed with exit code ' + str(code))
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
    return b''.join(parts)


def validate_metadata(metadata, artifact_id):
    require(isinstance(metadata, dict) and type(metadata.get('id')) is int and metadata['id'] == artifact_id,
            'Artifact metadata identity does not match the requested ID')
    require(metadata.get('name') in PROOF_NAMES and metadata.get('expired') is False,
            'Expected an unexpired macOS mixed Qt proof artifact')
    size = metadata.get('size_in_bytes')
    require(type(size) is int and 0 < size <= MAX_ZIP, 'Artifact compressed size must be at most 1 MiB')
    require(isinstance(metadata.get('digest'), str)
            and re.fullmatch(r'sha256:[0-9a-f]{64}', metadata['digest']) is not None,
            'Artifact metadata must provide an exact SHA256 digest')
    expected_url = f'https://api.github.com/repos/{REPOSITORY}/actions/artifacts/{artifact_id}/zip'
    require(metadata.get('archive_download_url') == expected_url, 'Artifact archive URL has a different repository or ID')
    workflow = metadata.get('workflow_run')
    require(isinstance(workflow, dict) and type(workflow.get('id')) is int and workflow['id'] > 0
            and workflow.get('repository_id') == REPOSITORY_ID and workflow.get('head_repository_id') == REPOSITORY_ID
            and isinstance(workflow.get('head_sha'), str)
            and re.fullmatch(r'[0-9a-f]{40}', workflow['head_sha']) is not None,
            'Artifact workflow does not identify this repository and a full source SHA')
    return workflow


def validate_run(run, workflow):
    require(isinstance(run, dict) and run.get('id') == workflow['id'] and run.get('head_sha') == workflow['head_sha']
            and run.get('path') == '.github/workflows/macos-mixed-qt-diagnostic.yml'
            and run.get('event') == 'workflow_dispatch', 'Artifact run is not the matching manual mixed Qt diagnostic')
    for key in ('repository', 'head_repository'):
        repository = run.get(key)
        require(isinstance(repository, dict) and repository.get('id') == REPOSITORY_ID
                and repository.get('full_name') == REPOSITORY, 'Artifact run repository identity is different')


def read_proof(data, metadata):
    require(len(data) == metadata['size_in_bytes'], 'Downloaded artifact byte size differs from metadata')
    require(hashlib.sha256(data).hexdigest() == metadata['digest'][7:], 'Downloaded artifact SHA256 differs from metadata')
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        entries = archive.infolist()
        require(0 < len(entries) <= 32, 'Proof ZIP has too many entries')
        names, selected, total = set(), {}, 0
        for entry in entries:
            # ZipInfo can normalize Windows backslashes or truncate NULs.
            # Validate the original header name before accepting that spelling.
            name = entry.orig_filename
            path = PurePosixPath(name)
            require(name and name == entry.filename and not path.is_absolute() and '\\' not in name and ':' not in name
                    and '\x00' not in name and all(part not in ('', '.', '..') for part in name.rstrip('/').split('/')),
                    'Proof ZIP contains an unsafe member path')
            require(name.casefold() not in names, 'Proof ZIP contains duplicate member names')
            names.add(name.casefold())
            mode = entry.external_attr >> 16
            require(not stat.S_ISLNK(mode) and stat.S_IFMT(mode) in (0, stat.S_IFREG, stat.S_IFDIR),
                    'Proof ZIP contains a symlink or special file')
            require(not entry.flag_bits & 1 and entry.compress_type in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED),
                    'Proof ZIP contains encryption or an unsupported compression method')
            total += entry.file_size
            require(0 <= entry.file_size <= MAX_MEMBER and total <= MAX_UNCOMPRESSED,
                    'Proof ZIP exceeds its uncompressed byte limits')
            require(entry.file_size == 0 or (entry.compress_size > 0 and entry.file_size <= entry.compress_size * 100),
                    'Proof ZIP exceeds its compression-ratio limit')
            if name in SELECTED:
                require(not entry.is_dir() and stat.S_IFMT(mode) in (0, stat.S_IFREG),
                        'Selected diagnostic member is a directory')
                with archive.open(entry) as stream:
                    value = stream.read(MAX_MEMBER + 1)
                require(len(value) <= MAX_MEMBER and len(value) == entry.file_size, 'Selected diagnostic member exceeded its bound')
                selected[name] = value
        require(set(selected) == set(SELECTED), 'Proof ZIP is missing a captured negative C++ diagnostic member')
    summary = unique_json(selected['summary.json'])
    require(isinstance(summary, dict) and summary.get('check') == 'mixed-qt-bundling-mechanism-fixture'
            and summary.get('controllerSourceSnapshot') == metadata['workflow_run']['head_sha']
            and summary.get('productRuntimeVerified') is False
            and summary.get('architecture') == PROOF_NAMES[metadata['name']],
            'Proof summary source, architecture, or fixture identity differs from metadata')
    return summary, selected


def redact(value):
    text = value.decode('utf-8-sig', errors='replace') if isinstance(value, bytes) else str(value)
    text = TERMINAL_SEQUENCES.sub('', text.replace('\r\n', '\n').replace('\r', '\n'))
    for key in ('GH_TOKEN', 'GITHUB_TOKEN'):
        token = os.environ.get(key, '')
        if token:
            text = text.replace(token, '<redacted-token>')
    text = re.sub(r'https?://[^\s<>\"\']+', '<url>', text, flags=re.IGNORECASE)
    text = re.sub(r'(?im)\b(?:proxy-authorization|authorization|cookie|set-cookie)\s*[:=][^\r\n]*', '<redacted-header>', text)
    text = re.sub(r'(?i)\b(?:bearer|basic|token)\s+[^\s,;]+', '<redacted-authorization>', text)
    text = re.sub(r'(?i)\b(?:GH_TOKEN|GITHUB_TOKEN|access_token|api_key|client_secret|password|sas_token|sig)\s*[=:]\s*[^\s,;]+', '<redacted-secret>', text)
    return re.sub(r'\b(?:gh[pousr]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+)\b', '<redacted-token>', text)


def tail(value, maximum):
    return redact(value).encode('utf-8')[-(maximum - 1):].decode('utf-8', errors='ignore')


def emit_sections(sections):
    # All section bodies are already sanitized and bounded. No remote bytes are
    # emitted before this unpredictable workflow-command stop marker.
    token = secrets.token_hex(32)
    while any(token in body for _, body in sections):
        token = secrets.token_hex(32)
    print(f'::stop-commands::{token}', flush=True)
    try:
        for title, body in sections:
            print(title)
            print(body)
    finally:
        print(f'::{token}::', flush=True)


def main(argv=None):
    arguments = sys.argv[1:] if argv is None else argv
    try:
        require(len(arguments) == 1, 'Provide exactly one artifact ID')
        artifact_id = numeric_id(arguments[0])
        require(os.environ.get('GITHUB_REPOSITORY') == REPOSITORY, 'Artifact reader is restricted to its authorized repository')
        endpoint = f'repos/{REPOSITORY}/actions/artifacts/{artifact_id}'
        metadata = unique_json(gh_get(endpoint, 65536))
        workflow = validate_metadata(metadata, artifact_id)
        run = unique_json(gh_get(f'repos/{REPOSITORY}/actions/runs/{workflow["id"]}', 65536))
        validate_run(run, workflow)
        summary, selected = read_proof(gh_get(endpoint + '/zip', metadata['size_in_bytes']), metadata)
        baseline = summary.get('cases', {}).get('negative', {}).get('cppBaseline', {})
        require(isinstance(baseline, dict), 'Captured C++ baseline record is malformed')
        identity = {key: summary.get(key) for key in ('check', 'architecture', 'controllerSourceSnapshot', 'productRuntimeVerified')}
        for key, limit in (('status', 32), ('phase', 128), ('negativeSourceSnapshot', 40), ('errorType', 80), ('error', 512)):
            value = summary.get(key)
            identity[key] = redact(value)[:limit] if isinstance(value, str) else None
        require(baseline.get('exitCode') is None or type(baseline.get('exitCode')) is int,
                'Captured C++ exit code is malformed')
        require(baseline.get('timedOut') is None or type(baseline.get('timedOut')) is bool,
                'Captured C++ timeout state is malformed')
        identity.update(artifactId=artifact_id, artifactSha256=metadata['digest'], workflowRunId=workflow['id'],
                        negativeCppExitCode=baseline.get('exitCode'), negativeCppTimedOut=baseline.get('timedOut'))
        emit_sections([('VERIFIED CAPTURE IDENTITY (at most 3 KiB):', tail(json.dumps(identity, sort_keys=True), 3072)),
            ('CAPTURED negative-cpp.stderr.log TAIL (at most 12 KiB):', tail(selected['negative-cpp.stderr.log'], 12 * 1024)),
            ('CAPTURED negative-cpp.stdout.ndjson TAIL (at most 8 KiB):', tail(selected['negative-cpp.stdout.ndjson'], 8 * 1024))])
        return 0
    except Exception as error:
        emit_sections([('ARTIFACT READ FAILED (at most 2 KiB):', tail(type(error).__name__ + ': ' + str(error), 2048))])
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
