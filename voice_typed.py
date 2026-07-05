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
SECRETS_PATH = Path.home() / ".config" / "secrets.env"
VOCAB_PATH = Path(__file__).resolve().parent / "vocab.txt"
VOCAB_MAX_CHARS = 800  # ~200 tokens; whisper prompt cap is 224 tokens
CORRECTIONS_PATH = Path(__file__).resolve().parent / "corrections.txt"
FLAGGED_PATH = Path(__file__).resolve().parent / "flagged.md"
LAST_TEXT = ""  # last typed transcript, held in memory for ⚑ flagging only
MAX_UTTERANCE_S = 60
API_TIMEOUT_S = 30  # per-engine connect+read timeout, NOT total deadline


class TranscribeError(Exception):
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


def inject(text):
    if not text or not text.strip():
        return
    if os.environ.get("VOICE_TYPED_PASTE") == "1":
        subprocess.run(
            ["xclip", "-selection", "clipboard"],
            input=text.encode(), check=True, timeout=10,
        )
        subprocess.run(
            ["xdotool", "key", "--clearmodifiers", "ctrl+v"],
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


def handle_utterance(wav_path, window_id):
    """Transcribe + inject one utterance. Never raises; deletes wav."""
    try:
        try:
            text = transcribe(wav_path)
        except TranscribeError as e:
            notify(f"transcription failed: {e}")
            return
        if not text:
            return  # silence -> type nothing
        text = apply_corrections(text)
        global LAST_TEXT
        LAST_TEXT = text
        if window_id is not None:
            current = active_window()
            if current is not None and current != window_id:
                notify(f"focus changed — dropped: {text[:60]}")
                return
        try:
            inject(text)
        except Exception as e:  # noqa: BLE001 — daemon must survive
            notify(f"injection failed: {e}")
    finally:
        Path(wav_path).unlink(missing_ok=True)


def stt_worker(q):
    while True:
        wav_path, window_id = q.get()
        handle_utterance(wav_path, window_id)
        q.task_done()


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
    devs, denied = find_keyboards(keycode)
    if not devs:
        sys.exit(
            f"no readable keyboard with {keyname} ({denied} device(s) denied). "
            "Is the user in the 'input' group? (sudo usermod -aG input $USER, re-login)"
        )
    run_dir = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp")) / "voice-typed"
    q = queue.Queue()
    threading.Thread(target=stt_worker, args=(q,), daemon=True).start()

    rec = None            # active pw-record Popen or None
    wav = None            # Path of in-flight recording
    deadline = 0.0        # monotonic cutoff for 60s cap
    awaiting_release = False  # cap fired while key held; swallow next keyup

    print(f"voice-typed: {len(devs)} device(s), key={keyname}", flush=True)
    while True:
        if rec is None:
            timeout = 1.0
        else:
            timeout = max(0.05, min(1.0, deadline - time.monotonic()))
        r, _, _ = select.select(devs, [], [], timeout)

        if rec is not None:
            if rec.poll() is not None:  # recorder died mid-recording
                notify(f"recorder died (exit {rec.returncode})")
                rec = None
            elif time.monotonic() >= deadline:  # 60s cap
                stop_recording(rec)
                rec = None
                awaiting_release = True
                notify("60s cap — transcribing")
                q.put((wav, active_window()))

        for d in r:
            try:
                events = list(d.read())
            except OSError:
                continue
            for ev in events:
                if ev.type != ecodes.EV_KEY or ev.code not in (keycode, flagcode):
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
                    deadline = time.monotonic() + MAX_UTTERANCE_S
                    notify("🎙 recording")
                elif ev.value == 0:  # keyup (value 2 autorepeat ignored)
                    if awaiting_release:
                        awaiting_release = False
                    elif rec is not None:
                        stop_recording(rec)
                        rec = None
                        notify("… transcribing")
                        q.put((wav, active_window()))


if __name__ == "__main__":
    main()
