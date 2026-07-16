# Security Policy

## Threat model, in one paragraph

voice-typed is a local daemon that listens for a held hotkey, records audio only
while the key is held, transcribes it once (locally or via a third-party STT
provider you configure), optionally rewrites it with an LLM grounded in a
screenshot of the active window, and types the result into the focused window via
`xdotool`. It reads global key events (`evdev`), captures screenshots, and can
send audio and screen context to external providers **only when you opt in with
your own API key**. Its web config panel (`config_server.py`) **binds
`127.0.0.1` only** and is **token-gated**. There is no always-on listening, no
wake word, and no standing transcript log — audio is deleted after transcription.

## What to know when running it

- **Global input access.** Reading `evdev` key events requires membership in the
  `input` group (or root). Any process with that access can observe keystrokes;
  keep the machine trusted.
- **Screenshots + STT egress.** In enhance/chat modes voice-typed captures the
  active window and may send audio and that image to your configured provider
  (OpenAI / Groq). Use the local Whisper option if you need zero egress. Don't
  dictate secrets on screens you would not send to a third party.
- **API keys live in a local `.env`** (gitignored) — never commit them. Rotate a
  key immediately if it is exposed.
- **Config panel is localhost + token.** Do not change the bind address away from
  `127.0.0.1`; the panel has no auth beyond the loopback + token guard.
- **`flagged.md` may contain dictated utterances** and is gitignored — treat it
  as private.

## Supported versions

This is a small single-maintainer project; only the latest `main` is supported.
Please update to the latest commit before reporting an issue.

## Reporting a vulnerability

If you believe you have found a security issue:

1. **Preferred:** use GitHub's private
   ["Report a vulnerability"](https://github.com/saurabhrkp-fiftyfive/voice-typed/security/advisories/new)
   flow (Security → Advisories) so details stay private until a fix is out.
2. If that is unavailable, open a **minimal** public issue that says a security
   report is coming, without exploit details, and request a private channel.

Please do **not** disclose exploit details in public issues before a fix is
available. I aim to acknowledge reports within a few days. As an unfunded
open-source project there is no bug bounty, but credit is gladly given in the
changelog unless you prefer to remain anonymous.
