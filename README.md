# voice-typed

Hold-to-talk voice dictation daemon for **X11 / GNOME**. Hold a key, speak,
release → your speech is transcribed (and optionally rewritten by an LLM,
grounded in a screenshot of the active window) and typed into the focused
window via `xdotool`.

No always-on listening. No wake word. No standing transcript log. Audio is
recorded only while a key is held, transcribed once, then deleted.

---

## Keys / modes

| Key | Env var | Mode | LLM rewrite | Screenshot | System prompt |
|-----|---------|------|-------------|-----------|---------------|
| **F9** | `VOICE_TYPED_KEY` | verbatim | no | no | — (types transcript as-is) |
| **F8** | `VOICE_TYPED_ENHANCE_KEY` | new-task enhance | yes | no | `ENHANCE_SYSTEM` — rewrites ramble into a structured coding-agent prompt |
| **F7** | `VOICE_TYPED_FOLLOWUP_KEY` | follow-up enhance | yes | **yes** | `FOLLOWUP_SYSTEM` (+ grounding line) — continuation message for an active agent session |
| **F6** | `VOICE_TYPED_MSG_KEY` | chat message | yes | **yes** | `MSG_SYSTEM` — natural chat/DM reply grounded in on-screen thread |
| **F10** | `VOICE_TYPED_FLAG_KEY` | flag last | no | no | appends last utterance to `flagged.md` |

`SCREENSHOT_MODES = {"followup", "message"}` gates capture. F8/F9 never capture
(F8 is a new-task start point — screen is usually blank; F9 is raw dictation).

---

## API models & how they connect

Two independent OpenAI-compatible pipelines, each with a Groq fallback. Keys
read from `~/.config/secrets.env` (`OPENAI_API_KEY`, `GROQ_API_KEY`).

### 1. Speech-to-text (all modes)

```
WAV (16 kHz mono, pw-record)
  → OpenAI  POST /v1/audio/transcriptions   model gpt-4o-transcribe   [primary]
  → Groq    POST /v1/audio/transcriptions   model whisper-large-v3    [fallback]
```

- First engine with a configured key is tried; on any error the next engine
  runs. If all fail → notify + skip (nothing typed).
- **vocab bias:** `vocab.txt` (domain words / names) is sent as the STT `prompt`
  parameter to bias spelling. Hot-reloaded per utterance, capped ~800 chars.
- **Hindi -> Roman (all modes):** after transcription, any Devanagari in the
  text is transliterated to natural Roman Hindi (Hinglish) via the enhance chat
  model — e.g. `क्या हाल है` -> `kya haal hai`. Gated on a Devanagari check, so
  English-only dictation makes no extra call. Latin text, numbers, code, and
  English words pass through unchanged; on any API failure the original text is
  kept. Runs before enhance, so F6/F7/F8 also get Roman input.

### 2. Prompt/message enhance (F6, F7, F8 only)

```
transcript (+ optional screenshot)
  → OpenAI  POST /v1/chat/completions   model gpt-4o-mini   [primary, vision-capable]
  → Groq    POST /v1/chat/completions   model llama-3.3-70b-versatile   [fallback, TEXT-ONLY]
temperature 0.3
```

- Model overridable via `VOICE_TYPED_ENHANCE_MODEL` (default `gpt-4o-mini`).
- **Vision:** when a screenshot is present, the OpenAI request sends a vision
  content array — `[{type:text, text:<transcript>}, {type:image_url, image_url:{url:"data:image/png;base64,…"}}]`.
  The Groq fallback is text-only (llama-3.3-70b has no vision) and silently
  drops the image, so the worst case equals plain text enhance.
- **Spelling guard:** the `vocab.txt` terms are appended to the enhance system
  prompt ("Preserve the EXACT spelling of these names/terms…") so the LLM does
  not re-spell names during the rewrite.
- **Correction safety net:** `corrections.txt` (`wrong => right`) is applied
  deterministically **after** the LLM rewrite too — the LLM cannot reintroduce a
  known misspelling.
- On enhance failure the **raw (already corrected) transcript** is typed — text
  is never lost.

---

## Screenshot / vision pipeline (F6 & F7)

