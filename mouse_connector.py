"""
Mouse Connector
================
Cursor control system-wide via pynput.mouse, didorong oleh velocity (px/detik)
yang di-update dari IMU tilt (lihat MuseConnector._update_cursor_control).

Loop gerakan berjalan di ~60Hz terpisah dari tick IMU (~6.7Hz) supaya gerakan
cursor terlihat halus meski sample tilt datang lebih jarang — nilai velocity
saat ini di-apply sebagai increment kecil tiap ~16ms, bukan lompatan besar
tiap ~150ms.

Penggunaan:
    from mouse_connector import MouseConnector
    mc = MouseConnector()
    mc.start()
    mc.set_velocity(120.0, -40.0)   # px/detik (vx, vy)
    mc.click_left()
    mc.stop()
"""

import threading
import time

try:
    from pynput.mouse import Controller, Button
    PYNPUT_AVAILABLE = True
except ImportError:
    PYNPUT_AVAILABLE = False

TICK_HZ = 60.0
TICK_DT = 1.0 / TICK_HZ


class MouseConnector:
    """Thread-safe, menggerakkan cursor OS berdasarkan velocity yang di-set eksternal."""

    def __init__(self):
        self._controller = Controller() if PYNPUT_AVAILABLE else None
        self._lock = threading.Lock()
        self._vx = 0.0
        self._vy = 0.0
        self._running = False
        self._thread = None

    # ── public API ────────────────────────────────────────────────────────

    def start(self) -> None:
        if self._running or self._controller is None:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Hentikan movement loop dan nolkan velocity (safety: cursor berhenti total)."""
        self._running = False
        with self._lock:
            self._vx = 0.0
            self._vy = 0.0

    def set_velocity(self, vx: float, vy: float) -> None:
        """vx/vy dalam px/detik. Dipanggil dari MuseConnector tiap IMU tick."""
        with self._lock:
            self._vx = vx
            self._vy = vy

    def click_left(self) -> None:
        """Non-blocking left click — dipanggil dari jaw clench saat cursor mode ON."""
        if self._controller is None:
            return
        threading.Thread(target=self._do_click, daemon=True).start()

    # ── internal ──────────────────────────────────────────────────────────

    def _run(self) -> None:
        while self._running:
            t0 = time.time()
            with self._lock:
                vx, vy = self._vx, self._vy
            if vx != 0.0 or vy != 0.0:
                try:
                    self._controller.move(vx * TICK_DT, vy * TICK_DT)
                except Exception as e:
                    print(f"⚠️  Mouse move failed: {e}")
            elapsed = time.time() - t0
            time.sleep(max(0.0, TICK_DT - elapsed))

    def _do_click(self) -> None:
        try:
            self._controller.click(Button.left)
            print("🖱️  Left click sent (cursor control mode)")
        except Exception as e:
            print(f"⚠️  Left click failed: {e}")
