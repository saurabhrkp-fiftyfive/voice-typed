# Screen-Context Dictation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Capture the active window as a screenshot on F7 (coding follow-up) and a new F6 (chat message) mode, and feed it to a vision model so dictated messages are grounded in what's on screen.

**Architecture:** At record-start, for screenshot-modes only, grab the focused window via `xdotool` geometry + `ffmpeg -f x11grab`, downscale with Pillow, and thread the PNG path through the existing STT queue into `enhance_prompt`, which sends it to OpenAI `gpt-4o-mini` as a vision content array. Groq stays text-only (no vision). All new behavior degrades to today's text-only path on any failure.

**Tech Stack:** Python 3 (stdlib + `requests`, `Pillow`, `evdev`, `Xlib`), `xdotool`, `ffmpeg` (x11grab), `pytest`. X11 GNOME session.

## Global Constraints

- Single file under change: `voice_typed.py`; tests in `test_voice_typed.py`. No new modules.
- No new external dependency beyond `ffmpeg` + `Pillow`, both already installed.
- Daemon must never crash on a screenshot/vision failure — every new path degrades to text-only enhance (today's behavior).
- Screenshots and wavs are deleted immediately after use.
- Screenshot capture only for modes in `SCREENSHOT_MODES = {"followup", "message"}`. F8 (`task`) and F9 (plain) never capture.
- New key: `VOICE_TYPED_MSG_KEY`, default `KEY_F6`, mode `"message"`.
- Match existing code style: `# noqa: BLE001` on broad guards, lazy imports for `PIL`, module-level constants near existing ones, `print(..., flush=True)` for daemon logs.
- Run all tests with: `cd ~/scripts/voice-typed && python3 -m pytest test_voice_typed.py -q`.

---

### Task 1: Mode routing (constants + testable mapping)

Extract the key→mode mapping out of `main()`'s nested `mode_for` (currently untestable) into a module-level function, and add the screenshot-mode gating set.

**Files:**
- Modify: `voice_typed.py` (add constants near line 56; add `mode_for_code` near other top-level helpers, e.g. after `enhance_prompt`)
- Test: `test_voice_typed.py`

**Interfaces:**
- Produces: `SCREENSHOT_MODES = {"followup", "message"}`; `mode_for_code(code, enhcode, folcode, msgcode) -> str` returning `"message"|"task"|"followup"|""`.

- [ ] **Step 1: Write the failing tests**

Append to `test_voice_typed.py`:

```python
def test_mode_for_code_maps_each_key():
    assert vt.mode_for_code(66, 65, 63, 67, ) == ""  # placeholder — replaced below
```

Replace that stub with the real tests (use symbolic args, not real keycodes):

```python
def test_mode_for_code_maps_each_key():
    ENH, FOL, MSG = 65, 63, 61
    assert vt.mode_for_code(MSG, ENH, FOL, MSG) == "message"
    assert vt.mode_for_code(ENH, ENH, FOL, MSG) == "task"
    assert vt.mode_for_code(FOL, ENH, FOL, MSG) == "followup"
    assert vt.mode_for_code(999, ENH, FOL, MSG) == ""  # plain / unknown


def test_screenshot_modes_gate():
    assert "followup" in vt.SCREENSHOT_MODES
    assert "message" in vt.SCREENSHOT_MODES
    assert "task" not in vt.SCREENSHOT_MODES
    assert "" not in vt.SCREENSHOT_MODES
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest test_voice_typed.py::test_mode_for_code_maps_each_key test_voice_typed.py::test_screenshot_modes_gate -q`
Expected: FAIL — `AttributeError: module 'voice_typed' has no attribute 'mode_for_code'` / `SCREENSHOT_MODES`.

- [ ] **Step 3: Add the constants and function**

In `voice_typed.py`, after the `FOLLOWUP_SYSTEM` block / near line 56 add:

```python
SCREENSHOT_MODES = {"followup", "message"}
```

Add a module-level function (place it just above `def handle_utterance`):

```python
def mode_for_code(code, enhcode, folcode, msgcode):
    if code == msgcode:
        return "message"
    if code == enhcode:
        return "task"
    if code == folcode:
        return "followup"
    return ""
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest test_voice_typed.py::test_mode_for_code_maps_each_key test_voice_typed.py::test_screenshot_modes_gate -q`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
cd ~/scripts/voice-typed
git add voice_typed.py test_voice_typed.py
git commit -m "feat(voice-typed): add mode_for_code routing + SCREENSHOT_MODES gate"
```

---

### Task 2: Vision-aware enhance (MSG_SYSTEM + image path through the model call)

Add the new message-mode system prompt, an image-grounding preamble, base64 encoding, and vision support in the chat call. OpenAI gets the image; Groq stays text-only.

**Files:**
- Modify: `voice_typed.py` — add `MSG_SYSTEM`, `GROUNDING_LINE` (near `FOLLOWUP_SYSTEM`); add `import base64` (top stdlib block); add `_encode_image`; extend `_chat_request` and `enhance_prompt`
- Test: `test_voice_typed.py`

**Interfaces:**
- Consumes: nothing from Task 1.
- Produces: `enhance_prompt(text, mode="task", timeout=API_TIMEOUT_S, image_path=None) -> str`; `_chat_request(url, key, model, text, timeout, system=ENHANCE_SYSTEM, image_b64=None) -> str`; `_encode_image(path) -> str`; module constants `MSG_SYSTEM`, `GROUNDING_LINE`.

- [ ] **Step 1: Write the failing tests**

Append to `test_voice_typed.py`:

```python
def _png_bytes():
    # 1x1 white PNG
    from PIL import Image
    import io
    buf = io.BytesIO()
    Image.new("RGB", (1, 1), "white").save(buf, "PNG")
    return buf.getvalue()


def test_encode_image_roundtrip(tmp_path):
    import base64
    p = tmp_path / "s.png"
    raw = _png_bytes()
    p.write_bytes(raw)
    assert base64.b64decode(vt._encode_image(p)) == raw


def test_enhance_prompt_message_uses_msg_system(secrets_file, monkeypatch):
    monkeypatch.setattr(vt, "SECRETS_PATH", secrets_file)
    with mock.patch.object(vt.requests, "post", return_value=_chat_resp()) as post:
        vt.enhance_prompt("say hi", "message")
        assert post.call_args.kwargs["json"]["messages"][0]["content"] == vt.MSG_SYSTEM


def test_enhance_prompt_with_image_sends_vision_array_to_openai(secrets_file, tmp_path, monkeypatch):
    monkeypatch.setattr(vt, "SECRETS_PATH", secrets_file)
    p = tmp_path / "s.png"
    p.write_bytes(_png_bytes())
    with mock.patch.object(vt.requests, "post", return_value=_chat_resp()) as post:
        vt.enhance_prompt("describe", "message", image_path=p)
        content = post.call_args_list[0].kwargs["json"]["messages"][1]["content"]
        assert isinstance(content, list)
        assert content[0] == {"type": "text", "text": "describe"}
        assert content[1]["type"] == "image_url"
        assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_enhance_prompt_groq_fallback_drops_image(secrets_file, tmp_path, monkeypatch):
    monkeypatch.setattr(vt, "SECRETS_PATH", secrets_file)
    p = tmp_path / "s.png"
    p.write_bytes(_png_bytes())
    with mock.patch.object(
        vt.requests, "post",
        side_effect=[_chat_resp(500), _chat_resp(content="via groq")],
    ) as post:
        assert vt.enhance_prompt("x", "followup", image_path=p) == "via groq"
        # OpenAI (first) got the vision array; Groq (second) got a plain string
        assert isinstance(post.call_args_list[0].kwargs["json"]["messages"][1]["content"], list)
        assert post.call_args_list[1].kwargs["json"]["messages"][1]["content"] == "x"


def test_enhance_prompt_followup_image_prepends_grounding(secrets_file, tmp_path, monkeypatch):
    monkeypatch.setattr(vt, "SECRETS_PATH", secrets_file)
    p = tmp_path / "s.png"
    p.write_bytes(_png_bytes())
    with mock.patch.object(vt.requests, "post", return_value=_chat_resp()) as post:
        vt.enhance_prompt("x", "followup", image_path=p)
        system = post.call_args_list[0].kwargs["json"]["messages"][0]["content"]
        assert system.startswith(vt.GROUNDING_LINE)
        assert vt.FOLLOWUP_SYSTEM in system


def test_enhance_prompt_no_image_stays_plain_string(secrets_file, monkeypatch):
    monkeypatch.setattr(vt, "SECRETS_PATH", secrets_file)
    with mock.patch.object(vt.requests, "post", return_value=_chat_resp()) as post:
        vt.enhance_prompt("fix bug")
        assert post.call_args.kwargs["json"]["messages"][1]["content"] == "fix bug"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest test_voice_typed.py -k "encode_image or message_uses_msg or vision_array or drops_image or prepends_grounding or stays_plain_string" -q`
Expected: FAIL — missing `MSG_SYSTEM` / `GROUNDING_LINE` / `_encode_image`, and `image_path` not accepted.

- [ ] **Step 3: Implement**

In `voice_typed.py` top stdlib import block (near `import os`), add:

```python
import base64
```

After the `FOLLOWUP_SYSTEM = """..."""` block add:

```python
MSG_SYSTEM = """\
You rewrite raw dictated speech into a message the speaker wants to send, using an attached screenshot of their current screen as grounding context.

Rules:
- The dictation is the SOURCE OF TRUTH for what to say. The screenshot only grounds references — who "him/her/they" is, the ongoing topic, names, the question being answered, quoted text. NEVER add content, claims, or details the speaker did not intend just because they appear on screen.
- Completeness beats brevity: carry over everything the speaker said. Fix grammar, remove filler ("um", "you know") and false starts, and turn rambling speech into a clear, direct message. Never add requirements or facts the speaker did not say.
- Write in a natural human messaging tone (chat/DM) — not a formal report, not a coding-agent prompt — unless the speaker explicitly asked for another tone.
- Output the whole message on ONE physical line — no newline characters anywhere.
- Output ONLY the message. No preamble, no commentary, no code fences.
"""
GROUNDING_LINE = (
    "A screenshot of the current screen is attached for context. Use it ONLY to "
    "ground references in the dictation — visible output, error messages, filenames, "
    "names, the topic, who is being replied to. Never add facts from the screenshot "
    "that the speaker did not reference."
)
```

Add the encoder (place it just above `_chat_request`):

```python
def _encode_image(path):
    return base64.b64encode(Path(path).read_bytes()).decode()
```

Replace `_chat_request` (lines ~139-154) with:

```python
def _chat_request(url, key, model, text, timeout, system=ENHANCE_SYSTEM, image_b64=None):
    if image_b64:
        user_content = [
            {"type": "text", "text": text},
            {"type": "image_url",
             "image_url": {"url": f"data:image/png;base64,{image_b64}"}},
        ]
    else:
        user_content = text
    r = requests.post(
        url,
        headers={"Authorization": f"Bearer {key}"},
        json={
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user_content},
            ],
            "temperature": 0.3,
        },
        timeout=timeout,
    )
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"].strip()
```

Replace `enhance_prompt` (lines ~157-182) with:

```python
def enhance_prompt(text, mode="task", timeout=API_TIMEOUT_S, image_path=None):
    try:
        secrets = load_secrets()
    except OSError as e:
        raise EnhanceError(f"cannot read secrets: {e}") from e
    system = {"message": MSG_SYSTEM, "followup": FOLLOWUP_SYSTEM}.get(mode, ENHANCE_SYSTEM)
    image_b64 = None
    if image_path:
        try:
            image_b64 = _encode_image(image_path)
        except OSError as e:
            print(f"voice-typed: image encode failed: {e}", flush=True)
    if image_b64 and mode != "message":
        system = GROUNDING_LINE + "\n\n" + system
    model = os.environ.get("VOICE_TYPED_ENHANCE_MODEL", "gpt-4o-mini")
    engines = [
        (OPENAI_CHAT_URL, secrets.get("OPENAI_API_KEY"), model),
        (GROQ_CHAT_URL, secrets.get("GROQ_API_KEY"), GROQ_ENHANCE_MODEL),
    ]
    configured = [(u, k, m) for u, k, m in engines if k]
    if not configured:
        raise EnhanceError(
            "no API keys — need OPENAI_API_KEY and/or GROQ_API_KEY in secrets.env"
        )
    last_err = None
    for url, key, m in configured:
        img = image_b64 if url == OPENAI_CHAT_URL else None
        try:
            out = _chat_request(url, key, m, text, timeout, system, image_b64=img)
            if out:
                return out
            last_err = EnhanceError("empty completion")
        except Exception as e:  # noqa: BLE001 — any engine error -> next engine
            last_err = e
    raise EnhanceError(f"all engines failed: {last_err}")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest test_voice_typed.py -q`
Expected: PASS (all — including the pre-existing `test_enhance_prompt_*` regression tests, which still send a plain-string user content when no image is given).

- [ ] **Step 5: Commit**

```bash
cd ~/scripts/voice-typed
git add voice_typed.py test_voice_typed.py
git commit -m "feat(voice-typed): vision-aware enhance_prompt (MSG_SYSTEM + image_path)"
```

---

### Task 3: Active-window screenshot capture

Add the capture helper + a separately-mockable downscale step.

**Files:**
- Modify: `voice_typed.py` — add `SHOT_MAX_PX` constant (near line 56), `_downscale`, `capture_active_window` (place after `start_recording`/`stop_recording`)
- Test: `test_voice_typed.py`

**Interfaces:**
- Produces: `capture_active_window(png_path) -> Path | None`; `_downscale(png_path) -> None`; constant `SHOT_MAX_PX = 1024`.

- [ ] **Step 1: Write the failing tests**

Append to `test_voice_typed.py`:

```python
_GEO = b"WINDOW=12345\nX=100\nY=50\nWIDTH=800\nHEIGHT=600\nSCREEN=0\n"


