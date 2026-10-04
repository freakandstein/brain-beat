"""
EEG Server
==========
Flask + SocketIO bridge antara EEG engine dan browser UI.

Penggunaan:
    python eeg_server.py
    Buka: http://localhost:8765
"""

import argparse
import sys
import os
import signal
import subprocess

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

PORT = 8765

import local_config
from obs_connector import OBSConnector
from keyboard_connector import KeyboardConnector
from mouse_connector import MouseConnector
from eeg_smart_comment import SmartCommentBot, VALID_COMMANDS as SMART_COMMENT_COMMANDS

obs_connector = OBSConnector(password=local_config.get("OBS_PASSWORD", ""))
keyboard_connector = KeyboardConnector()
mouse_connector = MouseConnector()
smart_comment = SmartCommentBot(topic=local_config.get("NTFY_TOPIC"))


def _parse_args():
    parser = argparse.ArgumentParser(description="EEG Server — Flask + SocketIO bridge")
    parser.add_argument(
        "--recalibrate-tilt", action="store_true",
        help="Hapus cache kalibrasi tilt_left/tilt_right tersimpan sebelum start, "
             "supaya kalibrasi penuh 3x diminta lagi saat connect berikutnya "
             "(dipakai kalau posisi headset berubah signifikan).",
    )
    return parser.parse_args()


def _kill_existing():
    """Matikan proses lain yang sedang pakai PORT yang sama."""
    try:
        result = subprocess.run(
            ["lsof", "-ti", f":{PORT}"],
            capture_output=True, text=True
        )
        pids = result.stdout.strip().split()
        current = os.getpid()
        for pid in pids:
            pid = int(pid)
            if pid != current:
                print(f"  ⚠️  Mematikan proses lama (PID {pid}) di port {PORT}...")
                os.kill(pid, signal.SIGTERM)
    except Exception:
        pass

from eeg_engine import MusicEngine, find_or_download_soundfont
from flask import Flask, render_template
from flask_socketio import SocketIO

try:
    from brainflow_connector import MuseConnector, BRAINFLOW_AVAILABLE, scan_muse_devices, BLEAK_AVAILABLE
except Exception:
    BRAINFLOW_AVAILABLE = False
    BLEAK_AVAILABLE = False
    MuseConnector = None
    def scan_muse_devices(timeout=5.0): return []

app = Flask(__name__)
app.config["SECRET_KEY"] = "eeg-engine-2026"
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

engine: MusicEngine = None
muse:   "MuseConnector" = None  # type: ignore

PRESETS = {
    "calm":  (0.80, 0.15, 0.20),   # alpha, beta, theta
    "tense": (0.20, 0.75, 0.30),
}


# ── routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/overlay/mental-command")
def overlay_mental_command():
    return render_template("overlay_mental_command.html")


@app.route("/overlay/constellation")
def overlay_constellation():
    return render_template("overlay_constellation.html")


# ── socket events ─────────────────────────────────────────────────────────────

@socketio.on("connect")
def on_connect():
    print("  → browser connected")


@socketio.on("disconnect")
def on_disconnect():
    print("  → browser disconnected")


@socketio.on("set_eeg")
def on_set_eeg(data):
    if engine:
        engine.set_eeg(
            float(data.get("alpha", 0.5)),
            float(data.get("beta",  0.3)),
            float(data.get("theta", 0.2)),
        )


@socketio.on("set_preset")
def on_set_preset(data):
    name = data.get("preset", "calm")
    if name in PRESETS and engine:
        a, b, t = PRESETS[name]
        engine.set_eeg(a, b, t)
        if not engine._running:
            engine.start()
        socketio.emit("preset_applied", {
            "preset": name, "alpha": a, "beta": b, "theta": t
        })


@socketio.on("get_mute")
def on_get_mute():
    if engine:
        socketio.emit("mute_state", {"muted": engine.is_muted()})


@socketio.on("set_mute")
def on_set_mute(data):
    if engine:
        muted = bool(data.get("muted"))
        engine.set_muted(muted)
        socketio.emit("mute_state", {"muted": muted})


