"""Offline profile compatibility and false-positive checks; no real decoder claims."""
from __future__ import annotations

import array
import importlib.util
import math
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest import mock
import wave

SCRIPT = Path(__file__).resolve().parents[1] / 'aac_runtime_smoke.py'
spec = importlib.util.spec_from_file_location('aac_runtime_smoke', SCRIPT)
smoke = importlib.util.module_from_spec(spec)
spec.loader.exec_module(smoke)


class DecoderProfileTests(unittest.TestCase):
    def inventory(self, major, unavailable=()):
        library = types.SimpleNamespace(
            avcodec_version=mock.Mock(return_value=(major << 16) | (11 << 8) | 100),
            avcodec_find_decoder_by_name=mock.Mock(side_effect=lambda name: None if name.decode('ascii') in unavailable else 1))
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'fixture-library'
            path.write_bytes(b'Offline inventory contract fixture; not a Mach-O or PE library')
            result = smoke.decoder_inventory(library, path)
        return result, library

    def test_ffmpeg8_requires_all_nineteen_native_decoders(self):
        result, _ = self.inventory(62)
        self.assertEqual(result['libavcodecVersion'], '62.11.100')
        self.assertEqual(len(result['decoders']), 19)
        self.assertIn('prores_raw', [item['name'] for item in result['decoders']])
        self.assertEqual(result['missing'], [])

    def test_ffmpeg6_and_7_do_not_require_nonexistent_prores_raw(self):
        for major in (60, 61):
            result, library = self.inventory(major)
            with self.subTest(major=major):
                self.assertEqual(len(result['decoders']), 18)
                self.assertEqual(result['missing'], [])
                self.assertNotIn(mock.call(b'prores_raw'), library.avcodec_find_decoder_by_name.call_args_list)

    def test_missing_decoder_is_reported_even_if_other_queries_pass(self):
        result, _ = self.inventory(62, ('aac_latm', 'vp9'))
        self.assertEqual(result['missing'], ['aac_latm', 'vp9'])
        self.assertEqual(sum(item['available'] for item in result['decoders']), 17)

    def test_older_abi_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, 'FFmpeg 6'):
            self.inventory(59)


class AudioSignalTests(unittest.TestCase):
    def wave(self, folder, frequency=440, rate=48000, amplitude=2000):
        path = Path(folder) / 'synthesized.wav'
        samples = array.array('h')
        for frame in range(rate):
            value = round(amplitude * math.sin(frame * frequency * 2 * math.pi / rate))
            samples.extend((value, value))
        if sys.byteorder != 'little':
            samples.byteswap()
        with wave.open(str(path), 'wb') as output:
            output.setparams((2, 2, rate, rate, 'NONE', 'not compressed'))
            output.writeframes(samples.tobytes())
        return path

    def test_expected_non_silent_stereo_tone_passes(self):
        with tempfile.TemporaryDirectory() as folder:
            result = smoke.pcm_wave(self.wave(folder))
        self.assertEqual(result['channels'], 2)
        self.assertEqual(result['frames'], 48000)
        self.assertAlmostEqual(result['estimatedFrequencyHz'], 440, delta=2)

    def test_silence_wrong_rate_and_wrong_tone_cannot_pass(self):
        for options in ({'amplitude': 0}, {'rate': 44100}, {'frequency': 1000}):
            with self.subTest(options=options), tempfile.TemporaryDirectory() as folder:
                with self.assertRaises(AssertionError):
                    smoke.pcm_wave(self.wave(folder, **options))

    def test_local_mac_is_rejected_before_library_or_work_creation(self):
        with tempfile.TemporaryDirectory() as folder:
            work = Path(folder) / 'must-not-exist'
            with mock.patch.object(smoke.sys, 'platform', 'darwin'), mock.patch.dict(os.environ, {}, clear=True), \
                    mock.patch.object(sys, 'argv', ['aac_runtime_smoke.py', '--runtime', folder, '--fixture', folder, '--work', str(work)]), \
                    mock.patch.object(smoke.ctypes, 'CDLL') as load:
                with self.assertRaisesRegex(AssertionError, 'GitHub-hosted'):
                    smoke.main()
            self.assertFalse(work.exists())
            load.assert_not_called()


if __name__ == '__main__':
    unittest.main()
