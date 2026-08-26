#!/usr/bin/env bash
#
# Remove readerboard.
#
# By default this stops the service and removes the program, but leaves your
# config file and the registry behind, so that reinstalling puts the sign back
# exactly as it was. Pass --purge to remove those too.
#
set -euo pipefail

SERVICE_USER=readerboard
INSTALL_DIR=/opt/readerboard
CONFIG_DIR=/etc/readerboard
STATE_DIR=/var/lib/readerboard
UNIT_NAME=readerboard.service
UNIT_FILE="/etc/systemd/system/$UNIT_NAME"

usage() {
    cat <<'USAGE'
Usage: sudo scripts/uninstall.sh [options]

Stops and removes the readerboard service and the program in /opt/readerboard.

Options:
  --purge        also remove /etc/readerboard (your config file and API key),
                 /var/lib/readerboard (the registered messages), and the
                 readerboard system user. This cannot be undone.
  --help         show this message

Without --purge, reinstalling later restores the sign to exactly what it was
showing, because the registry and the config file are still there.
USAGE
}

PURGE=0

while [ $# -gt 0 ]; do
    case "$1" in
        --purge) PURGE=1; shift ;;
        --help|-h) usage; exit 0 ;;
        *) echo "uninstall.sh: unrecognised option '$1'" >&2; usage >&2; exit 2 ;;
    esac
done

if [ "$(id -u)" -ne 0 ]; then
    echo "uninstall.sh: this needs root. Try: sudo scripts/uninstall.sh" >&2
    exit 1
fi

say() { printf '  %s\n' "$*"; }

echo
echo "Removing readerboard"
echo

if systemctl list-unit-files "$UNIT_NAME" >/dev/null 2>&1 && \
   systemctl cat "$UNIT_NAME" >/dev/null 2>&1; then
    systemctl stop "$UNIT_NAME" 2>/dev/null || true
    systemctl disable "$UNIT_NAME" >/dev/null 2>&1 || true
    say "stopped and disabled $UNIT_NAME"
fi

if [ -f "$UNIT_FILE" ]; then
    rm -f "$UNIT_FILE"
    systemctl daemon-reload
    say "removed $UNIT_FILE"
fi

if [ -d "$INSTALL_DIR" ]; then
    rm -rf "$INSTALL_DIR"
    say "removed $INSTALL_DIR"
fi

if [ "$PURGE" -eq 1 ]; then
    for directory in "$CONFIG_DIR" "$STATE_DIR"; do
        if [ -d "$directory" ]; then
            rm -rf "$directory"
            say "removed $directory"
        fi
    done

    if id "$SERVICE_USER" >/dev/null 2>&1; then
        userdel "$SERVICE_USER" 2>/dev/null || true
        say "removed the $SERVICE_USER user"
    fi

    echo
    echo "Done. Nothing is left behind."
    echo
    echo "  The sign itself still holds whatever it was last told to show. To blank"
    echo "  it, press the ADV key on its infrared keyboard, or reinstall and use"
    echo "  DELETE /v2/messages."
    echo
else
    echo
    echo "Done."
    echo
    say "Kept $CONFIG_DIR, including your API key."
    say "Kept $STATE_DIR, so reinstalling restores what the sign was showing."
    say "Pass --purge to remove both."
    echo
fi
