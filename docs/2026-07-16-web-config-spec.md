# voice-typed web config — engineering spec

**Date:** 2026-07-16
**Status:** decided
**Parent:** `2026-07-16-productization-plan.md` (Option B decision lives there)

Precise contract for the config layer, CLI, installer v2, and web config panel.
Two implementation plans execute this spec:
`docs/plans/2026-07-16-01-foundation.md` and `docs/plans/2026-07-16-02-web-panel.md`.

---

## 1. File layout (after install)

```
~/.config/voice-typed/
  config.toml          # settings (optional — absent = all defaults)
  vocab.txt            # migrated from repo dir
  corrections.txt      # migrated from repo dir
  secrets.env          # 0600; fallback read: ~/.config/secrets.env (legacy)
~/.local/share/voice-typed/
  flagged.md           # F10 log (migrated from repo dir)
  flagged-archive.md   # promoted/dismissed flag entries
<repo>/
  voice_typed.py       # daemon + CLI entry
  config_server.py     # web panel HTTP server (separate process)
  panel.html           # static UI, served by config_server
```

`XDG_CONFIG_HOME` / `XDG_DATA_HOME` respected when set.

## 2. config.toml schema + precedence

```toml
[keys]
dictate  = "KEY_F9"
enhance  = "KEY_F8"
followup = "KEY_F7"
message  = "KEY_F6"
flag     = "KEY_F10"

[engines]
enhance_model = "gpt-4o-mini"
api_timeout_s = 30

[behavior]
paste_mode = false
grab_keys = true
max_utterance_s = 300
transliterate_devanagari = true
```

**Precedence: defaults < config.toml < environment variables.** Env vars are
the legacy interface and MUST keep working: `VOICE_TYPED_KEY`,
`VOICE_TYPED_ENHANCE_KEY`, `VOICE_TYPED_FOLLOWUP_KEY`, `VOICE_TYPED_MSG_KEY`,
`VOICE_TYPED_FLAG_KEY`, `VOICE_TYPED_ENHANCE_MODEL`, `VOICE_TYPED_PASTE` (=1 →
`paste_mode=true`), `VOICE_TYPED_GRAB` (=0 → `grab_keys=false`).

Unknown sections/keys in the file are ignored. Malformed TOML → log one line,
run on defaults (daemon must never fail to start over config).
Loader: stdlib `tomllib` (Python ≥3.11 floor). Writer: hand-rolled
`dump_config()` (stdlib has no TOML writer; only the known schema is emitted).

## 3. Migration (one-time, idempotent)

At daemon startup and at installer step 4: for each of `vocab.txt`,
`corrections.txt` (→ config dir) and `flagged.md` (→ data dir): if the XDG
target does not exist and the legacy repo-dir file does, **copy** (never move —
the repo copy becomes dead but harmless). XDG paths win whenever they exist.

## 4. CLI contract

`voice_typed.py` grows argparse subcommands; no args = run daemon (systemd
unit unchanged).

| Command | Behavior | Exit |
|---|---|---|
| `voice-typed` | run daemon | — |
| `voice-typed status` | `systemctl --user status voice-typed` passthrough | systemctl's |
| `voice-typed restart` / `stop` | systemctl passthrough | systemctl's |
| `voice-typed logs` | `journalctl --user -u voice-typed -n 50 --no-pager` | journalctl's |
| `voice-typed config` | start config server, print URL, open browser | 0 on clean shutdown |
| `voice-typed doctor` | run checks below, one ✅/❌ line each + fix hint | 0 all pass, 1 any fail |

`voice-typed` = symlink `~/.local/bin/voice-typed → <repo>/voice_typed.py`
(installer creates; script is executable, has shebang).

### doctor checks (offline; no API calls)

1. session is X11 (`XDG_SESSION_TYPE`) — Wayland → fail with explanation
2. user in `input` group (`id -nG`); if `usermod` already ran but session
   predates it, say "re-login pending"
3. binaries on PATH: `pw-record xdotool xclip notify-send ffmpeg`
4. secrets readable + at least one of `OPENAI_API_KEY`/`GROQ_API_KEY`
5. `config.toml` parses; all 5 key names exist in `evdev.ecodes`
6. `/dev/input` has ≥1 readable keyboard with the dictate key
7. systemd unit installed + active

## 5. Web panel

### Lifecycle

```
voice-typed config
  → migrate_user_files()
  → bind 127.0.0.1, port 0 (OS-assigned random port)
  → token = secrets.token_urlsafe(32), per session
  → print "config panel: http://127.0.0.1:<port>/?token=<token>"
  → webbrowser.open(that URL)
  → serve until: Ctrl-C | POST /quit | 15 min without any request
```

Separate short-lived process — NOT inside the daemon. It edits the same files;
vocab/corrections changes are picked up by the daemon's per-utterance
hot-reload; key/engine changes require restart (panel offers the button).

### Authentication (all mandatory)

- Bind loopback only. Never `0.0.0.0`.
- `GET /` requires `?token=` query param; every `/api/*` and `/quit` request
  requires `X-Config-Token` header. Compare with `hmac.compare_digest`.
