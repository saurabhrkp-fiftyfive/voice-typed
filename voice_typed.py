#!/usr/bin/env python3
"""voice-typed — hold-to-talk dictation daemon (X11/GNOME).

Hold VOICE_TYPED_KEY (default F9), speak, release -> text typed into
focused window via xdotool. STT: OpenAI gpt-4o-transcribe, Groq fallback.
"""
import os
import queue
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

import requests

OPENAI_URL = "https://api.openai.com/v1/audio/transcriptions"
GROQ_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
OPENAI_CHAT_URL = "https://api.openai.com/v1/chat/completions"
GROQ_CHAT_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_ENHANCE_MODEL = "llama-3.3-70b-versatile"
ENHANCE_SYSTEM = """\
You rewrite raw dictated speech into a clear, COMPLETE prompt for an AI coding agent that is STARTING A NEW TASK.

Rules:
- Completeness beats brevity. Carry over EVERYTHING the speaker said: the intent, every technical detail (file names, commands, error messages, names, numbers), and ALL surrounding context — background, reasoning, motivations, examples, preferences, caveats, edge cases, asides. If the speaker said it, it appears in the output.
- Do NOT summarize, condense, or paraphrase content away. A long faithful prompt is correct; a short lossy one is a failure. Drop a spoken detail ONLY if it is pure filler or trivially obvious from the rest of the prompt.
- ALWAYS actively rewrite the wording: fix grammar, convert rambling speech into direct imperative instructions, and restructure for clarity. Never echo the dictation verbatim — improve HOW it is said while keeping WHAT is said intact.
- Never add requirements, constraints, or facts the speaker did not say.
- Remove only filler words ("um", "you know"), false starts, and verbatim repetition — nothing else.
- Output the whole prompt on ONE physical line — no newline characters anywhere — so it pastes and displays correctly in the Claude Code input box.
- Structure that single line with labelled sections joined by " | ": "Task: <...> | Context: <...> | Constraints: <...> | Expected output: <...>". Background, reasoning, and supporting details go under Context. Omit any label whose content is empty. A short simple request -> just "Task: <one tightened sentence>" with no other labels.
- Output ONLY the rewritten prompt. No preamble, no commentary, no code fences around the output.
"""
FOLLOWUP_SYSTEM = """\
You rewrite raw dictated speech into a COMPLETE follow-up message for an AI coding agent ALREADY working in an active session. It is a continuation, not a new task spec.

Rules:
- Completeness beats brevity. Carry over EVERYTHING the speaker said: every instruction, correction, technical detail (file names, commands, error messages, names, numbers), and any NEW context, reasoning, preference, or caveat they added. If the speaker said it, it appears in the output.
- Do NOT summarize, condense, or paraphrase content away. A long faithful message is correct; a short lossy one is a failure. Drop a spoken detail ONLY if it is pure filler or something the agent trivially already knows from the active session (e.g. restating what the overall task is).
- ALWAYS actively rewrite the wording: fix grammar, convert rambling speech into direct imperative instructions, and restructure for clarity. Never echo the dictation verbatim — improve HOW it is said while keeping WHAT is said intact.
- Never add requirements, constraints, or facts the speaker did not say.
- Remove only filler words ("um", "you know"), false starts, and verbatim repetition — nothing else.
- Assume the agent already has session context — do NOT re-explain the task or add headings or labels, but DO keep every new detail and clarification the speaker gave.
- Output the whole message on ONE physical line — no newline characters anywhere — so it pastes and displays correctly in the Claude Code input box.
- Keep it a direct instruction or correction (e.g. "Also handle X", "No, use Y instead", "Now run the tests"). Use " | " only to separate genuinely distinct points.
- Output ONLY the rewritten message. No preamble, no commentary, no code fences around the output.
"""
SECRETS_PATH = Path.home() / ".config" / "secrets.env"
VOCAB_PATH = Path(__file__).resolve().parent / "vocab.txt"
VOCAB_MAX_CHARS = 800  # ~200 tokens; whisper prompt cap is 224 tokens
CORRECTIONS_PATH = Path(__file__).resolve().parent / "corrections.txt"
FLAGGED_PATH = Path(__file__).resolve().parent / "flagged.md"
LAST_TEXT = ""  # last typed transcript, held in memory for ⚑ flagging only
MAX_UTTERANCE_S = 300
API_TIMEOUT_S = 30  # per-engine connect+read timeout, NOT total deadline
SCREENSHOT_MODES = {"followup", "message"}


class TranscribeError(Exception):
    pass


class EnhanceError(Exception):
    pass