def test_capture_active_window_builds_region_and_returns_path(tmp_path, monkeypatch):
    png = tmp_path / "shot.png"
    wid = mock.Mock(stdout=b"12345\n")
    geo = mock.Mock(stdout=_GEO)
    ff = mock.Mock(stdout=b"")
    monkeypatch.setattr(vt, "_downscale", lambda p: None)  # skip real PIL
    with mock.patch.object(vt.subprocess, "run", side_effect=[wid, geo, ff]) as run:
        out = vt.capture_active_window(png)
        assert out == png
        ffmpeg_cmd = run.call_args_list[2].args[0]
        assert ffmpeg_cmd[0] == "ffmpeg"
        assert "800x600" in ffmpeg_cmd
        assert any(a.endswith("+100,50") for a in ffmpeg_cmd)  # DISPLAY+X,Y offset


def test_capture_active_window_ffmpeg_failure_returns_none(tmp_path, monkeypatch):
    png = tmp_path / "shot.png"
    wid = mock.Mock(stdout=b"12345\n")
    geo = mock.Mock(stdout=_GEO)
    err = vt.subprocess.CalledProcessError(1, "ffmpeg")
    monkeypatch.setattr(vt, "_downscale", lambda p: None)
    with mock.patch.object(vt.subprocess, "run", side_effect=[wid, geo, err]):
        assert vt.capture_active_window(png) is None


