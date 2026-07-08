# AniAutoWatchList

AniAutoWatchList adds a local watchlist, episode tracker, and desktop GUI around `ani-cli`.

It keeps ani-cli's normal playback path intact. The patched ani-cli script only adds hook calls so the Python app can record launches, selected titles, listed episodes, playback starts, and playback finishes.

## Safety

This project does not add new streaming scrapers, does not bypass DRM/paywalls/login systems, and does not use EverythingMoe or link-list sites as playback resolvers. AniList is used only for metadata and cover art.

## Features

- Local SQLite watchlist
- Dark Tkinter desktop GUI
- Trending, top airing, most popular, and release schedule tabs powered by AniList metadata
- Status tabs: Watching, Completed, Dropped, On Hold, Plan to Watch
- Episode watched/unwatched tracking
- Continue button in the GUI for launching a selected episode through ani-cli
- Startup update check against the GitHub `main` branch
- Global AniList title search with suggestions and card results
- Related seasons on anime detail pages
- Automatic released episode refresh from AllAnime when titles are added or opened
- Automatic duplicate cleanup for matching AniList titles and season-title aliases
- AniList metadata search and cover caching
- English-first display titles when metadata is available
- Notes per anime
- JSON and CSV export
- SQLite backups and restore
- Duplicate detection and merge command
- Activity log and repair checks
- User-level install that does not overwrite system ani-cli

## Install

