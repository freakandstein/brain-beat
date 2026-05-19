# EEG Music Engine

Generative music engine yang merespons nilai EEG (delta/alpha/beta/theta) secara realtime.
Musik berubah otomatis berdasarkan kondisi mental: fokus, rileks, stres, atau kantuk.

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
| **δ delta** | 0.5–4 Hz | 0–1 via p10–p90 | µV² (biasanya terbesar, sifat 1/f) | Tidur dalam, kantuk berat |
| **θ theta** | 4–8 Hz | 0–1 | µV² | Mengantuk, meditasi, drowsy |
| **α alpha** | 8–13 Hz | 0–1 | µV² | Rileks, mata tertutup, flow |
| **β beta** | 13–30 Hz | 0–1 | µV² (biasanya terkecil) | Fokus, aktif berpikir, stres |

> Raw µV² hanya untuk display UI. State detection menggunakan nilai normalized 0–1.
> Delta selalu jauh lebih besar dari beta dalam satuan µV² — ini normal (karakteristik 1/f sinyal EEG).

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

| Kondisi EEG (normalized 0–1) | State | Prioritas |
|---|---|---|
| beta > 0.70 **AND** theta > 0.62 | `stress` | 1 (tertinggi) |
| beta > 0.52 **AND** alpha < 0.45 | `focus` | 2 |
| alpha > 0.55 **AND** beta < 0.4 | `relax` | 3 |
| theta > 0.65 | `drowsy` | 4 |
| beta > 0.40 | `focus` (fallback) | 5 |
| default | `relax` | 6 |

**Anti-flicker**: vote buffer `deque(maxlen=16)` + `Counter.most_common(1)` — state aktif = mayoritas 16 tick terakhir (~2 detik di 120 BPM).

**EMA smoothing** sebelum masuk engine: `EMA=0.35`, time constant ~3 detik.

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
script = ("from muselsl import stream; "
          f"stream(address='{mac_address}', ppg_enabled=True, ...")
proc = subprocess.Popen([sys.executable, "-c", script], ...)

# 2. Resolve LSL stream yang dibuat muselsl
eeg_streams = resolve_byprop("type", "EEG", timeout=1.0)
eeg_inlet = StreamInlet(eeg_streams[0])

# 3. Loop tiap 1 detik: pull chunk, hitung band power
chunk, _ = eeg_inlet.pull_chunk(timeout=0.0, max_samples=512)

# 4. Channel quality filter
std = np.std(eeg_win[ch])
q = 0.0 if std < 3.0 or std > 400.0 else 1.0  # simplified

# 5. Band power via BrainFlow DataFilter (4 band)
psd = DataFilter.get_psd_welch(ch_data, 256, 128, 256, BLACKMAN_HARRIS)
delta_list.append(DataFilter.get_band_power(psd, 0.5,  4.0))
alpha_list.append(DataFilter.get_band_power(psd, 8.0,  13.0))
beta_list.append( DataFilter.get_band_power(psd, 13.0, 30.0))
theta_list.append(DataFilter.get_band_power(psd, 4.0,   8.0))

# 6. Normalize + EMA → kirim ke engine
delta = self._normalize("delta", np.mean(delta_list))  # rolling p10–p90
ema_d = ema_d * 0.65 + delta * 0.35
self.engine.set_eeg(ema_a, ema_b, ema_t, ema_d)

# 7. Raw µV² EMA → simpan di self.raw_bands untuk display UI
ema_d_raw = ema_d_raw * 0.65 + np.mean(delta_list) * 0.35
self.raw_bands = {"delta": ema_d_raw, "alpha": ema_a_raw, ...}
```

Server (`music_server.py`) membaca `muse.raw_bands` dan mengirim ke frontend sebagai
`delta_raw`, `alpha_raw`, `beta_raw`, `theta_raw` dalam payload `state_update`.

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
