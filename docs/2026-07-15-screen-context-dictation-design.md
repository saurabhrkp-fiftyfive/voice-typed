# Screen-Context Dictation — Design

**Date:** 2026-07-15
**Status:** approved
**Component:** `voice_typed.py` (hold-to-talk dictation daemon)

## Problem

Dictation on F7 (coding follow-up) is blind to what's on screen. A follow-up
like "fix that error" or "reply to him" needs the visible context (Claude's
output, a chat thread) to be written well. Today the model only sees the raw
transcript.

Add a step: for context-dependent modes, capture the active window as a
screenshot at record-start, send it alongside the transcript to a vision
model, and let the on-screen content ground the rewritten message.

## Scope decisions (locked)

- **F9** verbatim dictation — **unchanged** (no screenshot, no model).
- **F8** `task` enhance — **unchanged**, no screenshot. It is the new-task
  start point; the screen is typically blank, so a screenshot adds noise.
- **F7** `followup` enhance — **now captures a screenshot**. Target is an
  active coding session with Claude's output visible.
- **F6** (new) `message` mode — captures a screenshot, writes a generic
  message grounded in on-screen context. Target is a Teams/Discord/chat window.
- **F10** flag — unchanged.
- Capture = **active window only** (not full screen): less noise, fewer
  tokens, more privacy.

## Key / mode map

| Key | Env | Mode | Screenshot | System prompt |
|-----|-----|------|------------|---------------|
| F9 | `VOICE_TYPED_KEY` | `""` (plain) | no | — (verbatim) |
| F8 | `VOICE_TYPED_ENHANCE_KEY` | `task` | no | `ENHANCE_SYSTEM` |
| F7 | `VOICE_TYPED_FOLLOWUP_KEY` | `followup` | **yes** | `FOLLOWUP_SYSTEM` (+ grounding line when image) |
| F6 | `VOICE_TYPED_MSG_KEY` (new, `KEY_F6`) | `message` (new) | **yes** | `MSG_SYSTEM` (new) |
| F10 | `VOICE_TYPED_FLAG_KEY` | — | no | — |

`SCREENSHOT_MODES = {"followup", "message"}` gates capture.

## Components

### 1. `capture_active_window(png_path) -> Path | None`
- Runs at **keydown, after `start_recording`** so audio capture isn't delayed.
- Geometry: `xdotool getwindowgeometry --shell <wid>` → `X Y WIDTH HEIGHT`.
- Grab: `ffmpeg -f x11grab -video_size WxH -i :0.0+X,Y -frames:v 1 -y <png>`.
- Downscale: Pillow `Image.thumbnail((1024, 1024))`, re-save PNG (caps vision
  tokens ~1 tile).
- Best-effort: any failure → log + `notify`, return `None`. Daemon never dies
  on a screenshot error; the mode degrades to text-only enhance.
- No new installs: `ffmpeg`, `xdotool`, Pillow 10.2 all already present. X11
  session (`XDG_SESSION_TYPE=x11`).

### 2. `MSG_SYSTEM` (new prompt for `message` mode)
- Dictation is the source of truth for **what to say**.
- Screenshot is context **only** to ground references (who "him" is, the
  topic, names, the question being answered) — never invent content.
- Completeness rule (carry over everything said, fix grammar, strip filler),
  same discipline as existing prompts.
- Natural human chat/DM tone, not a report.
- One physical line, output only the message (no preamble/fences).

### 3. `enhance_prompt(text, mode="task", timeout=..., image_path=None)`
- New optional `image_path`.
- When `image_path` set and readable: encode base64, and for the **OpenAI**
  engine build a vision content array:
  `[{"type":"text","text":...}, {"type":"image_url","image_url":{"url":"data:image/png;base64,<b64>"}}]`.
- **Groq** engine stays **text-only** (llama-3.3-70b has no vision) — it drops
  the image. Worst-case fallback == today's behavior.
- `message` mode selects `MSG_SYSTEM`. `followup`/`task` unchanged, except when
  an image is attached a short grounding line is prepended to the system prompt
  (e.g. "A screenshot of the current screen is attached; use it to ground
  references — visible output, errors, filenames, names.").
- `_chat_request` gains an optional `image_b64` param; builds the content array
  when present, plain string otherwise.

### 4. Queue plumbing
- `q.put((wav, window_id, enhance, shot_path))` — one added tuple element.
- `stt_worker` unpacks 4-tuple; `handle_utterance(wav, window_id, enhance,
  shot_path)` passes `shot_path` into `enhance_prompt(..., image_path=shot_path)`
  only when `enhance` set.
- `shot_path` unlinked in `handle_utterance`'s `finally`, alongside the wav.

### 5. Key handling in `main()`
- Parse `VOICE_TYPED_MSG_KEY` (default `KEY_F6`) → `msgcode`, same validation
  pattern as existing keys.
- `mode_for(code)`: add `msgcode → "message"`.
- Add `msgcode` to the accepted-codes tuple in the event filter.
- X11-grab F6 (like F7/F8) so it doesn't fire app shortcuts.
- At keydown, when `mode_for(ev.code) in SCREENSHOT_MODES`: capture screenshot
  into `run_dir` after starting the recorder; store `shot` alongside `wav`.
- Thread `shot` through both keyup and the MAX_UTTERANCE cap `q.put` calls.
- Cap-path and focus-change-drop path must also unlink the shot (finally in
  `handle_utterance` covers the normal + drop paths; cap path enqueues so it's
  covered too).

## Data flow

```
F6/F7 keydown
  → start_recording(wav)
  → capture_active_window(png)        # only if mode in SCREENSHOT_MODES
keyup / cap
  → q.put((wav, active_window(), mode, png))
worker
  → transcribe(wav)
  → apply_corrections
  → enhance_prompt(text, mode, image_path=png)   # vision if png present
  → focus guard (drop if active window changed)
  → inject (paste)
  finally → unlink wav + png
```

## Error handling

- Screenshot capture fail → `None` → text-only enhance (no crash).
- Vision request fail on OpenAI → Groq text-only fallback (existing loop).
- Transcribe fail → notify, skip (existing).
- Focus changed between record and inject → drop (existing guard), files unlinked.

## Testing (TDD)

- `mode_for`: F6 → `"message"`, F7 → `"followup"`, F8 → `"task"`, F9 → `""`.
- `SCREENSHOT_MODES` gating: `followup`/`message` in, `task`/`""` out.
- `enhance_prompt(image_path=...)`: OpenAI request body contains the vision
  content array with a `data:image/png;base64,` URL; Groq request stays a
  plain string (mock `requests.post`).
- `MSG_SYSTEM` selected for `message` mode.
- `capture_active_window`: mock `subprocess` for xdotool+ffmpeg — returns path
  on success; returns `None` when ffmpeg exits non-zero or xdotool fails.
- `handle_utterance`: unlinks the png in `finally` (mock, assert unlink called).

## Non-goals

- No full-screen capture. No OCR. No screenshot history/persistence.
- No change to F8/F9 behavior. No Wayland path (session is X11).
- No new external dependency.

## Cost / privacy

Screenshots of the active window (Teams / Discord / Claude) are sent to OpenAI
on F6/F7. Cheap on `gpt-4o-mini`, but the screen content leaves the machine —
accepted trade-off, noted here explicitly. Screenshots are deleted immediately
after use.