Linux Mint/Ubuntu dependencies:

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
```

The installer creates a virtual environment at:

```text
~/.local/share/ani-watchlist/venv
```

It installs the Python package dependencies used by the GUI and bundled `ani-cli` fixes into that virtual environment.

It symlinks these commands into `~/.local/bin`:

```text
ani-cli
ani-watch
ani-watch-gui
ani-watch-hook
ani-watch-sync
```

The original system ani-cli is not overwritten.

## Launch

Run ani-cli normally:

```sh
ani-cli
```

Open the GUI:

```sh
ani-watch-gui
```

or:

```sh
ani-watch gui
```

The GUI does not need to be open for tracking to work.

By default, an episode is marked watched when the player exits successfully. Playback failures are recorded but do not mark episodes watched.

In the GUI detail page, select an episode and click **Continue** to choose **Sub** or **Dub** and open ani-cli for that title and episode. Continue uses selected metadata to resolve the intended AllAnime show when it can do so confidently, then opens that show through ani-cli. If the metadata match is not confident enough, it falls back to the normal ani-cli title search. Dub launches use ani-cli's `--dub` option; if no dub is found for the selected episode, the GUI offers to search sub instead.

The anime detail page also includes a **Watch Party** menu. Hosting a watch party starts the selected episode, opens a host control window, and generates a share link. Guests can paste the link into **Watch Party > Join Watch Party** or run:

```sh
ani-watch party join URL
```

Watch parties synchronize local playback control only; video is not rebroadcast. For friends outside your local network, AniAutoWatchList uses Cloudflare Tunnel through `cloudflared` to create a temporary public link. If `cloudflared` is not already installed, the app downloads a user-local copy into its app data directory. If the download or tunnel startup fails, the generated link is local-only and the host window shows the tunnel error.

To open the GUI automatically for a single ani-cli run:

```sh
ANI_WATCH_OPEN_GUI=1 ani-cli
```

To temporarily disable tracking hooks:

```sh
ANI_WATCH_DISABLE=1 ani-cli
```

## Data Locations

```text
Database: ~/.local/share/ani-watchlist/watchlist.sqlite3
Config:   ~/.config/ani-watchlist/config.toml
Covers:   ~/.cache/ani-watchlist/covers/
Logs:     ~/.local/state/ani-watchlist/logs/
```

## Commands

List and inspect:

```sh
ani-watch list
ani-watch list --status watching
ani-watch show "Anime Title"
ani-watch dashboard
ani-watch continue
ani-watch next "Anime Title"
ani-watch discover trending
ani-watch discover trending --refresh
ani-watch schedule
ani-watch schedule --refresh
```

Edit progress:

```sh
ani-watch mark "Anime Title" 12 --watched
ani-watch mark "Anime Title" 12 --unwatched
ani-watch status "Anime Title" completed
ani-watch add "Anime Title" --episodes "1,2,3"
ani-watch delete "Anime Title"
```

Metadata:

```sh
ani-watch metadata search "Anime Title"
ani-watch metadata set "Anime Title" --anilist-id 12345
ani-watch metadata refresh "Anime Title"
ani-watch refresh-metadata "Anime Title"
ani-watch-sync
```

Import, export, and backups:

```sh
ani-watch export --format json --output watchlist.json
ani-watch export --format csv --output watchlist.csv
ani-watch import watchlist.json
ani-watch backup
ani-watch restore PATH
ani-watch import-history --search "Anime Title"
```

Maintenance:

```sh
ani-watch doctor
ani-watch doctor --no-network
ani-watch events "Anime Title"
ani-watch events --recent 20
ani-watch logs
ani-watch logs --tail
ani-watch duplicates
ani-watch merge "Title A" "Title B" --yes
ani-watch repair
ani-watch repair --yes
ani-watch config get tracking.mark_watched_after_seconds
ani-watch config set tracking.mark_watched_after_seconds 120
ani-watch install-desktop-entry
```

`tracking.mark_watched_after_seconds` defaults to `0`, which means any successful player exit marks the episode watched. Set it higher if you want a minimum watch time.

## GUI

The GUI includes:

- Trending tab
- Top Airing tab
- Most Popular tab
- Global search box in the top navigation with title suggestions
- Genre/tag filter for Most Popular
- Discovery tabs load 100 titles at a time and fetch the next batch as you page forward
- Long card titles can be scrolled inside the fixed-size cards
- 7-day release schedule tab
- Status tabs
- Cover grid
- Search/filter box
- Anime detail page
- Related seasons and side stories on detail pages
- Episode checklist
- Automatic released episode list refresh from AllAnime
- Startup cleanup for duplicate watchlist rows that refer to the same anime
- Continue selected episode through ani-cli
- Notes editor
- Metadata refresh
- AniList match selection
- Recent activity panel

Type in the top search box to fetch AniList title suggestions. Press Enter to show matching titles as discovery-style cards with the same Plan and AniList actions.

When a watchlist entry has an AniList match, the detail page shows related anime from AniList below the episode and activity panels. Sequels, prequels, parent entries, side stories, and spin-offs appear as discovery-style cards. Prequel, sequel, and parent links are followed across the relation chain so later seasons can appear even when AniList links them through an intermediate entry.

When an anime is added from discovery/search or opened from the watchlist, the GUI checks AllAnime for currently released sub episodes and upserts those episode rows. This keeps the watchlist progress count and detail episode list populated before launching playback.

Discovery and schedule data refresh from AniList at most once per local day on GUI startup, unless you press Refresh or use the CLI `--refresh` option. On startup, the GUI also checks GitHub for a newer `main` branch commit and checks whether the AniAutoWatchList-bundled patched `ani-cli` has moved beyond the local installed copy. If an AniAutoWatchList update is available and you accept it, a terminal opens, pulls the latest code, reruns `scripts/install-user.sh`, and prompts you to relaunch the GUI.

## How ani-cli Is Patched

The bundled `ani-cli/ani-cli` script is based on upstream ani-cli. The local patch adds calls to `ani-watch-hook` at these points:

- launch
- title selected
- episodes listed
- playback started
- playback finished

Search still uses ani-cli's existing AllAnime API path. The patch requests the existing `englishName` field so search results can display English titles when available.

Playback is still handled by ani-cli and the configured player.

Do not use `ani-cli -U` against the `~/.local/bin/ani-cli` symlink installed by this project. The bundled script disables that direct upstream self-patcher so the hook integration, embedded-player mpv flags, and mp4 provider fixes are not overwritten. Update through AniAutoWatchList instead.

## Development

```sh
python3 -m venv .venv
. .venv/bin/activate
pip install -e '.[test]'
make test
make shellcheck
make package-check
```

`make shellcheck` skips if shellcheck is not installed.

## Uninstall

Remove the user-level command shims and virtual environment:

```sh
scripts/uninstall-user.sh
hash -r
command -v ani-cli
```

The watchlist database is preserved.

Remove app data too:

```sh
scripts/uninstall-user.sh --purge-data
```

Manual rollback for only ani-cli:

```sh
rm -f ~/.local/bin/ani-cli
hash -r
command -v ani-cli
```

After rollback, your shell should use the previous system ani-cli, usually `/usr/local/bin/ani-cli` or `/usr/bin/ani-cli`.

## License

GPL-3.0-or-later.

The bundled `ani-cli/ani-cli` script is derived from the upstream ani-cli project. See `NOTICE.md` and `ani-cli/LICENSE` for attribution and license details.
