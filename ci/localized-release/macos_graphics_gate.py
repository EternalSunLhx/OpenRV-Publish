"""Fail before cold dependencies unless the real native graphics probe is ready.

This gate verifies runner capability only. Product GUI, codec and signing gates
remain required after the full build.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import re
import subprocess

PRODUCT_CGL_SEQUENCE = ('typed-hardware-float', 'typed-hardware-rgba8', 'typed-generic-float-renderer')
FLOAT_FORMATS = ('RGB16F', 'RGBA16F', 'RGBA32F')
QT_VERSION = '6.5.3'
SHA = re.compile(r'[0-9a-f]{40}')
POLICY_HEADER = 'src/lib/graphics/TwkGLFFBO/TwkGLFFBO/MacCGLContext.h'


def require(condition, message):
    if not condition:
        raise ValueError(message)


def integer(value):
    return type(value) is int


def git(repo, *args):
    return subprocess.check_output(['git', '-C', str(repo), *args], timeout=30).decode().strip()


def gitlink(repo, tree):
    entry = git(repo, 'ls-tree', tree, '--', 'src/pub')
    match = re.fullmatch(r'160000 commit ([0-9a-f]{40})\tsrc/pub', entry)
    require(match is not None, 'Source tree has no canonical src/pub gitlink')
    return match.group(1)


def vendor_identity(repo, plan):
    """Keep official provenance distinct from the verified composed vendor."""
    official = gitlink(repo, plan['official']['commit_sha'])
    composed = gitlink(repo, plan['patched_tree'])
    vendor = repo / 'src/pub'
    actual = git(vendor, 'rev-parse', 'HEAD')
    require(actual == composed, 'Checked-out vendor differs from composed source gitlink')
    require(not git(vendor, 'status', '--porcelain', '--untracked-files=no'), 'Vendor tracked source is modified')
    files = {}
    for name in ('glew/glew.c', 'glew/GL/glew.h'):
        data = (vendor / name).read_bytes()
        expected_blob = git(vendor, 'rev-parse', composed + ':' + name)
        require(SHA.fullmatch(expected_blob) is not None, 'Vendor source blob identity is invalid')
        actual_blob = hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest()
        require(actual_blob == expected_blob, 'Vendor GLEW bytes differ from composed source: ' + name)
        files[name] = {'gitBlob': expected_blob, 'sha256': hashlib.sha256(data).hexdigest()}
    return {'officialVendorCommit': official, 'composedVendorCommit': composed,
            'checkedOutVendorCommit': actual, 'files': files}


def readback_passed(gl):
    require(isinstance(gl, dict), 'Missing GL execution report')
    require(integer(gl.get('glewStatus')) and gl['glewStatus'] == 0, 'Bundled GLEW initialization failed')
    for field in ('contextCurrent', 'glewVersion21', 'extFramebufferObject', 'glsl120Linked',
                  'rvioContextReady', 'allFloatFormatsPassed'):
        require(gl.get(field) is True, 'GL requirement did not pass: ' + field)
    for field in ('version', 'renderer', 'vendor', 'glslVersion'):
        require(isinstance(gl.get(field), str) and bool(gl[field].strip()), 'Missing actual GL identity: ' + field)
    tests = gl.get('floatFboTests')
    require(isinstance(tests, list) and len(tests) == len(FLOAT_FORMATS), 'Missing three float FBO results')
    require(all(isinstance(test, dict) for test in tests), 'Malformed float FBO result')
    require(all(isinstance(test.get('format'), str) for test in tests)
            and {test['format'] for test in tests} == set(FLOAT_FORMATS),
            'Float FBO formats differ from product requirements')
    for test in tests:
        require(test.get('status') == 'passed' and test.get('pixelMatched') is True,
                'Float FBO shader readback failed: ' + test['format'])
        require(integer(test.get('framebufferStatus')) and test['framebufferStatus'] == 0x8CD5,
                'Float FBO was incomplete: ' + test['format'])
        require(integer(test.get('glError')) and test['glError'] == 0 and test.get('priorGlErrors') == [],
                'Float FBO reported GL errors: ' + test['format'])
        pixel = test.get('pixel')
        require(isinstance(pixel, list) and len(pixel) == 4, 'Missing float readback pixel')
        require(all(type(value) in (int, float) and math.isfinite(value)
                    and abs(value - expected) < 0.002
                    for value, expected in zip(pixel, (0.125, 0.5, 0.75, 1.0))),
                'Float readback values differ from the rendered shader color')


def checked_report(record, mode, glew_sha256, policy_sha256):
    require(isinstance(record, dict) and record.get('timedOut') is False
            and integer(record.get('exitCode')) and record['exitCode'] == 0,
            'Native process failed or timed out: ' + mode)
    report = record.get('report')
    require(isinstance(report, dict) and report.get('check') == 'native-macos-graphics-diagnostic'
            and report.get('mode') == mode and report.get('productRuntimeVerified') is False,
            'Unexpected native report identity: ' + mode)
    require(report.get('qtVersion') == QT_VERSION, 'Probe Qt differs from the product Qt SDK')
    require(report.get('bundledGlewSourceSha256') == glew_sha256,
            'Probe was compiled with different bundled GLEW source bytes')
    require(report.get('productionContextPolicySha256') == policy_sha256,
            'Probe was compiled with a different production CGL context policy')
    return report


def readiness(results, glew_sha256, policy_sha256):
    cgl = checked_report(results.get('cgl'), 'cgl', glew_sha256, policy_sha256)
    cases = cgl.get('cases')
    require(isinstance(cases, list) and all(isinstance(case, dict) for case in cases), 'Missing CGL candidate results')
    policies = [case for case in cases if case.get('name') == 'production-policy']
    require(len(policies) == 1, 'Missing or duplicated shared production-policy execution')
    policy = policies[0]
    selected = policy.get('selectedCandidate')
    require(selected in PRODUCT_CGL_SEQUENCE, 'Shared production policy did not select a product candidate')
    require(policy.get('status') == 'ready' and policy.get('contextCurrent') is True,
            'Shared production CGL context is not ready and current')
    attempts = policy.get('attempts')
    require(isinstance(attempts, list) and 1 <= len(attempts) <= len(PRODUCT_CGL_SEQUENCE)
            and all(isinstance(attempt, dict) for attempt in attempts), 'Missing actual production CGL attempts')
    require([attempt.get('name') for attempt in attempts] == list(PRODUCT_CGL_SEQUENCE[:len(attempts)])
            and attempts[-1]['name'] == selected, 'Actual CGL attempt trace differs from the production sequence')
    final = attempts[-1]
    for field in ('chooseError', 'createError', 'currentError'):
        require(integer(final.get(field)) and final[field] == 0, 'Production CGL call failed: ' + field)
    require(integer(final.get('pixelFormatCount')) and final['pixelFormatCount'] > 0,
            'Production CGL policy did not obtain a pixel format')
    # Exercise the exact context returned by the shared product implementation.
    # A different passing diagnostic candidate cannot hide a broken selection.
    readback_passed(policy.get('gl'))
    qt = checked_report(results.get('qt-default'), 'qt-default', glew_sha256, policy_sha256)
    require(qt.get('status') == 'passed' and qt.get('platformPlugin') == 'cocoa'
            and qt.get('softwareAttributeBeforeApplication') is False and qt.get('openGLES') is False,
            'Default Qt must use the real Cocoa desktop OpenGL path')
    require(integer(qt.get('actualMajor')) and integer(qt.get('actualMinor'))
            and qt['actualMinor'] >= 0
            and (qt['actualMajor'] > 2 or (qt['actualMajor'] == 2 and qt['actualMinor'] >= 1)),
            'Default Qt actual desktop OpenGL version is below 2.1')
    require(integer(qt.get('actualDepth')) and qt['actualDepth'] >= 24
            and integer(qt.get('actualStencil')) and qt['actualStencil'] >= 8,
            'Default Qt actual depth/stencil format is below the product 24/8 requirement')
    for field in ('widgetVisible', 'widgetValid', 'windowExposed', 'visibleReadbackMatched'):
        require(qt.get(field) is True, 'Default Qt visible widget requirement failed: ' + field)
    require(qt.get('timedOut') is False, 'Default Qt visible widget timed out')
    require(all(integer(qt.get(field)) and qt[field] > 0 for field in ('imageWidth', 'imageHeight')),
            'Default Qt framebuffer image is empty')
    pixel = qt.get('widgetPixel')
    require(isinstance(pixel, list) and len(pixel) == 4
            and all(integer(value) for value in pixel)
            and all(abs(value - expected) <= 3 for value, expected in zip(pixel[:3], (32, 128, 191)))
            and 252 <= pixel[3] <= 255, 'Default Qt framebuffer pixel differs from rendered color')
    readback_passed(qt.get('gl'))
    return selected


def collect(executable, output):
    environment = os.environ.copy()
    for key in ('QT_OPENGL', 'QT_QPA_PLATFORM', 'QT_QUICK_BACKEND'):
        environment.pop(key, None)
    results = {}
    for mode in ('cgl', 'qt-default'):
        record = {'timedOut': False, 'exitCode': None, 'report': None}
        stdout, stderr = '', ''
        try:
            process = subprocess.run([str(executable), mode], cwd=output, env=environment,
                capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=90)
            stdout, stderr = process.stdout, process.stderr
            record['exitCode'] = process.returncode
        except subprocess.TimeoutExpired as error:
            stdout = error.stdout.decode('utf-8', errors='replace') if isinstance(error.stdout, bytes) else (error.stdout or '')
            stderr = error.stderr.decode('utf-8', errors='replace') if isinstance(error.stderr, bytes) else (error.stderr or '')
            record['timedOut'] = True
        except OSError as error:
            stderr = str(error)
        (output / (mode + '.stdout.json')).write_text(stdout, encoding='utf-8')
        (output / (mode + '.stderr.log')).write_text(stderr, encoding='utf-8')
        try:
            def invalid_constant(value):
                raise ValueError('Non-finite JSON constant: ' + value)
            record['report'] = json.loads(stdout, parse_constant=invalid_constant)
        except ValueError as error:
            record['reportError'] = str(error)
        results[mode] = record
    return results


def build_probe(repo, qt, build, arch, output):
    require(not build.exists(), 'Readiness build must start in a fresh directory')
    for name, command in (
        ('configure', ['cmake', '-S', str(repo / 'ci/localized-release/macos-gl-readiness'), '-B', str(build),
            '-DCMAKE_BUILD_TYPE=Release', '-DCMAKE_PREFIX_PATH=' + str(qt),
            '-DCMAKE_OSX_ARCHITECTURES=' + arch, '-DCMAKE_OSX_DEPLOYMENT_TARGET=15.0']),
        ('build', ['cmake', '--build', str(build), '--parallel', '2'])):
        process = subprocess.run(command, cwd=repo, capture_output=True, text=True,
            encoding='utf-8', errors='replace', timeout=180)
        (output / (name + '.log')).write_text(process.stdout + '\n' + process.stderr, encoding='utf-8')
        require(process.returncode == 0, 'Tiny native readiness probe ' + name + ' failed')
    executable = build / 'openrv-macos-gl-readiness'
    require(executable.is_file(), 'Tiny native readiness executable is missing')
    return executable.resolve(strict=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', type=Path, default=Path.cwd())
    parser.add_argument('--qt', type=Path, required=True)
    parser.add_argument('--build', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--arch', choices=('x86_64', 'arm64'), required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    summary = {'check': 'macos-graphics-readiness', 'status': 'failed', 'productRuntimeVerified': False,
               'architecture': args.arch, 'productCglSequence': list(PRODUCT_CGL_SEQUENCE)}
    try:
        require(platform.system() == 'Darwin' and platform.machine() == args.arch,
                'Readiness requires the actual native macOS runner architecture')
        require(os.environ.get('GITHUB_ACTIONS') == 'true', 'Readiness requires the disposable official Actions workspace')
        require(re.fullmatch(r'[0-9a-f]{64}', os.environ.get('ORV_BUILD_PLAN_SHA256', '')) is not None,
                'Readiness requires the immutable official build plan digest')
        from official_workspace import verify_materialized
        repo = args.repo.resolve(strict=True)
        plan = verify_materialized(repo, expected_snapshot=os.environ.get('GITHUB_SHA'),
                                   expected_build=os.environ.get('ORV_BUILD_NUMBER'))
        summary.update({'controllerSourceSha': plan['localization_snapshot'],
                        'officialSourceCommit': plan['official']['commit_sha'], 'composedTree': plan['patched_tree']})
        vendor = vendor_identity(repo, plan)
        summary['vendor'] = vendor
        policy_sha256 = hashlib.sha256((repo / POLICY_HEADER).read_bytes()).hexdigest()
        summary['productionContextPolicySha256'] = policy_sha256
        executable = build_probe(repo, args.qt.resolve(strict=True), args.build.resolve(), args.arch, output)
        summary['modes'] = collect(executable, output)
        summary['selectedCglCandidate'] = readiness(summary['modes'], vendor['files']['glew/glew.c']['sha256'], policy_sha256)
        summary['status'] = 'passed'
    except (ValueError, OSError, TypeError, KeyError, subprocess.SubprocessError) as error:
        summary['error'] = str(error)
    encoded = json.dumps(summary, indent=2, sort_keys=True, allow_nan=False) + '\n'
    (output / 'readiness.json').write_text(encoded, encoding='utf-8', newline='\n')
    print(encoded, end='', flush=True)
    return 0 if summary['status'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
