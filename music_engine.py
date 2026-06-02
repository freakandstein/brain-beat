"""
EEG Music Engine
================
Standalone music engine yang menerima nilai EEG (alpha/beta/theta)
dan menghasilkan musik generatif via FluidSynth + Soundfont.

Penggunaan:
    python music_engine.py

Kontrol keyboard (saat berjalan):
    1  →  preset: calm
    2  →  preset: tense
    q  →  quit
    +/-  →  naik/turun alpha
    w/s  →  naik/turun beta
    e/d  →  naik/turun theta

Nanti saat EEG terhubung, ganti bagian INPUT SECTION
dengan nilai dari BrainFlow secara realtime.
"""

import time
import threading
import random
import sys
import math
import os
import subprocess
from dataclasses import dataclass, field
from typing import Optional
from collections import deque, Counter

# ── dependency check ─────────────────────────────────────────────────────────

def check_dependencies():
    missing = []
    try:
        import fluidsynth
    except ImportError:
        missing.append("pyfluidsynth")
    try:
        import numpy
    except ImportError:
        missing.append("numpy")
    if missing:
        print("❌  Library berikut belum terinstall:")
        for m in missing:
            print(f"    pip install {m}")
        print()
        print("Juga pastikan FluidSynth sudah terinstall:")
        print("    brew install fluid-synth")
        sys.exit(1)

check_dependencies()

import fluidsynth
import numpy as np

# ── soundfont path ────────────────────────────────────────────────────────────

# Path default soundfont di macOS via Homebrew
# Kalau tidak ada, script akan coba download GeneralUser GS
SOUNDFONT_PATHS = [
    os.path.expanduser("~/soundfonts/GeneralUser.sf2"),
    os.path.expanduser("~/soundfonts/FluidR3Mono_GM.sf2"),
    os.path.expanduser("~/soundfonts/default.sf2"),
    "/usr/local/share/sounds/sf2/default.sf2",
    "/opt/homebrew/share/soundfonts/default.sf2",
]

# Soundfont yang dilewati (synthesizer/chip-tune, bukan GM orkestra)
SOUNDFONT_BLACKLIST = ["VintageDreams", "vintage", "chip"]

# Beberapa URL fallback untuk download GM soundfont yang proper
SOUNDFONT_DOWNLOAD_URLS = [
    # FluidR3Mono GM dari MuseScore (~4MB, kualitas baik)
    "https://github.com/musescore/MuseScore/raw/master/share/sound/FluidR3Mono_GM.sf2",
    "https://github.com/musescore/MuseScore/raw/main/share/sound/FluidR3Mono_GM.sf2",
    # GeneralUser GS
    "https://github.com/mrbumpy409/GeneralUser-GS/raw/master/GeneralUser%20GS.sf2",
]


def find_or_download_soundfont() -> str:
    for path in SOUNDFONT_PATHS:
        if os.path.exists(path):
            print(f"✅  Soundfont ditemukan: {path}")
            return path

    # Cari via Homebrew/system
    vintage_path = None
    try:
        result = subprocess.run(
            ["find", "/usr/local", "/opt/homebrew", "-name", "*.sf2", "-type", "f"],
            capture_output=True, text=True, timeout=5
        )
        for line in result.stdout.strip().split("\n"):
            if not line or not os.path.exists(line):
                continue
            # Prioritas: skip VintageDreams dulu, coba yang lain
            if any(bl.lower() in line.lower() for bl in SOUNDFONT_BLACKLIST):
                vintage_path = line   # simpan sebagai fallback
                continue
            print(f"✅  Soundfont ditemukan: {line}")
            return line
    except Exception:
        vintage_path = None

    # Download soundfont GM yang proper
    sf_dir = os.path.expanduser("~/soundfonts")
    sf_path = os.path.join(sf_dir, "FluidR3Mono_GM.sf2")
    os.makedirs(sf_dir, exist_ok=True)

    print("⬇️   Soundfont GM tidak ditemukan. Mencoba download otomatis...")
    print(f"    Destination: {sf_path}")
    for url in SOUNDFONT_DOWNLOAD_URLS:
        try:
            print(f"    Mencoba: {url}")
            subprocess.run(
                ["curl", "-L", "--max-time", "60", "-o", sf_path, url],
                check=True
            )
            if os.path.getsize(sf_path) > 100_000:  # minimal 100KB
                # Validasi magic bytes: SF2 valid harus diawali "RIFF"
                with open(sf_path, "rb") as f:
                    magic = f.read(4)
                if magic == b"RIFF":
                    print("✅  Download selesai.")
                    return sf_path
                else:
                    print("    Bukan file SF2 valid (mungkin HTML redirect), skip.")
                    os.remove(sf_path)
            else:
                os.remove(sf_path)  # file terlalu kecil
        except Exception as e:
            print(f"    Gagal: {e}")
            if os.path.exists(sf_path):
                os.remove(sf_path)

    print()
    print("⚠️   Gagal download soundfont otomatis.")
    print("    Untuk suara lebih natural, download manual dan simpan ke ~/soundfonts/:")
    print("      GeneralUser GS  : https://www.schristiancollins.com/generaluser.php")
    print("      MuseScore General: https://ftp.osuosl.org/pub/musescore/soundfont/")
    print()

    # Fallback: gunakan VintageDreams jika tidak ada yang lain
    if vintage_path and os.path.exists(vintage_path):
        print(f"⚠️   Menggunakan fallback (suara synth): {vintage_path}")
        print("    → Unduh soundfont GM untuk suara orkestra yang natural!")
        return vintage_path

    # Fallback terakhir: cari semua .sf2 tanpa filter
    try:
        result = subprocess.run(
            ["find", "/opt/homebrew", "/usr/local", "-name", "*.sf2", "-type", "f"],
            capture_output=True, text=True, timeout=5
        )
        for line in result.stdout.strip().split("\n"):
            if line and os.path.exists(line):
                print(f"⚠️   Menggunakan fallback: {line}")
                return line
    except Exception:
        pass

    print("❌  Tidak ada soundfont ditemukan. Install dengan: brew install fluid-synth")
    sys.exit(1)


