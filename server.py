"""
MCP voice summary server: reads out loud a short summary of the work the
assistant just finished.

Engines, set with VOICE_ENGINE:
- sapi5 (default): pyttsx3 on the native system synthesizer. Offline, basic
  voices. SAPI5 on Windows, NSSpeech on Linux, NSSS on macOS.
- edge: edge-tts neural voices. Much better, but sends the text to Microsoft.

See the README for the full configuration reference.

Non-obvious constraints:
- pyttsx3 blocks forever if the engine is created in one thread and used in
  another (COM is apartment-threaded), so the engine is created lazily and
  only ever used from the worker thread.
- SAPI5's runAndWait can return before playback finishes, hence the isBusy
  wait in _speak_sapi5.
- stdout carries the MCP JSON-RPC stream, so imports of the synthesizer are
  wrapped in a redirect to stderr.
- The pyttsx3, edge_tts and langdetect imports are lazy so the server starts
  on any platform even when one engine is unavailable.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

mcp = MCPServer("VoiceSummaryServer")

logger = logging.getLogger("voice-summary")

ENGINE = os.environ.get("VOICE_ENGINE", "sapi5").strip().lower()
VOICE_NAME = os.environ.get("VOICE_NAME", "").strip()
VOICE_RATE = os.environ.get("VOICE_RATE", "100").strip()
VOICE_VOLUME = os.environ.get("VOICE_VOLUME", "100").strip()

# Empty means auto-detect.
VOICE_PLAYER = os.environ.get("VOICE_PLAYER", "").strip()

# Blocks playback that never returns (a wedged player or a hung HTTP call).
PLAYBACK_TIMEOUT = os.environ.get("PLAYBACK_TIMEOUT", "30").strip()

# Silences the server without disabling it: summaries are still queued and
# acknowledged, nothing is played. Useful in shared spaces and meetings.
VOICE_MUTE = os.environ.get("VOICE_MUTE", "").strip().lower() in ("1", "true", "yes")

# Strips credentials out of the text before it is spoken or sent to the cloud
# engine. On by default; set to 0 only if summaries never carry anything
# sensitive.
REDACT = os.environ.get("VOICE_REDACT", "1").strip().lower() not in (
    "0",
    "false",
    "no",
)

# A spoken notification is a poor channel for old news, so a queued summary
# older than this is dropped instead of read out minutes after it happened.
MAX_SUMMARY_AGE = os.environ.get("MAX_SUMMARY_AGE", "120").strip()

# Notifications per minute. An agent can otherwise fill the queue with spam
# spread over time, which the queue size limit alone does not catch.
VOICE_RATE_LIMIT = os.environ.get("VOICE_RATE_LIMIT", "30").strip()

# "" system language, "es" ISO 639-1, "pt-BR" language + region, "auto" detect
# per summary. Overridable at runtime with set_language.
VOICE_LANGUAGE = os.environ.get("VOICE_LANGUAGE", "").strip()

# "" follows the language, else "female"/"male" (or "f"/"m"). Edge only: SAPI5
# does not expose the gender of its voices.
VOICE_GENDER = os.environ.get("VOICE_GENDER", "").strip().lower()

# Not a length recommendation: the assistant decides that. This only stops an
# oversized `text` from becoming a multi-minute announcement.
MAX_SUMMARY_WORDS = os.environ.get("MAX_SUMMARY_WORDS", "400").strip()

# Without a cap, a client calling faster than playback grows this without limit
# and exhausts memory. Excess requests are rejected, not silently dropped.
MAX_QUEUE_SIZE = os.environ.get("MAX_QUEUE_SIZE", "20").strip()

# Widest voice coverage across both engines.
DEFAULT_LANGUAGE = "en"

# Used when the requested language has no voice at all.
DEFAULT_EDGE_VOICE = "es-ES-AlvaroNeural"

# None means "not forced": normal precedence applies. "auto" delegates to
# per-summary detection.
_forced_language: str | None = None
_forced_gender: str = ""

_edge_catalog: dict[str, dict] = {}
_catalog_loaded = False

_current_sapi5_voice = None  # type: ignore[assignment]

# Pending requests, consumed in order by the worker thread. Each entry is
# (text, enqueued_at) so the worker can drop stale notifications.
_queue: queue.Queue[tuple[str, float] | None] = queue.Queue()

# Guards the queue admission check, the rate limiter and the last-text dedup,
# which are read-modify-write sequences and would otherwise race between MCP
# handler threads.
_admission_lock = threading.Lock()
_notif_times: list[float] = []
_last_text = ""
_lock = threading.Lock()
_sapi5_engine = None  # type: ignore[assignment]


def _normalize_percentage(value: str, default: int = 0) -> str:
    """Converts a volume or speed value into the format edge-tts requires.

    edge-tts only accepts the relative format "+10%" or "-10%". SAPI5 instead
    uses an absolute scale where 100 is the normal value. Both notations are
    accepted so the same variable works for either engine: a bare number is
    read as an absolute scale (100 is normal) and converted to the equivalent
    relative change.
    """
    text = value.strip()
    if text.endswith("%"):
        return text
    try:
        absolute = int(float(text))
    except ValueError:
        return f"{default:+d}%"
    return f"{absolute - 100:+d}%"


def _timeout(value: str, default: int) -> int:
    """Parses a timeout in seconds, falling back when it is unusable."""
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        logger.warning("Invalid timeout %r; using %d seconds", value, default)
        return default
    return seconds if seconds > 0 else default


# Control characters make some engines choke. edge-tts escapes the text for
# SSML itself, but sapi5 hands it straight to the OS synthesizer, and even on
# edge a stray tag would be read out loud. Strip both everywhere.
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_TAGS = re.compile(r"<[^>]*>")


def _sanitize(text: str, engine: str = "") -> str:
    """Strips control characters and markup so the text is only ever spoken."""
    cleaned = _TAGS.sub(" ", _CONTROL_CHARS.sub(" ", text))
    return " ".join(cleaned.split())


# A summary is spoken out loud and, on the edge engine, sent to a third party.
# Credentials read aloud in an open office, or transmitted to a cloud TTS, are a
# leak, so the high-signal patterns are removed before anything else happens.
_SECRET_PATTERNS = [
    # Private key blocks, whole and unreadable.
    (re.compile(r"-----BEGIN[A-Z ]*PRIVATE KEY-----.*?-----END[A-Z ]*PRIVATE KEY-----", re.S), "[redacted key]", "{M}"),
    # Vendor-prefixed tokens: OpenAI, GitHub, Slack, Google, AWS access keys.
    (re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9_\-]{16,}"), "[redacted token]", "{M}"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"), "[redacted token]", "{M}"),
    (re.compile(r"\bxox[abposr]-[A-Za-z0-9\-]{10,}"), "[redacted token]", "{M}"),
    (re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}"), "[redacted token]", "{M}"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[redacted key]", "{M}"),
    # Authorization headers and JWTs.
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{12,}"), "[redacted token]", "{M}"),
    (re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{6,}"), "[redacted token]", "{M}"),
    # key=value assignments: the key stays readable, only the value is dropped.
    (
        re.compile(
            r"(?i)(\b(?:api[_-]?key|secret|passwd|password|token|access[_-]?key|"
            r"client[_-]?secret|private[_-]?key|auth)\b\s*[=:]\s*)"
            r"[\"']?([^\s\"',;]{6,})[\"']?"
        ),
        "[redacted]",
        r"\1{M}",
    ),
    # Email addresses and anything else shaped like one.
    (re.compile(r"\b[\w.+\-]+@[\w\-]+\.[\w.\-]+\b"), "[redacted email]", "{M}"),
]


def _admit(text: str, max_queue: int) -> str:
    """Decides whether a summary may be queued. Returns "" when it may.

    Combines three guards that each cover a different abuse: the queue size
    stops a burst, the rate limit stops sustained spam, and the dedup stops a
    client repeating itself.
    """
    global _last_text

    with _admission_lock:
        if max_queue > 0 and _queue.qsize() >= max_queue:
            return f"Nothing played: {max_queue} summaries already queued."

        limit = _timeout(VOICE_RATE_LIMIT, 30)
        cutoff = time.monotonic() - 60.0
        while _notif_times and _notif_times[0] < cutoff:
            _notif_times.pop(0)
        if limit > 0 and len(_notif_times) >= limit:
            return f"Nothing played: rate limit of {limit} per minute reached."

        if _last_text and _last_text == text:
            return "Skipped: identical to the previous summary."

        _notif_times.append(time.monotonic())
        _last_text = text
    return ""


def _redact(text: str) -> str:
    """Removes credentials and personal addresses from the text.

    Replacements go through placeholders so that a later pattern cannot match
    text an earlier one already redacted, which would otherwise mangle the
    marker itself (for example turning "[redacted token]" into
    "[redacted] token]").
    """
    if not REDACT:
        return text
    for index, (pattern, _, template) in enumerate(_SECRET_PATTERNS):
        text = pattern.sub(template.replace("{M}", f"\x01{index}\x01"), text)
    for index, (_, label, _) in enumerate(_SECRET_PATTERNS):
        text = text.replace(f"\x01{index}\x01", label)
    return text


# --------------------------------------------------------------------------
# Cross-platform MP3 playback
# --------------------------------------------------------------------------
def _play_with_mci(path: str) -> None:
    """Plays an MP3 using the Windows MCI player."""
    import ctypes

    def mci(command: str) -> None:
        ctypes.windll.winmm.mciSendStringW(ctypes.c_wchar_p(command), None, 0, 0)

    mci(f'open "{path}" type mpegvideo alias summary')
    try:
        mci("play summary wait")
    finally:
        mci("close summary")


# External players, in order of preference, per platform. Each entry is a list
# of arguments; the first available executable wins.
_EXTERNAL_PLAYERS: dict[str, list[list[str]]] = {
    "darwin": [["afplay", "{path}"]],
    "linux": [
        ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", "{path}"],
        ["mpg123", "-q", "{path}"],
        ["cvlc", "--play-and-exit", "--quiet", "{path}"],
        ["paplay", "{path}"],
    ],
}


def _play_with_external(path: str) -> None:
    """Plays an MP3 with a system command-line player."""
    import sys

    candidates: list[list[str]] = []
    if VOICE_PLAYER:
        candidates.append([VOICE_PLAYER, "{path}"])
    candidates.extend(_EXTERNAL_PLAYERS.get(sys.platform, []))

    for template in candidates:
        executable = shutil.which(template[0])
        if not executable:
            continue
        command = [path if p == "{path}" else p for p in template]
        command[0] = executable
        # DEVNULL on stdin is mandatory, not cosmetic: stdout/stdin carry the
        # MCP JSON-RPC stream, so a child that inherited stdin could consume
        # protocol messages meant for this server. Verified that a reader child
        # does drain it without this.
        subprocess.run(
            command,
            check=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=_timeout(PLAYBACK_TIMEOUT, 30),
        )
        return

    raise RuntimeError(
        "No audio player was found. Install one of: "
        + ", ".join(p[0] for p in candidates)
        + " or set VOICE_PLAYER."
    )


def _play_mp3(path: str) -> None:
    """Plays an MP3 with whichever player is available on this platform."""
    import sys

    if sys.platform == "win32" and not VOICE_PLAYER:
        _play_with_mci(path)
        return
    _play_with_external(path)


# --------------------------------------------------------------------------
# Language and voice selection
# --------------------------------------------------------------------------
# (female, male) per language, verified against the live catalog. If a name
# ever stops existing, the lookup falls back to another voice of the same
# language and gender.
VOICES_BY_LANGUAGE: dict[str, tuple[str, str]] = {
    "es": ("es-ES-XimenaNeural", "es-ES-AlvaroNeural"),
    "en": ("en-US-AvaNeural", "en-US-AndrewNeural"),
    "fr": ("fr-FR-VivienneMultilingualNeural", "fr-FR-RemyMultilingualNeural"),
    "de": ("de-DE-SeraphinaMultilingualNeural", "de-DE-FlorianMultilingualNeural"),
    "it": ("it-IT-ElsaNeural", "it-IT-GiuseppeNeural"),
    "pt": ("pt-BR-ThalitaMultilingualNeural", "pt-BR-AntonioNeural"),
    "ca": ("ca-ES-JoanaNeural", "ca-ES-EnricNeural"),
    "gl": ("gl-ES-SabelaNeural", "gl-ES-RoiNeural"),
    "ru": ("ru-RU-SvetlanaNeural", "ru-RU-DmitryNeural"),
    "uk": ("uk-UA-PolinaNeural", "uk-UA-OstapNeural"),
    "pl": ("pl-PL-ZofiaNeural", "pl-PL-MarekNeural"),
    "cs": ("cs-CZ-VlastaNeural", "cs-CZ-AntoninNeural"),
    "sk": ("sk-SK-ViktoriaNeural", "sk-SK-LukasNeural"),
    "hu": ("hu-HU-NoemiNeural", "hu-HU-TamasNeural"),
    "ro": ("ro-RO-AlinaNeural", "ro-RO-EmilNeural"),
    "bg": ("bg-BG-KalinaNeural", "bg-BG-BorislavNeural"),
    "el": ("el-GR-AthinaNeural", "el-GR-NestorasNeural"),
    "sv": ("sv-SE-SofieNeural", "sv-SE-MattiasNeural"),
    "da": ("da-DK-ChristelNeural", "da-DK-JeppeNeural"),
    "nb": ("nb-NO-PernilleNeural", "nb-NO-FinnNeural"),
    "fi": ("fi-FI-NooraNeural", "fi-FI-HarriNeural"),
    "nl": ("nl-NL-ColetteNeural", "nl-NL-MaartenNeural"),
    "tr": ("tr-TR-EmelNeural", "tr-TR-AhmetNeural"),
    "ar": ("ar-EG-SalmaNeural", "ar-EG-ShakirNeural"),
    "he": ("he-IL-HilaNeural", "he-IL-AvriNeural"),
    "hi": ("hi-IN-SwaraNeural", "hi-IN-MadhurNeural"),
    "ja": ("ja-JP-NanamiNeural", "ja-JP-KeitaNeural"),
    "ko": ("ko-KR-SunHiNeural", "ko-KR-HyunsuMultilingualNeural"),
    "zh": ("zh-CN-XiaoxiaoNeural", "zh-CN-YunjianNeural"),
    "th": ("th-TH-PremwadeeNeural", "th-TH-NiwatNeural"),
    "vi": ("vi-VN-HoaiMyNeural", "vi-VN-NamMinhNeural"),
    "id": ("id-ID-GadisNeural", "id-ID-ArdiNeural"),
    "ms": ("ms-MY-YasminNeural", "ms-MY-OsmanNeural"),
}

GENDER_ALIASES = {
    "f": "female",
    "female": "female",
    "woman": "female",
    "m": "male",
    "male": "male",
    "man": "male",
}

# Index of each gender inside the VOICES_BY_LANGUAGE tuples.
GENDER_INDEX = {"female": 0, "male": 1}

# Used when the curated voice does not exist.
LOCALE_BY_LANGUAGE: dict[str, str] = {
    "es": "es-ES",
    "en": "en-US",
    "fr": "fr-FR",
    "de": "de-DE",
    "it": "it-IT",
    "pt": "pt-BR",
    "ca": "ca-ES",
    "gl": "gl-ES",
    "ru": "ru-RU",
    "uk": "uk-UA",
    "pl": "pl-PL",
    "cs": "cs-CZ",
    "sk": "sk-SK",
    "hu": "hu-HU",
    "ro": "ro-RO",
    "bg": "bg-BG",
    "el": "el-GR",
    "sv": "sv-SE",
    "da": "da-DK",
    "nb": "nb-NO",
    "fi": "fi-FI",
    "nl": "nl-NL",
    "tr": "tr-TR",
    "ar": "ar-EG",
    "he": "he-IL",
    "hi": "hi-IN",
    "ja": "ja-JP",
    "ko": "ko-KR",
    "zh": "zh-CN",
    "th": "th-TH",
    "vi": "vi-VN",
    "id": "id-ID",
    "ms": "ms-MY",
}

# Codes that need a readable name in tool messages.
LANGUAGE_NAMES = {
    "zh": "Chinese",
    "ja": "Japanese",
    "ko": "Korean",
    "he": "Hebrew",
}


def _normalize_gender(value: str) -> str:
    """Normalizes a gender alias. Empty string if it is not recognized."""
    return GENDER_ALIASES.get((value or "").strip().lower(), "")


def _effective_gender(language: str) -> str:
    """Returns the voice gender to use for a given language.

    Returns "female", "male", or "" when the language has no curated voices
    and no gender has been forced.
    """
    if _forced_gender:
        return _forced_gender
    configured = _normalize_gender(VOICE_GENDER)
    if configured:
        return configured

    base = (language or "").lower().split("-")[0]
    # Curated languages default to the female voice unless told otherwise.
    return "female" if base in VOICES_BY_LANGUAGE else ""


def _language_name(language: str) -> str:
    """Readable name for an ISO code, for the tool messages."""
    base = (language or "").lower().split("-")[0]
    if base in LANGUAGE_NAMES:
        return f"{LANGUAGE_NAMES[base]} ({base})"
    return base or "unknown"


def _load_edge_catalog() -> dict[str, dict]:
    """Fetches and caches the edge-tts voice catalog."""
    global _catalog_loaded
    if _catalog_loaded:
        return _edge_catalog

    async def _fetch() -> dict[str, dict]:
        import edge_tts

        voices = await edge_tts.list_voices()
        return {v["ShortName"]: v for v in voices}

    try:
        _edge_catalog.update(asyncio.run(_fetch()))
        _catalog_loaded = True
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not download the voice catalog: %s", exc)
    return _edge_catalog


def _system_language() -> str:
    """Returns the system language in ISO format, empty if unavailable."""
    import locale

    for getter in (
        lambda: locale.getlocale()[0],
        lambda: locale.getdefaultlocale()[0],
        lambda: os.environ.get("LANG", "").split(".")[0],
    ):
        try:
            value = getter()
        except Exception:  # noqa: BLE001
            value = None
        if value:
            return value.replace("-", "_").split("_")[0].lower()
    return ""


def _detect_language(text: str) -> str:
    """Detects the language of the text. Reliability is limited.

    langdetect does well on a full sentence, but on very short text it can fail
    with total confidence: "Done." is identified as Czech with probability 1.0.
    That is why detection is opt-in through VOICE_LANGUAGE=auto and is never
    the default mechanism.
    """
    try:
        from langdetect import DetectorFactory, detect_langs
    except ImportError:
        return ""

    DetectorFactory.seed = 0
    try:
        candidates = detect_langs(text)
    except Exception:  # noqa: BLE001 - not enough features
        return ""
    return candidates[0].lang.split("-")[0].lower() if candidates else ""


def _resolve_language(text: str) -> str:
    """Resolves the utterance language following the configured precedence."""
    if _forced_language is not None and _forced_language.lower() != "auto":
        return _forced_language

    if _forced_language == "auto" or (
        _forced_language is None and VOICE_LANGUAGE.lower() == "auto"
    ):
        detected = _detect_language(text)
        if detected:
            return detected
        logger.warning(
            "Could not detect the text language; using '%s'.", DEFAULT_LANGUAGE
        )
        return DEFAULT_LANGUAGE

    if _forced_language is None and VOICE_LANGUAGE:
        return VOICE_LANGUAGE.replace("_", "-").lower()

    return _system_language() or DEFAULT_LANGUAGE


def _pick_by_gender(
    catalog: dict[str, dict], candidates: list[str], gender: str
) -> str | None:
    """From a list of voices, returns the first one matching the gender.

    If none matches, returns the first of the list so the user is never left
    without a voice.
    """
    if not candidates:
        return None
    if gender:
        for name in candidates:
            if catalog.get(name, {}).get("Gender", "").lower() == gender:
                return name
        logger.info(
            "None of the available voices in '%s' is %s; using the first one.",
            ",".join(candidates[:1]),
            gender,
        )
    return candidates[0]


def _edge_voice_for_language(
    language: str, gender: str = "", ignore_name: bool = False
) -> str:
    """Picks an edge-tts voice for the requested language and gender.

    If an explicit regional variant is requested ("pt-PT", "en-GB") that region
    is respected; if only the language is requested ("pt", "en") the preferred
    regional variant from the table is used.

    ignore_name skips the VOICE_NAME override. Only the tools use it, so they
    can show the user which alternatives would exist without that variable.
    """
    if VOICE_NAME and not ignore_name:
        return VOICE_NAME

    key = language.lower()
    base, _, region = key.partition("-")
    gender = gender or _effective_gender(key)

    catalog = _load_edge_catalog()
    if not catalog:
        # Offline: only the curated voice is usable.
        pair = VOICES_BY_LANGUAGE.get(base)
        if pair:
            return pair[GENDER_INDEX.get(gender, 0)]
        return DEFAULT_EDGE_VOICE

    by_locale: dict[str, list[str]] = {}
    for name, info in catalog.items():
        by_locale.setdefault(info.get("Locale", "").lower(), []).append(name)

    pair = VOICES_BY_LANGUAGE.get(base)
    index = GENDER_INDEX.get(gender, 0)
    curated = pair[index] if pair else None

    # An explicit region is respected before falling back to the language.
    if region and curated and curated in catalog:
        if catalog[curated].get("Locale", "").lower() == key:
            return curated

    if region:
        picked = _pick_by_gender(catalog, by_locale.get(key, []), gender)
        if picked:
            return picked

    if curated and curated in catalog:
        return curated

    regional = (LOCALE_BY_LANGUAGE.get(base) or "").lower()
    picked = _pick_by_gender(catalog, by_locale.get(regional, []), gender)
    if picked:
        return picked

    # Last resort: any voice of that language, in any region.
    every = [
        name
        for name, info in catalog.items()
        if info.get("Locale", "").split("-")[0].lower() == base
    ]
    picked = _pick_by_gender(catalog, every, gender)
    if picked:
        return picked

    logger.warning(
        "No edge-tts voices for '%s'; using '%s'.", language, DEFAULT_EDGE_VOICE
    )
    return DEFAULT_EDGE_VOICE


def _sapi5_voice_for_language(language: str, engine) -> str | None:
    """Picks a SAPI5 voice for the requested language, or None if none exists.

    Windows names its voices with a region code (TTS_MS_ES-ES_HELENA), so the
    exact regional variant is matched first and the bare language after that.
    """
    voices = engine.getProperty("voices")

    def matches(voice) -> bool:
        return language.lower() in f"{voice.id} {voice.name}".lower()

    if VOICE_NAME:
        target = VOICE_NAME.lower()
        for voice in voices:
            if target in f"{voice.id} {voice.name}".lower():
                return voice.id
        return None

    key = language.lower()
    base, _, region = key.partition("-")

    # Exact regional variant, if one was requested.
    if region:
        for voice in voices:
            if matches(voice):
                return voice.id

    for pattern in (f"{base}-", f"_{base}-", f" {base} "):
        for voice in voices:
            if pattern in f"{voice.id} {voice.name}".lower():
                return voice.id

    # Language as a standalone token in the identifier.
    for voice in voices:
        if base in f"{voice.id} {voice.name}".lower().replace("_", "-").split():
            return voice.id
    return None


# --------------------------------------------------------------------------
# sapi5 engine (offline)
# --------------------------------------------------------------------------
def _init_sapi5():
    """Creates the pyttsx3 engine. The voice is set per utterance by language.

    Anything the dependencies print while being imported is redirected to
    stderr. On a stdio MCP server stdout carries the JSON-RPC stream, so a stray
    print from a transitive dependency would corrupt the protocol handshake.
    """
    global _sapi5_engine
    with _lock:
        if _sapi5_engine is not None:
            return _sapi5_engine

        with contextlib.redirect_stdout(sys.stderr):
            import pyttsx3

            engine = pyttsx3.init()

        _sapi5_engine = engine
        return engine


def _apply_sapi5_voice(engine, language: str) -> None:
    """Sets the language voice on the engine, and only if it changed."""
    global _current_sapi5_voice

    target = _sapi5_voice_for_language(language, engine)
    if target is None:
        if _current_sapi5_voice is None:
            _current_sapi5_voice = engine.getProperty("voice")
        logger.warning(
            "SAPI5 has no voices for '%s'; keeping the current one.", language
        )
        return

    if target != _current_sapi5_voice:
        engine.setProperty("voice", target)
        _current_sapi5_voice = target


def _validate_voice_name() -> str:
    """Checks VOICE_NAME against the live catalog.

    Returns an empty string when the voice is usable, or a human readable
    reason why it is not. edge-tts also validates the name, but only once the
    audio has already been queued and a network round trip has been paid, which
    turns a typo into a cryptic late failure.
    """
    if not VOICE_NAME:
        return ""

    catalog = _load_edge_catalog()
    if not catalog:
        # Catalog unavailable (no network). Let edge-tts decide later.
        return ""

    if VOICE_NAME in catalog:
        return ""

    base = VOICE_NAME.split("-")[0].lower()
    suggestions = [
        name
        for name, info in catalog.items()
        if info.get("Locale", "").split("-")[0].lower() == base
    ]
    hint = ""
    if suggestions:
        hint = " Voices available for that language: " + ", ".join(sorted(suggestions)[:6])
    return (
        f"VOICE_NAME '{VOICE_NAME}' does not exist in the edge-tts catalog."
        f"{hint}"
    )


def _speak_sapi5(text: str) -> None:
    engine = _init_sapi5()
    _apply_sapi5_voice(engine, _resolve_language(text))
    engine.say(text)
    engine.runAndWait()
    # runAndWait can return before the synthesizer is done. Without this wait
    # the next message would interrupt the previous one.
    while engine.isBusy():
        time.sleep(0.1)


# --------------------------------------------------------------------------
# edge-tts engine (network, neural voices)
# --------------------------------------------------------------------------
async def _render_mp3_async(text: str, destination: str, voice: str) -> None:
    import edge_tts

    communication = edge_tts.Communicate(
        text,
        voice,
        rate=_normalize_percentage(VOICE_RATE),
        volume=_normalize_percentage(VOICE_VOLUME),
    )
    await communication.save(destination)


def _speak_edge(text: str) -> None:
    """Renders the MP3 with edge-tts and plays it."""
    language = _resolve_language(text)
    voice = _edge_voice_for_language(language)
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "summary.mp3")
        seconds = _timeout(PLAYBACK_TIMEOUT, 30)
        try:
            asyncio.run(asyncio.wait_for(_render_mp3_async(text, path, voice), seconds))
        except asyncio.TimeoutError:
            logger.warning("edge-tts timed out after %ds", seconds)
            return
        _play_mp3(path)


# --------------------------------------------------------------------------
# Worker thread
# --------------------------------------------------------------------------
def _voice_worker() -> None:
    """Consumes the queue and speaks each summary with the configured engine."""
    speak = _speak_edge if ENGINE == "edge" else _speak_sapi5
    max_age = _timeout(MAX_SUMMARY_AGE, 120)

    while True:
        item = _queue.get()
        if item is None:
            _queue.task_done()
            return

        text, enqueued_at = item
        try:
            if VOICE_MUTE:
                continue
            if max_age > 0 and time.monotonic() - enqueued_at > max_age:
                logger.info("Dropped a summary that waited too long to be spoken")
                continue
            speak(text)
        except Exception:  # noqa: BLE001 - speech must never take the server down
            # The summary can contain file names, error text or other sensitive
            # detail, so it is never written to the log. A short digest is
            # enough to correlate a log line with a call.
            logger.exception(
                "Could not play the summary (%d words, sha256 %s)",
                len(text.split()),
                hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:12],
            )
        finally:
            _queue.task_done()


_thread = threading.Thread(target=_voice_worker, name="voice", daemon=True)
_thread.start()


# --------------------------------------------------------------------------
# Tools
# --------------------------------------------------------------------------
@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=False,
        openWorldHint=True,
    )
)
def speak_summary(text: str) -> str:
    """
    Reads a summary out loud. Match its length to the work done: one short
    sentence for a small change, a fuller summary for a large task. First
    person, no literal code, no credentials or personal data.
    """
    cleaned = _sanitize(_redact(text))
    if not cleaned:
        return "Nothing played: empty summary."

    if ENGINE == "edge":
        problem = _validate_voice_name()
        if problem:
            return f"Nothing played: {problem}"

    max_queue = _timeout(MAX_QUEUE_SIZE, 20)
    refused = _admit(cleaned, max_queue)
    if refused:
        return refused

    words = cleaned.split()
    cap = _timeout(MAX_SUMMARY_WORDS, 400)
    truncated = False
    if cap > 0 and len(words) > cap:
        cleaned = " ".join(words[:cap]).rstrip(" ,;:.-")
        truncated = True
        logger.warning("Summary truncated to %d words (sent %d)", cap, len(words))

    _queue.put((cleaned, time.monotonic()))
    if truncated:
        return f"Played, truncated to {cap} words. Split the work for more detail."
    return "Muted." if VOICE_MUTE else "Played."


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    )
)
def list_voices() -> str:
    """
    Shows the language, gender and voice in use, and the alternatives for the
    current language. For the full edge-tts catalog, run:
    python -m edge_tts --list-voices
    """
    language = _resolve_language("")
    gender = _effective_gender(language) or "default"
    lines = [
        f"Engine: {ENGINE}",
        f"Language: {_language_name(language)}",
        f"Gender: {gender}",
    ]

    if ENGINE == "edge":
        lines.append(f"Voice: {_edge_voice_for_language(language, gender)}")
        if VOICE_NAME:
            lines.append(f"VOICE_NAME={VOICE_NAME} overrides all of the above.")
        lines.append(
            "Alternatives: female="
            f"{_edge_voice_for_language(language, 'female', ignore_name=True)} "
            f"male={_edge_voice_for_language(language, 'male', ignore_name=True)}"
        )
        lines.append(
            f"Curated languages: {len(VOICES_BY_LANGUAGE)}"
        )
        return "\n".join(lines)

    try:
        engine = _init_sapi5()
    except Exception as exc:  # noqa: BLE001
        lines.append(f"sapi5 unavailable on this platform: {exc}")
        return "\n".join(lines)

    current = engine.getProperty("voice")
    lines.append(f"Voice: {current}")
    lines.append("Installed:")
    for voice in engine.getProperty("voices"):
        marker = " *" if voice.id == current else ""
        lines.append(f"- {voice.id.split(chr(92))[-1]} | {voice.name}{marker}")
    return "\n".join(lines)


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )
)
def set_language(language: str = "", gender: str = "") -> str:
    """
    Changes the voice language and gender, for when the user asks to speak
    another language or use a different voice.

    language: ISO code ("es", "en", "pt-BR"), "system" to follow the OS, or
    "auto" to detect it per summary. Empty keeps the current one.
    gender: "f"/"m", or "any" to follow the language default. Empty keeps the
    current one.
    """
    global _forced_language, _forced_gender

    raw_gender = (gender or "").strip()
    if raw_gender and raw_gender.lower() not in ("any", "auto"):
        if not _normalize_gender(raw_gender):
            return f"Unknown gender: {gender}. Use 'f', 'm' or 'any'."
        _forced_gender = _normalize_gender(raw_gender)
    elif raw_gender:
        _forced_gender = ""

    value = (language or "").strip()
    if not value:
        pass
    elif value.lower() == "system":
        _forced_language = None
    elif value.lower() == "auto":
        _forced_language = "auto"
    else:
        value = value.replace("_", "-")
        if not value.replace("-", "").isalnum():
            return f"Invalid language: {language}. Try 'es', 'en' or 'pt-BR'."
        _forced_language = value

    effective_language = _resolve_language("")
    effective_gender = _forced_gender or _normalize_gender(VOICE_GENDER)

    if ENGINE != "edge":
        return (
            f"Language {_language_name(effective_language)}, gender "
            f"{effective_gender or 'default'}. sapi5 only picks by language, and "
            f"only if that voice is installed."
        )

    voice = _edge_voice_for_language(effective_language, effective_gender)
    state = f"Language {_language_name(effective_language)}, gender {effective_gender or 'default'}"
    if VOICE_NAME:
        wanted = _edge_voice_for_language(
            effective_language, effective_gender, ignore_name=True
        )
        return (
            f"{state}. But VOICE_NAME pins the voice to {VOICE_NAME}, so you will "
            f"hear that instead; {wanted} would be used without it."
        )
    return f"{state}. Voice: {voice}."


if __name__ == "__main__":
    mcp.run()
