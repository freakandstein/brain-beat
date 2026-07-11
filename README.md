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
    ↓  Mental Command detection (7 gestures — see below)
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
    ↓  /overlay/mental-command  → Mental Command Playground (7-command demo overlay)
    ↓  wink_left / wink_right / jaw_clench / eyebrow_raise / double_jaw / tilt_left / tilt_right events → full-screen visual FX per command
Cursor Control Mode (optional, toggle in UI)
    ↓  Muse 2 accelerometer/gyroscope — head-tilt joystick moves the OS mouse cursor
    ↓  jaw clench re-purposed as left-click while this mode is ON (scene switching disabled)
    ↓  mutually exclusive with tilt_left/tilt_right (both use the same IMU signal)
```

---

## Features

### Mental Command Playground — 7 Active Commands

Seven distinct gestures trigger a full-screen visual FX overlay at `/overlay/mental-command`, each using different electrodes/sensors and signal modalities:

| Command | Gesture | Channel/Sensor | Detection Logic |
|---|---|---|---|
| **A1 — Wink Left** | Kedip mata kiri | AF7 dominant | Strong side >thr_wink (adaptive, default 800µV), asymmetry ratio >2.0×, weak side (AF8) 1–400µV, AF7 ≥ AF8 |
| **A2 — Wink Right** | Kedip mata kanan | AF8 dominant | Same as Wink Left but AF8 > AF7 (`_wink_eye` picks the dominant channel) |
| **B — Jaw Clench** | Katupkan rahang | TP9 / TP10 | EMG envelope (RMS, ~300ms tail) >thr_jaw (adaptive, default 520µV), rising-edge triggered, fires ~1.5s after release (composer decide window) |
| **C — Eyebrow Raise** | Angkat alis | AF7 / AF8 | Both channels valid + bilateral (both >thr_eyebrow adaptive, ratio <3.0), sustained ≥3 ticks, tolerant of 1 isolated noisy tick |
| **D — Double Jaw Clench** | Katupkan rahang 2× cepat berurutan | TP9 / TP10 | Same detector as Jaw Clench, but counts 2 rising edges within the 1.5s composer decide window (`GestureComposer`) instead of 1 — toggles OBS recording start/stop instead of a scene switch |
| **E — Tilt Left** | Miringkan kepala ke kiri (telinga → bahu), lalu kembali tegak | Accelerometer + gyroscope | Quick tilt-and-release, roll-axis-only (see below) |
| **F — Tilt Right** | Miringkan kepala ke kanan (telinga → bahu), lalu kembali tegak | Accelerometer + gyroscope | Same detector, opposite sign of the calibrated axis |

**Key design insight**: all gestures use fundamentally different signal dimensions — wink uses *left-right asymmetry*, jaw clench uses *dedicated temporal channels*, eyebrow raise uses *bilateral symmetry + sustained duration*, tilt uses *IMU motion* (orthogonal to all EMG-based commands). A global mutex (`_last_cmd_time`, 1.5s) plus per-pair cooldown guards (up to 5s) prevent cross-triggering. Observed accuracy: ~90%.

**Wink left vs right**: the wink detector already computed which channel dominates (`_wink_eye = "left" if AF7 ≥ AF8 else "right"`) for logging, but originally fired a single generic `on_wink` callback regardless of side. It now dispatches to `on_wink_left` or `on_wink_right` based on `_wink_eye`, so the two sides are independent commands with their own overlay color and OBS scene mapping — no change to the underlying asymmetry detection itself.

**Wink unilateral range widened (1–400µV, from 10–300µV)**: real session logs showed wink left (AF7 dominant) repeatedly failing — the weak side (AF8) kept landing either near 0µV (treated as channel dropout, rejected) or just over 300µV (treated as "both sides active," rejected as near-eyebrow). The valid window rarely matched the gesture in practice. Widening it to 1–400µV let genuine unilateral winks (including near-zero weak-side readings, which are just a fully quiet channel, not a dropout) register reliably.

**Eyebrow raise: 3-tick sustained with 1-tick noise tolerance**: the original rule (3 consecutive bilateral ticks, reset to 0 on any single miss) almost never fired in practice — logs showed the streak repeatedly reaching 2 and dropping before reaching 3 due to one noisy tick. Lowering the requirement to 2 ticks fixed that but made the detector pick up brief microexpressions/twitches (~300ms) that aren't deliberate eyebrow raises — those fired with very strong, clearly bilateral amplitude (1000–2000µV vs an 80–400µV threshold), so the problem wasn't sensitivity to amplitude, it was sensitivity to *duration*. The fix keeps the 3-tick requirement but only resets the streak after 2 consecutive failed ticks (not 1), so an isolated noisy tick mid-gesture doesn't restart the count — genuine sustained raises still pass, brief twitches mostly don't. The jaw-artefact guard (temporal EMG high, or recent jaw cooldown) still resets the streak immediately with no tolerance — that boundary stays strict.

**Jaw clench envelope (RMS, short tail)**: the temporal EMG envelope (`_full_max`) is computed from the RMS of only the last ~300ms of the filtered signal, not the full 2s analysis window. Measuring it over the full window made a single clench spike keep the envelope elevated for up to 2 seconds after the jaw was actually released (the spike just sat inside the rolling window), which delayed single-jaw firing well past the intended decide window and made the second clench of a double-jaw attempt land while the detector still thought the jaw was clenched — collapsing every double jaw into a single. The short RMS tail tracks the real-time muscle state instead.

**Double jaw composer**: jaw clench rising edges are reported to `GestureComposer`, which restarts a decide-timer on every release. If a second clench edge arrives before the timer expires, it fires `double_jaw`; otherwise it falls back to a single `jaw_clench`. This is why single jaw clench has a perceived delay equal to the decide window — it's the time during which a second clench would still count as a double.

**Double jaw → OBS recording toggle**: unlike the other commands, `double_jaw` doesn't switch an OBS scene. `eeg_server.py`'s `_double_jaw_cb` calls `obs_connector.toggle_record()`, which queries OBS's actual recording state (`get_record_status().output_active`) and calls `start_record()` or `stop_record()` accordingly — so the first double jaw clench starts recording, the next one stops it.

**Adaptive EMG threshold**: during the first ~15 seconds of each session, the connector measures resting EMG noise on frontal (AF7/AF8) and temporal (TP9/TP10) channels. Thresholds are computed as `median_baseline × multiplier` and clamped to a safe range, replacing the hardcoded defaults for the rest of the session. Printed to terminal as `✅ EMG calibration done`.

**Tilt Left/Right — quick tilt-and-release, IMU-based (not EEG)**: a head "roll" gesture — tilting the ear toward the shoulder and back to upright — detected from the Muse 2's accelerometer + gyroscope, orthogonal to every EMG-based command above (no shared electrode, no shared sensor). Runs its own **multi-sample calibration** automatically once per connect (browser banner prompts "tilt right, repeat 3×"): the user repeats a tilt-right motion until 3 *consistent* samples are collected (cosine similarity ≥0.85 to the running average) — samples that are too weak or point in an inconsistent direction are silently discarded and re-requested, with no time limit, because early versions that accepted a single reference tilt (or capped retries) produced an unstable calibrated axis/direction that varied session to session. See `BRAINWAVE_MONITOR.md` → "Command E/F — Tilt Left / Tilt Right" for the full detection state machine (axis-dominance guard, direction/re-arm logic, auto re-centering).

To test without a Muse: open `/overlay/mental-command` and press **Shift+1** through **Shift+7**.

**Custom keymap**: each command can be remapped to any OS keystroke (including modifier combos like `cmd+r`) from the keymap panel on `/overlay/mental-command` — click a command's key button, then press the combo. Saved to `keymap.json` and sent via `pynput` (`keyboard_connector.py`) whenever that command fires. Tilt Right defaults to `cmd+b` (keystroke only, no OBS scene); Tilt Left defaults to the same OBS scene as Wink Right.

### Cursor Control Mode (Head-Tilt Joystick)

An optional mode that repurposes the Muse 2's onboard accelerometer/gyroscope (previously unused) to move the OS mouse cursor by tilting the head, with jaw clench as left-click. Toggled from a button in the main UI — **off by default**, and mutually exclusive with the jaw-clench mental command *and* the Tilt Left/Right commands above (same IMU signal — Tilt Left/Right detection is fully disabled while this mode is ON):

| Mode | Jaw clench behavior | Head tilt |
|---|---|---|
| **OFF** (default) | OBS scene switch + keystroke (unchanged) | Tilt Left/Right commands active (quick tilt-and-release) |
| **ON** | Left-click at current cursor position (scene/keystroke suppressed) | Moves the cursor — velocity/joystick style; Tilt Left/Right commands suppressed |

**Velocity-based control, not absolute position**: tilt angle maps to cursor *speed* in that direction (dead-zone + gentle exponential curve up to a clamped max speed), not to an absolute screen position. A level head means zero velocity — the cursor simply stops wherever it is, with no drift.

**3-stage explicit calibration on toggle-ON** (`MuseConnector._run_cursor_calibration`), each stage waiting for the accelerometer to prove *stable* (low variance) before advancing, not a fixed timer:
1. **Neutral** — hold head still → baseline accel vector recorded.
2. **Right** — tilt head right and hold → deviation from baseline recorded as the "right" axis.
3. **Up** — tilt head up and hold → deviation from baseline recorded as the "up" axis, then **Gram-Schmidt orthogonalized** against the "right" axis.

Explicit calibration (rather than assuming a fixed accelerometer axis maps to roll/pitch) was necessary because the Muse 2's physical chip orientation inside the headband isn't documented, and real session logs showed one axis capturing a mix of roll+pitch depending on how the headset sits — a hardcoded axis mapping produced weak/wrong-direction movement on one axis. The Gram-Schmidt step was added after logs showed the two calibrated axes weren't perfectly perpendicular (a person can rarely tilt purely sideways without a little pitch bleeding in), which caused pure left-right movement to visibly leak into up-down cursor motion.

**IMU acquisition runs in its own ~50Hz thread** (`_imu_loop`), separate from the main EEG loop (~6.7Hz, intentionally slow for PSD spectral resolution) — driving cursor velocity at the EEG loop's rate made movement visibly choppy (~7 discrete jumps/sec).

**Baseline does not auto-correct during a session**: an earlier drift-correction mechanism (nudging the baseline toward the current position whenever accelerometer variance looked "stable") was removed after real logs showed it couldn't distinguish "head genuinely neutral" from "head held steady at a deliberate tilt" — both look like low variance. It ended up chasing the last-held tilt as the new "neutral," making the cursor's true center wander continuously. If the baseline drifts from a posture change mid-session, the fix is to toggle the mode off and back on (re-run calibration), not automatic correction.

Safety: toggling ON force-recalibrates every time (no stale baseline carries over), and disconnecting the Muse always force-disables the mode and zeroes cursor velocity.

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
- **Footer controls** (below the main card) — **Mute** button (toggles drum output via MIDI CC7 channel volume) and **Cursor Control** button (toggles head-tilt cursor mode, walks through calibration instructions in-place: "hold still" → "tilt right" → "tilt up" → "Cursor: ON", blinking blue while active)

**Mental Command Playground** (`http://localhost:8765/overlay/mental-command`):
- Full-screen command demo overlay with 5 distinct color schemes per command
- Command A1 (Wink Left) — cyan, Command A2 (Wink Right) — pink, Command B (Jaw Clench) — orange, Command C (Eyebrow Raise) — green, Command D (Double Jaw Clench) — amber, combo badge
- Dev test: **Shift+1** through **Shift+5** to trigger each command without Muse

