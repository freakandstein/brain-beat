"""
BrainBeat — Drums Only
=======================
Menghasilkan pola drum via FluidSynth berdasarkan state EEG.

CALM  → brush jazz: ride lembut, side stick pada 2&4, kick minimal
TENSE → battle drums: 16th hi-hat, kick ganda, snare punchy
"""

import time
import threading
import os
import sys
import subprocess
from dataclasses import dataclass
from typing import Optional
from collections import deque, Counter


# ── dependency check ──────────────────────────────────────────────────────────

def check_dependencies():
    missing = []
    try:
        import fluidsynth
    except ImportError:
        missing.append("pyfluidsynth")
    if missing:
        print("❌  Library berikut belum terinstall:")
        for m in missing:
            print(f"    pip install {m}")
        sys.exit(1)

check_dependencies()
import fluidsynth


# ── soundfont ────────────────────────────────────────────────────────────────

SOUNDFONT_PATHS = [
    os.path.expanduser("~/soundfonts/GeneralUser-GS.sf2"),
    os.path.expanduser("~/soundfonts/GeneralUser.sf2"),
    os.path.expanduser("~/soundfonts/FluidR3Mono_GM.sf2"),
    os.path.expanduser("~/soundfonts/default.sf2"),
    "/opt/homebrew/share/soundfonts/default.sf2",
]
SOUNDFONT_BLACKLIST  = ["VintageDreams", "vintage", "chip"]
SOUNDFONT_DOWNLOAD_URLS = [
    "https://github.com/musescore/MuseScore/raw/master/share/sound/FluidR3Mono_GM.sf2",
    "https://github.com/musescore/MuseScore/raw/main/share/sound/FluidR3Mono_GM.sf2",
]


def find_or_download_soundfont() -> str:
    for path in SOUNDFONT_PATHS:
        if os.path.exists(path):
            print(f"✅  Soundfont ditemukan: {path}")
            return path

    vintage_path = None
    try:
        result = subprocess.run(
            ["find", "/usr/local", "/opt/homebrew", "-name", "*.sf2", "-type", "f"],
            capture_output=True, text=True, timeout=5
        )
        for line in result.stdout.strip().split("\n"):
            if not line or not os.path.exists(line):
                continue
            if any(bl.lower() in line.lower() for bl in SOUNDFONT_BLACKLIST):
                vintage_path = line
                continue
            print(f"✅  Soundfont ditemukan: {line}")
            return line
    except Exception:
        pass

    sf_dir  = os.path.expanduser("~/soundfonts")
    sf_path = os.path.join(sf_dir, "FluidR3Mono_GM.sf2")
    os.makedirs(sf_dir, exist_ok=True)

    print("⬇️   Soundfont tidak ditemukan. Mencoba download...")
    for url in SOUNDFONT_DOWNLOAD_URLS:
        try:
            subprocess.run(["curl", "-L", "--max-time", "60", "-o", sf_path, url], check=True)
            if os.path.getsize(sf_path) > 100_000:
                with open(sf_path, "rb") as f:
                    if f.read(4) == b"RIFF":
                        print("✅  Download selesai.")
                        return sf_path
            if os.path.exists(sf_path):
                os.remove(sf_path)
        except Exception as e:
            print(f"    Gagal: {e}")
            if os.path.exists(sf_path):
                os.remove(sf_path)

    if vintage_path and os.path.exists(vintage_path):
        print(f"⚠️   Menggunakan fallback: {vintage_path}")
        return vintage_path

    print("❌  Tidak ada soundfont. Install: brew install fluid-synth")
    sys.exit(1)


# ── GM drum notes (channel 9) ─────────────────────────────────────────────────

DR = {
    "kick":     36,   # Bass Drum 1
    "stick":    37,   # Side Stick / Rimshot
    "snare":    38,   # Acoustic Snare
    "hihat_c":  42,   # Closed Hi-Hat
    "hihat_p":  44,   # Pedal Hi-Hat
    "hihat_o":  46,   # Open Hi-Hat
    "ride":     51,   # Ride Cymbal 1
    "crash":    49,   # Crash Cymbal
    "tom_hi":   50,   # High Tom
    "tom_mid":  47,   # Low-Mid Tom
    "tom_lo":   45,   # Low Tom
}
CH_DRUMS = 9


# ── Drum patterns (16 steps = satu bar 16th note) ─────────────────────────────

