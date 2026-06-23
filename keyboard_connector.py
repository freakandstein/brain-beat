"""
Keyboard Connector
===================
Mapping mental command (wink_left, wink_right, eyebrow_raise, jaw_clench,
double_jaw) ke keystroke OS yang bisa dikonfigurasi, dikirim lewat pynput.

Mapping disimpan persist di keymap.json (key = command, value = key string
seperti "left", "a", "space", "cmd+shift+1").

Penggunaan:
    from keyboard_connector import KeyboardConnector
    kb = KeyboardConnector()
    kb.press("wink_left")          # tekan key yang di-map ke wink_left
    kb.set_mapping("wink_left", "left")
    kb.get_mapping()                # {"wink_left": "left", ...}
"""

import json
import os
import threading

try:
    from pynput.keyboard import Controller, Key
    PYNPUT_AVAILABLE = True
except ImportError:
    PYNPUT_AVAILABLE = False

KEYMAP_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "keymap.json")

# Nama tombol khusus (non-karakter) yang didukung — selain ini dikirim sebagai
# karakter literal (mis. "a", "1") ke pynput.
_SPECIAL_KEYS = {
    "left": Key.left, "right": Key.right, "up": Key.up, "down": Key.down,
    "space": Key.space, "enter": Key.enter, "tab": Key.tab, "esc": Key.esc,
    "backspace": Key.backspace, "delete": Key.delete,
    "shift": Key.shift, "ctrl": Key.ctrl, "alt": Key.alt, "cmd": Key.cmd,
    "f1": Key.f1, "f2": Key.f2, "f3": Key.f3, "f4": Key.f4,
    "f5": Key.f5, "f6": Key.f6, "f7": Key.f7, "f8": Key.f8,
    "f9": Key.f9, "f10": Key.f10, "f11": Key.f11, "f12": Key.f12,
} if PYNPUT_AVAILABLE else {}

DEFAULT_KEYMAP = {
    "wink_left":     "left",
    "wink_right":    "right",
    "eyebrow_raise": "up",
    "jaw_clench":    "down",
    "double_jaw":    "space",
}


def _parse_combo(combo: str) -> list:
    """'cmd+shift+1' → [Key.cmd, Key.shift, '1']"""
    parts = [p.strip().lower() for p in combo.split("+") if p.strip()]
    keys = []
    for p in parts:
        keys.append(_SPECIAL_KEYS.get(p, p))
    return keys


class KeyboardConnector:
    """Thread-safe, mengirim keystroke OS berdasarkan mapping yang bisa diatur."""

    def __init__(self, keymap_path: str = KEYMAP_PATH):
        self.keymap_path = keymap_path
        self._lock = threading.Lock()
        self._controller = Controller() if PYNPUT_AVAILABLE else None
        self.mapping = self._load()

    # ── public API ────────────────────────────────────────────────────────

    def get_mapping(self) -> dict:
        with self._lock:
            return dict(self.mapping)

    def set_mapping(self, command: str, key_combo: str) -> None:
        with self._lock:
            self.mapping[command] = key_combo
            self._save()

    def press(self, command: str) -> None:
        """Kirim keystroke yang di-map ke command. Non-blocking, gagal diam-diam."""
        with self._lock:
            combo = self.mapping.get(command)
        if not combo or self._controller is None:
            return
        threading.Thread(target=self._do_press, args=(combo, command), daemon=True).start()

    # ── internal ──────────────────────────────────────────────────────────

    def _do_press(self, combo: str, command: str) -> None:
        try:
            keys = _parse_combo(combo)
            for k in keys:
                self._controller.press(k)
            for k in reversed(keys):
                self._controller.release(k)
            print(f"⌨️  Keystroke '{combo}' sent (trigger: {command})")
        except Exception as e:
            print(f"⚠️  Keystroke send failed for '{command}' → '{combo}': {e}")

    def _load(self) -> dict:
        if os.path.exists(self.keymap_path):
            try:
                with open(self.keymap_path, "r") as f:
                    data = json.load(f)
                merged = dict(DEFAULT_KEYMAP)
                merged.update(data)
                return merged
            except Exception:
                pass
        return dict(DEFAULT_KEYMAP)

    def _save(self) -> None:
        try:
            with open(self.keymap_path, "w") as f:
                json.dump(self.mapping, f, indent=2)
        except Exception as e:
            print(f"⚠️  Gagal menyimpan keymap.json: {e}")
