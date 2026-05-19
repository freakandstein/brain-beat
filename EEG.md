# EEG Research — Device Summary

## Status Implementasi

| Komponen | Status | Keterangan |
|---|---|---|
| **music_engine.py** | ✅ Selesai | Generative music via FluidSynth, 4 state |
| **music_server.py** | ✅ Selesai | Flask + SocketIO, port 8765, auto-kill |
| **Web UI (index.html)** | ✅ Selesai | OBS overlay, realtime state + BPM + sliders |
| **State detection** | ✅ Selesai | Threshold-based (band power rules) |
| **BrainFlow integration** | 🔲 Belum | Masih pakai nilai manual / preset |
| **ML Classifier** | 🔲 Belum | Threshold cukup untuk tahap awal |
| **Muse 2 device** | 🔲 Belum tiba | Dataset simulasi bisa digunakan dulu |

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
| **Akses Raw EEG** | ✅ Via BrainFlow (direkomendasikan) atau MuseLSL |

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

| State | Sinyal EEG | Channel Utama | Threshold (implementasi saat ini) |
|---|---|---|---|
| **Fokus/Konsentrasi** | Beta tinggi (13–30Hz) | AF7/AF8 | beta > 0.6 AND alpha < 0.35 |
| **Rileks/Flow State** | Alpha tinggi (8–13Hz) | TP9/TP10 | alpha > 0.55 AND beta < 0.4 |
| **Stres/Frustrasi** | Theta + Beta tinggi | AF7/AF8 | beta > 0.65 AND theta > 0.55 |
| **Kantuk/Bosan** | Theta dominan (4–8Hz) | Semua channel | theta > 0.65 |

> Deteksi saat ini menggunakan **threshold rules** pada nilai band power yang dinormalisasi (0–1).
> Upgrade ke ML classifier (SVM/LDA) direncanakan setelah data dari Muse 2 terkumpul.

### Pipeline Overlay Stream

```
Muse 2 (Bluetooth)            ← [🔲 Belum] Device belum tiba
    ↓
BrainFlow (Python)            ← [🔲 Belum] Akuisisi data realtime
    ↓
MNE-Python / SciPy            ← [🔲 Belum] Preprocessing & filtering
    ↓
Band Power Extraction          ← [🔲 Belum] Hitung power alpha/beta/theta
    ↓
Mental State Classifier        ← [✅ Impl.] Threshold rules di music_engine.py
    ↓                                       (ML upgrade nanti jika diperlukan)
Generative Music Engine        ← [✅ Impl.] FluidSynth + MIDI per state
(music_engine.py)                           4 state: fokus/rileks/stres/kantuk
    ↓
WebSocket Server               ← [✅ Impl.] Flask-SocketIO, port 8765
(music_server.py)
    ↓
HTML/CSS/JS Overlay            ← [✅ Impl.] State badge, BPM, sliders, preset
(templates/index.html)
    ↓
OBS Browser Source             ← Ditampilkan di stream
```

> **Saat ini:** Nilai alpha/beta/theta diinput manual via slider/preset di Web UI.
> Saat Muse 2 tiba, ganti dengan BrainFlow loop yang memanggil `engine.set_eeg()`.

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

**1. Emotion-Reactive Generative Music** ✅ *In progress*
Real-time komposisi musik yang berubah berdasarkan mental state secara live — bukan sekedar playlist berdasarkan mood. Melodi, tempo, dan harmoni mengikuti sinyal alpha/beta/theta secara langsung.

Stack yang dipakai: **FluidSynth + Soundfont** (Python) via `music_engine.py`.

| State | Karakter Musik |
|---|---|
| Fokus | String ostinato + brass build-up (Hans Zimmer style) |
| Rileks | String pad + choir, reverb dalam, maj7 chord |
| Stres | Glitch stuttering, cluster disonan, dry/abrasif |
| Kantuk | Open fifth mengambang, tempo sangat lambat |

Input saat ini: keyboard manual. Nanti: nilai band power langsung dari BrainFlow.

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
