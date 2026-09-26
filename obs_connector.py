"""
OBS Connector
=============
Menghubungkan mental command dari EEG ke OBS WebSocket v5.
Tiap command di-mapping ke nama scene OBS yang bisa dikonfigurasi lewat UI
(OBS Keymap panel — lihat set_scene_mapping/get_scene_mapping) dan disimpan
persist di obs_keymap.json, sama arsitekturnya dengan KeyboardConnector's
keymap.json (lihat keyboard_connector.py).

Penggunaan:
    from obs_connector import OBSConnector
    obs = OBSConnector(password="xxx")
    obs.connect()
    obs.switch_scene("jaw_clench")  # → scene yang di-map ke "jaw_clench"
"""

import json
import os
import threading

try:
    import obsws_python as obsws
    OBS_AVAILABLE = True
except ImportError:
    OBS_AVAILABLE = False

OBS_KEYMAP_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "obs_keymap.json")


class OBSConnector:
    """
    Thread-safe connector ke OBS WebSocket v5.
    Auto-reconnect saat scene switch gagal.
    """

    # Command yang punya slot OBS Keymap — sama persis dengan command yang
    # punya slot di KeyboardConnector (lihat keyboard_connector.py
    # DEFAULT_KEYMAP) supaya kedua panel UI (Keymap & OBS Keymap) konsisten.
    DEFAULT_SCENE_MAP = {
        "eyebrow_raise": None,
        "jaw_clench":    None,
        "double_jaw":    None,
        "tilt_left":     None,
        "tilt_right":    None,
        "tilt_up":       None,
        "tilt_down":     None,
    }

    def __init__(
        self,
        host: str = "localhost",
        port: int = 4455,
        password: str = "",
        scene_map: dict = None,
        keymap_path: str = OBS_KEYMAP_PATH,
    ):
        self.host      = host
        self.port      = port
        self.password  = password
        self.keymap_path = keymap_path
        self.scene_map = scene_map if scene_map is not None else self._load()

        self._client = None
        self._lock   = threading.Lock()

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

    def get_scene_mapping(self) -> dict:
        """Return copy dari scene_map saat ini (untuk OBS Keymap UI)."""
        with self._lock:
            return dict(self.scene_map)

    def set_scene_mapping(self, command: str, scene: str) -> None:
        """Set/clear mapping command → scene, lalu persist ke obs_keymap.json.
        scene="" eksplisit → command ini sengaja di-clear (switch_scene()
        jadi no-op utk command itu), sama semantiknya dengan
        KeyboardConnector.set_mapping (lihat keyboard_connector.py)."""
        with self._lock:
            self.scene_map[command] = scene
            self._save()

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

    def _load(self) -> dict:
        """Sama arsitekturnya dengan KeyboardConnector._load (lihat
        keyboard_connector.py) — file yang ada di disk berarti sudah pernah
        ditulis _save() (dipanggil hanya dari set_scene_mapping(), yaitu
        saat user menyimpan lewat UI), jadi jadi sumber kebenaran penuh
        apapun isinya, termasuk saat SEMUA entry kosong sekaligus. Command
        yang belum pernah dikenal sama sekali (tidak ada di file) tetap
        fallback ke DEFAULT_SCENE_MAP per-key."""
        if os.path.exists(self.keymap_path):
            try:
                with open(self.keymap_path, "r") as f:
                    data = json.load(f)
                merged = dict(self.DEFAULT_SCENE_MAP)
                merged.update(data)
                return merged
            except Exception:
                pass
        return dict(self.DEFAULT_SCENE_MAP)

    def _save(self) -> None:
        try:
            with open(self.keymap_path, "w") as f:
                json.dump(self.scene_map, f, indent=2)
        except OSError as e:
            print(f"⚠️  Gagal menyimpan obs_keymap.json: {e}")

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
