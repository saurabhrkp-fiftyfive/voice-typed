#!/usr/bin/env python3
"""voice-typed — hold-to-talk dictation daemon (X11/GNOME).

Hold a key, speak, release -> text typed into the focused window via xdotool.
Keys: F9 verbatim | F8 new-task enhance | F7 follow-up enhance (+screenshot) |
F6 chat message (+screenshot) | F10 flag last. Screenshot modes grab the active
window and send it to a vision model for on-screen grounding.
STT: OpenAI gpt-4o-transcribe, Groq fallback. Enhance: gpt-4o-mini (vision).
"""
import base64
import os
import queue
import re
import subprocess
import sys
import threading
import time
import tomllib
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
TRANSLITERATE_SYSTEM = """\
You transliterate Hindi (Devanagari) text into the Latin/Roman alphabet.

Rules:
- Romanize any Devanagari (Hindi) into natural, phonetic Roman Hindi — the way Hindi is typed in Latin script in everyday chat (e.g. "क्या हाल है" -> "kya haal hai", "मैं ठीक हूँ" -> "main theek hoon"). Delete the implicit trailing schwa ("प्यार" -> "pyaar", not "pyaara").
- Keep the meaning and word order EXACTLY the same. Do NOT translate to English, summarize, correct, or add/remove anything.
- Leave text already in Latin script, numbers, punctuation, code, and English words unchanged and in place.
- Output ONLY the transliterated text on one line. No preamble, no commentary, no quotes, no code fences.
"""
DEVANAGARI_RE = re.compile(r"[ऀ-ॿ]")
SECRETS_PATH = Path.home() / ".config" / "secrets.env"
LEGACY_DIR = Path(__file__).resolve().parent
VOCAB_PATH = LEGACY_DIR / "vocab.txt"
VOCAB_MAX_CHARS = 800  # ~200 tokens; whisper prompt cap is 224 tokens
CORRECTIONS_PATH = LEGACY_DIR / "corrections.txt"
FLAGGED_PATH = LEGACY_DIR / "flagged.md"
LAST_TEXT = ""  # last typed transcript, held in memory for ⚑ flagging only
MAX_UTTERANCE_S = 300
API_TIMEOUT_S = 30  # per-engine connect+read timeout, NOT total deadline
SCREENSHOT_MODES = {"followup", "message"}
SHOT_MAX_PX = 1568  # longest side sent to the vision model (keeps on-screen text legible)
CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "voice-typed"
DATA_DIR = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "voice-typed"
CONFIG_PATH = CONFIG_DIR / "config.toml"
DEFAULT_CONFIG = {
    "keys": {
        "dictate": "KEY_F9", "enhance": "KEY_F8", "followup": "KEY_F7",
        "message": "KEY_F6", "flag": "KEY_F10",
    },
    "engines": {"enhance_model": "gpt-4o-mini", "api_timeout_s": 30},
    "behavior": {
        "paste_mode": False, "grab_keys": True,
        "max_utterance_s": 300, "transliterate_devanagari": True,
    },
}
_ENV_OVERRIDES = {
    ("keys", "dictate"): "VOICE_TYPED_KEY",
    ("keys", "enhance"): "VOICE_TYPED_ENHANCE_KEY",
    ("keys", "followup"): "VOICE_TYPED_FOLLOWUP_KEY",
    ("keys", "message"): "VOICE_TYPED_MSG_KEY",
    ("keys", "flag"): "VOICE_TYPED_FLAG_KEY",
    ("engines", "enhance_model"): "VOICE_TYPED_ENHANCE_MODEL",
    ("behavior", "paste_mode"): "VOICE_TYPED_PASTE",
    ("behavior", "grab_keys"): "VOICE_TYPED_GRAB",
}


def load_config(path=None):
    """defaults < config.toml < env vars. Never raises — bad file -> defaults."""
    cfg = {sec: dict(vals) for sec, vals in DEFAULT_CONFIG.items()}
    p = Path(path or CONFIG_PATH)
    try:
        with open(p, "rb") as f:
            user = tomllib.load(f)
        for sec, vals in user.items():
            if sec in cfg and isinstance(vals, dict):
                for k, v in vals.items():
                    if k in cfg[sec]:
                        cfg[sec][k] = v
    except FileNotFoundError:
        pass
    except (tomllib.TOMLDecodeError, OSError) as e:
        print(f"voice-typed: bad config.toml ({e}) — using defaults", flush=True)
    for (sec, key), env in _ENV_OVERRIDES.items():
        v = os.environ.get(env)
        if v is None:
            continue
        if key == "paste_mode":
            cfg[sec][key] = v == "1"
        elif key == "grab_keys":
            cfg[sec][key] = v != "0"
        else:
            cfg[sec][key] = v
    return cfg


def dump_config(cfg):
    """Known-schema dict -> TOML text (stdlib has no writer)."""
    lines = []
    for sec in ("keys", "engines", "behavior"):
        lines.append(f"[{sec}]")
        for k, v in cfg[sec].items():
            if isinstance(v, bool):
                lines.append(f"{k} = {'true' if v else 'false'}")
            elif isinstance(v, int):
                lines.append(f"{k} = {v}")
            else:
                lines.append(f'{k} = "{v}"')
        lines.append("")
    return "\n".join(lines)


