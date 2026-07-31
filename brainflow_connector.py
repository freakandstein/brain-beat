"""
Muse 2 Connector — muselsl + pylsl
====================================
Akuisisi EEG & PPG dari Muse 2 menggunakan muselsl (subprocess) dan
pylsl (membaca LSL streams). Signal processing tetap pakai BrainFlow
DataFilter.

Install: pip3 install muselsl pylsl

Digunakan oleh music_server.py (interface tidak berubah).
"""

import csv
import json
import subprocess
import sys
import threading
import time
import tempfile
import os
from datetime import datetime
from typing import Callable, Optional

import numpy as np

# BrainFlow DataFilter — signal processing only (no BoardShim needed)
try:
    from brainflow.data_filter import DataFilter, DetrendOperations, WindowOperations, FilterTypes
    _BF_FILTER = True
except ImportError:
    _BF_FILTER = False

# pylsl — reading LSL streams in main process
try:
    from pylsl import StreamInlet, resolve_byprop
    _PYLSL = True
except ImportError:
    _PYLSL = False

# muselsl — availability check (used via subprocess, not imported directly)
try:
    import muselsl  # noqa
    _MUSELSL = True
except ImportError:
    _MUSELSL = False

# bleak — BLE scanning only
try:
    from bleak import BleakScanner
    BLEAK_AVAILABLE = True
except ImportError:
    BLEAK_AVAILABLE = False

# music_server.py imports this flag to gate connector usage
BRAINFLOW_AVAILABLE = _MUSELSL and _PYLSL and _BF_FILTER

SAMPLE_RATE = 256   # Muse 2 EEG Hz
PPG_SR      = 64    # Muse 2 PPG Hz


def scan_muse_devices(timeout: float = 5.0) -> list:
    """Scan BLE and return [(name, address)] for Muse devices found."""
    if not BLEAK_AVAILABLE:
        return []
    import asyncio

    async def _scan():
        devices = await BleakScanner.discover(timeout=timeout)
        return [
            (d.name or "Muse", d.address)
            for d in devices
            if d.name and "muse" in d.name.lower()
        ]

    try:
        loop = asyncio.new_event_loop()
        result = loop.run_until_complete(_scan())
        loop.close()
        return result
    except Exception as e:
        print(f"⚠️  BLE scan error: {e}")
        return []


class GestureComposer:
    """
    Layer di atas detector jaw_clench yang mendeteksi double_jaw (2× clench
    dalam satu window) dengan EDGE-TRIGGERED COUNTING.

    Wink di-fire langsung dari detector (lihat MuseConnector.on_wink_left /
    on_wink_right), tidak
    lewat composer — composer ini fokus murni pada jaw counting.

    Cara kerja (edge counting):
      Detector mengirim 1 event per RISING EDGE (saat clench mulai), bukan per
      tick. Durasi gesture (mis. clench ditahan lama) tidak relevan.

      Setiap event men-start/restart timer DECIDE_DELAY. Selama timer berjalan,
      event berikutnya menambah hitungan. Saat timer expire (tidak ada event
      baru dalam DECIDE_DELAY detik), composer memutuskan:
        - 1 jaw            → single jaw clench
        - 2+ jaw           → double jaw
      Single terasa delay DECIDE_DELAY ms (waktu nunggu kemungkinan event kedua).
    """

    DECIDE_DELAY = 1.5   # detik — tunggu clench kedua, DIUKUR DARI RELEASE.
                          # Karena timer di-restart saat rahang dilepas (bukan
                          # saat clench mulai), durasi clench tidak relevan.
                          # Single jaw fire ~0.6s setelah rahang dilepas.
                          # Diturunkan dari 1.0s — double jaw terasa lebih snappy,
                          # clench kedua tetap harus masuk dalam window ini.

    ENABLE_DOUBLE_JAW = True

    def __init__(self):
        self.on_jaw_clench: Optional[Callable] = None
        self.on_double_jaw: Optional[Callable] = None

        self._jaw_count:  int = 0
        self._timer: Optional[threading.Timer] = None

    # ── public notify methods (dipanggil dari detector) ───────────────────

    def notify_jaw(self, t: float) -> bool:
        """
        Dipanggil 1× per clench rising edge (saat clench MULAI).
        TIDAK start timer di sini — karena clench bisa ditahan lama, timer yang
        mulai di sini akan expire mid-clench sebelum rahang dilepas. Timer hanya
        dimulai saat RELEASE (notify_jaw_release).
        Selama clench ditahan, timer di-cancel (clench belum selesai).
        """
        self._jaw_count += 1
        if self._timer:
            self._timer.cancel()
            self._timer = None
        return True

    def notify_jaw_release(self) -> None:
        """
        Dipanggil saat rahang DILEPAS (signal turun di bawah release threshold).
        DI SINI timer keputusan dimulai — window 'tunggu clench kedua' diukur dari
        saat lepas. Jadi durasi clench (berapa lama ditahan) tidak relevan: berapa
        pun lamanya, hitungan baru jalan setelah rahang benar-benar dilepas.
        """
        if self._jaw_count > 0:
            self._restart_timer()

    # ── internal ──────────────────────────────────────────────────────────

    def _restart_timer(self):
        if self._timer:
            self._timer.cancel()
        self._timer = threading.Timer(self.DECIDE_DELAY, self._decide)
        self._timer.daemon = True
        self._timer.start()

    def _decide(self):
        """Timer expire → putuskan single/double jaw berdasarkan hitungan event."""
        j = self._jaw_count
        self._jaw_count = 0
        self._timer     = None

        if j >= 2 and self.ENABLE_DOUBLE_JAW:
            print(f"🦷🦷  Double jaw FIRED (j={j})")
            self._fire(self.on_double_jaw, "on_double_jaw")
        elif j >= 1:
            print(f"🦷  Jaw clench FIRED (single, j={j})")
            self._fire(self.on_jaw_clench, "on_jaw_clench")

    @staticmethod
    def _fire(cb: Optional[Callable], name: str):
        if cb:
            try:
                cb()
            except Exception as e:
                print(f"⚠️  {name} error: {e}")

    def reset(self):
        if self._timer:
            self._timer.cancel()
            self._timer = None
        self._jaw_count = 0


