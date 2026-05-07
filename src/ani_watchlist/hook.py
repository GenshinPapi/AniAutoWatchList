from __future__ import annotations

import argparse
import json
import sys
import traceback
from pathlib import Path
from typing import Any

from .config import load_config
from .db import initialize
from .metadata import search_and_store_matches
from .paths import get_paths
from .store import (
    get_or_create_anime,
    get_or_create_episode,
    mark_episode,
    mark_started,
    record_event,
    upsert_episodes,
)


def _parse_json(value: str | None, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return fallback


def _log_failure(exc: BaseException) -> None:
    paths = get_paths()
    paths.ensure()
    log_path = paths.log_dir / "hook-errors.log"
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(traceback.format_exc())
        fh.write("\n")


def _maybe_metadata(conn, anime, created: bool) -> None:
    if not created:
        return
    config = load_config()
    if not config.metadata.search_on_new_title:
        return
    try:
        search_and_store_matches(conn, anime["id"], anime["display_title"], config)
    except Exception:
        _log_failure(sys.exc_info()[1] or RuntimeError("metadata lookup failed"))


def _handle_launch(args: argparse.Namespace) -> None:
    with initialize() as conn:
        record_event(conn, "launch", {"argv": _parse_json(args.argv_json, []), "argv_json": args.argv_json})


def _handle_title_selected(args: argparse.Namespace) -> None:
    with initialize() as conn:
        anime, created = get_or_create_anime(conn, args.title, args.source_title or args.title)
        record_event(
            conn,
            "title_selected",
            {"title": args.title, "source_title": args.source_title},
            anime_id=anime["id"],
        )
        _maybe_metadata(conn, anime, created)


def _handle_episodes_listed(args: argparse.Namespace) -> None:
    episodes = _parse_json(args.episodes_json, [])
    if not isinstance(episodes, list):
        episodes = []
    with initialize() as conn:
        anime, created = get_or_create_anime(conn, args.title, args.source_title or args.title)
        rows = upsert_episodes(conn, anime["id"], episodes, source_label=args.source_label)
        record_event(
            conn,
            "episodes_listed",
            {
                "title": args.title,
                "source_title": args.source_title,
                "episodes_json": args.episodes_json,
                "episode_count": len(rows),
            },
            anime_id=anime["id"],
        )
        _maybe_metadata(conn, anime, created)


def _handle_playback_started(args: argparse.Namespace) -> None:
    with initialize() as conn:
        anime, created = get_or_create_anime(conn, args.title, args.source_title or args.title)
        episode = mark_started(conn, anime["id"], args.episode)
        record_event(
            conn,
            "playback_started",
            {"title": args.title, "source_title": args.source_title, "episode": args.episode},
            anime_id=anime["id"],
            episode_id=episode["id"],
        )
        _maybe_metadata(conn, anime, created)


def _handle_playback_finished(args: argparse.Namespace) -> None:
    config = load_config()
    exit_code = int(args.exit_code)
    duration = int(args.duration_seconds)
    event_type = "playback_finished" if exit_code == 0 else "playback_failed"
    with initialize() as conn:
        anime, created = get_or_create_anime(conn, args.title, args.source_title or args.title)
        episode, _ = get_or_create_episode(conn, anime["id"], args.episode)
        threshold = max(0, config.tracking.mark_watched_after_seconds)
        should_mark = exit_code == 0 and duration >= threshold
        if should_mark:
            episode = mark_episode(conn, anime["id"], args.episode, watched=True)
        record_event(
            conn,
            event_type,
            {
                "title": args.title,
                "source_title": args.source_title,
                "episode": args.episode,
                "exit_code": exit_code,
                "duration_seconds": duration,
                "marked_watched": should_mark,
            },
            anime_id=anime["id"],
            episode_id=episode["id"],
        )
        _maybe_metadata(conn, anime, created)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ani-watch-hook")
    subparsers = parser.add_subparsers(dest="event", required=True)

    launch = subparsers.add_parser("launch")
    launch.add_argument("--argv-json")
    launch.set_defaults(func=_handle_launch)

    selected = subparsers.add_parser("title-selected")
    selected.add_argument("--title", required=True)
    selected.add_argument("--source-title")
    selected.set_defaults(func=_handle_title_selected)

    listed = subparsers.add_parser("episodes-listed")
    listed.add_argument("--title", required=True)
    listed.add_argument("--source-title")
    listed.add_argument("--episodes-json", required=True)
    listed.add_argument("--source-label")
    listed.set_defaults(func=_handle_episodes_listed)

    started = subparsers.add_parser("playback-started")
    started.add_argument("--title", required=True)
    started.add_argument("--source-title")
    started.add_argument("--episode", required=True)
    started.set_defaults(func=_handle_playback_started)

    finished = subparsers.add_parser("playback-finished")
    finished.add_argument("--title", required=True)
    finished.add_argument("--source-title")
    finished.add_argument("--episode", required=True)
    finished.add_argument("--exit-code", required=True)
    finished.add_argument("--duration-seconds", required=True)
    finished.set_defaults(func=_handle_playback_finished)
    return parser


def run(argv: list[str] | None = None) -> int:
    paths = get_paths()
    paths.ensure()
    args = build_parser().parse_args(argv)
    args.func(args)
    return 0


def main(argv: list[str] | None = None) -> int:
    try:
        return run(argv)
    except SystemExit:
        raise
    except Exception as exc:
        _log_failure(exc)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
