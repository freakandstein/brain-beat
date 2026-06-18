# EEG Research — Device Summary

## Status Implementasi

| Komponen | Status | Keterangan |
|---|---|---|
| **music_engine.py** (BrainBeat) | ✅ Selesai | Drums-only via FluidSynth GM ch9, 3-state calm/flow/tense, adaptive threshold (60s warm-up), tense_level build-up, flow_score dari frontal EEG |
| **music_server.py** | ✅ Selesai | Flask + SocketIO, port 8765, emit state + arousal/threshold/confidence/consistency/warming_up + raw µV² + HR + eyebrow_raise event |
| **Web UI (index.html)** | ✅ Selesai | OBS overlay "BRAINWAVE MONITOR" — single card: state badge, HR, mental command trigger (Scene N by brain signal), spectrum slider, EEG waveforms θ/α/β + Hz display, EEG channel map SVG, status reconnecting |
| **Overlay FX (overlay_mental_command.html)** | ✅ Selesai | Full-screen visual FX per command — electric arc, scan line, edge glow, auto-hide 2.8s (4s untuk combo), 4 warna berbeda |
| **Mental Command Playground** | ✅ Selesai | 4 active commands via `/overlay/mental-command` — wink, jaw clench, eyebrow raise, double jaw clench, masing-masing warna berbeda |
| **State detection** | ✅ Selesai | 3-state calm/flow/tense — arousal index + flow_score (frontal α+θ−β) + spectrum_pos 0..1 + adaptive threshold (warm-up 60s) + vote buffer 20 tick (70% supermajority) |
| **EMG rejection** | ✅ Selesai | Two-pass architecture: pre-scan AF7/AF8 frontal EMG, volume conduction blanking ke TP9/TP10 |
| **Eyebrow raise detection** | ✅ Selesai | Bilateral AF7+AF8 > thr_eyebrow (adaptive, default 300µV) + symmetry check (ratio <3.0) + sustained ≥3 tick (~750ms) + cooldown 3s + eyebrow_zone 1.5s |
| **Wink detection** | ✅ Selesai | Unilateral: satu sisi AF7/AF8 > thr_wink (adaptive, default 800µV) + asimetri ratio >2.0 + weak side 10–300µV — Command A playground |
| **Jaw clench detection** | ✅ Selesai | TP9/TP10 EMG envelope (RMS ~300ms tail, bukan ptp window 2s penuh) > thr_jaw (adaptive, default 520µV), rising-edge triggered + guard frontal bleed + cooldown 4s — Command B playground |
| **Double jaw clench detection** | ✅ Selesai | `GestureComposer` menghitung rising edge jaw clench dalam window 1.0s (diukur dari release) — 2+ edge → double_jaw, 1 edge → single jaw_clench — Command D playground |
| **Adaptive EMG threshold per-sesi** | ✅ Selesai | 15 detik pertama ukur baseline noise frontal+temporal → thr_eyebrow/wink/jaw dihitung otomatis via median×multiplier, clamp ke range aman |
| **Global mutex antar detector** | ✅ Selesai | `_last_cmd_time` + `_cmd_idle` (1.5s window) — satu command fire → semua detector diblokir, dua arah, dihitung sekali per tick sebelum semua detektor jalan |
| **Auto-reconnect** | ✅ Selesai | Jika koneksi Muse putus, retry otomatis dengan backoff 3s→5s→10s→15s, status reconnecting di UI |
| **Muse 2 BLE acquisition** | ✅ Selesai | muselsl subprocess + pylsl, EEG 256Hz + PPG 64Hz |
| **Heart Rate (PPG)** | ✅ Selesai | Peak-detection dari IR channel PPG, update setiap 5 detik |
| **Channel quality filter** | ✅ Selesai | Channel poor di-skip dari band power computation |
| **Spektral Hz display** | ✅ Selesai | Spectral centroid per band ditampilkan di UI dalam Hz |
| **Delta band** | ✅ Dihapus | δ tidak relevan untuk waking state — dihapus dari semua layer (connector/engine/server/UI) |
| **Musik saat Muse belum connect** | ✅ Selesai | engine.start() hanya dipanggil saat Muse status = "connected", stop saat disconnect |
| **ML Classifier** | 🔲 Belum | Threshold cukup untuk tahap awal, upgrade SVM/LDA nanti |

---

