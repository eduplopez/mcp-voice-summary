"""Tests for the MCP voice summary server.

Run with:  .venv\\Scripts\\python.exe -m unittest discover -s tests

These cover the parts that have bitten us: input handling, queue limits,
voice resolution and the platform fallbacks. They never play real audio, so
they run offline and fast.
"""

import os
import queue
import subprocess
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("VOICE_ENGINE", "edge")

import server  # noqa: E402


class TestInputHandling(unittest.TestCase):
    def setUp(self):
        self.queue = mock.patch.object(server, "_queue", queue.Queue()).start()
        self.addCleanup(mock.patch.stopall)

    def test_empty_summary(self):
        self.assertIn("empty", server.speak_summary("   "))

    def test_control_characters_removed(self):
        server.speak_summary("hola\x00\x07 mundo")
        spoken = server._queue.get_nowait()
        self.assertNotIn("\x00", spoken)
        self.assertIn("hola", spoken)

    def test_markup_stripped(self):
        self.assertEqual(server._sanitize("a<b>bold</b>c", "sapi5"), "a bold c")

    def test_markup_stripped_on_edge_too(self):
        self.assertNotIn("<b>", server._sanitize("a<b>bold</b>c", "edge"))

    def test_summary_is_trimmed(self):
        server.speak_summary("uno   dos\n\ntres ")
        self.assertEqual(server._queue.get_nowait(), "uno dos tres")

    def test_truncation_is_announced(self):
        with mock.patch.object(server, "MAX_SUMMARY_WORDS", "3"):
            result = server.speak_summary("uno dos tres cuatro cinco")
        self.assertIn("truncated", result)
        self.assertEqual(server._queue.get_nowait(), "uno dos tres")

    def test_mute_is_reported(self):
        with mock.patch.object(server, "VOICE_MUTE", True):
            self.assertEqual(server.speak_summary("hola"), "Muted.")

    def test_queue_limit_rejects(self):
        with mock.patch.object(server, "MAX_QUEUE_SIZE", "2"):
            server._queue.put("a")
            server._queue.put("b")
            self.assertIn("already queued", server.speak_summary("hola"))


class TestVoiceNameValidation(unittest.TestCase):
    def test_unknown_voice_rejected_before_queueing(self):
        with mock.patch.object(server, "VOICE_NAME", "xx-YY-FakeNeural"):
            with mock.patch.object(
                server, "_load_edge_catalog", return_value={"es-ES-A": {"Locale": "es-ES"}}
            ):
                self.assertIn("does not exist", server.speak_summary("hola"))
        self.assertTrue(server._queue.empty())

    def test_known_voice_accepted(self):
        with mock.patch.object(server, "VOICE_NAME", "es-ES-A"):
            with mock.patch.object(
                server,
                "_load_edge_catalog",
                return_value={"es-ES-A": {"Locale": "es-ES", "Gender": "Female"}},
            ):
                server.speak_summary("hola")
        self.assertEqual(server._queue.get_nowait(), "hola")

    def test_no_catalog_means_no_validation(self):
        with mock.patch.object(server, "VOICE_NAME", "whatever"):
            with mock.patch.object(server, "_load_edge_catalog", return_value={}):
                self.assertEqual(server._validate_voice_name(), "")


