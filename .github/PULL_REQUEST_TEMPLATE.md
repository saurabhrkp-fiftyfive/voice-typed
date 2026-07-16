## What this changes

Briefly describe the change and link the issue it addresses (`Closes #...`).

## Why

The motivation / use case.

## Checklist

- [ ] No unnecessary new runtime dependencies
- [ ] No always-on listening / standing transcript introduced (audio still deleted after transcription)
- [ ] Config server still binds `127.0.0.1` only and stays token-gated
- [ ] No secrets committed (API keys stay in a local `.env`)
- [ ] `python -m pytest test_voice_typed.py test_config_server.py -q` passes
- [ ] `shellcheck install.sh` passes (if the installer changed)
- [ ] Docs updated if behavior changed
- [ ] I agree to license my contribution under the project's MIT License
