#!/usr/bin/env bash
#
# Install readerboard as a systemd service.
#
# Safe to run more than once. Nothing already correct is changed, an existing
# config file is left alone, and an API key that already exists is never
# regenerated, so re-running this to pick up a new version will not break a
# Home Assistant rest_command that is already working.
#
set -euo pipefail

SERVICE_USER=readerboard
INSTALL_DIR=/opt/readerboard
CONFIG_DIR=/etc/readerboard
CONFIG_FILE="$CONFIG_DIR/config.toml"
UNIT_NAME=readerboard.service
UNIT_FILE="/etc/systemd/system/$UNIT_NAME"

SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

usage() {
    cat <<'USAGE'
Usage: sudo scripts/install.sh [options]

Installs readerboard into /opt/readerboard, writes /etc/readerboard/config.toml
if it is not already there, and enables the readerboard systemd service.

Options:
  --serial-url URL   the sign's pyserial URL, written into a newly created config
                     file. Ignored if the config file already exists.
                     Examples: socket://192.168.2.51:4001, /dev/ttyUSB0
  --no-start         install and enable the service but do not start it
  --help             show this message

Run it again after pulling a new version. Your config file and API key survive.
USAGE
}

SERIAL_URL=""
START_SERVICE=1

while [ $# -gt 0 ]; do
    case "$1" in
        --serial-url) SERIAL_URL="${2:-}"; shift 2 ;;
        --serial-url=*) SERIAL_URL="${1#*=}"; shift ;;
        --no-start) START_SERVICE=0; shift ;;
        --help|-h) usage; exit 0 ;;
        *) echo "install.sh: unrecognised option '$1'" >&2; usage >&2; exit 2 ;;
    esac
done

if [ "$(id -u)" -ne 0 ]; then
    echo "install.sh: this needs root. Try: sudo scripts/install.sh" >&2
    exit 1
fi

if ! command -v systemctl >/dev/null 2>&1; then
    echo "install.sh: systemctl not found. This installer targets a systemd machine." >&2
    exit 1
fi

if ! command -v python3 >/dev/null 2>&1; then
    echo "install.sh: python3 not found. Install it and run this again." >&2
    exit 1
fi

say() { printf '  %s\n' "$*"; }

echo
echo "Installing readerboard from $SOURCE_DIR"
echo

# == the service user ========================================================

if id "$SERVICE_USER" >/dev/null 2>&1; then
    say "user $SERVICE_USER already exists"
else
    useradd --system --no-create-home --shell /usr/sbin/nologin "$SERVICE_USER"
    say "created system user $SERVICE_USER"
fi

# == the virtual environment =================================================

if [ ! -x "$INSTALL_DIR/venv/bin/python" ]; then
    mkdir -p "$INSTALL_DIR"
    python3 -m venv "$INSTALL_DIR/venv"
    say "created a virtual environment in $INSTALL_DIR/venv"
else
    say "reusing the virtual environment in $INSTALL_DIR/venv"
fi

"$INSTALL_DIR/venv/bin/python" -m pip install --quiet --upgrade pip

# The lock file pins what the sign runs on, so that an upstream release cannot
# change what is on the wall without somebody deciding it should.
if [ -f "$SOURCE_DIR/requirements.lock" ]; then
    "$INSTALL_DIR/venv/bin/python" -m pip install --quiet -r "$SOURCE_DIR/requirements.lock"
    say "installed pinned dependencies from requirements.lock"
fi

"$INSTALL_DIR/venv/bin/python" -m pip install --quiet --no-deps "$SOURCE_DIR"
say "installed the readerboard package"

chown -R root:root "$INSTALL_DIR"

# == the config file =========================================================

mkdir -p "$CONFIG_DIR"

if [ -f "$CONFIG_FILE" ]; then
    say "keeping the existing $CONFIG_FILE"
    GENERATED_KEY=""
else
    install -m 0640 -o root -g "$SERVICE_USER" \
        "$SOURCE_DIR/packaging/config.example.toml" "$CONFIG_FILE"

    GENERATED_KEY="$("$INSTALL_DIR/venv/bin/python" -c \
        'import secrets; print(secrets.token_urlsafe(32))')"
    # The key can contain / and & so use a delimiter that cannot appear in it.
    sed -i "s|^api_key = \"\"|api_key = \"$GENERATED_KEY\"|" "$CONFIG_FILE"

    if [ -n "$SERIAL_URL" ]; then
        sed -i "s|^serial_url = .*|serial_url = \"$SERIAL_URL\"|" "$CONFIG_FILE"
        say "set serial_url to $SERIAL_URL"
    fi

    say "wrote $CONFIG_FILE with a freshly generated API key"
fi

chmod 0640 "$CONFIG_FILE"
chown root:"$SERVICE_USER" "$CONFIG_FILE"

# == the service =============================================================

install -m 0644 "$SOURCE_DIR/packaging/$UNIT_NAME" "$UNIT_FILE"
systemctl daemon-reload
systemctl enable "$UNIT_NAME" >/dev/null 2>&1
say "installed and enabled $UNIT_NAME"

if [ "$START_SERVICE" -eq 1 ]; then
    systemctl restart "$UNIT_NAME"
    say "started $UNIT_NAME"
fi

# == what to do next =========================================================

PORT="$(sed -n 's/^port = \([0-9]*\).*/\1/p' "$CONFIG_FILE" | head -n 1)"
PORT="${PORT:-5001}"

echo
echo "Done."
echo

if [ -n "$GENERATED_KEY" ]; then
    echo "  Your API key is:"
    echo
    echo "      $GENERATED_KEY"
    echo
    echo "  It is in $CONFIG_FILE and will not be shown again. Every write needs it,"
    echo "  as an X-API-Key header."
    echo
fi

cat <<NEXT
  Next:

    1. Check it came up:
         systemctl status $UNIT_NAME
         journalctl -u $UNIT_NAME -n 50

    2. Check it found the sign:
         curl -s http://localhost:$PORT/health

    3. Give every client the X-API-Key header shown above. Writes without it
       are refused.

    4. Nothing else needs to set the sign's clock. This service does it at
       startup, hourly, and whenever the link to the sign comes back.

    5. The API and its documentation are at:
         http://localhost:$PORT/docs

NEXT