# ── constants ─────────────────────────────────────────────────────────────────

# General MIDI program numbers
GM = {
    "strings":      48,   # String Ensemble 1
    "strings2":     49,   # String Ensemble 2
    "choir":        52,   # Choir Aahs
    "brass":        61,   # Brass Section
    "piano":        0,    # Acoustic Grand Piano
    "epiano":       4,    # Electric Piano 1
    "organ":        19,   # Church Organ
    "pad_warm":     89,   # Pad 2 (warm)
    "pad_choir":    91,   # Pad 4 (choir)
    "pad_bowed":    92,   # Pad 5 (bowed)
    "synth_lead":   80,   # Lead 1 (square)
    "bass_finger":  33,   # Finger Bass
    "bass_synth":   38,   # Synth Bass 1
    "cello":        42,   # Cello
    "violin":       40,   # Violin
    "flute":        73,   # Flute
    "oboe":         68,   # Oboe
    "timpani":      47,   # Timpani
    "perc_synth":   118,  # Synth Drum
}

# Channel assignments (MIDI ch 0–15, ch 9 = drums)
CH = {
    "strings":  0,
    "strings2": 1,
    "brass":    2,
    "bass":     3,
    "pad":      4,
    "melody":   5,
    "choir":    6,
    "glitch":   7,
    "drums":    9,   # GM drums fixed channel
}

# Note name → MIDI number helper
NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]

FLAT_TO_SHARP = {"Bb": "A#", "Eb": "D#", "Ab": "G#", "Db": "C#", "Gb": "F#"}

def note(name: str) -> int:
    """'C4' → 60, 'G#3' → 56, 'Bb2' → 46, dll."""
    if len(name) >= 3 and name[1] == "b" and name[:2] in FLAT_TO_SHARP:
        n, octave = FLAT_TO_SHARP[name[:2]], int(name[2:])
    elif len(name) >= 3 and name[1] == "#":
        n, octave = name[:2], int(name[2:])
    else:
        n, octave = name[0], int(name[1:])
    return NOTE_NAMES.index(n) + (octave + 1) * 12


# ── chord & scale definitions ─────────────────────────────────────────────────

# Fokus: Dm → Bb → F → C (minor, tegang, naik ke major di akhir)
FOCUS_PROGRESSIONS = [
    [note("D3"), note("F3"), note("A3")],   # Dm
    [note("Bb2"), note("D3"), note("F3")],  # Bb
    [note("F2"), note("A2"), note("C3")],   # F
    [note("C3"), note("E3"), note("G3")],   # C (resolve)
]

# Rileks: C maj7 → F maj7 → Am7 → G
RELAX_PROGRESSIONS = [
    [note("C3"), note("E3"), note("G3"), note("B3")],   # Cmaj7 — cerah, hangat
    [note("F2"), note("A2"), note("C3"), note("E3")],   # Fmaj7 — lapang, tenang
    [note("G2"), note("B2"), note("D3"), note("G3")],   # G major — resolved, ringan
    [note("A2"), note("C3"), note("E3"), note("A3")],   # Am — wistful tapi tidak gelap
]

# Stres: cluster disonan yang tidak resolve
STRESS_PROGRESSIONS = [
    [note("C3"), note("C#3"), note("F#3"), note("Bb3")],  # cluster
    [note("Ab2"), note("D3"), note("Eb3"), note("A3")],   # tritone stack
    [note("E2"), note("F2"), note("Bb2"), note("B2")],    # semitone clash
    [note("B2"), note("C3"), note("F3"), note("F#3")],    # double tritone
]

# Kantuk: open fifths, mengambang
DROWSY_PROGRESSIONS = [
    [note("C2"), note("G2"), note("D3")],
    [note("F2"), note("C3"), note("G3")],
    [note("G2"), note("D3"), note("A3")],
    [note("A2"), note("E3"), note("B3")],
]

# Ostinato fokus (string runs — 16th note pattern)
FOCUS_OSTINATO = [
    note("D4"), note("F4"), note("A4"), note("C5"),
    note("D4"), note("F4"), note("E4"), note("C5"),
]

# Glitch pool — note tinggi disonan
GLITCH_POOL = [
    note("C6"), note("C#6"), note("Eb6"), note("F#6"),
    note("G6"), note("Ab6"), note("Bb6"), note("B6"),
    note("C7"), note("C#7"), note("D7"),
]

# Durum pattern fokus (GM drums)
# 36=kick, 38=snare, 42=hihat closed, 46=open hihat
FOCUS_KICK_PATTERN    = [1, 0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0]
FOCUS_SNARE_PATTERN   = [0, 0, 0, 0, 1, 0, 0, 0, 0, 0, 0, 0, 1, 0, 0, 0]
FOCUS_HIHAT_PATTERN   = [1, 0, 1, 0, 1, 0, 1, 0, 1, 0, 1, 0, 1, 0, 1, 0]