def test_capture_active_window_xdotool_failure_returns_none(tmp_path, monkeypatch):
    png = tmp_path / "shot.png"
    err = vt.subprocess.CalledProcessError(1, "xdotool")
    with mock.patch.object(vt.subprocess, "run", side_effect=[err]):
        assert vt.capture_active_window(png) is None


def test_downscale_shrinks_large_image(tmp_path):
    from PIL import Image
    p = tmp_path / "big.png"
    Image.new("RGB", (4000, 3000), "white").save(p)
    vt._downscale(p)
    with Image.open(p) as im:
        assert max(im.size) <= vt.SHOT_MAX_PX
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest test_voice_typed.py -k "capture_active_window or downscale_shrinks" -q`
Expected: FAIL — `capture_active_window` / `_downscale` / `SHOT_MAX_PX` undefined.

- [ ] **Step 3: Implement**

Add constant near line 56:

```python
SHOT_MAX_PX = 1024  # longest side sent to the vision model
```

Add after `stop_recording` (near line 315):

```python
def _downscale(png_path):
    from PIL import Image
    with Image.open(png_path) as im:
        im.thumbnail((SHOT_MAX_PX, SHOT_MAX_PX))
        im.save(png_path)


def capture_active_window(png_path):
    """Grab the focused window region to png_path (downscaled). Return Path or None.

    Best-effort: any failure logs and returns None so the caller degrades to a
    text-only enhance. X11 only (session is x11).
    """
    png_path = Path(png_path)
    try:
        wid = subprocess.run(
            ["xdotool", "getactivewindow"],
            capture_output=True, check=True, timeout=5,
        ).stdout.strip().decode()
        geo = subprocess.run(
            ["xdotool", "getwindowgeometry", "--shell", wid],
            capture_output=True, check=True, timeout=5,
        ).stdout.decode()
        g = {}
        for line in geo.splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                g[k.strip()] = v.strip()
        region = f"{g['WIDTH']}x{g['HEIGHT']}"
        offset = f"{os.environ.get('DISPLAY', ':0')}+{g['X']},{g['Y']}"
        png_path.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
             "-f", "x11grab", "-video_size", region, "-i", offset,
             "-frames:v", "1", str(png_path)],
            check=True, timeout=10,
        )
        _downscale(png_path)
        return png_path
    except Exception as e:  # noqa: BLE001 — capture is best-effort
        print(f"voice-typed: screenshot failed: {e}", flush=True)
        return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest test_voice_typed.py -k "capture_active_window or downscale_shrinks" -q`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
cd ~/scripts/voice-typed
git add voice_typed.py test_voice_typed.py
git commit -m "feat(voice-typed): capture_active_window via ffmpeg x11grab + downscale"
```

