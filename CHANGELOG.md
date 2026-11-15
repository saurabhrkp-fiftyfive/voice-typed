# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project aims to
follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed
- **F8 `ENHANCE_SYSTEM` rewritten for fidelity.** The old prompt forced every
  output into `Task: … | Context: … | Expected output: …`, which made the model
  manufacture section content it was never told ("Expected output: a report
  with restart counts and timestamps", "Constraints: ready by Friday") and
  narrate the speaker in third person ("The user needs to share a reply…").
  Benchmarked on 23 reconstructed F8 dictations: invented content fell from
  **44.8% → 0%** of output tokens and detail retention went **80.9% → 100%** of
  source content terms. The new prompt keeps every dictated detail (names, IDs,
  branches, error strings, reasons, asides, hedges), forbids invented labels and
  third-person narration, and keeps deictic references ("this PR", "the above
  file") intact. `voice-typed preview "…"` now shows the rewrite, engine and
  latency without typing into a window.

### Added
- STT bias prompt now steers transcription toward Latin-script Hinglish and
  tells the model to keep every word from both languages — fixes Hindi+English
  mixed speech losing one language, and Hindi being written in Devanagari/Urdu
  script.

### Fixed
- An engine whose key is out of credit, revoked or rate-limited is benched
  instead of being retried first on every utterance. An exhausted OpenAI balance
  was adding a failed round trip (0.5–1.6 s) to every F8 before falling back to
  Groq. The serving engine is now journaled with each rewrite.
- `voice-typed doctor` no longer reports "API key configured ❌" on a working
  install: it read the module-level paths without the legacy
  `~/.config/secrets.env` fallback, which only `run` applied. `calibrate` and
  `config` had the same gap; every subcommand now resolves paths.
- Groq enhance fallback no longer 404s: `llama-3.3-70b-versatile` was
  decommissioned by Groq, so F6/F7/F8 failed on any box without an
  `OPENAI_API_KEY`. Now uses `openai/gpt-oss-120b`.
- Keys written by an older install (`~/.config/secrets.env`) are still read
  after the move to `$XDG_CONFIG_HOME/voice-typed/secrets.env`.
- `install.sh` installs `python3-tomli` on Python < 3.11, which the new
  `tomllib` fallback needs.
- Runs on Python 3.10 (`tomllib` fallback) and records via `parecord` on hosts
  where PipeWire exposes no capture node. (#1)
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
