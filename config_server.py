#!/usr/bin/env python3
"""voice-typed web config panel — loopback HTTP server.

Launched by `voice-typed config` as a short-lived separate process. Serves
panel.html + a JSON API that edits the same files the daemon hot-reloads.
Security: loopback bind, per-session token, Host/Origin loopback guard."""
import hmac
import json
import re
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import voice_typed as vt

PANEL_PATH = Path(__file__).resolve().parent / "panel.html"
BINDABLE_KEYS = frozenset(
    [f"KEY_F{n}" for n in range(1, 11)]
    + [f"KEY_{c}" for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"]
    + [f"KEY_{d}" for d in "0123456789"]
)
_LOOPBACK_HOST_RE = re.compile(r"^(127\.0\.0\.1|localhost)(:\d+)?$")
_LOOPBACK_ORIGIN_RE = re.compile(r"^http://(127\.0\.0\.1|localhost)(:\d+)?$")


class ConfigHandler(BaseHTTPRequestHandler):
    server_version = "voice-typed-config"

    def log_message(self, fmt, *args):  # default logger prints URLs (would leak token)
        pass

    def _json(self, status, obj):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _guard(self, token_from):
        """Host/Origin + token check. Returns True when the request may proceed."""
        host = self.headers.get("Host", "")
        if not _LOOPBACK_HOST_RE.match(host):
            self._json(403, {"error": "bad host"})
            return False
        origin = self.headers.get("Origin")
        if origin and not _LOOPBACK_ORIGIN_RE.match(origin):
            self._json(403, {"error": "bad origin"})
            return False
        supplied = token_from or ""
        if not hmac.compare_digest(supplied, self.server.token):
            self._json(403, {"error": "bad token"})
            return False
        self.server.last_request = time.monotonic()
        return True

    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/":
            qtoken = (parse_qs(url.query).get("token") or [""])[0]
            if not self._guard(qtoken):
                return
            body = PANEL_PATH.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if not self._guard(self.headers.get("X-Config-Token")):
            return
        if url.path == "/api/state":
            self._json(200, api_state())
            return
        self._json(404, {"error": "not found"})

    def do_POST(self):
        if not self._guard(self.headers.get("X-Config-Token")):
            return
        length = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._json(400, {"error": "bad json"})
            return
        route = {
            "/api/config": api_config,
            "/api/words": api_words,
            "/api/keys": api_keys,
            "/api/service": api_service,
        }.get(urlparse(self.path).path)
        if route is not None:
            status, obj = route(payload)
            self._json(status, obj)
            return
        if urlparse(self.path).path == "/quit":
            self._json(200, {"ok": True})
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return
        self._json(404, {"error": "not found"})


def _read(path):
    try:
        return Path(path).read_text()
    except OSError:
        return ""


def _service_state():
    active = subprocess.run(
        ["systemctl", "--user", "is-active", "voice-typed"], capture_output=True,
    ).returncode == 0
    log = subprocess.run(
        ["journalctl", "--user", "-u", "voice-typed", "-n", "20", "--no-pager"],
        capture_output=True,
    ).stdout.decode(errors="replace")
    return {"active": active, "log": log}


def api_state():
    vocab_prompt = vt.load_vocab()
    cfg = vt.load_config()
    secrets = {}
    try:
        secrets = vt.load_secrets()
    except OSError:
        pass
    return {
        "config": cfg,
        "vocab": _read(vt.VOCAB_PATH),
        "corrections": [list(p) for p in vt.load_corrections()],
        "flagged": vt.load_flagged(),
        "service": _service_state(),
        "bindable": sorted(BINDABLE_KEYS),
        "budget": {"used": len(vocab_prompt), "max": vt.VOCAB_MAX_CHARS},
        "keys_set": {  # existence only — values never leave the server
            "OPENAI_API_KEY": bool(secrets.get("OPENAI_API_KEY")),
            "GROQ_API_KEY": bool(secrets.get("GROQ_API_KEY")),
        },
    }


def api_config(payload):
    return 501, {"error": "not implemented"}


def api_words(payload):
    return 501, {"error": "not implemented"}


def api_keys(payload):
    return 501, {"error": "not implemented"}


def api_service(payload):
    return 501, {"error": "not implemented"}


def make_server(token, port=0):
    srv = ThreadingHTTPServer(("127.0.0.1", port), ConfigHandler)
    srv.token = token
    srv.last_request = time.monotonic()
    return srv
