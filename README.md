# ani-watchlist

`ani-watchlist` is a local watchlist and playback tracker for an existing `ani-cli` install. It adds a Python app, SQLite database, desktop GUI, and small hook calls in a development copy of `ani-cli`.

It does not add new streaming scrapers, does not bypass DRM/paywalls/login systems, and does not use EverythingMoe or any other link list for automated playback. Existing `ani-cli` playback behavior remains the playback path.

## Quick Install From Git

Prerequisites on Linux Mint/Ubuntu:

```sh
sudo apt install git python3 python3-venv python3-tk curl fzf mpv openssl
```

Clone and install:

```sh
git clone https://github.com/GenshinPapi/AniAutoWatchList.git
cd AniAutoWatchList
scripts/install-user.sh
export PATH="$HOME/.local/bin:$PATH"
hash -r
ani-watch doctor
ani-watch-gui
```

The installer creates a user-owned virtualenv at `~/.local/share/ani-watchlist/venv`, installs the Python package there, and symlinks the app commands plus the patched `ani-cli` into `~/.local/bin`.

It does not overwrite `/usr/bin/ani-cli`, `/usr/local/bin/ani-cli`, or any other system install.

## What It Does

- Records `ani-cli` launches, title selections, listed episodes, playback starts, playback finishes, and playback failures.
- Adds titles to a local SQLite watchlist.
- Stores episode availability when `ani-cli` exposes an episode list.
- Marks episodes started immediately.
- Marks episodes watched only after a successful player exit and the configured duration threshold, default `120` seconds.
- Searches AniList for metadata and cover art when a new title is tracked.
- Keeps tracking functional when AniList or the network is unavailable.
- Provides `ani-watch`, `ani-watch-hook`, `ani-watch-sync`, and `ani-watch-gui` commands.
- Adds local dashboard, next-episode helpers, duplicate detection, JSON/CSV import/export, activity logs, repair checks, and an optional desktop launcher.

## What It Does Not Do

- No new third-party streaming source resolver is implemented.
- No EverythingMoe scraping or automated streaming integration is implemented.
- No cloud account is required.
- No package-managed or system `ani-cli` file is overwritten.

## Paths

- Database: `~/.local/share/ani-watchlist/watchlist.sqlite3`
- Config: `~/.config/ani-watchlist/config.toml`
- Cover cache: `~/.cache/ani-watchlist/covers/`
- Logs: `~/.local/state/ani-watchlist/logs/`

## Development Install

```sh
python3 -m venv .venv
. .venv/bin/activate
pip install -e .
```

For test development, install the test extra:

```sh
pip install -e '.[test]'
```

Run tests with:

```sh
make test
```

Or directly from the project virtualenv:

```sh
.venv/bin/python -m pytest -q
```

Run shellcheck for the patched `ani-cli` if installed:

```sh
make shellcheck
```

Run the full pre-upload check:

```sh
make package-check
```

## User Install

The user installer creates a user-owned virtualenv and symlinks the modified bundled script into `~/.local/bin/ani-cli`.

```sh
scripts/install-user.sh
```

Ensure this appears before `/usr/local/bin` and `/usr/bin` in your shell:

```sh
export PATH="$HOME/.local/bin:$PATH"
```

Then verify:

```sh
command -v ani-cli
ani-watch doctor
ani-watch-gui --check
```

Expected command resolution after install:

```text
~/.local/bin/ani-cli
```

If `python3-tk` is missing, the command-line tracker can still install, but the GUI will not launch until that system package is installed.

## ani-cli Integration

The bundled patched `ani-cli/ani-cli` script is derived from upstream `ani-cli` and calls:

- `ani-watch-hook launch --argv-json ...`
- `ani-watch-hook title-selected --title ... --source-title ...`
- `ani-watch-hook episodes-listed --title ... --episodes-json ...`
- `ani-watch-hook playback-started --title ... --episode ...`
- `ani-watch-hook playback-finished --title ... --episode ... --exit-code ... --duration-seconds ...`

Detached playback is preserved. The patched script starts a background wrapper that waits for the player process and then records the finished/failed event.

Search still uses ani-cli's existing AllAnime API path. The local patch requests the existing `englishName` metadata field, displays English-first results when available, and falls back to the original title when English metadata is missing. No new streaming resolver or fallback source is added.

Disable hook calls temporarily with:

```sh
ANI_WATCH_DISABLE=1 ani-cli
```

## CLI

```sh
ani-watch list
ani-watch list --status watching
ani-watch show "Title"
ani-watch dashboard
ani-watch next "Title"
ani-watch continue
ani-watch mark "Title" 12 --watched
ani-watch mark "Title" 12 --unwatched
ani-watch status "Title" completed
ani-watch events "Title"
ani-watch events --recent 20
ani-watch gui
ani-watch doctor
ani-watch export --format json
ani-watch export --format csv --output watchlist.csv
ani-watch import watchlist.json
ani-watch import-history --search "Wistoria"
ani-watch backup
ani-watch restore PATH
ani-watch duplicates
ani-watch merge "Title A" "Title B" --yes
ani-watch config get tracking.mark_watched_after_seconds
ani-watch config set tracking.mark_watched_after_seconds 180
ani-watch repair
ani-watch repair --yes
ani-watch logs --tail
ani-watch install-desktop-entry
```

Extra local-management commands are also available:

```sh
ani-watch add "Title" --episodes "1,2,3"
ani-watch delete "Title"
ani-watch refresh-metadata "Title"
ani-watch metadata search "Title"
ani-watch metadata set "Title" --anilist-id 12345
ani-watch metadata refresh "Title"
ani-watch-sync
```

Normal output uses simple aligned text tables and does not require rich terminal libraries.

## GUI

Launch with:

```sh
ani-watch-gui
```

