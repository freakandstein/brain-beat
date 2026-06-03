# BrainBeat — EEG Drum Engine

Generative drum engine yang merespons nilai EEG (alpha/beta/theta) secara realtime.
Pola drum berubah otomatis berdasarkan mental state: calm (brush jazz) atau tense (battle drums).

## Arsitektur

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

| Band | Range | Normalized | Raw display | Kondisi dominan |
|---|---|---|---|---|
| **θ theta** | 4–8 Hz | 0–1 via p10–p90 | µV² + Hz centroid | Mengantuk, meditasi, drowsy |
| **α alpha** | 8–13 Hz | 0–1 | µV² + Hz centroid | Rileks, mata tertutup, flow |
| **β beta** | 13–25 Hz | 0–1 | µV² + Hz centroid | Fokus, aktif berpikir, stres |

> **Delta (δ) dihapus** — delta hanya relevan saat deep sleep, tidak berguna untuk waking state monitoring. Dihapus dari semua layer: connector, engine, server, dan UI.

> Raw µV² hanya untuk display waveform UI. State detection menggunakan nilai normalized 0–1.
> Beta dibatasi 13–25 Hz (bukan 30 Hz) untuk menghindari kontaminasi EMG otot wajah (25–40 Hz).
> **Hz centroid** per band (spectral centroid) ditampilkan di UI menggantikan nilai µV² di label — lebih intuitif.

## Instalasi

### 1. Install FluidSynth via Homebrew
```bash
brew install fluid-synth
```

### 2. Install Python dependencies
```bash
pip3 install pyfluidsynth numpy flask flask-socketio
```

### 3. Soundfont (penting untuk kualitas suara)

Engine akan otomatis mencoba download soundfont GM saat pertama kali dijalankan.
Karena GitHub menggunakan Git LFS, download otomatis sering gagal — **disarankan download manual**:

```bash
mkdir -p ~/soundfonts
# Download salah satu lalu rename ke GeneralUser.sf2:
```

- **GeneralUser GS** (~30MB): https://www.schristiancollins.com/generaluser.php  
- **MuseScore General** (~200MB): https://ftp.osuosl.org/pub/musescore/soundfont/

Simpan ke `~/soundfonts/GeneralUser.sf2`.

> Tanpa soundfont GM, engine akan fallback ke **VintageDreamsWaves** (synth/chip-tune).
> Semua state tetap berfungsi, tapi timbre terdengar lebih elektronik.

## Menjalankan

**Gunakan Terminal.app** (bukan VS Code terminal — proses akan di-kill saat idle):

```bash
cd ~/Documents/Project/EEG
python3 music_server.py
```

Buka browser: **http://localhost:8765**

## Web UI

Browser UI (`templates/index.html`) menampilkan:

**State Card (kiri)**
- State aktif saat ini (CALM / TENSE) dengan warna berbeda
- BPM, HR (heart rate dari PPG), dan BUILD level numerik
- Progress bar BUILD (tense momentum) dan TENSION level
- WARMING UP indicator saat adaptive threshold belum terkalibrasi (60 detik pertama)

**EEG Channel Map (kanan, di dalam state card)**
- Diagram kepala SVG dengan 4 elektroda (TP9, AF7, AF8, TP10)
- Warna tiap elektroda menunjukkan kualitas sinyal: hijau/kuning/merah/abu
- Label: **EEG CHANNEL MAP**

**EEG Channels (canvas waveforms)**
- 3 rolling waveform Canvas: θ → α → β (delta dihapus)
- Warna: theta=hijau, alpha=biru, beta=ungu
- Y-axis: auto-scale per band (µV², 0 ke max dengan slow decay)
- Nilai kanan: nilai saat ini dalam µV² (format: `158 µV²` / `3.95 µV²`)
- Label Hz di nama band: `α alpha 8–13 Hz`
- Buffer: 120 titik (~2 menit history)

**BCI Device Panel**
- Status koneksi Muse 2, tombol scan + connect/disconnect
- Socket live-dot (kiri header): abu=connecting, hijau=terhubung, merah=putus

Cocok digunakan sebagai **OBS browser source** (overlay stream).

## Cara Kerja

### State Detection

Sistem menggunakan **binary 2-class**: `calm` vs `tense`.

```
arousal = 0.50 × beta − 0.25 × alpha − 0.25 × TBR
```

**Adaptive threshold** — bukan nilai hardcoded:
- Selama 60 detik pertama (240 tick × 4 Hz) sistem **warm-up**, mengumpulkan sampel arousal ke buffer.
- Setelah warm-up selesai, `threshold = median(buffer) + 0.02` — bias sedikit ke calm agar tidak over-tense.
- Default sebelum warm-up selesai: `-0.05`.
- `is_warming_up()` dan `get_threshold()` tersedia dari server untuk ditampilkan di UI.

**Vote buffer** 12 tick — symmetric (50/50):
| Transisi | Threshold |
|---|---|
| calm → tense | ≥ 50% vote tense |
| tense → calm | ≥ 50% vote calm |

### Pola Drum per State