# CALM — brush jazz, lembut dan sparse (55-65 BPM)
# Ride pada setiap 8th note, side stick di beat 2 & 4, kick di beat 1 saja
CALM_RIDE   = [1,0,1,0, 1,0,1,0, 1,0,1,0, 1,0,1,0]  # tiap 8th note
CALM_STICK  = [0,0,0,0, 1,0,0,0, 0,0,0,0, 1,0,0,0]  # beat 2 & 4
CALM_KICK   = [1,0,0,0, 0,0,0,0, 0,0,0,0, 0,0,0,0]  # beat 1 saja
CALM_OHIHAT = [0,0,0,0, 0,0,0,0, 0,0,0,0, 0,0,1,0]  # "and" of 4 (aksen lembut)

# FLOW — groove mid-tempo, engaged calm (72-85 BPM)
# Hi-hat 8th seperti calm tapi snare defined di 2&4, kick lebih aktif
# Jembatan natural antara calm jazz dan tense battle drums
FLOW_HIHAT  = [1,0,1,0, 1,0,1,0, 1,0,1,0, 1,0,1,0]  # 8th note (sama dengan calm ride rhythm)
FLOW_SNARE  = [0,0,0,0, 1,0,0,0, 0,0,0,0, 1,0,0,0]  # snare solid beat 2 & 4
FLOW_KICK   = [1,0,0,0, 0,0,1,0, 1,0,0,0, 0,0,1,0]  # beat 1 + "and" of 2 & 3
FLOW_OHIHAT = [0,0,0,0, 0,0,0,1, 0,0,0,0, 0,0,0,1]  # open hi-hat "and" of 4 (groove accent)

# TENSE — battle drums, relentless (100-130 BPM)
# 16th hi-hat constant, kick ganda, snare punchy dengan ghost
TENSE_HIHAT = [1,1,1,1, 1,1,1,1, 1,1,1,1, 1,1,1,1]  # 16th constant
TENSE_KICK  = [1,0,0,0, 1,0,1,0, 1,0,0,0, 1,0,1,0]  # 4-on-floor + extra
TENSE_SNARE = [0,0,0,0, 1,0,0,1, 0,0,0,0, 1,0,0,1]  # beat 2&4 + ghost offbeat
TENSE_OHIHAT= [0,0,0,0, 0,0,1,0, 0,0,0,0, 0,0,1,0]  # open hi-hat aksen

# STRESS (tense_level > 0.65) — lebih intens, dengan tom fill
STRESS_HIHAT = [1,1,1,1, 1,1,1,1, 1,1,1,1, 1,1,1,1]
STRESS_KICK  = [1,0,1,0, 1,0,1,0, 1,0,1,0, 1,0,1,0]  # double-time kick
STRESS_SNARE = [0,0,0,0, 1,0,1,0, 0,0,0,1, 1,0,1,0]  # lebih banyak ghost
STRESS_CRASH = [1,0,0,0, 0,0,0,0, 0,0,0,0, 0,0,0,0]  # crash di bar start


# ── EEG state ─────────────────────────────────────────────────────────────────

@dataclass
class EEGState:
    alpha: float = 0.5
    beta:  float = 0.3
    theta: float = 0.2
    tbr:     float = 0.5
    tbr_raw: float = 1.0
    frontal_alpha: float = 0.5
    frontal_theta: float = 0.5

    def arousal(self) -> float:
        return 0.50 * self.beta - 0.30 * self.alpha - 0.20 * self.tbr

    def flow_score(self) -> float:
        """
        Flow index dari frontal EEG: alpha + theta − beta (semua frontal AF7/AF8).
        Flow state = engaged calm: alpha/theta frontal tinggi, beta rendah.
        Range ~−1 .. +1, di-clip ke 0..1 setelah normalisasi di engine.
        """
        return self.frontal_alpha + self.frontal_theta - self.beta

    def mental_state(self, threshold: float = -0.05) -> str:
        if self.beta < 0.22:
            return "calm"
        return "tense" if self.arousal() > threshold else "calm"

    def clamp(self):
        self.alpha = max(0.0, min(1.0, self.alpha))
        self.beta  = max(0.0, min(1.0, self.beta))
        self.theta = max(0.0, min(1.0, self.theta))
        self.tbr   = max(0.0, min(1.0, self.tbr))
        self.frontal_alpha = max(0.0, min(1.0, self.frontal_alpha))
        self.frontal_theta = max(0.0, min(1.0, self.frontal_theta))


