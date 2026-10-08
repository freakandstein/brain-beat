"""
Tes: zona tengah calm<->tense dinamai "neutral" (dulu "flow") dan engine
memperlakukannya sebagai zona tengah, bukan tense.

Risiko rename yang setengah jalan: _update_bpm, _tick_drums, dan
_on_state_change membedakan state lewat `elif state == ...` dengan `else` =
tense, jadi string state yang tidak dikenal DIAM-DIAM dimainkan sebagai tense
(BPM 95-135, pola battle drums). Tes di sini mengunci itu.

FluidSynth diganti FakeSynth karena MusicEngine.__init__ asli membuka device
CoreAudio; semua logika engine lainnya asli.

Jalankan dari root project:
    python3 -m unittest discover tests
"""

import contextlib
import io
import unittest
from unittest import mock

try:
    with contextlib.redirect_stdout(io.StringIO()):
        import eeg_engine
        import eeg_server
except (ImportError, SystemExit) as e:   # eeg_engine sys.exit(1) kalau pyfluidsynth tidak ada
    raise unittest.SkipTest(f"dependency engine/server belum terinstall: {e}")


class FakeSynth:
    """Pengganti fluidsynth.Synth: mencatat panggilan, tanpa membuka device audio."""

    def __init__(self, *args, **kwargs):
        self.calls = []

    def start(self, **kwargs): pass
    def sfload(self, path): return 0
    def program_select(self, *args): pass
    def set_chorus(self, *args): pass
    def system_reset(self): pass
    def delete(self): pass
    def set_reverb(self, **kwargs): self.calls.append(("reverb", kwargs))
    def cc(self, *args): self.calls.append(("cc",) + args)
    def noteon(self, ch, note, vel): self.calls.append(("on", note, vel))
    def noteoff(self, ch, note): self.calls.append(("off", note))


class QuietEngine(eeg_engine.MusicEngine):
    def __del__(self):   # MusicEngine.__del__ -> stop() mencetak & tidur; tidak perlu untuk FakeSynth
        pass


def make_engine():
    with mock.patch.object(eeg_engine.fluidsynth, "Synth", FakeSynth), \
            contextlib.redirect_stdout(io.StringIO()):
        engine = QuietEngine("unused.sf2")
    engine._running = True   # start() yang mengisi ini; _hit() mengabaikan nada kalau tidak running
    return engine


class NeutralZoneEngineTest(unittest.TestCase):
    def setUp(self):
        self.engine = make_engine()
        self.addCleanup(setattr, self.engine, "_running", False)

    def noteons(self):
        return [(c[1], c[2]) for c in self.engine.fs.calls if c[0] == "on"]

    def test_bpm_target_per_state(self):
        # EEGState default: alpha=0.5, frontal_alpha=0.5, tense_level=0 ->
        # calm 55+0.5*10, neutral 72+0.5*13, tense 95+0*40
        expected = {"calm": 60.0, "neutral": 78.5, "tense": 95.0}
        for state, target in expected.items():
            with self.subTest(state=state):
                self.engine._bpm = 62.0
                for _ in range(300):
                    self.engine._update_bpm(state, self.engine.eeg)
                self.assertAlmostEqual(self.engine._bpm, target, delta=0.5)

    def test_neutral_plays_the_mid_tempo_groove(self):
        # tick 16 = langkah 0 dengan fade penuh. Groove: hi-hat tertutup + kick,
        # keduanya velocity 72. Pola tense di langkah yang sama: hi-hat 70, kick 82.
        self.engine._tick = 16
        self.engine._tick_drums("neutral")
        self.assertEqual(sorted(self.noteons()), [(36, 72), (42, 72)])

    def test_neutral_is_silent_where_the_groove_rests(self):
        # Langkah 1: groove tidak punya hit; pola tense punya hi-hat 16th di setiap langkah.
        self.engine._tick = 17
        self.engine._tick_drums("neutral")
        self.assertEqual(self.noteons(), [])

    def test_switching_to_neutral_uses_the_mid_tempo_snap_and_reverb(self):
        self.engine._bpm = 62.0
        self.engine.fs.calls.clear()
        with contextlib.redirect_stdout(io.StringIO()):
            self.engine._on_state_change("calm", "neutral")
        self.assertAlmostEqual(self.engine._bpm, 71.6, places=6)   # 62 + (78 - 62) * 0.60
        reverbs = [c[1] for c in self.engine.fs.calls if c[0] == "reverb"]
        self.assertEqual(reverbs, [dict(roomsize=0.40, damping=0.65, width=0.65, level=0.35)])

    def test_loop_labels_the_middle_zone_neutral(self):
        e = self.engine
        # arousal == threshold -> spectrum_pos tepat 0.5 (tengah zona tengah)
        e.eeg.alpha, e.eeg.beta, e.eeg.tbr = 0.5, 0.4, 0.5
        e._adaptive_threshold = e.eeg.arousal()
        e._spectrum_pos_smooth = 0.5
        sleeps = []

        def fake_sleep(seconds):
            sleeps.append(seconds)
            if len(sleeps) >= 3:
                e._running = False

        with mock.patch.object(eeg_engine.time, "sleep", fake_sleep), \
                contextlib.redirect_stdout(io.StringIO()):
            e._loop()
        self.assertEqual(e._prev_state, "neutral")


class NeutralZoneServerTest(unittest.TestCase):
    class StubEngine:
        def __init__(self, spectrum_pos):
            self.spectrum_pos = spectrum_pos

        def get_spectrum_position(self):
            return self.spectrum_pos

    def reset_debounce(self):
        eeg_server._stable_state = "calm"
        eeg_server._pending_state = None
        eeg_server._pending_state_count = 0

    def setUp(self):
        self.reset_debounce()
        self.addCleanup(self.reset_debounce)

    def test_zone_boundaries(self):
        # Batas zona dihitung tangan dari kode: <0.35 calm, 0.35..0.65 (inklusif) tengah, >0.65 tense.
        cases = [(0.20, "calm"), (0.349, "calm"), (0.35, "neutral"), (0.50, "neutral"),
                 (0.65, "neutral"), (0.651, "tense"), (0.80, "tense")]
        for spectrum_pos, expected in cases:
            with self.subTest(spectrum_pos=spectrum_pos):
                self.reset_debounce()
                engine = self.StubEngine(spectrum_pos)
                labels = [eeg_server._debounced_mental_state(engine) for _ in range(10)]
                self.assertEqual(labels[-1], expected)
                self.assertNotIn("flow", labels)


if __name__ == "__main__":
    unittest.main()
