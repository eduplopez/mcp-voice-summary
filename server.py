"""
MCP voice summary server.

Exposes a tool that reads out loud a short summary of the work the assistant
just finished. Supports two synthesis engines, selected through environment
variables:

    VOICE_ENGINE = "sapi5" (default) | "edge"
    VOICE_NAME   = voice name or id (see list_voices)
    VOICE_RATE   = speed, 100 is normal, or relative ("+10%", "-15%")
    VOICE_VOLUME = volume, 100 is normal
    VOICE_GENDER = "female" | "male"
    VOICE_LANGUAGE = "" (system default) | "es" | "pt-BR" | "auto"

    MAX_SUMMARY_WORDS = safety cap on words per summary (400)

The summary length is not fixed by the server: the assistant decides it based
on how much work it did. MAX_SUMMARY_WORDS only prevents accidental
multi-minute announcements.

Engines:
- sapi5: pyttsx3 on the native system synthesizer. Offline and free.
  Uses SAPI5 on Windows, NSSpeech on Linux, NSSS on macOS.
- edge:  edge-tts (Azure neural voices). Much better quality, but needs an
  internet connection on every utterance.

Implementation notes:
- mcp 2.x: FastMCP was renamed to MCPServer (mcp.server.mcpserver).
- The engine is initialized once and used from a single worker thread.
  pyttsx3 is not thread-safe and COM is apartment-threaded: creating the
  engine in one thread and using it in another blocks the process forever.
- Requests are queued so two consecutive summaries never overlap.
- The worker thread is a daemon so it never blocks process shutdown.
- The pyttsx3, edge_tts and langdetect imports are lazy, so the server starts
  on any platform even if only one engine is available.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time

from mcp.server.mcpserver import MCPServer

mcp = MCPServer("VoiceSummaryServer")

logger = logging.getLogger("voice-summary")

ENGINE = os.environ.get("VOICE_ENGINE", "sapi5").strip().lower()
VOICE_NAME = os.environ.get("VOICE_NAME", "").strip()
VOICE_RATE = os.environ.get("VOICE_RATE", "100").strip()
VOICE_VOLUME = os.environ.get("VOICE_VOLUME", "100").strip()

# User-forced MP3 player. Empty means auto-detect.
VOICE_PLAYER = os.environ.get("VOICE_PLAYER", "").strip()

# Voice language. Values:
#   ""        detect from the system language (recommended)
#   "es"      force a language by ISO 639-1 code
#   "pt-BR"   force a language and a specific regional variant
#   "auto"    detect the language from the text of each summary
# Either one can be overridden at runtime through the set_language tool.
VOICE_LANGUAGE = os.environ.get("VOICE_LANGUAGE", "").strip()

# Voice gender. Values: "" (whatever the language maps to), "female" or "male".
# Also accepts "f"/"m". Only applies to the edge engine: SAPI5 does not expose
# the gender of its voices.
VOICE_GENDER = os.environ.get("VOICE_GENDER", "").strip().lower()

# Safety cap on words per summary. This is not a length recommendation: the
# assistant decides the length based on how much work it did. The cap only
# prevents accidental multi-minute announcements from an oversized `text`.
# Defaults to 400 words, roughly 2-3 minutes.
MAX_SUMMARY_WORDS = os.environ.get("MAX_SUMMARY_WORDS", "400").strip()

# Maximum number of summaries waiting to be spoken. The queue is otherwise
# unbounded, so a client looping over speak_summary faster than playback could
# grow it without limit and exhaust memory. Requests beyond the cap are
# rejected instead of dropped, so the caller always knows.
MAX_QUEUE_SIZE = os.environ.get("MAX_QUEUE_SIZE", "20").strip()

# Language used when no other can be determined. English has the widest voice
# coverage across both engines.
DEFAULT_LANGUAGE = "en"

# Fallback voice when the requested language has no voice at all.
DEFAULT_EDGE_VOICE = "es-ES-AlvaroNeural"

# Language forced at runtime through the set_language tool. None means "not
# forced": the normal precedence applies. The value "auto" delegates to
# detection over the text of each summary.
_forced_language: str | None = None

# Gender forced at runtime. Empty means "use the language default".
_forced_gender: str = ""

# edge-tts voice catalog, cached after the first fetch.
_edge_catalog: dict[str, dict] = {}
_catalog_loaded = False

# Voice currently active on the sapi5 engine.
_current_sapi5_voice = None  # type: ignore[assignment]

# Pending voice requests. The worker thread consumes them in order.
_queue: queue.Queue[str | None] = queue.Queue()

# Engine state, lazily initialized from the worker thread.
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
        subprocess.run(command, check=True)
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
# Preferred neural voice per language and gender: (female, male). The names
# have been verified against the real edge-tts catalog; if one ever stops
# existing, the lookup automatically falls back to any other voice with the
# same language and gender.
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

# Accepted aliases for the voice gender.
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

# Preferred regional variant per language, used when the curated voice does not
# exist and another one from the same language has to be picked.
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

# ISO 639-1 codes that need a readable name in tool messages, because the code
# alone would be confusing.
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
    # If the language has a curated voice pair, default to the female voice
    # unless the user asks otherwise.
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
        # Without a catalog only the curated voice can be used, if there is one.
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

    # 1. Curated voice for the language, if its regional variant is the one
    #    that was requested.
    if region and curated and curated in catalog:
        if catalog[curated].get("Locale", "").lower() == key:
            return curated

    # 2. Voice of the requested gender inside the requested region.
    if region:
        picked = _pick_by_gender(catalog, by_locale.get(key, []), gender)
        if picked:
            return picked

    # 3. Curated voice for the language with the requested gender.
    if curated and curated in catalog:
        return curated

    # 4. Preferred regional variant of the language.
    regional = (LOCALE_BY_LANGUAGE.get(base) or "").lower()
    picked = _pick_by_gender(catalog, by_locale.get(regional, []), gender)
    if picked:
        return picked

    # 5. Any voice of the language, in any regional variant.
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

    # 1. Exact regional variant, if one was requested.
    if region:
        for voice in voices:
            if matches(voice):
                return voice.id

    # 2. Any variant of the language.
    for pattern in (f"{base}-", f"_{base}-", f" {base} "):
        for voice in voices:
            if pattern in f"{voice.id} {voice.name}".lower():
                return voice.id

    # 3. The language appears as a standalone token in the identifier.
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
        asyncio.run(_render_mp3_async(text, path, voice))
        _play_mp3(path)


# --------------------------------------------------------------------------
# Worker thread
# --------------------------------------------------------------------------
def _voice_worker() -> None:
    """Consumes the queue and speaks each summary with the configured engine."""
    speak = _speak_edge if ENGINE == "edge" else _speak_sapi5

    while True:
        text = _queue.get()
        if text is None:
            _queue.task_done()
            return
        try:
            speak(text)
        except Exception:  # noqa: BLE001 - speech must never take the server down
            logger.exception("Could not play the summary: %s", text)
        finally:
            _queue.task_done()


_thread = threading.Thread(target=_voice_worker, name="voice", daemon=True)
_thread.start()


# --------------------------------------------------------------------------
# Tools
# --------------------------------------------------------------------------
@mcp.tool()
def speak_summary(text: str) -> str:
    """
    Reads a summary out loud through the system speakers.

    Match the length of the summary to how much work was done: one short
    sentence for a small change, and a fuller summary when the task was large
    or had several steps. Do not read out literal code or details that add no
    value; write in the first person.

    If the summary exceeds the safety cap (MAX_SUMMARY_WORDS, 400 by default)
    it is truncated automatically.

    The voice language is selected automatically: by default the system
    language, or whatever was set with set_language. There is no need to pass
    a language.
    """
    cleaned = " ".join(text.split()).strip()
    if not cleaned:
        return "Nothing was played: the summary was empty."

    if ENGINE == "edge":
        problem = _validate_voice_name()
        if problem:
            return f"Nothing was played: {problem}"

    try:
        max_queue = int(MAX_QUEUE_SIZE)
    except ValueError:
        logger.warning("Invalid MAX_QUEUE_SIZE: %s", MAX_QUEUE_SIZE)
        max_queue = 20

    if max_queue > 0 and _queue.qsize() >= max_queue:
        logger.warning(
            "Queue is full (%d pending); rejecting the request", max_queue
        )
        return (
            f"Nothing was played: {max_queue} summaries are already queued. "
            f"Wait for the queue to drain before sending more."
        )

    words = cleaned.split()
    truncated = False
    try:
        cap = int(MAX_SUMMARY_WORDS)
    except ValueError:
        logger.warning("Invalid MAX_SUMMARY_WORDS: %s", MAX_SUMMARY_WORDS)
        cap = 400

    if cap > 0 and len(words) > cap:
        cleaned = " ".join(words[:cap]).rstrip(" ,;:.-")
        truncated = True
        logger.warning("Summary truncated to %d words (sent %d)", cap, len(words))

    response = f"Playing summary successfully: {cleaned}"
    if truncated:
        response += (
            f" [Note: truncated to {cap} words for safety. Split the work into "
            f"several calls if you need more detail.]"
        )
    _queue.put(cleaned)
    return response


@mcp.tool()
def list_voices() -> str:
    """
    Shows which language and voice are in use right now, plus the synthesis
    voices installed on the system.
    """
    import sys

    language = _resolve_language("")
    language_origin = (
        "forced at runtime"
        if _forced_language is not None
        else VOICE_LANGUAGE or "system language"
    )
    lines = [
        f"Engine: {ENGINE}",
        f"Language: {_language_name(language)} (source: {language_origin})",
    ]

    if ENGINE == "edge":
        gender = _effective_gender(language) or "no preference"
        lines.append(f"Gender: {gender}")
        lines.append(f"Voice in use: {_edge_voice_for_language(language, gender)}")
        lines.append("")
        if VOICE_NAME:
            lines.append(
                f"WARNING: VOICE_NAME is set to {VOICE_NAME}, so it overrides the "
                f"automatic language and gender selection. These are the voices "
                f"that would be used without it:"
            )
        else:
            lines.append("Gender options for the current language:")
        for label, value in (("female", "female"), ("male", "male")):
            lines.append(
                f"- {label}: "
                f"{_edge_voice_for_language(language, value, ignore_name=True)}"
            )
        lines.append("")
        lines.append(
            "Languages with curated voices (female and male): "
            + ", ".join(sorted(VOICES_BY_LANGUAGE))
        )
        lines.append(
            "For the full catalog of neural voices run: "
            "python -m edge_tts --list-voices"
        )
        return "\n".join(lines)

    try:
        engine = _init_sapi5()
    except Exception as exc:  # noqa: BLE001
        lines.append(
            f"Could not initialize the sapi5 engine on {sys.platform}: {exc}. "
            f"Check the system voice dependencies or set VOICE_ENGINE to 'edge'."
        )
        return "\n".join(lines)

    current = engine.getProperty("voice")
    lines.append(f"Selected voice: {current}")
    lines.append("")
    lines.append("Installed voices:")
    for voice in engine.getProperty("voices"):
        marker = " (selected)" if voice.id == current else ""
        lines.append(f"- {voice.id.split(chr(92))[-1]} | {voice.name}{marker}")
    if sys.platform == "win32":
        lines.append("")
        lines.append(
            "Higher quality voices (Microsoft Laura, Microsoft Pablo) require "
            "registering the OneCore registry keys; run "
            "register_voices_onecore.ps1 as administrator."
        )
    return "\n".join(lines)


@mcp.tool()
def set_language(language: str, gender: str = "") -> str:
    """
    Sets the language and, optionally, the voice gender for the next
    utterances, without editing the configuration. Call this tool when the
    user asks to speak another language or use another voice, for example
    "speak in English", "switch to French" or "use a male voice".

    Accepts an ISO 639-1 code ("es", "en", "fr") or a code with a regional
    variant ("pt-BR", "en-GB"). Gender accepts "female"/"male" and "f"/"m". Use
    an empty string to go back to the system language, or "auto" to infer the
    language from the text of each summary.
    """
    global _forced_language, _forced_gender

    gender_value = _normalize_gender(gender)
    if gender.strip() and not gender_value:
        return (
            f"Unrecognized gender: {gender}. Use 'female' or 'male' "
            f"(short forms 'f' and 'm' also work)."
        )
    if gender_value:
        _forced_gender = gender_value

    value = (language or "").strip()
    if not value:
        _forced_language = None
        # Report the gender that will actually be used. Resetting the language
        # does not touch the gender, so claiming it is unforced here would be
        # wrong when set_gender was called earlier in the session.
        effective = _forced_gender or _normalize_gender(VOICE_GENDER)
        gender_text = (
            f", gender {effective} (still forced)" if effective else ", gender not forced"
        )
        return (
            f"Language reset to the system one: "
            f"{_language_name(_system_language() or DEFAULT_LANGUAGE)}{gender_text}. "
            f"Call set_gender('') to also reset the gender."
        )

    if value.lower() == "auto":
        _forced_language = "auto"
        return (
            "Automatic detection enabled. The language is inferred from the text "
            "of each summary. Keep in mind that on very short summaries detection "
            "can fail, because language libraries answer with confident errors. "
            "For a fixed language its code is always more reliable."
        )

    value = value.replace("_", "-")
    if not value.replace("-", "").isalnum():
        return f"Invalid language: {language}. Use a code like 'es', 'en' or 'pt-BR'."

    _forced_language = value

    if ENGINE == "edge":
        effective = gender_value or _effective_gender(value.lower())
        voice = _edge_voice_for_language(value.lower(), effective)
        return (
            f"Language set to {_language_name(value)}. "
            f"Voice selected: {voice} ({effective})."
        )
    warning = "" if gender_value else " Gender does not apply to sapi5."
    return (
        f"Language set to {_language_name(value)}. With the sapi5 engine the voice "
        f"for that language will be used if it is installed on the system.{warning}"
    )


@mcp.tool()
def set_gender(gender: str) -> str:
    """
    Changes the voice gender without touching the language. Call this tool when
    the user asks for a male or female voice, for example "use a male voice".

    Accepts "female"/"male" and "f"/"m". Use an empty string to go back to the
    default gender for the language.
    """
    global _forced_gender

    if not (gender or "").strip():
        _forced_gender = ""
        return "Gender reset to the default for the language."

    value = _normalize_gender(gender)
    if not value:
        return (
            f"Unrecognized gender: {gender}. Use 'female' or 'male' "
            f"(short forms 'f' and 'm' also work)."
        )

    _forced_gender = value
    if ENGINE != "edge":
        return (
            f"Gender set to {value}, but the sapi5 engine does not expose the gender "
            f"of its voices, so it will not be applied. Use the edge engine to "
            f"choose a voice by gender."
        )

    language = _resolve_language("")
    voice = _edge_voice_for_language(language, value)
    if VOICE_NAME:
        alternative = _edge_voice_for_language(language, value, ignore_name=True)
        return (
            f"Gender recorded as {value}, but VOICE_NAME still pins the voice to "
            f"{VOICE_NAME}, so the change will not be heard. Remove VOICE_NAME from "
            f"the configuration to choose by gender; {alternative} would be used."
        )
    return f"Gender set to {value}. Voice for {_language_name(language)}: {voice}."


if __name__ == "__main__":
    mcp.run()