@socketio.on("get_cursor_control")
def on_get_cursor_control():
    if muse:
        socketio.emit("cursor_control_state", {
            "enabled": muse.cursor_control_enabled,
            "phase":   muse.cursor_calib_phase,
        })


@socketio.on("set_cursor_control")
def on_set_cursor_control(data):
    if not muse:
        return
    enabled = bool(data.get("enabled"))
    muse.set_cursor_control(enabled)
    if enabled:
        mouse_connector.start()
    else:
        mouse_connector.stop()
    # Kirim phase SEKARANG JUGA (bukan cuma enabled) — client sebelumnya
    # menunggu broadcast state_update berikutnya (~100ms, atau lebih lambat
    # kalau kalibrasi di backend sudah lompat ke tahap berikutnya duluan)
    # untuk tahu instruksi tahap kalibrasi apa yang harus ditampilkan,
    # sehingga instruksi "tahan diam"/"tilt kanan" kadang tidak sempat
    # terlihat sama sekali saat kalibrasi berjalan cepat.
    socketio.emit("cursor_control_state", {
        "enabled": enabled,
        "phase":   muse.cursor_calib_phase,
    })


@socketio.on("muse_connect")
def on_muse_connect(data):
    global muse
    if not BRAINFLOW_AVAILABLE or muse is None:
        socketio.emit("muse_status", {
            "status": "error",
            "error":  "brainflow tidak terinstall. Jalankan: pip3 install brainflow"
        })
        return
    mac = (data.get("address") or "").strip()
    muse.connect(mac)


@socketio.on("muse_disconnect")
def on_muse_disconnect():
    global muse
    if muse:
        muse.disconnect()


@socketio.on("get_keymap")
def on_get_keymap():
    socketio.emit("keymap", keyboard_connector.get_mapping())


@socketio.on("set_keymap")
def on_set_keymap(data):
    command = (data.get("command") or "").strip()
    # "key" absent/None → request malformed, ignore. "" eksplisit → user
    # sengaja mengosongkan mapping ini (tombol clear di UI), bukan sekadar
    # belum diisi — KeyboardConnector.press() sudah no-op untuk combo kosong.
    if "key" not in data or not command:
        return
    key_combo = (data.get("key") or "").strip()
    keyboard_connector.set_mapping(command, key_combo)
    socketio.emit("keymap", keyboard_connector.get_mapping())


# ── OBS Keymap (command → OBS scene, terpisah dari keyboard keymap di atas) ──

@socketio.on("get_obs_keymap")
def on_get_obs_keymap():
    socketio.emit("obs_keymap", obs_connector.get_scene_mapping())


@socketio.on("set_obs_keymap")
def on_set_obs_keymap(data):
    command = (data.get("command") or "").strip()
    # Sama semantiknya dengan on_set_keymap di atas — "scene" absen berarti
    # request malformed, "" eksplisit berarti user sengaja clear mapping ini.
    if "scene" not in data or not command:
        return
    scene = (data.get("scene") or "").strip()
    obs_connector.set_scene_mapping(command, scene)
    socketio.emit("obs_keymap", obs_connector.get_scene_mapping())


@socketio.on("get_obs_scenes")
def on_get_obs_scenes():
    """Daftar nama scene OBS saat ini (untuk dropdown OBS Keymap UI) — list
    kosong kalau OBS belum connect, UI fallback ke text input manual."""
    socketio.emit("obs_scenes", obs_connector.get_scene_list())


# ── Smart Comment (mental command → komentar acak ke ntfy) ───────────────────

@socketio.on("get_smart_comment_config")
def on_get_smart_comment_config():
    socketio.emit("smart_comment_config", {
        "trigger_command": smart_comment.get_trigger_command(),
        "commands": sorted(SMART_COMMENT_COMMANDS),
    })


@socketio.on("set_smart_comment_config")
def on_set_smart_comment_config(data):
    # "command" absen berarti request malformed. "" / None eksplisit berarti
    # user sengaja menonaktifkan fitur ini (tidak ada trigger) — sama
    # semantiknya dengan clear pada Keymap/OBS Keymap.
    if "command" not in data:
        return
    command = (data.get("command") or "").strip()
    smart_comment.set_trigger_command(command)
    socketio.emit("smart_comment_config", {
        "trigger_command": smart_comment.get_trigger_command(),
        "commands": sorted(SMART_COMMENT_COMMANDS),
    })