def load_secrets(path=None):
    path = Path(path or SECRETS_PATH)
    secrets = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        secrets[k.strip()] = v.strip().strip('"').strip("'")
    return secrets


def load_vocab(path=None):
    """Domain words (one per line, # comments) -> STT bias prompt, "" if none."""
    path = Path(path or VOCAB_PATH)
    try:
        words = [
            w.strip() for w in path.read_text().splitlines()
            if w.strip() and not w.strip().startswith("#")
        ]
    except OSError:
        return ""
    if not words:
        return ""
    return ("Vocabulary: " + ", ".join(words))[:VOCAB_MAX_CHARS]


def _stt_request(url, key, model, wav_path, timeout, prompt=""):
    data = {"model": model}
    if prompt:
        data["prompt"] = prompt
    with open(wav_path, "rb") as f:
        r = requests.post(
            url,
            headers={"Authorization": f"Bearer {key}"},
            files={"file": ("utterance.wav", f, "audio/wav")},
            data=data,
            timeout=timeout,
        )
    r.raise_for_status()
    return r.json()["text"].strip()


def transcribe(wav_path, timeout=API_TIMEOUT_S):
    try:
        secrets = load_secrets()
    except OSError as e:
        raise TranscribeError(f"cannot read secrets: {e}") from e
    engines = [
        (OPENAI_URL, secrets.get("OPENAI_API_KEY"), "gpt-4o-transcribe"),
        (GROQ_URL, secrets.get("GROQ_API_KEY"), "whisper-large-v3"),
    ]
    configured = [(u, k, m) for u, k, m in engines if k]
    if not configured:
        raise TranscribeError(
            "no API keys — need OPENAI_API_KEY and/or GROQ_API_KEY in secrets.env"
        )
    prompt = load_vocab()
    last_err = None
    for url, key, model in configured:
        try:
            return _stt_request(url, key, model, wav_path, timeout, prompt)
        except Exception as e:  # noqa: BLE001 — any engine error -> next engine
            last_err = e
    raise TranscribeError(f"all engines failed: {last_err}")


def _chat_request(url, key, model, text, timeout, system=ENHANCE_SYSTEM):
    r = requests.post(
        url,
        headers={"Authorization": f"Bearer {key}"},
        json={
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": text},
            ],
            "temperature": 0.3,
        },
        timeout=timeout,
    )
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"].strip()


def enhance_prompt(text, mode="task", timeout=API_TIMEOUT_S):
    try:
        secrets = load_secrets()
    except OSError as e:
        raise EnhanceError(f"cannot read secrets: {e}") from e
    system = FOLLOWUP_SYSTEM if mode == "followup" else ENHANCE_SYSTEM
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
        try:
            out = _chat_request(url, key, m, text, timeout, system)
            if out:
                return out
            last_err = EnhanceError("empty completion")
        except Exception as e:  # noqa: BLE001 — any engine error -> next engine
            last_err = e
    raise EnhanceError(f"all engines failed: {last_err}")


# terminals paste with ctrl+shift+v (ctrl+v is verbatim-insert / image paste there)
TERMINAL_CLASS_HINTS = (
    "terminal", "kitty", "alacritty", "konsole", "xterm", "tilix",
    "terminator", "wezterm", "st-256color", "ptyxis", "foot", "urxvt",
)


def active_window_class():
    # xprop, not `xdotool getwindowclassname` — installed xdotool predates that command
    try:
        wid = subprocess.run(
            ["xdotool", "getactivewindow"],
            capture_output=True, check=True, timeout=5,
        ).stdout.strip().decode()
        out = subprocess.run(
            ["xprop", "-id", wid, "WM_CLASS"],
            capture_output=True, check=True, timeout=5,
        )
        return out.stdout.decode().lower()
    except Exception:  # noqa: BLE001 — guard is best-effort
        return ""


def paste_chord():
    cls = active_window_class()
    if any(h in cls for h in TERMINAL_CLASS_HINTS):
        return "ctrl+shift+v"
    return "ctrl+v"


def inject(text, force_paste=False):
    if not text or not text.strip():
        return
    if force_paste or os.environ.get("VOICE_TYPED_PASTE") == "1":
        subprocess.run(
            ["xclip", "-selection", "clipboard"],
            input=text.encode(), check=True, timeout=10,
        )
        subprocess.run(
            ["xdotool", "key", "--clearmodifiers", paste_chord()],
            check=True, timeout=10,
        )
    else:
        subprocess.run(
            ["xdotool", "type", "--clearmodifiers", "--delay", "2", "--", text],
            check=True, timeout=30,
        )


