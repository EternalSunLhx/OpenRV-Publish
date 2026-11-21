"""Verify native AAC discovery plus real RVIO video and non-silent PCM decoding."""
from __future__ import annotations

import argparse
import array
import ctypes
import hashlib
import json
import math
import os
from pathlib import Path
import struct
import subprocess
import sys

FIXTURE_SHA256 = '2d8ec77bbe47cab7da42db7d50455502ae01106f8b6cee59a5c817eda0853947'
REQUIRED_DECODERS = (
    'aac', 'aac_fixed', 'aac_latm', 'ac3', 'ac3_fixed', 'eac3',
    'binkvideo', 'binkaudio_dct', 'binkaudio_rdft', 'dnxhd', 'dvvideo',
    'prores', 'prores_raw', 'qtrle', 'vp9', 'hevc', 'mpeg2video', 'svq1', 'svq3',
)


def decoder_inventory(library, path: Path):
    """Probe the actual bundled library; configure flags are not codec support."""
    library.avcodec_version.argtypes = []
    library.avcodec_version.restype = ctypes.c_uint
    version = library.avcodec_version()
    major, minor, micro = version >> 16, (version >> 8) & 255, version & 255
    if major < 60:
        raise RuntimeError('The release decoder profile requires FFmpeg 6 or later')
    library.avcodec_find_decoder_by_name.argtypes = [ctypes.c_char_p]
    library.avcodec_find_decoder_by_name.restype = ctypes.c_void_p
    names = [name for name in REQUIRED_DECODERS if major >= 62 or name != 'prores_raw']
    availability = [{'name': name, 'available': bool(library.avcodec_find_decoder_by_name(name.encode('ascii')))}
                    for name in names]
    return {'libraryPath': str(path.resolve()), 'librarySha256': hashlib.sha256(path.read_bytes()).hexdigest(),
            'libavcodecVersion': f'{major}.{minor}.{micro}', 'libavcodecMajor': major,
            'versionInteger': version, 'decoders': availability,
            'missing': [item['name'] for item in availability if not item['available']]}


