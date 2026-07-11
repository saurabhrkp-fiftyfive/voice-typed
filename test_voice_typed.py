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
    with mock.patch.object(vt.subprocess, "run") as run:
        vt.inject("hello")
        cmds = [c.args[0] for c in run.call_args_list]
        assert cmds[0][0] == "xclip"
        assert cmds[1][:2] == ["xdotool", "key"]


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


def test_inject_force_paste_overrides_type(monkeypatch):
    monkeypatch.delenv("VOICE_TYPED_PASTE", raising=False)
    with mock.patch.object(vt.subprocess, "run") as run:
        vt.inject("multi\nline", force_paste=True)
        cmds = [c.args[0] for c in run.call_args_list]
        assert cmds[0][0] == "xclip"
        assert cmds[1][:2] == ["xdotool", "key"]


def test_handle_utterance_enhance_forces_paste(wav_file, monkeypatch):
    monkeypatch.setattr(vt, "transcribe", lambda p: "raw ramble")
    monkeypatch.setattr(vt, "enhance_prompt", lambda t: "## Task\nclean prompt")
    calls = []
    monkeypatch.setattr(
        vt, "inject", lambda t, force_paste=False: calls.append((t, force_paste))
    )
    vt.handle_utterance(wav_file, None, enhance=True)
    assert calls == [("## Task\nclean prompt", True)]
    assert vt.LAST_TEXT == "## Task\nclean prompt"


def test_handle_utterance_enhance_failure_injects_raw(wav_file, monkeypatch):
    monkeypatch.setattr(vt, "transcribe", lambda p: "raw ramble")
    def boom(t):
        raise vt.EnhanceError("down")
    monkeypatch.setattr(vt, "enhance_prompt", boom)
    calls = []
    monkeypatch.setattr(
        vt, "inject", lambda t, force_paste=False: calls.append((t, force_paste))
    )
    notes = []
    monkeypatch.setattr(vt, "notify", notes.append)
    vt.handle_utterance(wav_file, None, enhance=True)
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
