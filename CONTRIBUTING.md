# Contributing to voice-typed

Thanks for your interest! voice-typed is a small, focused dictation daemon, and
the guiding principle is **stay lean and private-by-default**: hold-to-talk only,
no always-on listening, no standing transcript log. Contributions that keep that
spirit are very welcome.

## Ways to contribute

- **Report a bug** — open an issue with steps to reproduce (see the bug template).
- **Suggest a feature** — open an issue describing the use case first. Because the
  project prizes a small surface, please explain why it belongs in core.
- **Improve docs** — clearer setup, troubleshooting, and platform notes help
  everyone (X11 / GNOME specifics especially).
- **Send a PR** — for anything beyond a typo, open (or comment on) an issue first
  so we can agree on the approach.

## Ground rules

1. **Keep dependencies minimal.** Runtime is Python 3.11+ with `evdev`,
   `requests`, and `pillow`, plus system tools (`xdotool`, `xclip`). Don't add a
   dependency without a strong reason.
2. **Private by default.** No always-on listening, no wake word, no standing
   transcript. Audio is recorded only while a key is held, transcribed once,
   then deleted. Keep it that way.
3. **Config server stays localhost-only.** `config_server.py` must bind
   `127.0.0.1` only and stay token-gated. Security posture is a feature.
4. **No secrets in the repo.** API keys live in a local `.env` (gitignored),
   never committed. Personal word lists start from the `.template` files.
5. **Match the existing style.** Small, direct, comment-only-where-needed.

## Development setup

```bash
git clone https://github.com/saurabhrkp-fiftyfive/voice-typed.git
cd voice-typed
sudo apt-get install -y xdotool xclip python3-evdev python3-requests python3-pil python3-pytest
cp vocab.txt.template vocab.txt
cp corrections.txt.template corrections.txt
# set your STT provider key in a local .env (see README)
```

## Before you open a PR

Run the same checks CI runs:

```bash
python -m pytest test_voice_typed.py test_config_server.py -q   # tests pass
shellcheck install.sh                                           # installer lint
```

## Commit and PR conventions

- Use clear, imperative commit subjects (e.g. `fix: drop audio buffer on release`).
- Reference the issue you are addressing.
- Keep PRs focused — one concern per PR.
- By contributing, you agree your contributions are licensed under the
  project's [MIT License](LICENSE).

## Code of Conduct

Participation is governed by our [Code of Conduct](CODE_OF_CONDUCT.md). Please
read it before contributing.