class MuseConnector:
    """
    Thread-safe Muse 2 connector.
    Uses muselsl (subprocess) for BLE acquisition + pylsl for reading.
    Same public interface as the previous BrainFlow-based version.

        conn = MuseConnector(engine, on_status=lambda s, e: print(s, e))
        conn.connect("45A06A8D-FC1E-6656-6CC2-BA3EF830CF41")
        conn.disconnect()
    """

    # File cache kalibrasi tilt — posisi headset user konsisten antar sesi,
    # jadi hasil _run_tilt_calibration sesi sebelumnya dipakai lagi sebagai
    # starting point (skip nunggu 3-sample) alih-alih re-kalibrasi dari nol
    # tiap connect. Lihat _load_tilt_calibration/_save_tilt_calibration.
    _TILT_CALIB_CACHE_PATH = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "tilt_calibration.json"
    )

    def __init__(self, engine, on_status: Optional[Callable] = None):
        if not BRAINFLOW_AVAILABLE:
            raise RuntimeError(
                "muselsl, pylsl, or brainflow not installed.\n"
                "Run: pip3 install muselsl pylsl brainflow"
            )

        self.engine    = engine
        self.on_status = on_status

        # Public state
        self.status             = "disconnected"
        self.error_msg          = ""
        self.heart_rate: Optional[float] = None
        self.channel_quality: dict = {"TP9": 0.0, "AF7": 0.0, "AF8": 0.0, "TP10": 0.0}
        self.raw_bands: dict = {"alpha": 0.0, "beta": 0.0, "theta": 0.0}
        self.peak_hz:  dict = {"alpha": None, "beta": None, "theta": None}
        self.tbr: float = 0.5   # Theta/Beta Ratio frontal (0=focused, 1=drowsy)
        # Frontal-only normalized values (AF7+AF8) — dipakai untuk flow_score
        self.frontal_alpha: float = 0.5
        self.frontal_theta: float = 0.5

        # Eyebrow raise detection — callback dipanggil saat terdeteksi
        self.on_eyebrow_raise: Optional[callable] = None
        self._eyebrow_cooldown:     float = 0.0
        self._eyebrow_streak:       int   = 0
        self._eyebrow_miss:         int   = 0    # tick non-bilateral BERUNTUN — toleransi 1 tick noise
        self._eyebrow_active_until: float = 0.0  # zona blokir wink/jaw saat bilateral aktif

        # ── Mental command playground (3 modalitas campuran) ──────────────
        # Tongue press (EMG 15-40Hz sedang TP9/TP10), Jaw clench (EMG broadband
        # kuat TP9/TP10), dan Eyebrow raise (EMG frontal bilateral AF7/AF8).
        # Tongue dan jaw pakai electrode yang sama tapi dibedakan amplitudo & band.
        self.on_wink_left: Optional[callable] = None
        self.on_wink_right: Optional[callable] = None
        self.on_jaw_clench: Optional[callable] = None
        self.on_eyes_closed_relax: Optional[callable] = None
        self.on_double_blink: Optional[callable] = None   # deprecated
        self.on_teeth_tap: Optional[callable] = None      # deprecated

        self._wink_cooldown: float = 0.0
        self._wink_streak: int = 0
        self._jaw_cooldown: float = 0.0
        self._jaw_strong_streak: int = 0
        self._jaw_released: bool = True   # True jika rahang sudah lepas (siap menerima clench edge baru)
        self._last_cmd_time: float = 0.0    # timestamp command apapun terakhir fire — global mutex antar detector

        # ── Head tilt (roll) command: tilt_left / tilt_right ────────────────
        # Gerakan MIRINGKAN kepala (telinga mendekat bahu) — bukan menoleh.
        # Hanya aktif saat Cursor Control Mode OFF (mutually exclusive —
        # cursor mode sudah memakai tilt kontinu sebagai joystick, jadi
        # command diskrit di sini sengaja tidak dijalankan bersamaan supaya
        # tidak dobel-fire saat user memang sedang menggerakkan cursor).
        # Kalibrasi 1-sumbu MANDIRI (bukan _calib_right_vec milik cursor
        # control) — dipicu otomatis sesaat setelah connect, independen dari
        # cursor_control_enabled, dan TIDAK di-null-kan saat cursor mode
        # ditoggle (lifecycle terpisah total).
        self.on_tilt_left: Optional[callable] = None
        self.on_tilt_right: Optional[callable] = None
        self.tilt_calib_phase: str = "idle"   # idle|neutral|right|ready — dibaca UI utk instruksi
        self.tilt_calib_progress: str = ""   # "N/M" (sample terkumpul/dibutuhkan) selama phase=="right" — dibaca UI utk feedback progres, lihat _run_tilt_calibration
        self._tilt_calib_ready: bool = False
        self._tilt_calib_vec: Optional[tuple] = None
        self._tilt_neutral: tuple = (0.0, 0.0, 1.0)   # posisi netral SENDIRI, lihat _run_tilt_calibration
        self._tilt_gyro_axis: Optional[int] = None   # 0=X/1=Y/2=Z, axis gyro dominan saat tilt kanan (kalibrasi)
        self._tilt_gyro_sign: float = 1.0             # tidak dipakai langsung saat ini, disimpan untuk diagnostik
        self._tilt_calib_thread: Optional[threading.Thread] = None
        self._tilt_calib_gen: int = 0   # generation token — cegah thread kalibrasi basi menimpa hasil baru saat reconnect cepat
        # Edge state: "idle" (netral) → "risen" (melewati threshold, nunggu
        # release) → kembali "idle" setelah release (valid) atau timeout
        # (dibuang, dianggap gerakan lambat/menahan, bukan quick tilt).
        self._tilt_state: str = "idle"
        self._tilt_rise_side: str = ""     # "left" | "right" saat _tilt_state=="risen"
        self._tilt_rise_time: float = 0.0
        self._tilt_rise_peak: float = 0.0   # |tilt_val| tertinggi selama "risen" saat ini — dasar release relatif, lihat _TILT_RELEASE_RATIO
        self._tilt_cooldown: float = 0.0
        self._tilt_refractory_until: float = 0.0   # blokir rise baru sampai timestamp ini, lihat _fire_tilt_command
        self._tilt_rearmed: bool = True   # False setelah fire sampai tilt_val terlihat dekat nol sekali — lihat _TILT_REARM_THRESHOLD. True di awal sesi (belum pernah fire, tidak perlu re-arm).
        self._tilt_last_fire_time: float = 0.0   # timestamp fire terakhir — dasar hitung _TILT_REARM_TIMEOUT_S, TIDAK berubah oleh rise berikutnya (beda dari _tilt_rise_time)
        self._tilt_diag_last: float = 0.0   # rate-limit print diagnostik [TILT], lihat _update_tilt_command
        self._tilt_still_since: Optional[float] = None   # timestamp mulai diam (gyro rendah), None = sedang bergerak — lihat _maybe_recenter_tilt_neutral
        self._tilt_abs_val_window: list = []   # buffer |tilt_val| beberapa tick terakhir — dasar cek arah gerakan (menjauh/mendekat), lihat _TILT_MOVING_AWAY_WINDOW_N
        self._relax_cooldown: float = 0.0
        self._relax_streak: int = 0         # tick BERTURUT-TURUT dgn alpha_ratio > threshold (sustained closure)
        self._relax_alpha_hist: list = []   # buffer raw frontal alpha power (uV^2) untuk baseline

        # ── Gesture composer: double_jaw detection ─────────────────────────
        # Wrap jaw_clench detector → bedakan single vs double clench
        # berdasarkan timing. Callback on_jaw_clench / on_double_jaw
        # di-assign dari luar (eeg_server.py).
        self.composer = GestureComposer()

        # ── Adaptive threshold per-sesi ───────────────────────────────────
        # Selama 15 detik pertama (CALIBRATION_TICKS tick @ ~6.7Hz), kumpulkan
        # sampel noise EMG saat istirahat. Threshold = median_baseline * multiplier.
        # Fallback ke nilai hardcoded jika kalibrasi belum selesai.
        self._CALIBRATION_TICKS = 100       # ~15 detik @ 6.7 Hz
        self._calib_frontal: list = []      # p2p max(AF7,AF8) saat istirahat
        self._calib_temporal: list = []     # p2p max(TP9,TP10) saat istirahat
        self._calib_done: bool = False
        # threshold aktif (dipakai detector) — diinisialisasi ke nilai hardcoded
        self._thr_wink:    float = 800.0
        self._thr_eyebrow: float = 300.0
        self._thr_jaw:     float = 520.0

        # ── Cursor Control Mode (head-tilt joystick, gyro+accelerometer) ────
        # Saat False: jaw clench tetap berjalan seperti biasa (OBS scene +
        # keystroke) — lihat eeg_server.py. Saat True: jaw clench single-fire
        # di-reroute jadi left-click, dan tilt kepala menggerakkan cursor OS.
        # Double-jaw TIDAK berubah (tetap toggle recording OBS) di kedua mode.
        self.cursor_control_enabled: bool = False
        self._cursor_baseline_ready: bool = False   # False selama jeda recenter (lihat _RECENTER_DELAY_S)
        self._recenter_timer: Optional[threading.Timer] = None
        self._imu_baseline: tuple = (0.0, 0.0, 1.0)
        self._tilt_ema_up:    float = 0.0
        self._tilt_ema_right: float = 0.0
        self._latest_acc:  tuple = (0.0, 0.0, 1.0)
        self._latest_gyro: tuple = (0.0, 0.0, 0.0)
        self._acc_recent:  list  = []   # rolling buffer utk baseline recenter (rata-rata, bukan 1 sample)
        self._acc_sample_count: int = 0   # increment HANYA saat chunk ACC nyata diterima (bukan kosong) — lihat _imu_loop. Dipakai kalibrasi tilt utk membedakan "belum ada data sensor sama sekali" dari "data sensor stabil"
        self._imu_thread: Optional[threading.Thread] = None
        self._imu_thread_stop = threading.Event()
        self.cursor_velocity_x: float = 0.0   # px/detik — dibaca mouse_connector
        self.cursor_velocity_y: float = 0.0
        self.on_cursor_velocity: Optional[Callable] = None   # (vx, vy) -> None

        # ── Kalibrasi arah eksplisit (right/up basis vector) ────────────────
        # Axis fisik chip Muse 2 di kepala TIDAK bisa diasumsikan statis
        # (index tetap X=roll, Y=pitch) — data nyata menunjukkan tergantung
        # cara headset terpasang, satu axis fisik bisa menangkap kombinasi
        # roll+pitch sekaligus (terbukti dari log: X punya std 3x lebih besar
        # dari Y dan berkorelasi 0.625 dengan Y — bukan axis independen murni).
        # Solusi: minta user tilt eksplisit ke KANAN lalu ke ATAS setelah
        # baseline netral, ukur vektor deviasi accel nyata sebagai basis
        # right/up — bukan menebak index axis mana yang "seharusnya" roll/pitch.
        self.cursor_calib_phase: str = "idle"   # idle|neutral|right|up|ready
        self._calib_right_vec: Optional[tuple] = None
        self._calib_up_vec: Optional[tuple] = None

        # Internal
        self.running       = False
        self._loop_tick    = 0
        self._history      = {"alpha": [], "beta": [], "theta": [], "tbr": [],
                              "frontal_alpha": [], "frontal_theta": []}
        self._HIST_LEN     = 120  # 30 s at 4 Hz — lebih responsif terhadap perubahan state
        self._stream_proc: Optional[subprocess.Popen] = None
        self._cancel       = threading.Event()
        self._mac_address  = ""        # stored for auto-reconnect
        self._err_file     = None      # temp file capturing muselsl stderr

        # ── Battery telemetry — via callback_telemetry pada koneksi BLE yang
        # SAMA dengan EEG (bukan koneksi kedua terpisah). muselsl.stream()
        # (dipakai _stream_proc untuk EEG/PPG/ACC/GYRO) tidak meneruskan
        # callback_telemetry ke Muse() yang dibuatnya secara internal, jadi
        # _launch_and_loop menyuntikkan monkey-patch kecil di script inline
        # subprocess: bungkus Muse.__init__ supaya callback_telemetry selalu
        # disisipkan sebelum stream() membuat instance-nya. Battery dicetak
        # ke stdout subprocess yang sama (dulu DEVNULL, sekarang PIPE) dan
        # dibaca oleh _stream_stdout_reader_loop.
        #
        # Versi sebelumnya membuka SUBPROCESS KEDUA yang connect BLE ke MAC
        # address yang sama, khusus untuk baca battery — di praktiknya banyak
        # adapter (termasuk yang dites di macOS ini) menolak 2 koneksi BLE
        # bersamaan ke headset yang sama, jadi battery_percent selalu None.
        # Pendekatan callback_telemetry ini menghindari masalah itu sama
        # sekali karena cuma 1 koneksi BLE yang dipakai.
        self.battery_percent: Optional[float] = None
        self._stream_stdout_thread: Optional[threading.Thread] = None

    # ── public API ────────────────────────────────────────────────────────

    def connect(self, mac_address: str = "") -> None:
        """Start connection in background thread (non-blocking)."""
        if self.running:
            self.running = False
        self._cancel.clear()
        self._set_status("connecting")
        threading.Thread(
            target=self._connect_thread, args=(mac_address,), daemon=True
        ).start()

    # Kalibrasi cursor control: 3 tahap berurutan, tiap tahap tunggu accel
    # STABIL (variance rendah) dulu sebelum lanjut — bukan delay waktu tetap
    # (terbukti dari log nyata: delay tetap masih bisa merekam baseline salah
    # kalau kepala belum benar-benar diam persis di detik yang ditentukan).
    #   1. neutral — kepala level, rekam _imu_baseline
    #   2. right   — user tilt ke kanan & tahan, rekam _calib_right_vec
    #   3. up      — user tilt ke atas & tahan, rekam _calib_up_vec
    # Kalibrasi arah (bukan cuma index axis statis) diperlukan karena axis
    # fisik chip Muse 2 tidak bisa diasumsikan sejajar sempurna dengan
    # roll/pitch anatomis — data nyata menunjukkan 1 axis kadang menangkap
    # kombinasi keduanya tergantung cara headset terpasang.
    _STABILITY_POLL_S     = 0.1     # interval cek stabilitas
    _STABILITY_WINDOW_N   = 8       # jumlah sample dicek (~0.8s @ 100ms poll)
    _STABILITY_STD_THRESH = 0.02    # std max per-axis (g) supaya dianggap "diam/tertahan"
    _STABILITY_TIMEOUT_S  = 6.0     # fallback per tahap: lanjut walau belum stabil

    def set_cursor_control(self, enabled: bool) -> None:
        """Toggle Cursor Control Mode. Velocity tetap 0 sampai seluruh
        kalibrasi 3-tahap selesai (lihat _run_cursor_calibration) — cursor
        hanya aktif setelah cursor_calib_phase == 'ready'."""
        # cursor_control_enabled di-set DULU sebelum start thread baru —
        # thread lama (jika masih berjalan dari toggle sebelumnya) mengecek
        # flag ini di while-loop-nya dan keluar sendiri dalam
        # <=_STABILITY_POLL_S detik (plain Thread, tidak perlu di-cancel).
        self.cursor_control_enabled = enabled
        self._cursor_baseline_ready = False
        self.cursor_calib_phase = "neutral" if enabled else "idle"
        self._calib_right_vec = None
        self._calib_up_vec = None
        self._recenter_timer = None
        if enabled:
            self._recenter_timer = threading.Thread(
                target=self._run_cursor_calibration, daemon=True
            )
            self._recenter_timer.start()
        else:
            self.cursor_velocity_x = 0.0
            self.cursor_velocity_y = 0.0

    def _wait_for_stable_window(self) -> Optional[list]:
        """Poll accel tiap _STABILITY_POLL_S sampai window terakhir
        (_STABILITY_WINDOW_N sample) punya std rendah di semua axis, lalu
        return window itu. _STABILITY_TIMEOUT_S adalah fallback per tahap
        supaya kalibrasi tidak macet selamanya kalau user tidak benar-benar
        menahan posisi (tetap lanjut pakai data seadanya). Return None kalau
        mode dimatikan/thread lama sebelum sempat stabil."""
        t_start = time.time()
        window: list = []
        while self.cursor_control_enabled and not self._imu_thread_stop.is_set():
            window.append(self._latest_acc)
            if len(window) > self._STABILITY_WINDOW_N:
                window.pop(0)
            elapsed = time.time() - t_start
            if len(window) >= self._STABILITY_WINDOW_N:
                arr = np.array(window)
                stds = np.std(arr, axis=0)
                if np.all(stds < self._STABILITY_STD_THRESH):
                    return window
            if elapsed > self._STABILITY_TIMEOUT_S:
                return window if window else None
            time.sleep(self._STABILITY_POLL_S)
        return None

    def _run_cursor_calibration(self) -> None:
        """Jalankan 3 tahap kalibrasi berurutan: neutral → right → up →
        ready. Tiap tahap tunggu accel stabil (_wait_for_stable_window),
        rekam vektor rata-rata window itu, lalu pindah ke tahap berikutnya.
        UI (index.html) membaca cursor_calib_phase via state_update untuk
        menampilkan instruksi yang sesuai tiap tahap.

        _CALIB_READ_DELAY_S diberi SEBELUM stability-check tiap tahap mulai
        mengukur (bukan sesudah) — kalau device sudah diam/stabil dari
        sebelumnya, _wait_for_stable_window bisa langsung mengembalikan
        window dalam <0.1s, sebelum user sempat MEMBACA instruksi yang baru
        saja muncul apalagi mulai menggerakkan kepala. Delay ini murni waktu
        baca+reaksi, terpisah dari pengukuran stabilitas itu sendiri."""
        _CALIB_READ_DELAY_S = 1.2

        # ── Tahap 1: neutral ────────────────────────────────────────────
        time.sleep(_CALIB_READ_DELAY_S)
        if not self.cursor_control_enabled:
            return
        window = self._wait_for_stable_window()
        if window is None or not self.cursor_control_enabled:
            return
        self._imu_baseline = tuple(np.mean(np.array(window), axis=0))
        self._tilt_ema_up = 0.0
        self._tilt_ema_right = 0.0
        print(f"🎯  Baseline netral direkam — {self._imu_baseline}")

        # ── Tahap 2: tilt kanan ──────────────────────────────────────────
        self.cursor_calib_phase = "right"
        time.sleep(_CALIB_READ_DELAY_S)
        if not self.cursor_control_enabled:
            return
        window = self._wait_for_stable_window()
        if window is None or not self.cursor_control_enabled:
            return
        avg = np.mean(np.array(window), axis=0)
        dev = avg - np.array(self._imu_baseline)
        dev_norm = np.linalg.norm(dev)
        if dev_norm > 1e-3:
            self._calib_right_vec = tuple(dev / dev_norm)
        else:
            # User tidak benar-benar tilt (dev nyaris nol) — fallback ke
            # asumsi X axis supaya tidak division-by-zero, tapi ini kasus
            # langka (kalibrasi gagal dipatuhi, bukan gagal deteksi).
            self._calib_right_vec = (1.0, 0.0, 0.0)
        print(f"➡️   Kalibrasi kanan direkam — dev={tuple(round(v,3) for v in dev)}")

        # ── Tahap 3: tilt atas (mendongak) ────────────────────────────────
        self.cursor_calib_phase = "up"
        time.sleep(_CALIB_READ_DELAY_S)
        if not self.cursor_control_enabled:
            return
        window = self._wait_for_stable_window()
        if window is None or not self.cursor_control_enabled:
            return
        avg = np.mean(np.array(window), axis=0)
        dev = avg - np.array(self._imu_baseline)
        dev_norm = np.linalg.norm(dev)
        if dev_norm > 1e-3:
            raw_up_vec = dev / dev_norm
        else:
            raw_up_vec = np.array([0.0, 1.0, 0.0])
        print(f"⬆️   Kalibrasi atas direkam (mentah) — dev={tuple(round(v,3) for v in dev)}")

        # Gram-Schmidt: paksa up_vec tegak lurus terhadap right_vec.
        # Secara anatomis nyaris mustahil tilt kepala murni ke kanan TANPA
        # sedikit ikut naik/turun (atau sebaliknya) — jadi 2 vektor kalibrasi
        # mentah hampir pasti tidak persis 90° satu sama lain. Kalau
        # dipakai apa adanya, gerakan MURNI ke kanan akan ikut menghasilkan
        # sinyal tilt_up (dan sebaliknya) sebesar cos(sudut_penyimpangan) —
        # ini yang menyebabkan gejala "gerak kanan/kiri jadi ikut naik/turun".
        # Buang komponen raw_up_vec yang sejajar right_vec, sisakan hanya
        # yang benar-benar ortogonal, baru re-normalize.
        right_arr = np.array(self._calib_right_vec)
        up_orthogonal = raw_up_vec - np.dot(raw_up_vec, right_arr) * right_arr
        up_norm = np.linalg.norm(up_orthogonal)
        if up_norm > 1e-3:
            self._calib_up_vec = tuple(up_orthogonal / up_norm)
        else:
            # raw_up_vec nyaris sejajar right_vec (user mungkin tidak benar2
            # tilt ke atas, atau tilt ke arah yang sama dengan kalibrasi kanan)
            # — fallback: tetap pakai raw (lebih baik daripada division by zero,
            # meski kemungkinan cross-talk masih ada di kasus langka ini).
            self._calib_up_vec = tuple(raw_up_vec)
            print("⚠️  Kalibrasi atas nyaris sejajar dengan kalibrasi kanan — "
                  "cross-talk mungkin masih terasa, ulangi kalibrasi jika perlu")
        print(f"⬆️   Kalibrasi atas (setelah ortogonalisasi) — {tuple(round(v,3) for v in self._calib_up_vec)}")

        self.cursor_calib_phase = "ready"
        self._cursor_baseline_ready = True
        print("✅  Kalibrasi cursor control selesai — siap dipakai")

    def disconnect(self) -> None:
        """Disconnect Muse 2 and kill the muselsl subprocess."""
        self.running = False
        self._cancel.set()
        self._imu_thread_stop.set()
        self._kill_proc()
        self.battery_percent = None
        self._history    = {"alpha": [], "beta": [], "theta": [], "tbr": [],
                            "frontal_alpha": [], "frontal_theta": []}
        self.heart_rate  = None
        self._eyebrow_cooldown      = 0.0
        self._eyebrow_streak        = 0
        self._eyebrow_miss          = 0
        self._eyebrow_active_until  = 0.0
        self._wink_cooldown         = 0.0
        self._wink_streak           = 0
        self._connected_at          = 0.0
        self._jaw_cooldown          = 0.0
        self._jaw_strong_streak     = 0
        self._jaw_released          = True
        self._last_cmd_time    = 0.0
        self._relax_cooldown   = 0.0
        self._relax_streak     = 0
        self._relax_alpha_hist = []
        self._calib_frontal    = []
        self._calib_temporal   = []
        self._calib_done       = False
        self._thr_wink         = 800.0
        self._thr_eyebrow      = 300.0
        self._thr_jaw          = 520.0
        # Safety: matikan cursor control total saat disconnect — tidak boleh
        # ada sesi yang meninggalkan cursor OS bergerak sendiri.
        # cursor_control_enabled = False DULU (sebelum reset lain) — thread
        # _run_cursor_calibration mengecek flag ini di while-loop-nya (lewat
        # _wait_for_stable_window) dan keluar sendiri dalam
        # <=_STABILITY_POLL_S detik, tidak perlu dibatalkan manual (ini plain
        # Thread, bukan Timer, tidak punya .cancel()).
        self.cursor_control_enabled = False
        self._cursor_baseline_ready = False
        self.cursor_calib_phase = "idle"
        self._calib_right_vec = None
        self._calib_up_vec = None
        self._recenter_timer = None
        self.cursor_velocity_x = 0.0
        self.cursor_velocity_y = 0.0
        self._imu_baseline    = (0.0, 0.0, 1.0)
        self._tilt_ema_up     = 0.0
        self._tilt_ema_right  = 0.0
        self._acc_recent      = []
        self._acc_sample_count = 0
        # Tilt command (tilt_left/tilt_right) — reset total, harus
        # dikalibrasi ulang tiap sesi connect (lihat _run_tilt_calibration).
        # gen di-increment DULU (sebelum reset flag lain) — thread kalibrasi
        # yang mungkin masih berjalan (mid-sleep/mid-wait) langsung melihat
        # gen-nya usang di iterasi berikutnya dan berhenti sendiri.
        self._tilt_calib_gen  += 1
        self._tilt_calib_ready = False
        self._tilt_calib_vec   = None
        self._tilt_neutral     = (0.0, 0.0, 1.0)
        self._tilt_gyro_axis   = None
        self._tilt_gyro_sign   = 1.0
        self._tilt_calib_thread = None
        self.tilt_calib_phase  = "idle"
        self.tilt_calib_progress = ""
        self._tilt_state       = "idle"
        self._tilt_rise_side   = ""
        self._tilt_rise_time   = 0.0
        self._tilt_rise_peak   = 0.0
        self._tilt_cooldown    = 0.0
        self._tilt_refractory_until = 0.0
        self._tilt_rearmed     = True
        self._tilt_last_fire_time = 0.0
        self._tilt_diag_last   = 0.0
        self._tilt_still_since = None
        self._tilt_abs_val_window = []
        self.composer.reset()
        self._loop_tick  = 0
        self.channel_quality = {"TP9": 0.0, "AF7": 0.0, "AF8": 0.0, "TP10": 0.0}
        self.raw_bands   = {"alpha": 0.0, "beta": 0.0, "theta": 0.0}
        self.peak_hz     = {"alpha": None, "beta": None, "theta": None}
        self.tbr         = 0.5
        self.frontal_alpha = 0.5
        self.frontal_theta = 0.5
        self._set_status("disconnected")
        print("■  Muse 2 disconnected.")

    # ── internal ──────────────────────────────────────────────────────────

    def _kill_proc(self) -> None:
        if self._stream_proc and self._stream_proc.poll() is None:
            try:
                self._stream_proc.terminate()
                self._stream_proc.wait(timeout=3)
            except Exception:
                try:
                    self._stream_proc.kill()
                except Exception:
                    pass
        self._stream_proc = None

    def _stream_stdout_reader_loop(self) -> None:
        """Baca stdout _stream_proc untuk baris 'BATTERY <val>' yang dicetak
        oleh callback_telemetry yang disuntikkan lewat monkey-patch di script
        inline (lihat _launch_and_loop) — berjalan di koneksi BLE yang SAMA
        dengan EEG, jadi tidak ada risiko penolakan koneksi BLE kedua."""
        proc = self._stream_proc
        if not proc or not proc.stdout:
            return
        _logged_once = False
        try:
            for line in proc.stdout:
                line = line.strip()
                if not line.startswith("BATTERY "):
                    continue
                try:
                    self.battery_percent = float(line.split(" ", 1)[1])
                except ValueError:
                    continue
                if not _logged_once:
                    _logged_once = True
                    print(f"🔋  Muse 2 battery: {self.battery_percent:.0f}%")
        except Exception:
            pass

    def _set_status(self, status: str, error: str = "") -> None:
        self.status    = status
        self.error_msg = error
        if self.on_status:
            try:
                self.on_status(status, error)
            except Exception:
                pass

    def _connect_thread(self, mac_address: str) -> None:
        self._mac_address = mac_address
        self._kill_proc()
        attempt = 0
        while not self._cancel.is_set():
            attempt += 1
            try:
                self._launch_and_loop(mac_address)
                # _launch_and_loop returned normally (disconnect() dipanggil user)
                break
            except Exception as e:
                self.running = False
                self._kill_proc()
                if self._cancel.is_set():
                    break
                # Backoff: 3s, 5s, 10s, lalu 15s untuk semua attempt berikutnya
                delay = [3, 5, 10][min(attempt - 1, 2)] if attempt <= 3 else 15
                msg = str(e)
                print(f"⚠️  Connection lost: {msg}")
                print(f"🔄  Auto-reconnect attempt {attempt} in {delay}s...")
                self._set_status("reconnecting", msg)
                for _ in range(delay * 4):  # check cancel setiap 250ms
                    if self._cancel.is_set():
                        break
                    time.sleep(0.25)
                if self._cancel.is_set():
                    break
        self.running = False
        if self._cancel.is_set():
            self._set_status("disconnected")
        else:
            self._set_status("error", "Reconnect stopped")

    def _launch_and_loop(self, mac_address: str) -> None:
        """Launch muselsl subprocess, wait for LSL streams, run _loop. Raises on failure."""
        if not mac_address:
            raise Exception("No device address — scan first and select a device")

        print(f"🔵  Starting muselsl for {mac_address}...")

        # Capture muselsl stderr to temp file so we can show why it died
        self._err_file = tempfile.NamedTemporaryFile(
            mode="w", suffix="_muselsl.log", delete=False
        )

        # Monkey-patch Muse.__init__ supaya callback_telemetry selalu
        # disisipkan sebelum muselsl.stream() membuat instance Muse-nya
        # secara internal — stream() sendiri tidak punya parameter untuk
        # meneruskan callback_telemetry (dicek langsung di source muselsl).
        # Ini memberi battery % lewat koneksi BLE yang SAMA dengan EEG,
        # bukan koneksi kedua terpisah (yang di banyak adapter ditolak).
        script = (
            "from muselsl.muse import Muse; "
            "from muselsl import stream; "
            "_orig_init = Muse.__init__; "
            "_cb = lambda ts, battery, fg, av, temp: print(f'BATTERY {battery:.1f}', flush=True); "
            "Muse.__init__ = lambda self, *a, **kw: _orig_init(self, *a, **{**kw, 'callback_telemetry': _cb}); "
            f"stream(address='{mac_address}', ppg_enabled=True, "
            "acc_enabled=True, gyro_enabled=True)"
        )
        self._stream_proc = subprocess.Popen(
            [sys.executable, "-c", script],
            stdout=subprocess.PIPE,
            stderr=self._err_file,
            text=True,
            bufsize=1,
        )
        self._stream_stdout_thread = threading.Thread(
            target=self._stream_stdout_reader_loop, daemon=True
        )
        self._stream_stdout_thread.start()

        # Poll until the EEG LSL stream appears (up to 25 s)
        print("⏳  Waiting for Muse 2 LSL streams...")
        eeg_streams = None
        for _ in range(25):
            if self._cancel.is_set():
                raise Exception("Cancelled by user")
            if self._stream_proc.poll() is not None:
                err = self._read_err_log()
                raise Exception(
                    f"muselsl exited unexpectedly (code={self._stream_proc.returncode}) — "
                    f"make sure Muse 2 is on and not connected to another app"
                    + (f"\n  muselsl: {err}" if err else "")
                )
            found = resolve_byprop("type", "EEG", timeout=1.0)
            if found:
                eeg_streams = found
                break
        if not eeg_streams:
            raise Exception("EEG LSL stream not found — Muse 2 did not connect within 25 s")

        ppg_streams  = resolve_byprop("type", "PPG",  timeout=3.0)
        acc_streams  = resolve_byprop("type", "ACC",  timeout=3.0)
        gyro_streams = resolve_byprop("type", "GYRO", timeout=3.0)

        eeg_inlet = StreamInlet(eeg_streams[0], max_buflen=30, max_chunklen=0)
        ppg_inlet = StreamInlet(ppg_streams[0], max_buflen=60, max_chunklen=0) if ppg_streams else None
        # ACC/GYRO @ ~52Hz — hanya butuh sample TERBARU tiap tick (lihat _loop),
        # bukan window besar seperti EEG, jadi buffer kecil cukup.
        acc_inlet  = StreamInlet(acc_streams[0],  max_buflen=5, max_chunklen=0) if acc_streams  else None
        gyro_inlet = StreamInlet(gyro_streams[0], max_buflen=5, max_chunklen=0) if gyro_streams else None

        if ppg_inlet:
            print("📡  PPG LSL stream found — HR enabled!")
        else:
            print("⚠️  PPG stream not found — HR disabled")

        if acc_inlet and gyro_inlet:
            print("🕹️   ACC + GYRO LSL streams found — cursor control available!")
        else:
            print("⚠️  ACC/GYRO stream not found — cursor control disabled this session")

        self.running = True
        self._connected_at = time.time()
        self._set_status("connected")
        print("✅  Muse 2 connected via muselsl!")

        # Battery telemetry sudah aktif sejak _stream_proc dimulai (lihat
        # monkey-patch callback_telemetry di atas) — tidak perlu langkah
        # tambahan di sini.

        # IMU (ACC/GYRO) dibaca di thread TERPISAH dari _loop utama supaya
        # cursor control terasa responsif — _loop EEG sengaja lambat (~150ms,
        # dibutuhkan utk resolusi Welch PSD), tapi accelerometer Muse 2
        # sebenarnya mengirim data ~52x/detik. Kalau cursor velocity hanya
        # di-update tiap 150ms, gerakan terasa lamat/tersendat walau
        # mouse_connector sendiri menggerakkan cursor di 60Hz — nilai
        # velocity-nya statis selama 150ms itu. Thread ini jalan ~50Hz,
        # mendekati native rate sensor, independen dari EEG.
        self._imu_thread_stop = threading.Event()
        if acc_inlet and gyro_inlet:
            self._imu_thread = threading.Thread(
                target=self._imu_loop, args=(acc_inlet, gyro_inlet), daemon=True
            )
            self._imu_thread.start()

            # Kalibrasi tilt_left/tilt_right — 1x otomatis tiap sesi connect,
            # independen dari Cursor Control Mode (lihat _run_tilt_calibration).
            # Thread terpisah dari _imu_thread supaya tidak memblokir baca
            # ACC/GYRO ~50Hz selama proses kalibrasi berjalan (~2.4s).
            # Generation token dinaikkan DULU — thread kalibrasi attempt
            # sebelumnya (jika masih hidup dari auto-reconnect cepat) akan
            # melihat gen-nya sudah usang dan berhenti sendiri tanpa menimpa
            # hasil kalibrasi yang baru ini (lihat _run_tilt_calibration).
            self._tilt_calib_gen += 1
            self._tilt_calib_ready = False
            self._tilt_calib_vec = None
            # Reset di sini (bukan cuma di disconnect()) — DITEMUKAN LEWAT
            # CODE REVIEW: auto-reconnect (_connect_thread) TIDAK memanggil
            # disconnect() di antara percobaan, cuma set running=False lalu
            # _kill_proc(). Kalau attempt sebelumnya sempat terima ACC data
            # asli sebelum putus (_acc_sample_count > 0), attempt baru ini
            # akan salah kira "ACC sudah siap" dan skip wait loop di
            # _run_tilt_calibration — padahal _imu_loop yang baru (dimulai
            # sesaat lagi) belum tentu sudah dapat data segar dari inlet
            # yang baru. Reset eksplisit di sini menutup celah itu.
            self._acc_sample_count = 0

            # Cache dari sesi sebelumnya — kalau ada, pakai langsung supaya
            # tilt_left/tilt_right BISA DIPAKAI SEGERA tanpa nunggu 3-sample
            # kalibrasi (posisi headset user biasanya konsisten antar sesi).
            # Kalibrasi sungguhan tetap jalan di background (thread yang
            # sama, target tidak berubah) untuk REFRESH nilai ini secara
            # diam-diam — begitu selesai, _run_tilt_calibration menimpa
            # _tilt_calib_vec/_tilt_neutral/_tilt_gyro_axis dengan hasil
            # segar dan menulis ulang cache-nya sendiri. User tidak pernah
            # diblokir menunggu; akurasi membaik sendiri di latar belakang.
            cached = self._load_tilt_calibration()
            _from_cache = cached is not None
            if cached is not None:
                self._tilt_calib_vec  = cached["tilt_calib_vec"]
                self._tilt_neutral    = cached["tilt_neutral"]
                self._tilt_gyro_axis  = cached["tilt_gyro_axis"]
                self._tilt_calib_ready = True
                self.tilt_calib_phase  = "ready"
                print("🎯  Kalibrasi tilt dimuat dari cache — tilt_left/tilt_right siap dipakai segera "
                      f"(vec={tuple(round(v,3) for v in cached['tilt_calib_vec'])}); "
                      "re-kalibrasi berjalan di background untuk refresh.")
            else:
                self.tilt_calib_phase = "neutral"

            self._tilt_calib_thread = threading.Thread(
                target=self._run_tilt_calibration, args=(self._tilt_calib_gen, _from_cache), daemon=True
            )
            self._tilt_calib_thread.start()
        else:
            self._imu_thread = None

        self._loop(eeg_inlet, ppg_inlet)

    def _read_err_log(self) -> str:
        """Read and clean up the muselsl stderr temp file."""
        if self._err_file is None:
            return ""
        try:
            self._err_file.flush()
            name = self._err_file.name
            self._err_file.close()
            self._err_file = None
            with open(name, "r") as f:
                content = f.read().strip()
            os.unlink(name)
            # Return last 300 chars to avoid wall of text
            return content[-300:] if content else ""
        except Exception:
            return ""

    def _imu_loop(self, acc_inlet, gyro_inlet) -> None:
        """Thread terpisah, ~50Hz — baca ACC/GYRO dan update cursor velocity
        jauh lebih sering daripada tick EEG (~6.7Hz), supaya kontrol cursor
        terasa real-time. Channel order muselsl: X,Y,Z. Unit: ACC=g, GYRO=dps."""
        IMU_DT = 1.0 / 50.0
        while self.running and not self._imu_thread_stop.is_set():
            t0 = time.time()
            try:
                acc_chunk, _ = acc_inlet.pull_chunk(timeout=0.0, max_samples=32)
            except Exception:
                acc_chunk = []
            if acc_chunk:
                self._latest_acc = acc_chunk[-1]
                self._acc_recent.append(acc_chunk[-1])
                if len(self._acc_recent) > 8:
                    self._acc_recent.pop(0)
                self._acc_sample_count += 1

            try:
                gyro_chunk, _ = gyro_inlet.pull_chunk(timeout=0.0, max_samples=32)
            except Exception:
                gyro_chunk = []
            if gyro_chunk:
                self._latest_gyro = gyro_chunk[-1]

            if self.cursor_control_enabled:
                self._update_cursor_control()
                # Mutual exclusion dengan tilt_left/right: paksa balik ke
                # idle supaya edge "risen" yang mungkin sedang menunggu
                # release tidak tiba-tiba fire begitu user mematikan cursor
                # mode di tengah gerakan (state basi dari sebelum mode ON).
                self._tilt_state     = "idle"
                self._tilt_rise_side = ""
            else:
                self._update_tilt_command()

            elapsed = time.time() - t0
            time.sleep(max(0.0, IMU_DT - elapsed))

    def _loop(self, eeg_inlet, ppg_inlet) -> None:
        EEG_MAX = SAMPLE_RATE * 10  # 10 s circular buffer, 4 ch
        PPG_MAX = PPG_SR * 30       # 30 s circular buffer

        eeg_buf   = np.zeros((4, EEG_MAX))
        eeg_ptr   = 0
        eeg_total = 0

        ppg_buf   = np.zeros(PPG_MAX)
        ppg_ptr   = 0
        ppg_total = 0

        # EMA smoothing — mencegah spike tiba-tiba dari artifact/normalisasi
        # alpha=0.20 at 4 Hz: time constant ~1.1 detik
        # Cukup responsif untuk genuine state change, tapi filter artifact pendek
        EMA = 0.20
        ema_a = ema_b = ema_t = 0.5       # normalized EMA
        ema_tbr = 0.5                              # Theta/Beta Ratio EMA (frontal, normalized)
        ema_tbr_raw = 1.0                          # Raw theta/beta ratio EMA — bypass normalization
        ema_a_raw = ema_b_raw = ema_t_raw = 0.0  # raw uV2 EMA
        ema_fa = ema_ft = 0.5             # frontal-only alpha/theta EMA (untuk flow_score)
        _poor_streak = 0   # tick berturut-turut tanpa channel valid

        # ── CSV logging ───────────────────────────────────────────────────
        _csv_path  = f"eeg_session_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        _csv_file  = open(_csv_path, 'w', newline='')
        _csv_w     = csv.writer(_csv_file)
        _csv_w.writerow(['time', 'elapsed_s', 'alpha', 'beta', 'theta', 'tbr',
                         'frontal_alpha', 'frontal_theta', 'state', 'hr',
                         'q_tp9', 'q_af7', 'q_af8', 'q_tp10',
                         'eog_extreme', 'eog_zcross', 'frontal_emg', 'blink_candidates',
                         'wink_af7', 'wink_af8', 'wink_ratio',
                         'jaw_p2p_tp9', 'jaw_p2p_tp10', 'jaw_streak',
                         'alpha_pow', 'alpha_baseline', 'alpha_ratio', 'relax_streak',
                         'cmd_fired'])
        _t0 = time.time()
        print(f"📊  Logging EEG ke {_csv_path}")

        # Diagnostik command playground — direset tiap tick, diisi detector,
        # ditulis ke CSV supaya bisa dianalisa offline dari file log session.
        _cmd_diag = {
            "eog_extreme": "", "eog_zcross": "", "frontal_emg": "", "blink_candidates": "",
            "wink_af7": "", "wink_af8": "", "wink_ratio": "",
            "jaw_p2p_tp9": "", "jaw_p2p_tp10": "", "jaw_streak": "",
            "alpha_pow": "", "alpha_baseline": "", "alpha_ratio": "", "relax_streak": "",
            "cmd_fired": "",
        }

        while self.running:
            time.sleep(0.15)  # ~6.7 Hz — lag max 150 ms sebelum EEG sampai ke engine
            self._loop_tick += 1
            for _k in _cmd_diag:
                _cmd_diag[_k] = ""

            # Subprocess health check
            if self._stream_proc and self._stream_proc.poll() is not None:
                err = self._read_err_log()
                msg = f"muselsl process exited (code={self._stream_proc.returncode})"
                if err:
                    msg += f"\n  muselsl: {err}"
                print(f"⚠️  {msg}")
                break

            # ── Pull EEG ─────────────────────────────────────────────────
            try:
                chunk, _ = eeg_inlet.pull_chunk(timeout=0.2, max_samples=512)
            except Exception:
                chunk = []
            for sample in chunk:
                for ch in range(4):
                    eeg_buf[ch, eeg_ptr % EEG_MAX] = sample[ch]
                eeg_ptr   += 1
                eeg_total += 1

            # ── Pull PPG ──────────────────────────────────────────────────
            if ppg_inlet:
                try:
                    ppg_chunk, _ = ppg_inlet.pull_chunk(timeout=0.0, max_samples=256)  # PPG non-blocking ok, buffer cukup besar
                except Exception:
                    ppg_chunk = []
                for sample in ppg_chunk:
                    # muselsl PPG: [ambient, IR, red] — use IR (index 1)
                    val = sample[1] if len(sample) > 1 else sample[0]
                    ppg_buf[ppg_ptr % PPG_MAX] = val
                    ppg_ptr   += 1
                    ppg_total += 1

            if eeg_total < SAMPLE_RATE * 2:
                continue  # tunggu minimal 2 s data untuk resolusi delta yang cukup

            try:
                # ── EEG window (last 2 s) ─────────────────────────────────────────────
                # 512 samples: Welch averaging 2× windows → resolusi delta 2× lebih baik
                n     = min(eeg_total, SAMPLE_RATE * 2)
                start = (eeg_ptr - n) % EEG_MAX
                if start + n <= EEG_MAX:
                    eeg_win = eeg_buf[:, start:start + n].copy()
                else:
                    e1 = eeg_buf[:, start:]
                    e2 = eeg_buf[:, :n - (EEG_MAX - start)]
                    eeg_win = np.concatenate([e1, e2], axis=1)

                # ── Channel quality (dihitung DULU sebelum band power) ────
                ch_quality = []
                for i, name in enumerate(["TP9", "AF7", "AF8", "TP10"]):
                    std = float(np.std(eeg_win[i]))
                    if std < 3.0 or std > 400.0:  q = 0.0    # flat atau extreme noise
                    elif std > 300.0:              q = 0.25   # sangat noisy tapi masih ada sinyal
                    elif std < 8.0:                q = std / 8.0 * 0.6
                    else:                          q = 1.0
                    q = round(q, 2)
                    self.channel_quality[name] = q
                    ch_quality.append(q)

                # Print channel quality setiap 10 tick (~2.5s) untuk diagnosis
                if not hasattr(self, '_qual_tick'): self._qual_tick = 0
                self._qual_tick += 1
                if self._qual_tick % 10 == 0:
                    print(f"  [QUAL] TP9={ch_quality[0]:.2f} AF7={ch_quality[1]:.2f} "
                          f"AF8={ch_quality[2]:.2f} TP10={ch_quality[3]:.2f}")

                # ── Band power — hanya dari channel yang cukup bagus ──────
                # Channel dengan quality <= 0.0 (flat/disconnected/sangat noisy)
                # dikecualikan karena noise broadband membuat semua band tampak tinggi
                _CH_NAMES = ["TP9", "AF7", "AF8", "TP10"]
                alpha_list, beta_list, theta_list = [], [], []
                frontal_alpha_list, frontal_beta_list, frontal_theta_list = [], [], []  # AF7=1, AF8=2 only

                # ── Pass 1: deteksi frontal EMG SEBELUM proses temporal ────────
                # Scan semua channel tanpa break — perlu nilai kedua channel untuk
                # eyebrow raise bilateral detection.
                _frontal_emg = False
                _ch_emg = {1: False, 2: False}
                _ch_p2p = {1: 0.0, 2: 0.0}
                _ch_valid = {1: False, 2: False}
                for ch in (1, 2):  # AF7=1, AF8=2
                    if ch_quality[ch] < 0.25:
                        continue
                    _ch_valid[ch] = True
                    _fd = eeg_win[ch].copy()
                    DataFilter.detrend(_fd, DetrendOperations.CONSTANT.value)
                    DataFilter.perform_bandpass(
                        _fd, SAMPLE_RATE, 0.5, 40.0, 4,
                        FilterTypes.BUTTERWORTH.value, 0
                    )
                    _p2p = float(np.ptp(_fd))
                    _ch_p2p[ch] = round(_p2p, 1)
                    _psd_pre = DataFilter.get_psd_welch(
                        _fd, SAMPLE_RATE, SAMPLE_RATE // 2, SAMPLE_RATE,
                        WindowOperations.BLACKMAN_HARRIS.value
                    )
                    _blo = DataFilter.get_band_power(_psd_pre, 13.0, 25.0)
                    _bhi = DataFilter.get_band_power(_psd_pre, 25.0, 40.0)
                    if _p2p > 150.0 or _bhi / (_blo + 1e-6) > 0.80:
                        _frontal_emg = True
                        _ch_emg[ch]  = True

                # ── Temporal EMG (TP9/TP10) — dihitung DI SINI (sebelum eyebrow) ──
                # Dipindah ke atas supaya eyebrow detector bisa cek apakah ada jaw
                # clench bersamaan. Jaw clench kuat menjalar ke AF7/AF8 dan bisa
                # disalahartikan sebagai eyebrow bilateral — maka eyebrow harus
                # menahan diri saat temporal EMG tinggi (jelas jaw, bukan eyebrow).
                # PENTING: ptp dihitung hanya dari potongan TERBARU (~300ms), bukan
                # seluruh window 2s. eeg_win punya panjang 2s (dibutuhkan untuk
                # band-power/Welch di tempat lain) — kalau ptp diukur di seluruh
                # window itu, satu spike clench akan membuat _full_max tetap tinggi
                # sampai ~2 DETIK setelah rahang dilepas (spike masih ada di window).
                # Akibatnya: release terdeteksi sangat lambat (menambah delay single
                # jaw jauh di atas DECIDE_DELAY), dan clench kedua pada double-jaw
                # sering jatuh SAAT _jaw_released masih macet False dari clench
                # pertama → rise kedua tidak terhitung → selalu jatuh ke single.
                _ENVELOPE_TAIL = SAMPLE_RATE // 3   # ~300ms terakhir
                _tp_full = {9: 0.0, 10: 0.0}   # 20-100Hz — total EMG untuk jaw
                for ch, key in ((0, 9), (3, 10)):
                    if ch_quality[ch] < 0.25:
                        continue
                    _td = eeg_win[ch].copy()
                    DataFilter.detrend(_td, DetrendOperations.CONSTANT.value)
                    DataFilter.perform_bandpass(_td, SAMPLE_RATE, 20.0, 100.0, 4,
                        FilterTypes.BUTTERWORTH.value, 0)
                    _tp_full[key] = round(float(np.ptp(_td[-_ENVELOPE_TAIL:])), 1)
                _full_max = max(_tp_full[9], _tp_full[10])

                # ── Eyebrow raise detection ────────────────────────────────────
                # Syarat:
                #   1. Bilateral symmetric: AF7 DAN AF8 keduanya >thr_eyebrow, rasio < 3.0
                #   2. Bilateral asymmetric: max >thr_eyebrow*1.67, min >thr_eyebrow*0.67
                #   3. Sustained ≥ 3 tick (~450ms), toleran 1 tick noise drop —
                #      streak hanya reset kalau GAGAL bilateral 2 tick beruntun.
                #      streak>=2 mentah terlalu sensitif (microexpression/kedutan
                #      sesaat ~300ms ikut lolos); streak>=3 mentah dulu gagal total
                #      karena 1 tick goyah langsung reset ke 0. Toleransi 1 tick
                #      menggabungkan keduanya: butuh durasi lebih panjang, tapi
                #      tidak macet gara2 satu tick noise di tengah gesture asli.
                _now = time.time()
                # Dihitung sekali di sini, sebelum semua detector — supaya kalau
                # eyebrow fire dan update _last_cmd_time di tick ini, wink/jaw
                # di bawah langsung melihat _cmd_idle = False di tick yang sama.
                _cmd_idle = (_now - self._last_cmd_time) > 1.5
                _p2p_af7 = _ch_p2p[1]
                _p2p_af8 = _ch_p2p[2]
                _both_strong = _p2p_af7 > self._thr_eyebrow and _p2p_af8 > self._thr_eyebrow
                _symmetric   = (max(_p2p_af7, _p2p_af8) / (min(_p2p_af7, _p2p_af8) + 1e-6)) < 3.0
                # Bilateral asimetri: dominant > 1.67×, secondary > 0.67× threshold eyebrow
                _eb_dominant  = max(_p2p_af7, _p2p_af8) > self._thr_eyebrow * 1.67
                _eb_secondary = min(_p2p_af7, _p2p_af8) > self._thr_eyebrow * 0.67
                _bilateral    = _both_strong and (_symmetric or (_eb_dominant and _eb_secondary))

                _both_ch_valid = _ch_valid[1] and _ch_valid[2]
                # Solo fallback dihapus — AF8 dropout saat wink kiri juga memenuhi
                # syarat solo (AF7 tinggi, AF8=0), menyebabkan cross-fire ke eyebrow.
                # Eyebrow hanya bisa fire via bilateral (kedua channel valid dan aktif).
                _bilateral_eff = _bilateral

                # Zona blokir: hanya aktif saat bilateral genuinely confirmed
                if _bilateral_eff:
                    self._eyebrow_active_until = _now + 1.5

                # Streak: naik hanya saat bilateral_eff, dan blokir total saat
                # after_jaw aktif / ada jaw clench bersamaan (artefak jaw kuat
                # menjalar ke AF7/AF8 dan bisa terlihat seperti eyebrow bilateral
                # — jika TP9/TP10 kuat, ini jaw, bukan eyebrow, reset paksa).
                #
                # Toleransi 1 tick noise: EMG asli sering punya 1 tick yang
                # goyah di tengah gesture sustained. Reset total tiap kali itu
                # terjadi membuat eyebrow nyaris tidak pernah capai streak>=3.
                # Sekarang streak hanya direset kalau GAGAL bilateral 2 tick
                # BERUNTUN (bukan 1 tick).
                _after_jaw_for_eb = (_now - self._jaw_cooldown) < 4.0
                _jaw_active_now   = _full_max > self._thr_jaw * 0.70
                if _after_jaw_for_eb or _jaw_active_now:
                    # Jaw artefak jelas — reset paksa, tidak ada toleransi di sini.
                    self._eyebrow_streak = 0
                    self._eyebrow_miss   = 0
                elif _bilateral_eff:
                    self._eyebrow_streak += 1
                    self._eyebrow_miss   = 0
                else:
                    self._eyebrow_miss += 1
                    if self._eyebrow_miss >= 2:
                        self._eyebrow_streak = 0
                        self._eyebrow_miss   = 0

                if max(_p2p_af7, _p2p_af8) > self._thr_eyebrow * 0.33:
                    _cd_left = max(0.0, 3.0 - (_now - self._eyebrow_cooldown))
                    print(
                        f"  [EYEBROW] AF7={_p2p_af7:.0f}µV AF8={_p2p_af8:.0f}µV "
                        f"both_strong={_both_strong} bilateral={_bilateral_eff} "
                        f"zone={_now < self._eyebrow_active_until} streak={self._eyebrow_streak} "
                        f"cooldown={_cd_left:.1f}s"
                    )

                _after_wink = (_now - self._wink_cooldown) < 4.0
                _after_jaw  = (_now - self._jaw_cooldown) < 4.0
                if (self._eyebrow_streak >= 3 and
                        _cmd_idle and
                        not _after_wink and
                        not _after_jaw and
                        self.on_eyebrow_raise and
                        _now - self._eyebrow_cooldown > 3.0):
                    self._eyebrow_cooldown = _now
                    self._last_cmd_time    = _now
                    self._eyebrow_streak   = 0
                    self._eyebrow_miss     = 0
                    _cmd_diag["cmd_fired"] = "eyebrow_raise"
                    print("⚡  Eyebrow raise FIRED")
                    try:
                        self.on_eyebrow_raise()
                    except Exception as _e:
                        print(f"⚠️  on_eyebrow_raise error: {_e}")

                # ══════════════════════════════════════════════════════════════
                # Mental command playground — 3 modalitas campuran
                # (lihat BRAINWAVE_MONITOR.md: kombinasi sinyal beda jenis ~95% andal)
                # ══════════════════════════════════════════════════════════════

                # ── Shared: EMG TP9/TP10 sudah dihitung di atas (sebelum eyebrow) ──
                _eyebrow_zone = _now < self._eyebrow_active_until
                _frontal_active = max(_ch_p2p[1], _ch_p2p[2]) > self._thr_eyebrow * 0.67

                # ── Adaptive threshold kalibrasi (15 detik pertama sesi) ──────
                # Kumpulkan sampel EMG selama 15 detik pertama tanpa syarat —
                # kalibrasi tetap jalan meski ada command fire di awal sesi.
                # Buffer merekam noise baseline termasuk saat gesture, tapi
                # median (bukan mean) membuat outlier spike tidak mempengaruhi hasil.
                if not self._calib_done:
                    self._calib_frontal.append(max(_ch_p2p[1], _ch_p2p[2]))
                    self._calib_temporal.append(_full_max)
                    if len(self._calib_frontal) >= self._CALIBRATION_TICKS:
                        _f_med = float(np.median(self._calib_frontal))
                        _t_med = float(np.median(self._calib_temporal))
                        # Multiplier: 3× median untuk eyebrow/wink, 4× untuk jaw
                        # (jaw clench jauh lebih kuat dari noise, margin lebih besar)
                        # Clamp ke range aman agar tidak terlalu sensitif atau kebal.
                        self._thr_eyebrow = float(np.clip(_f_med * 3.0,  80.0, 400.0))
                        self._thr_wink    = float(np.clip(_f_med * 5.0, 300.0, 1000.0))
                        self._thr_jaw     = float(np.clip(_t_med * 4.0, 300.0,  700.0))
                        self._calib_done  = True
                        print(
                            f"✅  EMG calibration done — "
                            f"frontal_baseline={_f_med:.0f}µV temporal_baseline={_t_med:.0f}µV  |  "
                            f"thr_eyebrow={self._thr_eyebrow:.0f}µV "
                            f"thr_wink={self._thr_wink:.0f}µV "
                            f"thr_jaw={self._thr_jaw:.0f}µV"
                        )

                # ── 1) Wink (EOG/EMG asimetri, AF7 vs AF8) ───────────────────
                # Kedip satu mata → defleksi UNILATERAL di AF7 atau AF8.
                # Dibedakan dari eyebrow raise (bilateral) dengan rasio asimetri.
                _after_eyebrow  = (_now - self._eyebrow_cooldown) < 5.0
                _during_eyebrow = self._eyebrow_streak >= 1 or _eyebrow_zone
                # Blokir semua command 5 detik setelah connect — elektrode belum settle
                _warmup         = (_now - self._connected_at) < 5.0
                # Jaw clench menarik kulit kepala → AF7 spike artefak mekanik
                # Blokir wink selama jaw aktif (TP >400µV) atau 2.5s setelah jaw fire
                _during_jaw     = _full_max > self._thr_jaw * 0.77
                _after_jaw      = (_now - self._jaw_cooldown) < 2.5

                _wink_af7    = _ch_p2p[1]
                _wink_af8    = _ch_p2p[2]
                _wink_side   = max(_wink_af7, _wink_af8)
                _wink_weak   = min(_wink_af7, _wink_af8)
                _wink_ratio  = _wink_side / (_wink_weak + 1e-6)
                _wink_strong = _wink_side > self._thr_wink
                _wink_asymm  = _wink_ratio > 2.0
                # Jika channel lemah >400µV → kedua frontal aktif = eyebrow, bukan wink
                # Channel lemah mendekati 0 = sisi itu benar2 diam (genuine unilateral,
                # bukan dropout) — batas bawah diturunkan dari 10 ke 1µV. Wink kiri
                # (AF7 dominan) sering gagal di sini: AF8 (weak side) kadang jatuh ke
                # 0-9µV (dianggap dropout invalid) atau naik tipis >300 (dianggap
                # dekat-eyebrow), sehingga jarang mendarat di rentang lama 10-300.
                _wink_unilateral = 1.0 <= _wink_weak < 400.0
                _wink_eye    = "left" if _wink_af7 >= _wink_af8 else "right"

                if _wink_strong:
                    print(f"  [WINK] AF7={_wink_af7:.0f}µV AF8={_wink_af8:.0f}µV "
                          f"ratio={_wink_ratio:.1f} asymm={_wink_asymm} unilat={_wink_unilateral} "
                          f"during_eyebrow={_during_eyebrow} after_eyebrow={_after_eyebrow} "
                          f"during_jaw={_during_jaw} after_jaw={_after_jaw}")

                _wink_now = (not _warmup
                             and _wink_strong and _wink_asymm and _wink_unilateral
                             and not _bilateral
                             and not _during_eyebrow and not _after_eyebrow
                             and not _during_jaw
                             and not _after_jaw
                             and _both_ch_valid)
                if _wink_now:
                    self._wink_streak += 1
                    # Jika sustained >2 tick = elektrode drift/artifact, bukan wink
                    if self._wink_streak > 2:
                        self._wink_streak = 0
                else:
                    self._wink_streak = 0

                _cmd_diag["wink_af7"]   = round(_wink_af7, 1)
                _cmd_diag["wink_af8"]   = round(_wink_af8, 1)
                _cmd_diag["wink_ratio"] = round(_wink_ratio, 2)

                # Cooldown wink: 1.5s, cukup untuk reset antar wink terpisah.
                if (self._wink_streak == 1 and
                        _now - self._wink_cooldown > 1.5):
                    self._wink_cooldown = _now
                    self._wink_streak   = 0
                    _cmd_diag["cmd_fired"] = f"wink_{_wink_eye}"
                    print(f"😉  Wink edge ({_wink_eye}) — full={_wink_af7:.0f}/{_wink_af8:.0f}µV "
                          f"ratio={_wink_ratio:.1f}")
                    _wink_cb = self.on_wink_left if _wink_eye == "left" else self.on_wink_right
                    if _wink_cb:
                        try:
                            _wink_cb()
                        except Exception as e:
                            print(f"⚠️  on_wink_{_wink_eye} error: {e}")

                # ── 2) Jaw clench ─────────────────────────────────────────────
                # Masseter EMG kuat di TP9/TP10 (>520µV).
                # Tidak pakai filter simetri — jaw clench kuat juga bilateral.
                # Discriminasi dari menoleh (neck/SCM) dilakukan via cooldown:
                # setelah fire, detector lock 4s — cukup untuk skip sustained neck.
                _tp_ratio = (max(_tp_full[9], _tp_full[10]) /
                             (min(_tp_full[9], _tp_full[10]) + 1e-6))

                # ── Edge-triggered clench detection dengan hysteresis ────────
                # Yang dikirim ke composer adalah RISING EDGE (saat rahang mulai
                # clench), bukan setiap tick selama clench ditahan. Durasi clench
                # tidak relevan — composer menghitung berapa kali clench terjadi.
                #
                # Hysteresis 2 threshold:
                #   - RISE   : _full_max > _thr_jaw          → clench mulai (edge)
                #   - RELEASE: _full_max < _thr_jaw * 0.70   → rahang lepas
                # Threshold release dinaikkan ke 0.70 supaya release lebih mudah
                # ter-register (mencegah stuck-state di dead-zone yang membuat
                # _jaw_released macet False selamanya).
                _thr_rel = self._thr_jaw * 0.70
                _jaw_rise_now = _full_max > self._thr_jaw
                # Guard eyebrow HANYA mempengaruhi rise, bukan release.
                # PENTING: jaw clench kuat juga mengaktifkan frontal AF7/AF8
                # (otot temporalis menjalar) sehingga terlihat seperti eyebrow.
                # Maka guard hanya berlaku saat jaw MARGINAL (≤ 1.6× threshold).
                # Clench kuat (TP9/TP10 jauh di atas threshold) jelas jaw, bukan
                # eyebrow — eyebrow raise tidak pernah menghasilkan EMG temporal
                # sebesar itu. Ini mencegah eyebrow "mencuri" jaw clench.
                _jaw_clearly_strong = _full_max > self._thr_jaw * 1.6
                _rise_blocked = (_jaw_rise_now and not _jaw_clearly_strong
                                 and (_eyebrow_zone or (_frontal_active and _bilateral)))

                if _jaw_rise_now and not _rise_blocked:
                    self._jaw_strong_streak += 1
                    # Rising edge: hanya saat sebelumnya benar-benar released
                    if self._jaw_released:
                        self._jaw_released = False
                        self._jaw_cooldown = _now   # dipakai eyebrow/wink utk blokir artefak jaw
                        print(f"🦷  Clench edge #{self.composer._jaw_count + 1} → composer — full={_full_max:.0f}µV")
                        self.composer.notify_jaw(_now)
                elif _full_max < _thr_rel:
                    # Turun di bawah release threshold → rahang lepas.
                    # Cek INDEPENDEN dari rise — selalu evaluasi release supaya
                    # state tidak pernah macet.
                    if not self._jaw_released:
                        print(f"🟢  Jaw RELEASED — full={_full_max:.0f}µV (thr_rel={_thr_rel:.0f}µV) — window clench kedua mulai")
                        self.composer.notify_jaw_release()
                    self._jaw_strong_streak = 0
                    self._jaw_released = True
                # Zona dead antara _thr_rel dan _thr_jaw: tahan state (hysteresis).

                _cmd_diag["jaw_p2p_tp9"]  = _tp_full[9]
                _cmd_diag["jaw_p2p_tp10"] = _tp_full[10]
                _cmd_diag["jaw_streak"]   = self._jaw_strong_streak

                if _full_max > _thr_rel * 0.6:
                    print(f"  [JAW] full={_full_max:.0f}µV thr={self._thr_jaw:.0f} thr_rel={_thr_rel:.0f} "
                          f"streak={self._jaw_strong_streak} "
                          f"released={self._jaw_released} "
                          f"jaw_count={self.composer._jaw_count} rise_blocked={_rise_blocked}")

                # ── Pass 2: hitung band power semua channel ────────────────────
                # Helper: spectral centroid (Hz dominan) dalam rentang band.
                # Rumus: Σ(f × PSD(f)) / Σ(PSD(f)) — lebih stabil dari peak frequency.
                def _centroid(p, f_lo, f_hi):
                    mask = (p[1] >= f_lo) & (p[1] <= f_hi)
                    if not mask.any(): return (f_lo + f_hi) / 2.0
                    a = p[0][mask]
                    s = float(a.sum())
                    if s < 1e-10: return (f_lo + f_hi) / 2.0
                    return float(np.sum(p[1][mask] * a) / s)

                alpha_hz_list, beta_hz_list, theta_hz_list = [], [], []
                # Threshold dinaikkan dari 0.25 → 0.65: kualitas marjinal (std rendah,
                # kontak elektroda kurang stabil) masih lolos di 0.25 dan mendistorsi
                # band power (mis. frontal_alpha turun ~40% saat q_af7/af8 dip ke 0.4-0.6
                # meski _frontal_emg tidak terdeteksi) — lihat sesi 20260719_140743.
                # Tidak mempengaruhi command detector (eyebrow/wink/jaw pakai gate
                # ch_quality terpisah di pass-1, baris ~1024 & ~1062).
                for ch in range(4):
                    if ch_quality[ch] < 0.65:
                        continue  # skip channel poor/marginal/disconnected
                    ch_data = eeg_win[ch].copy()
                    DataFilter.detrend(ch_data, DetrendOperations.CONSTANT.value)

                    # ── Cek kontaminasi PLN 50Hz ───────────────────────────────
                    # Hitung PSD dari sinyal asli untuk mengukur rasio power PLN
                    psd_raw = DataFilter.get_psd_welch(
                        ch_data.copy(), SAMPLE_RATE, SAMPLE_RATE // 2, SAMPLE_RATE,
                        WindowOperations.BLACKMAN_HARRIS.value
                    )
                    pln_power  = DataFilter.get_band_power(psd_raw, 48.0, 52.0)
                    eeg_power  = DataFilter.get_band_power(psd_raw,  1.0, 45.0)
                    pln_ratio  = pln_power / (eeg_power + 1e-10)
                    if ch in (1, 2) and pln_ratio > 0.30 and pln_power > 5.0:
                        # AF7/AF8: PLN dominan → skip dari komputasi band power,
                        # tapi JANGAN override channel_quality (elektroda mungkin masih nempel,
                        # hanya lingkungan noisy). Quality tetap dari std di atas.
                        continue
                    if eeg_power > 30000.0:
                        # Amplitudo terlalu tinggi (std >> 295 µV) — elektroda melayang/off-head
                        self.channel_quality[_CH_NAMES[ch]] = 0.0
                        ch_quality[ch] = 0.0
                        continue

                    # ── Bandpass 0.5–40 Hz — preprocessing standar EEG ──────────
                    # Menghapus sekaligus:
                    #   • DC drift & slow baseline wander  (< 0.5 Hz)
                    #   • EMG otot rahang/leher             (> 40 Hz)
                    #   • PLN 50 Hz                         (> 40 Hz, menggantikan notch)
                    DataFilter.perform_bandpass(
                        ch_data, SAMPLE_RATE, 0.5, 40.0, 4,
                        FilterTypes.BUTTERWORTH.value, 0
                    )

                    # ── Peak-to-peak artifact rejection ──────────────────────────
                    # Frontal sudah discan di pass 1; check ini hanya untuk temporal.
                    _p2p_limit = 300.0
                    if float(np.ptp(ch_data)) > _p2p_limit:
                        continue

                    psd = DataFilter.get_psd_welch(
                        ch_data, SAMPLE_RATE, SAMPLE_RATE // 2, SAMPLE_RATE,
                        WindowOperations.BLACKMAN_HARRIS.value
                    )
                    b_pow = DataFilter.get_band_power(psd, 13.0, 25.0)
                    t_pow = DataFilter.get_band_power(psd,  4.0,  8.0)

                    if ch in (1, 2):
                        a_pow = DataFilter.get_band_power(psd, 8.0, 13.0)
                        alpha_list.append(a_pow); theta_list.append(t_pow)
                        alpha_hz_list.append(_centroid(psd, 8.0, 13.0))
                        theta_hz_list.append(_centroid(psd, 4.0,  8.0))
                        if not _frontal_emg:
                            # Beta frontal valid kalau EMG tidak terdeteksi
                            frontal_alpha_list.append(a_pow)
                            frontal_beta_list.append(b_pow)
                            frontal_theta_list.append(t_pow)
                            beta_list.append(b_pow)
                            beta_hz_list.append(_centroid(psd, 13.0, 25.0))
                    else:
                        # TP9/TP10: skip beta hanya kalau frontal EMG jelas terdeteksi
                        a_pow = DataFilter.get_band_power(psd, 8.0, 13.0)
                        alpha_list.append(a_pow); theta_list.append(t_pow)
                        alpha_hz_list.append(_centroid(psd, 8.0, 13.0))
                        theta_hz_list.append(_centroid(psd, 4.0,  8.0))
                        if not _frontal_emg:
                            beta_list.append(b_pow)
                            beta_hz_list.append(_centroid(psd, 13.0, 25.0))

                if not alpha_list:
                    # Semua channel poor atau PLN-dominated
                    # Decay raw_bands menuju 0 sebagai indikator visual “tidak ada sinyal”
                    _poor_streak += 1
                    if _poor_streak >= 3:  # setelah 3 detik berturut-turut
                        decay = 0.80
                        ema_a_raw *= decay
                        ema_b_raw *= decay; ema_t_raw *= decay
                        self.raw_bands = {
                            "alpha": round(ema_a_raw, 2),
                            "beta":  round(ema_b_raw, 2),
                            "theta": round(ema_t_raw, 2),
                        }
                    continue  # jangan kirim nilai palsu ke engine
                _poor_streak = 0

                alpha = self._normalize("alpha", float(np.mean(alpha_list)))
                theta = self._normalize("theta", float(np.mean(theta_list)))

                # beta_list kosong → frontal EMG terdeteksi, semua temporal juga di-skip.
                # Jangan update ema_b — biarkan EMA decay sendiri ke baseline.
                if beta_list:
                    beta = self._normalize("beta", float(np.mean(beta_list)))
                    ema_b_updated = True
                else:
                    beta = ema_b   # pakai nilai EMA sebelumnya, tidak di-update
                    ema_b_updated = False

                # TBR (Theta/Beta Ratio) — dari frontal (AF7+AF8) jika tersedia
                # Frontal TBR adalah biomarker attention paling tervalidasi:
                #   TBR rendah  → beta > theta di frontal → focused (genuine)
                #   TBR tinggi  → theta > beta di frontal → drowsy / inattentive
                # Pakai raw ratio sebagai input normalize agar scale tetap bermakna
                if frontal_beta_list:
                    tbr_raw = float(np.mean(frontal_theta_list)) / (float(np.mean(frontal_beta_list)) + 1e-6)
                elif beta_list:
                    tbr_raw = float(np.mean(theta_list)) / (float(np.mean(beta_list)) + 1e-6)
                else:
                    tbr_raw = ema_tbr_raw  # jaga nilai sebelumnya
                tbr = self._normalize("tbr", tbr_raw)

                # EMA smoothing
                ema_a   = ema_a   * (1 - EMA) + alpha * EMA
                if ema_b_updated:
                    ema_b = ema_b * (1 - EMA) + beta  * EMA
                ema_t   = ema_t   * (1 - EMA) + theta * EMA
                ema_tbr     = ema_tbr     * (1 - EMA) + tbr     * EMA
                ema_tbr_raw = ema_tbr_raw * (1 - EMA) + tbr_raw * EMA
                self.tbr = round(ema_tbr, 3)

                # Frontal alpha/theta EMA — dari AF7+AF8 saja (frontal_*_list), untuk flow_score
                if frontal_alpha_list:
                    fa_raw = self._normalize("frontal_alpha", float(np.mean(frontal_alpha_list)))
                    ft_raw = self._normalize("frontal_theta", float(np.mean(frontal_theta_list)))
                    ema_fa = ema_fa * (1 - EMA) + fa_raw * EMA
                    ema_ft = ema_ft * (1 - EMA) + ft_raw * EMA
                self.frontal_alpha = round(ema_fa, 3)
                self.frontal_theta = round(ema_ft, 3)

                # ── Eyes-closed relax (alpha lonjak, sustained 2 s) ────────────
                # Menutup mata & relaks memicu Berger effect: power alpha
                # frontal naik tajam (sering 1.5-3x baseline) & bertahan lama —
                # beda dari blink/clench yang transient. Pakai RAW alpha power
                # (uV^2, dari frontal_alpha_list = AF7+AF8 murni) dibanding median 8 detik
                # terakhir (baseline "mata terbuka") — lebih sensitif & stabil
                # daripada nilai frontal_alpha yang sudah di-normalize+EMA berat.
                if frontal_alpha_list:
                    _fa_pow_now = float(np.mean(frontal_alpha_list))

                    # Hitung ratio dulu pakai baseline LAMA (sebelum diupdate),
                    # baru putuskan apakah sample ini layak masuk baseline.
                    if len(self._relax_alpha_hist) >= 12:
                        _fa_baseline = float(np.median(self._relax_alpha_hist))
                        _fa_ratio = _fa_pow_now / (_fa_baseline + 1e-9)
                    else:
                        _fa_baseline = _fa_pow_now
                        _fa_ratio = 1.0

                    # Hanya masukkan ke buffer baseline kalau TIDAK sedang
                    # lonjakan (ratio rendah) — kalau tidak, baseline ikut naik
                    # mengejar event yg sedang dideteksi & ratio kolaps balik
                    # ke ~1.0 dalam 1-2 tick (kebukti dari data: 7.44x -> 1.0x).
                    if _fa_ratio <= 1.4:
                        self._relax_alpha_hist.append(_fa_pow_now)
                        if len(self._relax_alpha_hist) > 32:  # 8 s @ 4Hz
                            self._relax_alpha_hist.pop(0)

                    # Hitung tick BERTURUT-TURUT di atas threshold — bukan
                    # vote tersebar. Data menunjukkan vote-window numpuk dari
                    # spike acak/terpisah (mis. ratio 1.0→12.29→3.9→1.0→1.0→
                    # 1.0→3.92→3.09 ikut numpuk jadi "sustained" palsu & fired).
                    # "Mata terpejam 2-3 detik" = elevasi yg BERTAHAN tanpa
                    # putus — reset total begitu satu tick turun di bawah
                    # threshold (lebih ketat & jujur thd definisi "sustained").
                    # Loop jalan @ 4Hz (~0.25s/tick) → ~2 detik ≈ 8 tick beruntun.
                    if _fa_ratio > 1.6:
                        self._relax_streak += 1
                    else:
                        self._relax_streak = 0

                    _relax_consec = self._relax_streak

                    _cmd_diag["alpha_pow"]      = round(_fa_pow_now, 1)
                    _cmd_diag["alpha_baseline"] = round(_fa_baseline, 1)
                    _cmd_diag["alpha_ratio"]    = round(_fa_ratio, 2)
                    _cmd_diag["relax_streak"]   = _relax_consec

                    if _relax_consec > 0 and _relax_consec % 2 == 0:
                        print(f"  [ALPHA] power={_fa_pow_now:.1f} baseline={_fa_baseline:.1f} "
                              f"ratio={_fa_ratio:.2f}x consecutive={_relax_consec}")

                    if (_relax_consec >= 8 and
                            self.on_eyes_closed_relax and
                            _now - self._relax_cooldown > 4.0):
                        self._relax_cooldown = _now
                        self._relax_streak = 0
                        self._relax_alpha_hist = []
                        _cmd_diag["cmd_fired"] = "eyes_closed_relax"
                        print(f"😌  Eyes-closed relax FIRED — alpha power={_fa_pow_now:.1f}µV² "
                              f"({_fa_ratio:.2f}x baseline={_fa_baseline:.1f})")
                        try:
                            self.on_eyes_closed_relax()
                        except Exception as _e:
                            print(f"⚠️  on_eyes_closed_relax error: {_e}")

                self.engine.set_eeg(ema_a, ema_b, ema_t, tbr=ema_tbr, tbr_raw=ema_tbr_raw,
                                    frontal_alpha=ema_fa, frontal_theta=ema_ft)

                # Raw uV2 EMA — untuk display UI
                ema_a_raw = ema_a_raw*(1-EMA) + float(np.mean(alpha_list))*EMA
                if beta_list:
                    ema_b_raw = ema_b_raw*(1-EMA) + float(np.mean(beta_list)) *EMA
                ema_t_raw = ema_t_raw*(1-EMA) + float(np.mean(theta_list))*EMA
                self.raw_bands = {
                    "alpha": round(ema_a_raw, 2),
                    "beta":  round(ema_b_raw, 2),
                    "theta": round(ema_t_raw, 2),
                }
                self.peak_hz = {
                    "alpha": round(float(np.mean(alpha_hz_list)), 1) if alpha_hz_list else self.peak_hz["alpha"],
                    "beta":  round(float(np.mean(beta_hz_list)),  1) if beta_hz_list  else self.peak_hz["beta"],
                    "theta": round(float(np.mean(theta_hz_list)), 1) if theta_hz_list else self.peak_hz["theta"],
                }

                # ── HR from PPG every 5 s ─────────────────────────────────
                if ppg_inlet and self._loop_tick % 33 == 0 and ppg_total >= PPG_SR * 4:  # every 5 s at ~6.7 Hz
                    n_p  = min(ppg_total, PPG_MAX)
                    sp   = (ppg_ptr - n_p) % PPG_MAX
                    if sp + n_p <= PPG_MAX:
                        ppg_win = ppg_buf[sp:sp + n_p].copy()
                    else:
                        ppg_win = np.concatenate([ppg_buf[sp:], ppg_buf[:n_p - (PPG_MAX - sp)]])
                    hr = self._compute_heart_rate(ppg_win, PPG_SR)
                    if hr is not None:
                        self.heart_rate = hr

                # ── Terminal log + CSV — 1 Hz (setiap 7 tick di ~6.7 Hz) ────────────────
                if self._loop_tick % 7 == 0:
                    if ema_b < 0.20:
                        state_hint = "calm"
                    else:
                        state_hint = (
                            "tense" if (0.50 * ema_b - 0.25 * ema_a - 0.25 * ema_tbr) > -0.05
                            else "calm"
                        )
                    hr_str = f"  ♥={self.heart_rate:.0f}" if self.heart_rate else ""
                    print(f"  EEG  α={ema_a:.2f}  β={ema_b:.2f}  θ={ema_t:.2f}  TBR={ema_tbr:.2f}(raw={ema_tbr_raw:.1f})  → {state_hint}{hr_str}")
                    _csv_w.writerow([
                        datetime.now().strftime('%H:%M:%S'),
                        round(time.time() - _t0, 1),
                        round(ema_a, 3), round(ema_b, 3), round(ema_t, 3),
                        round(ema_tbr, 3),
                        round(ema_fa, 3), round(ema_ft, 3), state_hint,
                        round(self.heart_rate) if self.heart_rate else '',
                        self.channel_quality["TP9"], self.channel_quality["AF7"],
                        self.channel_quality["AF8"], self.channel_quality["TP10"],
                        _cmd_diag["eog_extreme"], _cmd_diag["eog_zcross"], _cmd_diag["frontal_emg"], _cmd_diag["blink_candidates"],
                        _cmd_diag["wink_af7"], _cmd_diag["wink_af8"], _cmd_diag["wink_ratio"],
                        _cmd_diag["jaw_p2p_tp9"], _cmd_diag["jaw_p2p_tp10"], _cmd_diag["jaw_streak"],
                        _cmd_diag["alpha_pow"], _cmd_diag["alpha_baseline"], _cmd_diag["alpha_ratio"], _cmd_diag["relax_streak"],
                        _cmd_diag["cmd_fired"],
                    ])
                    _csv_file.flush()

            except Exception as e:
                if self.running:
                    print(f"⚠️  Loop error: {e}")

        # Loop ended — clean up
        self._imu_thread_stop.set()
        _csv_file.close()
        print(f"📊  Session log disimpan: {_csv_path}")
        err = self._read_err_log()
        if err:
            print(f"  muselsl last output: {err}")
        self._kill_proc()
        if self.status == "connected":
            self._set_status("disconnected")

    # ── Cursor Control (head-tilt joystick) ─────────────────────────────────

    # Konstanta cursor control — dituning empiris saat testing di headset asli.
    _TILT_EMA_ALPHA        = 0.55    # smoothing tilt angle — dinaikkan dari 0.35
                                      # supaya cursor lebih responsif (kurang lag),
                                      # masih cukup redam jitter BLE sesaat.
    _TILT_DEADZONE         = 0.025   # ~1.5° tilt — cukup kecil supaya gerakan
                                      # kepala ringan langsung terasa, tapi masih
                                      # menahan noise diam (accel Muse 2 quantized).
    _TILT_MAX              = 0.22    # tilt penuh dicapai pada sudut lebih kecil
                                      # dari sebelumnya (0.35) — mouse-like berarti
                                      # sedikit tilt = respons besar, bukan perlu
                                      # memiringkan kepala jauh untuk speed maksimum.
    _CURSOR_MAX_SPEED      = 2200.0  # px/detik pada tilt maksimum — dinaikkan
                                      # signifikan dari 900 supaya terasa senormal
                                      # menggerakkan mouse fisik, bukan merayap.
    _CURVE_EXPONENT        = 1.3     # kurva lebih landai dari 1.6 — speed naik
                                      # lebih cepat begitu keluar dead-zone,
                                      # tetap ada sedikit ruang presisi di awal.
    _GYRO_GATE_DPS         = 120.0   # di atas ini, accel dianggap terkontaminasi gerak cepat

    # Axis mapping Muse 2 → roll (kiri-kanan) / pitch (atas-bawah).
    #
    # Dua percobaan sebelumnya gagal karena sama-sama menebak axis:
    # 1. Proyeksi Gram-Schmidt generik (baseline-relative, tanpa asumsi axis
    #    fisik) — matematisnya solid tapi basis 'right'/'up' yang dihasilkan
    #    tidak terikat konsisten ke gerakan fisik kepala, tergantung arah
    #    baseline. Roll "bocor" ke pitch, magnitude jadi lemah/salah arah.
    # 2. Index axis fisik statis (X=roll, Y=pitch, tebakan dari asumsi umum
    #    "headband horizontal") — data nyata (log user) membuktikan asumsi
    #    ini salah: X punya std 3x lebih besar dari Y dan berkorelasi 0.625
    #    dengan Y, artinya axis fisik chip TIDAK sejajar murni dengan
    #    roll/pitch anatomis pada pemakaian headset ini — kemungkinan headset
    #    terpasang agak miring, atau chip IMU tidak presisi horizontal di
    #    dalam headband.
    #
    # Solusi final: KALIBRASI ARAH EKSPLISIT (_run_cursor_calibration di atas)
    # — user diminta tilt kanan lalu atas secara nyata, sistem mengukur
    # vektor deviasi accel yang SESUNGGUHNYA terjadi sebagai basis right/up.
    # Tidak ada tebakan axis/sign sama sekali — basis ini benar untuk
    # orientasi headset apapun, karena diukur langsung dari gerakan nyata.

    def _tilt_from_baseline(self, acc: tuple, baseline: tuple):
        """Proyeksi deviasi accel (relatif baseline netral) ke basis
        right/up hasil KALIBRASI NYATA (_calib_right_vec/_calib_up_vec),
        bukan index axis atau proyeksi geometris generik. Basis ini diukur
        langsung dari gerakan tilt user sendiri saat kalibrasi, sehingga
        otomatis benar untuk orientasi headset apapun."""
        dev = np.array(acc) - np.array(baseline)
        right_vec = np.array(self._calib_right_vec)
        up_vec    = np.array(self._calib_up_vec)
        tilt_right = float(np.dot(dev, right_vec))
        tilt_up    = float(np.dot(dev, up_vec))
        return tilt_up, tilt_right

    def _tilt_to_speed(self, tilt: float) -> float:
        """Dead-zone + kurva eksponensial gentle (bukan linear/kuadratik penuh)
        dari tilt angle ke velocity. Exponent ~1.3 memberi fine-control dekat
        netral (presisi klik target kecil) sambil tetap capai speed maksimum
        penuh saat tilt besar — sweet spot yang sama dipakai kurva analog
        stick game controller.

        CATATAN: sempat dicoba hysteresis anti-overshoot (dead-zone melebar
        2.5x saat magnitude menurun menuju netral) untuk meredam overshoot
        kecil manusiawi saat kepala kembali ke posisi netral. DIBUANG —
        terbukti dari testing nyata itu membuat cursor berhenti merespons
        terlalu dini (sebelum benar-benar sampai netral) dan bisa 'macet' di
        mode dead-zone lebar untuk waktu lama kalau ada sedikit goyangan
        balik. Trade-off itu lebih buruk daripada overshoot singkat (~1-2
        tick, ~20-40ms) yang coba diatasi. Kembali ke perhitungan sederhana:
        speed murni fungsi dari sudut tilt SAAT INI, tidak ada riwayat."""
        mag = abs(tilt)
        if mag < self._TILT_DEADZONE:
            return 0.0
        t = min((mag - self._TILT_DEADZONE) / (self._TILT_MAX - self._TILT_DEADZONE), 1.0)
        speed = (t ** self._CURVE_EXPONENT) * self._CURSOR_MAX_SPEED
        return speed if tilt > 0 else -speed

    # ── Head tilt command (tilt_left / tilt_right) ──────────────────────────
    # Gerakan MIRINGKAN kepala ke samping (telinga → bahu), quick tilt-and-
    # release — BUKAN gerakan tilt-and-hold dipakai Cursor Control Mode.
    # Sengaja pakai kalibrasi & state SENDIRI (tidak reuse _calib_right_vec
    # milik cursor control): kalibrasi cursor di-null-kan tiap toggle mode,
    # sedangkan command ini justru harus tetap hidup SAAT mode cursor OFF —
    # menyatukan keduanya berarti vector selalu None persis saat dibutuhkan.

    _TILT_MOVING_AWAY_WINDOW_N = 5   # jumlah sample terakhir (~100ms @50Hz)
                                      # dipakai cari nilai TERKECIL sebagai
                                      # basis pembanding "sedang menjauh" —
                                      # bukan cuma 1 tick sebelumnya (lihat
                                      # _TILT_MOVING_AWAY_EPS untuk kenapa).
    _TILT_MOVING_AWAY_EPS  = 0.006   # |tilt_val| tick ini harus lebih besar
                                      # dari nilai TERKECIL dalam window +
                                      # epsilon ini, supaya dianggap "sedang
                                      # menjauh dari netral" (syarat rise).
                                      # DITEMUKAN LEWAT LOG NYATA: gerakan
                                      # KEMBALI ke netral (mendekat, arah
                                      # manapun, kecepatan berapapun)
                                      # sebelumnya bisa tetap fire karena
                                      # sistem cuma lihat POSISI, tidak tahu
                                      # ARAH pergerakan. Percobaan pertama
                                      # (bandingkan cuma 1 tick sebelumnya)
                                      # DIBUANG — terlalu rapuh terhadap
                                      # osilasi natural pada tilt cepat, lihat
                                      # komentar panjang di _update_tilt_command.
    _TILT_CMD_THRESHOLD    = 0.19    # ambang deviasi (proyeksi ke calib vec)
                                      # untuk dianggap "tilt" — dinaikkan dari 0.06,
                                      # lalu 0.11, lalu 0.15 (testing nyata BERULANG:
                                      # tetap sedikit tilt saja sudah trigger tiap
                                      # kali dinaikkan sedikit, jadi kali ini dinaikkan
                                      # lebih signifikan). Dijaga tetap di bawah
                                      # _TILT_MAX (0.22, "tilt penuh" cursor control)
                                      # supaya command masih bisa dicapai dengan
                                      # gesture cepat wajar, bukan tilt ekstrem. Jauh
                                      # di atas _TILT_DEADZONE cursor (0.025) karena
                                      # ini gesture disengaja.
    _TILT_RELEASE_RATIO    = 0.6     # release valid kalau |tilt_val| turun ke
                                      # bawah RATIO × puncak yang dicapai
                                      # selama "risen" (bukan cuma ambang
                                      # absolut _TILT_CMD_THRESHOLD*0.5=0.055).
                                      # DITEMUKAN LEWAT LOG NYATA: gerakan
                                      # tilt KUAT (val naik jauh di atas
                                      # threshold, bahkan >1.0 utk gerakan
                                      # cepat) nyaris tidak pernah turun
                                      # kembali ke ambang absolut kecil dalam
                                      # window release — proporsinya terlalu
                                      # ketat utk gerakan besar, rise selalu
                                      # timeout ke idle tanpa fire meski
                                      # gesture-nya jelas terjadi.
    _TILT_REARM_THRESHOLD  = 0.04    # tilt_val harus terlihat di bawah ini
                                      # MINIMAL SEKALI (arah manapun) sebelum
                                      # rise baru diizinkan pasca-fire — lihat
                                      # _tilt_rearmed. Menutup celah dari log
                                      # nyata: kepala butuh waktu VARIABEL utk
                                      # settle balik ke netral pasca-fire,
                                      # timer refractory tetap (1.5s) tidak
                                      # selalu cukup; ini syarat berbasis SINYAL
                                      # (bukan cuma waktu) yang menutup celah
                                      # itu tanpa menebak durasi settling.
    _TILT_REARM_TIMEOUT_S  = 6.0     # fallback keras — kalau re-arm alami
                                      # (tilt_val < _TILT_REARM_THRESHOLD)
                                      # tidak kunjung terjadi dalam durasi ini
                                      # sejak fire terakhir, paksa re-arm saja.
                                      # Mencegah deadlock permanen kalau
                                      # baseline bergeser sangat jauh dan
                                      # recenter (EMA lambat, alpha=0.05)
                                      # belum sempat mengejar — trade-off satu
                                      # kemungkinan false-positive lebih baik
                                      # daripada command mati total.
    _TILT_CMD_RELEASE_MIN_S = 0.12   # rise harus bertahan minimal ini sebelum
                                      # release dihitung valid — buang micro-blip.
    _TILT_CMD_RELEASE_MAX_S = 0.8    # rise harus release SEBELUM ini — kalau
                                      # ditahan lebih lama (mis. memang lagi
                                      # menyandarkan kepala), dianggap bukan
                                      # command dan dibuang saat timeout.
    _TILT_CMD_COOLDOWN_S    = 1.5    # sama dengan wink — cukup untuk memisah
                                      # tilt berikutnya, selaras dgn global mutex.
    _TILT_CMD_REFRACTORY_S  = 1.5    # setelah FIRE (bukan setelah dibuang),
                                      # blokir total masuk state "risen" lagi
                                      # sampai durasi ini lewat — mencegah
                                      # rebound kepala kembali ke netral
                                      # ter-baca sebagai rise kedua yang valid
                                      # (trigger dobel dari 1 gerakan fisik).
    _TILT_GYRO_MIN_DPS      = 5.0    # gyro axis dominan harus melebihi ini
                                      # supaya dihitung sebagai rotasi nyata,
                                      # bukan noise diam (dipakai axis-dominance
                                      # guard, lihat _update_tilt_command).
                                      # Diturunkan dari 15.0 — data nyata
                                      # menunjukkan tilt genuine yang dilakukan
                                      # PELAN (ditahan, bukan sentakan cepat)
                                      # bisa serendah ~3-5 dps; 15.0 menolak
                                      # gesture asli yang tidak tergesa-gesa.
    _TILT_RECENTER_GYRO_DPS = _TILT_GYRO_MIN_DPS   # SAMA DENGAN _TILT_GYRO_
                                      # MIN_DPS SENGAJA (bukan angka independen
                                      # lebih rendah seperti percobaan awal) —
                                      # percobaan awal pakai 3.0 (lebih rendah
                                      # dari 5.0) dengan alasan "supaya tidak
                                      # pernah re-center saat user menahan tilt
                                      # pelan", tapi review menemukan itu
                                      # justru membuka CELAH 3-5 dps: gerakan
                                      # fidget/drift tak sadar di rentang itu
                                      # bisa lolos _is_roll_dominant (>3 dps
                                      # lama dianggap "rotasi nyata") TAPI
                                      # tidak pernah ter-koreksi oleh re-center
                                      # (>3 dps dianggap "tidak diam"), potensi
                                      # menambah false-positive baru. Menyamakan
                                      # kedua angka menutup celah itu — catatan:
                                      # gyro_mag (3-axis gabungan, dipakai di
                                      # sini) selalu >= roll_mag (1 axis,
                                      # dipakai _is_roll_dominant), jadi
                                      # menyamakan angka tetap konsisten secara
                                      # matematis (bukan sekadar kebetulan sama).
    _TILT_RECENTER_HOLD_S   = 1.0    # durasi diam (di bawah _TILT_RECENTER_
                                      # GYRO_DPS) sebelum re-center dieksekusi —
                                      # cegah re-center saat user cuma sesaat
                                      # transit lewat posisi netral di tengah
                                      # gerakan lain.
    _TILT_RECENTER_EMA_ALPHA = 0.05  # re-center pakai EMA LAMBAT (bukan
                                      # snapshot instan) — perubahan baseline
                                      # per re-center kecil, drift dikoreksi
                                      # bertahap. Mencegah lompatan baseline
                                      # tiba-tiba yang bisa terasa aneh kalau
                                      # user kebetulan langsung tilt lagi
                                      # persis setelah re-center terjadi.
    _TILT_GYRO_DOMINANCE    = 1.15   # axis roll kalibrasi harus >= sekian× lebih
                                      # besar dari axis gyro TERBESAR LAINNYA
                                      # supaya rotasi dianggap "murni roll" —
                                      # menoleh (yaw) / mengangguk (pitch) akan
                                      # didominasi axis lain dan ditolak di sini.
                                      # Diturunkan dari 1.5 — 1 titik data gyro
                                      # nyata (testing headset asli) menunjukkan
                                      # rasio dominasi genuine serendah ~1.4×
                                      # (kepala manusia nyaris tidak pernah
                                      # berputar murni 1 axis). 1.15 SENGAJA
                                      # diberi margin ekstra di bawah 1.4× itu
                                      # (bukan 1.4× persis) karena baru 1 sampel
                                      # — belum cukup untuk yakin itu batas
                                      # bawah sebenarnya; nilai lain (lebih
                                      # pelan/cepat) mungkin turun lebih jauh.
                                      # Trade-off: guard jadi lebih longgar
                                      # (mendekati 1.0 = tanpa diskriminasi
                                      # sama sekali), perlu di-tuning naik lagi
                                      # kalau menoleh/mengangguk ternyata mulai
                                      # false-positive setelah perubahan ini.
    _TILT_GYRO_PEAK_WINDOW_N = 6      # jumlah sample DI SEKITAR peak_idx (accel
                                      # deviation maksimum) dipakai hitung RMS
                                      # axis dominan saat kalibrasi — di
                                      # _STABILITY_POLL_S=0.1s/sample, setara
                                      # ~0.6s window terpusat di momen tilt
                                      # tercapai. Dipersempit dari "seluruh 2
                                      # detik rekaman" karena testing nyata
                                      # menunjukkan RMS window penuh bisa salah
                                      # pilih axis akibat 1 ledakan gyro sesaat
                                      # di luar gerakan roll yang sebenarnya.
    _CALIB_SAMPLE_COUNT      = 3      # jumlah percobaan tilt kanan TERPISAH
                                      # yang dikumpulkan & dirata-ratakan saat
                                      # kalibrasi — DITEMUKAN LEWAT PENGGUNAAN
                                      # NYATA BERULANG: kalibrasi dari 1
                                      # gerakan referensi terlalu sensitif
                                      # terhadap variasi kecil (kecepatan,
                                      # sudut, overshoot), menyebabkan axis &
                                      # arah tidak stabil antar sesi meski
                                      # headset & gerakan user sama secara
                                      # subjektif. Rata-rata dari beberapa
                                      # sample independen jauh lebih tahan
                                      # terhadap 1 sample yang kebetulan tidak
                                      # representatif.
    _CALIB_CONSISTENCY_MIN_DOT = 0.85 # ambang cosine similarity minimum
                                      # antara arah sample baru vs rata-rata
                                      # sample yang sudah terkumpul, supaya
                                      # sample baru diterima — DITEMUKAN LEWAT
                                      # LOG NYATA: sample dengan norm pas-
                                      # pasan di atas _MIN_DEV_NORM bisa lolos
                                      # tapi arahnya beda jauh dari sample
                                      # lain (mis. axis 0 vs 2), merusak rata-
                                      # rata. 0.85 ≈ sudut maksimum ~32° dari
                                      # rata-rata arah yang sudah terkumpul —
                                      # cukup ketat untuk menolak gerakan yang
                                      # tidak bersih/konsisten, cukup longgar
                                      # untuk variasi natural antar percobaan
                                      # manusia yang sama-sama tilt kanan.

    def _load_tilt_calibration(self) -> Optional[dict]:
        """Baca cache kalibrasi tilt dari sesi sebelumnya, kalau ada.
        Return None kalau file tidak ada/rusak — pemanggil harus fallback
        ke kalibrasi normal, bukan crash."""
        try:
            with open(self._TILT_CALIB_CACHE_PATH, "r") as f:
                data = json.load(f)
            vec = tuple(float(v) for v in data["tilt_calib_vec"])
            neutral = tuple(float(v) for v in data["tilt_neutral"])
            axis = data.get("tilt_gyro_axis")
            if len(vec) != 3 or len(neutral) != 3:
                return None
            return {
                "tilt_calib_vec": vec,
                "tilt_neutral": neutral,
                "tilt_gyro_axis": int(axis) if axis is not None else None,
            }
        except (FileNotFoundError, json.JSONDecodeError, KeyError, TypeError, ValueError):
            return None

    def _save_tilt_calibration(self) -> None:
        """Simpan hasil kalibrasi tilt sukses ke file supaya sesi berikutnya
        bisa langsung pakai (skip 3-sample) — lihat _load_tilt_calibration
        dan pemanggilnya di _launch_and_loop."""
        try:
            with open(self._TILT_CALIB_CACHE_PATH, "w") as f:
                json.dump({
                    "tilt_calib_vec": list(self._tilt_calib_vec),
                    "tilt_neutral": list(self._tilt_neutral),
                    "tilt_gyro_axis": self._tilt_gyro_axis,
                }, f)
        except OSError as e:
            print(f"⚠️  Gagal simpan cache kalibrasi tilt: {e}")

    def _run_tilt_calibration(self, gen: int, from_cache: bool = False) -> None:
        """Kalibrasi 1-sumbu MANDIRI untuk tilt_left/tilt_right — independen
        total dari _run_cursor_calibration (3 tahap, punya cursor control).
        Hanya butuh 1 vektor referensi (tilt kanan); tilt kiri = arah
        berlawanan pada sumbu yang sama, tidak perlu direkam terpisah.
        Dipicu SEKALI otomatis setelah connect (lihat _launch_and_loop),
        berjalan independen dari cursor_control_enabled — TIDAK bail-out
        kalau cursor mode mati (beda dari _wait_for_stable_window yang
        dipakai kalibrasi cursor, yang justru mensyaratkan mode itu ON).

        `tilt_calib_phase` di-broadcast lewat state_update (eeg_server.py)
        supaya UI bisa menampilkan instruksi "tilt kanan sekarang" — tanpa
        ini, user tidak tahu kapan harus bergerak, kedua window capture
        akan sama-sama netral, dan kalibrasi SELALU jatuh ke fallback
        (1,0,0) alih-alih mengukur arah asli. Sama filosofinya dengan
        cursor_calib_phase pada _run_cursor_calibration.

        `gen` (generation token) mencegah race saat reconnect cepat:
        _launch_and_loop menaikkan self._tilt_calib_gen tiap kali thread
        kalibrasi baru dibuat. Thread lama yang masih sleep/wait dari attempt
        sebelumnya (self.running sempat False lalu True lagi saat auto-
        reconnect) akan berhenti menulis begitu gen-nya sudah usang, alih-alih
        menimpa hasil kalibrasi thread yang baru dengan data basi.

        Fase "right" TIDAK menunggu accel stabil lalu snapshot sekali — itu
        menyebabkan bug arah terbalik di testing nyata: window stabil bisa
        tertangkap PERSIS saat user overshoot balik ke arah berlawanan dan
        sempat diam sesaat di sana, sehingga _tilt_calib_vec terekam
        berlawanan arah, dan SEMUA deteksi sesudahnya ikut terbalik
        konsisten sepanjang sesi. Sekarang fase ini merekam accel+gyro
        SEPANJANG _TILT_CALIB_RECORD_S detik (lihat _record_tilt_motion),
        lalu ambil titik DEVIASI MAKSIMUM dari seluruh riwayat itu sebagai
        arah kalibrasi — bukan snapshot di satu titik waktu tertentu. RMS
        gyro dari SELURUH rekaman (bukan cuma di titik deviasi maksimum)
        dipakai menentukan axis roll dominan untuk axis-dominance guard di
        _update_tilt_command, supaya menoleh/mengangguk tidak ter-baca
        sebagai tilt (_TILT_GYRO_DOMINANCE)."""
        _READ_DELAY_S = 1.2
        _MIN_DEV_NORM = 0.25   # dev_norm harus setinggi ini (dinaikkan dari
                                # 0.08) — DITEMUKAN LEWAT LOG NYATA: 0.08
                                # terlalu rendah, sample dengan norm=0.085
                                # (nyaris tidak bergerak) tetap lolos dan
                                # merusak rata-rata karena arahnya beda dari
                                # sample lain (axis 0 vs 2). Ambang lebih
                                # tinggi memaksa gerakan tilt yang JELAS/TEGAS
                                # sebelum dihitung sebagai sample valid.
        # TIDAK ADA BATAS PERCOBAAN — sesuai arahan user: lebih baik
        # kalibrasi makan waktu lama tapi akurat, daripada cepat tapi sample
        # jelek ikut lolos dan merusak hasil rata-rata. Loop terus mengulang
        # sampai _CALIB_SAMPLE_COUNT sample VALID & KONSISTEN benar-benar
        # terkumpul, banner + progress counter di browser memberi tahu user
        # ini masih berjalan (bukan macet), berapa lama pun itu perlu.
        _ACC_WAIT_TIMEOUT_S = 5.0   # tunggu sample ACC PERTAMA sebelum mulai
                                     # apapun — DITEMUKAN LEWAT LOG NYATA:
                                     # norm=0.000 PERSIS di semua 3 percobaan
                                     # adalah tanda _latest_acc masih macet di
                                     # default init (0,0,1) karena BLE
                                     # notification utk channel ACC/GYRO
                                     # kadang telat mulai mengalir dibanding
                                     # EEG (bisa >2 detik setelah connect).
                                     # _wait_for_stable_window_tilt tidak bisa
                                     # membedakan "data beku di default" dari
                                     # "data nyata yang kebetulan sangat
                                     # stabil" (keduanya sama-sama variance
                                     # nol) — makanya fase neutral lolos cepat
                                     # padahal belum ada sensor data sungguhan.

        # Tunggu sample ACC PERTAMA (bukan cuma stream resolved) sebelum
        # mulai fase "neutral" sama sekali — mencegah _tilt_neutral terekam
        # dari nilai default beku, yang membuat SEMUA deviasi berikutnya
        # (termasuk tilt kanan yang nyata) selalu terhitung persis nol.
        _acc_wait_start = time.time()
        while self._acc_sample_count == 0:
            if not self.running or gen != self._tilt_calib_gen:
                return
            if time.time() - _acc_wait_start > _ACC_WAIT_TIMEOUT_S:
                print("⚠️  Kalibrasi tilt: ACC sensor tidak pernah mengirim data "
                      f"dalam {_ACC_WAIT_TIMEOUT_S:.0f}s — lanjut apa adanya, "
                      "kemungkinan arah tidak akurat")
                break
            time.sleep(self._STABILITY_POLL_S)

        self.tilt_calib_phase = "neutral"
        time.sleep(_READ_DELAY_S)
        if not self.running or gen != self._tilt_calib_gen:
            return
        window = self._wait_for_stable_window_tilt(gen)
        if window is None or not self.running or gen != self._tilt_calib_gen:
            return
        # Neutral SENDIRI (_tilt_neutral) — sengaja TIDAK reuse _imu_baseline
        # milik cursor control: field itu hanya terisi kalau Cursor Control
        # Mode pernah diaktifkan sesi ini, dan bisa di-reset ke default
        # hardcoded (0,0,1) sewaktu-waktu oleh set_cursor_control()/disconnect().
        # Tilt command harus tetap kalibrasi valid walau cursor mode TIDAK
        # PERNAH dinyalakan sepanjang sesi.
        self._tilt_neutral = tuple(np.mean(np.array(window), axis=0))

        # Fase "right_refresh" (bukan "right") kalau tilt_calib_vec sudah
        # terisi dari cache — tilt_left/tilt_right SUDAH BISA DIPAKAI, jadi
        # banner "miringkan kepala sekarang" di UI (index.html, cek phase
        # persis == "right") tidak boleh muncul lagi seolah user wajib
        # menunggu. Refresh tetap jalan silent di background.
        self.tilt_calib_phase = "right_refresh" if from_cache else "right"
        # KALIBRASI MULTI-SAMPLE — DITEMUKAN LEWAT PENGGUNAAN NYATA BERULANG:
        # kalibrasi dari SATU gerakan referensi terlalu sensitif terhadap
        # variasi kecil (kecepatan, sudut, timing overshoot) — hasilnya
        # (_tilt_calib_vec, axis roll) tidak stabil antar sesi meski headset
        # & gerakan user secara subjektif sama, menyebabkan siklus false-
        # positive/negative yang berubah bentuk tiap sesi. Fix: kumpulkan
        # _CALIB_SAMPLE_COUNT percobaan tilt kanan TERPISAH (user mengulang
        # gerakan dengan jeda baca di antaranya), lalu RATA-RATAKAN vector
        # arah (masing-masing dinormalisasi dulu sebelum dirata-rata, supaya
        # sample yang kebetulan lebih kuat tidak mendominasi) dan pilih axis
        # dominan lewat VOTING MAYORITAS dari axis yang terpilih tiap sample
        # — jauh lebih tahan terhadap 1 sample yang kebetulan tidak
        # representatif dibanding bergantung pada 1 percobaan tunggal.
        collected_dirs: list = []   # unit vectors dari tiap sample sukses & konsisten
        collected_axes: list = []   # dominant_axis dari tiap sample sukses
        attempt = 0
        # Refresh dari cache TIDAK BOLEH loop tanpa batas seperti kalibrasi
        # blocking asli — di kalibrasi asli, user sedang aktif menonton
        # banner dan sengaja mengulang gerakan sampai berhasil, jadi retry
        # tanpa batas itu benar. Tapi refresh background ini jalan SAAT USER
        # SUDAH PAKAI APLIKASI SEPERTI BIASA (tidak sengaja tilt berulang-
        # ulang) — tanpa batas, ini akan retry SELAMANYA tiap sesi, terus
        # menerus print warning "sample tidak konsisten" ke console tanpa
        # progress nyata. Cap longgar (5x _CALIB_SAMPLE_COUNT percobaan)
        # lalu diam-diam menyerah dan tetap pakai cache lama (lihat
        # collected_dirs check di bawah, from_cache branch).
        _max_attempts = self._CALIB_SAMPLE_COUNT * 5 if from_cache else None
        while len(collected_dirs) < self._CALIB_SAMPLE_COUNT:
            if _max_attempts is not None and attempt >= _max_attempts:
                print(f"ℹ️  Refresh kalibrasi tilt (background) berhenti setelah {attempt} percobaan "
                      f"tanpa cukup sample konsisten — tetap pakai cache lama, tidak mengganggu penggunaan.")
                break
            attempt += 1
            # Progress ke browser (bukan cuma console print) — durasi
            # kalibrasi TIDAK DIBATASI (lihat komentar _MIN_DEV_NORM di
            # atas), bisa makan waktu lama kalau user berulang kali gagal
            # tilt cukup jelas/konsisten — progress counter mencegah user
            # mengira sistem macet.
            self.tilt_calib_progress = f"{len(collected_dirs)}/{self._CALIB_SAMPLE_COUNT}"
            if attempt > 1:
                # Jeda antar percobaan (selain _READ_DELAY_S internal di
                # _record_tilt_motion) — beri waktu user kembali netral dan
                # bersiap untuk percobaan berikutnya, banner tetap "tilt kanan".
                time.sleep(_READ_DELAY_S)
                if not self.running or gen != self._tilt_calib_gen:
                    return
            # PENTING — pendekatan lama (tunggu accel STABIL lalu snapshot
            # sesaat) punya bug fundamental yang ditemukan lewat code review:
            # window "stabil" bisa tertangkap PERSIS saat user overshoot balik
            # ke arah berlawanan dan sempat diam sesaat di sana — dev_norm
            # tetap besar tapi TANDA-nya terbalik. Perbaikan: rekam accel
            # SEPANJANG seluruh fase "right" (bukan snapshot akhir), ambil
            # deviasi MAKSIMUM dari seluruh riwayat sebagai arah kalibrasi
            # sample ini — titik terjauh dari netral selama SATU gerakan
            # tilt-kanan yang disengaja adalah puncak gerakan itu sendiri.
            samples_acc, samples_gyro = self._record_tilt_motion(gen)
            if samples_acc is None or not self.running or gen != self._tilt_calib_gen:
                return
            arr = np.array(samples_acc) - np.array(self._tilt_neutral)
            norms = np.linalg.norm(arr, axis=1)
            peak_idx = int(np.argmax(norms))
            cand_dev = arr[peak_idx]
            cand_norm = float(norms[peak_idx])

            if cand_norm < _MIN_DEV_NORM:
                # Deviasi maksimum sample ini masih terlalu kecil — user
                # kemungkinan belum sempat tilt sama sekali dalam attempt
                # ini. Retry, tidak dihitung sebagai sample valid.
                print(f"⚠️  Kalibrasi tilt: deviasi maksimum terlalu kecil (norm={cand_norm:.3f}) "
                      f"— percobaan {attempt}, tidak dihitung, retry...")
                continue

            cand_dir = cand_dev / cand_norm

            # Syarat KONSISTENSI ARAH — DITEMUKAN LEWAT LOG NYATA: sample
            # yang lolos _MIN_DEV_NORM tapi arahnya jauh berbeda dari
            # sample lain (mis. axis 0 vs axis 2 pada sample lain) tetap
            # ikut dirata-rata dan merusak hasil akhir. Sebelum diterima,
            # cocokkan cand_dir dengan RATA-RATA arah yang sudah terkumpul
            # (dot product unit vector = cosine sudut antar-arah) — kalau
            # sudutnya terlalu jauh (di bawah _CALIB_CONSISTENCY_MIN_DOT),
            # sample ini dianggap gerakan yang berbeda/tidak bersih, DIBUANG
            # tanpa menambah counter, user diminta ulangi.
            if collected_dirs:
                _ref_dir = np.mean(np.array(collected_dirs), axis=0)
                _ref_norm = np.linalg.norm(_ref_dir)
                if _ref_norm > 1e-6:
                    _cos_sim = float(np.dot(cand_dir, _ref_dir / _ref_norm))
                    if _cos_sim < self._CALIB_CONSISTENCY_MIN_DOT:
                        print(f"⚠️  Kalibrasi tilt: sample TIDAK KONSISTEN dengan "
                              f"sample sebelumnya (cos_sim={_cos_sim:.2f}, "
                              f"min={self._CALIB_CONSISTENCY_MIN_DOT}) — "
                              f"percobaan {attempt}, dibuang, ulangi gerakan yang sama.")
                        continue

            collected_dirs.append(cand_dir)

            # AXIS DOMINAN diambil dari titik GYRO MAGNITUDE MAKSIMUM sendiri
            # (bukan RMS di sekitar peak_idx accel) — DITEMUKAN LEWAT LOG
            # NYATA: peak_idx (posisi PALING MIRING) terjadi di UJUNG
            # gerakan, tepat saat kepala BERHENTI berputar sejenak di titik
            # terjauh — kecepatan rotasi (gyro) di titik itu MENDEKATI NOL,
            # bukan puncaknya. Kecepatan rotasi TERTINGGI terjadi di TENGAH
            # gerakan (saat kepala paling cepat berputar menuju posisi
            # tilt). Cari sample dengan gyro magnitude TERBESAR di seluruh
            # rekaman untuk menentukan axis dominan sample ini.
            if samples_gyro:
                g_arr = np.array(samples_gyro)
                gyro_mags = np.linalg.norm(g_arr, axis=1)
                gyro_peak_idx = int(np.argmax(gyro_mags))
                _half = self._TILT_GYRO_PEAK_WINDOW_N // 2
                _lo = max(0, gyro_peak_idx - _half)
                _hi = min(len(g_arr), gyro_peak_idx + _half + 1)
                g_near_peak = g_arr[_lo:_hi]
                rms = np.sqrt(np.mean(g_near_peak ** 2, axis=0))
                collected_axes.append(int(np.argmax(rms)))
            self.tilt_calib_progress = f"{len(collected_dirs)}/{self._CALIB_SAMPLE_COUNT}"
            print(f"  ✓ Sample {len(collected_dirs)}/{self._CALIB_SAMPLE_COUNT} "
                  f"terkumpul (norm={cand_norm:.3f}, axis={collected_axes[-1] if collected_axes else '?'})")

        if collected_dirs:
            mean_dir = np.mean(np.array(collected_dirs), axis=0)
            mean_norm = np.linalg.norm(mean_dir)
            if mean_norm > 1e-6:
                self._tilt_calib_vec = tuple(mean_dir / mean_norm)
            else:
                # Rata-rata vector saling meniadakan (kasus sangat langka —
                # sample-sample menunjuk arah yang jauh berbeda satu sama
                # lain) — fallback X axis, sama seperti dev is None.
                self._tilt_calib_vec = (1.0, 0.0, 0.0)
                print("⚠️  Kalibrasi tilt: sample-sample saling bertentangan arah — fallback ke axis default")
        elif from_cache:
            # Refresh diam-diam dari cache gagal total (user tidak sempat
            # tilt kanan lagi sesi ini) — JANGAN timpa vektor cache yang
            # sudah TERBUKTI valid dengan fallback X-axis, itu akan merusak
            # tilt_left/tilt_right yang sudah aktif dipakai. Tetap pakai
            # nilai cache apa adanya.
            print("⚠️  Refresh kalibrasi tilt (background) tidak dapat sample baru — tetap pakai cache lama")
        else:
            # Semua percobaan gagal — fallback X axis, sama seperti
            # fallback _run_cursor_calibration (kasus langka).
            self._tilt_calib_vec = (1.0, 0.0, 0.0)
            print("⚠️  Kalibrasi tilt gagal total — fallback ke axis default, arah mungkin tidak akurat")

        # Axis gyro dominan — VOTING MAYORITAS dari axis terpilih tiap
        # sample (bukan cuma 1 attempt) — jauh lebih tahan terhadap 1 sample
        # yang kebetulan menangkap axis salah.
        if collected_axes:
            _counts = [collected_axes.count(i) for i in range(3)]
            self._tilt_gyro_axis = int(np.argmax(_counts))
            print(f"  🗳️  Voting axis dari {len(collected_axes)} sample: {collected_axes} → axis={self._tilt_gyro_axis}")
        elif not from_cache:
            # Fallback path (tidak ada sample gyro terkumpul) — guard axis-
            # dominance di _update_tilt_command akan skip diam-diam kalau
            # axis ini None (fallback ke accel-only), tidak
            # crash. Kalau from_cache, biarkan _tilt_gyro_axis cache lama
            # apa adanya (sama alasannya dengan _tilt_calib_vec di atas).
            self._tilt_gyro_axis = None
            self._tilt_gyro_sign = 1.0

        self._tilt_calib_ready = True
        self.tilt_calib_phase = "ready"
        self.tilt_calib_progress = ""
        print(f"🎯  Kalibrasi tilt command selesai — vec={tuple(round(v,3) for v in self._tilt_calib_vec)} "
              f"gyro_axis={self._tilt_gyro_axis}")
        # Hanya cache kalibrasi yang benar-benar terukur dari gerakan user
        # (bukan fallback X-axis dari kegagalan total) — lihat collected_dirs
        # check di atas.
        if collected_dirs:
            self._save_tilt_calibration()

    def _wait_for_stable_window_tilt(self, gen: int) -> Optional[list]:
        """Sama seperti _wait_for_stable_window, tapi gate pakai self.running
        DAN generation token (bukan cursor_control_enabled) — kalibrasi tilt
        command berjalan independen dari status Cursor Control Mode, tapi
        tetap berhenti sendiri begitu ada kalibrasi baru (reconnect) yang
        menggantikannya."""
        t_start = time.time()
        window: list = []
        while (self.running and not self._imu_thread_stop.is_set()
               and gen == self._tilt_calib_gen):
            window.append(self._latest_acc)
            if len(window) > self._STABILITY_WINDOW_N:
                window.pop(0)
            elapsed = time.time() - t_start
            if len(window) >= self._STABILITY_WINDOW_N:
                arr = np.array(window)
                stds = np.std(arr, axis=0)
                if np.all(stds < self._STABILITY_STD_THRESH):
                    return window
            if elapsed > self._STABILITY_TIMEOUT_S:
                return window if window else None
            time.sleep(self._STABILITY_POLL_S)
        return None

    _TILT_CALIB_RECORD_S = 2.0   # durasi rekam kontinu fase "right" — cukup
                                  # untuk 1 gerakan tilt lengkap (naik+turun)
                                  # meski user agak lambat bereaksi ke banner.

    def _record_tilt_motion(self, gen: int):
        """Rekam SETIAP sample accel & gyro selama _TILT_CALIB_RECORD_S detik
        (bukan menunggu accel stabil lalu snapshot sesaat seperti
        _wait_for_stable_window_tilt) — dipakai fase "right" kalibrasi tilt.

        Beda filosofi dari _wait_for_stable_window_tilt: metode itu cocok
        untuk cursor control (butuh POSISI AKHIR yang ditahan), tapi untuk
        kalibrasi tilt_left/right kita justru butuh SELURUH RIWAYAT gerakan
        supaya bisa ambil titik deviasi maksimum sebagai arah kalibrasi
        (lihat pemanggil, _run_tilt_calibration) — tidak bergantung pada
        kapan tepatnya accel "kebetulan" stabil.

        Return (samples_acc, samples_gyro) — list of (x,y,z) masing-masing.
        samples_acc None kalau dibatalkan (disconnect/reconnect/generation
        baru) sebelum durasi rekam selesai."""
        t_start = time.time()
        samples_acc: list = []
        samples_gyro: list = []
        while (self.running and not self._imu_thread_stop.is_set()
               and gen == self._tilt_calib_gen):
            samples_acc.append(self._latest_acc)
            samples_gyro.append(self._latest_gyro)
            if time.time() - t_start > self._TILT_CALIB_RECORD_S:
                return samples_acc, samples_gyro
            time.sleep(self._STABILITY_POLL_S)
        return None, samples_gyro

    def _update_tilt_command(self) -> None:
        """Dipanggil tiap tick _imu_loop (~50Hz). Deteksi quick tilt-and-
        release sebagai command tilt_left/tilt_right — TERPISAH TOTAL dari
        _update_cursor_control (dipanggil hanya jika cursor mode OFF, lihat
        _imu_loop). Pola rise→release meniru arsitektur jaw clench edge-
        triggered yang sudah terbukti reliable (lihat GestureComposer/_loop),
        supaya "menoleh biasa" (lambat, tidak release cepat) tidak ke-trigger
        sebagai command.

        Axis-dominance guard: rise HANYA valid kalau axis gyro yang paling
        dominan SAAT INI sama dengan _tilt_gyro_axis (axis roll hasil
        kalibrasi) DAN cukup dominan dibanding axis lain (_TILT_GYRO_
        DOMINANCE). Menoleh (yaw) dan mengangguk (pitch) punya rotasi
        dominan di axis LAIN — proyeksi accel-nya kadang tetap lolos
        threshold (axis kalibrasi tidak pernah 100% ortogonal ke gerakan
        lain), tapi gyro-nya akan menunjukkan axis lain yang berputar,
        bukan axis roll — guard ini menolak rise itu sebelum sempat masuk
        state "risen" sama sekali."""
        if not self._tilt_calib_ready or self._tilt_calib_vec is None:
            return

        gx, gy, gz = self._latest_gyro
        gyro_vec = (gx, gy, gz)
        gyro_mag = (gx ** 2 + gy ** 2 + gz ** 2) ** 0.5
        if gyro_mag > self._GYRO_GATE_DPS:
            # Gerakan kepala terlalu cepat/kasar (mis. headset kegoyang) —
            # accel terkontaminasi, jangan pakai bacaan ini sama sekali
            # (beda dari cursor control yang menahan EMA lama; di sini kita
            # skip tick ini total supaya tidak salah mulai/mengakhiri edge).
            return

        dev = np.array(self._latest_acc) - np.array(self._tilt_neutral)
        tilt_val = float(np.dot(dev, np.array(self._tilt_calib_vec)))
        _now = time.time()

        # Arah gerakan (menjauh vs mendekat dari netral) — DITEMUKAN LEWAT
        # LOG NYATA: gerakan KEMBALI ke netral pelan-pelan (misal ekor dari
        # tilt yang tadinya gagal rise, atau sisa gerakan lain apapun) bisa
        # tetap fire, karena _update_tilt_command sebelumnya hanya melihat
        # POSISI (tilt_val) saat itu, tidak tahu apakah user sedang MENUJU
        # tilt (menjauh dari nol, gesture disengaja) atau SEDANG KEMBALI
        # (mendekat ke nol dari sisi manapun, bukan gesture baru).
        #
        # Percobaan PERTAMA (dibuang) membandingkan tilt_val dengan TEPAT 1
        # tick sebelumnya — ternyata terlalu rapuh: gerakan tilt CEPAT/NORMAL
        # (bukan cuma yang pelan) sering punya osilasi natural tick-ke-tick
        # (percepatan non-gravitasi ikut campur ke accel, bukan cuma sudut
        # statis), sehingga satu tick yang kebetulan turun sesaat — meski
        # tren keseluruhan JELAS menjauh — sudah cukup menggagalkan rise di
        # tick itu, dan syarat harus align lagi persis di tick berikutnya
        # (threshold + axis-guard + arah, ketiganya serentak). Ditemukan dari
        # log nyata: gerakan cepat (gyro >90dps) tetap gagal rise berulang.
        #
        # Fix: bandingkan dengan nilai TERKECIL dalam WINDOW beberapa tick
        # terakhir (bukan cuma 1 tick tepat sebelumnya) — toleran 1-2 tick
        # noise/osilasi, tapi tetap menolak tren mendekat yang konsisten.
        self._tilt_abs_val_window.append(abs(tilt_val))
        if len(self._tilt_abs_val_window) > self._TILT_MOVING_AWAY_WINDOW_N:
            self._tilt_abs_val_window.pop(0)
        _window_min = min(self._tilt_abs_val_window)
        _tilt_moving_away = abs(tilt_val) > _window_min + self._TILT_MOVING_AWAY_EPS

        # Diagnostik rate-limited (bukan tiap tick @50Hz — banjir terminal):
        # dicetak setiap kali proyeksi mendekati/melewati sebagian threshold,
        # supaya asimetri kiri/kanan atau axis-dominance yang menolak diam-
        # diam bisa terlihat langsung dari nilai mentah, bukan tebakan.
        if abs(tilt_val) > self._TILT_CMD_THRESHOLD * 0.5:
            if _now - self._tilt_diag_last > 0.15:
                self._tilt_diag_last = _now
                _would_side = "right" if tilt_val > 0 else "left"
                _roll_ok = self._is_roll_dominant(gyro_vec)
                print(f"  [TILT] val={tilt_val:+.3f} thr={self._TILT_CMD_THRESHOLD:.3f} "
                      f"side_if_pass={_would_side} gyro=({gx:+.1f},{gy:+.1f},{gz:+.1f}) "
                      f"gyro_axis_calib={self._tilt_gyro_axis} roll_dominant={_roll_ok} "
                      f"state={self._tilt_state}")

        if self._tilt_state == "idle":
            if _now < self._tilt_refractory_until:
                # Refractory setelah fire sebelumnya — blokir total masuk
                # "risen" lagi, mencegah rebound kepala kembali ke netral
                # ter-baca sebagai gesture kedua dari 1 gerakan fisik.
                #
                # Re-center SENGAJA TIDAK dievaluasi di sini (return lebih
                # dulu, sebelum sempat panggil _maybe_recenter_tilt_neutral)
                # — kalau kepala belum benar-benar kembali netral sempurna
                # saat settling pasca-fire (mis. masih sedikit miring), dan
                # itu ikut ter-recenter, baseline akan tertarik ke posisi
                # yang sedikit miring itu — membuat arah BALIKAN makin mudah
                # ter-trigger dan arah yang BARU SAJA di-fire makin susah di
                # sesi berikutnya (feedback loop asimetris, ditemukan lewat
                # code review, bukan cuma teori).
                return
            if not self._tilt_rearmed:
                # DITEMUKAN LEWAT LOG NYATA: timer refractory (1.5s) SAJA
                # tidak cukup — kepala butuh waktu VARIABEL untuk benar2
                # settle balik ke netral pasca-fire (fisik manusia, bukan
                # konstan). Log menunjukkan tilt_val masih naik ke arah
                # BERLAWANAN (mis. fire kiri, lalu val bergerak ke +0.117,
                # nyaris lewat threshold KANAN) tepat setelah refractory
                # numerik habis, padahal gyro rendah (bukan gerakan sengaja)
                # — residu settling fisik, bukan command baru.
                #
                # Fix: re-arm BUKAN cuma soal waktu, tapi syarat tambahan —
                # tilt_val harus terlihat kembali ke dekat nol (di bawah
                # _TILT_REARM_THRESHOLD) MINIMAL SEKALI sebelum rise baru
                # diizinkan, arah manapun. Begitu terlihat dekat nol sekali,
                # _tilt_rearmed dikunci True dan tidak perlu dicek ulang
                # sampai fire berikutnya me-reset ke False lagi.
                #
                # PENTING — re-center TETAP dipanggil (di bawah, satu kali
                # per tick — lihat penjelasan di luar blok ini) meski belum
                # re-armed, SELAMA gyro genuinely rendah (dicek di dalam
                # _maybe_recenter_tilt_neutral sendiri via _TILT_RECENTER_
                # GYRO_DPS). Tanpa ini ada risiko deadlock: kalau baseline
                # sudah bergeser cukup jauh sehingga tilt_val TIDAK PERNAH
                # turun di bawah _TILT_REARM_THRESHOLD lagi, sistem butuh
                # recenter untuk pulih — tapi recenter butuh jalur ini untuk
                # jalan. Memutus ketergantungan melingkar itu: begitu kepala
                # genuinely diam (syarat recenter), baseline perlahan ditarik
                # kembali mendekat, yang pada gilirannya membuat tilt_val
                # turun dan akhirnya memenuhi syarat re-arm secara alami.
                if (abs(tilt_val) < self._TILT_REARM_THRESHOLD
                        or _now - self._tilt_last_fire_time > self._TILT_REARM_TIMEOUT_S):
                    # Timeout fallback: kalau re-arm alami tidak kunjung
                    # terjadi dalam _TILT_REARM_TIMEOUT_S (mis. baseline
                    # bergeser sangat jauh dan recenter belum sempat
                    # mengejar), paksa re-arm saja — trade-off satu
                    # kemungkinan false-positive lebih baik daripada command
                    # mati total sampai reconnect manual.
                    self._tilt_rearmed = True
                else:
                    self._maybe_recenter_tilt_neutral(gyro_mag, _now)
                    return
            # Re-center dipanggil TEPAT SEKALI per tick di sini — mencakup
            # baik kasus "sudah re-armed sejak awal tick" (blok if di atas
            # di-skip total) maupun "baru saja re-armed tick ini" (blok di
            # atas TIDAK memanggil recenter di cabang sukses, sengaja
            # dihindari supaya tidak dobel-panggil dalam tick yang sama —
            # ditemukan lewat code review: dobel panggil saat _tilt_still_
            # since sudah lewat _TILT_RECENTER_HOLD_S membuat EMA diterapkan
            # 2× dalam 1 tick, ~9.75% pull alih-alih 5% yang seharusnya).
            # Re-center HANYA dievaluasi saat idle, di luar refractory, DAN
            # sudah re-armed (settling pasca-fire sudah genuinely selesai) —
            # lihat _maybe_recenter_tilt_neutral untuk kenapa ini aman
            # dilakukan tanpa mengulang kegagalan auto drift-correction
            # cursor control (_imu_baseline, lihat catatan di
            # _update_cursor_control soal 2 percobaan yang dibuang).
            self._maybe_recenter_tilt_neutral(gyro_mag, _now)
            if (abs(tilt_val) > self._TILT_CMD_THRESHOLD
                    and self._is_roll_dominant(gyro_vec)
                    and _tilt_moving_away):
                self._tilt_state     = "risen"
                self._tilt_rise_side = "right" if tilt_val > 0 else "left"
                self._tilt_rise_time = _now
                self._tilt_rise_peak = abs(tilt_val)
        elif self._tilt_state == "risen":
            held_s = _now - self._tilt_rise_time
            self._tilt_rise_peak = max(self._tilt_rise_peak, abs(tilt_val))
            # Release RELATIF terhadap puncak yang dicapai selama risen —
            # DITEMUKAN LEWAT LOG NYATA: gerakan tilt yang KUAT (val naik
            # sampai 0.14, bahkan >1.0 untuk gerakan sangat cepat) hampir
            # tidak pernah turun kembali ke ambang absolut tetap (0.055)
            # dalam window _TILT_CMD_RELEASE_MAX_S=0.8s, karena itu berarti
            # harus kembali HAMPIR SEPENUHNYA ke netral — proporsinya jauh
            # lebih ketat untuk gerakan besar dibanding gerakan pas-pasan di
            # atas threshold. Akibatnya rise SELALU timeout ke idle tanpa
            # fire, meski gesture jelas terjadi (axis-dominance & arah sudah
            # benar). Sengaja MURNI relatif (bukan OR dengan ambang absolut
            # lama) — code review menemukan OR itu justru memberi gerakan
            # LEMAH (peak pas-pasan di atas threshold) release condition
            # yang lebih longgar dari sebelumnya (0.6×0.12=0.072 > ambang
            # lama 0.055), efek samping tak disengaja yang bisa membuat
            # sustained-hold lemah ikut fire. Murni relatif memberi perilaku
            # proporsional konsisten untuk semua kekuatan gerakan: harus
            # turun 40% dari puncaknya sendiri, tidak peduli seberapa besar
            # puncak itu.
            _released = abs(tilt_val) < self._tilt_rise_peak * self._TILT_RELEASE_RATIO
            if _released:
                if self._TILT_CMD_RELEASE_MIN_S <= held_s <= self._TILT_CMD_RELEASE_MAX_S:
                    self._fire_tilt_command(self._tilt_rise_side, _now)
                # Held terlalu singkat (micro-blip) ATAU melewati window
                # (ditahan lama) — dibuang, bukan command. Kembali idle.
                self._tilt_state     = "idle"
                self._tilt_rise_side = ""
            elif held_s > self._TILT_CMD_RELEASE_MAX_S:
                # Timeout ditahan terlalu lama tanpa release — bukan quick
                # tilt (kemungkinan menyandarkan kepala), buang tanpa fire.
                self._tilt_state     = "idle"
                self._tilt_rise_side = ""

    def _maybe_recenter_tilt_neutral(self, gyro_mag: float, now: float) -> None:
        """Koreksi drift _tilt_neutral secara bertahap — dipanggil HANYA saat
        _tilt_state=="idle" (tidak pernah di tengah gesture "risen").

        Beda filosofi dari auto drift-correction _imu_baseline (cursor
        control) yang SUDAH DICOBA 2× dan DIBUANG (lihat catatan panjang di
        _update_cursor_control): cursor control gagal karena TIDAK BISA
        membedakan "kepala genuinely netral" dari "kepala diam MENAHAN tilt
        tertentu" — keduanya sama-sama low-variance accel dalam jangka
        pendek, sehingga baseline mengejar posisi tertahan itu dan membuat
        target netral "mengembara" tanpa henti.

        Command tilt (di sini) punya sinyal tambahan yang TIDAK dimiliki
        cursor control: GYRO. Kepala yang genuinely diam punya gyro mendekati
        nol di SEMUA axis; kepala yang ditahan miring (bahkan diam sempurna
        dalam arti accel) baru saja SELESAI berputar untuk sampai ke posisi
        itu — tapi karena re-center ini hanya jalan saat _tilt_state=="idle"
        (bukan "risen"), kasus "baru saja tilt dan sedang menahan" sudah
        tidak relevan: begitu _TILT_CMD_RELEASE_MAX_S (0.8s) terlewati tanpa
        release, state SUDAH kembali ke "idle" (dibuang, dianggap bukan
        command) — jadi menahan-tilt-lama tetap dianggap "idle" di titik ini,
        SAMA seperti benar-benar netral. Ini bukan celah baru: menahan tilt
        lama memang sudah didesain untuk tidak fire (lihat komentar rise
        blok di _update_tilt_command), jadi mengizinkannya ikut ter-recenter
        konsisten dengan perilaku yang sudah ada, bukan regresi.

        EMA lambat (bukan snapshot instan) — tiap re-center menggeser
        _tilt_neutral sedikit ke arah posisi accel saat ini, bukan lompat
        langsung, supaya tidak ada perubahan baseline mendadak yang terasa
        aneh kalau user langsung tilt lagi persis setelah re-center."""
        if gyro_mag >= self._TILT_RECENTER_GYRO_DPS:
            # Sedang bergerak (meski di bawah _GYRO_GATE_DPS) — bukan
            # kandidat "diam", reset penghitung durasi diam.
            self._tilt_still_since = None
            return
        if self._tilt_still_since is None:
            self._tilt_still_since = now
            return
        if now - self._tilt_still_since >= self._TILT_RECENTER_HOLD_S:
            old = np.array(self._tilt_neutral)
            new = old * (1 - self._TILT_RECENTER_EMA_ALPHA) + \
                  np.array(self._latest_acc) * self._TILT_RECENTER_EMA_ALPHA
            self._tilt_neutral = tuple(new)
            # Tidak reset _tilt_still_since — biarkan re-center lanjut
            # bertahap tiap tick selama user tetap diam, EMA alpha kecil
            # sudah membuat perubahan per-tick sangat halus (bukan lompatan).

    def _is_roll_dominant(self, gyro_vec: tuple) -> bool:
        """True kalau axis gyro roll hasil kalibrasi (_tilt_gyro_axis) yang
        paling dominan SAAT INI, dan cukup dominan dibanding axis kedua
        terbesar (_TILT_GYRO_DOMINANCE). Dipakai menolak menoleh/mengangguk
        yang kebetulan proyeksi accel-nya lolos threshold tilt."""
        if self._tilt_gyro_axis is None:
            # Kalibrasi gyro tidak berhasil merekam sample (kasus langka) —
            # tidak ada dasar untuk menolak, biarkan lolos (fallback ke
            # perilaku lama: hanya accel yang menentukan).
            return True
        mags = [abs(gyro_vec[0]), abs(gyro_vec[1]), abs(gyro_vec[2])]
        roll_mag = mags[self._tilt_gyro_axis]
        if roll_mag < self._TILT_GYRO_MIN_DPS:
            # Rotasi terlalu kecil untuk dinilai — bukan berarti BUKAN roll,
            # tapi juga tidak cukup bukti bahwa ITU roll. Tolak supaya sisi
            # aman (kurangi false positive) daripada asumsikan valid.
            return False
        others = [m for i, m in enumerate(mags) if i != self._tilt_gyro_axis]
        second_largest = max(others) if others else 0.0
        return roll_mag >= second_largest * self._TILT_GYRO_DOMINANCE

    def _fire_tilt_command(self, side: str, now: float) -> None:
        """Guard cross-trigger sebelum fire — sinyal accel/gyro sudah
        orthogonal terhadap EMG (channel & sensor beda total), tapi tilt
        kuat tetap bisa menggoyang elektroda dan memicu artefak EMG palsu
        di AF7/AF8 — maka tilt tetap dimasukkan ke global mutex & pairwise
        cooldown yang sama dipakai wink/eyebrow/jaw, bukan cuma cooldown
        sendiri, supaya command lain tidak ikut kesenggol atau sebaliknya."""
        _cmd_idle    = (now - self._last_cmd_time) > 1.5
        _after_wink  = (now - self._wink_cooldown) < 1.5
        _after_jaw   = (now - self._jaw_cooldown) < 2.5
        _after_eb    = (now - self._eyebrow_cooldown) < 3.0
        _own_cd_ok   = (now - self._tilt_cooldown) > self._TILT_CMD_COOLDOWN_S

        if not (_cmd_idle and not _after_wink and not _after_jaw
                and not _after_eb and _own_cd_ok):
            return

        self._tilt_cooldown = now
        self._last_cmd_time = now
        # Refractory: blokir masuk state "risen" lagi sampai durasi ini,
        # dicek di _update_tilt_command SEBELUM rise diizinkan (bukan cuma
        # dicek di titik fire seperti _tilt_cooldown) — mencegah rebound
        # kepala kembali ke netral setelah fire ter-baca sebagai rise kedua
        # yang valid, menghasilkan trigger dobel dari 1 gerakan fisik.
        self._tilt_refractory_until = now + self._TILT_CMD_REFRACTORY_S
        # Re-arm dikunci False — rise baru (arah manapun) diblokir sampai
        # tilt_val terlihat dekat nol (settling genuinely selesai) minimal
        # sekali, DI ATAS timer refractory (lihat _TILT_REARM_THRESHOLD di
        # _update_tilt_command). Menutup celah rebound/overshoot fisik yang
        # durasinya variabel, ditemukan dari log testing nyata.
        self._tilt_rearmed = False
        self._tilt_last_fire_time = now   # dasar hitung _TILT_REARM_TIMEOUT_S
        cb = self.on_tilt_left if side == "left" else self.on_tilt_right
        print(f"↩️  Tilt {side} FIRED — val_thr={self._TILT_CMD_THRESHOLD}")
        if cb:
            try:
                cb()
            except Exception as e:
                print(f"⚠️  on_tilt_{side} error: {e}")

    def _update_cursor_control(self) -> None:
        """Dipanggil tiap tick _imu_loop (~50Hz) saat cursor_control_enabled.
        Hitung tilt dari accel (baseline-relative), gate pakai gyro (motion
        artifact), smoothing EMA, lalu map ke velocity px/detik."""
        if not self._cursor_baseline_ready:
            # Masih dalam proses kalibrasi 3-tahap (lihat
            # _run_cursor_calibration) — baseline/basis right-up belum valid,
            # cursor harus diam total, bukan bergerak berdasar data lama/default.
            self.cursor_velocity_x = 0.0
            self.cursor_velocity_y = 0.0
            return

        gx, gy, gz = self._latest_gyro
        gyro_mag = (gx ** 2 + gy ** 2 + gz ** 2) ** 0.5

        if gyro_mag > self._GYRO_GATE_DPS:
            # Kepala bergerak cepat (bukan tilt statis) — accel terkontaminasi
            # akselerasi linear sesaat, bukan murni vektor gravitasi. Tahan
            # nilai tilt EMA sebelumnya daripada ikut lonjakan palsu.
            pass
        else:
            tilt_up, tilt_right = self._tilt_from_baseline(self._latest_acc, self._imu_baseline)
            self._tilt_ema_up    = (self._tilt_ema_up    * (1 - self._TILT_EMA_ALPHA)
                                     + tilt_up    * self._TILT_EMA_ALPHA)
            self._tilt_ema_right = (self._tilt_ema_right * (1 - self._TILT_EMA_ALPHA)
                                     + tilt_right * self._TILT_EMA_ALPHA)

            # Baseline drift correction DIHAPUS TOTAL — dua percobaan sama-sama
            # gagal:
            # 1. Dicek via "abs(tilt) < DEADZONE" — catch-22 kalau baseline
            #    sendiri sudah salah jauh (tilt selalu di luar deadzone
            #    terhadap baseline salah, koreksi tak pernah jalan).
            # 2. Dicek via variance accel jangka pendek (~0.4s @ 50Hz) — TIDAK
            #    bisa membedakan "kepala genuinely netral" dari "kepala diam
            #    MENAHAN tilt tertentu" (mis. user sengaja menahan cursor
            #    bergerak ke satu arah lama) — keduanya sama-sama variance
            #    rendah. Log nyata membuktikan: baseline X mengembara dari
            #    +0.478 ke -0.6 (drift 1.09g!) sepanjang satu sesi, karena
            #    tiap kali user menahan tilt, sistem salah kira itu "netral
            #    baru" dan mengejarnya — akibatnya baseline TIDAK PERNAH
            #    stabil, dan gejalanya sama seperti tanpa correction sama
            #    sekali (target netral yang bergerak-gerak terasa exactly
            #    seperti cursor "hanyut" ke arah tertentu terus).
            # Baseline sekarang HANYA direkam sekali di kalibrasi awal
            # (_run_cursor_calibration) dan tidak pernah berubah otomatis
            # selama sesi. Kalau baseline melenceng karena postur berubah,
            # solusi = toggle OFF lalu ON lagi (re-kalibrasi manual), bukan
            # auto-correction yang justru menambah masalah.

        # Konversi tilt → screen-space velocity. Layar: y kecil = atas, y
        # besar = bawah. tilt_up positif (kepala mendongak — arah "up" hasil
        # kalibrasi nyata di _run_cursor_calibration) harus menggerakkan
        # cursor ke ATAS → vy negatif.
        vx = self._tilt_to_speed(self._tilt_ema_right)
        vy = -self._tilt_to_speed(self._tilt_ema_up)

        # Diagnostik sementara — dicetak ~2x/detik (bukan tiap tick ~50Hz,
        # supaya terminal tidak banjir). Dipakai untuk debug axis mapping
        # bersama user secara langsung dari data nyata, bukan tebakan.
        self._cursor_dbg_tick = getattr(self, "_cursor_dbg_tick", 0) + 1
        if self._cursor_dbg_tick % 25 == 0:
            print(f"  [CURSOR] acc={tuple(round(v,3) for v in self._latest_acc)} "
                  f"baseline={tuple(round(v,3) for v in self._imu_baseline)} "
                  f"tilt_ema(up,right)=({self._tilt_ema_up:+.3f},{self._tilt_ema_right:+.3f}) "
                  f"v=({vx:+.0f},{vy:+.0f})")

        self.cursor_velocity_x = vx
        self.cursor_velocity_y = vy
        if self.on_cursor_velocity:
            try:
                self.on_cursor_velocity(vx, vy)
            except Exception as e:
                print(f"⚠️  on_cursor_velocity error: {e}")

    # ── Heart rate ────────────────────────────────────────────────────────

    def _compute_heart_rate(self, ppg: np.ndarray, sr: int = 64) -> Optional[float]:
        if len(ppg) < sr * 4:
            return None
        sig = ppg[-sr * 10:].astype(float)
        sig -= np.mean(sig)
        w = max(1, int(sr * 0.08))
        sig = np.convolve(sig, np.ones(w) / w, mode="same")
        peaks = self._find_peaks(sig, int(sr * 0.33))
        if len(peaks) < 3:
            return None
        intervals = np.diff(peaks) / float(sr)
        intervals = intervals[(intervals > 0.33) & (intervals < 1.7)]
        if len(intervals) < 2:
            return None
        return round(60.0 / float(np.median(intervals)))

    @staticmethod
    def _find_peaks(signal: np.ndarray, min_distance: int) -> list:
        threshold = np.std(signal) * 0.3
        peaks: list = []
        for i in range(1, len(signal) - 1):
            if (signal[i] > signal[i - 1] and signal[i] > signal[i + 1]
                    and signal[i] > threshold):
                if not peaks or (i - peaks[-1]) >= min_distance:
                    peaks.append(i)
                elif signal[i] > signal[peaks[-1]]:
                    peaks[-1] = i
        return peaks

    # ── EEG normalization ─────────────────────────────────────────────────

    def _normalize(self, key: str, value: float) -> float:
        h = self._history[key]
        h.append(value)
        if len(h) > self._HIST_LEN:
            h.pop(0)
        if len(h) < 5:
            return 0.5
        lo = float(np.percentile(h, 10))
        hi = float(np.percentile(h, 90))
        if hi <= lo:
            return 0.5
        return float(np.clip((value - lo) / (hi - lo), 0.0, 1.0))
