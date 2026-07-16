# voice-typed productization plan — installer, config interface, user panel

**Date:** 2026-07-16
**Status:** decided — web config panel (Option B)

> **Decision 2026-07-16:** the settings UI is a **self-served web page**
> (`voice-typed config` → loopback HTTP server → browser), not a GTK app.
> Rationale: zero new dependencies (stdlib `http.server` + one static HTML
> file), preserves the single-daemon ethos, usable over SSH port-forward,
> contributors edit HTML/JS instead of PyGObject, and the HTTP layer fits the
> existing mocked-request test style. Note: this makes the *config UI*
> browser-portable, but the daemon itself remains Linux/X11-only (evdev,
> xdotool, pw-record).

Goal: turn the single-script daemon into an installable, configurable open-source
app. Three deliverables: (1) installer v2, (2) config layer + CLI, (3) web config
panel (corrections/mispronunciation editor + shortcut binding).

Non-goals: Wayland support (xdotool + x11grab are X11-only — detect and fail
with a clear message), packaging beyond `install.sh` (no .deb/flatpak in v1),
local/offline STT.

---

## 1. Config layer (foundation — everything else sits on it)

Today all tuning is env vars baked into the systemd unit, and user data
(`vocab.txt`, `corrections.txt`, `flagged.md`) lives *inside the git checkout* —
a fork/pull nightmare for outside users.

### 1.1 XDG split

| What | New location |
|---|---|
| config | `~/.config/voice-typed/config.toml` |
| user words | `~/.config/voice-typed/vocab.txt`, `corrections.txt` |
| API keys | `~/.config/voice-typed/secrets.env` (0600; fallback: existing `~/.config/secrets.env`) |
| flag log | `~/.local/share/voice-typed/flagged.md` |
| runtime | `$XDG_RUNTIME_DIR/voice-typed/` (unchanged) |