def load_corrections(path=None):
    """corrections.txt: 'wrong => right' per line, # comments. [] if missing."""
    path = Path(path or CORRECTIONS_PATH)
    pairs = []
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return pairs
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=>" not in line:
            continue
        wrong, right = line.split("=>", 1)
        wrong, right = wrong.strip(), right.strip()
        if wrong:
            pairs.append((wrong, right))
    return pairs


def apply_corrections(text, path=None):
    for wrong, right in load_corrections(path):
        text = re.sub(rf"\b{re.escape(wrong)}\b", right, text, flags=re.IGNORECASE)
    return text


def flag_last():
    """Append last typed utterance to flagged.md for later correction."""
    if not LAST_TEXT:
        notify("nothing to flag")
        return
    ts = time.strftime("%Y-%m-%d %H:%M")
    try:
        with open(FLAGGED_PATH, "a") as f:
            f.write(f'- {ts} ⚑ "{LAST_TEXT}" → \n')
        notify(f"⚑ flagged: {LAST_TEXT[:40]}")
    except OSError as e:
        notify(f"flag write failed: {e}")


def notify(msg):
    try:
        subprocess.run(
            ["notify-send", "-t", "2000", "voice-typed", msg],
            check=False, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        print(f"voice-typed: {msg}", flush=True)


def active_window():
    try:
        out = subprocess.run(
            ["xdotool", "getactivewindow"],
            capture_output=True, check=True, timeout=5,
        )
        return out.stdout.strip().decode()
    except Exception:  # noqa: BLE001 — guard is best-effort
        return None


def start_recording(wav_path):
    wav_path = Path(wav_path)
    wav_path.parent.mkdir(parents=True, exist_ok=True)
    return subprocess.Popen(
        [
            "pw-record", "--format", "s16", "--rate", "16000",
            "--channels", "1", str(wav_path),
        ]
    )


def stop_recording(proc):
    try:
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
    except OSError:
        pass


def mode_for_code(code, enhcode, folcode, msgcode):
    if code == msgcode:
        return "message"
    if code == enhcode:
        return "task"
    if code == folcode:
        return "followup"
    return ""


def handle_utterance(wav_path, window_id, enhance=""):
    """Transcribe + inject one utterance. `enhance` is "" (plain), "task", or
    "followup". Never raises; deletes wav."""
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
                text = enhance_prompt(text, enhance)
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


def stt_worker(q):
    while True:
        wav_path, window_id, enhance = q.get()
        handle_utterance(wav_path, window_id, enhance)
        q.task_done()


def x11_grab_key(keyname):
    """Grab key at X11 level so the focused app never sees it (evdev still does).

    Best-effort: any failure logs and leaves behavior as before (key leaks).
    Runs forever discarding the grabbed events — call in a daemon thread.
    """
    try:
        from Xlib import X, XK, display
        d = display.Display()
        keysym = XK.string_to_keysym(keyname.replace("KEY_", "", 1))
        code = d.keysym_to_keycode(keysym)
        if not code:
            raise ValueError(f"no X keycode for {keyname}")
        root = d.screen().root
        root.grab_key(code, X.AnyModifier, False, X.GrabModeAsync, X.GrabModeAsync)
        d.sync()
        print(f"voice-typed: X11 grab active on {keyname}", flush=True)
        while True:
            d.next_event()  # discard — grab exists only to swallow the key
    except Exception as e:  # noqa: BLE001 — grab is optional, daemon must run
        print(f"voice-typed: X11 grab failed for {keyname}: {e}", flush=True)


def find_keyboards(keycode):
    import evdev
    devs, denied = [], 0
    for path in evdev.list_devices():
        try:
            d = evdev.InputDevice(path)
        except (PermissionError, OSError):
            denied += 1
            continue
        if keycode in d.capabilities().get(evdev.ecodes.EV_KEY, []):
            devs.append(d)
        else:
            d.close()
    return devs, denied


def rescan_devices(devs, keycode):
    """Add hotplugged keyboards (BT reconnect, dongle re-plug) to devs in place."""
    known = {d.path for d in devs}
    added = 0
    for d in find_keyboards(keycode)[0]:
        if d.path in known:
            d.close()
        else:
            devs.append(d)
            added += 1
            print(f"voice-typed: device added {d.path} ({d.name})", flush=True)
    return added


def main():
    import select
    from evdev import ecodes

    keyname = os.environ.get("VOICE_TYPED_KEY", "KEY_F9")
    keycode = getattr(ecodes, keyname, None)
    if keycode is None:
        sys.exit(f"unknown VOICE_TYPED_KEY: {keyname}")
    flagname = os.environ.get("VOICE_TYPED_FLAG_KEY", "KEY_F10")
    flagcode = getattr(ecodes, flagname, None)
    if flagcode is None:
        sys.exit(f"unknown VOICE_TYPED_FLAG_KEY: {flagname}")
    enhname = os.environ.get("VOICE_TYPED_ENHANCE_KEY", "KEY_F8")
    enhcode = getattr(ecodes, enhname, None)
    if enhcode is None:
        sys.exit(f"unknown VOICE_TYPED_ENHANCE_KEY: {enhname}")
    folname = os.environ.get("VOICE_TYPED_FOLLOWUP_KEY", "KEY_F7")
    folcode = getattr(ecodes, folname, None)
    if folcode is None:
        sys.exit(f"unknown VOICE_TYPED_FOLLOWUP_KEY: {folname}")

    def mode_for(code):
        if code == enhcode:
            return "task"
        if code == folcode:
            return "followup"
        return ""
    devs, denied = find_keyboards(keycode)
    if not devs:
        sys.exit(
            f"no readable keyboard with {keyname} ({denied} device(s) denied). "
            "Is the user in the 'input' group? (sudo usermod -aG input $USER, re-login)"
        )
    run_dir = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp")) / "voice-typed"
    q = queue.Queue()
    threading.Thread(target=stt_worker, args=(q,), daemon=True).start()
    if os.environ.get("VOICE_TYPED_GRAB", "1") != "0":
        threading.Thread(target=x11_grab_key, args=(enhname,), daemon=True).start()
        threading.Thread(target=x11_grab_key, args=(folname,), daemon=True).start()

    rec = None            # active pw-record Popen or None
    wav = None            # Path of in-flight recording
    deadline = 0.0        # monotonic cutoff for MAX_UTTERANCE_S cap
    awaiting_release = False  # cap fired while key held; swallow next keyup
    rec_key = None        # keycode that started the in-flight recording
    input_mtime = os.stat("/dev/input").st_mtime  # changes on device add/remove

    print(
        f"voice-typed: {len(devs)} device(s), key={keyname}, "
        f"enhance={enhname}, followup={folname}",
        flush=True,
    )
    while True:
        if rec is None:
            timeout = 1.0
        else:
            timeout = max(0.05, min(1.0, deadline - time.monotonic()))
        r, _, _ = select.select(devs, [], [], timeout)

        try:
            m = os.stat("/dev/input").st_mtime
        except OSError:
            m = input_mtime
        if m != input_mtime:
            input_mtime = m
            rescan_devices(devs, keycode)

        if rec is not None:
            if rec.poll() is not None:  # recorder died mid-recording
                notify(f"recorder died (exit {rec.returncode})")
                rec = None
            elif time.monotonic() >= deadline:  # MAX_UTTERANCE_S cap
                stop_recording(rec)
                rec = None
                awaiting_release = True
                notify(f"{MAX_UTTERANCE_S}s cap — transcribing")
                q.put((wav, active_window(), mode_for(rec_key)))

        for d in r:
            try:
                events = list(d.read())
            except OSError:  # device unplugged/reconnected — drop dead fd, don't spin on it
                devs.remove(d)
                d.close()
                if not devs:
                    notify("all input devices lost — exiting")
                    sys.exit(1)
                continue
            for ev in events:
                if ev.type != ecodes.EV_KEY or ev.code not in (
                    keycode, flagcode, enhcode, folcode,
                ):
                    continue
                if ev.code == flagcode:
                    if ev.value == 1:
                        flag_last()
                    continue
                if ev.value == 1 and rec is None and not awaiting_release:
                    wav = run_dir / f"utt-{int(time.time() * 1000)}.wav"
                    try:
                        rec = start_recording(wav)
                    except OSError as e:
                        notify(f"recorder start failed: {e}")
                        rec = None
                        continue
                    rec_key = ev.code
                    deadline = time.monotonic() + MAX_UTTERANCE_S
                    print(
                        f"voice-typed: keydown code={ev.code} mode={mode_for(ev.code)!r}",
                        flush=True,
                    )
                    notify({
                        "task": "🎙 recording (enhance)",
                        "followup": "🎙 recording (follow-up)",
                    }.get(mode_for(ev.code), "🎙 recording"))
                elif ev.value == 0 and ev.code == rec_key:
                    # keyup (value 2 autorepeat ignored); other key's keyup ignored
                    if awaiting_release:
                        awaiting_release = False
                        rec_key = None
                    elif rec is not None:
                        stop_recording(rec)
                        rec = None
                        notify("… transcribing")
                        q.put((wav, active_window(), mode_for(rec_key)))
                        rec_key = None


if __name__ == "__main__":
    main()
