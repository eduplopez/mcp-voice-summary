# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] - 2026-10-04

First public release.

### Added

- MCP server over stdio exposing `speak_summary`, `list_voices` and
  `set_language`.
- Two synthesis engines: `sapi5` (offline, native voices) and `edge`
  (edge-tts neural voices, 142 locales, 34 languages with curated female and
  male voices, automatic selection from the system language).
- Automatic voice gender and regional variant selection, changeable at runtime.
- Configurable summary length by the assistant, with `MAX_SUMMARY_WORDS` as a
  safety cap rather than a recommendation.
- Cross-platform playback: MCI on Windows, `afplay` on macOS, and `ffplay`,
  `mpg123`, `cvlc` or `paplay` on Linux, with `VOICE_PLAYER` override.
- Silent mode (`VOICE_MUTE`) and generic message mode (`VOICE_MODE=short`).

### Security

- Credential redaction before text is spoken or sent anywhere, using
  single-pass span merging so the function is idempotent. Covers private key
  blocks, common provider tokens, JWTs, `key=value` secrets and emails.
- Summaries are never written to the log; failures log only the word count and
  a short digest.
- Child processes get `stdin=DEVNULL` so they cannot drain the MCP JSON-RPC
  stream, plus a timeout on both network synthesis and playback.
- `stdout` is reserved for the protocol: anything the synthesizer prints is
  redirected to `stderr`.
- Abuse bounded on three axes: queue size, per-minute rate limit, and
  consecutive-duplicate suppression, all under one lock.
- Stale queued summaries are dropped instead of read out minutes late.
- `VOICE_NAME` is validated against the live catalog before anything is queued.

### Tooling

- 41 unit tests covering input handling, queue limits, voice resolution,
  platform fallbacks, log privacy, the token budget and the redactor.
- `pre_release_check.py` gate running tests, `pip-audit`, stdout purity, a
  dangerous-call scan, the tool catalog budget and a counter privacy check.
- `register_voices_onecore.ps1` to expose Microsoft Laura and Pablo to SAPI5.
- GitHub Actions CI across Linux, Windows and macOS.

### Known limitations

- Voice and language are process-global, not per session.
- Redaction is defensive and cannot catch every secret shape; do not rely on it
  as the only control for highly sensitive data.
- Unexpected tool arguments are ignored rather than rejected, because the SDK
  derives the schema from the function signature. Verified harmless: no
  model-supplied argument reaches the network, the filesystem or a command line.