## Device yang Digunakan: Muse 2 (InteraXon)

### Spesifikasi Utama

| Parameter | Detail |
|---|---|
| **Manufacturer** | InteraXon (Kanada) |
| **Harga** | ~$250–$350 USD |
| **Channel EEG** | 4 channel (TP9, AF7, AF8, TP10) |
| **Sample Rate** | 256 Hz |
| **Tipe Elektroda** | Dry electrode (tanpa gel) |
| **Sensor tambahan** | PPG (heart rate), accelerometer, gyroscope |
| **Konektivitas** | Bluetooth Low Energy |
| **Akses Raw EEG** | ✅ Via muselsl (subprocess) + pylsl (LSL inlet) |

### Lokasi Elektroda

```
        DEPAN
   AF7        AF8       ← Frontal
TP9                TP10  ← Temporal
        BELAKANG
```

> ⚠️ **Catatan:** Tidak ada elektroda di C3/C4 (motor cortex), sehingga **Motor Imagery tidak dapat dilakukan**. Paradigma yang sesuai adalah Band Power Classification untuk monitoring mental state.

---

## Tujuan Penelitian

- Membangun sistem monitoring kondisi emosional/mental state otak secara realtime saat bermain game
- Menampilkan overlay stream di OBS yang menunjukkan kondisi otak pemain secara live
- Eksplorasi active command berbasis EEG yang applicable untuk konten streaming game (TBD)

---

## Konsep Utama: Passive BCI + Overlay Stream

### Pendekatan
Muse 2 digunakan sebagai **mental state monitor (Passive BCI)**, bukan sebagai game controller. Output utama adalah overlay visual di OBS yang menampilkan kondisi emosional/mental otak secara realtime saat streamer bermain game.

Pendekatan ini dipilih karena:
- Lebih feasible secara teknis dengan 4 channel Muse 2
- Lebih menarik untuk viewer — kondisi otak streamer terlihat live dan tidak bisa di-fake
- Tidak ada masalah latency seperti pada BCI control aktif
- Potensi konten unik: "streaming pertama yang menampilkan kondisi otak streamer secara live"

### Mental State yang Dideteksi

Sistem menggunakan **binary 2-class classifier**: `calm` vs `tense`.

| State | Sinyal EEG | Logika Deteksi |
|---|---|---|
| **Tense / Aroused** | Beta dominan, alpha & TBR rendah | `arousal = 0.50·β − 0.25·α − 0.25·TBR > threshold` |
| **Calm / Relaxed** | Alpha/theta dominan, beta rendah | `arousal ≤ threshold` |

> **Threshold** bukan hardcoded — dikalibrasi otomatis via warm-up 60 detik pertama: `threshold = median(arousal_buffer) + 0.02`. Default sebelum kalibrasi: `-0.05`.
> Bias `+0.02` agar engine tidak over-sensitif terhadap fluktuasi arousal minor.

> Nilai alpha/beta/theta/TBR yang dikirim ke engine adalah **normalized 0–1** (rolling percentile p10–p90, window 60 sampel).
> Raw band power dalam **µV²** dikirim ke UI terpisah.
> Channel quality < 0.25 **dikecualikan** dari band power computation.

### State Smoothing (Anti-Flicker)

Nilai EEG mentah berfluktuasi setiap 16th note tick. Tanpa smoothing, state bisa loncat-loncat meski kondisi otak masih sama.

**Mekanisme**: symmetric vote buffer 12 tick — threshold 50% untuk kedua arah transisi.

| Parameter | Nilai | Efek |
|---|---|---|
| Vote buffer size | 12 tick | ~1.5–2.5 detik tergantung BPM |
| calm → tense | ≥ 50% vote tense | Balanced — tidak over-sensitive |
| tense → calm | ≥ 50% vote calm | Balanced — recovery proporsional |
| Implementasi | `Counter(deque(maxlen=12))` | O(1) per tick |

### Adaptive Threshold

Threshold arousal bukan hardcoded — dikalibrasi otomatis dari sinyal tiap sesi:

| Fase | Durasi | Behavior |
|---|---|---|
| **Warm-up** | 60 detik pertama (240 tick) | Kumpulkan sampel arousal, `threshold = -0.05` sementara |
| **Kalibrasi** | Setelah warm-up | `threshold = median(buffer) + 0.02` — bias sedikit ke calm |
| **Stabil** | Sepanjang sesi | Threshold tetap, tidak berubah lagi |

