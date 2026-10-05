"""Distribution, mix and source isolation tests; Python standard library only."""
import array
import hashlib
import json
from pathlib import Path
import unittest
import wave

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = json.loads((ROOT / 'animation_manifest.json').read_text(encoding='utf-8'))


class AssetTests(unittest.TestCase):
    def test_browser_assets_use_valid_utf8(self):
        for path in [ROOT / 'animation_manifest.json', *ROOT.glob('web/*')]:
            if path.is_file():
                text = path.read_bytes().decode('utf-8', errors='strict')
                self.assertNotIn('\ufffd', text, path.name)

    def test_every_sound_is_valid_stereo_pcm_with_headroom(self):
        for filename in MANIFEST['sounds'].values():
            with self.subTest(file=filename), wave.open(str(ROOT / filename)) as wav:
                self.assertEqual((wav.getnchannels(), wav.getsampwidth(), wav.getframerate()), (2, 2, 48000))
                samples = array.array('h', wav.readframes(wav.getnframes()))
                peak = max(abs(x) for x in samples)
                self.assertGreater(peak, 1000); self.assertLess(peak, 20000)
                self.assertLess(abs(sum(samples) / len(samples)), 2)

    def test_periodic_hum_loops_have_no_seam_click(self):
        for name in ['ambient_hum', 'charged_hum', 'stable_hum', 'emergency_idle_hum']:
            with wave.open(str(ROOT / 'audio' / f'{name}.wav')) as wav:
                data = array.array('h', wav.readframes(wav.getnframes()))
                for channel in [0, 1]:
                    # A periodic high harmonic legitimately changes many PCM units
                    # per sample. Compare the wrap slope to neighboring slopes,
                    # rather than imposing an amplitude-dependent fixed threshold.
                    near = [abs(data[i + 2] - data[i]) for i in range(channel, 128, 2)]
                    near += [abs(data[i + 2] - data[i]) for i in range(len(data) - 130 + channel, len(data) - 2, 2)]
                    self.assertLessEqual(abs(data[channel] - data[-2 + channel]), max(near) * 1.05 + 2)

    def test_asset_manifest_hashes_and_mix_headroom(self):
        report = json.loads((ROOT / 'audio/mix_report.json').read_text())
        self.assertLess(report['mix_peak_at_volume_1'], .86)
        self.assertLess(report['mix_peak_default_dbfs'], -6)
        for name, data in report['stems'].items():
            self.assertEqual(hashlib.sha256((ROOT / 'audio' / f'{name}.wav').read_bytes()).hexdigest(), data['sha256'])

    def test_production_art_is_byte_identical(self):
        production = ROOT.parents[1] / 'desktop/ChatNexus.Desktop/nexus-core-splash.png'
        self.assertEqual((ROOT / 'assets/nexus-core-splash.png').read_bytes(), production.read_bytes())

    def test_host_is_isolated_and_ships_static_fallback(self):
        project = (ROOT / 'CoreUnlockSplash.csproj').read_text()
        self.assertNotIn('ProjectReference', project)
        self.assertIn('audio\\*.wav', project)
        host = (ROOT / 'Program.cs').read_text()
        self.assertIn('ProcessFailed', host); self.assertIn('ShowStatic', host)
        self.assertNotIn('Process.Start(', host)

    def test_failure_mix_has_headroom_and_is_not_louder_than_normal(self):
        failure = json.loads((ROOT / 'audio/failure_mix_report.json').read_text())
        normal = json.loads((ROOT / 'audio/mix_report.json').read_text())
        self.assertLess(failure['failure_peak_at_volume_1'], normal['mix_peak_at_volume_1'])
        self.assertLess(max(failure['interruption_peaks_at_volume_1'].values()), .86)
        self.assertLessEqual(max(failure['interruption_peaks_at_volume_1'].values()), normal['mix_peak_at_volume_1'] * 1.05)


if __name__ == '__main__':
    unittest.main()
