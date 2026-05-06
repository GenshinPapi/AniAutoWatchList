#!/bin/sh
set -eu

VENV_DIR=${ANI_WATCHLIST_VENV:-"$HOME/.local/share/ani-watchlist/venv"}
BIN_DIR="$HOME/.local/bin"
PURGE_DATA=0

if [ "${1:-}" = "--purge-data" ]; then
    PURGE_DATA=1
fi

rm -f \
    "$BIN_DIR/ani-cli" \
    "$BIN_DIR/ani-watch" \
    "$BIN_DIR/ani-watch-gui" \
    "$BIN_DIR/ani-watch-hook" \
    "$BIN_DIR/ani-watch-sync"

rm -rf "$VENV_DIR"

if [ "$PURGE_DATA" = "1" ]; then
    rm -rf \
        "$HOME/.local/share/ani-watchlist" \
        "$HOME/.config/ani-watchlist" \
        "$HOME/.cache/ani-watchlist" \
        "$HOME/.local/state/ani-watchlist"
else
    printf '%s\n' "Watchlist data was preserved."
    printf '%s\n' "Remove it with: scripts/uninstall-user.sh --purge-data"
fi

printf '%s\n' "Uninstalled ani-watchlist command shims. Run: hash -r && command -v ani-cli"