> Bias `+0.02` ke calm mencegah over-sensitif terhadap fluktuasi arousal minor.
> Server mengirim `threshold` dan `warming_up` (boolean) ke UI setiap update.

### Pipeline Overlay Stream

```
Muse 2 (Bluetooth)
    ↓
muselsl subprocess            ← [✅ Impl.] Stream EEG + PPG via LSL
    ↓  auto-reconnect: retry dengan backoff 3s→5s→10s→15s jika koneksi putus
pylsl StreamInlet             ← [✅ Impl.] Baca EEG 256Hz + PPG 64Hz
    ↓
BrainFlow DataFilter          ← [✅ Impl.] PSD Welch, band power (filter only)
    ↓
Channel Quality Filter        ← [✅ Impl.] Skip channel quality < 0.25
    ↓
Band Power θ/α/β              ← [✅ Impl.] 4–8 / 8–13 / 13–25 Hz (delta dihapus — hanya deep sleep)
    ↓
EMG Rejection (2-pass)        ← [✅ Impl.] Pass 1: scan AF7/AF8 p2p + spektral rasio (tanpa break)
    ↓                                      Pass 2: beta dari TP9/TP10 di-blank jika frontal EMG aktif
Adaptive EMG Calibration      ← [✅ Impl.] 15 detik pertama: ukur baseline noise frontal+temporal
    ↓                                      thr_eyebrow = median_frontal×3.0 (80–400µV)
    ↓                                      thr_wink = median_frontal×5.0 (300–1000µV)
    ↓                                      thr_jaw = median_temporal×4.0 (300–700µV)
Eyebrow Raise Detection       ← [✅ Impl.] AF7+AF8 keduanya >thr_eyebrow + simetris (ratio <3.0) + sustained ≥3 tick + cooldown 3s + zone 1.5s
    ↓                                      Bilateral sustained = eyebrow raise genuine; blink refleks = 1 tick (difilter)
    ↓  Global mutex (_cmd_idle 1.5s)       Semua detector share _last_cmd_time — satu fire → blokir semua, dua arah
Normalization + EMA           ← [✅ Impl.] Rolling percentile p10–p90 + EMA=0.20
    ↓                                     Juga track raw µV² + spectral centroid Hz untuk display UI
Mental State Classifier       ← [✅ Impl.] 3-state calm/flow/tense
    ↓                                      arousal index 0.50β−0.30α−0.20TBR
    ↓                                      flow_score = frontal_α + frontal_θ − β (AF7/AF8)
    ↓                                      spectrum_pos 0..1 → calm (<0.35) / flow (0.35–0.65) / tense (>0.65)
    ↓                                      Adaptive threshold: warm-up 60s → median+0.03
    ↓                                      Vote buffer 20 tick (70% supermajority)
BrainBeat Drum Engine         ← [✅ Impl.] FluidSynth GM channel 9 (drums only)
(music_engine.py)                          CALM: brush jazz 55–65 BPM
    ↓                                      FLOW: groove mid-tempo 72–85 BPM
    ↓                                      TENSE: battle drums 95–135 BPM
    ↓                                      STRESS escalation (tense_level > 0.65): double-time kick
    ↓                                      Musik HANYA aktif saat Muse 2 terhubung
WebSocket Server              ← [✅ Impl.] Flask-SocketIO, port 8765
(music_server.py)                          payload: state, BPM, tense_level,
    ↓                                      arousal, threshold, confidence, consistency,
    ↓                                      warming_up, eeg_active
    ↓                                      θ/α/β raw µV², θ/α/β Hz centroid, HR, muse status
    ↓                                      event: eyebrow_raise (SocketIO emit)
HTML/CSS/JS Overlay           ← [✅ Impl.] "BRAINWAVE MONITOR" — single card layout:
(templates/index.html)                     state badge (CALM/FLOW/TENSE) + HR + mental command trigger,
    ↓                                      spectrum slider (spectrum_pos 0..1),
    ↓                                      waveform canvas θ/α/β + Hz centroid per band,
    ↓                                      EEG channel map SVG (TP9/AF7/AF8/TP10)
    ↓                                      status: connecting / reconnecting / connected / error
Overlay FX                    ← [✅ Impl.] templates/overlay_mental_command.html — http://localhost:8765/overlay/mental-command
(templates/overlay_mental_command.html)                   Triggered by wink / jaw_clench / eyebrow_raise / double_jaw events via SocketIO
    ↓                                      Visual: electric arc + scan line + edge glow + center text
    ↓                                      Auto-hide setelah 2.8s (4s untuk double_jaw + combo badge), fade-out 1.4s
OBS Browser Source            ← index.html: monitor overlay (port 8765)
                                 overlay_mental_command.html: mental command overlay (/overlay/mental-command)
```

