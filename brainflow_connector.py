"""
Muse 2 Connector — muselsl + pylsl
====================================
Akuisisi EEG & PPG dari Muse 2 menggunakan muselsl (subprocess) dan
pylsl (membaca LSL streams). Signal processing tetap pakai BrainFlow
DataFilter.

Install: pip3 install muselsl pylsl

Digunakan oleh music_server.py (interface tidak berubah).
"""

import subprocess
import sys
import threading
import time
import tempfile
import os
from typing import Callable, Optional

import numpy as np

# BrainFlow DataFilter — signal processing only (no BoardShim needed)
try:
    from brainflow.data_filter import DataFilter, DetrendOperations, WindowOperations
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
        self.raw_bands: dict = {"delta": 0.0, "alpha": 0.0, "beta": 0.0, "theta": 0.0}

        # Internal
        self.running       = False
        self._loop_tick    = 0
        self._history      = {"delta": [], "alpha": [], "beta": [], "theta": []}
        self._HIST_LEN     = 60
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
        self._history    = {"delta": [], "alpha": [], "beta": [], "theta": []}
        self.heart_rate  = None
        self._loop_tick  = 0
        self.channel_quality = {"TP9": 0.0, "AF7": 0.0, "AF8": 0.0, "TP10": 0.0}
        self.raw_bands   = {"delta": 0.0, "alpha": 0.0, "beta": 0.0, "theta": 0.0}
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
        try:
            self._launch_and_loop(mac_address)
        except Exception as e:
            self.running = False
            self._kill_proc()
            self._set_status("error", str(e))
            print(f"❌  Connection failed: {e}")
        finally:
            self.running = False

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

        eeg_inlet = StreamInlet(eeg_streams[0], max_buflen=5)
        ppg_inlet = StreamInlet(ppg_streams[0], max_buflen=30) if ppg_streams else None

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
        # alpha=0.35: nilai baru berkontribusi 35%, time constant ~3 detik
        # (cukup responsif untuk tangkap focus nyata, tetap filter artifact)
        EMA = 0.35
        ema_d = ema_a = ema_b = ema_t = 0.5       # normalized EMA
        ema_d_raw = ema_a_raw = ema_b_raw = ema_t_raw = 0.0  # raw uV2 EMA

        while self.running:
            time.sleep(1.0)
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
                chunk, _ = eeg_inlet.pull_chunk(timeout=0.0, max_samples=512)
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
                    ppg_chunk, _ = ppg_inlet.pull_chunk(timeout=0.0, max_samples=256)
                except Exception:
                    ppg_chunk = []
                for sample in ppg_chunk:
                    # muselsl PPG: [ambient, IR, red] — use IR (index 1)
                    val = sample[1] if len(sample) > 1 else sample[0]
                    ppg_buf[ppg_ptr % PPG_MAX] = val
                    ppg_ptr   += 1
                    ppg_total += 1

            if eeg_total < SAMPLE_RATE:
                continue  # not enough data yet

            try:
                # ── EEG window (last 1 s) ─────────────────────────────────
                n     = min(eeg_total, SAMPLE_RATE)
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
                    if std < 3.0 or std > 400.0:  q = 0.0
                    elif std > 150.0:              q = 0.25
                    elif std < 8.0:                q = std / 8.0 * 0.6
                    else:                          q = 1.0
                    q = round(q, 2)
                    self.channel_quality[name] = q
                    ch_quality.append(q)

                # ── Band power — hanya dari channel yang cukup bagus ──────
                # Channel dengan quality <= 0.0 (flat/disconnected/sangat noisy)
                # dikecualikan karena noise broadband membuat semua band tampak tinggi
                delta_list, alpha_list, beta_list, theta_list = [], [], [], []
                for ch in range(4):
                    if ch_quality[ch] < 0.25:
                        continue  # skip channel poor/disconnected
                    ch_data = eeg_win[ch].copy()
                    DataFilter.detrend(ch_data, DetrendOperations.CONSTANT.value)
                    psd = DataFilter.get_psd_welch(
                        ch_data, SAMPLE_RATE, SAMPLE_RATE // 2, SAMPLE_RATE,
                        WindowOperations.BLACKMAN_HARRIS.value
                    )
                    delta_list.append(DataFilter.get_band_power(psd, 0.5,  4.0))
                    alpha_list.append(DataFilter.get_band_power(psd, 8.0,  13.0))
                    beta_list.append( DataFilter.get_band_power(psd, 13.0, 30.0))
                    theta_list.append(DataFilter.get_band_power(psd, 4.0,   8.0))

                if not alpha_list:
                    continue  # semua channel poor — skip update, jangan kirim nilai palsu

                delta = self._normalize("delta", float(np.mean(delta_list)))
                alpha = self._normalize("alpha", float(np.mean(alpha_list)))
                beta  = self._normalize("beta",  float(np.mean(beta_list)))
                theta = self._normalize("theta", float(np.mean(theta_list)))

                # EMA smoothing — blends spike sebelum dikirim ke engine
                ema_d = ema_d * (1 - EMA) + delta * EMA
                ema_a = ema_a * (1 - EMA) + alpha * EMA
                ema_b = ema_b * (1 - EMA) + beta  * EMA
                ema_t = ema_t * (1 - EMA) + theta * EMA
                self.engine.set_eeg(ema_a, ema_b, ema_t, ema_d)

                # Raw uV2 EMA — untuk display UI
                ema_d_raw = ema_d_raw*(1-EMA) + float(np.mean(delta_list))*EMA
                ema_a_raw = ema_a_raw*(1-EMA) + float(np.mean(alpha_list))*EMA
                ema_b_raw = ema_b_raw*(1-EMA) + float(np.mean(beta_list)) *EMA
                ema_t_raw = ema_t_raw*(1-EMA) + float(np.mean(theta_list))*EMA
                self.raw_bands = {
                    "delta": round(ema_d_raw, 2),
                    "alpha": round(ema_a_raw, 2),
                    "beta":  round(ema_b_raw, 2),
                    "theta": round(ema_t_raw, 2),
                }

                # ── HR from PPG every 5 s ─────────────────────────────────
                if ppg_inlet and self._loop_tick % 5 == 0 and ppg_total >= PPG_SR * 4:
                    n_p  = min(ppg_total, PPG_MAX)
                    sp   = (ppg_ptr - n_p) % PPG_MAX
                    if sp + n_p <= PPG_MAX:
                        ppg_win = ppg_buf[sp:sp + n_p].copy()
                    else:
                        ppg_win = np.concatenate([ppg_buf[sp:], ppg_buf[:n_p - (PPG_MAX - sp)]])
                    hr = self._compute_heart_rate(ppg_win, PPG_SR)
                    if hr is not None:
                        self.heart_rate = hr

                # ── Terminal log ──────────────────────────────────────────
                state_hint = (
                    "stress" if beta > 0.65 and theta > 0.55 else
                    "focus"  if beta > 0.60 and alpha < 0.35 else
                    "drowsy" if theta > 0.65 else
                    "relax"
                )
                hr_str = f"  ♥={self.heart_rate:.0f}" if self.heart_rate else ""
                print(f"  EEG  α={ema_a:.2f}  β={ema_b:.2f}  θ={ema_t:.2f}  → {state_hint}{hr_str}")

            except Exception as e:
                if self.running:
                    print(f"⚠️  Loop error: {e}")

        # Loop ended — clean up
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
