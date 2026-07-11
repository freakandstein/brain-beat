# Brainwave Monitor

Generative drum engine that responds to real-time EEG values (alpha/beta/theta).
Drum patterns change automatically based on mental state: calm (brush jazz) or tense (battle drums).

## Architecture

```
Muse 2 (via muselsl + pylsl) / Simulator
        │
        ▼
  brainflow_connector.py       ← EEG acquisition, EMG rejection, mental command detection
        │                        + ACC/GYRO acquisition (own ~50Hz thread), cursor tilt math
        │
  eeg_engine.py                ← Brainwave Monitor core: FluidSynth drums-only (GM channel 9)
        │
  eeg_server.py                ← Flask + SocketIO bridge (port 8765)
        │                ↘
        │           obs_connector.py    ← OBS WebSocket v5 scene switching
        │           mouse_connector.py  ← Cursor movement/click via pynput.mouse (60Hz mover thread)
        │
  templates/index.html         ← Web UI "BRAINWAVE MONITOR" (OBS overlay) + Cursor Control toggle
  templates/overlay_mental_command.html ← 5-command mental command overlay (/overlay/mental-command)
```

## EEG Bands

| Band | Range | Normalized | Raw display | Dominant condition |
|---|---|---|---|---|
| **θ theta** | 4–8 Hz | 0–1 via p10–p90 | µV² + Hz centroid | Drowsy, meditation |
| **α alpha** | 8–13 Hz | 0–1 | µV² + Hz centroid | Relaxed, eyes closed, flow |
| **β beta** | 13–25 Hz | 0–1 | µV² + Hz centroid | Focused, active thinking, stressed |

> **Delta (δ) removed** — delta is only relevant during deep sleep, not useful for waking state monitoring. Removed from all layers: connector, engine, server, and UI.

> Raw µV² is for waveform display only. State detection uses normalized 0–1 values.
> Beta is capped at 13–25 Hz (not 30 Hz) to avoid EMG contamination from jaw muscles (25–40 Hz).
> **Hz centroid** per band (spectral centroid) is shown in the UI alongside µV² values.

## Installation

### 1. Install FluidSynth
```bash
brew install fluid-synth   # macOS
```

### 2. Install Python dependencies
```bash
pip3 install pyfluidsynth numpy flask flask-socketio obsws-python
```

### 3. Soundfont

The engine will try to auto-download a GM soundfont on first run.
Because GitHub uses Git LFS, auto-download often fails — **manual download recommended**:

```bash
mkdir -p ~/soundfonts
# Download one of these and save as GeneralUser.sf2:
```

- **GeneralUser GS** (~30MB): https://www.schristiancollins.com/generaluser.php
- **MuseScore General** (~200MB): https://ftp.osuosl.org/pub/musescore/soundfont/

Save to `~/soundfonts/GeneralUser.sf2`.

> Without a GM soundfont, the engine falls back to **VintageDreamsWaves** (synth/chiptune).
> All states still work, but the timbre will sound more electronic.

## Running

**Use Terminal.app** (not VS Code terminal — the process gets killed on idle):

```bash
python3 eeg_server.py
```

Open browser: **http://localhost:8765**

## Mental Command Detection

Seven active commands are detected in real-time, each using distinct signal dimensions to avoid cross-triggering.

### Command A1/A2 — Wink Left / Wink Right (`on_wink_left` / `on_wink_right`)

```
Channel      : AF7 (ch1) and AF8 (ch2)
Condition    : strong side > thr_wink (adaptive, default 800µV, clamp 300–1000µV)
Asymmetry    : max / min ratio > 2.0                  (one side dominates — unilateral)
Unilateral   : min(p2p_AF7, p2p_AF8) between 1–400µV  (weak side low = truly unilateral)
Side         : _wink_eye = "left" if p2p_AF7 >= p2p_AF8 else "right"
Guard        : NOT bilateral_eff, NOT during/after eyebrow zone
Cooldown     : 3 seconds
```

Wink left → AF7 dominates → fires `on_wink_left`. Wink right → AF8 dominates → fires `on_wink_right`. These are two independent commands (separate overlay color, separate OBS scene by default), but they share one detector — the underlying asymmetry/unilateral logic that distinguishes a wink from an eyebrow raise is identical, only the dispatch target differs based on `_wink_eye`. The `_wink_unilateral` check (weak channel 1–400µV) is the key separator from eyebrow raise — if both frontal electrodes exceed `thr_eyebrow`, it's treated as eyebrow activity, not a wink. Ratio threshold lowered 3.5 → 2.0 based on real-world data showing genuine wink ratio is typically 1.7–2.2 due to electrode proximity.

**Unilateral range widened from 10–300µV to 1–400µV**: real logs showed wink left (AF7 dominant) consistently failing because the weak side (AF8) landed either near 0µV — rejected as a "dropout," even though a fully quiet channel during a genuine unilateral wink is expected, not an error — or just above 300µV, rejected as "both sides active" (near-eyebrow). The valid window rarely matched what actually happens during a real wink. Lowering the floor to 1µV and raising the ceiling to 400µV fixed both failure modes without touching the asymmetry ratio check, which still does the heavy lifting of separating wink from eyebrow.

### Command B — Jaw Clench (`on_jaw_clench`)

```
Channel    : TP9 (ch0) and TP10 (ch3)
Filter     : 20–100Hz bandpass (Butterworth order 4)
Envelope   : RMS over the last ~300ms of the filtered tail, converted to a
             ptp-equivalent scale (×2√2) — NOT ptp over the full 2s window
Condition  : max(TP9, TP10) > thr_jaw (adaptive, default 520µV, clamp 300–700µV)
Edge       : rising-edge triggered (clench start), reported to GestureComposer
Release    : full_max < thr_jaw × 0.70 → jaw considered released
Guard      : NOT eyebrow_zone AND NOT (frontal_active AND bilateral_eff)
Cooldown   : 4 seconds
```