### Kenapa HTML Browser Source (bukan aplikasi desktop)?
- Background transparan native — tidak perlu crop manual di OBS
- Animasi CSS lebih smooth dan mudah dikustomisasi
- Standar yang sudah banyak dipakai streamer profesional untuk overlay custom
- Tidak ada risiko overlay geser saat window tidak sengaja dipindah

---

## Paradigma BCI yang Digunakan

| Paradigma | Keterangan | Cocok untuk Muse 2 | Peran |
|---|---|---|---|
| **Band Power Classification** | Klasifikasi alpha/beta/theta untuk mental state | ✅ | **Utama** — deteksi kondisi emosional/mental |
| **SSVEP** | Fokus mata ke objek berkedip di layar | ✅ | Opsional — untuk active command jika diperlukan |
| **P300** | Respons otak terhadap stimulus target | ✅ | Opsional |
| **Motor Imagery** | Membayangkan gerakan tangan/kaki | ❌ (tidak ada C3/C4) | Tidak digunakan |

> **Catatan:** Active command via SSVEP masih dipertimbangkan sebagai fitur tambahan yang disesuaikan dengan game yang dimainkan. Command spesifik belum ditentukan (TBD).

---

## Software Stack

```
Muse 2 (Bluetooth BLE)
    ↓
muselsl (subprocess)          ← Stream EEG 256Hz + PPG 64Hz via LSL
    ↓
pylsl StreamInlet             ← Baca LSL stream di main process
    ↓
BrainFlow DataFilter          ← PSD Welch, band power, filtering only
    ↓
Feature Extraction            ← Band power θ/α/β + EMG rejection + EMA
    ↓
Mental State Classifier       ← Threshold-based: arousal index + adaptive threshold
    ↓
Flask-SocketIO (WebSocket)    ← Server penghubung Python → Browser (port 8765)
    ↓
HTML/CSS/JS                   ← Overlay UI realtime
    ↓
OBS Browser Source            ← Tampil di live stream
```

### Library & Tools

| Tool | Fungsi | Status |
|---|---|---|
| **muselsl** | Akuisisi EEG + PPG dari Muse 2 via Bluetooth, stream ke LSL | ✅ Digunakan |
| **pylsl** | Baca LSL stream (EEG 256Hz + PPG 64Hz) di main process | ✅ Digunakan |
| **BrainFlow DataFilter** | PSD Welch, band power computation, filtering only (bukan BoardShim) | ✅ Digunakan |
| **bleak** | BLE scanner — scan Muse 2 device untuk mendapatkan MAC address | ✅ Digunakan |
| **numpy** | Komputasi numerik — band power, normalisasi, EMA | ✅ Digunakan |
| **FluidSynth** | Audio engine untuk generative drums — render soundfont GM ch9 | ✅ Digunakan |
| **pyfluidsynth** | Python binding untuk FluidSynth | ✅ Digunakan |
| **Flask-SocketIO** | WebSocket server — jembatan Python ke browser overlay (port 8765) | ✅ Digunakan |
| **HTML/CSS/JS** | UI overlay yang ditampilkan via OBS Browser Source | ✅ Digunakan |
| **OBS Studio** | Software streaming, menampilkan overlay via Browser Source | ✅ Target output |
| **scikit-learn** | Machine learning classifier mental state (SVM/LDA) | 🔲 Planned (upgrade dari threshold) |
| **MNE-Python** | Preprocessing, filtering, visualisasi EEG lanjutan | 🔲 Planned |
| **EEG-Notebooks** | Dataset dari Muse asli untuk training/fine-tuning classifier | 🔲 Planned |

---

## Dataset untuk Simulasi (Sebelum Device Tiba)

> ⚠️ Jangan gunakan dataset lab 64-channel yang dipotong 4 channel — noise profile, tipe elektroda, dan referensi-nya berbeda fundamental dari Muse 2.

