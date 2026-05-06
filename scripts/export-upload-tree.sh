#!/bin/sh
set -eu

ROOT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
DEST=${1:-}

if [ -z "$DEST" ]; then
    printf '%s\n' "Usage: scripts/export-upload-tree.sh DEST_DIR" >&2
    exit 2
fi

mkdir -p "$DEST"
if [ -n "$(find "$DEST" -mindepth 1 -maxdepth 1 -print -quit)" ]; then
    printf '%s\n' "ERROR: destination is not empty: $DEST" >&2
    exit 1
fi

cd "$ROOT_DIR"
tar \
    --exclude='./.git' \
    --exclude='./ani-cli/.git' \
    --exclude='./ani-cli/.github' \
    --exclude='./ani-cli/.assets' \
    --exclude='./.venv' \
    --exclude='./.pytest_cache' \
    --exclude='./src/*.egg-info' \
    --exclude='*/__pycache__' \
    --exclude='*.pyc' \
    --exclude='*.sqlite' \
    --exclude='*.sqlite3' \
    --exclude='*.db' \
    -cf - . | tar -C "$DEST" -xf -

printf '%s\n' "Clean upload tree written to: $DEST"
printf '%s\n' "Next:"
printf '%s\n' "  cd $DEST"
printf '%s\n' "  git init"
printf '%s\n' "  git add ."
printf '%s\n' "  git status"