or:

```sh
ani-watch gui
```

The GUI supports status grouping, search/filtering, cover display when cached, manual add/edit/delete, notes, episode watched/unwatched edits, status moves, reordering inside a status, metadata refresh, and AniList match selection.
It auto-refreshes from the SQLite database every few seconds, so new hook-created watchlist entries should appear while the GUI is already open.

The GUI does not open automatically during normal tracking. To open it for a single `ani-cli` run:

```sh
ANI_WATCH_OPEN_GUI=1 ani-cli
```

For dependency and import checks without opening a window:

```sh
ani-watch-gui --check
ani-watch gui --check
```

### Screenshots

Screenshots are not committed yet. Suggested captures:

- Main library grouped by status
- Anime detail view with episode checklist
- Metadata match chooser
- Dashboard and recent activity panel

## Metadata

AniList GraphQL is used for metadata and cover art only. It is not used for playback.

On new titles, AniList candidates are stored in `metadata_matches`. High-confidence single matches can be auto-linked. Low-confidence or ambiguous matches remain selectable in the GUI. A selected match is not overwritten by later automatic searches.

Metadata commands:

```sh
ani-watch metadata search "Title"
ani-watch metadata set "Title" --anilist-id 12345
ani-watch metadata refresh "Title"
```

Cover art is cached locally. If a cover download fails, a placeholder image is used where practical and the GUI continues without crashing.

## Backup And Export

Create a timestamped SQLite backup:

```sh
ani-watch backup
```

Export JSON:

```sh
ani-watch export --format json --output watchlist.json
```

Export CSV:

```sh
ani-watch export --format csv --output watchlist.csv
```

Import JSON:

```sh
ani-watch import watchlist.json
```

Import a local ani-cli history entry into the watchlist:

```sh
ani-watch import-history --search "Wistoria"
```

Restore a SQLite backup:

```sh
ani-watch restore ~/.local/share/ani-watchlist/watchlist-YYYYMMDD-HHMMSS.sqlite3
```

To test restore without touching the active database, point the app at a temporary DB:

```sh
ANI_WATCHLIST_DB=/tmp/ani-watch-restore-test.sqlite3 ani-watch restore PATH
```

## Rollback

Rollback does not require touching the original system install.

```sh
scripts/uninstall-user.sh
hash -r
command -v ani-cli
```

Or manually remove only the patched ani-cli shim:

```sh
rm -f ~/.local/bin/ani-cli
hash -r
command -v ani-cli
```

After removing or renaming the `~/.local/bin/ani-cli` symlink, your shell should fall back to the previous system command, commonly `/usr/local/bin/ani-cli` or `/usr/bin/ani-cli`.

The watchlist database is preserved unless you remove it manually:

```text
~/.local/share/ani-watchlist/watchlist.sqlite3
```

To remove only the app command shims while preserving data:

```sh
rm -f ~/.local/bin/ani-watch ~/.local/bin/ani-watch-gui ~/.local/bin/ani-watch-hook ~/.local/bin/ani-watch-sync
```

Optional data removal:

```sh
scripts/uninstall-user.sh --purge-data
```

The manual equivalent is:

```sh
rm -rf ~/.local/share/ani-watchlist ~/.config/ani-watchlist ~/.cache/ani-watchlist ~/.local/state/ani-watchlist
```

## Troubleshooting

Run:

```sh
ani-watch doctor
```

It checks the database path, config path, cover cache path, logs path, whether the modified `ani-cli` is first on PATH, whether the hook integration is present, GUI dependency availability, and AniList metadata lookup.
It also checks Python version, schema version, readability/writability of local paths, and whether an original/system `ani-cli` still exists.

Use `ani-watch doctor --no-network` to skip the live AniList check.

If you opened a terminal before installing the user-level `ani-cli`, that shell may have cached `/usr/local/bin/ani-cli`. In that same terminal run:

```sh
hash -r
type -a ani-cli
```

The first entry should be `~/.local/bin/ani-cli`.

View hook error logs:

```sh
ani-watch logs
ani-watch logs --tail
```

Run repair diagnostics:

```sh
ani-watch repair
ani-watch repair --yes
```

## Known Limitations

- Tracking depends on the patched bundled copy being first on PATH.
- Cover thumbnails need Pillow. Pillow is installed by default through project metadata.
- AniList failures never block local watch tracking, but cover art and total episode counts may be missing until a later refresh.
- Existing `ani-cli` upstream update behavior patches the running script. If you use `ani-cli -U` on the symlinked modified script, reapply this branch or reinstall from this workspace.
- Shellcheck is optional. If it is not installed, `make shellcheck` reports that it was skipped.

## Publishing Notes

Before publishing to GitHub:

1. Confirm the root `LICENSE` and `NOTICE.md` are present.
2. Confirm generated files are ignored: `.venv/`, `.pytest_cache/`, `__pycache__/`, `*.egg-info/`, SQLite databases, exports, `ani-cli/.git/`, and upstream-only `ani-cli/.github/` or `ani-cli/.assets/` folders.
3. Run `make package-check`.
4. Initialize the repository if needed and commit from the project root:

```sh
git init
git add .
git status
git commit -m "Initial ani-watchlist release"
git branch -M main
git remote add origin https://github.com/GenshinPapi/AniAutoWatchList.git
git push -u origin main
```

If the working directory contains local Git metadata you do not want to reuse, create a clean upload tree first:

```sh
make export-upload-tree DEST=/tmp/ani-watchlist-upload
cd /tmp/ani-watchlist-upload
git init
git add .
git status
```

The `ani-cli/.git/` directory may exist in this local workspace because the patched script was originally developed from an upstream clone. It is intentionally ignored so the bundled patched script is committed as a normal file, not as a submodule.