| Dataset | Sumber | Konten | Prioritas |
|---|---|---|---|
| **EEG-Notebooks** | github.com/NeuroTechX/eeg-notebooks | Dataset dari Muse asli, auto-download | ⭐ Utama |
| **yueeyue/Muse2-eeg** | github.com/yueeyue/Muse2-eeg | 6 aktivitas, 8 subjek, format CSV RAW_TP9/AF7/AF8/TP10 | Alternatif |
| **OSF Academic** | osf.io/u6y9g | 37 subjek, Muse 2, paradigma kognitif | Referensi riset |

---

## Pelatihan Mental State Classifier

### Strategi: Transfer Learning
Pre-train model menggunakan dataset publik Muse (EEG-Notebooks), lalu fine-tune dengan data rekaman sendiri. Ini mengurangi jumlah data personal yang dibutuhkan secara signifikan.

### Kebutuhan Data

| State | Jumlah Sesi | Durasi/Sesi | Hari Berbeda? |
|---|---|---|---|
| Band Power (semua mental state) | 3–7 sesi | ~20–30 menit | ✅ Wajib |

> Sinyal EEG berubah setiap hari (pengaruh tidur, stres, mood). Merekam di hari berbeda membuat classifier jauh lebih robust.

### Struktur Sesi Rekaman

Tiap sesi mencakup fase berikut secara bergantian:
- **Fase rileks** (~5 menit) — duduk tenang, mata terbuka
- **Fase fokus** (~5 menit) — mengerjakan soal matematika atau membaca teks
- **Fase stres ringan** (~5 menit) — tugas dengan tekanan waktu
- **Istirahat** (~2–3 menit) antar fase

> Catatan: 2–3 menit pertama setiap sesi sinyal belum stabil — elektroda dry butuh waktu kontak yang baik dengan kulit kepala. Mulai rekaman setelah periode settling ini.

### Target Kualitas

| Metrik | Minimum | Target |
|---|---|---|
| Akurasi classifier mental state | 70% | 85%+ |
| Latency update overlay | <500ms | <200ms |
| Stabilitas antar hari | >65% | >80% |

---

## Roadmap

| Fase | Status | Aktivitas |
|---|---|---|
| **Fase 1** | ✅ Selesai | Pipeline lengkap: muselsl + pylsl + BrainFlow DataFilter + adaptive threshold + drum engine + OBS overlay |
| **Fase 2** | ✅ Selesai | Integrasi Muse 2 realtime — BLE acquisition, EMG rejection, vote buffer, PPG heart rate |
| **Fase 3** | ✅ Selesai | Overlay OBS live — kalibrasi threshold, signal quality, warming-up indicator, waveform display |
| **Fase 4a** | ✅ Selesai | Active command via eyebrow raise — bilateral AF7+AF8 EMG detection, overlay FX, auto-reconnect |
| **Fase 4b** | ✅ Selesai | Mental Command Playground — 3 commands (wink, jaw clench, eyebrow raise) semua reliable |
| **Fase 4c** | ✅ Selesai | Double jaw clench (Command D) — `GestureComposer` edge counting + RMS short-tail envelope fix supaya release jaw terdeteksi real-time |
| **Fase 5** | 🔲 Next | Rekam dataset personal 3–7 sesi, train ML classifier (SVM/LDA) sebagai upgrade dari threshold |
| **Fase 6** | 🔲 Planned | Integrasi active command ke gameplay nyata — disesuaikan dengan game yang dimainkan |

---

## Ide Eksplorasi Muse 2 — Beyond the Obvious

Ide-ide di bawah ini melampaui use case utama streaming game, mencakup berbagai domain yang masih sangat underexplored di consumer level.

### 🎮 Gaming & Entertainment

**1. EEG-Reactive Drum Engine (BrainBeat)** ✅ *Live — Muse 2 terhubung*
Pola drum generatif yang berubah berdasarkan mental state secara live. Tempo dan intensitas drum mengikuti sinyal alpha/beta/theta dari Muse 2 secara langsung.

Stack: **FluidSynth 2.4.x + pyfluidsynth 1.3.4 + Soundfont GM ch9** via `music_engine.py` (BrainBeat).

