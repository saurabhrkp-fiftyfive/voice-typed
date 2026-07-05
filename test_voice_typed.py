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
