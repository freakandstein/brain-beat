# EEG Music Engine

Generative music engine yang merespons nilai EEG (alpha/beta/theta) secara realtime.
Musik berubah otomatis berdasarkan kondisi mental: fokus/rileks (calm) atau stres/tegang (tense).

## Arsitektur

```
BrainFlow / Simulator
        │
        ▼
  music_engine.py      ← core engine (FluidSynth + MIDI generatif)
        │
  music_server.py      ← Flask + SocketIO bridge (port 8765)
        │
  templates/index.html ← Web UI (OBS overlay-compatible)
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
- State aktif saat ini (FOCUS / RELAX / STRESS / DROWSY) dengan warna berbeda
- BPM, HR (heart rate dari PPG), dan BUILD level numerik
- Progress bar BUILD (focus momentum) dan TENSION (stress build)

**EEG Channel Map (kanan, di dalam state card)**
- Diagram kepala SVG dengan 4 elektroda (TP9, AF7, AF8, TP10)
- Warna tiap elektroda menunjukkan kualitas sinyal: hijau/kuning/merah/abu
- Label: **EEG CHANNEL MAP**

**EEG Channels (canvas waveforms)**
- 4 rolling waveform Canvas, urutan low→high: δ → θ → α → β
- Warna: delta=oranye, theta=hijau, alpha=biru, beta=ungu
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

**Tense** jika `arousal > 0.02` AND bertahan 8/12 tick (~1.5 detik)
**Calm** jika `arousal ≤ 0.02` OR drowsy override aktif

**Drowsy override** (paksa calm meski arousal positif):
```
tbr_raw > 2.0           → theta 2× lebih besar dari beta secara absolut
tbr_norm > 0.45 AND beta < 0.45  → normalized check
beta < 0.25             → beta terlalu rendah, hampir ketiduran
```

**Asymmetric vote buffer** — masuk tense lebih sulit, keluar lebih mudah:
| Transisi | Threshold | Waktu (~72 BPM) |
|---|---|---|
| calm → tense | 8/12 = 67% | ~1.5 detik |
| tense → calm | 4/12 = 33% | ~0.8 detik |

### Musik per State

**Fokus** — Hans Zimmer / cinematic tension
- String ostinato 16th note terus-menerus
- Build level naik seiring durasi fokus (0 → 1 dalam ~2 menit)
- Brass swell masuk saat build > 0.4, choir climax saat build > 0.7
- BPM: 80 → 95 seiring build
- Chord: Dm → Bb → F → C (minor, driving)
- Reverb: dry dan tight

**Rileks** — ambient / lo-fi peaceful
- String pad lembut + melody fill sesekali (65% probabilitas)
- Pad warm (bukan choir) di register menengah
- Choir sangat jarang (15% probabilitas) dan pelan
- BPM: 55 → 65
- Chord: Cmaj7 → Fmaj7 → G → Am (major-leaning, hangat)
- Reverb: warm hall (roomsize 0.75)

**Stres** — game boss fight / intense action
- Drum pattern relentless 16th note (kick / snare / hihat terus)
- Bass pulse tiap 8th note, staccato
- Chord stab tiap quarter note (punchy, tidak sustain)
- Melodi motif chromatic pendek dan gelisah (synth lead)
- Glitch engine sebagai aksen (25% probabilitas per tick)
- BPM: 110 → 128
- Chord: cluster disonan + tritone stack yang tidak resolve
- Reverb: hampir dry

**Kantuk** — dreamy / hypnagogic
- Open fifth chord yang mengambang
- Pad breathing (CC11 expression cycling cos wave)
- Bass sangat jarang (tiap half note), velocity pelan
- BPM: 45 → 53
- Reverb: maksimal, floaty

### Timing

Engine berjalan di loop 16th note. Setiap tick:
- 1 tick = satu 16th note = `(60 / BPM) / 4` detik
- Chord: setiap 16 tick (1 bar), kecuali stress setiap 4 tick
- Drum pattern: 16 step loop

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