| State | Karakter Drum | BPM |
|---|---|---|
| **Calm** | Brush jazz — ride 8th, side stick 2&4, kick minimal | 55–65 |
| **Tense** | Battle drums — 16th hi-hat constant, kick ganda, snare punchy | 95–135 |
| **Stress** (tense_level > 0.65) | Double-time kick, snare + tom fill escalation | 110–135 |

Adaptive threshold dikalibrasi otomatis 60 detik pertama (warm-up) berdasarkan distribusi arousal sesi itu.
UI menampilkan **SIGNAL** (confidence), **CONS** (consistency), dan **WARMING UP** indicator saat kalibrasi berlangsung.

**2. EEG-Driven Procedural Storytelling**
Game RPG/visual novel yang cabang ceritanya tidak ditentukan oleh pilihan tombol, tapi oleh mental state pemain. Kalau otak mendeteksi stres saat konfrontasi, karakter bereaksi berbeda dibanding saat rileks. Genuine adaptive narrative berbasis biofeedback.

**3. "Brain Fingerprint" Authentication**
Pola EEG setiap orang unik seperti sidik jari. Muse 2 bisa dieksplor sebagai sistem autentikasi — login ke sistem hanya saat otak yang benar terdeteksi. Masih sangat early-stage di consumer level, potensial sebagai riset orisinal.

---

### 🧘 Wellness & Self-Improvement

**4. Personalized Sleep Architecture Tracker**
Muse 2 punya rotating headband yang bisa dipakai tidur. Mapping transisi sleep stages (wake → N1 → N2 → REM) berdasarkan perubahan dominasi frekuensi EEG — lebih direct secara neurofisiologis dibanding tracker berbasis heart rate variability seperti Oura Ring.

**5. Flow State Detector & Trainer**
Flow state punya signature EEG yang spesifik — kombinasi alpha dan theta di frontal dengan beta rendah. Sistem yang bisa:
- Mendeteksi kapan benar-benar masuk flow
- Logging aktivitas apa yang secara konsisten trigger flow state
- Neurofeedback untuk melatih kemampuan masuk flow lebih cepat

Ini bisa jadi riset personal longitudinal yang sangat valuable untuk produktivitas.

**6. Emotional Journaling Otomatis**
Rekam EEG di interval tertentu sepanjang hari, lalu korelasikan dengan log aktivitas (calendar/app). Hasilnya: jurnal emosional yang objektif — bukan berdasarkan apa yang ditulis, tapi apa yang otak tunjukkan secara neurofisiologis. Contoh insight: "meeting jam 3 sore selalu memicu theta spike."

---

### 🎨 Seni & Kreativitas

**7. EEG-Generative Visual Art**
Real-time generative art yang di-drive oleh raw EEG — bukan sekedar waveform display, tapi karya visual kompleks menggunakan p5.js atau TouchDesigner. Setiap sesi menghasilkan karya unik yang merupakan "lukisan" kondisi otak. Potensial sebagai performance art, instalasi, atau karya digital.

**8. "Brain Duet" — Collaborative EEG Music**
Dua orang memakai Muse 2 masing-masing, dan musik yang dihasilkan merupakan interaksi antara dua mental state secara real-time. Kalau keduanya sync (alpha coherence tinggi) → harmoni. Kalau conflict → disonansi. Konsep ini ada di paper akademik tapi belum ada yang membuat versi accessible untuk publik umum.

**9. EEG Cinema — Film yang Bereaksi ke Penonton**
Film pendek yang editing-nya berubah berdasarkan respons EEG penonton. Scene yang memicu arousal tinggi diperpanjang, scene boring di-skip. Setiap penonton menonton versi film yang berbeda. Pernah dieksplor di lab neuroscience, belum ada versi consumer-accessible.

---

### 🔬 Riset Personal & Citizen Science

**10. Cognitive Load Mapping Aktivitas Sehari-hari**
Systematic tracking cognitive load dari berbagai aktivitas: membaca vs scrolling sosmed, belajar bahasa baru vs mengerjakan matematika, meditasi vs journaling. Data personal longitudinal dengan bukti neurofisiologis ini belum banyak dimiliki orang secara terstruktur.

**11. "Digital Detox" Biofeedback**
Ukur dampak nyata screen time terhadap otak — bukan berdasarkan laporan subjektif, tapi EEG sebelum dan sesudah. Apakah 2 jam scrolling benar-benar berbeda dengan 2 jam membaca buku secara neurofisiologis? Pertanyaan yang bisa dijawab sendiri dengan data.

