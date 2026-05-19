"""
BrainFlow Connector — Muse 2
=============================
Membaca data EEG dari Muse 2 via BrainFlow, menghitung band power
(alpha/beta/theta), normalisasi rolling percentile, lalu memanggil
engine.set_eeg() setiap detik.

Digunakan oleh music_server.py.
"""

import threading
import time
from typing import Callable, Optional

try:
    import numpy as np
    from brainflow.board_shim import BoardShim, BrainFlowInputParams, BoardIds, LogLevels
    from brainflow.data_filter import DataFilter, DetrendOperations, WindowOperations
    BRAINFLOW_AVAILABLE = True
except ImportError:
    BRAINFLOW_AVAILABLE = False

try:
    from bleak import BleakScanner
    BLEAK_AVAILABLE = True
except ImportError:
    BLEAK_AVAILABLE = False

BOARD_ID    = 38     # BoardIds.MUSE_2_BOARD
SAMPLE_RATE = 256    # Hz, fixed untuk Muse 2


def scan_muse_devices(timeout: float = 5.0) -> list:
    """
    Scan BLE devices dan kembalikan list Muse yang ditemukan.
    Return: [(name, address), ...]
    Membutuhkan bleak: pip3 install bleak
    """
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
    Thread-safe connector untuk Muse 2 via BrainFlow.

    Cara pakai:
        conn = MuseConnector(engine, on_status=lambda s, e: print(s, e))
        conn.connect()             # auto-discover, atau
        conn.connect("CE:23:CC:54:D3:EE")  # MAC spesifik
        conn.disconnect()
    """

    def __init__(self, engine, on_status: Optional[Callable] = None):
        """
        engine      : MusicEngine instance
        on_status   : callback(status: str, error: str) dipanggil saat status berubah
                      status: "disconnected" | "connecting" | "connected" | "error"
        """
        if not BRAINFLOW_AVAILABLE:
            raise RuntimeError(
                "brainflow tidak terinstall. Jalankan:\n"
                "    pip3 install brainflow"
            )

        self.engine     = engine
        self.on_status  = on_status
        self.board: Optional[BoardShim] = None
        self.running    = False
        self._thread: Optional[threading.Thread] = None

        # Status publik
        self.status    = "disconnected"
        self.error_msg = ""
        self.heart_rate: Optional[float] = None  # reserved — BrainFlow MUSE_2_BOARD tidak stream PPG
        self.channel_quality: dict = {"TP9": 0.0, "AF7": 0.0, "AF8": 0.0, "TP10": 0.0}

        # Rolling history untuk normalisasi per-user (60 detik)
        self._history = {"alpha": [], "beta": [], "theta": []}
        self._HIST_LEN = 60

    # ── public API ────────────────────────────────────────────────────────

    def connect(self, mac_address: str = "") -> None:
        """Mulai koneksi di background thread. Tidak blocking."""
        if self.running:
            return
        self._set_status("connecting")
        t = threading.Thread(
            target=self._connect_thread,
            args=(mac_address,),
            daemon=True
        )
        t.start()

    def disconnect(self) -> None:
        """Putuskan koneksi."""
        self.running = False
        try:
            if self.board and self.board.is_prepared():
                self.board.stop_stream()
                self.board.release_session()
        except Exception as e:
            print(f"⚠️  Disconnect error: {e}")
        self.board = None
        self._history = {"alpha": [], "beta": [], "theta": []}
        self.heart_rate  = None
        self.channel_quality = {"TP9": 0.0, "AF7": 0.0, "AF8": 0.0, "TP10": 0.0}
        self._set_status("disconnected")
        print("■  Muse 2 disconnected.")

    # ── internal ──────────────────────────────────────────────────────────

    def _set_status(self, status: str, error: str = "") -> None:
        self.status    = status
        self.error_msg = error
        if self.on_status:
            try:
                self.on_status(status, error)
            except Exception:
                pass

    def _connect_thread(self, mac_address: str) -> None:
        # Cleanup board lama sebelum membuat yang baru
        # (mencegah GC men-release session aktif secara tak terduga)
        if self.board is not None:
            try:
                self.board.release_session()
            except Exception:
                pass
            self.board = None

        board = None
        try:
            BoardShim.disable_board_logger()

            params = BrainFlowInputParams()
            if mac_address:
                params.serial_port = mac_address
                print(f"🔵  Menghubungkan ke Muse 2 ({mac_address})...")
            else:
                print("🔵  Mencari Muse 2 (auto-discover)...")

            board = BoardShim(BOARD_ID, params)
            board.prepare_session()
            board.start_stream(45000)

            # Assign ke self.board HANYA setelah prepare+stream berhasil
            self.board = board
            self.running = True
            self._set_status("connected")
            print("✅  Muse 2 terhubung! Membaca EEG...")
            self._loop()

        except Exception as e:
            err = str(e)
            # Cleanup board yang gagal sebelum set error status
            if board is not None:
                try:
                    board.release_session()
                except Exception:
                    pass
            self.board = None
            self._set_status("error", err)
            print(f"❌  Gagal koneksi Muse 2: {err}")
            print("    Tips: Pastikan Muse 2 menyala (LED berkedip) dan belum terhubung ke app lain.")

    def _loop(self) -> None:
        eeg_channels = BoardShim.get_eeg_channels(BOARD_ID)
        # Muse 2: eeg_channels = [1, 2, 3, 4] → TP9, AF7, AF8, TP10
        frontal  = [eeg_channels[1], eeg_channels[2]]  # AF7, AF8 — frontal
        temporal = [eeg_channels[0], eeg_channels[3]]  # TP9, TP10 — temporal
        all_ch   = frontal + temporal

        while self.running:
            time.sleep(1.0)
            try:
                # Ambil 2 detik data (buffer), pakai 1 detik terakhir
                data = self.board.get_current_board_data(SAMPLE_RATE * 2)
                if data.shape[1] < SAMPLE_RATE:
                    continue  # belum cukup data

                alpha_list, beta_list, theta_list = [], [], []

                for ch in all_ch:
                    ch_data = data[ch, -SAMPLE_RATE:].copy()

                    # Detrend → hilangkan DC offset
                    DataFilter.detrend(ch_data, DetrendOperations.CONSTANT.value)

                    # PSD via Welch
                    psd = DataFilter.get_psd_welch(
                        ch_data,
                        SAMPLE_RATE,         # nfft = 256 → resolusi 1 Hz
                        SAMPLE_RATE // 2,    # overlap = 50%
                        SAMPLE_RATE,
                        WindowOperations.BLACKMAN_HARRIS.value
                    )

                    # Band power (µV²)
                    alpha_list.append(DataFilter.get_band_power(psd, 8.0,  13.0))
                    beta_list.append( DataFilter.get_band_power(psd, 13.0, 30.0))
                    theta_list.append(DataFilter.get_band_power(psd, 4.0,   8.0))

                # Rata-rata lintas channel
                alpha_raw = float(np.mean(alpha_list))
                beta_raw  = float(np.mean(beta_list))
                theta_raw = float(np.mean(theta_list))

                # Normalisasi rolling percentile per-user → 0–1
                alpha = self._normalize("alpha", alpha_raw)
                beta  = self._normalize("beta",  beta_raw)
                theta = self._normalize("theta", theta_raw)

                self.engine.set_eeg(alpha, beta, theta)

                # ── Kualitas sinyal per channel ──────────────────────────────
                ch_names = ["TP9", "AF7", "AF8", "TP10"]
                for name, ch in zip(ch_names, eeg_channels):
                    raw = data[ch, -SAMPLE_RATE:].copy()
                    DataFilter.detrend(raw, DetrendOperations.CONSTANT.value)
                    std = float(np.std(raw))
                    # µV thresholds: <3 flat/disconnected, >400 saturated/artifact
                    if std < 3.0:
                        q = 0.0
                    elif std > 400.0:
                        q = 0.0
                    elif std > 150.0:
                        q = 0.25
                    elif std < 8.0:
                        q = std / 8.0 * 0.6
                    else:
                        q = 1.0
                    self.channel_quality[name] = round(q, 2)

                # Print ke terminal untuk monitoring
                state_hint = (
                    "stress" if beta > 0.65 and theta > 0.55 else
                    "focus"  if beta > 0.60 and alpha < 0.35 else
                    "drowsy" if theta > 0.65 else
                    "relax"
                )
                print(f"  EEG  α={alpha:.2f}  β={beta:.2f}  θ={theta:.2f}  → {state_hint}")

            except Exception as e:
                if self.running:
                    print(f"⚠️  BrainFlow loop error: {e}")

    def _normalize(self, key: str, value: float) -> float:
        """
        Normalisasi adaptif berbasis rolling percentile (10–90 persen).
        Adaptif terhadap individual differences — tidak butuh kalibrasi manual.
        Butuh ~10 detik data sebelum hasil stabil.
        """
        h = self._history[key]
        h.append(value)
        if len(h) > self._HIST_LEN:
            h.pop(0)
        if len(h) < 5:
            return 0.5  # belum cukup data

        lo = float(np.percentile(h, 10))
        hi = float(np.percentile(h, 90))
        if hi <= lo:
            return 0.5

        return float(np.clip((value - lo) / (hi - lo), 0.0, 1.0))