_USER_FILES = (  # (name, dest kind): config dir or data dir
    ("vocab.txt", "config"), ("corrections.txt", "config"), ("flagged.md", "data"),
)


def migrate_user_files(config_dir=None, data_dir=None, legacy_dir=None):
    """Copy legacy repo-dir user files to XDG dirs when targets missing.
    Idempotent; returns names copied this run."""
    import shutil as _shutil
    config_dir = Path(config_dir or CONFIG_DIR)
    data_dir = Path(data_dir or DATA_DIR)
    legacy_dir = Path(legacy_dir or LEGACY_DIR)
    copied = []
    for name, kind in _USER_FILES:
        dest = (config_dir if kind == "config" else data_dir) / name
        src = legacy_dir / name
        if dest.exists() or not src.exists():
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        _shutil.copy2(src, dest)
        copied.append(name)
    return copied


def resolve_user_paths():
    """Point the module path globals at XDG files when they exist."""
    global VOCAB_PATH, CORRECTIONS_PATH, FLAGGED_PATH
    for attr, kind, name in (
        ("VOCAB_PATH", "config", "vocab.txt"),
        ("CORRECTIONS_PATH", "config", "corrections.txt"),
        ("FLAGGED_PATH", "data", "flagged.md"),
    ):
        p = (CONFIG_DIR if kind == "config" else DATA_DIR) / name
        if p.exists():
            globals()[attr] = p


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


def _encode_image(path):
    return base64.b64encode(Path(path).read_bytes()).decode()


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
    vocab = load_vocab()
    if vocab:
        system += (
            "\n\nPreserve the EXACT spelling of any of these names/terms that appear: "
            + vocab.replace("Vocabulary: ", "", 1)
        )
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


def transliterate(text, timeout=API_TIMEOUT_S):
    """Romanize Devanagari (Hindi) in `text` to Latin script. No Devanagari ->
    return unchanged. On any failure -> return original text (never lose it)."""
    if not DEVANAGARI_RE.search(text):
        return text
    try:
        secrets = load_secrets()
    except OSError as e:
        print(f"voice-typed: transliterate skipped (secrets): {e}", flush=True)
        return text
    model = os.environ.get("VOICE_TYPED_ENHANCE_MODEL", "gpt-4o-mini")
    engines = [
        (OPENAI_CHAT_URL, secrets.get("OPENAI_API_KEY"), model),
        (GROQ_CHAT_URL, secrets.get("GROQ_API_KEY"), GROQ_ENHANCE_MODEL),
    ]
    for url, key, m in [(u, k, mm) for u, k, mm in engines if k]:
        try:
            out = _chat_request(url, key, m, text, timeout, TRANSLITERATE_SYSTEM)
            if out and not DEVANAGARI_RE.search(out):
                return out
        except Exception as e:  # noqa: BLE001 — any engine error -> next engine
            print(f"voice-typed: transliterate engine failed: {e}", flush=True)
    return text


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


def mode_for_code(code, enhcode, folcode, msgcode):
    if code == msgcode:
        return "message"
    if code == enhcode:
        return "task"
    if code == folcode:
        return "followup"
    return ""


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
        text = transliterate(text)  # Devanagari -> Roman Hindi; no-op for Latin text
        print(
            f"voice-typed: utterance enhance={enhance} text={text[:60]!r}",
            flush=True,
        )
        if enhance:
            notify("✨ enhancing (follow-up)" if enhance == "followup" else "✨ enhancing")
            try:
                text = enhance_prompt(text, enhance, image_path=shot_path)
                text = apply_corrections(text)  # LLM may re-spell; re-normalize known terms
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


def stt_worker(q):
    while True:
        wav_path, window_id, enhance, shot_path = q.get()
        handle_utterance(wav_path, window_id, enhance, shot_path)
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

    migrate_user_files()
    resolve_user_paths()
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
    msgname = os.environ.get("VOICE_TYPED_MSG_KEY", "KEY_F6")
    msgcode = getattr(ecodes, msgname, None)
    if msgcode is None:
        sys.exit(f"unknown VOICE_TYPED_MSG_KEY: {msgname}")

    def mode_for(code):
        return mode_for_code(code, enhcode, folcode, msgcode)
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
        threading.Thread(target=x11_grab_key, args=(msgname,), daemon=True).start()

    rec = None            # active pw-record Popen or None
    wav = None            # Path of in-flight recording
    shot = None           # Path of in-flight screenshot (screenshot-modes only) or None
    deadline = 0.0        # monotonic cutoff for MAX_UTTERANCE_S cap
    awaiting_release = False  # cap fired while key held; swallow next keyup
    rec_key = None        # keycode that started the in-flight recording
    input_mtime = os.stat("/dev/input").st_mtime  # changes on device add/remove

    print(
        f"voice-typed: {len(devs)} device(s), key={keyname}, "
        f"enhance={enhname}, followup={folname}, message={msgname}",
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
                q.put((wav, active_window(), mode_for(rec_key), shot))

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
                    keycode, flagcode, enhcode, folcode, msgcode,
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


if __name__ == "__main__":
    main()