# ── BrainBeat engine ──────────────────────────────────────────────────────────

class MusicEngine:
    def __init__(self, soundfont_path: str):
        self.sf_path   = soundfont_path
        self.eeg       = EEGState()
        self._lock     = threading.Lock()
        self._running  = False
        self._tick     = 0
        self._bpm      = 62.0
        self._tense_level = 0.0
        self._prev_state  = None
        # Vote buffer 20 tick (~5 detik): butuh 70% supermajority untuk switch state
        # Supaya tidak flip-flop saat sinyal di borderline
        self._state_votes = deque(maxlen=20)

        # Adaptive threshold — rolling buffer 120 detik (4 Hz × 120 = 480 samples)
        # Warm-up diperpendek ke 30 detik supaya adaptasi lebih cepat
        self._arousal_buf   = deque(maxlen=480)
        self._adaptive_threshold = 0.02    # default lebih tinggi — bias ke calm
        self._warmup_ticks  = 60           # 15 detik × 4 tick/detik

        self.fs = fluidsynth.Synth(gain=0.8, samplerate=44100.0)
        self.fs.start(driver="coreaudio")
        self.sfid = self.fs.sfload(soundfont_path)
        if self.sfid == -1:
            raise RuntimeError(f"Gagal load soundfont: {soundfont_path}")

        # Aktifkan channel 9 sebagai drum kit (bank 128, preset 0)
        self.fs.program_select(CH_DRUMS, self.sfid, 128, 0)

        # Reverb hangat untuk drums
        self.fs.set_reverb(roomsize=0.5, damping=0.6, width=0.7, level=0.4)
        self.fs.set_chorus(0, 0.0, 0.3, 0.5, 0)
        print("✅  BrainBeat siap.")

    # ── public API ──────────────────────────────────────────────────────────

    def set_eeg(self, alpha: float, beta: float, theta: float,
                tbr: float = 0.5, tbr_raw: float = 1.0,
                frontal_alpha: float = 0.5, frontal_theta: float = 0.5):
        with self._lock:
            self.eeg.alpha         = alpha
            self.eeg.beta          = beta
            self.eeg.theta         = theta
            self.eeg.tbr           = tbr
            self.eeg.tbr_raw       = tbr_raw
            self.eeg.frontal_alpha = frontal_alpha
            self.eeg.frontal_theta = frontal_theta
            self.eeg.clamp()

    def get_arousal(self) -> float:
        return round(self.eeg.arousal(), 4)

    def get_confidence(self) -> float:
        diff = abs(self.get_arousal() - self._adaptive_threshold)
        return min(1.0, diff / 0.08)

    def get_threshold(self) -> float:
        return self._adaptive_threshold

    def is_warming_up(self) -> bool:
        return self._tick < self._warmup_ticks

    def get_consistency(self) -> float:
        if not self._state_votes:
            return 0.0
        top = Counter(self._state_votes).most_common(1)[0][1]
        return round(top / len(self._state_votes), 3)

    def get_flow_score(self) -> float:
        """
        Flow score 0..1: 1.0 = deep flow, 0.0 = tidak ada flow.
        Raw range flow_score() adalah ~−1..+1 (alpha+theta−beta, masing-masing 0..1).
        Di-map ke 0..1: score 0 = −1, score 1 = +1.
        """
        raw = self.eeg.flow_score()   # range ~−1 .. +1
        return round(float(max(0.0, min(1.0, (raw + 1.0) / 2.0))), 3)

    def get_spectrum_position(self) -> float:
        """
        Posisi di spektrum calm←→tense sebagai 0..1.
        0.0 = murni calm, 0.5 = flow zone, 1.0 = murni tense.
        Diturunkan dari arousal relatif terhadap threshold:
          arousal jauh di bawah threshold → mendekati 0 (calm)
          arousal di sekitar threshold    → mendekati 0.5 (flow zone)
          arousal jauh di atas threshold  → mendekati 1 (tense)
        """
        arousal   = self.eeg.arousal()
        threshold = self._adaptive_threshold
        # Normalisasi: delta ±0.15 = full swing dari center
        delta = (arousal - threshold) / 0.15
        return round(float(max(0.0, min(1.0, (delta + 1.0) / 2.0))), 3)

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread  = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        print("▶  BrainBeat mulai...")

    def stop(self):
        self._running = False
        time.sleep(0.05)   # beri waktu tick terakhir selesai
        self._all_notes_off()
        try:
            self.fs.system_reset()   # matikan semua suara aktif di FluidSynth
        except Exception:
            pass
        print("■  BrainBeat berhenti.")

    # ── main loop ───────────────────────────────────────────────────────────

    def _loop(self):
        while self._running:
            try:
                with self._lock:
                    eeg = EEGState(
                        alpha          = self.eeg.alpha,
                        beta           = self.eeg.beta,
                        theta          = self.eeg.theta,
                        tbr            = self.eeg.tbr,
                        tbr_raw        = self.eeg.tbr_raw,
                        frontal_alpha  = self.eeg.frontal_alpha,
                        frontal_theta  = self.eeg.frontal_theta,
                    )

                # Feed arousal ke adaptive buffer
                self._arousal_buf.append(eeg.arousal())

                # Update threshold setiap 120 tick (~30 detik) setelah warm-up
                if (self._tick >= self._warmup_ticks
                        and self._tick % 120 == 0
                        and len(self._arousal_buf) >= 60):
                    import statistics
                    median = statistics.median(self._arousal_buf)
                    self._adaptive_threshold = round(median + 0.03, 4)
                    print(f"  ⚙  Adaptive threshold → {self._adaptive_threshold:.4f}")

                # Vote buffer — 3-state: calm / flow / tense
                # spectrum_pos: 0..0.35 = calm, 0.35..0.65 = flow, 0.65..1 = tense
                sp = self.get_spectrum_position()
                if sp > 0.65:
                    raw = "tense"
                elif sp >= 0.35:
                    raw = "flow"
                else:
                    raw = "calm"
                self._state_votes.append(raw)
                total  = len(self._state_votes)
                counts = Counter(self._state_votes)
                cur = self._prev_state or "calm"
                # Butuh 70% supermajority untuk switch state — mencegah flip-flop
                best = counts.most_common(1)[0]
                state = best[0] if best[1] >= max(1, int(total * 0.70)) else cur

                if state != self._prev_state and self._prev_state is not None:
                    self._on_state_change(self._prev_state, state)
                self._prev_state = state

                self._update_build(state)
                self._update_bpm(state, eeg)
                self._tick_drums(state)

                time.sleep((60.0 / self._bpm) / 4.0)
                self._tick += 1

            except Exception as e:
                print(f"⚠️  Loop error (tick {self._tick}): {e}")
                time.sleep(0.1)

    def _tick_drums(self, state: str):
        t   = self._tick
        lvl = self._tense_level
        s   = t % 16  # step dalam bar

        if state == "calm":
            # ── CALM: brush jazz ─────────────────────────────────────────
            fade = min(1.0, (t / 16.0))

            if CALM_RIDE[s]:
                vel = int((65 if s % 8 == 0 else 48) * fade)
                self._hit(DR["ride"], max(40, vel), 0.04)

            if CALM_STICK[s]:
                vel = int(58 * fade)
                self._hit(DR["stick"], max(40, vel), 0.04)

            if CALM_KICK[s]:
                vel = int(62 * fade)
                self._hit(DR["kick"], max(40, vel), 0.06)

            if CALM_OHIHAT[s]:
                vel = int(45 * fade)
                self._hit(DR["hihat_o"], max(35, vel), 0.08)

        elif state == "flow":
            # ── FLOW: groove mid-tempo ────────────────────────────────────
            fade = min(1.0, (t / 16.0))

            if FLOW_HIHAT[s]:
                # Aksen di downbeat, lebih soft di offbeat
                vel = int((72 if s % 8 == 0 else 55) * fade)
                self._hit(DR["hihat_c"], max(42, vel), 0.03)

            if FLOW_SNARE[s]:
                vel = int(70 * fade)
                self._hit(DR["snare"], max(48, vel), 0.05)

            if FLOW_KICK[s]:
                vel = int(72 * fade)
                self._hit(DR["kick"], max(48, vel), 0.06)

            if FLOW_OHIHAT[s]:
                vel = int(52 * fade)
                self._hit(DR["hihat_o"], max(38, vel), 0.07)

        else:
            # ── TENSE: battle drums ───────────────────────────────────────
            if lvl > 0.65:
                kick_p  = STRESS_KICK
                snare_p = STRESS_SNARE
                hihat_p = STRESS_HIHAT
                crash_p = STRESS_CRASH
                use_crash = True
            else:
                kick_p  = TENSE_KICK
                snare_p = TENSE_SNARE
                hihat_p = TENSE_HIHAT
                crash_p = [0] * 16
                use_crash = False

            if hihat_p[s]:
                # Aksen di downbeat, ghost di 16th
                vel = int(70 + lvl * 25) if s % 4 == 0 else int(45 + lvl * 20)
                self._hit(DR["hihat_c"], vel, 0.02)

            if kick_p[s]:
                vel = int(82 + lvl * 28)
                self._hit(DR["kick"], min(127, vel), 0.07)

            if snare_p[s]:
                # Ghost note (offbeat) lebih pelan
                is_ghost = (s % 4 != 0) and snare_p[s]
                vel = int(55 + lvl * 30) if is_ghost else int(75 + lvl * 30)
                self._hit(DR["snare"], min(127, vel), 0.05)

            if use_crash and crash_p[s] and (t % 16 == 0):
                vel = int(80 + lvl * 20)
                self._hit(DR["crash"], min(127, vel), 0.08)

            # Open hi-hat aksen
            if TENSE_OHIHAT[s]:
                vel = int(55 + lvl * 25)
                self._hit(DR["hihat_o"], vel, 0.06)

    # ── helpers ─────────────────────────────────────────────────────────────

    def _hit(self, note: int, vel: int, dur: float):
        if vel <= 0 or not self._running:
            return
        self.fs.noteon(CH_DRUMS, note, vel)
        threading.Timer(dur, lambda: self.fs.noteoff(CH_DRUMS, note)).start()

    def _update_build(self, state: str):
        if state == "tense":
            self._tense_level = min(1.0, self._tense_level + 0.006)
        else:
            self._tense_level = max(0.0, self._tense_level - 0.004)

    def _update_bpm(self, state: str, eeg: EEGState):
        if state == "calm":
            target = 55 + eeg.alpha * 10          # 55–65 BPM
        elif state == "flow":
            target = 72 + eeg.frontal_alpha * 13  # 72–85 BPM
        else:
            target = 95 + self._tense_level * 40  # 95–135 BPM
        self._bpm += (target - self._bpm) * 0.04   # smooth glide

    def _on_state_change(self, prev: str, curr: str):
        print(f"  → {prev} → {curr}")
        # Cut drums saat transisi supaya tidak bleeding
        self._all_notes_off()
        # Snap 60% BPM ke target baru
        if curr == "tense":
            snap = 95 + self._tense_level * 40
        elif curr == "flow":
            snap = 78.0
        else:
            snap = 62.0
        self._bpm += (snap - self._bpm) * 0.60
        # Update reverb — flow di tengah antara calm dan tense
        if curr == "calm":
            self.fs.set_reverb(roomsize=0.60, damping=0.55, width=0.8, level=0.45)
        elif curr == "flow":
            self.fs.set_reverb(roomsize=0.40, damping=0.65, width=0.65, level=0.35)
        else:
            self.fs.set_reverb(roomsize=0.25, damping=0.75, width=0.5, level=0.25)

    def _all_notes_off(self, channels: Optional[list] = None):
        chs = channels if channels else [CH_DRUMS]
        for ch in chs:
            self.fs.cc(ch, 123, 0)   # MIDI CC 123 = All Notes Off — lebih cepat dari loop 128 noteoff

    def __del__(self):
        try:
            self.stop()
            self.fs.delete()
        except Exception:
            pass


# ── standalone mode ───────────────────────────────────────────────────────────

def main():
    import sys, tty, termios, select
    sf = find_or_download_soundfont()
    eng = MusicEngine(sf)
    eng.start()

    def get_key():
        fd  = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            r, _, _ = select.select([sys.stdin], [], [], 0.05)
            return sys.stdin.read(1) if r else None
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)

    print("Preset: 1=calm  2=tense  q=quit")
    try:
        while True:
            k = get_key()
            if k == "q":
                break
            elif k == "1":
                eng.set_eeg(0.80, 0.15, 0.20, tbr=0.65)
            elif k == "2":
                eng.set_eeg(0.20, 0.75, 0.30, tbr=0.30)
    finally:
        eng.stop()


if __name__ == "__main__":
    main()
