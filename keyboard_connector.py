"""
Keyboard Connector
===================
Mapping mental command (wink_left, wink_right, eyebrow_raise, jaw_clench,
double_jaw) ke keystroke OS yang bisa dikonfigurasi.

Dua metode pengiriman keystroke, dipilih otomatis per-command lewat
_HID_TAP_COMMANDS:
  - pynput (default) — cukup untuk kebanyakan aplikasi.
  - Quartz/CGEventPost dengan kCGHIDEventTap (macOS only) — meniru event
    lebih dekat ke hardware asli. DITEMUKAN LEWAT TESTING NYATA: TikTok
    LIVE Studio (dan kemungkinan aplikasi capture/live-streaming sejenis
    lain) mengabaikan synthetic keyboard event dari pynput sama sekali
    (tidak ada error di kirimnya, event terkirim ke OS, tapi aplikasi
    tidak bereaksi) — kemungkinan aplikasi ini secara sengaja hanya
    mendengarkan HID event tap untuk mencegah automation/cheating pada
    hotkey mereka. CGEventPost dengan kCGHIDEventTap berhasil di test
    manual yang sama.

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

try:
    import Quartz
    QUARTZ_AVAILABLE = True
except ImportError:
    QUARTZ_AVAILABLE = False

KEYMAP_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "keymap.json")

# Command yang HARUS dikirim lewat Quartz/kCGHIDEventTap, bukan pynput —
# lihat penjelasan panjang di docstring modul. tilt_right → TikTok LIVE
# Studio hotkey (Cmd+1, sound effect clap), dikonfirmasi lewat testing
# manual: pynput terkirim tanpa error tapi TIDAK direspon aplikasi,
# sedangkan tombol fisik & Quartz HID tap sama-sama berhasil.
_HID_TAP_COMMANDS = {"tilt_right"}

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

# Virtual keycode macOS (US ANSI layout) — dipakai HANYA oleh jalur Quartz
# HID tap (_send_via_hid_tap). Referensi standar Apple HIToolbox/Events.h.
_MACOS_KEYCODES = {
    "a": 0x00, "b": 0x0B, "c": 0x08, "d": 0x02, "e": 0x0E, "f": 0x03,
    "g": 0x05, "h": 0x04, "i": 0x22, "j": 0x26, "k": 0x28, "l": 0x25,
    "m": 0x2E, "n": 0x2D, "o": 0x1F, "p": 0x23, "q": 0x0C, "r": 0x0F,
    "s": 0x01, "t": 0x11, "u": 0x20, "v": 0x09, "w": 0x0D, "x": 0x07,
    "y": 0x10, "z": 0x06,
    "0": 0x1D, "1": 0x12, "2": 0x13, "3": 0x14, "4": 0x15, "5": 0x17,
    "6": 0x16, "7": 0x1A, "8": 0x1C, "9": 0x19,
    "space": 0x31, "enter": 0x24, "tab": 0x30, "esc": 0x35,
    "backspace": 0x33, "delete": 0x75,
    "left": 0x7B, "right": 0x7C, "up": 0x7E, "down": 0x7D,
    "shift": 0x38, "ctrl": 0x3B, "alt": 0x3A, "cmd": 0x37,
    "f1": 0x7A, "f2": 0x78, "f3": 0x63, "f4": 0x76,
    "f5": 0x60, "f6": 0x61, "f7": 0x62, "f8": 0x64,
    "f9": 0x65, "f10": 0x6D, "f11": 0x67, "f12": 0x6F,
}
_MACOS_MODIFIER_FLAGS = {
    "cmd":   Quartz.kCGEventFlagMaskCommand,
    "shift": Quartz.kCGEventFlagMaskShift,
    "ctrl":  Quartz.kCGEventFlagMaskControl,
    "alt":   Quartz.kCGEventFlagMaskAlternate,
} if QUARTZ_AVAILABLE else {}

DEFAULT_KEYMAP = {
    "wink_left":     "left",
    "wink_right":    "right",
    "eyebrow_raise": "up",
    "jaw_clench":    "down",
    "double_jaw":    "space",
    # tilt_right → Command+1, hotkey sound effect "clap" di TikTok Live
    # Studio (lihat eeg_server.py _tilt_right_cb).
    "tilt_right":    "cmd+1",
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
        if not combo:
            return
        use_hid_tap = command in _HID_TAP_COMMANDS and QUARTZ_AVAILABLE
        if not use_hid_tap and self._controller is None:
            return
        threading.Thread(
            target=self._do_press, args=(combo, command, use_hid_tap), daemon=True
        ).start()

    # ── internal ──────────────────────────────────────────────────────────

    def _do_press(self, combo: str, command: str, use_hid_tap: bool = False) -> None:
        try:
            if use_hid_tap:
                self._send_via_hid_tap(combo)
                print(f"⌨️  Keystroke '{combo}' sent via HID tap (trigger: {command})")
            else:
                keys = _parse_combo(combo)
                for k in keys:
                    self._controller.press(k)
                for k in reversed(keys):
                    self._controller.release(k)
                print(f"⌨️  Keystroke '{combo}' sent (trigger: {command})")
        except Exception as e:
            print(f"⚠️  Keystroke send failed for '{command}' → '{combo}': {e}")

    def _send_via_hid_tap(self, combo: str) -> None:
        """Kirim keystroke lewat Quartz CGEventPost + kCGHIDEventTap —
        posisinya lebih dekat ke hardware event asli dibanding pynput
        (yang post di session event tap). Lihat docstring modul untuk
        alasan kenapa ini perlu ada: TikTok LIVE Studio mengabaikan
        synthetic event dari pynput sama sekali (tanpa error), tapi
        merespon jalur ini seperti tombol fisik."""
        parts = [p.strip().lower() for p in combo.split("+") if p.strip()]
        modifier_names = [p for p in parts if p in _MACOS_MODIFIER_FLAGS]
        main_keys = [p for p in parts if p not in _MACOS_MODIFIER_FLAGS]
        if not main_keys:
            raise ValueError(f"Kombinasi '{combo}' tidak punya key utama (cuma modifier)")

        flags = 0
        for m in modifier_names:
            flags |= _MACOS_MODIFIER_FLAGS[m]

        for key_name in main_keys:
            keycode = _MACOS_KEYCODES.get(key_name)
            if keycode is None:
                raise ValueError(f"Key '{key_name}' tidak dikenal di _MACOS_KEYCODES")

            down = Quartz.CGEventCreateKeyboardEvent(None, keycode, True)
            Quartz.CGEventSetFlags(down, flags)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, down)

            up = Quartz.CGEventCreateKeyboardEvent(None, keycode, False)
            Quartz.CGEventSetFlags(up, flags)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, up)

    def _load(self) -> dict:
        if os.path.exists(self.keymap_path):
            try:
                with open(self.keymap_path, "r") as f:
                    data = json.load(f)
                merged = dict(DEFAULT_KEYMAP)
                # Cuma timpa default hardcode kalau file punya nilai NON-
                # KOSONG — string kosong di keymap.json berarti "belum
                # di-set lewat UI", bukan "sengaja kosongkan command ini".
                # Tanpa guard ini, entry kosong bawaan file (mis. dari
                # template awal) diam-diam menimpa DEFAULT_KEYMAP dan
                # command jadi tidak pernah kirim keystroke apapun.
                merged.update({k: v for k, v in data.items() if v})
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
