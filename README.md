# Brainwave Monitor

> Real-time EEG mental state monitor with a generative drum engine for live game streaming.

Uses the **Muse 2** as a Passive BCI — the streamer's brain state (calm vs tense) is displayed as a live OBS overlay and drives generative drum patterns automatically.

---

## How It Works

```
Muse 2 (Bluetooth BLE)
    ↓  muselsl + pylsl — EEG 256Hz, PPG 64Hz
    ↓  auto-reconnect with backoff (3s → 5s → 10s → 15s) on drop
BrainFlow DataFilter — PSD Welch, band power θ/α/β
    ↓  2-pass EMG rejection (frontal + temporal)
    ↓  Mental Command detection (3 gestures — see below)
    ↓  Rolling normalization p10–p90 + EMA 0.20
Mental State Classifier — arousal = 0.50β − 0.30α − 0.20TBR
    ↓  flow_score = frontal_α + frontal_θ − β  (AF7/AF8)
    ↓  spectrum_pos 0..1 → calm / flow / tense zones
    ↓  Adaptive threshold (60s warm-up) + 20-tick vote buffer (70% supermajority)
Brainwave Monitor — FluidSynth GM ch9
    ↓  CALM: brush jazz (55–65 BPM)
    ↓  FLOW: groove mid-tempo (72–85 BPM)
    ↓  TENSE: battle drums (95–135 BPM)
Flask-SocketIO (port 8765)
    ↓  /                        → BrainWave Monitor overlay (OBS Browser Source)
    ↓  /overlay/mental-command  → Mental Command Playground (3-command demo overlay)
    ↓  wink / jaw_clench / eyebrow_raise events → full-screen visual FX per command
```

---

## Features

### Mental Command Playground — 3 Active Commands

Three distinct gestures trigger a full-screen visual FX overlay at `/overlay/mental-command`, each using different electrodes and signal modalities:

| Command | Gesture | Channel | Detection Logic |
|---|---|---|---|
| **A — Wink** | Kedip satu mata | AF7 / AF8 | Strong side >800µV, asymmetry ratio >3.5×, weak side 10–150µV (unilateral EOG) |
| **B — Jaw Clench** | Katupkan rahang | TP9 / TP10 | EMG p2p >520µV, sustained ≥2 consecutive ticks (~0.5s) |
| **C — Eyebrow Raise** | Angkat alis | AF7 / AF8 | Both >300µV, symmetric ratio <3.0, sustained ≥2 ticks — reflex blinks filtered |

**Key design insight**: all three use fundamentally different signal dimensions — wink uses *left-right asymmetry*, jaw clench uses *dedicated temporal channels*, eyebrow raise uses *bilateral symmetry + sustained duration*. A global mutex (`_last_cmd_time`, 1.5s) blocks all detectors after any command fires, preventing cross-triggering in both directions. Observed accuracy: ~85–90%.

To test without a Muse: open `/overlay/mental-command` and press **Shift+1**, **Shift+2**, **Shift+3**.

### Mental State Detection

The system classifies three states in real-time via a continuous spectrum:

```
0.0 ────────── 0.35 ──[FLOW ZONE]── 0.65 ────────── 1.0
  calm                   flow                  tense
```

| State | EEG Signature | Drum Character | BPM |
|---|---|---|---|
| **CALM** | Alpha/theta dominant | Brush jazz — ride 8th, side stick 2&4, minimal kick | 55–65 |
| **FLOW** | Frontal alpha+theta high, beta low | Groove — closed hi-hat 8th, solid snare 2&4, active kick | 72–85 |
| **TENSE** | Beta dominant | Battle drums — constant 16th hi-hat, double kick, punchy snare | 95–135 |

State badge and drum engine are both driven by `spectrum_pos` — always in sync.

As tense duration increases, `tense_level` builds from 0 → 1, gradually escalating drum intensity (double-time kick, tom fills) within the TENSE state itself.

**Arousal index:**
```
arousal    = 0.50 × beta − 0.30 × alpha − 0.20 × TBR
flow_score = frontal_alpha + frontal_theta − beta  (AF7/AF8 only)
```

### Adaptive Threshold

The threshold is not hardcoded — it self-calibrates each session:

| Phase | Duration | Behavior |
|---|---|---|
| Warm-up | First 60 seconds | Collects arousal distribution, default threshold `-0.05` |
| Calibration | After 60s | `threshold = median(buffer) + 0.02` |
| Stable | Rest of session | Threshold stays fixed |

The `+0.02` bias nudges the threshold slightly toward calm to avoid over-sensitivity to minor arousal fluctuations.

### 2-Pass EMG Rejection

Facial muscle artifacts (EMG) are the biggest source of false positives in frontal EEG:

- **Pass 1** — pre-scan AF7/AF8: if amplitude > 150µV or high-freq ratio > 0.80 → `frontal_emg = True`
- **Pass 2** — if `frontal_emg`: beta from TP9/TP10 is also blanked (volume conduction)
- Beta is capped at 13–25 Hz (not 30 Hz) to avoid EMG contamination from jaw muscles

### State Smoothing