---

### Task 4: Wire screenshot through queue + F6 key in the daemon

Thread the PNG path from keydown-capture through the queue into `handle_utterance`, add the F6 key + capture trigger in `main()`, and update the pre-existing enhance-mock signatures.

**Files:**
- Modify: `voice_typed.py` — `handle_utterance` (add `shot_path`), `stt_worker` (4-tuple), `main()` (F6 key parse, grab, keydown capture, `q.put` 4-tuples)
- Test: `test_voice_typed.py` — add 2 tests, patch 3 existing enhance-mock lambdas

**Interfaces:**
- Consumes: `mode_for_code` + `SCREENSHOT_MODES` (Task 1), `enhance_prompt(..., image_path=)` (Task 2), `capture_active_window` (Task 3).
- Produces: `handle_utterance(wav_path, window_id, enhance="", shot_path=None)`; queue items are now 4-tuples `(wav, window_id, enhance, shot_path)`.

- [ ] **Step 1: Update the 3 existing enhance-mock signatures (they break otherwise)**

`handle_utterance` will call `enhance_prompt(text, enhance, image_path=shot_path)`. Three existing tests mock `enhance_prompt` with a signature lacking `image_path`. Edit them:

In `test_handle_utterance_enhance_forces_paste`:
```python
    monkeypatch.setattr(vt, "enhance_prompt", lambda t, mode="task", image_path=None: "Task: clean prompt")
```
In `test_handle_utterance_followup_passes_mode`:
```python
    monkeypatch.setattr(
        vt, "enhance_prompt", lambda t, mode="task", image_path=None: seen.append(mode) or "also do X"
    )
```
In `test_handle_utterance_enhance_failure_injects_raw`, change the `boom` def:
```python
    def boom(t, mode="task", image_path=None):
        raise vt.EnhanceError("down")
```

