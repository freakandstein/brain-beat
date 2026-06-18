"""
OBS Connector
=============
Menghubungkan mental command dari EEG ke OBS WebSocket v5.
Tiap command di-mapping ke nama scene OBS yang bisa dikonfigurasi.

Penggunaan:
    from obs_connector import OBSConnector
    obs = OBSConnector(password="xxx")
    obs.connect()
    obs.switch_scene("wink_left")  # → scene yang di-map ke "wink_left"
"""

import threading

try:
    import obsws_python as obsws
    OBS_AVAILABLE = True
except ImportError:
    OBS_AVAILABLE = False


class OBSConnector:
    """
    Thread-safe connector ke OBS WebSocket v5.
    Auto-reconnect saat scene switch gagal.
    """

    DEFAULT_SCENE_MAP = {
        "wink_left":     "Scene 1 (2 Views Without Top)",
        "wink_right":    "Scene 1 (2 Views Without Top)",
        "jaw_clench":    "Scene 2 (3 Views)",
        "eyebrow_raise": "Scene 3 (2 Views Without Front)",
    }

    def __init__(
        self,
        host: str = "localhost",
        port: int = 4455,
        password: str = "",
        scene_map: dict = None,
    ):
        self.host      = host
        self.port      = port
        self.password  = password
        self.scene_map = scene_map if scene_map is not None else dict(self.DEFAULT_SCENE_MAP)

        self._client = None
        self._lock   = threading.Lock()
        self._is_recording = False

    # ── public API ────────────────────────────────────────────────────────────

    def connect(self) -> bool:
        """Konek ke OBS WebSocket. Return True jika berhasil."""
        if not OBS_AVAILABLE:
            print("⚠️  obsws-python tidak terinstall. Jalankan: pip install obsws-python")
            return False
        try:
            cl = obsws.ReqClient(
                host=self.host, port=self.port,
                password=self.password, timeout=3,
            )
            with self._lock:
                self._client = cl
            print(f"✅  OBS WebSocket terhubung ({self.host}:{self.port})")
            return True
        except Exception as e:
            print(f"⚠️  OBS WebSocket tidak bisa konek: {e}")
            print("    Pastikan OBS buka & WebSocket Server aktif (Tools → WebSocket Server Settings)")
            return False

    def disconnect(self):
        with self._lock:
            self._client = None

    def switch_scene(self, command: str):
        """Switch scene OBS berdasarkan nama command. Non-blocking."""
        scene = self.scene_map.get(command)
        if not scene:
            return
        threading.Thread(target=self._do_switch, args=(command, scene), daemon=True).start()

    def toggle_record(self):
        """Toggle recording OBS: belum recording → start, sedang recording → stop. Non-blocking."""
        threading.Thread(target=self._do_toggle_record, daemon=True).start()

    def get_scene_list(self) -> list[str]:
        """Return daftar nama scene dari OBS (untuk debugging/konfigurasi)."""
        with self._lock:
            cl = self._client
        if cl is None:
            return []
        try:
            return [s["sceneName"] for s in cl.get_scene_list().scenes]
        except Exception:
            return []

    # ── internal ──────────────────────────────────────────────────────────────

    def _do_switch(self, command: str, scene: str):
        with self._lock:
            cl = self._client
        if cl is None:
            self.connect()
            with self._lock:
                cl = self._client
        if cl is None:
            return

        try:
            cl.set_current_program_scene(scene)
            print(f"🎬  OBS scene → {scene} (trigger: {command})")
        except Exception as e:
            print(f"⚠️  OBS scene switch gagal: {e} — mencoba reconnect...")
            with self._lock:
                self._client = None
            if self.connect():
                with self._lock:
                    cl2 = self._client
                try:
                    cl2.set_current_program_scene(scene)
                    print(f"🎬  OBS scene → {scene} (setelah reconnect)")
                except Exception as e2:
                    print(f"⚠️  OBS scene switch tetap gagal: {e2}")

    def _do_toggle_record(self):
        with self._lock:
            cl = self._client
        if cl is None:
            self.connect()
            with self._lock:
                cl = self._client
        if cl is None:
            return

        try:
            self._toggle_record_with_client(cl)
        except Exception as e:
            print(f"⚠️  OBS record toggle gagal: {e} — mencoba reconnect...")
            with self._lock:
                self._client = None
            if self.connect():
                with self._lock:
                    cl2 = self._client
                try:
                    self._toggle_record_with_client(cl2)
                except Exception as e2:
                    print(f"⚠️  OBS record toggle tetap gagal: {e2}")

    def _toggle_record_with_client(self, cl):
        # Tanya status asli ke OBS dulu (bukan asumsi dari _is_recording lokal) —
        # supaya tetap akurat kalau user juga start/stop manual dari OBS.
        status = cl.get_record_status()
        if status.output_active:
            cl.stop_record()
            self._is_recording = False
            print("⏹️  OBS recording STOPPED (trigger: double_jaw)")
        else:
            cl.start_record()
            self._is_recording = True
            print("⏺️  OBS recording STARTED (trigger: double_jaw)")
