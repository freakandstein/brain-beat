"""
Notify
======
Script kecil berdiri sendiri untuk kirim notifikasi ke ntfy topic yang diisi
lewat NTFY_TOPIC (env var atau file .env — lihat local_config.py dan
.env.example) — sama tujuan dan endpoint dengan eeg_smart_comment.py, tapi
tanpa perlu jalankan server EEG. Berguna untuk testing manual atau kirim
pesan ad-hoc kapan saja.

Penggunaan:
    python notify.py "teks yang mau dikirim"
"""

import sys
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

import local_config


def send(text: str) -> bool:
    topic = local_config.get("NTFY_TOPIC")
    if not topic:
        print("⚠️  NTFY_TOPIC belum diisi (isi di .env, lihat .env.example, atau export NTFY_TOPIC=...)")
        return False
    try:
        resp = requests.post(
            f"https://ntfy.sh/{topic}",
            data=text.encode("utf-8"),
            timeout=5,
        )
        resp.raise_for_status()
        print(f"✅  Terkirim ke ntfy/{topic}: {text!r}")
        return True
    except Exception as e:
        print(f"⚠️  Gagal kirim ke ntfy: {e}")
        return False


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Penggunaan: python notify.py \"teks yang mau dikirim\"")
        sys.exit(1)
    ok = send(sys.argv[1])
    sys.exit(0 if ok else 1)
