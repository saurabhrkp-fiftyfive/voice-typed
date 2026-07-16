#!/usr/bin/env python3
"""voice-typed web config panel — loopback HTTP server.

Launched by `voice-typed config` as a short-lived separate process. Serves
panel.html + a JSON API that edits the same files the daemon hot-reloads.
Security: loopback bind, per-session token, Host/Origin loopback guard."""
import hmac
import json
import re
import secrets
import subprocess
import threading
import time
import webbrowser
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
    cfg = vt.load_config()
    new = {sec: dict(vals) for sec, vals in cfg.items()}
    for sec in ("keys", "engines", "behavior"):
        for k, v in (payload.get(sec) or {}).items():
            if k in new[sec]:
                new[sec][k] = v
    for action, name in new["keys"].items():
        if name not in BINDABLE_KEYS:
            return 422, {"error": f"key not bindable: {action}={name}"}
    if len(set(new["keys"].values())) != len(new["keys"]):
        return 422, {"error": "duplicate key binding"}
    if not isinstance(new["engines"]["api_timeout_s"], int) \
            or new["engines"]["api_timeout_s"] <= 0:
        return 422, {"error": "bad api_timeout_s"}
    if not isinstance(new["behavior"]["max_utterance_s"], int) \
            or new["behavior"]["max_utterance_s"] <= 0:
        return 422, {"error": "bad max_utterance_s"}
    restart = (new["keys"] != cfg["keys"] or new["engines"] != cfg["engines"]
               or new["behavior"]["grab_keys"] != cfg["behavior"]["grab_keys"])
    vt.CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    vt.CONFIG_PATH.write_text(vt.dump_config(new))
    return 200, {"ok": True, "restart_required": restart}


def api_words(payload):
    if "corrections" in payload:
        pairs = payload["corrections"]
        if not all(isinstance(p, list) and len(p) == 2
                   and all(isinstance(s, str) for s in p) and p[0].strip()
                   for p in pairs):
            return 422, {"error": "corrections must be [[wrong, right]...]"}
    if "vocab" in payload and not isinstance(payload["vocab"], str):
        return 422, {"error": "vocab must be a string"}

    if "vocab" in payload:
        vt.VOCAB_PATH.parent.mkdir(parents=True, exist_ok=True)
        vt.VOCAB_PATH.write_text(payload["vocab"])
    if "corrections" in payload:
        lines = [f"{w.strip()} => {r.strip()}" for w, r in payload["corrections"]]
        vt.CORRECTIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
        vt.CORRECTIONS_PATH.write_text("\n".join(lines) + ("\n" if lines else ""))
    for ts in payload.get("archive_flagged") or []:
        _archive_flag(ts)
    return 200, {"ok": True}


def _archive_flag(ts):
    try:
        lines = Path(vt.FLAGGED_PATH).read_text().splitlines(keepends=True)
    except OSError:
        return
    keep, archived = [], []
    for line in lines:
        m = vt.FLAG_LINE_RE.match(line.strip())
        (archived if m and m.group(1) == ts else keep).append(line)
    if archived:
        Path(vt.FLAGGED_PATH).write_text("".join(keep))
        archive = Path(vt.FLAGGED_PATH).with_name("flagged-archive.md")
        with open(archive, "a") as f:
            f.writelines(archived)


_ALLOWED_KEY_FIELDS = ("OPENAI_API_KEY", "GROQ_API_KEY")


def api_keys(payload):
    if any(k not in _ALLOWED_KEY_FIELDS for k in payload):
        return 422, {"error": "unknown field"}
    if any(not isinstance(v, str) for v in payload.values()):
        return 422, {"error": "values must be strings"}
    try:
        current = vt.load_secrets()
    except OSError:
        current = {}
    for field in _ALLOWED_KEY_FIELDS:
        v = payload.get(field, "")
        if v:
            current[field] = v.strip()
    path = Path(vt.SECRETS_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(mode=0o600, exist_ok=True)
    path.chmod(0o600)
    path.write_text("".join(f"{k}={v}\n" for k, v in current.items()))
    return 200, {"ok": True}


def api_service(payload):
    action = payload.get("action")
    if action not in ("start", "stop", "restart"):
        return 422, {"error": "action must be start|stop|restart"}
    r = subprocess.run(["systemctl", "--user", action, "voice-typed"],
                       capture_output=True)
    return 200, {"ok": r.returncode == 0,
                 "detail": r.stderr.decode(errors="replace").strip()}


def make_server(token, port=0):
    srv = ThreadingHTTPServer(("127.0.0.1", port), ConfigHandler)
    srv.token = token
    srv.last_request = time.monotonic()
    return srv


def run(open_browser=True, idle_timeout_s=900):
    vt.migrate_user_files()
    vt.resolve_user_paths()
    token = secrets.token_urlsafe(32)
    srv = make_server(token)
    port = srv.server_address[1]
    url = f"http://127.0.0.1:{port}/?token={token}"
    print(f"voice-typed config panel: {url}\n(Ctrl-C closes it)", flush=True)

    def reaper():
        while True:
            time.sleep(30)
            if time.monotonic() - srv.last_request > idle_timeout_s:
                srv.shutdown()
                return
    threading.Thread(target=reaper, daemon=True).start()
    if open_browser:
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0
