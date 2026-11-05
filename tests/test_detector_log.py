"""
Tes: log diagnostik per-tick detektor eyebrow/jaw (eeg_detector_*.csv).

Latar belakang: CSV sesi hanya ditulis tiap 7 tick dan _cmd_diag direset tiap
tick, jadi `cmd_fired` cuma menangkap ~1 dari 7 fire; threshold dan amplitudo
eyebrow hanya muncul di terminal. Log ini merekam SETIAP tick supaya kejadian
"eyebrow susah / ketrigger sendiri" bisa dibaca angkanya dari file.

Dua hal yang dikunci di sini:
  1. isi log benar (kolom, nilai, setiap fire tercatat),
  2. log hanya observasi — kegagalan menulisnya tidak boleh mengganggu loop
     deteksi, dan loop deteksi tetap fire seperti sebelumnya.

Test integrasi menjalankan MuseConnector._loop yang ASLI dengan inlet LSL palsu
(tanpa Muse, tanpa server). File CSV ditulis ke direktori sementara.

Jalankan dari root project:
    python3 -m unittest discover tests
"""

import contextlib
import csv
import io
import os
import tempfile
import threading
import unittest
from unittest import mock

import numpy as np

try:
    with contextlib.redirect_stdout(io.StringIO()):
        import brainflow_connector
except (ImportError, SystemExit) as e:
    raise unittest.SkipTest(f"brainflow_connector tidak bisa diimpor: {e}")

DetectorTickLog = brainflow_connector.DetectorTickLog


class DetectorTickLogTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = os.path.join(tmp.name, "eeg_detector_test.csv")

    def _read(self):
        with open(self.path, newline="") as f:
            return list(csv.reader(f))

    def test_header_is_the_declared_columns(self):
        log = DetectorTickLog(self.path)
        log.close()
        self.assertEqual(self._read()[0], DetectorTickLog.COLUMNS)

    def test_values_are_written_by_column_name_and_missing_are_blank(self):
        log = DetectorTickLog(self.path)
        log.write(tick=5, p2p_af7=123.4, thr_eyebrow=250.0, eb_fired=1)
        log.close()
        header, row = self._read()
        cell = dict(zip(header, row))
        self.assertEqual(cell["tick"], "5")
        self.assertEqual(cell["p2p_af7"], "123.4")
        self.assertEqual(cell["thr_eyebrow"], "250.0")
        self.assertEqual(cell["eb_fired"], "1")
        self.assertEqual(cell["p2p_af8"], "")

    def test_write_after_close_does_not_raise(self):
        log = DetectorTickLog(self.path)
        log.close()
        log.write(tick=1)
        log.close()

    def test_unwritable_path_disables_log_without_raising(self):
        bad = os.path.join(os.path.dirname(self.path), "tidak", "ada", "x.csv")
        with contextlib.redirect_stdout(io.StringIO()):
            log = DetectorTickLog(bad)
            log.write(tick=1)
            log.close()
        self.assertFalse(os.path.exists(bad))

    def test_write_failure_does_not_raise_and_disables_log(self):
        log = DetectorTickLog(self.path)
        failing = mock.Mock(side_effect=OSError("disk penuh"))
        log._w = mock.Mock(writerow=failing)
        with contextlib.redirect_stdout(io.StringIO()):
            log.write(tick=1)
            log.write(tick=2)
        self.assertEqual(failing.call_count, 1, "setelah gagal, log harus mati — bukan mencoba terus tiap tick")
        log.close()


