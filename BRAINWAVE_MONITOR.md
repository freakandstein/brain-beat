# Brainwave Monitor

Generative drum engine that responds to real-time EEG values (alpha/beta/theta).
Drum patterns change automatically based on mental state: calm (brush jazz) or tense (battle drums).

## Architecture

```
Muse 2 (via muselsl + pylsl) / Simulator
        │
        ▼
  brainflow_connector.py       ← EEG acquisition, EMG rejection, mental command detection
        │
  eeg_engine.py                ← Brainwave Monitor core: FluidSynth drums-only (GM channel 9)
        │
  eeg_server.py                ← Flask + SocketIO bridge (port 8765)
        │                ↘
        │           obs_connector.py  ← OBS WebSocket v5 scene switching
        │
  templates/index.html         ← Web UI "BRAINWAVE MONITOR" (OBS overlay)
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

Five active commands are detected in real-time, each using distinct signal dimensions to avoid cross-triggering.

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

### Design Principle

All commands use fundamentally different signal dimensions:
- **Wink left/right** → left-right *asymmetry* on frontal channels (one side active, other silent) — the side is just which channel dominates, not a separate signal dimension
- **Jaw clench** → dedicated *temporal* channels (TP9/TP10), completely separate electrodes
- **Eyebrow raise** → bilateral frontal activation (both AF7 and AF8 rise together)
- **Double jaw clench** → same channels as jaw clench, distinguished purely by *edge count within a timing window* (`GestureComposer`), not a different signal dimension

The weak-channel boundary (1–400µV) is the key separator between wink and eyebrow: if both sides exceed `thr_eyebrow`, it's bilateral (eyebrow); if only one side is strong with the other below 400µV, it's unilateral (wink). Observed accuracy: ~90% in real-world use.

### Overlay FX

**`/overlay/mental-command`** — 5-command overlay. Each command has its own color:
- Command A1 (Wink Left): cyan
- Command A2 (Wink Right): pink
- Command B (Jaw Clench): orange  
- Command C (Eyebrow Raise): green
- Command D (Double Jaw Clench): amber, with a "COMBO SEQUENCE" badge and longer 4s hold

Dev test: **Shift+1** through **Shift+5**. Single commands auto-hide after 2.8 seconds; double jaw holds for 4 seconds.

## OBS Scene Switching & Recording Control

Mental commands trigger OBS scene changes via WebSocket v5 (`obs_connector.py`).

| Command | Default Scene |
|---|---|
| Wink Left | Scene 1 (2 Views Without Top) |
| Wink Right | Scene 1 (2 Views Without Top) — same default as Wink Left, change independently in `DEFAULT_SCENE_MAP` if needed |
| Jaw Clench | Scene 2 (3 Views) |
| Eyebrow Raise | Scene 3 (2 Views Without Front) |
| Double Jaw Clench | not a scene switch — toggles OBS recording instead (see below) |

Scene names can be changed in `obs_connector.py` → `DEFAULT_SCENE_MAP`.

**Double jaw clench → recording toggle**: `OBSConnector.toggle_record()` checks OBS's actual recording state via `get_record_status().output_active`, then calls `start_record()` or `stop_record()` accordingly. Runs in a background thread (non-blocking), with the same reconnect-on-failure behavior as scene switching. First double jaw clench starts recording, the next stops it — independent of `DEFAULT_SCENE_MAP`.

**Setup:**
1. OBS → Tools → WebSocket Server Settings → Enable
2. Set password in `eeg_server.py`: `OBSConnector(password="...")`
3. Make sure scene names in `DEFAULT_SCENE_MAP` match exactly what's in OBS

Connection is established at startup and auto-reconnects if OBS restarts.

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

**Mental Commands** (socket events → UI trigger):
- `wink_left` → Scene 1 by brain signal
- `wink_right` → Scene 1 by brain signal (same default scene as wink_left, configurable independently)
- `jaw_clench` → Scene 2 by brain signal
- `eyebrow_raise` → Scene 3 by brain signal
- `double_jaw` → no scene switch — toggles OBS recording start/stop, and triggers the overlay FX at `/overlay/mental-command`

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
