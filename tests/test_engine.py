"""Behavior checks; run with python -m unittest discover -s tests -v."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

import numpy as np
import soundfile as sf

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from engine import Matcher


class MatcherTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.rng = np.random.default_rng(52)
        self.rate = 48000
        time = np.arange(19200) / self.rate
        envelope = np.sin(np.linspace(0, np.pi, len(time))) ** 2
        self.sound = (np.cos(2*np.pi*(420*time + .5*(2600-420)/time[-1]*time**2)) * envelope * .5).astype(np.float32)
        self.other = (np.cos(2*np.pi*(3400*time + .5*(900-3400)/time[-1]*time**2)) * envelope * .5).astype(np.float32)
        refs = []
        for group, samples in ((1, self.sound), (2, self.other)):
            path = Path(self.folder.name) / f"{group}.wav"
            sf.write(path, samples, self.rate, subtype="FLOAT")
            refs.append({"group_id": group, "kind": "pickup", "path": str(path)})
        self.matcher = Matcher(refs)

    def test_shift_gain_noise_and_stereo(self):
        window = self.rng.normal(0, .004, self.rate * 3).astype(np.float32)
        offset = int(.713 * self.rate)
        window[offset:offset+len(self.sound)] += .28 * self.sound
        stereo = np.column_stack((window, window * .8))
        result = self.matcher.match(stereo, self.rate)
        self.assertEqual(result[0]["group_id"], 1)
        self.assertGreater(result[0]["score"], .9)
        self.assertLess(abs(result[0]["offset_seconds"] - .713), .1)

    def test_resampling(self):
        clip = np.interp(np.arange(round(len(self.other) * 44100 / 48000)) * 48000 / 44100,
                         np.arange(len(self.other)), self.other)
        window = np.pad(clip, (15000, 24000))
        self.assertEqual(self.matcher.match(window, 44100)[0]["group_id"], 2)

    def test_unknown_silence_noise_impulse(self):
        self.assertEqual(self.matcher.match(np.zeros(48000), 48000), [])
        self.assertEqual(self.matcher.match(self.rng.normal(0, .2, 96000), 48000), [])
        impulse = np.zeros(96000)
        impulse[30000] = 1
        self.assertEqual(self.matcher.match(impulse, 48000), [])

    def test_two_events_remain_candidates(self):
        window = np.zeros(144000, dtype=np.float32)
        window[18000:18000+len(self.sound)] = self.sound
        window[80000:80000+len(self.other)] = self.other
        self.assertEqual({r["group_id"] for r in self.matcher.match(window, 48000)}, {1, 2})

    def test_partial_event_and_missing_reference(self):
        self.assertEqual(self.matcher.match(self.sound[:1000], 48000), [])
        bad = Matcher([{"group_id": 1, "kind": "pickup", "path": "missing-no-such-reference.ogg"}])
        self.assertEqual(len(bad.errors), 1)
        self.assertEqual(bad.match(self.sound, 48000), [])


class CatalogReferenceTests(unittest.TestCase):
    def test_all_available_reference_clips(self):
        base = Path(__file__).resolve().parents[1] / "data"
        if not (base / "catalog.json").exists():
            self.skipTest("Catalog not downloaded")
        catalog = json.loads((base / "catalog.json").read_text(encoding="utf-8"))
        refs = [{"group_id": row["group_id"], "kind": kind, "path": str(base / row[kind])}
                for row in catalog["sounds"] for kind in ("pickup", "drop")]
        matcher = Matcher(refs)
        self.assertEqual(matcher.errors, [])
        for ref in refs:
            with self.subTest(group=ref["group_id"], kind=ref["kind"]):
                audio, rate = sf.read(ref["path"], dtype="float32")
                if audio.ndim > 1:
                    audio = audio.mean(axis=1)
                audio = np.pad(audio * .35, (int(rate * .311), int(rate * .17)))
                result = matcher.match(audio, int(rate))
                self.assertTrue(result)
                # Identical clips across groups legitimately tie. Require the
                # expected group among the best-scoring acoustic candidates.
                own = next((r for r in result if r["group_id"] == ref["group_id"]), None)
                self.assertIsNotNone(own)
                self.assertGreater(own["score"], .9)
                self.assertLess(result[0]["score"] - own["score"], .02)

    def test_catalog_with_noise_gain_and_fractional_sample_offset(self):
        base = Path(__file__).resolve().parents[1] / "data"
        if not (base / "catalog.json").exists():
            self.skipTest("Catalog not downloaded")
        catalog = json.loads((base / "catalog.json").read_text(encoding="utf-8"))
        refs = [{"group_id": row["group_id"], "kind": kind, "path": str(base / row[kind])}
                for row in catalog["sounds"] for kind in ("pickup", "drop")]
        matcher = Matcher(refs)
        random = np.random.default_rng(713)
        for ref in refs:
            with self.subTest(group=ref["group_id"], kind=ref["kind"]):
                clip, rate = sf.read(ref["path"], dtype="float32", always_2d=True)
                clip = clip.mean(axis=1) * .27
                # 10 dB signal-to-noise ratio within the clip. A 15025-sample
                # delay is not an integer after conversion from 48 to 16 kHz.
                noise_rms = np.sqrt(np.mean(clip ** 2)) * 10 ** (-10 / 20)
                window = random.normal(0, noise_rms, int(rate * 3)).astype(np.float32)
                window[15025:15025 + len(clip)] += clip
                results = matcher.match(window, int(rate))
                self.assertTrue(results)
                # Different game events may share byte-identical references
                # (Necklace / GoldChain). Their acoustic scores legitimately tie.
                own = next((result for result in results if result["group_id"] == ref["group_id"]), None)
                self.assertIsNotNone(own)
                self.assertLess(results[0]["score"] - own["score"], .02)
        for _ in range(8):
            self.assertEqual(matcher.match(random.normal(0, .03, 48000 * 3), 48000), [])


if __name__ == "__main__":
    unittest.main()
