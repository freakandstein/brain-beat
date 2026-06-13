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
  templates/overlay_mental_command.html ← 3-command mental command overlay (/overlay/mental-command)
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

Three active commands are detected in real-time, each using distinct signal dimensions to avoid cross-triggering.

### Command A — Wink (`on_wink`)

```
Channel      : AF7 (ch1) and AF8 (ch2)
Condition    : max(p2p_AF7, p2p_AF8) > 300µV         (above noise floor)
Asymmetry    : max / min ratio > 3.5                  (one side dominates — unilateral)
Unilateral   : min(p2p_AF7, p2p_AF8) between 1–150µV (weak side low = truly unilateral)
Guard        : NOT bilateral_eff, NOT during/after eyebrow zone
Cooldown     : 3 seconds
```

Wink left → AF7 dominates. Wink right → AF8 dominates. The `_wink_unilateral` check (weak channel 1–150µV) is the key separator from eyebrow raise — if the weak channel is above 150µV, both frontal electrodes are active and it's treated as eyebrow activity, not a wink.

### Command B — Jaw Clench (`on_jaw_clench`)

```
Channel    : TP9 (ch0) and TP10 (ch3)
Filter     : 20–100 Hz bandpass (broadband EMG range)
Condition  : p2p > 520µV on either channel
Sustained  : ≥ 2 consecutive ticks (~500ms) above threshold
Guard      : NOT eyebrow_zone AND NOT (frontal_active AND bilateral_eff)
Cooldown   : 2.5 seconds
```

Masseter EMG is strong, sustained, and confined to temporal channels — completely separate from frontal (AF7/AF8). The eyebrow guard prevents jaw clench from triggering during frontal muscle activity.

### Command C — Eyebrow Raise (`on_eyebrow_raise`)

```
Channel      : AF7 (ch1) and AF8 (ch2)
Condition A  : AF7 > 300µV AND AF8 > 300µV, with max/min ratio < 3.0 (symmetric bilateral)
Condition B  : max(AF7,AF8) > 500µV AND min(AF7,AF8) > 150µV (asymmetric bilateral — AF7 dominant)
Condition C  : AF8 dropout (p2p < 1µV or invalid) AND AF7 > 1200µV (solo fallback)
Sustained    : ≥ 2 consecutive ticks (~500ms)
Cooldown     : 3 seconds
```

Three bilateral detection paths handle real-world electrode variability: symmetric raise (Condition A), dominant-side raise where AF7 is much stronger due to anatomy (Condition B), and AF8 electrode dropout during raise — common on Muse 2 when scalp moves (Condition C, solo threshold 1200µV to avoid overlap with wink at <600µV).

### Mutual Exclusion (Global Mutex)

All three detectors share a single `_last_cmd_time` timestamp. Once any command fires, a **1.5-second idle window** must pass before any detector can fire again.

`_cmd_idle` is evaluated **once per tick, before all detectors run** — if eyebrow fires first in a tick, wink and jaw see `_cmd_idle = False` immediately.

Additionally, `_eyebrow_active_until` zone (800ms after any bilateral activity) blocks wink and jaw. And `_after_eyebrow` (2.5s after eyebrow fires) further suppresses wink.

### Design Principle

All three commands use fundamentally different signal dimensions:
- **Wink** → left-right *asymmetry* on frontal channels (one side active, other silent)
- **Jaw clench** → dedicated *temporal* channels (TP9/TP10), completely separate electrodes
- **Eyebrow raise** → bilateral frontal activation (both AF7 and AF8 rise together)

The 150µV boundary is the key separator between wink and eyebrow: below 150µV on the weak channel = wink; above 150µV = eyebrow territory. Observed accuracy: ~90% in real-world use.

### Overlay FX

**`/overlay/mental-command`** — 3-command overlay. Each command has its own color:
- Command A (Wink): cyan
- Command B (Jaw Clench): orange  
- Command C (Eyebrow Raise): green

Dev test: **Shift+1 / Shift+2 / Shift+3**. Auto-hides after 2.8 seconds.

## OBS Scene Switching

Mental commands trigger OBS scene changes via WebSocket v5 (`obs_connector.py`).

| Command | Default Scene |
|---|---|
| Wink | Scene 1 (2 Views Without Top) |
| Jaw Clench | Scene 2 (3 Views) |
| Eyebrow Raise | Scene 3 (2 Views Without Front) |

Scene names can be changed in `obs_connector.py` → `DEFAULT_SCENE_MAP`.

**Setup:**
1. OBS → Tools → WebSocket Server Settings → Enable
2. Set password in `eeg_server.py`: `OBSConnector(password="...")`
3. Make sure scene names in `DEFAULT_SCENE_MAP` match exactly what's in OBS

Connection is established at startup and auto-reconnects if OBS restarts.

## Web UI

The browser UI (`templates/index.html`) is a single consolidated card layout.

**Main Card** — one container with all EEG info:

- **State row (top):** Active state badge (CALM / FLOW / TENSE) with color + description on the left; HR (heart rate from PPG) and mental command trigger on the right
- **Mental command trigger:** appears below HR for 2.5 seconds when a brain signal fires — e.g. `Scene 1 by brain signal` (green). Hidden when idle.
- **Spectrum slider:** CALM ↔ FLOW ZONE ↔ TENSE gradient with cursor tracking `spectrum_pos`
- **EEG Channels + Channel Map (side by side):**
  - Left: 3 rolling waveform canvases θ → α → β, colors: theta=green, alpha=blue, beta=purple; Hz centroid label per band
  - Right: SVG head diagram (TP9, AF7, AF8, TP10), electrode color = signal quality: green/yellow/red/grey

**BCI Device Panel** (below main card)
- Muse 2 connection status, scan + connect/disconnect buttons
- Socket live-dot (header): grey=idle, green=connected, red=disconnected
- Auto-reconnect with backoff (3s → 5s → 10s → 15s); UI shows `🔄 Reconnecting...` during retries

**Mental Commands** (socket events → UI trigger):
- `wink` → Scene 1 by brain signal
- `jaw_clench` → Scene 2 by brain signal
- `eyebrow_raise` → Scene 3 by brain signal

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
    if np.ptp(filtered) > 150.0 or b_hi / (b_lo + 1e-6) > 0.50:
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
