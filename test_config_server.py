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
