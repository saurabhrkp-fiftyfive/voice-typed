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


def test_post_config_writes_toml_and_flags_restart(server, user_files):
    status, body = _req(server, "POST", "/api/config",
                        body={"keys": {"dictate": "KEY_F5"}})
    assert status == 200
    assert json.loads(body) == {"ok": True, "restart_required": True}
    assert vt.load_config()["keys"]["dictate"] == "KEY_F5"
    assert vt.load_config()["keys"]["enhance"] == "KEY_F8"   # partial update


def test_post_config_behavior_only_no_restart(server, user_files):
    status, body = _req(server, "POST", "/api/config",
                        body={"behavior": {"paste_mode": True}})
    assert json.loads(body)["restart_required"] is False


def test_post_config_rejects_unbindable_key(server, user_files):
    status, body = _req(server, "POST", "/api/config",
                        body={"keys": {"dictate": "KEY_F11"}})
    assert status == 422
    assert vt.load_config()["keys"]["dictate"] == "KEY_F9"   # nothing written


def test_post_config_rejects_duplicate_keys(server, user_files):
    status, _ = _req(server, "POST", "/api/config",
                     body={"keys": {"dictate": "KEY_F8"}})   # collides with enhance
    assert status == 422


def test_post_config_rejects_bad_timeout(server, user_files):
    status, _ = _req(server, "POST", "/api/config",
                     body={"engines": {"api_timeout_s": -5}})
    assert status == 422


def test_post_words_vocab_and_corrections(server, user_files):
    cfg_d, _ = user_files
    status, _ = _req(server, "POST", "/api/words", body={
        "vocab": "Kubernetes\nGraphQL\n",
        "corrections": [["cloud code", "Claude Code"], ["zoofie", "GraphQL"]],
    })
    assert status == 200
    assert (cfg_d / "vocab.txt").read_text() == "Kubernetes\nGraphQL\n"
    assert vt.load_corrections(cfg_d / "corrections.txt") == [
        ("cloud code", "Claude Code"), ("zoofie", "GraphQL")]


def test_post_words_archive_flagged(server, user_files):
    _, data_d = user_files
    (data_d / "flagged.md").write_text(
        '- 2026-07-15 10:30 ⚑ "keep me" → \n'
        '- 2026-07-16 09:00 ⚑ "archive me" → fixed\n'
    )
    status, _ = _req(server, "POST", "/api/words",
                     body={"archive_flagged": ["2026-07-16 09:00"]})
    assert status == 200
    assert [e["ts"] for e in vt.load_flagged()] == ["2026-07-15 10:30"]
    assert "archive me" in (data_d / "flagged-archive.md").read_text()


def test_post_words_rejects_bad_corrections_shape(server, user_files):
    status, _ = _req(server, "POST", "/api/words",
                     body={"corrections": ["not-a-pair"]})
    assert status == 422


def test_post_keys_writes_0600_and_merges(server, user_files):
    cfg_d, _ = user_files
    (cfg_d / "secrets.env").write_text("GROQ_API_KEY=gsk-old\n")
    status, body = _req(server, "POST", "/api/keys",
                        body={"OPENAI_API_KEY": "sk-new", "GROQ_API_KEY": ""})
    assert status == 200
    assert b"sk-new" not in body
    s = vt.load_secrets(cfg_d / "secrets.env")
    assert s["OPENAI_API_KEY"] == "sk-new"
    assert s["GROQ_API_KEY"] == "gsk-old"          # empty string = leave
    mode = (cfg_d / "secrets.env").stat().st_mode & 0o777
    assert mode == 0o600


def test_post_keys_rejects_unknown_field(server, user_files):
    status, _ = _req(server, "POST", "/api/keys", body={"EVIL": "x"})
    assert status == 422


def test_post_service_restart(server, user_files):
    with mock.patch.object(cs.subprocess, "run",
                           return_value=mock.Mock(returncode=0, stdout=b"", stderr=b"")) as run:
        status, body = _req(server, "POST", "/api/service", body={"action": "restart"})
    assert status == 200
    assert json.loads(body)["ok"] is True
    assert ["systemctl", "--user", "restart", "voice-typed"] in [
        c.args[0] for c in run.call_args_list]


def test_post_service_rejects_unknown_action(server, user_files):
    status, _ = _req(server, "POST", "/api/service", body={"action": "explode"})
    assert status == 422


def test_quit_shuts_server_down(user_files):
    srv = cs.make_server(token="tok-test", port=0)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    status, _ = _req(srv, "POST", "/quit")
    assert status == 200
    t.join(timeout=5)
    assert not t.is_alive()
    srv.server_close()


def test_run_prints_url_and_serves(capsys, monkeypatch):
    opened = {}
    monkeypatch.setattr(cs.webbrowser, "open", lambda url: opened.setdefault("url", url))

    def fake_serve(self):          # shut down immediately instead of serving
        pass
    monkeypatch.setattr(cs.ThreadingHTTPServer, "serve_forever", fake_serve)
    monkeypatch.setattr(vt, "migrate_user_files", lambda *a, **k: [])
    monkeypatch.setattr(vt, "resolve_user_paths", lambda: None)
    assert cs.run(open_browser=True) == 0
    out = capsys.readouterr().out
    assert "http://127.0.0.1:" in out and "?token=" in out
    assert opened["url"].startswith("http://127.0.0.1:")
