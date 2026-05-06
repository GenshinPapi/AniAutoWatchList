# Notices

This repository contains `ani-watchlist`, a local Python/SQLite watchlist and GUI that integrates with a patched copy of `ani-cli`.

## ani-cli

The file `ani-cli/ani-cli` is derived from the upstream `ani-cli` project and is distributed under the GNU General Public License version 3 or later.

- Upstream project: https://github.com/pystardust/ani-cli
- Upstream license: `ani-cli/LICENSE`
- Local modifications: watchlist hook calls, safe optional GUI launch, playback start/finish wrappers, and English-first search result display using metadata already returned by the existing ani-cli search API path.

## Safety Boundary

This project does not add new streaming scrapers, does not bypass DRM/paywalls/login systems, and does not use EverythingMoe or any other link list as an automated playback resolver. AniList is used only for metadata and cover art.
