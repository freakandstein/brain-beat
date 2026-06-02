# EEG Research — Device Summary

## Status Implementasi

| Komponen | Status | Keterangan |
|---|---|---|
| **music_engine.py** | ✅ Selesai | Generative music via FluidSynth, binary calm/tense, bar-locked transitions, tense build-up level |
| **music_server.py** | ✅ Selesai | Flask + SocketIO, port 8765, emit state + arousal/confidence/consistency + raw µV² + HR |
| **Web UI (index.html)** | ✅ Selesai | OBS overlay — BPM, HR, BUILD bar, SIGNAL (confidence), CONS (consistency), EEG channel map, waveform Hz display |
| **State detection** | ✅ Selesai | Binary calm/tense — weighted arousal index + raw TBR drowsy override + asymmetric vote buffer |
| **EMG rejection** | ✅ Selesai | Two-pass architecture: pre-scan AF7/AF8 frontal EMG, volume conduction blanking ke TP9/TP10 |
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
| **Tense / Aroused** | Beta dominan, alpha & TBR rendah | `arousal = 0.50·β − 0.25·α − 0.25·TBR > 0.02` |
| **Calm / Relaxed** | Alpha/theta dominan, beta rendah | `arousal ≤ 0.02` — atau drowsy override aktif |

**Drowsy override** (anti-misklasifikasi mengantuk sebagai tense):
```
tbr_raw > 2.0            → force calm  (theta 2× beta secara absolut)
tbr_norm > 0.45 AND beta_norm < 0.45  → force calm  (normalized check)
beta_norm < 0.25         → force calm  (beta sangat rendah = hampir tidur)
```

> **Kenapa dead zone 0.02?** Fluktuasi EEG minor selalu ada; arousal harus *jelas* positif untuk vote tense, bukan sekedar sedikit di atas nol.

> Nilai alpha/beta/theta/TBR yang dikirim ke engine adalah **normalized 0–1** (rolling percentile p10–p90, window 60 sampel).
> Raw band power dalam **µV²** dikirim ke UI terpisah.
> Channel quality < 0.25 **dikecualikan** dari band power computation.

### State Smoothing (Anti-Flicker)

Nilai EEG mentah berfluktuasi setiap 16th note tick. Tanpa smoothing, state bisa loncat-loncat meski kondisi otak masih sama.

**Mekanisme**: asymmetric vote buffer 12 tick — **masuk tense lebih ketat, keluar lebih mudah**.

| Parameter | Nilai | Efek |
|---|---|---|
| Vote buffer size | 12 tick | ~1.5–2.5 detik tergantung BPM |
| Masuk tense | ≥ 8/12 = 67% | Beta harus sustained ~1.5 detik |
| Keluar tense | ≥ 4/12 = 33% calm | Relaks cepat begitu kondisi membaik |
| Implementasi | `Counter(deque(maxlen=12))` | O(1) per tick |

### Pipeline Overlay Stream

```
Muse 2 (Bluetooth)
    ↓
muselsl subprocess            ← [✅ Impl.] Stream EEG + PPG via LSL
    ↓
pylsl StreamInlet             ← [✅ Impl.] Baca EEG 256Hz + PPG 64Hz
    ↓
BrainFlow DataFilter          ← [✅ Impl.] PSD Welch, band power (filter only)
    ↓
Channel Quality Filter        ← [✅ Impl.] Skip channel quality < 0.25
    ↓
Band Power θ/α/β              ← [✅ Impl.] 4–8 / 8–13 / 13–25 Hz (delta dihapus — hanya deep sleep)
    ↓
EMG Rejection (2-pass)        ← [✅ Impl.] Pass 1: pre-scan AF7/AF8 frontal EMG sebelum temporal
    ↓                                      Pass 2: beta dari TP9/TP10 di-blank jika frontal EMG aktif
Normalization + EMA           ← [✅ Impl.] Rolling percentile p10–p90 + EMA=0.20
    ↓                                     Juga track raw µV² + spectral centroid Hz untuk display UI
Mental State Classifier       ← [✅ Impl.] Binary calm/tense — arousal index 0.50β−0.25α−0.25TBR
    ↓                                      Drowsy override: tbr_raw>2.0 OR beta<0.25
    ↓                                      Asymmetric vote buffer 12 tick (67% masuk, 33% keluar)
Generative Music Engine       ← [✅ Impl.] FluidSynth + MIDI per state
(music_engine.py)                          calm (BPM 55–72) vs tense (BPM 80–130)
    ↓                                      bar-locked transitions (t%8==0, setiap half-bar)
    ↓                                      Musik HANYA aktif saat Muse 2 terhubung
WebSocket Server              ← [✅ Impl.] Flask-SocketIO, port 8765
(music_server.py)                          payload: state, BPM, tense_level,
    ↓                                      arousal, confidence, consistency, eeg_active
    ↓                                      θ/α/β raw µV², θ/α/β Hz centroid, HR, muse status
HTML/CSS/JS Overlay           ← [✅ Impl.] BPM, HR, BUILD bar, SIGNAL (confidence),
(templates/index.html)                     CONS (consistency), EEG channel map SVG,
    ↓                                      waveform canvas + Hz centroid per band
OBS Browser Source            ← Ditampilkan di stream
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
Muse 2 (Bluetooth)
    ↓
BrainFlow (Python)            ← Akuisisi data real-time [JALUR UTAMA]
    ↓
MNE-Python / SciPy            ← Preprocessing & filtering
    ↓
Feature Extraction            ← Band power (alpha, beta, theta)
    ↓
Classifier                    ← scikit-learn (SVM/LDA)
    ↓
Flask-SocketIO (WebSocket)    ← Server penghubung Python → Browser
    ↓
HTML/CSS/JS                   ← Overlay UI realtime
    ↓
OBS Browser Source            ← Tampil di live stream
```