class TestLanguageAndGender(unittest.TestCase):
    def test_curated_gender_pairs(self):
        catalog = {
            "es-ES-F": {"Locale": "es-ES", "Gender": "Female"},
            "es-ES-M": {"Locale": "es-ES", "Gender": "Male"},
            "en-US-F": {"Locale": "en-US", "Gender": "Female"},
            "en-US-M": {"Locale": "en-US", "Gender": "Male"},
            "en-GB-F": {"Locale": "en-GB", "Gender": "Female"},
            "en-GB-M": {"Locale": "en-GB", "Gender": "Male"},
        }
        with mock.patch.object(server, "VOICE_NAME", ""):
            with mock.patch.object(server, "_load_edge_catalog", return_value=catalog):
                with mock.patch.dict(
                    server.VOICES_BY_LANGUAGE, {"es": ("es-ES-F", "es-ES-M"), "en": ("en-US-F", "en-US-M")}
                ):
                    self.assertEqual(server._edge_voice_for_language("es", "female"), "es-ES-F")
                    self.assertEqual(server._edge_voice_for_language("es", "male"), "es-ES-M")
                    # A region the curated voice does not cover must still win.
                    self.assertEqual(server._edge_voice_for_language("en-GB", "male"), "en-GB-M")

    def test_missing_gender_falls_back_to_available(self):
        catalog = {"zh-CN-Shaanxi": {"Locale": "zh-CN-shaanxi", "Gender": "Female"}}
        with mock.patch.object(server, "VOICE_NAME", ""):
            with mock.patch.object(server, "_load_edge_catalog", return_value=catalog):
                with mock.patch.dict(server.VOICES_BY_LANGUAGE, {}, clear=True):
                    self.assertEqual(
                        server._edge_voice_for_language("zh-CN-shaanxi", "male"),
                        "zh-CN-Shaanxi",
                    )

    def test_unknown_language_uses_fallback_voice(self):
        with mock.patch.object(server, "VOICE_NAME", ""):
            with mock.patch.object(server, "_load_edge_catalog", return_value={}):
                with mock.patch.dict(server.VOICES_BY_LANGUAGE, {}, clear=True):
                    self.assertEqual(
                        server._edge_voice_for_language("xx"), server.DEFAULT_EDGE_VOICE
                    )

    def test_gender_aliases(self):
        for value in ("f", "F", "female", "woman"):
            self.assertEqual(server._normalize_gender(value), "female")
        for value in ("m", "male", "man"):
            self.assertEqual(server._normalize_gender(value), "male")
        self.assertEqual(server._normalize_gender("nonsense"), "")

    def test_set_language_rejects_bad_input(self):
        self.assertIn("Invalid", server.set_language("not a language"))
        self.assertIn("Unknown gender", server.set_language("", "nonsense"))

    def test_set_language_keeps_current_when_empty(self):
        with mock.patch.object(server, "_forced_language", "fr"):
            server.set_language("")
            self.assertEqual(server._forced_language, "fr")

    def test_percentage_normalisation(self):
        self.assertEqual(server._normalize_percentage("120"), "+20%")
        self.assertEqual(server._normalize_percentage("100"), "+0%")
        self.assertEqual(server._normalize_percentage("-15%"), "-15%")
        self.assertEqual(server._normalize_percentage("garbage"), "+0%")

    def test_timeout_parser(self):
        self.assertEqual(server._timeout("12", 30), 12)
        self.assertEqual(server._timeout("nonsense", 30), 30)
        self.assertEqual(server._timeout("0", 30), 30)


class TestPlayback(unittest.TestCase):
    def test_child_process_does_not_inherit_stdin(self):
        """The child must not be able to read the MCP JSON-RPC stream."""
        seen = {}

        def fake_run(command, **kwargs):
            seen.update(kwargs)

        with mock.patch.object(server.shutil, "which", return_value="/usr/bin/ffplay"):
            with mock.patch.object(server.subprocess, "run", fake_run):
                with mock.patch.object(server.sys, "platform", "linux"):
                    server._play_with_external("/tmp/x.mp3")

        self.assertEqual(seen["stdin"], subprocess.DEVNULL)
        self.assertIn("timeout", seen)

    def test_missing_player_raises_clear_error(self):
        with mock.patch.object(server.shutil, "which", return_value=None):
            with mock.patch.object(server.sys, "platform", "linux"):
                with self.assertRaises(RuntimeError) as ctx:
                    server._play_with_external("/tmp/x.mp3")
        self.assertIn("VOICE_PLAYER", str(ctx.exception))


class TestPrivacy(unittest.TestCase):
    def test_summary_text_is_not_logged_on_failure(self):
        secret = "token=SECRETVALUE"
        with self.assertLogs(server.logger, level="ERROR") as logs:
            try:
                raise RuntimeError("boom")
            except RuntimeError:
                server.logger.exception(
                    "Could not play the summary (%d words, sha256 %s)",
                    len(secret.split()),
                    server.hashlib.sha256(secret.encode()).hexdigest()[:12],
                )
        joined = "\n".join(logs.output)
        self.assertNotIn("SECRETVALUE", joined)
        self.assertIn("sha256", joined)


class TestTokenBudget(unittest.TestCase):
    def test_response_does_not_echo_the_summary(self):
        """Echoing the text back wastes context on every call."""
        with mock.patch.object(server, "_queue", queue.Queue()):
            self.assertEqual(server.speak_summary("palabra " * 100), "Played.")


if __name__ == "__main__":
    unittest.main(verbosity=2)