Masseter EMG is strong and confined to temporal channels — completely separate from frontal (AF7/AF8). Neck/SCM muscle (head turn) also activates TP9/TP10 but cannot be reliably separated by symmetry ratio alone (strong jaw clench is also bilateral). The 4-second cooldown is the primary discriminator: jaw clench is a brief impulse, head turns are sustained but only fire once then lock out.

**Why RMS over a short tail, not ptp over the full window**: the 2-second analysis window (`eeg_win`) is needed elsewhere for spectral resolution, but measuring peak-to-peak envelope over the *entire* window meant a single clench spike kept the envelope elevated for up to 2 seconds after the jaw was actually released (the spike just hadn't scrolled out of the window yet). This made single-jaw-clench feel sluggish (release detection lagged ~2s) and broke double jaw clench entirely — the second clench landed while the detector still thought the jaw was clenched from the first, so the rising edge was never counted. Measuring RMS over just the most recent ~300ms tracks the real-time muscle state instead.

### Command D — Double Jaw Clench (`on_double_jaw`)

```
Detector   : same Jaw Clench detector as Command B (TP9/TP10 EMG envelope)
Composer   : GestureComposer — edge-triggered counting, not a separate detector
Decide delay: 1.5 seconds (DECIDE_DELAY), restarted on every jaw RELEASE (not on clench start)
Outcome    : 1 clench in the window → single jaw_clench
             2+ clenches in the window → double_jaw
Action     : toggles OBS recording (obs_connector.toggle_record()), not a scene switch
```

Every jaw clench rising edge increments a counter in `GestureComposer` and cancels any pending decide-timer (a held clench shouldn't expire mid-clench). The decide-timer is (re)started only on **release** — so however long a clench is held, the "wait for a second clench" window is measured from when the jaw actually relaxes, not from when it tensed. If no second clench edge arrives within `DECIDE_DELAY` (1.5s) of release, the composer fires `on_jaw_clench` (single); if a second edge arrives in time, it fires `on_double_jaw` instead. This is also why a single jaw clench has a perceived delay (currently ~1.5s) before the overlay fires — that's the window during which a second clench would still count as a double. This value has been tuned back and forth (1.0s → 0.6s → 1.5s) — lower values made double jaw feel snappier but require unrealistically fast clench-release-clench timing; the current value favors reliability over speed.

**Double jaw → OBS recording, not a scene**: `on_double_jaw` is wired to `obs_connector.toggle_record()` in `eeg_server.py`, not `switch_scene()`. `toggle_record()` calls `get_record_status()` on OBS to check the *actual* current recording state (`output_active`), then calls `start_record()` or `stop_record()` accordingly — so it stays correct even if the user also starts/stops recording manually from inside OBS between double-jaw triggers. The first double jaw clench in a session starts recording; the next one stops it.

### Command C — Eyebrow Raise (`on_eyebrow_raise`)

```
Channel      : AF7 (ch1) and AF8 (ch2)
Condition A  : AF7 > thr_eyebrow AND AF8 > thr_eyebrow, with max/min ratio < 3.0 (symmetric bilateral)
Condition B  : max(AF7,AF8) > thr_eyebrow×1.67 AND min(AF7,AF8) > thr_eyebrow×0.67 (asymmetric bilateral)
              thr_eyebrow adaptive, default 300µV, clamp 80–400µV
Sustained    : ≥ 3 consecutive ticks, tolerant of 1 isolated non-bilateral tick
              (streak only resets after 2 consecutive failed ticks, not 1)
Cooldown     : 3 seconds
```

**Solo fallback removed** — previously a path allowed AF8 dropout + AF7 > 1200µV to count as eyebrow (electrode lift). This caused cross-fire with left wink (identical signal: AF7 high, AF8 zero). Eyebrow now requires **both channels valid and bilateral**.

**Sustained streak tuning — 3-tick-strict → 2-tick → 3-tick-with-tolerance**: the original rule (3 ticks bilateral, reset to 0 on any single miss) essentially never fired — real logs showed the streak repeatedly building to 2 and dropping to 0 right before reaching 3, because of one noisy tick in the middle of a genuine raise. Lowering the requirement to 2 ticks made eyebrow raise fire reliably, but logs then showed it firing on brief microexpressions/twitches (~300ms, well under a deliberate raise) — those still had strong, clearly bilateral amplitude (1000–2000µV against an 80–400µV threshold), so amplitude wasn't the issue, duration was. The fix: keep the 3-tick requirement, but track consecutive *misses* (`_eyebrow_miss`) separately from the streak, and only reset the streak once 2 misses happen in a row. A single noisy tick mid-gesture no longer wipes out an otherwise-genuine sustained raise, but a brief twitch still can't accumulate 3 ticks fast enough to fire. The jaw-artefact reset (temporal EMG active, or recent jaw cooldown) is exempt from this tolerance — it still resets the streak immediately, since that boundary needs to stay strict to avoid eyebrow "stealing" jaw clench artefacts.

### Command E/F — Tilt Left / Tilt Right (`on_tilt_left` / `on_tilt_right`)

```
Sensor       : accelerometer + gyroscope (IMU), NOT EEG — orthogonal to every
               EMG command above (no shared electrode, no shared sensor)
Signal       : tilt_val = dot(latest_acc - tilt_neutral, tilt_calib_vec)
Rise         : |tilt_val| > 0.11 AND is_roll_dominant() AND moving_away_from_neutral()
Release      : |tilt_val| falls below 0.6 × the peak reached during "risen"
               (relative to that gesture's own peak, not a fixed absolute value)
Window       : rise→release must complete within 0.12s–0.8s (else discarded, no fire)
Side         : "right" if tilt_val > 0 else "left"
Guard        : NOT during Cursor Control Mode (mutually exclusive, same IMU signal)
Cooldown     : 1.5s global mutex + re-arm gate (see below)
```

A head **roll** gesture — tilting the ear toward the shoulder and back upright, *not* turning the head side to side (yaw) or nodding (pitch). Detected in the same `_imu_loop` (~50Hz) that drives Cursor Control Mode, but only when that mode is OFF.

**Why this took many iterations to get right**: unlike the EMG commands above (tuned in a handful of passes), tilt detection went through roughly a dozen real-device debugging cycles because IMU-based gesture detection has failure modes EMG amplitude thresholds don't — direction sign, rotation axis, and motion-vs-position all needed separate fixes. Each subsection below documents one specific bug found from real session logs, in the order they were found, because the fixes build on each other.

**1. Multi-sample calibration (not a single reference tilt)**

Calibration runs once automatically after connect (browser banner: "tilt right, repeat 3× with a relaxed pause between each"). It collects `_CALIB_SAMPLE_COUNT = 3` **separate** successful tilt-right attempts and averages them, rather than trusting a single gesture:

```
for each attempt (no retry limit — keeps going until 3 GOOD samples are collected):
    record accel+gyro continuously for 2.0s (not a single snapshot)
    peak_idx = index of MAXIMUM accel deviation from neutral during that window
    cand_dev, cand_norm = accel deviation, magnitude at peak_idx
    if cand_norm < 0.25 (_MIN_DEV_NORM): discard, retry — motion too weak/unclear
    cand_dir = normalize(cand_dev)
    if collected samples exist:
        cos_sim = dot(cand_dir, normalize(mean(collected directions)))
        if cos_sim < 0.85 (_CALIB_CONSISTENCY_MIN_DOT): discard, retry — inconsistent direction
    accept sample → collected_dirs.append(cand_dir)
    gyro_peak_idx = index of MAXIMUM gyro magnitude during the same 2.0s window
    collected_axes.append(dominant gyro axis in a small window around gyro_peak_idx)

tilt_calib_vec = normalize(mean(collected_dirs))          # average direction
tilt_gyro_axis = mode(collected_axes)                       # majority-vote axis
```

**Why average 3 samples instead of trusting 1**: real testing showed a single reference tilt was too sensitive to per-gesture variance (speed, angle, a slight overshoot on the way back to neutral) — the resulting `tilt_calib_vec` and `tilt_gyro_axis` weren't stable session to session even with visually-identical headset placement and gestures, causing false-positive/false-negative patterns that kept changing shape after each targeted fix. Averaging 3 independently-collected samples is far more resistant to any single sample being unrepresentative.

**Why no retry limit, and why `_MIN_DEV_NORM` was raised 0.08 → 0.25**: an earlier version capped retries and accepted any sample above a low bar (0.08). A real log showed a sample with `norm=0.085` — barely above that bar, essentially noise — get accepted and averaged in alongside two genuine samples, corrupting the final direction. Per explicit user direction ("don't put a time limit on it, keep going until it's actually correct"), the retry cap was removed entirely and the acceptance bar raised to 0.25 (a clearly deliberate tilt), so calibration now takes as long as it takes but only accepts real gestures.

**Why a consistency check on top of the norm check**: raising `_MIN_DEV_NORM` alone doesn't catch a sample that's strong *and* wrong — e.g. a clean but different motion (the same log run that had `norm=0.085` also showed a `dominant_axis` disagreement between samples, `[0, 2, 2]`, meaning one sample was measuring a physically different rotation than the other two). Comparing each new sample's direction against the running average (cosine similarity ≥0.85, ≈32° tolerance) rejects a sample whose direction doesn't match what's already been collected, before it can corrupt the average — while still being loose enough to admit normal human repeat-gesture variance (typically >0.95 cosine similarity for the same real gesture).

**2. Direction (`tilt_calib_vec`) — captured from peak deviation over the full recording window, not a single stable snapshot**

An early version waited for the accelerometer to go *stable* (low variance) then took one snapshot — the same pattern used successfully for Cursor Control Mode's calibration. That approach has a specific failure mode for a quick tilt-and-release gesture: the "stable" window can land exactly on an **overshoot pause** — the user tilts right, swings back past neutral toward left, and pauses there momentarily — which is also low-variance and gets mistaken for the intended position, recording the calibration **backwards**. The fix: record accel continuously for the whole 2-second window and take the point of *maximum* deviation from neutral as the calibration direction — physically, the furthest point from neutral during one deliberate tilt gesture is the peak of that gesture itself; an unintentional overshoot is almost always smaller in amplitude than the deliberate motion that preceded it.

**3. Roll axis (`tilt_gyro_axis`) — captured from peak gyro magnitude, not from the same peak-accel moment**

Even after fixing direction capture, `tilt_gyro_axis` (used by the axis-dominance guard below) kept coming out inconsistent between sessions (sometimes axis 0/X, sometimes axis 2/Z) despite the same headset and gesture. Root cause: the accel-deviation peak (used for direction, above) occurs at the moment the head has **stopped** rotating — the far end of the tilt, where angular velocity is near zero — while the true peak rotation *speed* happens mid-swing, on the way toward that position. Taking a gyro-axis reading centered on the accel peak was therefore sampling near-zero/noisy rotation, not the real motion. The fix searches the same recording window independently for its own point of *maximum gyro magnitude* (not the accel peak's index) and determines the dominant axis from a small window around that point instead.

**4. Axis-dominance guard — rejects yaw/pitch that happens to also cross the tilt threshold**

```
is_roll_dominant():
    roll_mag = |gyro on tilt_gyro_axis|
    if roll_mag < 5.0 dps (_TILT_GYRO_MIN_DPS): reject — rotation too small to judge
    reject unless roll_mag >= 1.15 × (largest gyro reading on any OTHER axis)
```

A pure head turn (yaw) or nod (pitch) can still produce an `accel` deviation that crosses the tilt threshold — the calibrated direction vector isn't perfectly orthogonal to every other possible head motion — but its gyro reading will be dominant on a *different* axis than the calibrated roll axis. This guard checks the live gyro reading at the moment of a candidate rise and rejects it if the dominant rotation isn't on the calibrated roll axis. `_TILT_GYRO_MIN_DPS` was lowered 15.0 → 5.0 dps after logs showed genuine *slow* deliberate tilts (held, not snapped) sometimes have peak angular velocity as low as 3–5 dps — the original 15.0 threshold rejected real slow gestures as "rotation too small." `_TILT_GYRO_DOMINANCE` was lowered 1.5 → 1.15 after logs showed genuine tilts with a dominance ratio as low as ~1.4× (a human head rarely rotates purely around one axis) — 1.5 rejected real gestures; 1.15 was chosen with margin below the observed 1.4× floor since it came from a single data point, not deemed the true minimum.

**5. Direction-of-motion guard — rejects the head returning to neutral, at any speed**

The most persistent false-positive: after any tilt attempt (successful or not), the head returning toward neutral — however slowly — could itself cross the rise threshold on the way past a point where `|tilt_val|` happened to be increasing relative to a moment prior, especially at faster/oscillating return speeds. The system had no notion of *direction of travel*, only instantaneous position. The fix tracks a short rolling window (`_TILT_MOVING_AWAY_WINDOW_N = 5` samples, ~100ms) of `|tilt_val|` and only allows a rise if the current value exceeds the **minimum** value seen in that window plus a small epsilon (`_TILT_MOVING_AWAY_EPS = 0.006`) — i.e., the signal must be trending away from neutral, not toward it. Using the window minimum (not just the immediately-previous tick) makes this tolerant of 1–2 ticks of natural oscillation during a fast genuine tilt (an earlier version compared only against the prior tick and rejected valid fast tilts whose signal dipped for a single tick mid-rise), while still rejecting any sustained trend back toward zero regardless of how slowly it happens.

**6. Relative (not absolute) release threshold**

An early version required `|tilt_val|` to fall back below a small fixed value (`_TILT_CMD_THRESHOLD × 0.5 = 0.055`) to count as "released." Real logs showed strong/fast tilts (peaking well above threshold, sometimes >1.0) essentially never fall back to that fixed value within the 0.8s release window — a large tilt's rebound doesn't snap all the way back to near-zero that fast — so the gesture would enter "risen" correctly (axis and direction guards both passing) and then simply time out unfired. The fix makes release relative to that gesture's own peak: `_TILT_RELEASE_RATIO = 0.6`, so release fires once the signal drops to 60% of whatever peak it reached, proportional for weak and strong tilts alike (an earlier attempt kept the old absolute check as a fallback `OR` condition, but that gave weak tilts an easier release condition than before by accident — replaced with a purely relative check).

**7. Re-arm gate — closes the gap the fixed-duration refractory left open**

```
after a fire:
    tilt_rearmed = False
    blocked until: |tilt_val| is observed below 0.04 (_TILT_REARM_THRESHOLD) at least once,
                   OR 6.0s (_TILT_REARM_TIMEOUT_S) have passed since the fire (hard fallback)
    while un-armed, auto-recentering (see below) still runs, to avoid a deadlock where a
    drifted baseline never lets tilt_val fall low enough to re-arm naturally
```

A fixed 1.5s refractory window (`_TILT_CMD_REFRACTORY_S`) alone wasn't enough: real logs showed the head's physical settle-back-to-neutral motion after a fire is variable in duration, and could still be crossing the *opposite* direction's threshold right as the fixed timer expired (e.g. fire left, then the rebound registers as a right-tilt rise moments later). Re-arming now requires *seeing* the signal actually return near neutral, not just waiting a fixed amount of time.

**8. Auto re-centering — closes long-session baseline drift, without repeating a documented Cursor Control failure**

```
runs only when: state == "idle" AND outside the post-fire refractory window
if gyro_mag < 5.0 dps (_TILT_RECENTER_GYRO_DPS) held for 1.0s (_TILT_RECENTER_HOLD_S):
    tilt_neutral = tilt_neutral × 0.95 + latest_acc × 0.05     (slow EMA, per tick)
```

Cursor Control Mode's own baseline auto-correction was tried and **removed** (see below) because it couldn't distinguish "head genuinely neutral" from "head held steady mid-tilt" — both look like low accelerometer variance over a short window, so the baseline ended up chasing whatever tilt the user happened to be holding. Tilt command detection has a signal Cursor Control's correction didn't use: gyro. A genuinely still head has near-zero rotation on every axis; a head merely *holding* a tilt position only reaches this recentering branch after `_TILT_CMD_RELEASE_MAX_S` (0.8s) has already elapsed without a release, at which point the gesture has already been discarded as "not a quick tilt" by the state machine above — so by the time recentering can run, sustained-hold and genuine-neutral are already the same case by design, not a new ambiguity. Recentering is explicitly skipped during the refractory window after a fire, to avoid a separate feedback loop: if the head hasn't fully settled back to neutral yet and that near-tilted position got recentered as the new "neutral," the just-fired direction would become harder to trigger again and the opposite direction easier — asymmetric drift found during code review, not just theorized.

**Result**: after all 8 fixes above, direction/axis calibration is stable session to session, deliberate tilts of varying speed and strength fire reliably, and head turns/nods/idle drift/return-to-neutral motion don't.

### Adaptive EMG Threshold (Per-Session Calibration)

During the first ~15 seconds of each session (100 ticks @ 6.7 Hz), resting EMG noise is sampled from all channels. Thresholds are computed from the median baseline and clamped to a safe range:

| Threshold | Formula | Clamp |
|---|---|---|
| `thr_eyebrow` | `median_frontal × 3.0` | 80–400µV |
| `thr_wink` | `median_frontal × 5.0` | 300–1000µV |
| `thr_jaw` | `median_temporal × 4.0` | 300–700µV |

Calibration runs for the full 15 seconds regardless of whether commands fire during that window — median is robust to outlier spikes. Printed to terminal as `✅ EMG calibration done — frontal_baseline=XXµV ...`.

### Mutual Exclusion (Global Mutex)

All detectors share a single `_last_cmd_time` timestamp. Once any command fires, a **1.5-second idle window** must pass before any detector can fire again (`_cmd_idle` is checked once per tick before all detectors run).

Additional cross-fire guards (learned from real-world testing):

| Guard | Duration | Blocks |
|---|---|---|
| `_after_eyebrow` | 5 seconds | Wink detector (both sides) |
| `_after_wink` | 4 seconds | Eyebrow detector |
| `_after_jaw` (for eyebrow) | 4 seconds | Eyebrow streak + fire |
| `_eyebrow_active_until` zone | 1.5s after bilateral activity | Wink (both sides) and jaw |
| Tilt fire → global mutex | 1.5s | Wink, eyebrow, jaw (tilt joins the same `_last_cmd_time` mutex, since a strong tilt can jostle electrodes and produce a spurious EMG artifact) |
| Tilt re-arm gate | until signal returns near-neutral or 6.0s | New tilt rise (see Command E/F above — this is in addition to, not instead of, the global mutex) |
| Cursor Control Mode ON | for the whole session it's ON | Tilt left/right entirely (same IMU signal as the cursor joystick) |

### Design Principle

All commands use fundamentally different signal dimensions:
- **Wink left/right** → left-right *asymmetry* on frontal channels (one side active, other silent) — the side is just which channel dominates, not a separate signal dimension
- **Jaw clench** → dedicated *temporal* channels (TP9/TP10), completely separate electrodes
- **Eyebrow raise** → bilateral frontal activation (both AF7 and AF8 rise together)
- **Double jaw clench** → same channels as jaw clench, distinguished purely by *edge count within a timing window* (`GestureComposer`), not a different signal dimension
- **Tilt left/right** → IMU motion (accelerometer + gyroscope), a completely different sensor from every EMG command above — cross-fire with EMG commands is only a concern in the direction of tilt *causing* an electrode artifact, not the reverse, which is why tilt joins the EMG global mutex but EMG commands don't need a tilt-specific guard

The weak-channel boundary (1–400µV) is the key separator between wink and eyebrow: if both sides exceed `thr_eyebrow`, it's bilateral (eyebrow); if only one side is strong with the other below 400µV, it's unilateral (wink). Observed accuracy: ~90% in real-world use.

### Overlay FX

**`/overlay/mental-command`** — 7-command overlay. Each command has its own color:
- Command A1 (Wink Left): cyan
- Command A2 (Wink Right): pink
- Command B (Jaw Clench): orange  
- Command C (Eyebrow Raise): green
- Command D (Double Jaw Clench): amber, with a "COMBO SEQUENCE" badge and longer 4s hold
- Command E (Tilt Left): violet (hue 258) — deliberately a new hue family, distinct from every EMG command, and shows no active channel node (it isn't EEG)
- Command F (Tilt Right): indigo (hue 272) — same rationale as Tilt Left; readout "Power" is labeled in `mg` (milli-g, accelerometer units) instead of `µV`, since it isn't an EMG signal

Dev test: **Shift+1** through **Shift+7**. Single commands auto-hide after 2.8 seconds; double jaw holds for 4 seconds.

## OBS Scene Switching & Recording Control

Mental commands trigger OBS scene changes via WebSocket v5 (`obs_connector.py`).

| Command | Default Scene |
|---|---|
| Wink Left | Scene 1 (2 Views Without Top) |
| Wink Right | Scene 1 (2 Views Without Top) — same default as Wink Left, change independently in `DEFAULT_SCENE_MAP` if needed |
| Jaw Clench | Scene 2 (3 Views) |
| Eyebrow Raise | Scene 3 (2 Views Without Front) |
| Double Jaw Clench | not a scene switch — toggles OBS recording instead (see below) |
| Tilt Left | Scene 1 (2 Views Without Top) — same default as Wink Right |
| Tilt Right | not a scene switch — sends the `cmd+b` keystroke instead (see `keyboard_connector.py` → `DEFAULT_KEYMAP`) |

Scene names can be changed in `obs_connector.py` → `DEFAULT_SCENE_MAP`.

**Double jaw clench → recording toggle**: `OBSConnector.toggle_record()` checks OBS's actual recording state via `get_record_status().output_active`, then calls `start_record()` or `stop_record()` accordingly. Runs in a background thread (non-blocking), with the same reconnect-on-failure behavior as scene switching. First double jaw clench starts recording, the next stops it — independent of `DEFAULT_SCENE_MAP`.

**Setup:**
1. OBS → Tools → WebSocket Server Settings → Enable
2. Set password in `eeg_server.py`: `OBSConnector(password="...")`
3. Make sure scene names in `DEFAULT_SCENE_MAP` match exactly what's in OBS

Connection is established at startup and auto-reconnects if OBS restarts.

## Cursor Control Mode (Head-Tilt Joystick)

Repurposes the Muse 2's accelerometer + gyroscope (previously acquired with `acc_enabled=False, gyro_enabled=False` — now both `True`) to move the OS mouse cursor via head tilt, with jaw clench as left-click. Off by default; toggled from a button in `templates/index.html`.

### Mutual Exclusion With Tilt Left/Right

This mode and the Tilt Left/Right mental commands (see "Command E/F" above) consume the *same* IMU signal for different purposes — one as a continuous joystick, the other as a discrete gesture — so they're strictly mutually exclusive, not just cooperatively guarded: `_imu_loop` calls either `_update_cursor_control()` or `_update_tilt_command()` each tick, never both, based on `cursor_control_enabled`. Toggling Cursor Control ON immediately and unconditionally resets any in-progress tilt gesture state back to idle, so a tilt rise that hadn't fired yet doesn't fire later once the mode switches.

### Mutual Exclusion With Jaw Clench

```
cursor_control_enabled == False (default):
    jaw clench single-fire → OBS scene switch + keystroke (unchanged)
    double jaw clench      → OBS recording toggle (unchanged, in BOTH modes)

cursor_control_enabled == True:
    jaw clench single-fire → mouse_connector.click_left() (scene/keystroke suppressed)
    double jaw clench      → still OBS recording toggle — NOT gated
```

Double jaw clench is deliberately left ungated in both modes: it's orthogonal to what single clench does (starting/stopping a recording is a session-level action a user would plausibly want regardless of cursor state), and gating it would remove the only recording toggle exactly while demoing the cursor feature.

The branch lives inside the existing `_jaw_clench_cb` closure in `eeg_server.py` (checked at fire-time via `muse.cursor_control_enabled`), not by swapping which callback is registered on toggle — simpler and avoids a race between a toggle event and an in-flight clench.

### IMU Acquisition — Separate ~50Hz Thread

ACC/GYRO are pulled in `MuseConnector._imu_loop`, a dedicated thread started alongside the main EEG loop, **not** inside it. The main `_loop` intentionally runs at ~6.7Hz (`time.sleep(0.15)`) — that cadence is needed for Welch PSD spectral resolution on EEG, but it's far too slow for cursor control: driving mouse velocity updates at 6.7Hz produces ~7 visible discrete jumps per second, distinctly choppy. `_imu_loop` runs independently at ~50Hz (close to the Muse 2's native ACC/GYRO rate), reading only the *latest* sample each tick (not a windowed buffer like EEG — a real-time control loop wants the freshest reading, not an average that adds lag).

`mouse_connector.py`'s `MouseConnector` then runs its own ~60Hz mover thread, applying `velocity × dt` as a small increment each tick — decoupling "how often we sense" (50Hz IMU) from "how often we move the cursor" (60Hz), since driving movement only at the IMU's own tick rate would still look slightly stepped.

### 3-Stage Explicit Direction Calibration

Toggling the mode ON does **not** immediately activate cursor movement — it starts `MuseConnector._run_cursor_calibration`, which walks through 3 stages, each waiting for the accelerometer to prove *stable* (low variance over a rolling window) before advancing, rather than a fixed timer:

```
neutral → right → up → ready
```

1. **neutral** — user holds head still; the stable window's mean accel vector becomes `_imu_baseline`.
2. **right** — user tilts head right and holds; the *deviation* from baseline (normalized) becomes `_calib_right_vec`.
3. **up** — user tilts head up and holds; the deviation from baseline becomes a raw "up" vector, which is then **Gram-Schmidt orthogonalized** against `_calib_right_vec` before being stored as `_calib_up_vec`.

`cursor_calib_phase` is broadcast to the UI via `state_update` (and immediately in the `cursor_control_state` response to the toggle event, to avoid a race where the phase-specific instruction text never gets a chance to render before the phase moves on) so the toggle button can show live instructions: "hold still" → "tilt right, hold" → "tilt up, hold" → "Cursor: ON". A `_CALIB_READ_DELAY_S` (1.2s) pause is inserted **before** each stage starts measuring — without it, if the device happened to already be stable from the previous stage, a new stage could pass its stability check before the user even finished reading the new instruction.

**Why explicit calibration, not a hardcoded axis mapping**: two earlier approaches were tried and both failed against real session logs:

1. A generic Gram-Schmidt projection relative to the neutral baseline (no assumption about which physical accelerometer axis is "roll" vs "pitch") — mathematically sound, but the resulting right/up basis wasn't tied consistently to the physical tilt gesture; it depended on which direction the baseline vector happened to point, so roll partially leaked into pitch and vice versa depending on session.
2. A hardcoded index mapping (`X = roll, Y = pitch`, sign flipped by trial and error) — real logs proved this wrong for this device/headset combination: the X axis showed ~3× the variance of Y and a 0.625 correlation with it, meaning the physical chip axis simply isn't aligned with anatomical roll/pitch the way a Muse 2 datasheet-based guess assumed.

Recording the *actual* deviation vectors from a real right-tilt and a real up-tilt sidesteps needing to know the chip's mounting orientation at all — the calibration is correct for whatever orientation the headset happens to be in.

**Why Gram-Schmidt orthogonalization on top of that**: even with real calibration gestures, the two raw vectors aren't perfectly perpendicular in practice — it's anatomically difficult to tilt purely sideways without a little up/down bleeding in (and vice versa). Testing showed the angle between the two raw calibrated vectors could be off from 90° by a large margin, and that leftover non-orthogonality meant a *pure* rightward head movement still registered a non-trivial `tilt_up` component — the reported symptom was "moving left/right also drifts up/down." Explicitly discarding the component of `raw_up_vec` that's parallel to `_calib_right_vec` (then re-normalizing) forces the two axes to be exactly perpendicular, eliminating that cross-talk regardless of how imprecisely the user performed the calibration gestures.

### Tilt-to-Velocity — What Was Tried and Removed

```python
tilt_up, tilt_right = dot(accel_deviation, calibrated_up_vec), dot(accel_deviation, calibrated_right_vec)
speed = (clamp(|tilt| - deadzone, 0, max) / max) ** 1.3 × max_speed   # per axis, sign preserved
```

- **Dead-zone** (~0.025, ~1.5°) below which speed is forced to zero, to absorb accelerometer quantization noise at rest.
- **Exponential curve** (exponent 1.3, not linear or full quadratic) between dead-zone and max tilt — gives fine control near center while still reaching full speed without needing an extreme tilt angle.
- **Gyro-magnitude gate** (>120°/s holds the previous smoothed tilt instead of updating): fast head motion contaminates the accelerometer reading with linear acceleration, not pure gravity, so a sudden gyro spike is treated as "this accel sample is unreliable this tick," not as a real tilt change.

Two additional mechanisms were tried and **removed** after real-device testing surfaced worse problems than they solved:

- **Baseline drift correction** — nudged `_imu_baseline` toward the current accel reading whenever a short-window variance check looked "stable," meant to slowly correct for postural drift across a long session. Removed because the variance check couldn't distinguish "head genuinely neutral" from "head held steady at a deliberately-sustained tilt" (both have low short-term variance) — real logs showed the baseline wandering by over 1g across a single session as it kept chasing whatever position the user happened to hold the cursor at, making "neutral" a moving target and producing exactly the symptom it was meant to prevent (cursor drifting toward one corner).
- **Hysteresis anti-overshoot** — widened the effective dead-zone whenever tilt magnitude was decreasing (returning toward neutral), meant to absorb the small natural overshoot when a person's head passes slightly past center on the way back. Removed because it made the cursor stop responding *before* the head actually reached neutral, and because the "peak" tracking used to decide when to shrink the dead-zone back down didn't decay, so a small natural wobble while trying to settle at neutral could leave the cursor stuck in the widened dead-zone for an extended period — reported as "cursor gets stuck; hard to move it back after hitting one direction." Reverting to a pure per-tick calculation (no history) traded a short, predictable overshoot (~1-2 ticks, ~20-40ms) for eliminating that stuck state entirely.

### Safety

- Toggling ON always re-runs the full 3-stage calibration — no stale baseline/calibration vectors carry over from a previous session.
- Disconnecting the Muse (`MuseConnector.disconnect()`) force-sets `cursor_control_enabled = False` and zeroes velocity — no session can leave the OS cursor drifting after a drop.
- The UI toggle button blinks blue while active — an explicit, hard-to-miss visual indicator, since this feature moves the real system-wide cursor.

## Web UI

The browser UI (`templates/index.html`) is a single consolidated card layout.

**Main Card** — one container with all EEG info:

- **State row (top):** Active state badge (CALM / FLOW / TENSE) with color + description on the left; HR (heart rate from PPG) and mental command trigger on the right
  - Badge has a continuous breathing animation (scale + opacity); cadence follows state — CALM 3.2s, FLOW 2.2s, TENSE 1.4s
  - Main card background has an ambient mood-lighting glow (radial gradient) tinted to the current state color, transitioning over 1.2s on state change
- **Mental command trigger:** appears below HR for 2.5 seconds when a brain signal fires — e.g. `Scene 1 by brain signal` (green). Hidden when idle. Also fires a ripple ring expanding outward from the center of the brain map (Channel Map)
- **Spectrum slider:** CALM ↔ FLOW ZONE ↔ TENSE gradient with cursor tracking `spectrum_pos`
- **EEG Channels + Channel Map (side by side):**
  - Left: 3 rolling waveform canvases θ → α → β, colors: theta=green, alpha=blue, beta=purple; Hz centroid label per band
  - Right: SVG top-down brain illustration with TP9/AF7/AF8/TP10 electrode dots overlaid; dot color = signal quality (green/yellow/red/grey). Dots above weak threshold pulse (scale + glow halo) at a rate proportional to signal quality; off/poor electrodes stay static
  - Neural link line: a dashed arc connects AF7↔AF8 and another connects TP9↔TP10; each arc lights up and animates (flowing dash) only when both electrodes in that pair are simultaneously above the weak-signal threshold

**BCI Device Panel** (below main card)
- Muse 2 connection status, scan + connect/disconnect buttons
- Socket live-dot (header): grey=idle, green=connected, red=disconnected
- Auto-reconnect with backoff (3s → 5s → 10s → 15s); UI shows `🔄 Reconnecting...` during retries

**Mute button** (header, next to Connect)
- Toggles the drum engine's output via `set_mute`/`get_mute` socket events — `eeg_engine.py`'s `set_muted()` sets MIDI CC7 (channel volume) to 0 (muted) or 127 (unmuted) on the drum channel, without stopping/restarting FluidSynth
- Button state (`mute_state` event) syncs on connect and is styled to match `#bci-btn` (red border/background when muted, same as the green "connected" state)

**Keymap panel** (`/overlay/mental-command`)
- Each mental command (wink_left, wink_right, eyebrow_raise, jaw_clench, double_jaw, tilt_left, tilt_right) can be remapped to any OS keystroke, including modifier combos (e.g. `cmd+r`)
- Click a command's key button, then press the desired key combo — captured via a capture-phase `keydown` listener that waits for a non-modifier key before committing (so holding Cmd then pressing R correctly resolves to `cmd+r`, not just `cmd`)
- Saved via `set_keymap` socket event → persisted to `keymap.json` by `keyboard_connector.py`, which sends the keystroke through `pynput` whenever the mapped mental command fires

**Mental Commands** (socket events → UI trigger):
- `wink_left` → Scene 1 by brain signal
- `wink_right` → Scene 1 by brain signal (same default scene as wink_left, configurable independently)
- `jaw_clench` → Scene 2 by brain signal
- `eyebrow_raise` → Scene 3 by brain signal
- `double_jaw` → no scene switch — toggles OBS recording start/stop, and triggers the overlay FX at `/overlay/mental-command`
- `tilt_left` → Scene 1 by head tilt (same default scene as wink_right)
- `tilt_right` → no scene switch — sends `cmd+b` keystroke, and triggers the overlay FX at `/overlay/mental-command`

Compatible with **OBS Browser Source** (stream overlay).

## How It Works

### State Detection

3-class system: `calm` / `flow` / `tense`, driven by `spectrum_pos` (0..1).

```
arousal    = 0.50 × beta − 0.30 × alpha − 0.20 × TBR
flow_score = frontal_alpha + frontal_theta − beta   (AF7/AF8 only)

spectrum_pos: 0.0──────0.35──[FLOW ZONE]──0.65──────1.0
               calm            flow               tense
```

State badge dan drum engine keduanya driven oleh `spectrum_pos` — tidak ada divergensi antara UI dan audio.

**Adaptive threshold** — not hardcoded:
- During the first 60 seconds, the engine **warms up** and collects arousal samples.
- After warm-up: `threshold = median(buffer) + 0.03` — slight bias toward calm.
- Default before calibration: `+0.02`.
- `is_warming_up()` and `get_threshold()` are exposed from the server and shown in the UI.

**Vote buffer** — 20-tick, 70% supermajority to switch state (prevents flip-flop):

| From | To | Requirement |
|---|---|---|
| any | flow | ≥ 70% flow votes |
| any | tense | ≥ 70% tense votes |
| any | calm | ≥ 70% calm votes |

### Drum Patterns per State

**CALM** — brush jazz, sparse (55–65 BPM)
```
Ride      : [1,0,1,0, 1,0,1,0, 1,0,1,0, 1,0,1,0]  → every 8th note
Side Stick: [0,0,0,0, 1,0,0,0, 0,0,0,0, 1,0,0,0]  → beats 2 & 4
Kick      : [1,0,0,0, 0,0,0,0, 0,0,0,0, 0,0,0,0]  → beat 1 only
Open HH   : [0,0,0,0, 0,0,0,0, 0,0,0,0, 0,0,1,0]  → "and" of 4 (soft accent)
```

**FLOW** — groove mid-tempo, engaged calm (72–85 BPM)
```
Hi-Hat c  : [1,0,1,0, 1,0,1,0, 1,0,1,0, 1,0,1,0]  → 8th note (same rhythm as calm ride)
Snare     : [0,0,0,0, 1,0,0,0, 0,0,0,0, 1,0,0,0]  → solid beats 2 & 4
Kick      : [1,0,0,0, 0,0,1,0, 1,0,0,0, 0,0,1,0]  → beat 1 + "and" of 2 & 3
Open HH   : [0,0,0,0, 0,0,0,1, 0,0,0,0, 0,0,0,1]  → groove accent "and" of 4
```
BPM: `72 + frontal_alpha × 13` (range 72–85). Natural bridge between calm and tense.

**TENSE** — battle drums, relentless (95–135 BPM)
```
Hi-Hat  : [1,1,1,1, 1,1,1,1, 1,1,1,1, 1,1,1,1]  → constant 16th
Kick    : [1,0,0,0, 1,0,1,0, 1,0,0,0, 1,0,1,0]  → 4-on-floor + extra hits
Snare   : [0,0,0,0, 1,0,0,1, 0,0,0,0, 1,0,0,1]  → beats 2&4 + ghost offbeat
Open HH : [0,0,0,0, 0,0,1,0, 0,0,0,0, 0,0,1,0]  → offbeat accent
```

**tense_level** is a build-up momentum value (0.0 → 1.0):
- Increases `+0.006` per tick while tense
- Decreases `−0.004` per tick while calm or flow
- TENSE BPM: `95 + tense_level × 40` (range 95–135 BPM)
- When `tense_level > 0.65`: drum pattern intensifies — double-time kick and tom fills kick in

### Timing

Engine runs on a 16th-note loop (4 ticks per beat):
- 1 tick = `(60 / BPM) / 4` seconds
- Drum pattern: 16-step loop (= 1 bar of 4/4)
- All hits via `note_on` + auto `note_off` after `dur` seconds (separate thread)

## Auto-Reconnect

If the LSL stream drops (Muse out of range, BLE hiccup), the connector automatically retries without any user action:

| Attempt | Delay before retry |
|---|---|
| 1 | 3 seconds |
| 2 | 5 seconds |
| 3 | 10 seconds |
| 4+ | 15 seconds |

Status transitions during auto-reconnect:
```
connected → (drop) → reconnecting → ... → connected
                   → (user disconnects) → disconnected
```

The UI shows an orange dot and `🔄 Reconnecting...` button while retrying. Manual disconnect cancels the loop immediately.

## Muse 2 Integration

Actual implementation uses **muselsl + pylsl** (not BrainFlow BoardShim):

```python
# brainflow_connector.py — flow summary

# 1. Launch muselsl as subprocess
proc = subprocess.Popen([sys.executable, "-c",
    f"from muselsl import stream; stream(address='{mac}', ppg_enabled=True)"])

# 2. Resolve LSL stream
eeg_inlet = StreamInlet(resolve_byprop("type", "EEG", timeout=1.0)[0])

# 3. Loop every 250ms (4 Hz): pull chunk, compute band power
chunk, _ = eeg_inlet.pull_chunk(timeout=0.0, max_samples=512)

# 4. Pass 1: pre-scan AF7/AF8 for frontal EMG
_frontal_emg = False
for ch in (1, 2):  # AF7, AF8
    if np.ptp(filtered) > 150.0 or b_hi / (b_lo + 1e-6) > 0.80:
        _frontal_emg = True; break

# 5. Pass 2: band power per channel
# Frontal (AF7/AF8): alpha + theta only, NEVER beta
# Temporal (TP9/TP10): all bands, but beta is blanked if _frontal_emg=True
psd = DataFilter.get_psd_welch(ch_data, 256, 128, 256, BLACKMAN_HARRIS)
beta_list.append(DataFilter.get_band_power(psd, 13.0, 25.0))  # 25 Hz max (not 30)
beta_hz_list.append(_centroid(psd, 13.0, 25.0))               # spectral centroid

# 6. Normalize + EMA → send to engine
alpha = self._normalize("alpha", np.mean(alpha_list))  # rolling p10–p90
ema_a = ema_a * 0.80 + alpha * 0.20  # EMA=0.20, time constant ~1.1s
# frontal_alpha/frontal_theta (AF7+AF8 only) juga dikirim untuk flow_score
self.engine.set_eeg(ema_a, ema_b, ema_t, tbr=ema_tbr, tbr_raw=ema_tbr_raw,
                    frontal_alpha=ema_fa, frontal_theta=ema_ft)

# 7. Raw µV² EMA + Hz centroid → stored for UI display
ema_a_raw = ema_a_raw * 0.80 + np.mean(alpha_list) * 0.20
self.raw_bands  = {"alpha": ema_a_raw, "beta": ema_b_raw, "theta": ema_t_raw}
self.peak_hz    = {"alpha": centroid_a, "beta": centroid_b, "theta": centroid_t}
self.frontal_alpha = round(ema_fa, 3)
self.frontal_theta = round(ema_ft, 3)
```

**engine.start()** is called only when `muse_status == "connected"` — no audio before Muse is connected.
**engine.stop()** is called when `muse_status == "disconnected"` or `"error"`.

## Troubleshooting

**`No module named 'fluidsynth'`**
```bash
pip install pyfluidsynth
```

**`FluidSynth library not found`**
```bash
brew install fluid-synth
# If still failing:
export DYLD_LIBRARY_PATH=/opt/homebrew/lib:$DYLD_LIBRARY_PATH
```

**No audio**
- Check macOS volume is not muted
- Try changing `driver="coreaudio"` to `driver="alsa"` (Linux) or `driver="dsound"` (Windows)

**Unexpected drum sound**
- Try a different soundfont — GeneralUser GS tends to sound better than FluidR3 for percussive hits
