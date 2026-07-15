# voice-typed — durable memory & gotchas

Portable snapshot of the operational knowledge for this daemon (distilled from
the maintainer's private notes so the repo is self-contained). Point-in-time as
of 2026-07-15 — verify against code before treating as law.

## What it is

Hold-to-talk dictation daemon (X11/GNOME): hold a key → speak → release → text
typed into the focused window. Shipped + e2e-verified 2026-07-05. Single-file
daemon (`voice_typed.py`), own git repo, systemd `--user` unit.

## Models

- **STT:** OpenAI `gpt-4o-transcribe` → Groq `whisper-large-v3` fallback.
  gpt-4o-transcribe measured ~2.5% WER vs Groq ~7.5% on this workload, hence
  primary.
- **Enhance:** OpenAI `gpt-4o-mini` (vision-capable) → Groq
  `llama-3.3-70b-versatile` fallback (text-only, no vision). Override model with
  `VOICE_TYPED_ENHANCE_MODEL`.
- Keys from `~/.config/secrets.env` (`OPENAI_API_KEY`, `GROQ_API_KEY`) — external
  to the repo.

## Config files (hot-reloaded, no restart)

- **vocab.txt** — domain words/names, one per line. Two jobs: (1) STT `prompt`
  bias so names transcribe correctly; (2) appended to the enhance system prompt
  as a spelling guard so the LLM does not re-spell them. Cap ~800 chars.
- **corrections.txt** — `wrong => right`, deterministic replace, applied both
  pre- and post-enhance. Active quirk: `groq => grok` (user-chosen; dictated
  "Groq" always becomes "grok" — hand-fix real Groq-API mentions).
- **flagged.md** — F10 appends the last utterance for later correction; fill the
  text after `→`, then move the pair into `corrections.txt`. Flag-only privacy:
  there is **no** standing transcript log. (Runtime file is gitignored.)

## Gotchas

- **input group:** daemon needs read access to `/dev/input` (evdev). Installer
  runs `usermod -aG input`; you must **log out/in** — a stale session shows "no
  readable keyboard" and crash-loops (evdev `list_devices` hides unreadable
  nodes, so `denied=0` is misleading).
- **X11 key grab:** F9 (verbatim) still leaks to the focused app. The enhance
  keys **F8/F7/F6 are X11-grabbed** (via python-xlib) so apps never see them —
  resolves conflicts with app shortcuts (e.g. a TUI's own F8). `VOICE_TYPED_GRAB=0`
  disables. Needs `python3-xlib`.
- **Paste vs type:** enhanced / multi-line output is injected via clipboard
  paste (`xdotool type` would send a Return per newline and submit early).
  `paste_chord()` picks `ctrl+shift+v` for terminals (where `ctrl+v` is
  verbatim-insert / image paste) else `ctrl+v`, by matching the active window's
  WM_CLASS against terminal hints.
- **No VAD:** silence can hallucinate a word occasionally (STT artifact) — a
  known v2 candidate.
- **Hotplug:** daemon rescans `/dev/input` on dir-mtime change, so a Bluetooth
  reconnect or dongle re-plug is picked up live (no restart). Was startup-only
  enumeration before 2026-07-12.
- **Wayland:** unsupported. This is an X11-only path (xdotool + x11grab).

## Screenshot / vision (F6, F7)

- Captures the **active window only** at keydown: `xdotool` geometry + `ffmpeg
  -f x11grab` region → Pillow downscale to 1568 px longest side.
- Best-effort — any failure returns `None` and the mode degrades to text-only
  enhance; the daemon never dies on a capture error.
- PNG deleted immediately after use. On F6/F7 the active-window image is sent to
  OpenAI (accepted trade-off; cheap on gpt-4o-mini).

## Name-drift fix (2026-07-15)

Enhance modes were losing name spellings (e.g. Ada, Linus): corrections ran
only **before** the LLM, which then re-spelled them freely. Fix = (a) vocab
spelling-guard fed into the enhance prompt, (b) `apply_corrections` re-run
**after** enhance as a deterministic safety net, (c) missing names added to
`vocab.txt`. If a name still drifts: add it to `vocab.txt` (one/line) or a
`wrong => right` pair in `corrections.txt` — both hot-reload.

## Tests

`python3 -m pytest test_voice_typed.py -q` — 60 pure unit tests (mocks for
network, subprocess, PIL). Live event-loop + real capture are manual checks.
