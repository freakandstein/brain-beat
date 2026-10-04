"""
Smart Comment Bot
==================
Saat mental command tertentu (bisa diatur lewat UI — lihat get_trigger_
command/set_trigger_command) terdeteksi, kirim satu komentar acak dari
kolam template ke ntfy. Streamer membaca notifikasi itu di device kedua
(akun TikTok kedua yang sedang nonton live-nya sendiri) dan menempelkannya
secara manual ke kolom komentar TikTok Live — bot ini TIDAK memposting apa
pun secara otomatis ke TikTok.

Kolam komentar: array string sederhana di comment_templates/*.json. Persis
SATU file di folder itu boleh berakhiran "_enable.json" — itu yang dipakai.
Ganti game/kolam aktif = rename file lama ke "_disable.json", file baru ke
"_enable.json", lalu panggil reload_templates() (atau restart server).

Trigger command disimpan persist di smart_comment_config.json, sama
arsitekturnya dengan KeyboardConnector/OBSConnector (lihat keyboard_
connector.py, obs_connector.py) — file di disk adalah sumber kebenaran
penuh begitu pernah ditulis lewat UI.

Penggunaan:
    from eeg_smart_comment import SmartCommentBot
    bot = SmartCommentBot(topic="BosTioGaming")
    bot.maybe_send(command)   # no-op kalau command bukan trigger aktif
"""

import glob
import json
import os
import random
import threading
import warnings

# Redam NotOpenSSLWarning — cosmetic, muncul karena Python bawaan macOS
# (baik /usr/bin/python3 maupun .venv) dikompilasi pakai LibreSSL, bukan
# OpenSSL, yang tidak disukai urllib3 v2. Tidak menghalangi fungsi apa pun
# (request tetap berhasil), cuma noise di terminal. Filter berdasarkan
# MESSAGE (bukan category=NotOpenSSLWarning) karena warning-nya dilempar
# PERSIS SAAT urllib3 di-import — mengimpor class-nya dulu untuk filter
# by-category sudah terlambat, warning sudah keburu tercetak.
warnings.filterwarnings("ignore", message=".*urllib3 v2 only supports OpenSSL.*")

import requests

TEMPLATES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "comment_templates")
CONFIG_PATH   = os.path.join(os.path.dirname(os.path.abspath(__file__)), "smart_comment_config.json")

# Command yang bisa dipilih sebagai trigger — sama persis dengan 7 command
# yang punya slot di KeyboardConnector/OBSConnector, supaya dropdown UI-nya
# konsisten dengan panel Keymap/OBS Keymap yang sudah ada.
VALID_COMMANDS = {
    "eyebrow_raise", "jaw_clench", "double_jaw",
    "tilt_left", "tilt_right", "tilt_up", "tilt_down",
}

DEFAULT_CONFIG = {
    "trigger_command": None,   # None = fitur ini nonaktif, belum ada command dipilih
}


