# EEG Music Engine

Generative music engine yang merespons nilai EEG (alpha/beta/theta) secara realtime.
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
- State aktif saat ini (Fokus / Rileks / Stres / Kantuk) dengan warna berbeda
- BPM dan build level realtime
- Slider untuk adjust nilai alpha / beta / theta secara manual
- Tombol preset cepat: **Rileks · Fokus · Stres · Kantuk**

Cocok digunakan sebagai **OBS browser source** (overlay stream).

## Cara Kerja

### State Detection

| Kondisi EEG | State |
|---|---|
| beta > 0.65 **AND** theta > 0.55 | `stress` |
| beta > 0.6 **AND** alpha < 0.35 | `focus` |
| alpha > 0.55 **AND** beta < 0.4 | `relax` |
| theta > 0.65 | `drowsy` |
| beta > 0.45 | `focus` (fallback) |
| default | `relax` |

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

## Integrasi BrainFlow (nanti)

Ganti `engine.set_eeg()` di `music_server.py` dengan loop BrainFlow:

```python
from brainflow.board_shim import BoardShim, BrainFlowInputParams, BoardIds
from brainflow.data_filter import DataFilter, FilterTypes, DetrendOperations

def brainflow_loop(engine: MusicEngine):
    params = BrainFlowInputParams()
    board = BoardShim(BoardIds.MUSE_2_BOARD, params)
    board.prepare_session()
    board.start_stream()
    eeg_channels = BoardShim.get_eeg_channels(BoardIds.MUSE_2_BOARD)

    while True:
        data = board.get_current_board_data(256)  # ~1 detik data
        if data.shape[1] < 256:
            time.sleep(0.1)
            continue

        # Bandpass filter per band
        for ch in eeg_channels:
            DataFilter.perform_bandpass(data[ch], 256, 8, 13, 4,
                FilterTypes.BUTTERWORTH.value, 0)  # alpha
            # dst...

        # Hitung band power
        alpha_power = np.mean(np.abs(data[eeg_channels[0]]))
        beta_power  = np.mean(np.abs(data[eeg_channels[1]]))
        theta_power = np.mean(np.abs(data[eeg_channels[2]]))

        # Normalize 0–1
        total = alpha_power + beta_power + theta_power + 1e-10
        engine.set_eeg(alpha_power/total, beta_power/total, theta_power/total)

        time.sleep(0.5)  # update setiap 500ms
```

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