---

## Stack

| Component | Library | Role |
|---|---|---|
| EEG Acquisition | muselsl + pylsl | BLE stream from Muse 2 via LSL (EEG + PPG + ACC + GYRO) |
| Signal Processing | BrainFlow DataFilter | PSD Welch, band power computation |
| Audio Engine | FluidSynth + pyfluidsynth | Drum rendering via GM Soundfont |
| Web Server | Flask + Flask-SocketIO | WebSocket bridge Python → Browser |
| OBS Integration | obsws-python | WebSocket v5 scene switching + recording start/stop toggle |
| Cursor Control | pynput.mouse | Head-tilt joystick cursor movement + jaw-clench left-click |
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
| Mental Command Playground (7 commands) | ✅ |
| Wink left/right detection (unilateral EOG asymmetry, split by dominant channel) | ✅ |
| Jaw clench detection (temporal EMG, RMS short-tail envelope) | ✅ |
| Double jaw clench detection (GestureComposer edge counting) | ✅ |
| Tilt left/right detection (IMU quick tilt-and-release, multi-sample calibration) | ✅ |
| OBS scene switching via mental commands | ✅ |
| OBS recording start/stop toggle via double jaw clench | ✅ |
| Custom keymap per mental command (modifier combos supported) | ✅ |
| Mute/unmute drum output (UI button) | ✅ |
| Auto-reconnect with backoff | ✅ |
| Cursor Control Mode (head-tilt joystick + jaw-clench click) | ✅ |
| ML classifier (SVM/LDA) | 🔲 planned |
