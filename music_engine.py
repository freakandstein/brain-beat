"""
EEG Music Engine
================
Standalone music engine yang menerima nilai EEG (alpha/beta/theta)
dan menghasilkan musik generatif via FluidSynth + Soundfont.

Penggunaan:
    python music_engine.py

Kontrol keyboard (saat berjalan):
    1  →  preset: rileks
    2  →  preset: fokus
    3  →  preset: stres
    4  →  preset: kantuk
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
    delta: float = 0.4   # 0–1, tidur dalam / drowsy
    alpha: float = 0.5   # 0–1, rileks/flow
    beta:  float = 0.3   # 0–1, fokus/stres
    theta: float = 0.2   # 0–1, kantuk/meditasi

    def mental_state(self) -> str:
        b, a, t = self.beta, self.alpha, self.theta
        # Stress: butuh beta DAN theta tinggi — jangan mudah trigger saat focus
        if b > 0.70 and t > 0.62:  return "stress"
        # Focus: beta moderat-tinggi, alpha tidak dominan
        if b > 0.52 and a < 0.45:  return "focus"
        if a > 0.55 and b < 0.4:   return "relax"
        if t > 0.65:                return "drowsy"
        # Fallback: beta di atas rata-rata → masih lebih focus dari relax
        if b > 0.40:                return "focus"
        return "relax"

    def clamp(self):
        self.delta = max(0.0, min(1.0, self.delta))
        self.alpha = max(0.0, min(1.0, self.alpha))
        self.beta  = max(0.0, min(1.0, self.beta))
        self.theta = max(0.0, min(1.0, self.theta))


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
        self._chord_idx = 0
        self._ost_step  = 0
        self._drum_step = 0
        self._build_level  = 0.0      # fokus tension build (0→1)
        self._stress_build = 0.0      # stress escalation build (0→1)
        self._prev_state  = None
        self._state_votes  = deque(maxlen=16)  # vote buffer ~2s @120BPM (8 tick/s)
        self._glitch_cooldown = 0

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

    def set_eeg(self, alpha: float, beta: float, theta: float, delta: float = 0.4):
        """Update nilai EEG. Ini yang nanti dipanggil dari BrainFlow."""
        with self._lock:
            self.eeg.delta = delta
            self.eeg.alpha = alpha
            self.eeg.beta  = beta
            self.eeg.theta = theta
            self.eeg.clamp()

    def start(self):
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
                        theta=self.eeg.theta
                    )

                # ── State smoothing: vote buffer mencegah loncat-loncat ──
                # raw_state dihitung dari nilai EEG sesaat, bisa flicker.
                # state (smooth) = mayoritas dari 16 tick terakhir (~2 detik).
                raw_state = eeg.mental_state()
                self._state_votes.append(raw_state)
                state = Counter(self._state_votes).most_common(1)[0][0]
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
        t = self._tick

        # ── Chord / pad ───────────────────────────────────────────────────
        # Stres: stab setiap quarter note (agresif, rhythmic)
        # Lainnya: sustain setiap 1 bar
        if state == "stress":
            if t % 4 == 0:
                self._play_chord(state, eeg)
        elif t % 16 == 0:
            self._play_chord(state, eeg)

        # ── String ostinato — setiap 16th note (fokus only) ──────────────
        if state == "focus":
            self._play_ostinato(eeg)

        # ── Bass ──────────────────────────────────────────────────────────
        # Stres: 8th note (2 ticks) — relentless pulse
        # Kantuk: half note (8 ticks) — sparse
        # Lainnya: quarter note (4 ticks)
        bass_interval = 8 if state == "drowsy" else (2 if state == "stress" else 4)
        if t % bass_interval == 0:
            self._play_bass(state)

        # ── Melody / choir filler — setiap half bar ───────────────────────
        if t % 8 == 0 and state in ("relax", "drowsy"):
            self._play_melody_fill(state, eeg)

        # ── Brass swell (fokus) — masuk saat build tinggi ─────────────────
        if state == "focus" and t % 8 == 0 and self._build_level > 0.4:
            self._play_brass_swell()

        # ── Drums (fokus) ─────────────────────────────────────────────────
        if state == "focus":
            step = t % 16
            if FOCUS_KICK_PATTERN[step]:
                vel = int(85 + self._build_level * 30)
                self.fs.noteon(CH["drums"], 36, vel)
                threading.Timer(0.05, lambda: self.fs.noteoff(CH["drums"], 36)).start()
            if FOCUS_SNARE_PATTERN[step]:
                vel = int(70 + self._build_level * 25)
                self.fs.noteon(CH["drums"], 38, vel)
                threading.Timer(0.05, lambda: self.fs.noteoff(CH["drums"], 38)).start()
            if FOCUS_HIHAT_PATTERN[step]:
                vel = int(50 + self._build_level * 20)
                self.fs.noteon(CH["drums"], 42, vel)
                threading.Timer(0.03, lambda: self.fs.noteoff(CH["drums"], 42)).start()

        # ── Drums (stres) — pattern relentless seperti game boss fight ────
        if state == "stress":
            step = t % 16
            intensity = min(1.0, eeg.beta * 0.7 + eeg.theta * 0.3)
            build_boost = int(self._stress_build * 18)
            if STRESS_KICK_PATTERN[step]:
                vel = min(127, int(90 + intensity * 25) + build_boost)
                self.fs.noteon(CH["drums"], 36, vel)
                threading.Timer(0.05, lambda: self.fs.noteoff(CH["drums"], 36)).start()
            if STRESS_SNARE_PATTERN[step]:
                vel = min(127, int(75 + intensity * 30) + build_boost)
                self.fs.noteon(CH["drums"], 38, vel)
                threading.Timer(0.06, lambda: self.fs.noteoff(CH["drums"], 38)).start()
            if STRESS_HIHAT_PATTERN[step]:
                # Hihat 16th relentless, velocity sedikit variatif
                vel = min(127, int(50 + intensity * 20) + (15 if step % 4 == 0 else 0) + build_boost // 2)
                self.fs.noteon(CH["drums"], 42, vel)
                threading.Timer(0.025, lambda: self.fs.noteoff(CH["drums"], 42)).start()

        # ── Melodi stres — motif pendek, gelisah (synth lead) ────────────
        if state == "stress" and t % 2 == 0:
            self._play_stress_melody(eeg)

        # ── Stress climax — brass stab disonan saat stress build puncak ───
        if state == "stress" and t % 4 == 0:
            self._play_stress_climax()

        # ── Glitch (stres) — sebagai aksen, probabilitas naik seiring build ─
        if state == "stress" and random.random() < (0.20 + self._stress_build * 0.40):
            self._play_glitch(eeg)

        # ── Focus climax — choir line saat build puncak (>0.7) ───────────
        if state == "focus" and t % 4 == 0:
            self._play_focus_climax()

        # ── Drowsy breathing — pad swell ──────────────────────────────────
        if state == "drowsy":
            self._play_drowsy_breathing()

        self._ost_step += 1

    # ── layer players ──────────────────────────────────────────────────────

    def _play_chord(self, state: str, eeg: EEGState):
        progs = {
            "focus":  FOCUS_PROGRESSIONS,
            "relax":  RELAX_PROGRESSIONS,
            "stress": STRESS_PROGRESSIONS,
            "drowsy": DROWSY_PROGRESSIONS,
        }
        prog = progs[state]
        chord = prog[self._chord_idx % len(prog)]
        self._chord_idx += 1

        dur_sec = (60.0 / self._bpm) * 4.0  # 1 bar
        # Stres: chord stab pendek dan punchy (quarter note), bukan sustain
        if state == "stress":
            dur_sec = (60.0 / self._bpm) * 0.3

        # Strings pad — selalu ada
        str_vel = {
            "focus":  int(55 + self._build_level * 40),
            "relax":  55,   # lembut — tidak menghantam
            "stress": int(70 + self._stress_build * 35),  # makin keras seiring build
            "drowsy": 50,
        }[state]

        for n in chord:
            self.fs.noteon(CH["strings"], n, str_vel)
        threading.Timer(dur_sec * 0.9,
            lambda chord=chord: [self.fs.noteoff(CH["strings"], n) for n in chord]
        ).start()

        # Strings2 harmony (fokus & rileks)
        if state in ("focus", "relax"):
            upper = [n + 12 for n in chord[:2]]
            vel2 = int(str_vel * 0.65)
            for n in upper:
                self.fs.noteon(CH["strings2"], n, vel2)
            threading.Timer(dur_sec * 0.85,
                lambda upper=upper: [self.fs.noteoff(CH["strings2"], n) for n in upper]
            ).start()

        # Pad (rileks & kantuk) — catatan: satu oktaf bawah, tapi rileks pakai root saja
        if state in ("relax", "drowsy"):
            if state == "relax":
                # Rileks: hanya root & fifth satu oktaf bawah, velocity sangat lembut
                pad_notes = [chord[0] - 12, chord[2] - 12] if len(chord) >= 3 else [chord[0] - 12]
                pad_vel = 38
            else:
                pad_notes = [n - 12 for n in chord]
                pad_vel = 45
            for n in pad_notes:
                self.fs.noteon(CH["pad"], n, pad_vel)
            threading.Timer(dur_sec,
                lambda pad_notes=pad_notes: [self.fs.noteoff(CH["pad"], n) for n in pad_notes]
            ).start()

        # Choir (rileks, sangat subtle — hanya sesekali)
        if state == "relax" and random.random() < 0.15:
            for n in chord[:2]:
                self.fs.noteon(CH["choir"], n, 32)
            threading.Timer(dur_sec * 0.8,
                lambda chord=chord: [self.fs.noteoff(CH["choir"], n) for n in chord[:2]]
            ).start()

    def _play_ostinato(self, eeg: EEGState):
        """String ostinato — 16th note run, kunci dari suara fokus Zimmer."""
        step = self._ost_step % len(FOCUS_OSTINATO)
        n = FOCUS_OSTINATO[step]

        # Humanize sedikit — velocity variatif tapi teratur
        accent = step % 4 == 0  # aksen di beat
        vel_base = 70 + int(self._build_level * 35)
        vel = vel_base + (15 if accent else 0) + random.randint(-5, 5)
        vel = max(40, min(127, vel))

        dur_sec = (60.0 / self._bpm) / 4.0 * 0.8  # sedikit staccato
        self.fs.noteon(CH["melody"], n, vel)
        threading.Timer(dur_sec,
            lambda: self.fs.noteoff(CH["melody"], n)
        ).start()

    def _play_bass(self, state: str):
        progs = {
            "focus":  FOCUS_PROGRESSIONS,
            "relax":  RELAX_PROGRESSIONS,
            "stress": STRESS_PROGRESSIONS,
            "drowsy": DROWSY_PROGRESSIONS,
        }
        chord = progs[state][self._chord_idx % len(progs[state])]
        bass_note = chord[0] - 12  # root, satu oktaf bawah
        bass_note = max(24, bass_note)  # jangan terlalu rendah

        vel = {
            "focus":  int(75 + self._build_level * 25),
            "relax":  60,
            "stress": min(127, int(95 + self._stress_build * 22)),
            "drowsy": 45,
        }.get(state, 70)

        # Stres: staccato pendek (seperti pulse bass game)
        if state == "stress":
            dur_sec = (60.0 / self._bpm) * 0.18
        else:
            dur_sec = (60.0 / self._bpm) * 0.8
        self.fs.noteon(CH["bass"], bass_note, vel)
        threading.Timer(dur_sec,
            lambda: self.fs.noteoff(CH["bass"], bass_note)
        ).start()

    def _play_brass_swell(self):
        """Brass masuk perlahan saat tension build-up tinggi."""
        if self._build_level < 0.4:
            return
        chord = FOCUS_PROGRESSIONS[self._chord_idx % len(FOCUS_PROGRESSIONS)]
        upper = [n + 7 for n in chord[:2]]  # fifth up
        vel = int(40 + self._build_level * 55)
        dur_sec = (60.0 / self._bpm) * 2.0

        for n in upper:
            if 0 <= n <= 127:
                self.fs.noteon(CH["brass"], n, vel)
        threading.Timer(dur_sec,
            lambda upper=upper: [self.fs.noteoff(CH["brass"], n) for n in upper]
        ).start()

    def _play_melody_fill(self, state: str, eeg: EEGState):
        """Melody filler untuk rileks/kantuk — sesekali saja."""
        # Probabilitas berbeda: rileks lebih aktif, kantuk sangat jarang
        prob = {"relax": 0.65, "drowsy": 0.30}.get(state, 0.55)
        if random.random() > prob:
            return
        scales = {
            # Rileks: C major pentatonic + 7th — lapang, damai
            "relax": [note("C5"), note("E5"), note("G5"), note("A5"),
                      note("B5"), note("D5"), note("C6")],
            # Kantuk: lebih rendah dan sedikit — berat, mengantuk
            "drowsy": [note("C4"), note("G4"), note("B4"),
                       note("D5"), note("F4")],
        }
        scale = scales.get(state, scales["relax"])
        n = random.choice(scale)

        # Kantuk: pelan dan panjang (dreamy sustain), rileks: medium
        if state == "drowsy":
            vel = random.randint(28, 48)
            dur_sec = (60.0 / self._bpm) * random.choice([1.5, 2.0, 3.0])
        else:
            vel = random.randint(45, 68)
            dur_sec = (60.0 / self._bpm) * random.choice([0.5, 1.0, 1.5, 2.0])

        ch = CH["choir"] if state == "drowsy" else CH["melody"]
        self.fs.noteon(ch, n, vel)
        threading.Timer(dur_sec, lambda: self.fs.noteoff(ch, n)).start()

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

    def _play_drowsy_breathing(self):
        """Efek 'nafas' pada pad/choir — swell perlahan masuk dan keluar."""
        # CC 11 (Expression) bersiklus menggunakan gelombang cosinus
        phase = (self._tick % 48) / 48.0          # siklus ~3 bar
        expr = int(40 + 55 * (0.5 - 0.5 * math.cos(2 * math.pi * phase)))
        self.fs.cc(CH["pad"], 11, expr)
        self.fs.cc(CH["choir"], 11, expr)

    def _play_focus_climax(self):
        """Melodi choir ascending saat build sangat tinggi (>0.7) — klimaks."""
        if self._build_level < 0.7:
            return
        step = (self._tick // 4) % len(FOCUS_CLIMAX_PHRASE)
        n = FOCUS_CLIMAX_PHRASE[step]
        vel = int(55 + self._build_level * 45)   # 55–100
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
        """Fokus tension naik perlahan, turun lambat saat state berubah."""
        if state == "focus":
            self._build_level = min(1.0, self._build_level + 0.005)
        else:
            self._build_level = max(0.0, self._build_level - 0.003)

        if state == "stress":
            self._stress_build = min(1.0, self._stress_build + 0.005)
        else:
            self._stress_build = max(0.0, self._stress_build - 0.003)

        if state != self._prev_state and self._prev_state is not None:
            self._on_state_change(self._prev_state, state)
        self._prev_state = state

    def _on_state_change(self, prev: str, curr: str):
        """Transisi antar state — flush note yang sedang berbunyi."""
        print(f"  → state berubah: {prev} → {curr}")
        # Bersihkan channel melodis agar tidak ada note stuck
        self._all_notes_off(channels=[CH["melody"], CH["choir"], CH["glitch"]])
        if prev == "stress":
            self._all_notes_off()   # Cut total dari stress — transisi bersih
        # Ganti instrumen melody sesuai karakter state
        self._switch_instruments(curr)

    def _switch_instruments(self, state: str):
        """Ganti instrumen channel melody sesuai state.

        Menggunakan pad/synth agar cocok dengan VintageDreamsWaves maupun GM orkestra.
        VintageDreamsWaves: pad (88-95) dan synth lead (80-87) adalah strong points-nya.
        GM orkestra (GeneralUser GS, FluidR3): semua GM number akan berbunyi natural.
        """
        melody_instruments = {
            "focus":  GM["synth_lead"],  # 80 – synth lead square: tegang, driving
            "relax":  GM["pad_warm"],    # 89 – warm pad: lembut, mengalir
            "stress": GM["synth_lead"],  # 80 – harsh synth: agresif
            "drowsy": GM["pad_bowed"],   # 92 – bowed pad: dreamy, mengambang
        }
        prog = melody_instruments.get(state, GM["pad_warm"])
        self.fs.program_select(CH["melody"], self.sfid, 0, prog)
        # Rileks: pad dan choir pakai warm pad (bukan choir pad yang terdengar horor)
        if state == "relax":
            self.fs.program_select(CH["pad"],   self.sfid, 0, GM["pad_warm"])
            self.fs.program_select(CH["choir"], self.sfid, 0, GM["pad_warm"])
        elif state == "drowsy":
            self.fs.program_select(CH["pad"],   self.sfid, 0, GM["pad_choir"])
            self.fs.program_select(CH["choir"], self.sfid, 0, GM["pad_choir"])

    def _update_bpm(self, state: str, eeg: EEGState):
        target_bpm = {
            "focus":  80 + self._build_level * 15,   # 80→95 seiring build
            "relax":  55 + eeg.alpha * 10,            # 55→65 (lebih rileks)
            "stress": 110 + eeg.beta * 18 + self._stress_build * 22,  # 110→150 seiring build
            "drowsy": 45 + eeg.theta * 8,             # 45→53 (lebih lambat)
        }[state]
        # Smooth BPM transition
        self._bpm += (target_bpm - self._bpm) * 0.01

    def _update_mixing(self, state: str, eeg: EEGState):
        """Update reverb & volume sesuai state."""
        reverb_params = {
            "focus":  {"roomsize": 0.45, "damping": 0.6, "level": 0.35},  # dry, tight
            "relax":  {"roomsize": 0.75, "damping": 0.5, "level": 0.55},  # warm hall
            "stress": {"roomsize": 0.10, "damping": 0.8, "level": 0.10},  # almost dry
            "drowsy": {"roomsize": 0.85, "damping": 0.3, "level": 0.65},  # deep, floaty
        }[state]
        self.fs.set_reverb(
            roomsize=reverb_params["roomsize"],
            damping=reverb_params["damping"],
            width=0.8,
            level=reverb_params["level"]
        )

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
    build = engine._build_level
    bpm   = engine._bpm

    state_colors = {
        "focus":  "\033[35m",   # magenta
        "relax":  "\033[32m",   # green
        "stress": "\033[31m",   # red
        "drowsy": "\033[33m",   # yellow
    }
    RESET = "\033[0m"
    BOLD  = "\033[1m"
    col   = state_colors.get(state, "")

    bar = lambda v, w=20: "█" * int(v * w) + "░" * (w - int(v * w))

    os.system("clear")
    print(f"{BOLD}╔══════════════════════════════════════╗{RESET}")
    print(f"{BOLD}║     EEG MUSIC ENGINE  —  v1.0        ║{RESET}")
    print(f"{BOLD}╚══════════════════════════════════════╝{RESET}")
    print()
    print(f"  STATE   {col}{BOLD}{state.upper():10}{RESET}   BPM: {bpm:.1f}")
    print()
    print(f"  alpha   {bar(eeg.alpha)}  {eeg.alpha:.2f}")
    print(f"  beta    {bar(eeg.beta)}  {eeg.beta:.2f}")
    print(f"  theta   {bar(eeg.theta)}  {eeg.theta:.2f}")
    if state == "focus":
        print(f"\n  build   {bar(build)}  {build:.2f}  {'🔥' * int(build * 5)}")
    print()
    print("  ─────────────────────────────────────")
    print("  Preset:  1=rileks  2=fokus  3=stres  4=kantuk")
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
                engine.set_eeg(0.85, 0.10, 0.25)
            elif key == "2":
                engine.set_eeg(0.10, 0.90, 0.08)
            elif key == "3":
                engine.set_eeg(0.05, 0.85, 0.80)
            elif key == "4":
                engine.set_eeg(0.20, 0.08, 0.90)
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