Repo keeps only `*.template` versions; personal `vocab.txt`/`corrections.txt`
leave git history-forward (gitignore + installer migrates existing files on
first run — copy, don't delete).

### 1.2 config.toml schema

```toml
[keys]                    # evdev names
dictate   = "KEY_F9"
enhance   = "KEY_F8"
followup  = "KEY_F7"
message   = "KEY_F6"
flag      = "KEY_F10"

[engines]
stt_primary    = "openai:gpt-4o-transcribe"
stt_fallback   = "groq:whisper-large-v3"
enhance_model  = "gpt-4o-mini"
groq_enhance_model = "llama-3.3-70b-versatile"
api_timeout_s  = 30

[behavior]
paste_mode      = false     # was VOICE_TYPED_PASTE
grab_keys       = true      # was VOICE_TYPED_GRAB
max_utterance_s = 300
transliterate_devanagari = true

[vision]
screenshot_modes = ["followup", "message"]
max_px = 1568
```

- Loader: stdlib `tomllib` (py ≥3.11, Ubuntu 22.04+ OK). Missing file → all
  defaults (current behavior). Env vars keep working as **overrides** so
  existing installs don't break.
- Hot-reload: everything except `[keys]` and `grab_keys` reloads per-utterance
  (same pattern as vocab today). Key changes need daemon restart — panel and
  CLI do it automatically on save.

### 1.3 `voice-typed` CLI

Thin argparse front (`voice_typed.py` grows subcommands; symlink into
`~/.local/bin/voice-typed`):

```
voice-typed              # run daemon (what systemd calls)
voice-typed status       # service state + last log lines
voice-typed restart|stop|logs
voice-typed config       # start config server + open browser panel
voice-typed doctor       # full diagnostics (below)
```

`doctor` checks: X11 vs Wayland, `input` group membership (and whether re-login
is pending), all runtime binaries, API keys present + a 1-token live ping per
engine, mic device visible to pipewire, systemd unit state. Each line ✅/❌ with
the fix command. This becomes the standard first line of every bug report.

---

## 2. Installer v2

`install.sh` rewritten, idempotent, interactive-by-default:

1. **Preflight**: refuse Wayland with explanation; detect distro (apt only in
   v1, print manual dep list otherwise).
2. **Deps**: current apt set unchanged — the web panel adds **no** new
   dependencies (stdlib `http.server` + `webbrowser`).
3. **input group**: unchanged, but re-login warning moves to the *end* summary
   and `doctor` re-checks it.
4. **Config scaffold**: create XDG dirs, write default `config.toml`, migrate
   any repo-dir `vocab.txt`/`corrections.txt`/`flagged.md`.
5. **API keys**: if none found, prompt (hidden input) for `OPENAI_API_KEY`
   and/or `GROQ_API_KEY` → write `secrets.env` 0600. At least one required;
   explain the primary/fallback roles.
6. **Tests** → **systemd unit** → **enable+start** (unchanged).
7. **Desktop integration**: `voice-typed` CLI symlink + optional
   `voice-typed-config.desktop` entry that runs `voice-typed config` (panel
   reachable from app grid too).
8. **Summary card**: key bindings, config path, "run `voice-typed doctor`".

Flags: `--non-interactive` (CI / dotfile managers; requires keys already
present), `--no-service`, `--uninstall` (removes unit, symlink, desktop entry;
**keeps** `~/.config/voice-typed` unless `--purge`).

---

## 3. Web config panel (`voice-typed config`)

**Stack: stdlib only.** `http.server.ThreadingHTTPServer` embedded behind the
`config` subcommand + one static `panel.html` (vanilla HTML/CSS/JS, no
framework, shipped in the repo). No pip, no apt, no GTK.

### Lifecycle

```
voice-typed config
  → bind 127.0.0.1:<random port>
  → generate per-session token (secrets.token_urlsafe)
  → webbrowser.open("http://127.0.0.1:<port>/?token=…")
  → serve until Ctrl-C / browser sends /quit / 15 min idle
```

Server is **not** part of the daemon process — separate short-lived process
that edits the same config files; daemon picks changes up via hot-reload or
gets restarted by the panel (`systemctl --user restart voice-typed`).

### HTTP API (JSON, token-checked on every request)

```
GET  /api/state        # config + vocab + corrections + flagged + service status
POST /api/config       # write config.toml   → restart offer
POST /api/words        # write vocab.txt / corrections.txt / archive flagged
POST /api/keys         # write secrets.env (0600), values never echoed back
POST /api/service      # start|stop|restart via systemctl --user
POST /quit
```

### Tabs (same five as before, now HTML)

1. **Shortcuts** — one row per action (dictate / enhance / follow-up / message /
   flag): current key as a keycap, `[Rebind]` → "press a key…" capture via
   `KeyboardEvent.code`, mapped to evdev name (`"F9"`→`KEY_F9`). Bindable set
   restricted to F1–F10 + printable keys (browser owns F11 fullscreen and F12
   devtools). Conflict check across the 5 actions. Save → `[keys]` → restart.
2. **Corrections** (the mispronunciation panel) — two lists:
   - *Flagged inbox*: entries parsed from `flagged.md`. Each shows the bad
     transcript; user edits wrong→right inline, hits **Promote** → appends to
     `corrections.txt` (or **Add to vocab** for a name STT should learn) →
     entry moved to `flagged-archive.md`.
   - *Active corrections*: editable `wrong => right` table (add/edit/delete),
     round-trips comments in `corrections.txt`. Live preview field: type a
     sentence, see corrections applied.
3. **Vocabulary** — line editor for `vocab.txt` with a char-budget bar
   (800-char STT prompt cap) that turns red when entries would be truncated.
4. **Engines** — API key fields (masked, saved 0600), enhance-model dropdown,
   paste-mode / transliteration / screenshot-mode toggles, timeout input.
5. **Service** — running/stopped badge, start/stop/restart buttons, tail of
   last 20 journal lines, "test dictation" hint.

Corrections/vocab edits are picked up by the daemon's existing per-utterance
hot-reload — no restart. Keys/engines prompt restart.

### Security (non-negotiable)

Bind `127.0.0.1` only; random port; per-session URL token required on every
request (constant-time compare); reject non-loopback `Host`/`Origin` headers
(DNS-rebinding guard); server runs only while the panel is in use; API keys are
write-only through the API and never logged. Without the token any local
process — or a malicious web page via CSRF — could rewrite the config; with it
the surface matches a native app.

### Scope honesty

The panel runs in any browser, but it configures a daemon that is
**Linux/X11-only** (evdev key grab, xdotool typing, pw-record audio, x11grab
screenshots). Option B does not make voice-typed cross-platform — it makes the
config surface portable and dependency-free. Cross-platform support would be a
separate (large) project per subsystem.

---

## 4. Open-source hygiene

- `LICENSE` (MIT), `CONTRIBUTING.md`, issue template that asks for
  `voice-typed doctor` output.
- README rewrite: 30-sec demo GIF, quickstart (`git clone && ./install.sh`),
  config reference generated from the TOML defaults.
- GitHub Actions: pytest on push (already pure-mocked, no display needed) +
  shellcheck on `install.sh`.
- Strip personal data: replace committed `vocab.txt`/`corrections.txt` with
  templates; add both to `.gitignore`.

---

## 5. Testing

- Unit: config loader (defaults / partial file / env override precedence),
  `KeyboardEvent.code`→evdev mapping table, corrections round-trip
  (parse→edit→serialize preserves comments), flagged.md inbox parser, budget
  calculator. Extends existing 60-test suite; same mocked style.
- HTTP API tested headless with `http.client` against a server on a random
  port: token required, bad token → 403, non-loopback Origin → 403, each
  endpoint round-trips its file. The static HTML stays untested (thin layer).
- Manual matrix: fresh Ubuntu VM install, upgrade-in-place from current
  layout, `--uninstall`, Wayland refusal, panel in Firefox + Chrome.

---

## 6. Milestones

| # | Scope | Est |
|---|---|---|
| M1 | config.toml + XDG migration + env-override back-compat + tests | 0.5 d |
| M2 | installer v2 + CLI subcommands + `doctor` + uninstall | 0.5 d |
| M3 | config server (token auth, API) + Shortcuts + Service tabs | 0.5 d |
| M4 | panel: Corrections inbox/editor + Vocabulary + Engines tabs | 1 d |
| M5 | OSS polish: LICENSE, CI, README/GIF, personal-data strip | 0.5 d |

Order matters: M1 unblocks everything; M3/M4 depend on M1+M2; M5 last.
Total ≈ 3 focused days.

## Risks

- **Browser-owned keys**: F11 (fullscreen) and F12 (devtools) can't be reliably
  captured for rebinding — bindable set restricted to F1–F10 + printable keys,
  with an explanatory tooltip.
- **Grabbed keys** (enhance modes use X11 grabs) — the browser receives grabbed
  keys only if the daemon isn't holding them, so the Rebind flow pauses/resumes
  the service around the "press a key…" state.
- **Local HTTP surface**: mitigations in §3 Security — loopback bind, session
  token, Origin/Host check, short-lived server. Skipping any one of these is a
  real vulnerability, not a nice-to-have.
- **Secrets in panel**: keys written 0600, write-only API, never logged;
  masked in UI.
