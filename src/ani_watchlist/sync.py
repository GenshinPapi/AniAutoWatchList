from __future__ import annotations

import argparse

from .config import load_config
from .db import initialize
from .metadata import refresh_metadata_for_anime
from .store import list_anime


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ani-watch-sync")
    parser.add_argument("--status")
    args = parser.parse_args(argv)
    config = load_config()
    failures = 0
    with initialize() as conn:
        for anime in list_anime(conn, status=args.status):
            try:
                matches = refresh_metadata_for_anime(conn, anime["id"], config)
                print(f"{anime['display_title']}: {len(matches)} candidate(s)")
            except Exception as exc:
                failures += 1
                print(f"{anime['display_title']}: failed: {exc}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