class FakeEEGInlet:
    """Pengganti pylsl.StreamInlet: noise datar + satu pulsa bilateral di AF7/AF8
    (setara kedipan/gerakan alis) pada panggilan ke-`burst_call`."""

    SR = 256

    def __init__(self, burst_call=6, amp=400.0):
        self.calls = 0
        self.burst_call = burst_call
        self.amp = amp
        self.rng = np.random.default_rng(0)

    def pull_chunk(self, timeout=0.0, max_samples=512):
        self.calls += 1
        n = 600 if self.calls == 1 else 40          # 40 sampel ≈ satu tick (0.15 s)
        data = self.rng.normal(0.0, 10.0, (n, 5))   # std 10 µV → q=1.0 di semua channel
        if self.calls == self.burst_call:
            t = np.arange(n) / self.SR
            pulse = self.amp * np.exp(-0.5 * ((t - t[n // 2]) / 0.06) ** 2)
            data[:, 1] += pulse                     # AF7
            data[:, 2] += pulse                     # AF8
        return data.tolist(), [0.0] * n


class LoopIntegrationTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        cwd = os.getcwd()
        os.chdir(self.dir)                           # _loop menulis CSV relatif ke cwd
        self.addCleanup(os.chdir, cwd)

    def _run_loop(self):
        try:
            conn = brainflow_connector.MuseConnector(engine=mock.MagicMock())
        except RuntimeError as e:
            self.skipTest(str(e))
        conn._calib_done = True                      # lewati kalibrasi 100 tick
        conn._thr_eyebrow = 150.0
        fired = threading.Event()
        calls = []

        def on_eyebrow():
            calls.append(1)
            fired.set()

        conn.on_eyebrow_raise = on_eyebrow
        conn.running = True
        errors = []

        def target():
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    conn._loop(FakeEEGInlet(), None)
            except Exception as e:                   # pragma: no cover
                errors.append(e)

        th = threading.Thread(target=target, daemon=True)
        th.start()
        self.assertTrue(fired.wait(20), "eyebrow tidak fire pada pulsa bilateral — perilaku deteksi berubah")
        threading.Event().wait(0.8)                  # beberapa tick lagi supaya cooldown terlihat di log
        conn.running = False
        th.join(10)
        self.assertFalse(th.is_alive(), "_loop tidak berhenti")
        self.assertEqual(errors, [])
        return calls

    def test_loop_logs_every_tick_and_eyebrow_still_fires_once(self):
        calls = self._run_loop()
        self.assertEqual(len(calls), 1, "satu pulsa harus tetap menghasilkan tepat satu fire (cooldown 3 s)")

        files = [f for f in os.listdir(self.dir) if f.startswith("eeg_detector_")]
        self.assertEqual(len(files), 1)
        sess = [f for f in os.listdir(self.dir) if f.startswith("eeg_session_")]
        self.assertEqual(len(sess), 1)
        self.assertEqual(files[0].replace("eeg_detector_", ""), sess[0].replace("eeg_session_", ""),
                         "log detektor harus berpasangan dengan CSV sesi (stempel waktu sama)")

        with open(os.path.join(self.dir, files[0]), newline="") as f:
            rows = list(csv.DictReader(f))
        self.assertGreaterEqual(len(rows), 8)
        self.assertEqual(list(rows[0].keys()), DetectorTickLog.COLUMNS)

        for r in rows:
            for col, val in r.items():
                self.assertNotEqual(val, "", f"kolom {col} kosong — field tidak diisi di _loop")
        ticks = [int(r["tick"]) for r in rows]
        self.assertEqual(ticks, sorted(set(ticks)), "satu baris per tick, urut")
        self.assertEqual(ticks, list(range(ticks[0], ticks[0] + len(ticks))), "tidak boleh ada tick yang terlewat")

        self.assertTrue(all(float(r["thr_eyebrow"]) == 150.0 for r in rows))
        self.assertTrue(all(r["calib_done"] == "1" for r in rows))

        fired = [r for r in rows if r["eb_fired"] == "1"]
        self.assertEqual(len(fired), 1)
        self.assertEqual(fired[0]["bilateral"], "1")
        self.assertGreaterEqual(int(fired[0]["streak"]), 3, "streak dicatat sebelum direset oleh fire")
        self.assertGreater(float(fired[0]["p2p_af7"]), 150.0)
        self.assertGreater(float(fired[0]["p2p_af8"]), 150.0)
        quiet = rows[0]
        self.assertEqual(quiet["bilateral"], "0")
        self.assertLess(float(quiet["p2p_af7"]), 150.0)


if __name__ == "__main__":
    unittest.main()
