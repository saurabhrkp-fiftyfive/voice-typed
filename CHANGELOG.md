# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project aims to
follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- STT bias prompt now steers transcription toward Latin-script Hinglish and
  tells the model to keep every word from both languages — fixes Hindi+English
  mixed speech losing one language, and Hindi being written in Devanagari/Urdu
  script.

### Fixed
- Romanization now also converts Urdu/Arabic-script Hindi to Roman Hinglish, not
  only Devanagari (`NONLATIN_RE`); Arabic-script output no longer passes through
  untouched.
- LLM refusals ("I cannot assist…") are never injected as text: both the enhance
  and transliterate steps detect refusal-style output and fall back to the raw
  transcript instead of typing the refusal.

## [0.1.0] - 2026-07-17

### Added
- Initial public release.
- `voice_typed.py` — hold-to-talk dictation daemon for X11 / GNOME: records
  while a key is held, transcribes once (OpenAI / Groq / local Whisper),
  optionally rewrites with an LLM grounded in a screenshot, types into the
  focused window via `xdotool`. No always-on listening, no standing transcript.
- Multiple modes on separate keys (verbatim / enhance / chat).
- `config_server.py` + `panel.html` — localhost-only, token-gated web config
  panel (engines, shortcuts, vocabulary, corrections, service).
- `install.sh` — installer; `voice-typed.service` systemd unit;
  `voice-typed-config.desktop` launcher.
- Vocabulary bias and correction lists via `vocab.txt` / `corrections.txt`
  (start from the `.template` files).
- Tests: `test_voice_typed.py`, `test_config_server.py`; CI (pytest + shellcheck).
- Documentation under `docs/` and community health files: README, LICENSE (MIT),
  Code of Conduct, contributing guide, security policy, issue/PR templates.

[Unreleased]: https://github.com/saurabhrkp-fiftyfive/voice-typed/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/saurabhrkp-fiftyfive/voice-typed/releases/tag/v0.1.0