A 20-tick vote buffer (70% supermajority) prevents the state from flickering:
- Each tick votes `calm`, `flow`, or `tense` based on `spectrum_pos`
- State only changes when ≥ 70% of the last 20 ticks agree on the new state
- Buffer window = ~2–4 seconds depending on BPM

---

### Auto-Reconnect

If the Muse 2 connection drops (out of range, BLE hiccup), the connector retries automatically:

| Attempt | Delay |
|---|---|
| 1st retry | 3 seconds |
| 2nd retry | 5 seconds |
| 3rd retry | 10 seconds |
| 4th+ | 15 seconds |

The UI shows an orange dot and `🔄 Reconnecting...`. Manual disconnect cancels the loop.

## Overlay UI (OBS Browser Source)

`http://localhost:8765` — transparent background, ready to use as an OBS Browser Source.

```
┌─────────────────────────────────────────────────────┐
│  ● BRAINWAVE MONITOR    FluidSynth · Realtime      │
├─────────────────────────────────────────────────────┤
│  CALM                          ♥ 68 bpm             │
│  Calm / Relaxed                Scene 1 by brain     │
│                                signal  ← (2.5s)     │
│  CALM ──⬡ FLOW ZONE──────── TENSE                   │
│        ●                                            │
│  relaxed    engaged calm    aroused                 │
├──────────────────────┬──────────────────────────────┤
│  EEG CHANNELS        │  EEG CHANNEL MAP             │
│  θ theta  4–8Hz  ~~  │   ○ AF7    AF8 ○             │
│  α alpha  8–13Hz ~~  │                              │
│  β beta  13–25Hz ~~  │   ○ TP9   TP10 ○             │
└──────────────────────┴──────────────────────────────┘
```

**UI elements:**
- **STATE** — CALM / FLOW / TENSE badge with color (green / yellow / purple)
- **HR** — heart rate from Muse 2 PPG, top-right of state row
- **Mental command trigger** — appears below HR for 2.5s when a brain signal fires: `Scene 1 by brain signal` (green). Hidden when idle.
- **Spectrum slider** — cursor tracks `spectrum_pos` (0=calm → 1=tense) across gradient bar
- **EEG Channel Map** — SVG head diagram, electrode color = signal quality (green/yellow/red/grey)
- **Waveform** — rolling θ/α/β canvas with spectral centroid Hz per band
- **Reconnecting dot** — orange pulsing dot when auto-reconnect is in progress

**Mental Command Playground** (`http://localhost:8765/overlay/mental-command`):
- Full-screen command demo overlay with 3 distinct color schemes per command
- Command A (Wink) — cyan, Command B (Jaw Clench) — orange, Command C (Eyebrow Raise) — green
- Dev test: **Shift+1 / Shift+2 / Shift+3** to trigger each command without Muse

---

## Stack

| Component | Library | Role |
|---|---|---|
| EEG Acquisition | muselsl + pylsl | BLE stream from Muse 2 via LSL |
| Signal Processing | BrainFlow DataFilter | PSD Welch, band power computation |
| Audio Engine | FluidSynth + pyfluidsynth | Drum rendering via GM Soundfont |
| Web Server | Flask + Flask-SocketIO | WebSocket bridge Python → Browser |
| OBS Integration | obsws-python | WebSocket v5 scene switching |
| Overlay UI | HTML/CSS/JS | OBS Browser Source |

**Install dependencies:**
```bash
pip install muselsl pylsl brainflow pyfluidsynth flask flask-socketio numpy obsws-python
brew install fluid-synth   # macOS
```

---

## Running

```bash
# Use Terminal.app (not VS Code terminal — the process gets killed on idle)
python3 eeg_server.py
```

Open browser: `http://localhost:8765`

In OBS: add a **Browser Source** → URL `http://localhost:8765` → check *Shutdown source when not visible*.

---

## Device: Muse 2 (InteraXon)

```
        FRONT
   AF7        AF8    ← Frontal  (alpha dominant, EMG-prone)
TP9                TP10  ← Temporal (cleaner beta signal)
        BACK
```

| Spec | Detail |
|---|---|
| EEG channels | 4 (TP9, AF7, AF8, TP10) @ 256 Hz |
| Additional sensors | PPG (HR), accelerometer, gyroscope |
| Connectivity | Bluetooth Low Energy |
| Raw EEG access | via muselsl (open source) |

> No C3/C4 electrodes — Motor Imagery is not feasible. Band Power Classification for mental state monitoring is the right paradigm for this device.

---

## Status

| Component | Status |
|---|---|
| Muse 2 BLE acquisition (muselsl + pylsl) | ✅ |
| Band power θ/α/β + EMG rejection | ✅ |
| Adaptive threshold + vote buffer | ✅ |
| Heart rate via PPG | ✅ |
| Brainwave Monitor drum engine (FluidSynth) | ✅ |
| OBS overlay UI (index.html) | ✅ |
| Eyebrow raise detection + overlay FX | ✅ |
| Mental Command Playground (3 commands) | ✅ |
| Wink detection (unilateral EOG asymmetry) | ✅ |
| Jaw clench detection (temporal EMG sustained) | ✅ |
| OBS scene switching via mental commands | ✅ |
| Auto-reconnect with backoff | ✅ |
| ML classifier (SVM/LDA) | 🔲 planned |
