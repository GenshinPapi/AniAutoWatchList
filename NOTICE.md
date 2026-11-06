# Notices

This repository contains `ani-watchlist`, a local Python/SQLite watchlist and GUI that integrates with a patched copy of `ani-cli`.

## ani-cli

The file `ani-cli/ani-cli` is derived from the upstream `ani-cli` project and is distributed under the GNU General Public License version 3 or later.

- Upstream project: https://github.com/pystardust/ani-cli
- Upstream license: `ani-cli/LICENSE`
- Local modifications: watchlist hook calls, safe optional GUI launch, playback start/finish wrappers, English-first search result display using metadata already returned by the existing ani-cli search API path, and a hianime.at playback path with megaplay.buzz, zokoanime.video, anizone.to and kaa.lt fallbacks.

The hianime.at provider (`hianime_*` functions in `ani-cli/ani-cli`) is derived from work proposed upstream under the same GPL-3.0-or-later license:

- https://github.com/pystardust/ani-cli/pull/1894 by Dhairya3391, which reverse engineered the hianime
  endpoints, the HD-1 embed, and the `base64(json XOR "otaku-embed-v1")` player config, with external
  WebVTT handling contributed by Tsagar3.
- https://github.com/pystardust/ani-cli/pull/1897 by U-L-M-S, which reworked it and fixed the search
  sidebar, subtitle track selection, and player subtitle flags.

AniAutoWatchList adapts it to this fork's AllAnime search and episode-list path and uses it as the
primary playback provider.

## Safety Boundary

This project does not bypass DRM, paywalls, or login systems, and does not use EverythingMoe or any other link list as an automated playback resolver. Playback resolves against the same class of public source ani-cli already targets: hianime.at first, with megaplay.buzz, zokoanime.video, anizone.to and kaa.lt as fallbacks when it is unavailable. AniList is used only for metadata and cover art.
