"""
Local Config
============
Rahasia & setting khusus mesin ini (password OBS WebSocket, topic ntfy) yang
TIDAK boleh masuk git. Urutan pencarian tiap key:
    1. environment variable asli
    2. file .env di folder project (di-gitignore; salin dari .env.example)
    3. default dari pemanggil

Tanpa dependency tambahan — parser KEY=VALUE kecil, cukup untuk setting ini
(tidak mendukung interpolasi, awalan `export`, atau komentar di akhir baris).
Nilai kosong dianggap belum diisi, jadi "KEY=" di .env.example tidak menutupi
default.

Penggunaan:
    import local_config
    password = local_config.get("OBS_PASSWORD", "")
    topic    = local_config.get("NTFY_TOPIC")    # None kalau belum diisi
"""

import os

ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")


def _read_env_file() -> dict:
    values = {}
    try:
        with open(ENV_PATH, "r") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                value = value.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                    value = value[1:-1]
                values[key.strip()] = value
    except OSError:
        pass
    return values


def get(key: str, default=None):
    return os.environ.get(key) or _read_env_file().get(key) or default
