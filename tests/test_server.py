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
        spoken = server._queue.get_nowait()[0]
        self.assertNotIn("\x00", spoken)
        self.assertIn("hola", spoken)

    def test_markup_stripped(self):
        self.assertEqual(server._sanitize("a<b>bold</b>c", "sapi5"), "a bold c")

    def test_markup_stripped_on_edge_too(self):
        self.assertNotIn("<b>", server._sanitize("a<b>bold</b>c", "edge"))

    def test_summary_is_trimmed(self):
        server.speak_summary("uno   dos\n\ntres ")
        self.assertEqual(server._queue.get_nowait()[0], "uno dos tres")

    def test_truncation_is_announced(self):
        with mock.patch.object(server, "MAX_SUMMARY_WORDS", "3"):
            result = server.speak_summary("uno dos tres cuatro cinco")
        self.assertIn("truncated", result)
        self.assertEqual(server._queue.get_nowait()[0], "uno dos tres")

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
        self.assertEqual(server._queue.get_nowait()[0], "hola")

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


class TestRedaction(unittest.TestCase):
    def test_redacts_common_token_shapes(self):
        cases = {
            "api_key=ABC123XYZ456TOKEN": "ABC123XYZ456TOKEN",
            "password: hunter2hunter2": "hunter2hunter2",
            "ghp_abcdefghijklmnopqrstuvwxyz0123": "abcdefghijklmnopqrstuvwxyz",
            "AKIAIOSFODNN7EXAMPLE": "AKIAIOSFODNN7EXAMPLE",
            "Bearer abcdefghijklmnopqrstuvwxyz": "abcdefghijklmnopqrstuvwxyz",
            "sk-abcdefghijklmnopqrstuvwxyz12": "abcdefghijklmnop",
            "xoxb-123456789012-abcdefghij": "abcdefghij",
            "juan.perez@ejemplo.com": "ejemplo.com",
        }
        for text, secret in cases.items():
            cleaned = server._redact(text)
            self.assertNotIn(secret, cleaned, f"not redacted: {text}")
            self.assertIn("[redacted", cleaned)

    def test_redacts_private_key_block(self):
        key = (
            "-----BEGIN RSA PRIVATE KEY-----\nMIIEow\nIBAAKC\n"
            "-----END RSA PRIVATE KEY-----"
        )
        self.assertNotIn("MIIEow", server._redact(f"deploy con {key}"))

    def test_redaction_can_be_disabled(self):
        with mock.patch.object(server, "REDACT", False):
            self.assertEqual(server._redact("api_key=ABC123XYZ"), "api_key=ABC123XYZ")

    def test_ordinary_text_is_untouched(self):
        text = "He actualizado el modulo de autenticacion y sus tres pruebas"
        self.assertEqual(server._redact(text), text)

    def test_markers_do_not_get_remangled(self):
        """A later pattern must not match what an earlier one replaced."""
        out = server._redact("api_key=sk-abcdefghijklmnopqrstuvwxyz12")
        self.assertIn("[redacted", out)
        self.assertNotIn("[redacted] token]", out)

    def test_secret_never_reaches_the_queue(self):
        with mock.patch.object(server, "_queue", queue.Queue()):
            server.speak_summary("despliegue hecho con api_key=SUPERSECRET1234")
            self.assertNotIn("SUPERSECRET1234", server._queue.get_nowait()[0])


class TestAdmissionControl(unittest.TestCase):
    def setUp(self):
        self.q = mock.patch.object(server, "_queue", queue.Queue()).start()
        server._notif_times.clear()
        server._last_text = ""

    def test_queue_limit(self):
        with mock.patch.object(server, "MAX_QUEUE_SIZE", "1"):
            self.assertEqual(server.speak_summary("primera"), "Played.")
            self.assertIn("already queued", server.speak_summary("segunda"))

    def test_rate_limit(self):
        with mock.patch.object(server, "VOICE_RATE_LIMIT", "2"):
            server.speak_summary("una")
            server.speak_summary("dos")
            self.assertIn("rate limit", server.speak_summary("tres"))

    def test_identical_consecutive_summaries_are_skipped(self):
        self.assertEqual(server.speak_summary("igual"), "Played.")
        self.assertIn("identical", server.speak_summary("igual"))
        # A different text is fine again.
        self.assertEqual(server.speak_summary("distinta"), "Played.")

    def test_mute_is_reported(self):
        with mock.patch.object(server, "VOICE_MUTE", True):
            self.assertEqual(server.speak_summary("hola"), "Muted.")

    def test_queue_entries_carry_a_timestamp(self):
        server.speak_summary("hola")
        text, enqueued_at = server._queue.get_nowait()
        self.assertEqual(text, "hola")
        self.assertGreater(enqueued_at, 0)


class TestStdoutPurity(unittest.TestCase):
    """stdout carries the MCP JSON-RPC stream, so nothing may write to it."""

    def test_noisy_synthesizer_cannot_reach_stdout(self):
        import subprocess
        import tempfile

        noisy = os.path.join(tempfile.gettempdir(), "_noisy_pyttsx3_probe.py")
        # A stand-in synthesizer that prints to stdout when imported, which is
        # exactly what comtypes did before the redirect was added.
        with open(noisy, "w", encoding="utf-8") as handle:
            handle.write(
                "import sys, types\n"
                "import server_stub as s\n"
                "mod = types.ModuleType('pyttsx3')\n"
                "class E:\n"
                "    def getProperty(self, k): return [] if k == 'voices' else None\n"
                "    def setProperty(self, *a): pass\n"
                "    def say(self, t): pass\n"
                "    def runAndWait(self): pass\n"
                "    def isBusy(self): return False\n"
                "def init():\n"
                "    print('NOISE-ON-STDOUT')\n"
                "    return E()\n"
                "mod.init = init\n"
                "sys.modules['pyttsx3'] = mod\n"
                "s.server._sapi5_engine = None\n"
                "s.server._init_sapi5()\n"
            )
        stub = os.path.join(tempfile.gettempdir(), "server_stub.py")
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(stub, "w", encoding="utf-8") as handle:
            handle.write(
                "import os, sys\n"
                f"sys.path.insert(0, {root!r})\n"
                "os.environ['VOICE_ENGINE'] = 'sapi5'\n"
                "import server\n"
            )

        proc = subprocess.run(
            [sys.executable, noisy], capture_output=True, text=True, timeout=60
        )
        self.assertNotIn("NOISE-ON-STDOUT", proc.stdout)
        self.assertIn("NOISE-ON-STDOUT", proc.stderr)


class TestTokenBudget(unittest.TestCase):
    def test_response_does_not_echo_the_summary(self):
        """Echoing the text back wastes context on every call."""
        with mock.patch.object(server, "_queue", queue.Queue()):
            self.assertEqual(server.speak_summary("palabra " * 100), "Played.")


if __name__ == "__main__":
    unittest.main(verbosity=2)