- [ ] **Step 2: Write the new failing tests**

Append to `test_voice_typed.py`:

```python
def test_handle_utterance_passes_shot_to_enhance_and_unlinks(wav_file, tmp_path, monkeypatch):
    shot = tmp_path / "shot.png"
    shot.write_bytes(b"\x89PNG\r\n")
    monkeypatch.setattr(vt, "transcribe", lambda p: "raw")
    seen = {}
    monkeypatch.setattr(
        vt, "enhance_prompt",
        lambda t, mode="task", image_path=None: seen.update(img=image_path) or "clean",
    )
    monkeypatch.setattr(vt, "inject", lambda t, force_paste=False: None)
    vt.handle_utterance(wav_file, None, enhance="message", shot_path=shot)
    assert seen["img"] == shot
    assert not shot.exists()      # screenshot deleted
    assert not wav_file.exists()  # wav deleted


def test_handle_utterance_unlinks_shot_even_on_failure(wav_file, tmp_path, monkeypatch):
    shot = tmp_path / "shot.png"
    shot.write_bytes(b"\x89PNG\r\n")
    def boom(p):
        raise vt.TranscribeError("down")
    monkeypatch.setattr(vt, "transcribe", boom)
    monkeypatch.setattr(vt, "notify", lambda m: None)
    vt.handle_utterance(wav_file, None, enhance="message", shot_path=shot)
    assert not shot.exists()
    assert not wav_file.exists()
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `python3 -m pytest test_voice_typed.py -k "passes_shot or unlinks_shot" -q`
Expected: FAIL — `handle_utterance` does not accept `shot_path`.

- [ ] **Step 4: Implement `handle_utterance` + `stt_worker`**

Change `handle_utterance` signature (line ~317) and its `enhance_prompt` call + `finally`:

```python
def handle_utterance(wav_path, window_id, enhance="", shot_path=None):
    """Transcribe + inject one utterance. `enhance` is "" (plain), "task",
    "followup", or "message". `shot_path` is an optional screenshot PNG for
    vision-grounded enhance. Never raises; deletes wav + shot."""
    try:
        try:
            text = transcribe(wav_path)
        except TranscribeError as e:
            notify(f"transcription failed: {e}")
            return
        if not text:
            return  # silence -> type nothing
        text = apply_corrections(text)
        print(
            f"voice-typed: utterance enhance={enhance} text={text[:60]!r}",
            flush=True,
        )
        if enhance:
            notify("✨ enhancing (follow-up)" if enhance == "followup" else "✨ enhancing")
            try:
                text = enhance_prompt(text, enhance, image_path=shot_path)
                print(f"voice-typed: enhanced -> {text[:60]!r}", flush=True)
            except EnhanceError as e:
                notify(f"enhance failed — raw text: {e}")
                print(f"voice-typed: enhance FAILED: {e}", flush=True)
        global LAST_TEXT
        LAST_TEXT = text
        if window_id is not None:
            current = active_window()
            if current is not None and current != window_id:
                notify(f"focus changed — dropped: {text[:60]}")
                return
        try:
            # multi-line prompts must paste: xdotool type sends Return per newline
            inject(text, force_paste=bool(enhance))
        except Exception as e:  # noqa: BLE001 — daemon must survive
            notify(f"injection failed: {e}")
    finally:
        Path(wav_path).unlink(missing_ok=True)
        if shot_path:
            Path(shot_path).unlink(missing_ok=True)
