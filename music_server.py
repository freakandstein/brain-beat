"""
EEG Music Engine — Web Server
==============================
Flask + SocketIO bridge antara music_engine dan browser UI.

Penggunaan:
    python music_server.py
    Buka: http://localhost:5000
"""

import sys
import os
import signal
import subprocess

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

PORT = 8765


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

from music_engine import MusicEngine, find_or_download_soundfont
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
    "relax":  (0.70, 0.20, 0.10),
    "focus":  (0.20, 0.70, 0.10),
    "stress": (0.15, 0.70, 0.60),
    "drowsy": (0.20, 0.10, 0.70),
}


# ── routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


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
    name = data.get("preset", "relax")
    if name in PRESETS and engine:
        a, b, t = PRESETS[name]
        engine.set_eeg(a, b, t)
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
        if engine:
            with engine._lock:
                eeg   = engine.eeg
                state = eeg.mental_state()
                payload = {
                    "state": state,
                    "bpm":   round(engine._bpm, 1),
                    "build": round(engine._build_level, 3),
                    "stress_build": round(engine._stress_build, 3),
                    "delta": round(eeg.delta, 3),
                    "alpha": round(eeg.alpha, 3),
                    "beta":  round(eeg.beta,  3),
                    "theta": round(eeg.theta, 3),
                    "delta_raw": round(muse.raw_bands["delta"], 2) if muse else None,
                    "alpha_raw": round(muse.raw_bands["alpha"], 2) if muse else None,
                    "beta_raw":  round(muse.raw_bands["beta"],  2) if muse else None,
                    "theta_raw": round(muse.raw_bands["theta"], 2) if muse else None,
                    "muse":  muse.status if muse else "unavailable",
                    "heart_rate": muse.heart_rate if muse else None,
                    "channel_quality": muse.channel_quality if muse else None,
                }
            socketio.emit("state_update", payload)
        socketio.sleep(0.1)


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    global engine, muse

    _kill_existing()

    print("🎵  EEG Music Engine — Web UI")
    print("    Mencari soundfont...")
    sf_path = find_or_download_soundfont()

    print("    Inisialisasi FluidSynth...")
    engine = MusicEngine(sf_path)
    engine.start()

    # Inisialisasi Muse connector (jika brainflow terinstall)
    if BRAINFLOW_AVAILABLE and MuseConnector:
        def _muse_status_cb(status: str, error: str):
            socketio.emit("muse_status", {"status": status, "error": error})
        muse = MuseConnector(engine, on_status=_muse_status_cb)
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