def pcm_wave(path: Path):
    data = path.read_bytes()
    assert data[:4] == b'RIFF' and data[8:12] == b'WAVE', 'Expected RIFF PCM WAV'
    offset, fmt, pcm = 12, None, None
    while offset + 8 <= len(data):
        name, size = data[offset:offset + 4], struct.unpack_from('<I', data, offset + 4)[0]
        payload = data[offset + 8:offset + 8 + size]
        assert len(payload) == size, 'Truncated WAV chunk'
        if name == b'fmt ':
            fmt = payload
        elif name == b'data':
            pcm = payload
        offset += 8 + size + (size % 2)
    assert fmt is not None and len(fmt) >= 16 and pcm is not None
    encoding, channels, rate, byte_rate, alignment, bits = struct.unpack_from('<HHIIHH', fmt)
    if encoding == 0xFFFE:
        assert len(fmt) >= 40 and fmt[24:40] == bytes.fromhex('0100000000001000800000aa00389b71'), 'Expected extensible PCM'
        encoding = 1
    assert (encoding, channels, rate, bits) == (1, 2, 48000, 16), (encoding, channels, rate, bits)
    assert alignment == channels * bits // 8 and byte_rate == rate * alignment
    assert len(pcm) % alignment == 0
    samples = array.array('h')
    samples.frombytes(pcm)
    if sys.byteorder != 'little':
        samples.byteswap()
    frames = len(samples) // channels
    duration = frames / rate
    assert 0.90 <= duration <= 1.10, duration
    channel_rms = [math.sqrt(sum(value * value for value in samples[channel::channels]) / frames)
                   for channel in range(channels)]
    assert all(100 < rms < 10000 for rms in channel_rms), channel_rms
    # This fixture is a 440-Hz sine, so silence, random bytes and uninitialized
    # buffers cannot pass on amplitude alone. Count positive-going crossings.
    channel = samples[0::channels]
    crossings = sum(previous <= 0 < current for previous, current in zip(channel, channel[1:]))
    frequency = crossings / duration
    assert 400 <= frequency <= 480, frequency
    return {'channels': channels, 'sampleRate': rate, 'bits': bits, 'frames': frames,
            'durationSeconds': duration, 'channelRms': channel_rms, 'estimatedFrequencyHz': frequency}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime', required=True, type=Path)
    parser.add_argument('--fixture', required=True, type=Path)
    parser.add_argument('--work', required=True, type=Path)
    args = parser.parse_args()
    if sys.platform == 'darwin':
        assert all(os.environ.get(key) == value for key, value in {
            'GITHUB_ACTIONS': 'true', 'RUNNER_OS': 'macOS', 'RUNNER_ENVIRONMENT': 'github-hosted'}.items()), \
            'Mac runtime checks require a disposable GitHub-hosted runner'
    runtime, fixture, work = (path.resolve() for path in (args.runtime, args.fixture, args.work))
    assert fixture.is_file() and hashlib.sha256(fixture.read_bytes()).hexdigest() == FIXTURE_SHA256
    assert work != runtime and not work.is_relative_to(runtime)
    work.mkdir(parents=True, exist_ok=False)
    content = runtime / 'Contents' if runtime.suffix == '.app' else runtime
    bindir = content / ('MacOS' if runtime.suffix == '.app' else 'bin')
    executable = bindir / ('rvio.exe' if os.name == 'nt' else 'rvio')
    assert executable.is_file(), executable
    dll_dirs = []
    if os.name == 'nt':
        dll_dirs.append(os.add_dll_directory(str(bindir)))
        libraries = list(bindir.glob('avcodec-*.dll'))
    else:
        libraries = sorted({path.resolve() for path in (content / 'lib').rglob('libavcodec*.dylib')})
    libraries = sorted({path.resolve() for path in libraries})
    assert len(libraries) == 1, libraries
    assert libraries[0].is_relative_to(runtime), 'The codec library must belong to the packaged runtime'
    library = ctypes.CDLL(str(libraries[0]))
    library.avcodec_get_name.argtypes = [ctypes.c_int]
    library.avcodec_get_name.restype = ctypes.c_char_p
    library.avcodec_find_decoder.argtypes = [ctypes.c_int]
    library.avcodec_find_decoder.restype = ctypes.c_void_p
    assert library.avcodec_get_name(86018) == b'aac'
    decoder_available = bool(library.avcodec_find_decoder(86018))
    inventory = decoder_inventory(library, libraries[0])
    report = {'status': 'failed', 'runtime': str(runtime),
              'fixtureSha256': FIXTURE_SHA256, 'codecId': 86018, 'codecName': 'aac',
              'nativeDecoderAvailable': decoder_available, 'decoderInventory': inventory,
              'rvioSha256': hashlib.sha256(executable.read_bytes()).hexdigest(),
              'steps': []}
    environment = os.environ.copy()
    for key in ('PYTHONHOME', 'PYTHONPATH', 'PYTHONSTARTUP', 'QT_PLUGIN_PATH', 'QT_QPA_PLATFORM_PLUGIN_PATH',
                'QT_QPA_PLATFORM', 'OCIO', 'RV_INIT', 'RV_PYINIT', 'RVIO_INIT', 'RV_UI_LANGUAGE', 'MU_MODULE_PATH',
                'QML_IMPORT_PATH', 'QML2_IMPORT_PATH', 'RV_ALLOW_SITE_PYTHONHOME', 'RV_PYTHONPATH_APPEND_ONLY',
                'TWK_APP_SUPPORT_PATH', 'TWK_PROFILE_PLUGIN_PATH', 'TWK_FB_PLUGIN_PATH', 'TWK_MOVIE_PLUGIN_PATH',
                'TWK_NODE_PLUGIN_PATH', 'TWK_OUTPUT_PLUGIN_PATH', 'TWK_MEDIA_LIBRARY_PLUGIN_PATH', 'OIIO_LIBRARY_PATH',
                'DYLD_LIBRARY_PATH', 'DYLD_FRAMEWORK_PATH', 'DYLD_FALLBACK_LIBRARY_PATH', 'DYLD_FALLBACK_FRAMEWORK_PATH',
                'GH_TOKEN', 'GITHUB_TOKEN'):
        environment.pop(key, None)
    for key, name in (('APPDATA', 'appdata'), ('LOCALAPPDATA', 'localappdata'), ('TEMP', 'temp'), ('TMP', 'temp'),
                      ('RV_SUPPORT_PATH', 'support'), ('RV_CRASH_DUMPS_DIR', 'crashes')):
        destination = work / name
        destination.mkdir(exist_ok=True)
        environment[key] = str(destination)
    scripts = content / 'Resources/English.lproj' if runtime.suffix == '.app' else content / 'scripts/rv'
    environment.update(PYTHONNOUSERSITE='1', PYTHONDONTWRITEBYTECODE='1', RV_NO_CONSOLE_REDIRECT='1',
                       RVIO_INIT=str(scripts / 'rviorc.mu'))
    windows_root = Path(os.environ['SystemRoot']) if os.name == 'nt' else None
    environment['PATH'] = os.pathsep.join([str(bindir)] + ([str(windows_root / 'System32'), str(windows_root)]
                                                         if windows_root else ['/usr/bin', '/bin', '/usr/sbin', '/sbin']))

    def invoke(name, arguments):
        completed = subprocess.run([str(executable), *arguments], cwd=work, env=environment,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
        stdout, stderr = (value.decode('utf-8', errors='replace') for value in (completed.stdout, completed.stderr))
        (work / (name + '.stdout.log')).write_text(stdout, encoding='utf-8')
        (work / (name + '.stderr.log')).write_text(stderr, encoding='utf-8')
        report['steps'].append({'name': name, 'exitCode': completed.returncode,
                                'unsupportedAac': "Unsupported codec_id '86018'" in stdout + stderr})
        return completed.returncode, stdout + stderr

    try:
        audio_code, audio_log = invoke('audio', ['-noSequence', '-rthreads', '1', '-wthreads', '1', str(fixture),
                '-t', '1-24', '-audiorate', '48000', '-audiochannels', '2', '-audiocodec', 'pcm_s16le', '-o', str(work / 'decoded.wav')])
        video_code, video_log = invoke('video', ['-noSequence', '-rthreads', '1', '-wthreads', '1', str(fixture),
                '-t', '1-1', '-o', str(work / 'decoded.png')])
        assert video_code == 0, video_log[-3000:]
        png = (work / 'decoded.png').read_bytes()
        assert png[:8] == b'\x89PNG\r\n\x1a\n' and struct.unpack_from('>II', png, 16) == (64, 48)
        report['videoDecoded'] = True
        assert decoder_available, 'The installed FFmpeg AAC decoder is unavailable'
        assert audio_code == 0, audio_log[-3000:]
        assert "Unsupported codec_id '86018'" not in audio_log + video_log
        report['decodedAudio'] = pcm_wave(work / 'decoded.wav')
        assert not inventory['missing'], ('Required native software decoders unavailable', inventory['missing'])
        report['status'] = 'passed'
    finally:
        (work / 'report.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
        print(json.dumps(report, indent=2), flush=True)
        if report['status'] == 'passed':
            print('AAC_MEDIA_SMOKE_OK', flush=True)


if __name__ == '__main__':
    main()
