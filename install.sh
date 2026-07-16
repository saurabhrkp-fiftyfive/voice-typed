#!/usr/bin/env bash
# voice-typed installer — RUN AS USER (needs sudo for apt + group)
set -euo pipefail
cd "$(dirname "$0")"

NONINTERACTIVE=0 NOSERVICE=0 UNINSTALL=0 PURGE=0
for arg in "$@"; do
  case "$arg" in
    --non-interactive) NONINTERACTIVE=1 ;;
    --no-service)      NOSERVICE=1 ;;
    --uninstall)       UNINSTALL=1 ;;
    --purge)           PURGE=1 ;;
    *) echo "unknown flag: $arg"; exit 2 ;;
  esac
done

CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/voice-typed"
DATA_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/voice-typed"
BIN_LINK="$HOME/.local/bin/voice-typed"
DESKTOP="$HOME/.local/share/applications/voice-typed-config.desktop"

if [ "$UNINSTALL" = 1 ]; then
  systemctl --user disable --now voice-typed.service 2>/dev/null || true
  rm -f ~/.config/systemd/user/voice-typed.service "$BIN_LINK" "$DESKTOP"
  systemctl --user daemon-reload
  if [ "$PURGE" = 1 ]; then
    echo "⚠ purge removes your settings, vocabulary, corrections and flag log:"
    echo "    $CONFIG_DIR"
    echo "    $DATA_DIR"
    read -rp "type 'yes' to delete them: " ans
    [ "$ans" = "yes" ] && rm -rf "$CONFIG_DIR" "$DATA_DIR"
  fi
  echo "uninstalled."
  exit 0
fi

echo "[1/8] preflight"
if [ "${XDG_SESSION_TYPE:-}" = "wayland" ]; then
  echo "ERROR: Wayland session. voice-typed needs X11 (xdotool/x11grab)."
  echo "Log in with 'Ubuntu on Xorg' and re-run."
  exit 1
fi
if ! command -v apt-get >/dev/null; then
  echo "ERROR: non-apt distro. Install manually, then re-run:"
  echo "  xdotool xclip python3-evdev python3-requests python3-pil python3-pytest"
  echo "  pipewire (pw-record) libnotify ffmpeg"
  exit 1
fi

echo "[2/8] apt deps"
sudo apt-get install -y xdotool xclip python3-evdev python3-requests python3-pil \
  python3-pytest pipewire-bin libnotify-bin ffmpeg
for bin in pw-record xdotool notify-send xclip ffmpeg; do
  command -v "$bin" >/dev/null || { echo "MISSING: $bin"; exit 1; }
done

echo "[3/8] input group (evdev read access)"
NEED_RELOGIN=0
if ! id -nG "$USER" | grep -qw input; then
  sudo usermod -aG input "$USER"
  NEED_RELOGIN=1
fi

echo "[4/8] config scaffold + migrate user files"
mkdir -p "$CONFIG_DIR" "$DATA_DIR"
python3 -c "import voice_typed; print('migrated:', voice_typed.migrate_user_files() or 'nothing')"
[ -f "$CONFIG_DIR/config.toml" ] || \
  python3 -c "import voice_typed as v; open('$CONFIG_DIR/config.toml','w').write(v.dump_config(v.DEFAULT_CONFIG))"
[ -f "$CONFIG_DIR/vocab.txt" ]       || cp vocab.txt.template "$CONFIG_DIR/vocab.txt"
[ -f "$CONFIG_DIR/corrections.txt" ] || cp corrections.txt.template "$CONFIG_DIR/corrections.txt"

echo "[5/8] API keys"
has_key() {
  python3 - <<'EOF'
import sys, voice_typed
try:
    s = voice_typed.load_secrets()
except OSError:
    s = {}
sys.exit(0 if s.get("OPENAI_API_KEY") or s.get("GROQ_API_KEY") else 1)
EOF
}
if ! has_key; then
  if [ "$NONINTERACTIVE" = 1 ]; then
    echo "ERROR: no API key and --non-interactive. Put OPENAI_API_KEY or GROQ_API_KEY"
    echo "in $CONFIG_DIR/secrets.env first."
    exit 1
  fi
  echo "voice-typed needs at least one API key (OpenAI preferred, Groq fallback)."
  read -rsp "OPENAI_API_KEY (empty to skip): " OK; echo
  read -rsp "GROQ_API_KEY  (empty to skip): " GK; echo
  [ -z "$OK$GK" ] && { echo "ERROR: need at least one key."; exit 1; }
  umask 077
  { [ -n "$OK" ] && echo "OPENAI_API_KEY=$OK"
    [ -n "$GK" ] && echo "GROQ_API_KEY=$GK"; } > "$CONFIG_DIR/secrets.env"
  umask 022
fi

echo "[6/8] run tests"
python3 -m pytest test_voice_typed.py -q

echo "[7/8] systemd unit + CLI + desktop entry"
if [ "$NOSERVICE" = 0 ]; then
  mkdir -p ~/.config/systemd/user
  cp voice-typed.service ~/.config/systemd/user/
  systemctl --user daemon-reload
  dbus-update-activation-environment --systemd \
    DISPLAY XAUTHORITY DBUS_SESSION_BUS_ADDRESS 2>/dev/null || true
  systemctl --user import-environment DISPLAY XAUTHORITY DBUS_SESSION_BUS_ADDRESS 2>/dev/null || true
  systemctl --user enable --now voice-typed.service
fi
mkdir -p "$(dirname "$BIN_LINK")" "$(dirname "$DESKTOP")"
chmod +x voice_typed.py
ln -sf "$PWD/voice_typed.py" "$BIN_LINK"
cp voice-typed-config.desktop "$DESKTOP"

echo "[8/8] done"
echo "  keys:    hold F9 dictate · F8 enhance · F7 follow-up · F6 message · F10 flag"
echo "  config:  $CONFIG_DIR/config.toml   (or run: voice-typed config)"
echo "  check:   voice-typed doctor"
if [ "$NEED_RELOGIN" = 1 ]; then
  echo "⚠ added to 'input' group — LOG OUT AND BACK IN, then: voice-typed restart"
fi
