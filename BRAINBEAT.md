# BrainBeat — EEG Drum Engine

Generative drum engine that responds to real-time EEG values (alpha/beta/theta).
Drum patterns change automatically based on mental state: calm (brush jazz) or tense (battle drums).

## Architecture

```
Muse 2 (via muselsl + pylsl) / Simulator
        │
        ▼
  music_engine.py      ← BrainBeat core: FluidSynth drums-only (GM channel 9)
        │
  music_server.py      ← Flask + SocketIO bridge (port 8765)
        │
  templates/index.html ← Web UI "BRAIN BEAT MONITOR" (OBS overlay)
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
pip3 install pyfluidsynth numpy flask flask-socketio
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
cd ~/Documents/Project/EEG
python3 music_server.py
```

Open browser: **http://localhost:8765**

## Web UI

The browser UI (`templates/index.html`) displays:

**State Card (left)**
- Active state (CALM / TENSE) with distinct colors
- BPM, HR (heart rate from PPG), and BUILD level
- BUILD progress bar (tense momentum) and TENSION level
- WARMING UP indicator while adaptive threshold is still calibrating (first 60 seconds)

**EEG Channel Map (right, inside state card)**
- SVG head diagram with 4 electrodes (TP9, AF7, AF8, TP10)
- Electrode color = signal quality: green / yellow / red / grey
- Label: **EEG CHANNEL MAP**

**EEG Channels (canvas waveforms)**
- 3 rolling waveform canvases: θ → α → β (delta removed)
- Colors: theta=green, alpha=blue, beta=purple
- Y-axis: auto-scale per band (µV², 0 to max with slow decay)
- Right label: current value in µV² (e.g. `158 µV²` / `3.95 µV²`)
- Band label includes Hz range: `α alpha 8–13 Hz`
- Buffer: 120 points (~2 minutes of history)

**BCI Device Panel**
- Muse 2 connection status, scan + connect/disconnect buttons
- Socket live-dot (header left): grey=connecting, green=connected, red=disconnected

Compatible with **OBS Browser Source** (stream overlay).

## How It Works

### State Detection

Binary 2-class system: `calm` vs `tense`.

```
arousal = 0.50 × beta − 0.25 × alpha − 0.25 × TBR
```

**Adaptive threshold** — not hardcoded:
- During the first 60 seconds (240 ticks × 4 Hz), the engine **warms up** and collects arousal samples.
- After warm-up: `threshold = median(buffer) + 0.02` — slight bias toward calm.
- Default before calibration: `-0.05`.
- `is_warming_up()` and `get_threshold()` are exposed from the server and shown in the UI.

**Vote buffer** — 12-tick symmetric (50/50):

| Transition | Threshold |
|---|---|
| calm → tense | ≥ 50% tense votes |
| tense → calm | ≥ 50% calm votes |

### Drum Patterns per State

**CALM** — brush jazz, sparse (55–65 BPM)
```
Ride      : [1,0,1,0, 1,0,1,0, 1,0,1,0, 1,0,1,0]  → every 8th note
Side Stick: [0,0,0,0, 1,0,0,0, 0,0,0,0, 1,0,0,0]  → beats 2 & 4
Kick      : [1,0,0,0, 0,0,0,0, 0,0,0,0, 0,0,0,0]  → beat 1 only
Open HH   : [0,0,0,0, 0,0,0,0, 0,0,0,0, 0,0,1,0]  → "and" of 4 (soft accent)
```

**TENSE** — battle drums, relentless (95–135 BPM)
```
Hi-Hat  : [1,1,1,1, 1,1,1,1, 1,1,1,1, 1,1,1,1]  → constant 16th
Kick    : [1,0,0,0, 1,0,1,0, 1,0,0,0, 1,0,1,0]  → 4-on-floor + extra hits
Snare   : [0,0,0,0, 1,0,0,1, 0,0,0,0, 1,0,0,1]  → beats 2&4 + ghost offbeat
Open HH : [0,0,0,0, 0,0,1,0, 0,0,0,0, 0,0,1,0]  → offbeat accent
```

**tense_level** is a build-up momentum value (0.0 → 1.0):
- Increases `+0.006` per tick while tense
- Decreases `−0.004` per tick while calm
- TENSE BPM: `95 + tense_level × 40` (range 95–135 BPM)
- When `tense_level > 0.65`: drum pattern intensifies — double-time kick and tom fills kick in

### Timing

Engine runs on a 16th-note loop (4 ticks per beat):
- 1 tick = `(60 / BPM) / 4` seconds
- Drum pattern: 16-step loop (= 1 bar of 4/4)
- All hits via `note_on` + auto `note_off` after `dur` seconds (separate thread)

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
self.engine.set_eeg(ema_a, ema_b, ema_t, tbr=ema_tbr, tbr_raw=ema_tbr_raw)

# 7. Raw µV² EMA + Hz centroid → stored for UI display
ema_a_raw = ema_a_raw * 0.80 + np.mean(alpha_list) * 0.20
self.raw_bands = {"alpha": ema_a_raw, "beta": ema_b_raw, "theta": ema_t_raw}
self.peak_hz   = {"alpha": centroid_a, "beta": centroid_b, "theta": centroid_t}
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
