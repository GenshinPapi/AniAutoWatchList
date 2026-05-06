#!/bin/sh
set -eu

ROOT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$ROOT_DIR"

PYTHON=${PYTHON:-}
if [ -z "$PYTHON" ]; then
    if [ -x .venv/bin/python ]; then
        PYTHON=.venv/bin/python
    else
        PYTHON=python3
    fi
fi

printf '%s\n' "== Python tests =="
"$PYTHON" -m pytest

printf '%s\n' "== Python compile check =="
"$PYTHON" -m compileall -q src/ani_watchlist

printf '%s\n' "== Shell syntax =="
sh -n scripts/install-user.sh
sh -n scripts/uninstall-user.sh
sh -n ani-cli/ani-cli
bash -n ani-cli/ani-cli

printf '%s\n' "== GUI import check =="
if [ -x .venv/bin/ani-watch-gui ]; then
    .venv/bin/ani-watch-gui --check
else
    PYTHONPATH="$ROOT_DIR/src${PYTHONPATH:+:$PYTHONPATH}" "$PYTHON" -m ani_watchlist.gui --check
fi

printf '%s\n' "== Optional shellcheck =="
if command -v shellcheck >/dev/null 2>&1; then
    shellcheck scripts/install-user.sh scripts/uninstall-user.sh ani-cli/ani-cli
else
    printf '%s\n' "shellcheck not installed; skipping"
fi

if grep -R "REPLACE-ME" pyproject.toml README.md NOTICE.md >/dev/null 2>&1; then
    printf '%s\n' "WARN: replace REPLACE-ME GitHub URLs before publishing."
fi

if [ -d ani-cli/.git ]; then
    printf '%s\n' "NOTE: ani-cli/.git exists locally. It is ignored by .gitignore and should not be committed."
fi

printf '%s\n' "Package check complete."