@socketio.on("muse_scan")
def on_muse_scan():
    """Scan BLE devices dan emit hasilnya ke browser."""
    if not BLEAK_AVAILABLE:
        socketio.emit("muse_scan_result", {
            "devices": [],
            "error": "bleak tidak terinstall. Jalankan: pip3 install bleak"
        })
        return
    socketio.emit("muse_scan_result", {"devices": [], "scanning": True})
    devices = scan_muse_devices(timeout=5.0)
    socketio.emit("muse_scan_result", {
        "devices": [{"name": n, "address": a} for n, a in devices],
        "scanning": False
    })


# ── background updater ────────────────────────────────────────────────────────

# Histeresis untuk field "state" (calm/flow/tense) yang di-broadcast ke SEMUA
# overlay lewat state_update — spectrum_pos sendiri sudah EMA-smoothed tapi
# masih bisa goyang persis di sekitar batas zona (0.35/0.65), jadi tanpa ini
# label bisa lompat antar-state tiap tick (100ms), terlihat "patah" di
# overlay manapun yang menampilkannya. State kandidat baru harus konsisten
# selama _STATE_HYSTERESIS_TICKS tick berturut-turut sebelum benar-benar
# di-switch; state saat ini dipertahankan sampai itu terpenuhi.
_STATE_HYSTERESIS_TICKS = 5   # ~500ms @ 100ms/tick — cukup meredam jitter
                               # di sekitar ambang tanpa membuat transisi
                               # terasa lambat/lag.
_stable_state = "calm"
_pending_state = None
_pending_state_count = 0


def _debounced_mental_state(engine) -> str:
    global _stable_state, _pending_state, _pending_state_count
    # spectrum_pos is already adaptive-threshold-based and EMA-smoothed
    # (see get_spectrum_position in eeg_engine.py) — using it here instead
    # of eeg.mental_state()'s hardcoded threshold=-0.05 keeps state_update
    # in sync with the same calm/flow/tense zones the drum engine already
    # uses, and actually emits "flow" instead of only ever calm/tense.
    sp = engine.get_spectrum_position()
    if sp > 0.65:
        candidate = "tense"
    elif sp >= 0.35:
        candidate = "flow"
    else:
        candidate = "calm"
    if candidate == _stable_state:
        _pending_state = None
        _pending_state_count = 0
        return _stable_state
    if candidate == _pending_state:
        _pending_state_count += 1
    else:
        _pending_state = candidate
        _pending_state_count = 1
    if _pending_state_count >= _STATE_HYSTERESIS_TICKS:
        _stable_state = candidate
        _pending_state = None
        _pending_state_count = 0
    return _stable_state


