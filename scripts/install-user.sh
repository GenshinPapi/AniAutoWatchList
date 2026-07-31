#!/bin/sh
set -eu

ROOT_DIR=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
VENV_DIR=${ANI_WATCHLIST_VENV:-"$HOME/.local/share/ani-watchlist/venv"}
BIN_DIR="$HOME/.local/bin"
ANI_CLI_SCRIPT="$ROOT_DIR/ani-cli/ani-cli"

info() {
    printf '%s\n' "$*"
}

warn() {
    printf '%s\n' "WARN: $*" >&2
}

die() {
    printf '%s\n' "ERROR: $*" >&2
    exit 1
}

command -v python3 >/dev/null 2>&1 || {
    die "python3 is required. On Linux Mint/Ubuntu install: sudo apt install python3 python3-venv python3-tk"
}

[ -f "$ANI_CLI_SCRIPT" ] || die "patched ani-cli script not found: $ANI_CLI_SCRIPT"

if ! python3 -c 'import tkinter' >/dev/null 2>&1; then
    warn "python3-tk is not importable; the CLI can still install, but the GUI will not launch."
    warn "On Linux Mint/Ubuntu install it with: sudo apt install python3-tk"
fi

if ! python3 -m venv "$VENV_DIR"; then
    die "failed to create virtualenv. On Linux Mint/Ubuntu install: sudo apt install python3-venv"
fi

"$VENV_DIR/bin/python" -m pip install --upgrade pip
"$VENV_DIR/bin/python" -m pip install -e "$ROOT_DIR"

mkdir -p "$BIN_DIR"
ln -sfn "$VENV_DIR/bin/ani-watch" "$BIN_DIR/ani-watch"
ln -sfn "$VENV_DIR/bin/ani-watch-gui" "$BIN_DIR/ani-watch-gui"
ln -sfn "$VENV_DIR/bin/ani-watch-hook" "$BIN_DIR/ani-watch-hook"
ln -sfn "$VENV_DIR/bin/ani-watch-sync" "$BIN_DIR/ani-watch-sync"
chmod +x "$ANI_CLI_SCRIPT"
ln -sfn "$ANI_CLI_SCRIPT" "$BIN_DIR/ani-cli"

case ":$PATH:" in
    *":$BIN_DIR:"*) ;;
    *)
        warn "$BIN_DIR is not on PATH."
        info "Add this to your shell profile: export PATH=\"\$HOME/.local/bin:\$PATH\""
        ;;
esac

info "Installed ani-watchlist commands into $BIN_DIR"
info "Patched ani-cli symlink: $BIN_DIR/ani-cli -> $ANI_CLI_SCRIPT"
info "With $BIN_DIR first on PATH, ani-cli resolves to: $(PATH="$BIN_DIR:$PATH" command -v ani-cli 2>/dev/null || printf 'not found')"
info "Run: hash -r && ani-watch doctor"