> MuseLSL / BlueMuse tetap bisa digunakan sebagai jalur alternatif, terutama jika ingin kompatibilitas dengan tools berbasis LSL.

### Library & Tools

| Tool | Fungsi | Status |
|---|---|---|
| **BrainFlow** | Library utama akuisisi EEG real-time, support Muse 2 langsung | 🔲 Belum diintegrasikan |
| **MNE-Python** | Preprocessing, filtering, visualisasi EEG | 🔲 Belum |
| **scikit-learn** | Machine learning classifier mental state (SVM/LDA) | 🔲 Belum (pakai threshold dulu) |
| **FluidSynth** | Audio engine untuk generative music — render soundfont GM | ✅ Digunakan |
| **pyfluidsynth** | Python binding untuk FluidSynth | ✅ Digunakan |
| **Flask-SocketIO** | WebSocket server — jembatan Python ke browser overlay | ✅ Digunakan |
| **HTML/CSS/JS** | UI overlay yang ditampilkan via OBS Browser Source | ✅ Digunakan |
| **OBS Studio** | Software streaming, menampilkan overlay via Browser Source | ✅ Target output |
| **EEG-Notebooks** | Dataset dari Muse asli + koleksi eksperimen BCI | 🔲 Untuk simulasi nanti |
| **MuseLSL** | Alternatif connector via LSL | 🔲 Backup |
| **Mind Monitor** | App third-party untuk streaming data via OSC (backup) | 🔲 Backup |

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

| Fase | Kapan | Aktivitas |
|---|---|---|
| **Fase 1** | Sekarang (tanpa device) | Bangun pipeline lengkap dengan dataset publik Muse; buat prototype overlay HTML |
| **Fase 2** | Saat Muse 2 tiba | Rekam data sendiri 3–7 sesi di hari berbeda, fine-tune classifier mental state |
| **Fase 3** | ~2 minggu setelah device | Integrasi overlay ke OBS, kalibrasi threshold, uji latency realtime saat gaming |
| **Fase 4** | Setelah overlay stabil | Eksplorasi active command (SSVEP) sebagai fitur tambahan — disesuaikan dengan game |

---

## Ide Eksplorasi Muse 2 — Beyond the Obvious

Ide-ide di bawah ini melampaui use case utama streaming game, mencakup berbagai domain yang masih sangat underexplored di consumer level.

### 🎮 Gaming & Entertainment

**1. Emotion-Reactive Generative Music** ✅ *Live — Muse 2 terhubung*
Real-time komposisi musik yang berubah berdasarkan mental state secara live. Melodi, tempo, dan harmoni mengikuti sinyal alpha/beta/theta secara langsung dari Muse 2.

Stack: **FluidSynth 2.4.x + pyfluidsynth 1.3.4 + Soundfont GM** via `music_engine.py`.

| State | Karakter Musik | BPM |
|---|---|---|
| **Calm** | String pad + choir, reverb dalam, maj7 progressi, ostinato pelan | 55–65 |
| **Tense** | Brass swell, ostinato cepat, glitch stuttering, disonansi cluster | 80–130 |

Transisi antar state dikunci ke bar boundary (16th note tick, t%16==0) — tidak abrupt mid-bar.
UI menampilkan **SIGNAL** (confidence: seberapa jauh dari ambang batas 0.0) dan **CONS** (consistency: proporsi vote buffer yang sepakat) untuk monitoring signal quality.

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