class SmartCommentBot:
    """Thread-safe. Kegagalan ntfy/template tidak boleh melempar exception
    ke pemanggil (callback mental command di eeg_server.py) — sama prinsip
    dengan OBSConnector/KeyboardConnector, satu fitur gagal tidak boleh
    menjatuhkan pipeline EEG/OBS/keyboard lain."""

    def __init__(
        self,
        topic: str,
        templates_dir: str = TEMPLATES_DIR,
        config_path: str = CONFIG_PATH,
    ):
        self.topic         = topic
        self.templates_dir = templates_dir
        self.config_path   = config_path

        self._lock     = threading.Lock()
        self._comments = []          # kolam komentar aktif (dikocok, dihabiskan)
        self._template_name = None   # nama file *_enable.json yang sedang dipakai (untuk log/debug)
        self.config     = self._load_config()

        self.reload_templates()

    # ── public API ────────────────────────────────────────────────────────

    def get_trigger_command(self) -> str:
        with self._lock:
            return self.config.get("trigger_command")

    def set_trigger_command(self, command: str) -> None:
        """command="" atau None → nonaktifkan fitur (tidak ada trigger)."""
        if command and command not in VALID_COMMANDS:
            print(f"⚠️  set_trigger_command: '{command}' bukan command yang dikenal, diabaikan")
            return
        with self._lock:
            self.config["trigger_command"] = command or None
            self._save_config()

    def maybe_send(self, command: str) -> None:
        """Dipanggil dari SETIAP callback mental command di eeg_server.py.
        No-op kalau command ini bukan trigger yang sedang aktif."""
        if command != self.get_trigger_command():
            return
        self.send_random_comment()

    def send_random_comment(self) -> None:
        """Kirim satu komentar acak dari kolam ke ntfy. Non-blocking."""
        threading.Thread(target=self._do_send, daemon=True).start()

    def reload_templates(self) -> None:
        """Scan comment_templates/ untuk file *_enable.json (harus persis
        satu), muat isinya sebagai kolam komentar baru. Kolam lama (posisi
        acak yang sudah terpakai) dibuang — kolam baru mulai fresh."""
        with self._lock:
            self._comments, self._template_name = self._load_active_template()

    # ── internal ──────────────────────────────────────────────────────────

    def _load_active_template(self):
        pattern = os.path.join(self.templates_dir, "*_enable.json")
        matches = sorted(glob.glob(pattern))
        if len(matches) == 0:
            print(f"⚠️  Smart Comment: tidak ada file *_enable.json di {self.templates_dir} — fitur tidak akan mengirim apa pun sampai ada satu file diaktifkan (rename ke _enable.json).")
            return [], None
        if len(matches) > 1:
            names = ", ".join(os.path.basename(m) for m in matches)
            print(f"⚠️  Smart Comment: ada {len(matches)} file *_enable.json ({names}) — harus persis SATU. Rename yang tidak dipakai ke _disable.json. Fitur tidak akan mengirim apa pun sampai ambigu ini diselesaikan.")
            return [], None

        path = matches[0]
        try:
            with open(path, "r") as f:
                data = json.load(f)
            if not isinstance(data, list) or not all(isinstance(c, str) for c in data):
                print(f"⚠️  Smart Comment: {os.path.basename(path)} harus berisi array string, ditemukan format lain — diabaikan.")
                return [], None
            comments = [c for c in data if c.strip()]
            if not comments:
                print(f"⚠️  Smart Comment: {os.path.basename(path)} kosong — tidak ada komentar untuk dikirim.")
                return [], None
            random.shuffle(comments)
            print(f"✅  Smart Comment: {len(comments)} komentar dimuat dari {os.path.basename(path)}")
            return comments, os.path.basename(path)
        except Exception as e:
            print(f"⚠️  Smart Comment: gagal baca {os.path.basename(path)}: {e}")
            return [], None

    def _next_comment(self):
        """Ambil komentar berikutnya, acak tanpa ulang sampai kolam habis
        lalu dikocok ulang. Return None kalau tidak ada kolam sama sekali."""
        with self._lock:
            if not self._comments:
                # Kolam habis ATAU belum pernah termuat — coba reload sekali
                # (menangani kasus file *_enable.json baru muncul setelah
                # start tanpa perlu restart server).
                self._comments, self._template_name = self._load_active_template()
                if not self._comments:
                    return None
            return self._comments.pop()

    def _do_send(self) -> None:
        comment = self._next_comment()
        if comment is None:
            return
        try:
            resp = requests.post(
                f"https://ntfy.sh/{self.topic}",
                data=comment.encode("utf-8"),
                timeout=5,
            )
            resp.raise_for_status()
            print(f"💬  Smart Comment terkirim ke ntfy/{self.topic}: {comment!r}")
        except Exception as e:
            print(f"⚠️  Smart Comment: gagal kirim ke ntfy: {e}")

    def _load_config(self) -> dict:
        """Sama arsitekturnya dengan KeyboardConnector._load /
        OBSConnector._load — file di disk (kalau ada) adalah sumber
        kebenaran penuh, karena hanya ditulis lewat set_trigger_command()
        (yaitu saat user mengatur lewat UI)."""
        if os.path.exists(self.config_path):
            try:
                with open(self.config_path, "r") as f:
                    data = json.load(f)
                merged = dict(DEFAULT_CONFIG)
                merged.update(data)
                return merged
            except Exception:
                pass
        return dict(DEFAULT_CONFIG)

    def _save_config(self) -> None:
        try:
            with open(self.config_path, "w") as f:
                json.dump(self.config, f, indent=2)
        except OSError as e:
            print(f"⚠️  Gagal menyimpan smart_comment_config.json: {e}")