**12. Korelasi EEG dengan Performa Olahraga**
Rekam EEG saat pemanasan, mid-exercise, dan recovery. Mapping hubungan antara mental state EEG dengan performa fisik. Data jenis ini biasanya hanya dimiliki tim olahraga profesional dengan biaya besar.

---

### 🤖 AI & Tech Integration

**13. LLM yang "Membaca" Mood via EEG**
Integrasi pipeline: Muse 2 → mental state classifier → context injection ke LLM. AI yang responnya beradaptasi bukan hanya dari apa yang diketik, tapi dari kondisi mental saat itu. Kalau stres terdeteksi → AI lebih supportive. Kalau fokus tinggi → AI lebih teknikal dan dense. Belum ada implementasi publik yang terdokumentasi.

**14. EEG-Triggered Smart Home**
Bukan voice command, bukan gesture — tapi lingkungan yang bereaksi ke mental state. Lampu redup otomatis saat alpha tinggi (rileks), musik berhenti saat theta spike (ngantuk), notifikasi di-mute saat fokus terdeteksi. Truly ambient computing berbasis neurofisiologi.

**15. "Attention Economy" Personal Research**
Ukur secara objektif mana konten yang benar-benar menarik perhatian otak vs yang hanya menarik mata. Menggunakan EEG sebagai ground truth untuk attention, bukan self-report. Relevan dan potensial publishable sebagai citizen science research.

---

### Prioritas Berdasarkan Novelty & Feasibility

```
Tinggi novelty + Feasible dengan Muse 2:
  → Flow State Detector & Trainer       (personal value tinggi, data longitudinal unik)
  → EEG Generative Art                  (showcase & performance potential)
  → LLM + EEG mood integration          (cutting-edge, belum ada implementasi publik)
  → Emotional Journal Otomatis          (riset longitudinal yang genuinely baru)

High novelty, kompleksitas lebih tinggi:
  → Brain Duet / EEG Cinema             (butuh lebih banyak infrastruktur)
  → Sleep Architecture Tracker          (butuh data malam, comfort perlu diuji)
  → Brain Fingerprint Auth              (butuh riset keamanan lebih dalam)
```

> **Catatan:** Yang paling underexplored dan genuinely bisa dipioneer di consumer level adalah **LLM + EEG integration** dan **Flow State longitudinal research** — hampir tidak ada yang melakukan ini dengan dokumentasi publik yang baik.

---

## Referensi & Komunitas

- **Riset:** 200+ peer-reviewed studies menggunakan Muse
- **Komunitas:** NeurotechX, OpenBCI Forum, GitHub topics: `muse-headband`
- **Dataset utama:** EEG-Notebooks (NeuroTechX) — direkam dari Muse asli
- **Dokumentasi BrainFlow:** https://brainflow.readthedocs.io
- **Dokumentasi MuseLSL:** https://github.com/alexandrebarachant/muse-lsl
- **EEG-Notebooks:** https://github.com/NeuroTechX/eeg-notebooks

---

## Catatan Penting

- Muse 2 **tidak memerlukan subscription** untuk mengakses raw EEG data (berbeda dengan Emotiv yang memerlukan EmotivPRO)
- Shipping tersedia worldwide, termasuk Indonesia (estimasi bea masuk 10–20% dari harga produk)
- Alternatif yang dipertimbangkan sebelumnya:
  - **Emotiv Insight** — masalah fulfillment sejak Feb 2026, raw data butuh EmotivPRO berbayar
  - **Neurosity Crown** — Motor Imagery lebih baik tapi tidak ada elektroda occipital untuk SSVEP, harga ~$999
  - **OpenBCI Cyton + Headband Kit** — harga terlalu tinggi ~$1,448
  - **Unicorn Hybrid Black** — SSVEP official support tapi harga ~$1,089
  - **BrainBit Flex4** — fleksibel tapi hanya 4 channel, harga ~$499
- Konsep full EEG game control (Motor Imagery untuk racing game) tidak feasible dengan consumer EEG saat ini karena latency dan akurasi belum memadai untuk real-time racing
- Pendekatan Passive BCI (mental state monitoring) dipilih sebagai use case utama karena lebih achievable, lebih menarik untuk konten streaming, dan lebih solid secara riset
