import wave
from pathlib import Path
from unittest import mock

import pytest
import requests

import voice_typed as vt


@pytest.fixture
def wav_file(tmp_path):
    p = tmp_path / "utterance.wav"
    with wave.open(str(p), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x00\x00" * 1600)  # 0.1s silence
    return p


@pytest.fixture
def secrets_file(tmp_path):
    p = tmp_path / "secrets.env"
    p.write_text('export OPENAI_API_KEY="sk-test"\nGROQ_API_KEY=gsk-test\n# comment\n')
    return p


def test_load_secrets_parses_export_quotes_comments(secrets_file):
    s = vt.load_secrets(secrets_file)
    assert s["OPENAI_API_KEY"] == "sk-test"
    assert s["GROQ_API_KEY"] == "gsk-test"


def _resp(status=200, text="hello world"):
    r = mock.Mock()
    r.status_code = status
    r.json.return_value = {"text": text}
    if status >= 400:
        r.raise_for_status.side_effect = requests.HTTPError(str(status))
    else:
        r.raise_for_status.return_value = None
    return r


def test_transcribe_openai_success_request_shape(wav_file, secrets_file, monkeypatch):
    monkeypatch.setattr(vt, "SECRETS_PATH", secrets_file)
    with mock.patch.object(vt.requests, "post", return_value=_resp()) as post:
        assert vt.transcribe(wav_file) == "hello world"
        assert post.call_count == 1
        call = post.call_args
        assert call.args[0] == vt.OPENAI_URL
        assert call.kwargs["headers"]["Authorization"] == "Bearer sk-test"
        assert call.kwargs["data"]["model"] == "gpt-4o-transcribe"
        assert call.kwargs["files"]["file"][0] == "utterance.wav"
        assert call.kwargs["files"]["file"][2] == "audio/wav"
        assert call.kwargs["timeout"] == vt.API_TIMEOUT_S


def test_transcribe_falls_back_to_groq(wav_file, secrets_file, monkeypatch):
    monkeypatch.setattr(vt, "SECRETS_PATH", secrets_file)
    with mock.patch.object(
        vt.requests, "post", side_effect=[_resp(500), _resp(text="via groq")]
    ) as post:
        assert vt.transcribe(wav_file) == "via groq"
        assert post.call_count == 2
        assert post.call_args_list[0].args[0] == vt.OPENAI_URL
        assert post.call_args_list[1].args[0] == vt.GROQ_URL
        assert post.call_args_list[1].kwargs["headers"]["Authorization"] == "Bearer gsk-test"
        assert post.call_args_list[1].kwargs["data"]["model"] == "whisper-large-v3"


def test_transcribe_both_fail_raises(wav_file, secrets_file, monkeypatch):
    monkeypatch.setattr(vt, "SECRETS_PATH", secrets_file)
    with mock.patch.object(vt.requests, "post", side_effect=[_resp(500), _resp(500)]):
        with pytest.raises(vt.TranscribeError, match="all engines failed"):
            vt.transcribe(wav_file)


def test_transcribe_whitespace_result_returns_empty(wav_file, secrets_file, monkeypatch):
    monkeypatch.setattr(vt, "SECRETS_PATH", secrets_file)
    with mock.patch.object(vt.requests, "post", return_value=_resp(text="  \n")):
        assert vt.transcribe(wav_file) == ""


def test_transcribe_missing_secrets_file_raises_transcribe_error(wav_file, tmp_path, monkeypatch):
    monkeypatch.setattr(vt, "SECRETS_PATH", tmp_path / "nope.env")
    with pytest.raises(vt.TranscribeError, match="secrets"):
        vt.transcribe(wav_file)


def test_transcribe_no_keys_raises_clear_error(wav_file, tmp_path, monkeypatch):
    empty = tmp_path / "empty.env"
    empty.write_text("# nothing here\n")
    monkeypatch.setattr(vt, "SECRETS_PATH", empty)
    with pytest.raises(vt.TranscribeError, match="OPENAI_API_KEY"):
        vt.transcribe(wav_file)


def test_inject_empty_is_noop():
    with mock.patch.object(vt.subprocess, "run") as run:
        vt.inject("   \n")
        run.assert_not_called()


def test_inject_types_via_xdotool(monkeypatch):
    monkeypatch.delenv("VOICE_TYPED_PASTE", raising=False)
    with mock.patch.object(vt.subprocess, "run") as run:
        vt.inject("hello world")
        cmd = run.call_args.args[0]
        assert cmd[:2] == ["xdotool", "type"]
        assert "--clearmodifiers" in cmd
        assert cmd[-1] == "hello world"
        assert run.call_args.kwargs["timeout"] == 30


def test_inject_paste_mode(monkeypatch):
    monkeypatch.setenv("VOICE_TYPED_PASTE", "1")
    monkeypatch.setattr(vt, "active_window_class", lambda: "google-chrome")
    with mock.patch.object(vt.subprocess, "run") as run:
        vt.inject("hello")
        cmds = [c.args[0] for c in run.call_args_list]
        assert cmds[0][0] == "xclip"
        assert cmds[1][:2] == ["xdotool", "key"]
        assert cmds[1][-1] == "ctrl+v"


def test_active_window_class_parses_xprop():
    wid = mock.Mock(stdout=b"12345678\n")
    xprop = mock.Mock(stdout=b'WM_CLASS(STRING) = "gnome-terminal-server", "Gnome-terminal"\n')
    with mock.patch.object(vt.subprocess, "run", side_effect=[wid, xprop]) as run:
        cls = vt.active_window_class()
        assert "gnome-terminal-server" in cls
        assert run.call_args_list[1].args[0][:2] == ["xprop", "-id"]


def test_paste_chord_terminal_uses_shift(monkeypatch):
    for cls in ("gnome-terminal-server", "kitty", "org.wezfurlong.wezterm"):
        monkeypatch.setattr(vt, "active_window_class", lambda c=cls: c)
        assert vt.paste_chord() == "ctrl+shift+v"


def test_paste_chord_gui_uses_plain(monkeypatch):
    for cls in ("google-chrome", "obsidian", "code", ""):
        monkeypatch.setattr(vt, "active_window_class", lambda c=cls: c)
        assert vt.paste_chord() == "ctrl+v"


def test_inject_terminal_paste_sends_shift_chord(monkeypatch):
    monkeypatch.setattr(vt, "active_window_class", lambda: "gnome-terminal-server")
    with mock.patch.object(vt.subprocess, "run") as run:
        vt.inject("## Task\nprompt", force_paste=True)
        cmds = [c.args[0] for c in run.call_args_list]
        assert cmds[1][-1] == "ctrl+shift+v"


def test_start_recording_spawns_pw_record(tmp_path):
    wav = tmp_path / "sub" / "u.wav"
    with mock.patch.object(vt.subprocess, "Popen") as popen:
        vt.start_recording(wav)
        cmd = popen.call_args.args[0]
        assert cmd[0] == "pw-record"
        assert "--rate" in cmd and "16000" in cmd
        assert wav.parent.exists()


def test_stop_recording_terminates_then_kills():
    proc = mock.Mock()
    proc.wait.side_effect = [vt.subprocess.TimeoutExpired("pw-record", 2), None]
    vt.stop_recording(proc)
    proc.terminate.assert_called_once()
    proc.kill.assert_called_once()


def test_handle_utterance_success_no_guard(wav_file, monkeypatch):
    monkeypatch.setattr(vt, "transcribe", lambda p: "typed text")
    injected = []
    monkeypatch.setattr(vt, "inject", lambda t, force_paste=False: injected.append(t))
    vt.handle_utterance(wav_file, None)  # window_id None -> guard skipped
    assert injected == ["typed text"]
    assert not wav_file.exists()  # cleaned up


def test_handle_utterance_stt_failure_notifies_no_inject(wav_file, monkeypatch):
    def boom(p):
        raise vt.TranscribeError("down")
    monkeypatch.setattr(vt, "transcribe", boom)
    injected = []
    monkeypatch.setattr(vt, "inject", injected.append)
    notes = []
    monkeypatch.setattr(vt, "notify", notes.append)
    vt.handle_utterance(wav_file, None)  # must not raise
    assert injected == []
    assert any("fail" in n.lower() for n in notes)


def test_handle_utterance_focus_changed_drops(wav_file, monkeypatch):
    monkeypatch.setattr(vt, "transcribe", lambda p: "secret text")
    monkeypatch.setattr(vt, "active_window", lambda: "999")
    injected = []
    monkeypatch.setattr(vt, "inject", injected.append)
    notes = []
    monkeypatch.setattr(vt, "notify", notes.append)
    vt.handle_utterance(wav_file, "111")
    assert injected == []
    assert any("focus" in n.lower() for n in notes)


def test_handle_utterance_inject_failure_contained(wav_file, monkeypatch):
    monkeypatch.setattr(vt, "transcribe", lambda p: "text")
    def boom(t, force_paste=False):
        raise FileNotFoundError("xdotool")
    monkeypatch.setattr(vt, "inject", boom)
    notes = []
    monkeypatch.setattr(vt, "notify", notes.append)
    vt.handle_utterance(wav_file, None)  # must not raise
    assert any("inject" in n.lower() for n in notes)


def test_load_vocab_parses_comments_blanks(tmp_path):
    v = tmp_path / "vocab.txt"
    v.write_text("# names\nKubernetes\n\nSaurabh\nRedis\n")
    p = vt.load_vocab(v)
    assert p.startswith("Vocabulary: ")
    assert "Kubernetes" in p and "Saurabh" in p and "Redis" in p
    assert "#" not in p


def test_load_vocab_missing_or_empty_returns_empty(tmp_path):
    assert vt.load_vocab(tmp_path / "nope.txt") == ""
    empty = tmp_path / "empty.txt"
    empty.write_text("# only comment\n\n")
    assert vt.load_vocab(empty) == ""


def test_load_vocab_truncated(tmp_path):
    v = tmp_path / "vocab.txt"
    v.write_text("\n".join(f"word{i}" for i in range(500)))
    assert len(vt.load_vocab(v)) <= vt.VOCAB_MAX_CHARS


def test_transcribe_sends_vocab_prompt(wav_file, secrets_file, tmp_path, monkeypatch):
    monkeypatch.setattr(vt, "SECRETS_PATH", secrets_file)
    v = tmp_path / "vocab.txt"
    v.write_text("Kubernetes\nPostgreSQL\n")
    monkeypatch.setattr(vt, "VOCAB_PATH", v)
    with mock.patch.object(vt.requests, "post", return_value=_resp()) as post:
        vt.transcribe(wav_file)
        prompt = post.call_args.kwargs["data"]["prompt"]
        assert "Kubernetes" in prompt and "PostgreSQL" in prompt


def test_transcribe_no_vocab_no_prompt_key(wav_file, secrets_file, tmp_path, monkeypatch):
    monkeypatch.setattr(vt, "SECRETS_PATH", secrets_file)
    monkeypatch.setattr(vt, "VOCAB_PATH", tmp_path / "nope.txt")
    with mock.patch.object(vt.requests, "post", return_value=_resp()) as post:
        vt.transcribe(wav_file)
        assert "prompt" not in post.call_args.kwargs["data"]


def test_load_corrections_and_apply(tmp_path):
    c = tmp_path / "corrections.txt"
    c.write_text("# fixes\njee brain => Redis\nhyd => Kubernetes\n")
    assert vt.apply_corrections("Jee Brain and hyd rock", c) == "Redis and Kubernetes rock"


def test_apply_corrections_missing_file_passthrough(tmp_path):
    assert vt.apply_corrections("hello", tmp_path / "nope.txt") == "hello"


def test_handle_utterance_applies_corrections(wav_file, monkeypatch, tmp_path):
    monkeypatch.setattr(vt, "transcribe", lambda p: "jee brain")
    c = tmp_path / "c.txt"
    c.write_text("jee brain => Redis\n")
    monkeypatch.setattr(vt, "CORRECTIONS_PATH", c)
    injected = []
    monkeypatch.setattr(vt, "inject", lambda t, force_paste=False: injected.append(t))
    vt.handle_utterance(wav_file, None)
    assert injected == ["Redis"]


def test_flag_last_appends_to_flagged_md(tmp_path, monkeypatch):
    monkeypatch.setattr(vt, "FLAGGED_PATH", tmp_path / "flagged.md")
    monkeypatch.setattr(vt, "LAST_TEXT", "jee brain rocks")
    notes = []
    monkeypatch.setattr(vt, "notify", notes.append)
    vt.flag_last()
    content = (tmp_path / "flagged.md").read_text()
    assert "jee brain rocks" in content and "⚑" in content
    assert any("flagged" in n for n in notes)


def test_flag_last_empty_notifies_no_file(tmp_path, monkeypatch):
    monkeypatch.setattr(vt, "FLAGGED_PATH", tmp_path / "flagged.md")
    monkeypatch.setattr(vt, "LAST_TEXT", "")
    notes = []
    monkeypatch.setattr(vt, "notify", notes.append)
    vt.flag_last()
    assert not (tmp_path / "flagged.md").exists()
    assert notes


def _chat_resp(status=200, content="engineered prompt"):
    r = mock.Mock()
    r.status_code = status
    r.json.return_value = {"choices": [{"message": {"content": content}}]}
    if status >= 400:
        r.raise_for_status.side_effect = requests.HTTPError(str(status))
    else:
        r.raise_for_status.return_value = None
    return r


def test_enhance_prompt_openai_request_shape(secrets_file, monkeypatch):
    monkeypatch.setattr(vt, "SECRETS_PATH", secrets_file)
    monkeypatch.delenv("VOICE_TYPED_ENHANCE_MODEL", raising=False)
    with mock.patch.object(vt.requests, "post", return_value=_chat_resp()) as post:
        assert vt.enhance_prompt("fix bug") == "engineered prompt"
        call = post.call_args
        assert call.args[0] == vt.OPENAI_CHAT_URL
        assert call.kwargs["headers"]["Authorization"] == "Bearer sk-test"
        body = call.kwargs["json"]
        assert body["model"] == "gpt-4o-mini"
        assert body["messages"][0]["role"] == "system"
        assert body["messages"][1] == {"role": "user", "content": "fix bug"}
        assert call.kwargs["timeout"] == vt.API_TIMEOUT_S


def test_enhance_prompt_falls_back_to_groq(secrets_file, monkeypatch):
    monkeypatch.setattr(vt, "SECRETS_PATH", secrets_file)
    with mock.patch.object(
        vt.requests, "post",
        side_effect=[_chat_resp(500), _chat_resp(content="via groq")],
    ) as post:
        assert vt.enhance_prompt("fix bug") == "via groq"
        assert post.call_args_list[0].args[0] == vt.OPENAI_CHAT_URL
        assert post.call_args_list[1].args[0] == vt.GROQ_CHAT_URL
        assert post.call_args_list[1].kwargs["json"]["model"] == vt.GROQ_ENHANCE_MODEL


def test_enhance_prompt_both_fail_raises(secrets_file, monkeypatch):
    monkeypatch.setattr(vt, "SECRETS_PATH", secrets_file)
    with mock.patch.object(
        vt.requests, "post", side_effect=[_chat_resp(500), _chat_resp(500)]
    ):
        with pytest.raises(vt.EnhanceError, match="all engines failed"):
            vt.enhance_prompt("fix bug")


def test_enhance_prompt_model_env_override(secrets_file, monkeypatch):
    monkeypatch.setattr(vt, "SECRETS_PATH", secrets_file)
    monkeypatch.setenv("VOICE_TYPED_ENHANCE_MODEL", "gpt-5-mini")
    with mock.patch.object(vt.requests, "post", return_value=_chat_resp()) as post:
        vt.enhance_prompt("x")
        assert post.call_args.kwargs["json"]["model"] == "gpt-5-mini"


def test_enhance_prompt_task_uses_enhance_system(secrets_file, monkeypatch):
    monkeypatch.setattr(vt, "SECRETS_PATH", secrets_file)
    with mock.patch.object(vt.requests, "post", return_value=_chat_resp()) as post:
        vt.enhance_prompt("x")  # default mode="task"
        assert post.call_args.kwargs["json"]["messages"][0]["content"].startswith(vt.ENHANCE_SYSTEM)


def test_enhance_prompt_followup_uses_followup_system(secrets_file, monkeypatch):
    monkeypatch.setattr(vt, "SECRETS_PATH", secrets_file)
    with mock.patch.object(vt.requests, "post", return_value=_chat_resp()) as post:
        vt.enhance_prompt("x", "followup")
        assert post.call_args.kwargs["json"]["messages"][0]["content"].startswith(vt.FOLLOWUP_SYSTEM)


def test_inject_force_paste_overrides_type(monkeypatch):
    monkeypatch.delenv("VOICE_TYPED_PASTE", raising=False)
    monkeypatch.setattr(vt, "active_window_class", lambda: "obsidian")
    with mock.patch.object(vt.subprocess, "run") as run:
        vt.inject("multi\nline", force_paste=True)
        cmds = [c.args[0] for c in run.call_args_list]
        assert cmds[0][0] == "xclip"
        assert cmds[1][:2] == ["xdotool", "key"]


def test_handle_utterance_enhance_forces_paste(wav_file, monkeypatch):
    monkeypatch.setattr(vt, "transcribe", lambda p: "raw ramble")
    monkeypatch.setattr(vt, "enhance_prompt", lambda t, mode="task", image_path=None: "Task: clean prompt")
    calls = []
    monkeypatch.setattr(
        vt, "inject", lambda t, force_paste=False: calls.append((t, force_paste))
    )
    vt.handle_utterance(wav_file, None, enhance="task")
    assert calls == [("Task: clean prompt", True)]
    assert vt.LAST_TEXT == "Task: clean prompt"


def test_handle_utterance_followup_passes_mode(wav_file, monkeypatch):
    monkeypatch.setattr(vt, "transcribe", lambda p: "raw ramble")
    seen = []
    monkeypatch.setattr(
        vt, "enhance_prompt", lambda t, mode="task", image_path=None: seen.append(mode) or "also do X"
    )
    calls = []
    monkeypatch.setattr(
        vt, "inject", lambda t, force_paste=False: calls.append((t, force_paste))
    )
    vt.handle_utterance(wav_file, None, enhance="followup")
    assert seen == ["followup"]
    assert calls == [("also do X", True)]


def test_handle_utterance_enhance_failure_injects_raw(wav_file, monkeypatch):
    monkeypatch.setattr(vt, "transcribe", lambda p: "raw ramble")
    def boom(t, mode="task", image_path=None):
        raise vt.EnhanceError("down")
    monkeypatch.setattr(vt, "enhance_prompt", boom)
    calls = []
    monkeypatch.setattr(
        vt, "inject", lambda t, force_paste=False: calls.append((t, force_paste))
    )
    notes = []
    monkeypatch.setattr(vt, "notify", notes.append)
    vt.handle_utterance(wav_file, None, enhance="task")
    assert calls == [("raw ramble", True)]
    assert any("enhance failed" in n for n in notes)


def test_handle_utterance_plain_never_enhances(wav_file, monkeypatch):
    monkeypatch.setattr(vt, "transcribe", lambda p: "plain")
    def boom(t):
        raise AssertionError("enhance called in plain mode")
    monkeypatch.setattr(vt, "enhance_prompt", boom)
    calls = []
    monkeypatch.setattr(
        vt, "inject", lambda t, force_paste=False: calls.append((t, force_paste))
    )
    vt.handle_utterance(wav_file, None)
    assert calls == [("plain", False)]


def test_rescan_devices_adds_new_closes_dupes(monkeypatch):
    old = mock.Mock(path="/dev/input/event3")
    dupe = mock.Mock(path="/dev/input/event3")
    fresh = mock.Mock(path="/dev/input/event6")
    monkeypatch.setattr(vt, "find_keyboards", lambda kc: ([dupe, fresh], 0))
    devs = [old]
    added = vt.rescan_devices(devs, 67)
    assert added == 1
    assert devs == [old, fresh]
    dupe.close.assert_called_once()
    fresh.close.assert_not_called()


def test_rescan_devices_no_new(monkeypatch):
    old = mock.Mock(path="/dev/input/event3")
    dupe = mock.Mock(path="/dev/input/event3")
    monkeypatch.setattr(vt, "find_keyboards", lambda kc: ([dupe], 0))
    devs = [old]
    assert vt.rescan_devices(devs, 67) == 0
    assert devs == [old]
    dupe.close.assert_called_once()


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
        assert post.call_args.kwargs["json"]["messages"][0]["content"].startswith(vt.MSG_SYSTEM)


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


def test_enhance_prompt_appends_vocab_spelling_guard(secrets_file, tmp_path, monkeypatch):
    monkeypatch.setattr(vt, "SECRETS_PATH", secrets_file)
    v = tmp_path / "vocab.txt"
    v.write_text("Ada\nLinus\n")
    monkeypatch.setattr(vt, "VOCAB_PATH", v)
    with mock.patch.object(vt.requests, "post", return_value=_chat_resp()) as post:
        vt.enhance_prompt("x", "message")
        system = post.call_args.kwargs["json"]["messages"][0]["content"]
        assert "Ada" in system and "Linus" in system
        assert vt.MSG_SYSTEM in system  # guard is appended, base prompt intact


def test_enhance_prompt_no_vocab_no_guard(secrets_file, tmp_path, monkeypatch):
    monkeypatch.setattr(vt, "SECRETS_PATH", secrets_file)
    monkeypatch.setattr(vt, "VOCAB_PATH", tmp_path / "nope.txt")
    with mock.patch.object(vt.requests, "post", return_value=_chat_resp()) as post:
        vt.enhance_prompt("x", "message")
        assert post.call_args.kwargs["json"]["messages"][0]["content"] == vt.MSG_SYSTEM


def test_handle_utterance_reapplies_corrections_after_enhance(wav_file, tmp_path, monkeypatch):
    monkeypatch.setattr(vt, "transcribe", lambda p: "raw")
    monkeypatch.setattr(
        vt, "enhance_prompt", lambda t, mode="task", image_path=None: "call Linuz now"
    )
    c = tmp_path / "c.txt"
    c.write_text("Linuz => Linus\n")
    monkeypatch.setattr(vt, "CORRECTIONS_PATH", c)
    calls = []
    monkeypatch.setattr(vt, "inject", lambda t, force_paste=False: calls.append(t))
    vt.handle_utterance(wav_file, None, enhance="message")
    assert calls == ["call Linus now"]


# ── config layer ─────────────────────────────────────────────


def test_load_config_missing_file_returns_defaults(tmp_path):
    cfg = vt.load_config(tmp_path / "none.toml")
    assert cfg["keys"]["dictate"] == "KEY_F9"
    assert cfg["keys"]["flag"] == "KEY_F10"
    assert cfg["engines"]["enhance_model"] == "gpt-4o-mini"
    assert cfg["behavior"]["paste_mode"] is False
    assert cfg["behavior"]["grab_keys"] is True


def test_load_config_partial_file_merges_over_defaults(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text('[keys]\ndictate = "KEY_F5"\n\n[behavior]\npaste_mode = true\n')
    cfg = vt.load_config(p)
    assert cfg["keys"]["dictate"] == "KEY_F5"
    assert cfg["keys"]["enhance"] == "KEY_F8"          # untouched default
    assert cfg["behavior"]["paste_mode"] is True
    assert cfg["engines"]["api_timeout_s"] == 30


def test_load_config_malformed_toml_falls_back_to_defaults(tmp_path, capsys):
    p = tmp_path / "config.toml"
    p.write_text("not [valid toml ===")
    cfg = vt.load_config(p)
    assert cfg["keys"]["dictate"] == "KEY_F9"
    assert "config" in capsys.readouterr().out


def test_load_config_unknown_keys_ignored(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text('[keys]\nbogus = "KEY_F1"\n\n[wat]\nx = 1\n')
    cfg = vt.load_config(p)
    assert "bogus" not in cfg["keys"]
    assert "wat" not in cfg


def test_load_config_env_overrides_toml(tmp_path, monkeypatch):
    p = tmp_path / "config.toml"
    p.write_text('[keys]\ndictate = "KEY_F5"\n')
    monkeypatch.setenv("VOICE_TYPED_KEY", "KEY_F2")
    monkeypatch.setenv("VOICE_TYPED_PASTE", "1")
    monkeypatch.setenv("VOICE_TYPED_GRAB", "0")
    monkeypatch.setenv("VOICE_TYPED_ENHANCE_MODEL", "gpt-x")
    cfg = vt.load_config(p)
    assert cfg["keys"]["dictate"] == "KEY_F2"
    assert cfg["behavior"]["paste_mode"] is True
    assert cfg["behavior"]["grab_keys"] is False
    assert cfg["engines"]["enhance_model"] == "gpt-x"


def test_dump_config_round_trips(tmp_path):
    cfg = vt.load_config(tmp_path / "none.toml")
    cfg["keys"]["dictate"] = "KEY_F3"
    cfg["behavior"]["paste_mode"] = True
    out = tmp_path / "out.toml"
    out.write_text(vt.dump_config(cfg))
    assert vt.load_config(out) == cfg


def test_migrate_copies_missing_targets(tmp_path):
    legacy = tmp_path / "repo"; legacy.mkdir()
    cfg_d = tmp_path / "cfg"; data_d = tmp_path / "data"
    (legacy / "vocab.txt").write_text("Kubernetes\n")
    (legacy / "corrections.txt").write_text("json => JSON\n")
    (legacy / "flagged.md").write_text("- x\n")
    moved = vt.migrate_user_files(cfg_d, data_d, legacy)
    assert sorted(moved) == ["corrections.txt", "flagged.md", "vocab.txt"]
    assert (cfg_d / "vocab.txt").read_text() == "Kubernetes\n"
    assert (data_d / "flagged.md").read_text() == "- x\n"
    assert (legacy / "vocab.txt").exists()  # copy, not move


def test_migrate_skips_existing_and_missing(tmp_path):
    legacy = tmp_path / "repo"; legacy.mkdir()
    cfg_d = tmp_path / "cfg"; cfg_d.mkdir(); data_d = tmp_path / "data"
    (legacy / "vocab.txt").write_text("OLD\n")
    (cfg_d / "vocab.txt").write_text("NEW\n")   # target exists -> untouched
    moved = vt.migrate_user_files(cfg_d, data_d, legacy)
    assert moved == []                            # corrections/flagged absent -> no-op
    assert (cfg_d / "vocab.txt").read_text() == "NEW\n"


def test_resolve_user_paths_prefers_xdg(tmp_path, monkeypatch):
    cfg_d = tmp_path / "cfg"; cfg_d.mkdir()
    data_d = tmp_path / "data"; data_d.mkdir()
    (cfg_d / "vocab.txt").write_text("x\n")
    monkeypatch.setattr(vt, "CONFIG_DIR", cfg_d)
    monkeypatch.setattr(vt, "DATA_DIR", data_d)
    monkeypatch.setattr(vt, "VOCAB_PATH", vt.VOCAB_PATH)          # auto-restore
    monkeypatch.setattr(vt, "CORRECTIONS_PATH", vt.CORRECTIONS_PATH)
    monkeypatch.setattr(vt, "FLAGGED_PATH", vt.FLAGGED_PATH)
    legacy_corrections = vt.CORRECTIONS_PATH
    vt.resolve_user_paths()
    assert vt.VOCAB_PATH == cfg_d / "vocab.txt"
    assert vt.CORRECTIONS_PATH == legacy_corrections   # no XDG file -> legacy stays


def test_enhance_model_comes_from_config_toml(wav_file, secrets_file, tmp_path, monkeypatch):
    monkeypatch.setattr(vt, "SECRETS_PATH", secrets_file)
    p = tmp_path / "config.toml"
    p.write_text('[engines]\nenhance_model = "my-model"\n')
    monkeypatch.setattr(vt, "CONFIG_PATH", p)
    r = mock.Mock(); r.raise_for_status.return_value = None
    r.json.return_value = {"choices": [{"message": {"content": "out"}}]}
    with mock.patch.object(vt.requests, "post", return_value=r) as post:
        vt.enhance_prompt("hi")
        assert post.call_args.kwargs["json"]["model"] == "my-model"


def test_inject_paste_mode_from_config(tmp_path, monkeypatch):
    p = tmp_path / "config.toml"
    p.write_text("[behavior]\npaste_mode = true\n")
    monkeypatch.setattr(vt, "CONFIG_PATH", p)
    monkeypatch.delenv("VOICE_TYPED_PASTE", raising=False)
    with mock.patch.object(vt.subprocess, "run") as run:
        with mock.patch.object(vt, "paste_chord", return_value="ctrl+v"):
            vt.inject("hello")
        assert run.call_args_list[0].args[0][:2] == ["xclip", "-selection"]


def test_doctor_all_green_returns_zero(secrets_file, monkeypatch, capsys):
    monkeypatch.setattr(vt, "SECRETS_PATH", secrets_file)
    monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
    monkeypatch.setattr(vt, "find_keyboards", lambda code: ([mock.Mock()], 0))
    ok_run = mock.Mock(returncode=0, stdout=b"user input\n")
    with mock.patch.object(vt.subprocess, "run", return_value=ok_run):
        with mock.patch.object(vt.shutil, "which", return_value="/usr/bin/x"):
            assert vt.doctor() == 0
    out = capsys.readouterr().out
    assert "❌" not in out


def test_doctor_wayland_fails(secrets_file, monkeypatch, capsys):
    monkeypatch.setattr(vt, "SECRETS_PATH", secrets_file)
    monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
    monkeypatch.setattr(vt, "find_keyboards", lambda code: ([mock.Mock()], 0))
    ok_run = mock.Mock(returncode=0, stdout=b"user input\n")
    with mock.patch.object(vt.subprocess, "run", return_value=ok_run):
        with mock.patch.object(vt.shutil, "which", return_value="/usr/bin/x"):
            assert vt.doctor() == 1
    assert "wayland" in capsys.readouterr().out.lower()


def test_doctor_missing_keys_fails(tmp_path, monkeypatch):
    empty = tmp_path / "e.env"; empty.write_text("# none\n")
    monkeypatch.setattr(vt, "SECRETS_PATH", empty)
    monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
    monkeypatch.setattr(vt, "find_keyboards", lambda code: ([mock.Mock()], 0))
    ok_run = mock.Mock(returncode=0, stdout=b"user input\n")
    with mock.patch.object(vt.subprocess, "run", return_value=ok_run):
        with mock.patch.object(vt.shutil, "which", return_value="/usr/bin/x"):
            assert vt.doctor() == 1


def test_cli_status_calls_systemctl(monkeypatch):
    with mock.patch.object(vt.subprocess, "run", return_value=mock.Mock(returncode=0)) as run:
        assert vt.cli(["status"]) == 0
        assert run.call_args.args[0] == [
            "systemctl", "--user", "status", "voice-typed", "--no-pager"]


def test_cli_logs_calls_journalctl(monkeypatch):
    with mock.patch.object(vt.subprocess, "run", return_value=mock.Mock(returncode=0)) as run:
        assert vt.cli(["logs"]) == 0
        assert run.call_args.args[0][0] == "journalctl"


def test_load_flagged_parses_entries(tmp_path):
    p = tmp_path / "flagged.md"
    p.write_text(
        '# junk header\n'
        '- 2026-07-15 10:30 ⚑ "kube sweet" → Kubernetes Suite\n'
        '- 2026-07-16 09:00 ⚑ "no note yet" → \n'
        'not a flag line\n'
    )
    entries = vt.load_flagged(p)
    assert entries == [
        {"ts": "2026-07-15 10:30", "text": "kube sweet", "note": "Kubernetes Suite"},
        {"ts": "2026-07-16 09:00", "text": "no note yet", "note": ""},
    ]


def test_load_flagged_missing_file_empty(tmp_path):
    assert vt.load_flagged(tmp_path / "nope.md") == []


def test_cli_config_launches_server(monkeypatch):
    import config_server
    called = {}
    monkeypatch.setattr(config_server, "run",
                        lambda open_browser=True: called.setdefault("ob", open_browser) or 0)
    assert vt.cli(["config", "--no-browser"]) == 0
    assert called["ob"] is False