# Drum pattern stres — relentless seperti boss fight / game intense
# Kick ganda (4-on-floor + extra), snare dengan ghost note, hihat 16th terus-terusan
STRESS_KICK_PATTERN   = [1, 0, 0, 0,  1, 0, 1, 0,  1, 0, 0, 0,  1, 0, 1, 0]
STRESS_SNARE_PATTERN  = [0, 0, 0, 0,  1, 0, 0, 0,  0, 0, 0, 1,  1, 0, 0, 0]
STRESS_HIHAT_PATTERN  = [1, 1, 1, 1,  1, 1, 1, 1,  1, 1, 1, 1,  1, 1, 1, 1]

# Melodi stres — motif pendek, gelisah, kromatik (game danger feel)
STRESS_MELODY = [
    note("C5"), note("Eb5"), note("C5"), note("F#5"),
    note("Eb5"), note("C5"), note("Bb4"), note("F#4"),
]

# Focus climax melodic phrase untuk choir saat build > 0.7
FOCUS_CLIMAX_PHRASE = [
    note("D5"), note("F5"), note("A5"), note("C6"), note("D6"),
]


# ── EEG state ─────────────────────────────────────────────────────────────────

@dataclass
class EEGState:
    alpha: float = 0.5   # 0–1, rileks/flow
    beta:  float = 0.3   # 0–1, fokus/stres
    theta: float = 0.2   # 0–1, kantuk/meditasi
    tbr:     float = 0.5   # Theta/Beta Ratio (frontal) — normalized 0=focused, 1=drowsy
    tbr_raw: float = 1.0   # Raw theta/beta power ratio — tidak terpengaruh adaptive normalize

    def mental_state(self) -> str:
        # Drowsy override — pakai raw TBR supaya tidak tertipu adaptive normalization.
        # Setelah ngantuk 10+ menit, TBR ternormalisasi ke ~0.5 (jadi "normal baru").
        # Raw TBR > 2.0 artinya theta 2× beta secara absolut — reliably drowsy.
        # tbr > 0.45 diturunkan dari 0.55 karena normalizer bisa drift saat lama merem.
        # beta < 0.25 = sangat rendah (hampir ketiduran) → paksa calm meski alpha ikut turun.
        if (self.tbr_raw > 2.0
                or (self.tbr > 0.45 and self.beta < 0.45)
                or self.beta < 0.25):
            return "calm"
        # Binary 2-class — weighted arousal index
        # Beta dinaikkan (0.50) agar cognitive load sedang (Sudoku, fokus) bisa trigger tense.
        # TBR diturunkan (0.25) — drowsy sudah dilindungi guard di atas, tidak perlu besar.
        # Dead zone dikecilkan ke 0.02 — masih filter pure noise, tapi tidak terlalu ketat.
        arousal = 0.50 * self.beta - 0.25 * self.alpha - 0.25 * self.tbr
        return "tense" if arousal > 0.02 else "calm"

    def clamp(self):
        self.alpha = max(0.0, min(1.0, self.alpha))
        self.beta  = max(0.0, min(1.0, self.beta))
        self.theta = max(0.0, min(1.0, self.theta))
        self.tbr   = max(0.0, min(1.0, self.tbr))


# ── music engine ──────────────────────────────────────────────────────────────

