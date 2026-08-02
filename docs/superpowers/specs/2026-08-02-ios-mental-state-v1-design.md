# iOS Mental State Monitor — v1 Design

**Date**: 2026-08-02
**Status**: Approved for planning

## Goal

Standalone iOS app that connects directly to a Muse 2 headset via BLE and
displays live mental state (calm / flow / tense) on the iPhone screen. No
Mac, no Python runtime on-device, no companion server.

This repo's Python code (`brainflow_connector.py`, `eeg_engine.py`) and docs
(`README.md`, `EEG.md`, `BRAINWAVE_MONITOR.md`) are **not** dependencies —
none of that stack runs on iOS (muselsl subprocess model, pylsl, BrainFlow
`DataFilter` have no iOS/Swift bindings). They serve as the **spec source**:
the tuned formulas and constants below are read out of the Python
implementation and re-expressed here as a language-agnostic contract for a
from-scratch Swift implementation.

## Scope

**In scope for v1:**
- BLE connection to Muse 2, EEG acquisition (4 channels: TP9, AF7, AF8, TP10 @ 256Hz)
- Band power computation (theta/alpha/beta) per channel
- Mental state classification (calm / flow / tense) matching the Python formulas below
- Live, non-persisted display of current state + spectrum position

**Out of scope for v1** (explicitly deferred, no v1 code for these):
- EMG gesture detection: wink left/right, jaw clench, double jaw, eyebrow raise, tilt left/right
- OBS control, keyboard/mouse remapping
- Drum/music generation engine
- Session history / CSV logging / playback
- Cursor control mode
- PPG / heart rate

## Reuse strategy

Because no code can be shared across the Python↔Swift boundary, "reuse"
means: extract the exact numeric formulas and constants that took multiple
tuning iterations to get right, and translate those — not the surrounding
code — into Swift. The existing recorded sessions (`eeg_session_*.csv` in
repo root) are available to validate the Swift implementation's output
against the Python implementation's output for numerical parity before
wiring to live BLE.

## Algorithm spec (source of truth for Swift implementation)

Extracted from `eeg_engine.py` (`MusicEngine` class) and
`brainflow_connector.py`.

### 1. Band power → EMA smoothing

Per tick (~100ms), compute raw theta/alpha/beta power via Welch PSD per
channel, then EMA-smooth with `alpha = 0.20`:

```
ema_x = ema_x * (1 - 0.20) + raw_x * 0.20   # for alpha, beta, theta, TBR (theta/beta ratio)
```

Frontal-only (AF7 + AF8) alpha/theta EMA is tracked separately (needed for
flow_score below), same 0.20 smoothing constant.

Initial EMA seed values: `ema_a = ema_b = ema_t = ema_tbr = 0.5` (normalized
scale), `ema_tbr_raw = 1.0`, `ema_fa = ema_ft = 0.5`.

### 2. Arousal

```
arousal = 0.50 * ema_beta - 0.30 * ema_alpha - 0.20 * ema_tbr
```

### 3. Flow score

Frontal-only (AF7/AF8), raw range approximately -1..+1:

```
flow_score_raw = ema_frontal_alpha + ema_frontal_theta - ema_beta
```

(Not required for the calm/flow/tense classification itself — `arousal` +
`spectrum_pos` below drive state. Included here for completeness since it's
part of the same formula family; v1 may omit surfacing it in UI.)

### 4. Adaptive threshold

- Buffer: last 480 arousal samples (rolling deque)
- Default before warm-up: `0.02` (biases toward "calm")
- After warm-up (buffer has ≥60 samples, i.e. ~15s at 100ms/tick), recompute
  every 120 ticks (~30s):

```
adaptive_threshold = round(median(arousal_buffer) + 0.03, 4)
```

### 5. Spectrum position (0..1)

```
delta = (arousal - adaptive_threshold) / 0.15
raw   = clamp((delta + 1.0) / 2.0, 0.0, 1.0)
spectrum_pos_smooth += (raw - spectrum_pos_smooth) * 0.15   # EMA alpha=0.15
```

Initial seed: `spectrum_pos_smooth = 0.4`.

Interpretation: 0.0 = purely calm, 0.5 = flow zone, 1.0 = purely tense.

### 6. Zone bands

```
if spectrum_pos > 0.65:      raw_state = "tense"
elif spectrum_pos >= 0.35:   raw_state = "flow"
else:                        raw_state = "calm"
```

### 7. Vote buffer (anti flip-flop)

- Rolling buffer of last 20 raw_state values (~5s at 100ms/tick, i.e. one
  push per tick as computed above)
- On each tick, append `raw_state`, then:

```
counts = count of each state in buffer
best = (state, count) with highest count
required = max(1, floor(total_in_buffer * 0.70))
state = best.state if best.count >= required else current_state
```

- `current_state` persists across ticks; only changes when a state reaches
  70% supermajority in the vote buffer. Initial value: `"calm"`.

### Tick rate

Python ticks at `(60.0 / bpm) / 4.0` seconds, tied to the (out-of-scope for
v1) drum engine's BPM — not meaningful for iOS. **For v1, use a fixed tick
rate of 100ms** (10Hz), matching the EMA/buffer constants above, which were
tuned assuming ~100ms ticks.

## Components

1. **CoreBluetooth acquisition layer** (new, no Python equivalent) —
   connects to Muse 2 GATT services, streams raw EEG samples for
   TP9/AF7/AF8/TP10 @ 256Hz. Built from scratch against Muse's BLE protocol;
   no muselsl/LSL equivalent exists on iOS.

2. **Signal processing module** (Accelerate/vDSP) — Welch PSD band power
   (theta/alpha/beta) per channel, pure function with no BLE/UI dependency,
   unit-testable against recorded CSV sessions.

3. **Mental state classifier** — direct translation of steps 1–7 above.

4. **UI** — single live-updating screen showing current state
   (calm/flow/tense) and spectrum position. No persistence.

## Testing approach

- Signal processing module and classifier are pure functions/structs,
  testable in isolation from BLE and UI.
- Validate against existing `eeg_session_*.csv` recordings: feed recorded
  samples through the Swift pipeline and compare state transitions /
  spectrum_pos trajectory against what the Python engine would have
  produced, to catch translation errors before live BLE testing.
- BLE layer tested against a real Muse 2 device (no simulator path for BLE
  hardware).
