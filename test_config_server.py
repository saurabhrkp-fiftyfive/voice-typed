import http.client
import json
import threading
from unittest import mock

import pytest

import config_server as cs
import voice_typed as vt


@pytest.fixture
def server():
    srv = cs.make_server(token="tok-test", port=0)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv
    srv.shutdown()
    srv.server_close()


def _req(srv, method, path, body=None, token="tok-test", host=None, origin=None):
    conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=5)
    headers = {}
    if token is not None:
        headers["X-Config-Token"] = token
    if host:
        headers["Host"] = host
    if origin:
        headers["Origin"] = origin
    conn.request(method, path, body=json.dumps(body) if body is not None else None,
                 headers=headers)
    r = conn.getresponse()
    data = r.read()
    conn.close()
    return r.status, data


def test_panel_requires_token_query(server):
    status, _ = _req(server, "GET", "/", token=None)
    assert status == 403
    status, body = _req(server, "GET", "/?token=tok-test", token=None)
    assert status == 200
    assert b"voice-typed" in body


def test_api_bad_token_403(server):
    status, body = _req(server, "GET", "/api/state", token="wrong")
    assert status == 403
    assert json.loads(body) == {"error": "bad token"}


def test_bad_host_403(server):
    status, _ = _req(server, "GET", "/api/state", host="evil.example.com")
    assert status == 403


def test_bad_origin_403(server):
    status, _ = _req(server, "GET", "/api/state", origin="https://evil.example.com")
    assert status == 403


def test_loopback_origin_ok(server, tmp_path, monkeypatch):
    monkeypatch.setattr(vt, "CONFIG_PATH", tmp_path / "none.toml")
    port = server.server_address[1]
    status, _ = _req(server, "GET", "/api/state",
                     origin=f"http://127.0.0.1:{port}")
    assert status == 200


def test_unknown_path_404(server):
    status, _ = _req(server, "GET", "/api/nope")
    assert status == 404


@pytest.fixture
def user_files(tmp_path, monkeypatch):
    """Point every vt path at tmp so API tests never touch real files."""
    cfg_d = tmp_path / "cfg"; cfg_d.mkdir()
    data_d = tmp_path / "data"; data_d.mkdir()
    monkeypatch.setattr(vt, "CONFIG_DIR", cfg_d)
    monkeypatch.setattr(vt, "DATA_DIR", data_d)
    monkeypatch.setattr(vt, "CONFIG_PATH", cfg_d / "config.toml")
    monkeypatch.setattr(vt, "VOCAB_PATH", cfg_d / "vocab.txt")
    monkeypatch.setattr(vt, "CORRECTIONS_PATH", cfg_d / "corrections.txt")
    monkeypatch.setattr(vt, "FLAGGED_PATH", data_d / "flagged.md")
    monkeypatch.setattr(vt, "SECRETS_PATH", cfg_d / "secrets.env")
    return cfg_d, data_d


def test_state_shape(server, user_files):
    cfg_d, data_d = user_files
    (cfg_d / "vocab.txt").write_text("Kubernetes\nPostgreSQL\n")
    (cfg_d / "corrections.txt").write_text("cloud code => Claude Code\n")
    (cfg_d / "secrets.env").write_text("OPENAI_API_KEY=sk-supersecret\n")
    (data_d / "flagged.md").write_text('- 2026-07-16 09:00 ⚑ "x" → \n')
    with mock.patch.object(cs.subprocess, "run",
                           return_value=mock.Mock(returncode=0, stdout=b"log line\n")):
        status, body = _req(server, "GET", "/api/state")
    assert status == 200
    st = json.loads(body)
    assert st["config"]["keys"]["dictate"] == "KEY_F9"
    assert st["vocab"] == "Kubernetes\nPostgreSQL\n"
    assert st["corrections"] == [["cloud code", "Claude Code"]]
    assert st["flagged"] == [{"ts": "2026-07-16 09:00", "text": "x", "note": ""}]
    assert st["service"]["active"] is True
    assert "KEY_F9" in st["bindable"] and "KEY_F11" not in st["bindable"]
    assert st["budget"]["max"] == vt.VOCAB_MAX_CHARS
    assert st["budget"]["used"] == len("Vocabulary: Kubernetes, PostgreSQL")
    assert st["keys_set"] == {"OPENAI_API_KEY": True, "GROQ_API_KEY": False}
    assert "sk-supersecret" not in body.decode()   # key material never leaves server