- Wrong/missing token → `403` JSON `{"error": "bad token"}`. No content leaks.
- `Host` header must be `127.0.0.1[:port]` or `localhost[:port]` → else 403
  (DNS-rebinding guard). `Origin` header, when present, must be a loopback
  origin → else 403 (CSRF guard).
- API keys: accepted via POST, written 0600, **never** included in any
  response or log line.

### HTTP API (JSON everywhere; errors `{"error": "<msg>"}` + 4xx/5xx)

```
GET  /                → panel.html (token-checked)
GET  /api/state       → {config, vocab: str, corrections: [[wrong, right]...],
                         flagged: [{ts, text, note}...],
                         service: {active: bool, log: str},
                         bindable: [<evdev name>...], budget: {used, max},
                         keys_set: {OPENAI_API_KEY: bool, GROQ_API_KEY: bool}}
POST /api/config      ← {keys?, engines?, behavior?}   # partial update, validated
                      → {ok: true, restart_required: bool}
POST /api/words       ← {vocab?: str, corrections?: [[wrong, right]...],
                         archive_flagged?: [<ts>...]}
                      → {ok: true}
POST /api/keys        ← {OPENAI_API_KEY?: str, GROQ_API_KEY?: str}  # empty str = leave
                      → {ok: true}
POST /api/service     ← {action: "start"|"stop"|"restart"}
                      → {ok: bool, detail: str}
POST /quit            → {ok: true}, then server shuts down
```

Validation on `/api/config`: every key name must exist in `evdev.ecodes` AND
be in the bindable set; the 5 actions must be pairwise distinct; numeric
fields must be positive ints. Reject whole request on any violation (422).

### Key rebinding

Browser capture via `KeyboardEvent.code`, mapped client-side:
`F1..F10` → `KEY_F1..KEY_F10`, `KeyA..KeyZ` → `KEY_A..KEY_Z`,
`Digit0..Digit9` → `KEY_0..KEY_9`. That is the whole bindable set v1 —
F11/F12 excluded (browser-owned: fullscreen/devtools). Server re-validates;
never trust the client. Conflict check across the 5 actions in UI and server.

### Tabs

1. **Shortcuts** — 5 action rows, current keycap, Rebind (press-a-key state,
   `preventDefault` while capturing), conflict warning, Save → POST
   `/api/config` → "Restart daemon" button on `restart_required`.
2. **Corrections** — flagged inbox (from `flagged.md`; per entry: text, editable
   wrong→right fields, Promote → corrections + archive, Add-to-vocab, Dismiss →
   archive) + active corrections table (add/edit/delete rows) + live preview
   input applying current table client-side.
3. **Vocabulary** — textarea of `vocab.txt`; live budget bar `used/800` chars
   (same formula as `load_vocab`: `len("Vocabulary: " + ", ".join(words))`).
4. **Engines** — password-type inputs for the two API keys (the panel only
   learns *whether* a key exists, via `/api/state.keys_set` — never the
   value), enhance-model text field, toggles
   (paste_mode, grab_keys, transliterate_devanagari), timeout number input.
5. **Service** — active/inactive badge, Start/Stop/Restart, last journal lines
   (from `/api/state.service.log`), "hold F9 and speak to test" hint.

## 6. Installer v2 (`install.sh`)

Steps: preflight (X11 or die; apt or print manual deps + die) → apt deps
(current set, unchanged — panel adds none) → input group → **config scaffold +
migration** (§3) → **API key prompt** (`read -rs`; skipped when a key already
readable; at least one required unless `--non-interactive`) → pytest → systemd
unit → enable+start → `~/.local/bin/voice-typed` symlink + desktop entry for
`voice-typed config` → summary (bindings, paths, "run voice-typed doctor").

Flags: `--non-interactive` (no prompts; fail if no key), `--no-service`,
`--uninstall` (unit, symlink, desktop entry; keeps `~/.config/voice-typed`),
`--uninstall --purge` (also config+data dirs, after explicit `yes` prompt).

## 7. OSS hygiene

MIT `LICENSE`; `vocab.txt`/`corrections.txt` gitignored + `.template` versions
committed; GitHub Actions: `pytest` + `shellcheck install.sh`; README: web
panel section + quickstart; issue template asks for `voice-typed doctor`
output.

## 8. Testing strategy

- Existing 60 tests keep passing untouched (they monkeypatch module constants —
  constants stay).
- Config: defaults / partial TOML / malformed TOML / env-over-TOML precedence /
  `dump_config` round-trip.
- Migration: copies when target missing, skips when present, tolerates missing
  legacy files.
- Doctor: each check unit-tested with mocked `subprocess`/env.
- Server: real `ThreadingHTTPServer` on port 0 + `http.client` in tests — no
  browser. Token 403s, Host/Origin 403s, every endpoint round-trips its file,
  keys never echoed, validation 422s.
- `panel.html`: not unit-tested (thin static layer); manual matrix Firefox +
  Chrome.

## 9. Out of scope (v1)

Wayland, non-apt distros (manual dep list only), cross-platform daemon,
`doctor --ping` live API checks, HTTPS on loopback, multi-user.