```

Change `stt_worker` (line ~357) to unpack the 4-tuple:

```python
def stt_worker(q):
    while True:
        wav_path, window_id, enhance, shot_path = q.get()
        handle_utterance(wav_path, window_id, enhance, shot_path)
        q.task_done()
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python3 -m pytest test_voice_typed.py -q`
Expected: PASS (all, including the 3 edited mocks).

- [ ] **Step 6: Wire `main()` — F6 key, capture at keydown, 4-tuple `q.put`**

In `main()`, after the `folname`/`folcode` block (line ~433-436) add:

```python
    msgname = os.environ.get("VOICE_TYPED_MSG_KEY", "KEY_F6")
    msgcode = getattr(ecodes, msgname, None)
    if msgcode is None:
        sys.exit(f"unknown VOICE_TYPED_MSG_KEY: {msgname}")
```

Replace the nested `mode_for` (lines ~438-443) with a thin wrapper over the module function:

```python
    def mode_for(code):
        return mode_for_code(code, enhcode, folcode, msgcode)
```

In the X11-grab block (lines ~453-455) add a grab for the msg key:

```python
    if os.environ.get("VOICE_TYPED_GRAB", "1") != "0":
        threading.Thread(target=x11_grab_key, args=(enhname,), daemon=True).start()
        threading.Thread(target=x11_grab_key, args=(folname,), daemon=True).start()
        threading.Thread(target=x11_grab_key, args=(msgname,), daemon=True).start()