Captured at **keydown**, after the recorder starts (so audio isn't delayed):

```
xdotool getactivewindow                         → window id
xdotool getwindowgeometry --shell <id>          → X Y WIDTH HEIGHT
ffmpeg -f x11grab -video_size WxH -i :D+X,Y ...  → active-window PNG
Pillow thumbnail → longest side SHOT_MAX_PX (1568 px)  → re-saved PNG
```

- **Active window only** (not full screen) — less noise, fewer tokens, more
  privacy.
- **Best-effort:** any failure (geometry, ffmpeg, PIL) logs and returns `None`;
  the mode degrades to text-only enhance. The daemon never crashes on a
  screenshot error.
- The PNG is deleted immediately after use (in `handle_utterance`'s `finally`,
  alongside the WAV).
- 1568 px keeps on-screen text (chat threads, terminal output) legible to the
  vision model.

**Privacy/cost note:** on F6/F7 a screenshot of the active window leaves the
machine (sent to OpenAI). Cheap on `gpt-4o-mini`, deleted locally right after.

---

## Data flow

```
F6/F7 keydown
  → start_recording(wav)
  → capture_active_window(png)          # only if mode in SCREENSHOT_MODES
keyup (or MAX_UTTERANCE cap)
  → queue.put((wav, active_window_id, mode, png))
worker thread
  → transcribe(wav)                     # STT chain
  → apply_corrections(text)             # deterministic, pre-enhance
  → enhance_prompt(text, mode, image_path=png)   # LLM (+vision) chain
  → apply_corrections(text)             # deterministic, post-enhance
  → focus guard (drop if active window changed since keydown)
  → inject(text)                        # xdotool type, or clipboard paste for enhanced/multi-line
  finally → unlink wav + png
```

---

## Configuration files (same dir, all hot-reloaded)

| File | Purpose |
|------|---------|
| `vocab.txt` | Domain words / names, one per line (`#` comments). STT spelling bias **and** enhance spelling guard. Cap ~80 entries / 800 chars. |
| `corrections.txt` | `wrong => right` pairs, `#` comments. Deterministic post-STT + post-enhance replace (case-insensitive, word-boundary). |
| `flagged.md` | F10 log of bad transcripts for later correction. Runtime file — **gitignored**; `flagged.md.template` shows the format. |
| `~/.config/secrets.env` | `OPENAI_API_KEY` / `GROQ_API_KEY` (`export KEY=value` lines). **Not** in this repo. |

### Environment variables

| Var | Default | Meaning |
|-----|---------|---------|
| `VOICE_TYPED_KEY` | `KEY_F9` | verbatim dictation key (evdev name) |
| `VOICE_TYPED_ENHANCE_KEY` | `KEY_F8` | new-task enhance key |
| `VOICE_TYPED_FOLLOWUP_KEY` | `KEY_F7` | follow-up enhance key (+screenshot) |
| `VOICE_TYPED_MSG_KEY` | `KEY_F6` | chat-message key (+screenshot) |
| `VOICE_TYPED_FLAG_KEY` | `KEY_F10` | flag-last key |
| `VOICE_TYPED_ENHANCE_MODEL` | `gpt-4o-mini` | enhance model override |
| `VOICE_TYPED_PASTE` | unset | `1` = always clipboard-paste instead of typing |
| `VOICE_TYPED_GRAB` | `1` | `0` disables X11 key-grab (enhance keys otherwise swallowed from apps) |

---

## Install

```bash
./install.sh          # apt deps, input-group, tests, systemd user unit, enable+start
```

Deps: `xdotool xclip python3-evdev python3-requests python3-pil python3-pytest
pipewire-bin libnotify-bin ffmpeg`. Requires membership of the `input` group
(installer adds it — **log out/in** after first install).

Service:

```bash
systemctl --user status  voice-typed
systemctl --user restart voice-typed
journalctl --user -u voice-typed -f
```

---

## Tests

```bash
python3 -m pytest test_voice_typed.py -q      # 60 tests
```

Pure unit tests — `requests.post`, `subprocess`, and PIL paths are mocked; no
network, no audio device, no real key grab. The live event loop + real capture
are verified manually (hold a key and speak).

---

## Repo layout / what's here

```
voice_typed.py        # the daemon (single file)
test_voice_typed.py   # pytest suite (60 tests)
install.sh            # installer
voice-typed.service   # systemd --user unit
vocab.txt             # STT bias + enhance spelling guard
corrections.txt       # deterministic wrong=>right fixes
flagged.md.template   # F10-log format example (runtime flagged.md is gitignored)
docs/
  2026-07-15-screen-context-dictation-design.md   # F6/screenshot feature design
  2026-07-15-screen-context-dictation-plan.md      # its TDD implementation plan
  MEMORY.md                                         # durable facts / gotchas (portable)
README.md
```

## Feature history (what has been built)

- **2026-07-05** — shipped: hold-F9 verbatim dictation, STT fallback chain,
  `vocab.txt` bias, systemd unit.
- **2026-07-11** — F8 enhance mode (rewrite ramble → structured coding prompt);
  clipboard-paste for multi-line; terminal-aware paste chord.
- **2026-07-12** — hotplug rescan: `/dev/input` re-enumerated on dir-mtime
  change (BT reconnect / dongle re-plug picked up live).
- **2026-07-15** — F7 follow-up + F6 chat-message modes with **active-window
  screenshot → vision grounding** (gpt-4o-mini). Then: enhance spelling guard,
  post-enhance re-correction, screenshot cap raised to 1568 px.

See `docs/MEMORY.md` for the full gotcha list.
