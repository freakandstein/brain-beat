"""
Tes: topic ntfy tidak lagi hardcode di source — datang dari NTFY_TOPIC
(env var / .env). Tanpa topic, tidak boleh ada request keluar sama sekali
(kalau tidak, pesan terkirim ke topic bernama "None" di ntfy.sh publik).

requests.post di-mock karena itu satu-satunya batas ke jaringan; yang
diperiksa adalah URL & isi yang kita kirim, bukan mock-nya.

Jalankan dari root project:
    python3 -m unittest discover tests
"""

import contextlib
import io
import json
import os
import tempfile
import unittest
from unittest import mock

import eeg_smart_comment
import local_config
import notify


class NtfyTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name

        # Isolasi dari NTFY_TOPIC / .env milik developer yang menjalankan tes.
        patch_path = mock.patch.object(
            local_config, "ENV_PATH", os.path.join(self.tmp, ".env"))
        patch_path.start()
        self.addCleanup(patch_path.stop)
        patch_env = mock.patch.dict(os.environ)
        patch_env.start()
        self.addCleanup(patch_env.stop)
        os.environ.pop("NTFY_TOPIC", None)

        patch_post = mock.patch("requests.post")
        self.post = patch_post.start()
        self.addCleanup(patch_post.stop)


class SmartCommentTopicTest(NtfyTestCase):
    def make_bot(self, topic):
        templates = os.path.join(self.tmp, "templates")
        os.makedirs(templates, exist_ok=True)
        with open(os.path.join(templates, "pool_enable.json"), "w") as f:
            json.dump(["halo dari tes"], f)
        with contextlib.redirect_stdout(io.StringIO()):
            return eeg_smart_comment.SmartCommentBot(
                topic=topic,
                templates_dir=templates,
                config_path=os.path.join(self.tmp, "smart_comment_config.json"),
            )

    def test_without_topic_sends_nothing_and_says_how_to_fix(self):
        for topic in (None, ""):
            with self.subTest(topic=topic):
                self.post.reset_mock()
                bot = self.make_bot(topic)
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    bot._do_send()
                self.post.assert_not_called()
                self.assertIn("NTFY_TOPIC", out.getvalue())

    def test_with_topic_posts_the_comment_to_that_topic(self):
        # Pengaman: guard "tanpa topic" tidak boleh ikut memblokir jalur normal.
        bot = self.make_bot("my-test-topic")
        with contextlib.redirect_stdout(io.StringIO()):
            bot._do_send()
        self.post.assert_called_once()
        args, kwargs = self.post.call_args
        self.assertEqual(args[0], "https://ntfy.sh/my-test-topic")
        self.assertEqual(kwargs["data"], "halo dari tes".encode("utf-8"))


class NotifyTopicTest(NtfyTestCase):
    def send(self, text):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            ok = notify.send(text)
        return ok, out.getvalue()

    def test_without_topic_sends_nothing_and_says_how_to_fix(self):
        ok, output = self.send("halo")
        self.assertFalse(ok)
        self.post.assert_not_called()
        self.assertIn("NTFY_TOPIC", output)

    def test_posts_to_topic_from_env_var(self):
        os.environ["NTFY_TOPIC"] = "env-topic"
        ok, _ = self.send("halo")
        self.assertTrue(ok)
        args, kwargs = self.post.call_args
        self.assertEqual(args[0], "https://ntfy.sh/env-topic")
        self.assertEqual(kwargs["data"], b"halo")

    def test_posts_to_topic_from_env_file(self):
        with open(local_config.ENV_PATH, "w") as f:
            f.write("NTFY_TOPIC=file-topic\n")
        ok, _ = self.send("halo")
        self.assertTrue(ok)
        args, kwargs = self.post.call_args
        self.assertEqual(args[0], "https://ntfy.sh/file-topic")
        self.assertEqual(kwargs["data"], b"halo")


if __name__ == "__main__":
    unittest.main()