```

Add `shot = None` next to the `wav = None` init (line ~458):

```python
    rec = None            # active pw-record Popen or None
    wav = None            # Path of in-flight recording
    shot = None           # Path of in-flight screenshot (screenshot-modes only) or None
```

Update the startup log line (line ~464-468) to include the msg key:

```python
    print(
        f"voice-typed: {len(devs)} device(s), key={keyname}, "
        f"enhance={enhname}, followup={folname}, message={msgname}",
        flush=True,
    )
```

In the MAX_UTTERANCE cap path (line ~493), add `shot` to the tuple:

```python
                q.put((wav, active_window(), mode_for(rec_key), shot))
```

In the event filter's accepted-codes tuple (lines ~506-508), add `msgcode`:

```python
                if ev.type != ecodes.EV_KEY or ev.code not in (
                    keycode, flagcode, enhcode, folcode, msgcode,
                ):
                    continue
```

In the keydown branch (lines ~514-531), after `rec_key = ev.code` capture the screenshot for screenshot-modes:

```python
                if ev.value == 1 and rec is None and not awaiting_release:
                    wav = run_dir / f"utt-{int(time.time() * 1000)}.wav"
                    try:
                        rec = start_recording(wav)
                    except OSError as e:
                        notify(f"recorder start failed: {e}")
                        rec = None
                        continue
                    rec_key = ev.code
                    shot = None
                    if mode_for(ev.code) in SCREENSHOT_MODES:
                        shot = capture_active_window(
                            run_dir / f"shot-{int(time.time() * 1000)}.png"
                        )
                    deadline = time.monotonic() + MAX_UTTERANCE_S
                    print(
                        f"voice-typed: keydown code={ev.code} mode={mode_for(ev.code)!r}",
                        flush=True,
                    )
                    notify({
                        "task": "🎙 recording (enhance)",
                        "followup": "🎙 recording (follow-up)",
                        "message": "🎙 recording (message)",
                    }.get(mode_for(ev.code), "🎙 recording"))
```

In the keyup branch (lines ~532-542), add `shot` to the `q.put`:

```python
                elif ev.value == 0 and ev.code == rec_key:
                    # keyup (value 2 autorepeat ignored); other key's keyup ignored
                    if awaiting_release:
                        awaiting_release = False
                        rec_key = None
                    elif rec is not None:
                        stop_recording(rec)
                        rec = None
                        notify("… transcribing")
                        q.put((wav, active_window(), mode_for(rec_key), shot))
                        rec_key = None
```

- [ ] **Step 7: Verify import + full suite still green**

Run: `python3 -c "import voice_typed" && python3 -m pytest test_voice_typed.py -q`
Expected: import OK, all tests PASS.

- [ ] **Step 8: Commit**

```bash
cd ~/scripts/voice-typed
git add voice_typed.py test_voice_typed.py
git commit -m "feat(voice-typed): F6 message mode + screenshot through queue"
```

---

### Task 5: Install deps + doc the new keys, then live-verify

Add `ffmpeg` + `python3-pil` to the installer, refresh the module docstring, restart the daemon, and manually confirm end-to-end.

**Files:**
- Modify: `install.sh` (apt deps + preflight), `voice_typed.py` (module docstring)

- [ ] **Step 1: Add deps to `install.sh`**

Change the apt line (lines 7-8) to include `ffmpeg` and `python3-pil`:

```bash
sudo apt-get install -y xdotool xclip python3-evdev python3-requests python3-pil \
  python3-pytest pipewire-bin libnotify-bin ffmpeg