def _background_updater():
    """Push engine state ke semua browser setiap 100ms."""
    while True:
        try:
            if engine:
                with engine._lock:
                    eeg   = engine.eeg
                    state = _debounced_mental_state(engine)
                    payload = {
                        "state": state,
                        "bpm":   round(engine._bpm, 1),
                        "tense_level": round(engine._tense_level, 3),
                        "arousal":     round(engine.get_arousal(), 4),
                        "threshold":   round(engine.get_threshold(), 4),
                        "warming_up":  engine.is_warming_up(),
                        "confidence":  round(engine.get_confidence(), 3),
                        "consistency": round(engine.get_consistency(), 3),
                        "flow_score":  engine.get_flow_score(),
                        "spectrum_pos": engine.get_spectrum_position(),
                        "eeg_active":  engine._running,
                        "alpha": round(eeg.alpha, 3),
                        "beta":  round(eeg.beta,  3),
                        "theta": round(eeg.theta, 3),
                        "tbr":   round(eeg.tbr,   3),
                        "alpha_raw": round(muse.raw_bands["alpha"], 2) if muse else None,
                        "beta_raw":  round(muse.raw_bands["beta"],  2) if muse else None,
                        "theta_raw": round(muse.raw_bands["theta"], 2) if muse else None,
                        "alpha_hz": muse.peak_hz["alpha"] if muse else None,
                        "beta_hz":  muse.peak_hz["beta"]  if muse else None,
                        "theta_hz": muse.peak_hz["theta"] if muse else None,
                        "muse":  muse.status if muse else "unavailable",
                        "heart_rate": muse.heart_rate if muse else None,
                        "battery_percent": muse.battery_percent if muse else None,
                        "channel_quality": muse.channel_quality if muse else None,
                        "cursor_control": muse.cursor_control_enabled if muse else False,
                        "cursor_baseline_ready": muse._cursor_baseline_ready if muse else False,
                        "cursor_calib_phase": muse.cursor_calib_phase if muse else "idle",
                        "tilt_calib_phase": muse.tilt_calib_phase if muse else "idle",
                        "tilt_calib_progress": muse.tilt_calib_progress if muse else "",
                        "cursor_vx": round(muse.cursor_velocity_x, 1) if muse else 0.0,
                        "cursor_vy": round(muse.cursor_velocity_y, 1) if muse else 0.0,
                    }
                socketio.emit("state_update", payload)
        except Exception as e:
            print(f"⚠️  _background_updater error: {e}")
        socketio.sleep(0.1)


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    global engine, muse

    args = _parse_args()
    _kill_existing()

    if args.recalibrate_tilt and BRAINFLOW_AVAILABLE and MuseConnector:
        cache_path = MuseConnector._TILT_CALIB_CACHE_PATH
        try:
            os.remove(cache_path)
            print("🔄  Cache kalibrasi tilt dihapus — kalibrasi penuh 3x akan diminta lagi saat connect.")
        except FileNotFoundError:
            print("ℹ️   Tidak ada cache kalibrasi tilt tersimpan — kalibrasi akan berjalan normal.")

    print("🥁  Brainwave Monitor — Web UI")
    obs_connector.connect()
    print("    Mencari soundfont...")
    sf_path = find_or_download_soundfont()

    print("    Inisialisasi FluidSynth...")
    engine = MusicEngine(sf_path)
    # Engine TIDAK langsung dimulai — musik hanya diputar saat Muse 2 terhubung

    # Inisialisasi Muse connector (jika brainflow terinstall)
    if BRAINFLOW_AVAILABLE and MuseConnector:
        def _muse_status_cb(status: str, error: str):
            socketio.emit("muse_status", {"status": status, "error": error})
            if status == "connected" and engine:
                engine.start()
            elif status in ("disconnected", "error") and engine:
                engine.stop()
                engine.set_eeg(alpha=0.70, beta=0.20, theta=0.20, tbr=0.60)
                # Safety: Muse putus → paksa matikan cursor control, jangan
                # biarkan cursor OS terus bergerak dari state basi.
                mouse_connector.stop()
                socketio.emit("cursor_control_state", {"enabled": False})

        def _eyebrow_cb():
            print("⚡  Eyebrow raise detected — triggering overlay")
            socketio.emit("eyebrow_raise", {})
            obs_connector.switch_scene("eyebrow_raise")
            keyboard_connector.press("eyebrow_raise")
            smart_comment.maybe_send("eyebrow_raise")

        def _jaw_clench_cb():
            if muse.cursor_control_enabled:
                # Cursor Control Mode aktif → jaw clench = left-click, BUKAN
                # OBS scene switch/keystroke. Mutual exclusion: saat mode ini
                # ON, fungsi jaw clench yang lama sengaja tidak dijalankan.
                print("🖱️  Jaw clench → left-click (cursor control mode)")
                socketio.emit("cursor_left_click", {})
                mouse_connector.click_left()
            else:
                print("🦷  Jaw clench detected — triggering overlay")
                socketio.emit("jaw_clench", {})
                obs_connector.switch_scene("jaw_clench")
                keyboard_connector.press("jaw_clench")
                smart_comment.maybe_send("jaw_clench")

        def _double_jaw_cb():
            print("🦷🦷  Double jaw detected — triggering overlay")
            socketio.emit("double_jaw", {})
            obs_connector.switch_scene("double_jaw")
            keyboard_connector.press("double_jaw")
            smart_comment.maybe_send("double_jaw")

        def _tilt_left_cb():
            # Defense-in-depth: detector di brainflow_connector.py sudah
            # skip total saat cursor mode ON (lihat _imu_loop), guard ini
            # cuma jaring kedua supaya command tidak pernah lolos ke OBS/
            # keyboard walau ada race saat toggle di tengah gerakan.
            if muse.cursor_control_enabled:
                return
            print("↩️  Tilt left detected — triggering overlay")
            socketio.emit("tilt_left", {})
            obs_connector.switch_scene("tilt_left")
            keyboard_connector.press("tilt_left")
            smart_comment.maybe_send("tilt_left")

        def _tilt_right_cb():
            if muse.cursor_control_enabled:
                return
            print("↩️  Tilt right detected — triggering overlay")
            socketio.emit("tilt_right", {})
            obs_connector.switch_scene("tilt_right")
            keyboard_connector.press("tilt_right")
            smart_comment.maybe_send("tilt_right")

        def _tilt_up_cb():
            # Sama arsitekturnya dengan _tilt_left_cb/_tilt_right_cb — axis
            # pitch (mengangguk), bukan roll. Belum ada entry di keymap.json
            # (lihat keyboard_connector.py) — press() akan no-op diam-diam
            # sampai dipetakan, sama seperti eyebrow saat ini.
            if muse.cursor_control_enabled:
                return
            print("↕️  Tilt up detected — triggering overlay")
            socketio.emit("tilt_up", {})
            obs_connector.switch_scene("tilt_up")
            keyboard_connector.press("tilt_up")
            smart_comment.maybe_send("tilt_up")

        def _tilt_down_cb():
            if muse.cursor_control_enabled:
                return
            print("↕️  Tilt down detected — triggering overlay")
            socketio.emit("tilt_down", {})
            obs_connector.switch_scene("tilt_down")
            keyboard_connector.press("tilt_down")
            smart_comment.maybe_send("tilt_down")

        muse = MuseConnector(engine, on_status=_muse_status_cb)
        muse.on_eyebrow_raise = _eyebrow_cb
        # Jaw single/double lewat composer (perlu window untuk membedakan
        # single vs double clench).
        muse.composer.on_jaw_clench      = _jaw_clench_cb
        muse.composer.on_double_jaw      = _double_jaw_cb
        # tilt_left/tilt_right diaktifkan kembali setelah redesign kalibrasi
        # multi-sample (3 percobaan dirata-ratakan, lihat _run_tilt_calibration).
        muse.on_tilt_left                = _tilt_left_cb
        muse.on_tilt_right               = _tilt_right_cb
        muse.on_tilt_up                  = _tilt_up_cb
        muse.on_tilt_down                = _tilt_down_cb
        muse.on_cursor_velocity          = mouse_connector.set_velocity
        # Catatan: eyes_closed_relax tidak lagi dipakai playground (diganti
        # eyebrow_raise — gesture cepat & deliberate, lebih konsisten dgn
        # double-blink & jaw clench dibanding "merem-relaks 2 detik" yg
        # bersifat sustained-passive). Detector tetap ada di MuseConnector
        # (sudah teruji), tapi callback-nya sengaja tidak di-wire di sini.
        print("✅  BrainFlow siap. Tekan 'Hubungkan Muse 2' di browser.")
    else:
        print("⚠️   brainflow tidak terinstall — koneksi Muse 2 tidak tersedia.")
        print("     Jalankan: pip3 install brainflow")

    socketio.start_background_task(_background_updater)

    print()
    print("╔════════════════════════════════╗")
    print(f"║  🌐  http://localhost:{PORT}      ║")
    print("║  Buka URL di browser           ║")
    print("╚════════════════════════════════╝")
    print()
    print("  Ctrl+C untuk berhenti")
    print()

    try:
        socketio.run(app, host="127.0.0.1", port=PORT,
                     debug=False, use_reloader=False)
    finally:
        engine.stop()
        print("\n✅  Selesai.")


if __name__ == "__main__":
    main()
