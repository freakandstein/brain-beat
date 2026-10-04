"""
Tes untuk local_config — loader rahasia/setting lokal (env var + file .env).

Jalankan dari root project:
    python3 -m unittest discover tests
"""

import os
import tempfile
import unittest
from unittest import mock

import local_config

# Sengaja bukan key asli (OBS_PASSWORD dst) supaya env/.env milik developer
# yang menjalankan tes tidak ikut masuk ke hasil tes.
KEY = "LOCAL_CONFIG_TEST_KEY"


class LocalConfigTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.env_path = os.path.join(tmp.name, ".env")

        patch_path = mock.patch.object(local_config, "ENV_PATH", self.env_path)
        patch_path.start()
        self.addCleanup(patch_path.stop)

        patch_env = mock.patch.dict(os.environ)
        patch_env.start()
        self.addCleanup(patch_env.stop)
        os.environ.pop(KEY, None)

    def write_env(self, text):
        with open(self.env_path, "w") as f:
            f.write(text)

    def test_returns_env_var(self):
        os.environ[KEY] = "from-env"
        self.assertEqual(local_config.get(KEY), "from-env")

    def test_falls_back_to_env_file_when_env_var_missing(self):
        self.write_env(f"{KEY}=from-file\n")
        self.assertEqual(local_config.get(KEY), "from-file")

    def test_env_var_wins_over_env_file(self):
        os.environ[KEY] = "from-env"
        self.write_env(f"{KEY}=from-file\n")
        self.assertEqual(local_config.get(KEY), "from-env")

    def test_returns_default_when_unset_and_env_file_missing(self):
        self.assertIsNone(local_config.get(KEY))
        self.assertEqual(local_config.get(KEY, "fallback"), "fallback")

    def test_returns_default_when_key_absent_from_env_file(self):
        self.write_env("SOMETHING_ELSE=x\n")
        self.assertEqual(local_config.get(KEY, "fallback"), "fallback")

    def test_empty_value_in_env_file_counts_as_unset(self):
        # .env.example dikirim dengan "KEY=" kosong — artinya belum diisi.
        self.write_env(f"{KEY}=\n")
        self.assertEqual(local_config.get(KEY, "fallback"), "fallback")

    def test_empty_env_var_does_not_mask_env_file(self):
        os.environ[KEY] = ""
        self.write_env(f"{KEY}=from-file\n")
        self.assertEqual(local_config.get(KEY), "from-file")

    def test_env_file_line_formats(self):
        cases = [
            ("LCT_PLAIN=abc",          "LCT_PLAIN",  "abc"),
            ("  LCT_SPACED  =  abc  ", "LCT_SPACED", "abc"),
            ('LCT_DQ="has space"',     "LCT_DQ",     "has space"),
            ("LCT_SQ='has space'",     "LCT_SQ",     "has space"),
            ("LCT_EQ=a=b=c",           "LCT_EQ",     "a=b=c"),
            # '#' di tengah nilai bukan komentar — password boleh mengandung '#'.
            ("LCT_HASH=abc#def",       "LCT_HASH",   "abc#def"),
        ]
        self.write_env(
            "# komentar satu baris penuh\n"
            "\n"
            + "\n".join(line for line, _, _ in cases) + "\n"
        )
        for _, key, expected in cases:
            with self.subTest(key=key):
                self.assertEqual(local_config.get(key), expected)

    def test_commented_out_assignment_is_ignored(self):
        self.write_env(f"# {KEY}=should-not-load\n")
        self.assertIsNone(local_config.get(KEY))


if __name__ == "__main__":
    unittest.main()
