"""
EEG Server
==========
Flask + SocketIO bridge antara EEG engine dan browser UI.

Penggunaan:
    python eeg_server.py
    Buka: http://localhost:8765
"""

import sys
import os
import signal
import subprocess

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

PORT = 8765

from obs_connector import OBSConnector

obs_connector = OBSConnector(password="OmU3IAuGtlNcUPUY")


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


@app.route("/overlay/brainwave-visual")
def overlay_brainwave_visual():
    return render_template("overlay_brainwave_visual.html")


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

def _background_updater():
    """Push engine state ke semua browser setiap 100ms."""
    while True:
        try:
            if engine:
                with engine._lock:
                    eeg   = engine.eeg
                    state = eeg.mental_state()
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
                        "channel_quality": muse.channel_quality if muse else None,
                    }
                socketio.emit("state_update", payload)
        except Exception as e:
            print(f"⚠️  _background_updater error: {e}")
        socketio.sleep(0.1)


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    global engine, muse

    _kill_existing()

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

        def _eyebrow_cb():
            print("⚡  Eyebrow raise detected — triggering overlay")
            socketio.emit("eyebrow_raise", {})
            obs_connector.switch_scene("eyebrow_raise")

        def _wink_cb():
            print("😉  Wink detected — triggering overlay")
            socketio.emit("wink", {})
            obs_connector.switch_scene("wink")

        def _jaw_clench_cb():
            print("🦷  Jaw clench detected — triggering overlay")
            socketio.emit("jaw_clench", {})
            obs_connector.switch_scene("jaw_clench")

        muse = MuseConnector(engine, on_status=_muse_status_cb)
        muse.on_eyebrow_raise = _eyebrow_cb
        muse.on_wink          = _wink_cb
        muse.on_jaw_clench    = _jaw_clench_cb
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
