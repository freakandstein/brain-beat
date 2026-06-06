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


class MuseConnector:
    """
    Thread-safe Muse 2 connector.
    Uses muselsl (subprocess) for BLE acquisition + pylsl for reading.
    Same public interface as the previous BrainFlow-based version.

        conn = MuseConnector(engine, on_status=lambda s, e: print(s, e))
        conn.connect("45A06A8D-FC1E-6656-6CC2-BA3EF830CF41")
        conn.disconnect()
    """

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
        self._eyebrow_cooldown: float = 0.0   # timestamp terakhir trigger

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

    def disconnect(self) -> None:
        """Disconnect Muse 2 and kill the muselsl subprocess."""
        self.running = False
        self._cancel.set()
        self._kill_proc()
        self._history    = {"alpha": [], "beta": [], "theta": [], "tbr": [],
                            "frontal_alpha": [], "frontal_theta": []}
        self.heart_rate  = None
        self._eyebrow_cooldown = 0.0
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

        script = (
            "from muselsl import stream; "
            f"stream(address='{mac_address}', ppg_enabled=True, "
            "acc_enabled=False, gyro_enabled=False)"
        )
        self._stream_proc = subprocess.Popen(
            [sys.executable, "-c", script],
            stdout=subprocess.DEVNULL,
            stderr=self._err_file,
        )

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

        ppg_streams = resolve_byprop("type", "PPG", timeout=3.0)

        eeg_inlet = StreamInlet(eeg_streams[0], max_buflen=30, max_chunklen=0)
        ppg_inlet = StreamInlet(ppg_streams[0], max_buflen=60, max_chunklen=0) if ppg_streams else None

        if ppg_inlet:
            print("📡  PPG LSL stream found — HR enabled!")
        else:
            print("⚠️  PPG stream not found — HR disabled")

        self.running = True
        self._set_status("connected")
        print("✅  Muse 2 connected via muselsl!")
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
        _csv_w.writerow(['time', 'elapsed_s', 'alpha', 'beta', 'theta', 'tbr', 'state', 'hr'])
        _t0 = time.time()
        print(f"📊  Logging EEG ke {_csv_path}")

        while self.running:
            time.sleep(0.25)  # 4 Hz — lag max 250 ms sebelum EEG sampai ke engine
            self._loop_tick += 1

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

                # ── Band power — hanya dari channel yang cukup bagus ──────
                # Channel dengan quality <= 0.0 (flat/disconnected/sangat noisy)
                # dikecualikan karena noise broadband membuat semua band tampak tinggi
                _CH_NAMES = ["TP9", "AF7", "AF8", "TP10"]
                alpha_list, beta_list, theta_list = [], [], []
                frontal_beta_list, frontal_theta_list = [], []  # AF7=1, AF8=2 only

                # ── Pass 1: deteksi frontal EMG SEBELUM proses temporal ────────
                # Scan semua channel tanpa break — perlu nilai kedua channel untuk
                # eyebrow raise bilateral detection.
                _frontal_emg = False
                _ch_emg = {1: False, 2: False}
                _ch_p2p = {1: 0.0,   2: 0.0}
                for ch in (1, 2):  # AF7=1, AF8=2
                    if ch_quality[ch] < 0.25:
                        continue
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

                # ── Eyebrow raise detection ────────────────────────────────────
                # Syarat:
                #   1. Bilateral: AF7 DAN AF8 keduanya >350µV
                #   2. Simetris: rasio max/min kedua channel < 3.0
                #      (eyebrow raise genuine = kedua sisi aktif proporsional,
                #       noise/artifact unilateral biasanya satu sisi jauh lebih tinggi)
                _now = time.time()
                _p2p_af7 = _ch_p2p[1]
                _p2p_af8 = _ch_p2p[2]
                _both_strong = _p2p_af7 > 350.0 and _p2p_af8 > 350.0
                _symmetric   = (max(_p2p_af7, _p2p_af8) / (min(_p2p_af7, _p2p_af8) + 1e-6)) < 3.0
                _bilateral   = _both_strong and _symmetric

                if _frontal_emg:
                    _cd_left = max(0.0, 3.0 - (_now - self._eyebrow_cooldown))
                    print(
                        f"  [EMG] AF7 p2p={_ch_p2p[1]}µV | AF8 p2p={_ch_p2p[2]}µV | "
                        f"bilateral={_bilateral} cooldown={_cd_left:.1f}s"
                    )

                if (_bilateral and
                        self.on_eyebrow_raise and
                        _now - self._eyebrow_cooldown > 3.0):
                    self._eyebrow_cooldown = _now
                    print("⚡  Eyebrow raise FIRED")
                    try:
                        self.on_eyebrow_raise()
                    except Exception as _e:
                        print(f"⚠️  on_eyebrow_raise error: {_e}")

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
                for ch in range(4):
                    if ch_quality[ch] < 0.25:
                        continue  # skip channel poor/disconnected
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

                # Frontal alpha/theta EMA — dari AF7+AF8 saja, untuk flow_score
                if alpha_list:  # alpha_list berisi AF7+AF8 (pass 2, ch in (1,2))
                    fa_raw = self._normalize("frontal_alpha", float(np.mean(alpha_list)))
                    ft_raw = self._normalize("frontal_theta", float(np.mean(theta_list)))
                    ema_fa = ema_fa * (1 - EMA) + fa_raw * EMA
                    ema_ft = ema_ft * (1 - EMA) + ft_raw * EMA
                self.frontal_alpha = round(ema_fa, 3)
                self.frontal_theta = round(ema_ft, 3)

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
                if ppg_inlet and self._loop_tick % 20 == 0 and ppg_total >= PPG_SR * 4:  # every 5 s at 4 Hz
                    n_p  = min(ppg_total, PPG_MAX)
                    sp   = (ppg_ptr - n_p) % PPG_MAX
                    if sp + n_p <= PPG_MAX:
                        ppg_win = ppg_buf[sp:sp + n_p].copy()
                    else:
                        ppg_win = np.concatenate([ppg_buf[sp:], ppg_buf[:n_p - (PPG_MAX - sp)]])
                    hr = self._compute_heart_rate(ppg_win, PPG_SR)
                    if hr is not None:
                        self.heart_rate = hr

                # ── Terminal log + CSV — 1 Hz (setiap 4 tick di 4 Hz) ────────────────
                if self._loop_tick % 4 == 0:
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
                        round(ema_tbr, 3), state_hint,
                        round(self.heart_rate) if self.heart_rate else ''
                    ])
                    _csv_file.flush()

            except Exception as e:
                if self.running:
                    print(f"⚠️  Loop error: {e}")

        # Loop ended — clean up
        _csv_file.close()
        print(f"📊  Session log disimpan: {_csv_path}")
        err = self._read_err_log()
        if err:
            print(f"  muselsl last output: {err}")
        self._kill_proc()
        if self.status == "connected":
            self._set_status("disconnected")

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