**CALM** — brush jazz, sparse (55–65 BPM)
```
Ride     : [1,0,1,0, 1,0,1,0, 1,0,1,0, 1,0,1,0]  → tiap 8th note
Side Stick: [0,0,0,0, 1,0,0,0, 0,0,0,0, 1,0,0,0]  → beat 2 & 4
Kick      : [1,0,0,0, 0,0,0,0, 0,0,0,0, 0,0,0,0]  → beat 1 saja
Open HH   : [0,0,0,0, 0,0,0,0, 0,0,0,0, 0,0,1,0]  → "and" of 4 (aksen lembut)
```

**TENSE** — battle drums, relentless (95–135 BPM)
```
Hi-Hat  : [1,1,1,1, 1,1,1,1, 1,1,1,1, 1,1,1,1]  → 16th constant
Kick    : [1,0,0,0, 1,0,1,0, 1,0,0,0, 1,0,1,0]  → 4-on-floor + extra
Snare   : [0,0,0,0, 1,0,0,1, 0,0,0,0, 1,0,0,1]  → beat 2&4 + ghost offbeat
Open HH : [0,0,0,0, 0,0,1,0, 0,0,0,0, 0,0,1,0]  → aksen offbeat
```

**STRESS** (tense_level > 0.65) — escalasi dari TENSE:
```
Kick ganda: [1,0,1,0, 1,0,1,0, 1,0,1,0, 1,0,1,0]  → double-time kick
Snare tambahan + tom fill otomatis
```

**tense_level** adalah momentum build-up (0.0 → 1.0):
- Naik `+0.006` per tick saat tense
- Turun `−0.004` per tick saat calm
- BPM tense: `95 + tense_level × 40` (range 95–135 BPM)

### Timing

Engine berjalan di loop 16th note (4 tick per beat):
- 1 tick = `(60 / BPM) / 4` detik
- Pattern drum: 16 step loop (= 1 bar 4/4)
- Semua hit via `note_on` + auto-`note_off` setelah `dur` detik (thread terpisah)

## Integrasi Muse 2 (Aktif)

Implementasi aktual menggunakan **muselsl + pylsl** (bukan BrainFlow BoardShim):

```python
# brainflow_connector.py — ringkasan alur

# 1. Launch muselsl sebagai subprocess
proc = subprocess.Popen([sys.executable, "-c",
    f"from muselsl import stream; stream(address='{mac}', ppg_enabled=True)"])

# 2. Resolve LSL stream
eeg_inlet = StreamInlet(resolve_byprop("type", "EEG", timeout=1.0)[0])

# 3. Loop tiap 250ms (4 Hz): pull chunk, hitung band power
chunk, _ = eeg_inlet.pull_chunk(timeout=0.0, max_samples=512)

# 4. Pass 1: pre-scan AF7/AF8 untuk frontal EMG
_frontal_emg = False
for ch in (1, 2):  # AF7, AF8
    if np.ptp(filtered) > 150.0 or b_hi / (b_lo + 1e-6) > 0.50:
        _frontal_emg = True; break

# 5. Pass 2: band power per channel
# Frontal (AF7/AF8): alpha + theta saja, TIDAK pernah beta
# Temporal (TP9/TP10): semua band, tapi beta di-skip jika _frontal_emg=True
psd = DataFilter.get_psd_welch(ch_data, 256, 128, 256, BLACKMAN_HARRIS)
beta_list.append(DataFilter.get_band_power(psd, 13.0, 25.0))  # 25 Hz max (bukan 30)
beta_hz_list.append(_centroid(psd, 13.0, 25.0))  # spectral centroid

# 6. Normalize + EMA → kirim ke engine
alpha = self._normalize("alpha", np.mean(alpha_list))  # rolling p10–p90
ema_a = ema_a * 0.80 + alpha * 0.20  # EMA=0.20, time constant ~1.1 detik
self.engine.set_eeg(ema_a, ema_b, ema_t, tbr=ema_tbr, tbr_raw=ema_tbr_raw)

# 7. Raw µV² EMA + Hz centroid → simpan untuk display UI
ema_a_raw = ema_a_raw * 0.80 + np.mean(alpha_list) * 0.20
self.raw_bands  = {"alpha": ema_a_raw, "beta": ema_b_raw, "theta": ema_t_raw}
self.peak_hz    = {"alpha": centroid_a, "beta": centroid_b, "theta": centroid_t}
```

**engine.start()** hanya dipanggil saat `muse_status == "connected"` — tidak ada suara sebelum Muse terhubung.
**engine.stop()** dipanggil saat `muse_status == "disconnected"` atau `"error"`.

## Troubleshooting

**`No module named 'fluidsynth'`**
```bash
pip install pyfluidsynth
```

**`FluidSynth library not found`**
```bash
brew install fluid-synth
# Kalau masih error:
export DYLD_LIBRARY_PATH=/opt/homebrew/lib:$DYLD_LIBRARY_PATH
```

**Tidak ada suara**
- Pastikan volume macOS tidak mute
- Coba ganti `driver="coreaudio"` dengan `driver="alsa"` (Linux) atau `driver="dsound"` (Windows)

**Suara glitchy tidak seperti yang diharapkan**
- Coba soundfont berbeda — GeneralUser GS lebih baik dari FluidR3 untuk lead synth
- Adjust `intensity` multiplier di fungsi `_play_glitch()`
