#!/usr/bin/env bash
# voice-typed installer — RUN AS USER (needs sudo for apt + group)
set -euo pipefail
cd "$(dirname "$0")"

echo "[1/6] apt deps"
sudo apt-get install -y xdotool xclip python3-evdev python3-requests python3-pil \
  python3-pytest pipewire-bin libnotify-bin ffmpeg

echo "[2/6] preflight runtime binaries"
for bin in pw-record xdotool notify-send xclip ffmpeg; do
  command -v "$bin" >/dev/null || { echo "MISSING: $bin"; exit 1; }
done

echo "[3/6] input group (evdev read access)"
NEED_RELOGIN=0
if ! id -nG "$USER" | grep -qw input; then
  sudo usermod -aG input "$USER"
  NEED_RELOGIN=1
fi

echo "[4/6] run tests"
python3 -m pytest test_voice_typed.py -q

echo "[5/6] install systemd user unit + session env"
mkdir -p ~/.config/systemd/user
cp voice-typed.service ~/.config/systemd/user/
systemctl --user daemon-reload
# GNOME/Ubuntu imports these on login; belt+braces for this session:
dbus-update-activation-environment --systemd \
  DISPLAY XAUTHORITY DBUS_SESSION_BUS_ADDRESS 2>/dev/null || true
systemctl --user import-environment DISPLAY XAUTHORITY DBUS_SESSION_BUS_ADDRESS 2>/dev/null || true

echo "[6/6] enable + start"
systemctl --user enable --now voice-typed.service
systemctl --user --no-pager --lines=5 status voice-typed.service || true

if [ "$NEED_RELOGIN" = "1" ]; then
  echo "⚠ added to 'input' group — LOG OUT AND BACK IN, then: systemctl --user restart voice-typed"
fi