```

Add `ffmpeg` to the preflight loop (line 11):

```bash
for bin in pw-record xdotool notify-send xclip ffmpeg; do
```

- [ ] **Step 2: Update the module docstring**

Replace the module docstring (lines 1-6) key list:

```python
#!/usr/bin/env python3
"""voice-typed — hold-to-talk dictation daemon (X11/GNOME).

Hold a key, speak, release -> text typed into the focused window via xdotool.
Keys: F9 verbatim | F8 new-task enhance | F7 follow-up enhance (+screenshot) |
F6 chat message (+screenshot) | F10 flag last. Screenshot modes grab the active
window and send it to a vision model for on-screen grounding.
STT: OpenAI gpt-4o-transcribe, Groq fallback. Enhance: gpt-4o-mini (vision).
"""
```

- [ ] **Step 3: Commit the code changes**

```bash
cd ~/scripts/voice-typed
git add install.sh voice_typed.py
git commit -m "chore(voice-typed): install ffmpeg+pillow, document F6/screenshot keys"
```

- [ ] **Step 4: Restart the daemon and confirm it's up**

Run:
```bash
systemctl --user restart voice-typed && sleep 1 && \
  systemctl --user --no-pager --lines=8 status voice-typed
journalctl --user -u voice-typed -n 5 --no-pager
```
Expected: `active (running)`; log shows `message=KEY_F6` in the startup line and `X11 grab active on KEY_F6`.

- [ ] **Step 5: Manual end-to-end verification (needs a human at the keyboard)**

These paths are not unit-tested (event loop + real capture). Verify by hand:

1. **F9 (regression):** focus a text field, hold F9, say "hello world", release → types verbatim, no screenshot in `journalctl` (no `screenshot failed`, no capture line).
2. **F6 message:** open a chat window with a visible conversation, focus its input, hold F6, say "reply saying I'll join at five" → a natural chat reply grounded in the on-screen thread is pasted. `journalctl` shows `keydown ... mode='message'` and no `screenshot failed`.
3. **F7 follow-up:** in a terminal with Claude output visible, hold F7, say "fix that error" → follow-up references the visible error. `journalctl` shows `mode='followup'`.
4. **Privacy check:** after each, confirm no `shot-*.png` left in `$XDG_RUNTIME_DIR/voice-typed/`:
   `ls "$XDG_RUNTIME_DIR/voice-typed/" 2>/dev/null` → no `shot-*.png` / `utt-*.wav`.
5. **Degradation:** temporarily break capture (`VOICE_TYPED_MSG_KEY` on a window with no geometry, or watch a `screenshot failed` log) → message still gets written text-only, daemon stays up.

Report results. If all pass, the branch is ready to merge to `master`.

---

## Self-Review

**Spec coverage:**
- Screenshot capture (active window, ffmpeg, downscale, fail→None) → Task 3. ✓
- New `message` mode + F6 key + X11 grab → Task 1 (routing) + Task 4 (main wiring). ✓
- Screenshot gating (followup/message only) → Task 1 `SCREENSHOT_MODES` + Task 4 keydown guard. ✓
- Vision path (`enhance_prompt` image_path, OpenAI vision array, Groq text-only, grounding line, MSG_SYSTEM) → Task 2. ✓
- Queue plumbing (4-tuple) + unlink png in finally → Task 4. ✓
- F8/F9 unchanged → covered by regression tests (Task 2 plain-string test; Task 5 manual F9). ✓
- Deps + docs + privacy/cost note → Task 5. ✓

**Placeholder scan:** the Task 1 Step 1 stub is explicitly replaced in the same step; no TBD/TODO elsewhere; every code step shows full code. ✓

**Type consistency:** `capture_active_window(png_path)->Path|None`, `enhance_prompt(text, mode, timeout, image_path)`, `_chat_request(..., image_b64=None)`, `handle_utterance(wav_path, window_id, enhance="", shot_path=None)`, queue 4-tuple `(wav, window_id, enhance, shot_path)`, `mode_for_code(code, enhcode, folcode, msgcode)` — consistent across Tasks 1-4. ✓