class MusicEngine:
    def __init__(self, soundfont_path: str):
        self.sf_path = soundfont_path
        self.eeg = EEGState()
        self._lock = threading.Lock()
        self._running = False

        # FluidSynth init
        self.fs = fluidsynth.Synth(gain=0.7, samplerate=44100.0)
        self.fs.start(driver="coreaudio")  # macOS CoreAudio
        self.sfid = self.fs.sfload(soundfont_path)
        if self.sfid == -1:
            raise RuntimeError(f"Gagal load soundfont: {soundfont_path}")

        self._setup_channels()

        # State tracking
        self._chord_idx   = 0
        self._ost_step    = 0
        self._drum_step   = 0
        self._tense_level = 0.0       # arousal build-up: 0=calm baseline, 1=peak tense
        self._prev_state  = None
        self._state_votes  = deque(maxlen=12)  # 12 ticks ~1.7 s @72 BPM calm, ~1.1 s @130 BPM tense
        self._glitch_cooldown    = 0
        self._pending_transition = None  # (prev, curr) — bar-locked, tunggu t%16==0

        # Timing
        self._tick = 0               # 16th note counter
        self._bpm  = 72.0

        print("✅  Music engine siap.")

    def _setup_channels(self):
        """Load instrument ke setiap channel."""
        # Menggunakan pad & synth — cocok untuk VintageDreamsWaves maupun GM orkestra.
        # Pad sounds (88-95) adalah ambient pad yang bekerja di semua soundfont.
        instruments = {
            CH["strings"]:  GM["strings"],       # 48 – vintage strings ensemble
            CH["strings2"]: GM["pad_warm"],       # 89 – warm pad (lebih smooth dari strings2)
            CH["brass"]:    GM["brass"],          # 61 – brass / synth brass
            CH["bass"]:     GM["bass_synth"],     # 38 – synth bass (lebih kuat dari finger bass)
            CH["pad"]:      GM["pad_choir"],      # 91 – choir pad (ambient, mengambang)
            CH["melody"]:   GM["pad_warm"],       # 89 – akan di-override oleh _switch_instruments
            CH["choir"]:    GM["pad_choir"],      # 91 – pad choir (lebih baik dari choir aahs di synth SF)
            CH["glitch"]:   GM["synth_lead"],     # 80 – square synth lead (glitch)
        }
        for ch, prog in instruments.items():
            self.fs.program_select(ch, self.sfid, 0, prog)

        # Reverb — awal netral, akan di-update per state
        self.fs.set_reverb(roomsize=0.7, damping=0.6, width=0.8, level=0.5)
        # Chorus DIMATIKAN — menyebabkan suara "digital warble" yang aneh
        self.fs.set_chorus(0, 0.0, 0.3, 0.5, 0)

    # ── public API ─────────────────────────────────────────────────────────

    def set_eeg(self, alpha: float, beta: float, theta: float,
                tbr: float = 0.5, tbr_raw: float = 1.0):
        """Update nilai EEG. Dipanggil dari MuseConnector setiap ~1 detik."""
        with self._lock:
            self.eeg.alpha   = alpha
            self.eeg.beta    = beta
            self.eeg.theta   = theta
            self.eeg.tbr     = tbr
            self.eeg.tbr_raw = tbr_raw
            self.eeg.clamp()

    def get_arousal(self) -> float:
        """Raw arousal index — positif=tense, negatif=calm. Berguna untuk debug."""
        return round(0.45 * self.eeg.beta - 0.25 * self.eeg.alpha - 0.30 * self.eeg.tbr, 4)

    def get_confidence(self) -> float:
        """Seberapa jauh dari ambang batas 0.0 (0=borderline, 1=sangat yakin).
        Normalisasi ke ±0.08 — disesuaikan dengan range arousal Muse 2 frontal."""
        return min(1.0, abs(self.get_arousal()) / 0.08)

    def get_consistency(self) -> float:
        """Seberapa konsisten vote buffer (0.5=50/50, 1.0=bulat satu state)."""
        if not self._state_votes:
            return 0.0
        top = Counter(self._state_votes).most_common(1)[0][1]
        return round(top / len(self._state_votes), 3)

    def start(self):
        if self._running:
            return  # cegah double-start saat reconnect
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        print("▶  Musik mulai diputar...")

    def stop(self):
        self._running = False
        self._all_notes_off()
        print("■  Musik berhenti.")

    # ── main loop ──────────────────────────────────────────────────────────

    def _loop(self):
        # Initialize instrumen berdasarkan state awal
        self._switch_instruments(self.eeg.mental_state())

        while self._running:
            try:
                with self._lock:
                    eeg = EEGState(
                        alpha=self.eeg.alpha,
                        beta=self.eeg.beta,
                        theta=self.eeg.theta,
                        tbr=self.eeg.tbr,
                        tbr_raw=self.eeg.tbr_raw,
                    )

                # ── State smoothing: asymmetric vote buffer ───────────────
                # EMG artifact sudah diblok di connector (pass 1 frontal scan).
                # Buffer lebih kecil (12 tick) dan threshold lebih rendah → responsif.
                #
                # Masuk tense : butuh 9/12 = 75% → harus sustained ~2.5 s @72 BPM
                # Keluar tense: butuh 4/12 = 33% calm → relaks cukup cepat
                raw_state = eeg.mental_state()
                self._state_votes.append(raw_state)
                total  = len(self._state_votes)
                counts = Counter(self._state_votes)
                current = self._prev_state or "calm"
                if current == "tense":
                    # Mudah keluar: 4/12 calm sudah cukup
                    calm_count = counts.get("calm", 0)
                    state = "calm" if calm_count >= max(1, int(total * 0.334)) else "tense"
                else:
                    # Masuk tense: butuh 8/12 (67%) — cukup ketat untuk noise, cukup responsif
                    tense_count = counts.get("tense", 0)
                    state = "tense" if tense_count >= max(1, int(total * 0.667)) else "calm"
                self._update_build_level(state)
                self._update_bpm(state, eeg)
                self._update_mixing(state, eeg)

                # 16th note events
                self._tick_event(state, eeg)

                # Sleep satu 16th note
                sleep_sec = (60.0 / self._bpm) / 4.0
                time.sleep(sleep_sec)

                self._tick += 1

            except Exception as e:
                print(f"⚠️  Loop error (tick {self._tick}): {e}")
                time.sleep(0.1)  # jangan tight-loop jika terus error

    def _tick_event(self, state: str, eeg: EEGState):
        t   = self._tick
        lvl = self._tense_level

        # ── Bar-locked transition ─────────────────────────────────────────
        # Eksekusi di half-bar (t%8==0) — musikal dan lebih responsif dari full bar
        if self._pending_transition and t % 8 == 0:
            prev_t, curr_t = self._pending_transition
            self._pending_transition = None
            self._on_state_change(prev_t, curr_t)

        # ── Chord / pad ───────────────────────────────────────────────────
        # Tense tinggi: stab setiap quarter note (agresif, rhythmic)
        # Semua lain: sustain setiap 1 bar
        if state == "tense" and lvl > 0.55:
            if t % 4 == 0:
                self._play_chord(state, eeg)
        elif t % 16 == 0:
            self._play_chord(state, eeg)

        # ── String ostinato — 16th note (tense, setelah build cukup) ──────
        if state == "tense" and lvl > 0.25:
            self._play_ostinato(eeg)

        # ── Bass ──────────────────────────────────────────────────────────
        # Calm: half note (8 ticks) — sparse
        # Tense low: quarter note (4 ticks)
        # Tense high: 8th note (2 ticks) — relentless
        if state == "calm":
            bass_interval = 8
        elif lvl > 0.55:
            bass_interval = 2
        else:
            bass_interval = 4
        if t % bass_interval == 0:
            self._play_bass(state)

        # ── Melody / pad filler — calm ────────────────────────────────────
        if t % 8 == 0 and state == "calm":
            self._play_melody_fill(eeg)

        # ── Brass swell (tense) — masuk saat build tinggi ─────────────────
        if state == "tense" and t % 8 == 0 and lvl > 0.4:
            self._play_brass_swell()

        # ── Drums ─────────────────────────────────────────────────────────
        # Mulai saat tense_level > 0.3; escalate ke stress pattern saat > 0.55
        if state == "tense" and lvl >= 0.3:
            step     = t % 16
            kick_p   = STRESS_KICK_PATTERN  if lvl > 0.55 else FOCUS_KICK_PATTERN
            snare_p  = STRESS_SNARE_PATTERN if lvl > 0.55 else FOCUS_SNARE_PATTERN
            hihat_p  = STRESS_HIHAT_PATTERN if lvl > 0.55 else FOCUS_HIHAT_PATTERN
            if kick_p[step]:
                vel = min(127, int(85 + lvl * 30))
                self.fs.noteon(CH["drums"], 36, vel)
                threading.Timer(0.05, lambda: self.fs.noteoff(CH["drums"], 36)).start()
            if snare_p[step]:
                vel = min(127, int(70 + lvl * 30))
                self.fs.noteon(CH["drums"], 38, vel)
                threading.Timer(0.05, lambda: self.fs.noteoff(CH["drums"], 38)).start()
            if hihat_p[step]:
                vel = min(127, int(50 + lvl * 25) + (15 if step % 4 == 0 else 0))
                self.fs.noteon(CH["drums"], 42, vel)
                threading.Timer(0.025, lambda: self.fs.noteoff(CH["drums"], 42)).start()

        # ── Tense climax ─────────────────────────────────────────────────
        if state == "tense" and t % 4 == 0:
            self._play_tense_climax()

        # ── Glitch (tense high) ───────────────────────────────────────────
        if state == "tense" and lvl > 0.65 and random.random() < (0.20 + lvl * 0.40):
            self._play_glitch(eeg)

        # ── Calm breathing — pad swell ────────────────────────────────────
        if state == "calm":
            self._play_calm_breathing()

        self._ost_step += 1

    # ── layer players ──────────────────────────────────────────────────────

    def _play_chord(self, state: str, eeg: EEGState):
        lvl = self._tense_level
        if state == "calm":
            prog = RELAX_PROGRESSIONS
        elif lvl > 0.55:
            prog = STRESS_PROGRESSIONS
        else:
            prog = FOCUS_PROGRESSIONS
        chord = prog[self._chord_idx % len(prog)]
        self._chord_idx += 1

        # Tense tinggi: stab pendek; semua lain: sustain penuh
        if state == "tense" and lvl > 0.55:
            dur_sec = (60.0 / self._bpm) * 0.3
        else:
            dur_sec = (60.0 / self._bpm) * 4.0

        str_vel = int(55 + (lvl * 60 if state == "tense" else 0))  # 55 calm → 55-115 tense

        for n in chord:
            self.fs.noteon(CH["strings"], n, str_vel)
        threading.Timer(dur_sec * 0.9,
            lambda chord=chord: [self.fs.noteoff(CH["strings"], n) for n in chord]
        ).start()

        # Strings2 harmony — calm & mild tense only
        if state == "calm" or lvl < 0.55:
            upper = [n + 12 for n in chord[:2]]
            vel2 = int(str_vel * 0.65)
            for n in upper:
                self.fs.noteon(CH["strings2"], n, vel2)
            threading.Timer(dur_sec * 0.85,
                lambda upper=upper: [self.fs.noteoff(CH["strings2"], n) for n in upper]
            ).start()

        # Pad — calm only (root & fifth, sangat lembut)
        if state == "calm":
            pad_notes = [chord[0] - 12, chord[2] - 12] if len(chord) >= 3 else [chord[0] - 12]
            for n in pad_notes:
                self.fs.noteon(CH["pad"], n, 38)
            threading.Timer(dur_sec,
                lambda pad_notes=pad_notes: [self.fs.noteoff(CH["pad"], n) for n in pad_notes]
            ).start()

    def _play_ostinato(self, eeg: EEGState):
        """String ostinato — 16th note run, kunci dari suara fokus Zimmer."""
        step = self._ost_step % len(FOCUS_OSTINATO)
        n = FOCUS_OSTINATO[step]

        # Humanize sedikit — velocity variatif tapi teratur
        accent = step % 4 == 0  # aksen di beat
        vel_base = 70 + int(self._tense_level * 35)
        vel = vel_base + (15 if accent else 0) + random.randint(-5, 5)
        vel = max(40, min(127, vel))

        dur_sec = (60.0 / self._bpm) / 4.0 * 0.8  # sedikit staccato
        self.fs.noteon(CH["melody"], n, vel)
        threading.Timer(dur_sec,
            lambda: self.fs.noteoff(CH["melody"], n)
        ).start()

    def _play_bass(self, state: str):
        lvl = self._tense_level
        if state == "calm":
            prog = RELAX_PROGRESSIONS
        elif lvl > 0.55:
            prog = STRESS_PROGRESSIONS
        else:
            prog = FOCUS_PROGRESSIONS
        chord     = prog[self._chord_idx % len(prog)]
        bass_note = max(24, chord[0] - 12)

        if state == "calm":
            vel     = 60
            dur_sec = (60.0 / self._bpm) * 0.8
        else:  # tense
            vel     = int(75 + lvl * 35)
            dur_sec = (60.0 / self._bpm) * (0.18 if lvl > 0.55 else 0.8)

        self.fs.noteon(CH["bass"], bass_note, vel)
        threading.Timer(dur_sec,
            lambda: self.fs.noteoff(CH["bass"], bass_note)
        ).start()

    def _play_brass_swell(self):
        """Brass masuk perlahan saat tension build-up tinggi."""
        if self._tense_level < 0.4:
            return
        chord = FOCUS_PROGRESSIONS[self._chord_idx % len(FOCUS_PROGRESSIONS)]
        upper = [n + 7 for n in chord[:2]]  # fifth up
        vel = int(40 + self._tense_level * 55)
        dur_sec = (60.0 / self._bpm) * 2.0

        for n in upper:
            if 0 <= n <= 127:
                self.fs.noteon(CH["brass"], n, vel)
        threading.Timer(dur_sec,
            lambda upper=upper: [self.fs.noteoff(CH["brass"], n) for n in upper]
        ).start()

    def _play_melody_fill(self, eeg: EEGState):
        """Melody filler untuk calm — sesekali saja."""
        if random.random() > 0.55:
            return
        scale = [note("C5"), note("E5"), note("G5"), note("A5"),
                 note("B5"), note("D5"), note("C6")]
        n       = random.choice(scale)
        vel     = random.randint(42, 68)
        dur_sec = (60.0 / self._bpm) * random.choice([0.5, 1.0, 1.5, 2.0])
        self.fs.noteon(CH["melody"], n, vel)
        threading.Timer(dur_sec, lambda: self.fs.noteoff(CH["melody"], n)).start()

    def _play_stress_melody(self, eeg: EEGState):
        """Motif melodi pendek dan gelisah pada synth lead — game danger feel."""
        step = (self._tick // 2) % len(STRESS_MELODY)
        n = STRESS_MELODY[step]
        # Velocity variatif: aksen di downbeat, ghost di offbeat
        accent = (self._tick % 4 == 0)
        vel = int(65 + eeg.beta * 30) + (20 if accent else 0)
        vel = max(40, min(120, vel))
        # Durasi sangat pendek — staccato, punchy
        dur_sec = (60.0 / self._bpm) * (0.4 if accent else 0.25)
        self.fs.noteon(CH["melody"], n, vel)
        threading.Timer(dur_sec, lambda: self.fs.noteoff(CH["melody"], n)).start()

    def _play_stress_climax(self):
        """Brass stab disonan saat stress build sangat tinggi (>0.7) — klimaks anxietas."""
        if self._stress_build < 0.7:
            return
        chord = STRESS_PROGRESSIONS[self._chord_idx % len(STRESS_PROGRESSIONS)]
        # Minor 2nd + tritone di atas root — sangat disonan dan menekan
        stab = [chord[0], chord[0] + 1, chord[0] + 6]
        vel = int(60 + self._stress_build * 55)   # 60–115
        dur_sec = (60.0 / self._bpm) * 0.20        # sangat pendek, punchy
        for n in stab:
            if 0 <= n <= 127:
                self.fs.noteon(CH["brass"], n, vel)
        threading.Timer(dur_sec,
            lambda stab=stab: [self.fs.noteoff(CH["brass"], n) for n in stab]
        ).start()

    def _play_stress_perc(self, eeg: EEGState):
        """Perkusi erratic untuk state stress — nervous, tidak teratur."""
        intensity = min(1.0, eeg.beta * 0.55 + eeg.theta * 0.45)

        # Hi-hat erratic — sering dan acak
        if random.random() < intensity * 0.55:
            vel = random.randint(45, 85)
            self.fs.noteon(CH["drums"], 42, vel)
            threading.Timer(0.025, lambda: self.fs.noteoff(CH["drums"], 42)).start()

        # Snare acak — kejutan mendadak
        if random.random() < intensity * 0.30:
            vel = random.randint(55, 95)
            self.fs.noteon(CH["drums"], 38, vel)
            threading.Timer(0.06, lambda: self.fs.noteoff(CH["drums"], 38)).start()

        # Kick acak — ground yang tidak stabil
        if random.random() < intensity * 0.20:
            vel = random.randint(65, 105)
            self.fs.noteon(CH["drums"], 36, vel)
            threading.Timer(0.07, lambda: self.fs.noteoff(CH["drums"], 36)).start()

        # Open hi-hat sesekali — anxious, terbuka
        if random.random() < intensity * 0.15:
            vel = random.randint(40, 70)
            self.fs.noteon(CH["drums"], 46, vel)
            threading.Timer(0.12, lambda: self.fs.noteoff(CH["drums"], 46)).start()

    def _play_calm_breathing(self):
        """Efek 'nafas' pada pad saat calm — swell perlahan CC11."""
        phase = (self._tick % 48) / 48.0
        expr  = int(35 + 55 * (0.5 - 0.5 * math.cos(2 * math.pi * phase)))
        self.fs.cc(CH["pad"],   11, expr)
        self.fs.cc(CH["choir"], 11, expr)

    def _play_tense_climax(self):
        """Climax saat tense level tinggi: choir ascending (medium), brass stab (puncak)."""
        lvl = self._tense_level
        if lvl < 0.65:
            return
        if lvl >= 0.85:
            # Sangat tense: brass stab disonan
            chord   = STRESS_PROGRESSIONS[self._chord_idx % len(STRESS_PROGRESSIONS)]
            stab    = [chord[0], chord[0] + 1, chord[0] + 6]
            vel     = int(60 + lvl * 55)
            dur_sec = (60.0 / self._bpm) * 0.20
            for n in stab:
                if 0 <= n <= 127:
                    self.fs.noteon(CH["brass"], n, vel)
            threading.Timer(dur_sec,
                lambda stab=stab: [self.fs.noteoff(CH["brass"], n) for n in stab]
            ).start()
        else:
            # Moderately tense: choir ascending phrase
            step    = (self._tick // 4) % len(FOCUS_CLIMAX_PHRASE)
            n       = FOCUS_CLIMAX_PHRASE[step]
            vel     = int(55 + lvl * 45)
            dur_sec = (60.0 / self._bpm) * 0.85
            self.fs.noteon(CH["choir"], n, vel)
            threading.Timer(dur_sec, lambda: self.fs.noteoff(CH["choir"], n)).start()

    def _play_glitch(self, eeg: EEGState):
        """
        Glitch engine — simulasi broken/stuttering electronics.

        Teknik:
        1. Random trigger dengan probabilitas dari beta+theta
        2. Rapid retriggering (stutter) dengan delay sangat pendek
        3. Note cluster disonan dari GLITCH_POOL
        4. Kadang silence sudden (semua note off) lalu burst
        """
        intensity = eeg.beta * 0.55 + eeg.theta * 0.55
        intensity = min(1.0, intensity)

        if self._glitch_cooldown > 0:
            self._glitch_cooldown -= 1
            return

        # Main glitch trigger
        if random.random() < intensity * 0.7:
            n = random.choice(GLITCH_POOL)
            vel = random.randint(60, 110)
            dur_ms = random.choice([0.02, 0.03, 0.05, 0.08, 0.12])

            self.fs.noteon(CH["glitch"], n, vel)
            threading.Timer(dur_ms, lambda: self.fs.noteoff(CH["glitch"], n)).start()

            # Stutter: repeat note dengan interval sangat pendek
            if random.random() < intensity * 0.6:
                for i, delay in enumerate([0.04, 0.07, 0.10]):
                    if random.random() < 0.6:
                        stutter_vel = max(30, vel - i * 15)
                        threading.Timer(
                            delay,
                            lambda n=n, v=stutter_vel: (
                                self.fs.noteon(CH["glitch"], n, v),
                                threading.Timer(0.02,
                                    lambda: self.fs.noteoff(CH["glitch"], n)
                                ).start()
                            )
                        ).start()

            # Cluster burst — beberapa note sekaligus
            if random.random() < intensity * 0.35:
                cluster = random.sample(GLITCH_POOL, k=random.randint(2, 4))
                delay = random.uniform(0.05, 0.15)
                for cn in cluster:
                    threading.Timer(
                        delay + random.uniform(0, 0.03),
                        lambda cn=cn: (
                            self.fs.noteon(CH["glitch"], cn, random.randint(50, 90)),
                            threading.Timer(0.04,
                                lambda cn=cn: self.fs.noteoff(CH["glitch"], cn)
                            ).start()
                        )
                    ).start()

            # Sudden silence lalu burst (glitch artifact klasik)
            if random.random() < intensity * 0.2:
                self._all_notes_off(channels=[CH["glitch"]])
                self._glitch_cooldown = random.randint(2, 5)
                burst_delay = random.uniform(0.05, 0.2)
                threading.Timer(burst_delay, self._glitch_burst).start()

    def _glitch_burst(self):
        """Burst of notes setelah silence."""
        for _ in range(random.randint(3, 6)):
            n = random.choice(GLITCH_POOL)
            delay = random.uniform(0, 0.08)
            vel = random.randint(70, 120)
            threading.Timer(
                delay,
                lambda n=n, v=vel: (
                    self.fs.noteon(CH["glitch"], n, v),
                    threading.Timer(0.03, lambda: self.fs.noteoff(CH["glitch"], n)).start()
                )
            ).start()

    # ── state management ───────────────────────────────────────────────────

    def _update_build_level(self, state: str):
        """tense_level naik perlahan saat tense, turun saat calm."""
        if state == "tense":
            self._tense_level = min(1.0, self._tense_level + 0.005)
        else:
            self._tense_level = max(0.0, self._tense_level - 0.003)

        if state != self._prev_state and self._prev_state is not None:
            # Bar-lock: simpan pending, eksekusi di t%16==0 pada _tick_event
            self._pending_transition = (self._prev_state, state)
        self._prev_state = state

    def _on_state_change(self, prev: str, curr: str):
        """Transisi antar state — bar-locked, fluent."""
        print(f"  → state berubah: {prev} → {curr}")

        # Melody/choir/glitch selalu di-cut (tidak terdengar kasar karena pendek)
        self._all_notes_off(channels=[CH["melody"], CH["choir"], CH["glitch"]])

        # Tense → calm: cut drums juga, tapi biarkan strings/bass decay natural
        # (TIDAK full all_notes_off — strings sustain ke calm terdengar lebih halus)
        if prev == "tense":
            self._all_notes_off(channels=[CH["drums"]])

        # Reset chord index → mulai dari awal progressi yang benar
        self._chord_idx = 0

        # Snap BPM 60% ke target baru supaya terasa langsung
        if curr == "tense":
            target_snap = 80 + self._tense_level * 50
        else:  # calm
            target_snap = 60.0
        self._bpm += (target_snap - self._bpm) * 0.60
        self._switch_instruments(curr)

    def _switch_instruments(self, state: str):
        """Ganti instrumen channel melody sesuai state (calm / tense)."""
        if state == "tense":
            self.fs.program_select(CH["melody"], self.sfid, 0, GM["synth_lead"])  # tegang
        else:  # calm
            self.fs.program_select(CH["melody"], self.sfid, 0, GM["pad_warm"])
            self.fs.program_select(CH["pad"],    self.sfid, 0, GM["pad_warm"])
            self.fs.program_select(CH["choir"],  self.sfid, 0, GM["pad_warm"])

    def _update_bpm(self, state: str, eeg: EEGState):
        if state == "calm":
            target_bpm = 55 + eeg.alpha * 10          # 55→65 seiring alpha
        else:  # tense
            target_bpm = 80 + self._tense_level * 50  # 80→130 seiring tense
        self._bpm += (target_bpm - self._bpm) * 0.05  # 0.01→0.05: ~6 detik konvergen

    def _update_mixing(self, state: str, eeg: EEGState):
        """Update reverb sesuai state — calm: warm hall, tense: tightens with level."""
        if state == "calm":
            self.fs.set_reverb(roomsize=0.75, damping=0.5, width=0.8, level=0.55)
        else:  # tense — ruangan semakin kering seiring tense_level
            lvl = self._tense_level
            rs  = max(0.08, 0.45 - lvl * 0.37)
            lv  = max(0.08, 0.35 - lvl * 0.27)
            self.fs.set_reverb(roomsize=rs, damping=0.7 + lvl * 0.1, width=0.8, level=lv)

    def _all_notes_off(self, channels: Optional[list] = None):
        chs = channels if channels else list(CH.values())
        for ch in chs:
            for n in range(128):
                self.fs.noteoff(ch, n)

    def __del__(self):
        try:
            self.stop()
            self.fs.delete()
        except Exception:
            pass


# ── terminal display ───────────────────────────────────────────────────────────

def display_status(eeg: EEGState, engine: MusicEngine):
    state = eeg.mental_state()
    lvl   = engine._tense_level
    bpm   = engine._bpm

    state_colors = {
        "calm":  "\033[32m",   # green
        "tense": "\033[35m",   # magenta
    }
    RESET = "\033[0m"
    BOLD  = "\033[1m"
    col   = state_colors.get(state, "")

    bar = lambda v, w=20: "█" * int(v * w) + "░" * (w - int(v * w))

    os.system("clear")
    print(f"{BOLD}╔══════════════════════════════════════╗{RESET}")
    print(f"{BOLD}║     EEG MUSIC ENGINE  —  v2.0        ║{RESET}")
    print(f"{BOLD}╚══════════════════════════════════════╝{RESET}")
    print()
    print(f"  STATE   {col}{BOLD}{state.upper():10}{RESET}   BPM: {bpm:.1f}")
    print()
    print(f"  alpha   {bar(eeg.alpha)}  {eeg.alpha:.2f}")
    print(f"  beta    {bar(eeg.beta)}  {eeg.beta:.2f}")
    print(f"  theta   {bar(eeg.theta)}  {eeg.theta:.2f}")
    print(f"  TBR     {bar(eeg.tbr)}  {eeg.tbr:.2f}")
    print(f"\n  tense   {bar(lvl)}  {lvl:.2f}  {'🔴' * int(lvl * 5)}")
    print()
    print("  ─────────────────────────────────────")
    print("  Preset:  1=calm  2=tense")
    print("  Manual:  α: +/─    β: w/s    θ: e/d")
    print("  Quit:    q")
    print()
    print("  ⚠️  Nanti: ganti keyboard input dengan nilai BrainFlow")


# ── keyboard input ─────────────────────────────────────────────────────────────

def get_key():
    """Non-blocking single key read di macOS/Linux."""
    import tty, termios, select
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        r, _, _ = select.select([sys.stdin], [], [], 0.05)
        if r:
            return sys.stdin.read(1)
        return None
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


# ── main ───────────────────────────────────────────────────────────────────────

def main():
    print("🎵  EEG Music Engine")
    print("    Mencari soundfont...")
    sf_path = find_or_download_soundfont()

    print("    Inisialisasi FluidSynth...")
    engine = MusicEngine(sf_path)
    engine.start()

    STEP = 0.05  # increment per keypress

    try:
        while True:
            key = get_key()
            eeg = engine.eeg

            if key == "q":
                break
            elif key == "1":
                engine.set_eeg(0.80, 0.15, 0.20, tbr=0.65)   # calm
            elif key == "2":
                engine.set_eeg(0.20, 0.75, 0.30, tbr=0.30)   # tense
            elif key == "=":
                engine.set_eeg(eeg.alpha + STEP, eeg.beta, eeg.theta)
            elif key == "-":
                engine.set_eeg(eeg.alpha - STEP, eeg.beta, eeg.theta)
            elif key == "w":
                engine.set_eeg(eeg.alpha, eeg.beta + STEP, eeg.theta)
            elif key == "s":
                engine.set_eeg(eeg.alpha, eeg.beta - STEP, eeg.theta)
            elif key == "e":
                engine.set_eeg(eeg.alpha, eeg.beta, eeg.theta + STEP)
            elif key == "d":
                engine.set_eeg(eeg.alpha, eeg.beta, eeg.theta - STEP)

            display_status(engine.eeg, engine)

    except KeyboardInterrupt:
        pass
    finally:
        engine.stop()
        print("\n✅  Selesai.")


if __name__ == "__main__":
    main()
