from __future__ import annotations

import argparse
import csv
import json
import os
import re
import shutil
import sys
import time
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path
from typing import Any

from .config import get_config_value, load_config, set_config_value
from .db import initialize
from .discovery import load_discovery, refresh_discovery
from .doctor import run_doctor
from .launcher import LaunchError, launch_episode
from .metadata import refresh_metadata_for_anime, search_and_store_matches, select_match, set_anilist_id
from .party import MpvIpcController, WatchPartyError, WatchPartyMedia, WatchPartyRemoteClient, party_ipc_path
from .paths import get_paths
from .providers.anilist import AniListProvider
from .timefmt import local_time
from .store import (
    STATUSES,
    backup_database,
    clean_display_title,
    delete_anime,
    episodes_for_anime,
    export_data,
    get_anime,
    get_or_create_anime,
    import_data,
    likely_duplicates,
    list_anime,
    mark_episode,
    merge_anime,
    next_unwatched_episode,
    recently_watched_anime,
    repair_database,
    restore_database,
    status_counts,
    update_anime_fields,
    upsert_episodes,
    watch_events,
    watched_episode_count,
)


STATUS_LABELS = {
    "watching": "Watching",
    "completed": "Completed",
    "dropped": "Dropped",
    "on_hold": "On Hold",
    "plan_to_watch": "Plan to Watch",
}


def _table(headers: list[str], rows: list[list[Any]]) -> str:
    values = [[str(cell) if cell is not None else "" for cell in row] for row in rows]
    widths = [len(header) for header in headers]
    for row in values:
        for idx, cell in enumerate(row):
            widths[idx] = max(widths[idx], len(cell))
    lines = ["  ".join(header.ljust(widths[idx]) for idx, header in enumerate(headers))]
    lines.append("  ".join("-" * width for width in widths))
    for row in values:
        lines.append("  ".join(cell.ljust(widths[idx]) for idx, cell in enumerate(row)))
    return "\n".join(lines)


def _progress(row) -> str:
    total = row["total_episodes"] if row["total_episodes"] is not None else "?"
    available = row["available_episode_count"] if row["available_episode_count"] is not None else "?"
    watched = row["watched_count"] if "watched_count" in row.keys() else "?"
    return f"{watched}/{available}/{total}"


def _print_grouped_anime(rows) -> None:
    grouped: dict[str, list[Any]] = {status: [] for status in STATUSES}
    for row in rows:
        grouped[row["status"]].append(row)
    any_rows = False
    for status in STATUSES:
        items = grouped[status]
        if not items:
            continue
        any_rows = True
        print(STATUS_LABELS[status])
        print(
            _table(
                ["Title", "Progress", "Last Watched"],
                [[row["display_title"], _progress(row), local_time(row["last_watched_at"])] for row in items],
            )
        )
        print()
    if not any_rows:
        print("No anime found.")


def _find_anime_or_error(conn, title: str):
    anime = get_anime(conn, title)
    if anime is None:
        raise KeyError(f"not found: {title}")
    return anime


def cmd_list(args: argparse.Namespace) -> int:
    with initialize() as conn:
        rows = list_anime(conn, status=args.status, search=args.search)
        _print_grouped_anime(rows)
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    with initialize() as conn:
        anime = get_anime(conn, args.title)
        if anime is None:
            print(f"not found: {args.title}", file=sys.stderr)
            return 1
        print(f"Title: {anime['display_title']}")
        print(f"Status: {anime['status']}")
        print(f"AniList ID: {anime['anilist_id'] or '-'}")
        print(f"Total Episodes: {anime['total_episodes'] or '?'}")
        print(f"Available Episodes: {anime['available_episode_count'] or '?'}")
        print(f"Last Watched: {local_time(anime['last_watched_at'])}")
        print(f"Cover: {anime['cover_path'] or anime['cover_url'] or '-'}")
        print(f"Notes: {anime['notes'] or ''}")
        episodes = episodes_for_anime(conn, anime["id"])
        if episodes:
            print()
            print(
                _table(
                    ["Watched", "Episode", "Title", "Started", "Watched At"],
                    [
                        [
                            "yes" if episode["watched"] else "no",
                            episode["episode_key"],
                            episode["title"] or "",
                            local_time(episode["last_started_at"]),
                            local_time(episode["watched_at"]),
                        ]
                        for episode in episodes
                    ],
                )
            )
    return 0


def cmd_mark(args: argparse.Namespace) -> int:
    watched = bool(args.watched)
    with initialize() as conn:
        anime, _ = get_or_create_anime(conn, args.title)
        episode = mark_episode(conn, anime["id"], args.episode, watched)
        state = "watched" if episode["watched"] else "unwatched"
        print(f"{anime['display_title']} episode {episode['episode_key']}: {state}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    with initialize() as conn:
        anime, _ = get_or_create_anime(conn, args.title, status=args.status)
        anime = update_anime_fields(conn, anime["id"], status=args.status)
        print(f"{anime['display_title']}: {anime['status']}")
    return 0


def cmd_add(args: argparse.Namespace) -> int:
    episodes = []
    if args.episodes:
        episodes = [item.strip() for item in args.episodes.split(",") if item.strip()]
    with initialize() as conn:
        anime, created = get_or_create_anime(conn, args.title, status=args.status)
        if args.notes is not None:
            anime = update_anime_fields(conn, anime["id"], notes=args.notes)
        if episodes:
            upsert_episodes(conn, anime["id"], episodes, source_label="manual")
        print(("added" if created else "updated") + f": {anime['display_title']}")
    return 0


def cmd_delete(args: argparse.Namespace) -> int:
    with initialize() as conn:
        if delete_anime(conn, args.title):
            print(f"deleted: {args.title}")
            return 0
    print(f"not found: {args.title}", file=sys.stderr)
    return 1


def cmd_dashboard(args: argparse.Namespace) -> int:
    with initialize() as conn:
        counts = status_counts(conn)
        total = sum(counts.values())
        print("Dashboard")
        print(_table(["Metric", "Count"], [["Total anime", total], *[[STATUS_LABELS[s], counts[s]] for s in STATUSES], ["Watched episodes", watched_episode_count(conn)]]))
        recent = recently_watched_anime(conn, limit=5)
        if recent:
            print()
            print("Recently Watched")
            print(_table(["Title", "Last Watched"], [[row["display_title"], local_time(row["last_watched_at"])] for row in recent]))
        watching = list_anime(conn, status="watching")
        next_rows = []
        for anime in watching:
            episode = next_unwatched_episode(conn, anime["id"])
            next_rows.append([anime["display_title"], episode["episode_key"] if episode else "-"])
        if next_rows:
            print()
            print("Next Unwatched")
            print(_table(["Title", "Next Episode"], next_rows))
    return 0


def cmd_next(args: argparse.Namespace) -> int:
    with initialize() as conn:
        anime = get_anime(conn, args.title)
        if anime is None:
            print(f"not found: {args.title}", file=sys.stderr)
            return 1
        episode = next_unwatched_episode(conn, anime["id"])
        if episode is None:
            print(f"{anime['display_title']}: no unwatched episodes")
        else:
            print(f"{anime['display_title']}: episode {episode['episode_key']}")
    return 0


def cmd_continue(args: argparse.Namespace) -> int:
    with initialize() as conn:
        rows = []
        for anime in recently_watched_anime(conn, limit=args.limit):
            episode = next_unwatched_episode(conn, anime["id"])
            rows.append([anime["display_title"], local_time(anime["last_watched_at"]), episode["episode_key"] if episode else "-"])
        print(_table(["Title", "Last Watched", "Next Episode"], rows) if rows else "No recently watched anime.")
    return 0


def cmd_discover_trending(args: argparse.Namespace) -> int:
    with initialize() as conn:
        if args.refresh:
            refresh_discovery(conn, load_config(), force=True)
        data = load_discovery(conn)["trending"]
    items = list(data.get("items") or [])[: args.limit]
    if data.get("error"):
        print(f"warning: {data['error']}", file=sys.stderr)
    if not items:
        print("No trending data cached. Re-run with --refresh or open the GUI.")
        return 0
    print(
        _table(
            ["Title", "Score", "Trending", "Status", "Next"],
            [
                [
                    item.get("display_title") or "-",
                    item.get("average_score") or "-",
                    item.get("trending") or "-",
                    item.get("status") or "-",
                    (item.get("next_airing_episode") or {}).get("episode") or "-",
                ]
                for item in items
            ],
        )
    )
    print(f"Last updated: {local_time(data.get('fetched_at'))}")
    return 0


def cmd_schedule(args: argparse.Namespace) -> int:
    with initialize() as conn:
        if args.refresh:
            refresh_discovery(conn, load_config(), force=True)
        data = load_discovery(conn)["schedule"]
    items = list(data.get("items") or [])
    if data.get("error"):
        print(f"warning: {data['error']}", file=sys.stderr)
    if not items:
        print("No schedule data cached. Re-run with --refresh or open the GUI.")
        return 0
    print(
        _table(
            ["Day", "Time", "Episode", "Title"],
            [
                [
                    item.get("local_day") or "-",
                    item.get("local_time") or "-",
                    item.get("episode") or "-",
                    ((item.get("media") or {}).get("display_title") if isinstance(item.get("media"), dict) else "-"),
                ]
                for item in items
            ],
        )
    )
    print(f"Last updated: {local_time(data.get('fetched_at'))}")
    return 0


def cmd_duplicates(args: argparse.Namespace) -> int:
    with initialize() as conn:
        groups = likely_duplicates(conn)
        if not groups:
            print("No likely duplicates found.")
            return 0
        for key, rows in groups:
            print(f"Duplicate key: {key}")
            print(_table(["ID", "Title", "Status", "AniList ID"], [[r["id"], r["display_title"], r["status"], r["anilist_id"] or "-"] for r in rows]))
            print()
    return 0


def cmd_merge(args: argparse.Namespace) -> int:
    with initialize() as conn:
        try:
            target = _find_anime_or_error(conn, args.target_title)
            source = _find_anime_or_error(conn, args.source_title)
        except KeyError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        print("Merge plan")
        print(f"Target: {target['display_title']} (id {target['id']})")
        print(f"Source: {source['display_title']} (id {source['id']})")
        print(f"Source episodes: {len(episodes_for_anime(conn, source['id']))}")
        print("Progress, metadata candidates, notes, and watch events will move into the target.")
        if not args.yes:
            print("Re-run with --yes to apply this merge.")
            return 2
        result = merge_anime(conn, target["id"], source["id"])
        print(f"Merged {result['episode_count']} episode row(s).")
    return 0


def cmd_metadata_search(args: argparse.Namespace) -> int:
    config = load_config()
    with initialize() as conn:
        try:
            anime = get_anime(conn, args.title)
            if anime is None:
                matches = AniListProvider(config.anilist).search_title(args.title)
            else:
                matches = search_and_store_matches(conn, anime["id"], args.title, config)
        except Exception as exc:
            print(f"metadata search failed: {exc}", file=sys.stderr)
            return 1
        rows = [[idx + 1, match.media_id, f"{match.confidence_score:.2f}", match.title] for idx, match in enumerate(matches)]
        print(_table(["#", "AniList ID", "Confidence", "Title"], rows) if rows else "No matches.")
    return 0


def cmd_metadata_set(args: argparse.Namespace) -> int:
    config = load_config()
    with initialize() as conn:
        anime, _ = get_or_create_anime(conn, args.title)
        try:
            set_anilist_id(conn, anime["id"], args.anilist_id, AniListProvider(config.anilist))
        except Exception as exc:
            print(f"metadata set failed: {exc}", file=sys.stderr)
            return 1
        print(f"{anime['display_title']}: AniList ID set to {args.anilist_id}")
    return 0


def cmd_metadata_refresh(args: argparse.Namespace) -> int:
    config = load_config()
    with initialize() as conn:
        anime = get_anime(conn, args.title)
        if anime is None:
            print(f"not found: {args.title}", file=sys.stderr)
            return 1
        try:
            matches = refresh_metadata_for_anime(conn, anime["id"], config)
        except Exception as exc:
            print(f"metadata refresh failed: {exc}", file=sys.stderr)
            return 1
        print(f"stored {len(matches)} AniList candidate(s)")
    return 0


def cmd_refresh_metadata(args: argparse.Namespace) -> int:
    return cmd_metadata_refresh(args)


def cmd_events(args: argparse.Namespace) -> int:
    with initialize() as conn:
        anime_id = None
        if args.title:
            anime = get_anime(conn, args.title)
            if anime is None:
                print(f"not found: {args.title}", file=sys.stderr)
                return 1
            anime_id = anime["id"]
        events = watch_events(conn, anime_id=anime_id, recent=args.recent, title_hint=args.title)
        rows = []
        for event in events:
            payload = json.loads(event["payload_json"])
            rows.append(
                [
                    local_time(event["created_at"]),
                    event["event_type"],
                    event["anime_title"] or "-",
                    event["episode_key"] or payload.get("episode", "-"),
                ]
            )
        print(_table(["Created", "Event", "Title", "Episode"], rows) if rows else "No events.")
    return 0


def cmd_gui(args: argparse.Namespace) -> int:
    from .gui import main as gui_main

    return gui_main(["--check"] if args.check else [])


def cmd_doctor(args: argparse.Namespace) -> int:
    code, lines = run_doctor(check_network=not args.no_network)
    for line in lines:
        print(line)
    return code


def _csv_export(rows) -> str:
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(
        [
            "Title",
            "Status",
            "Watched episode count",
            "Available episode count",
            "Total episodes",
            "Last watched date",
            "Notes",
        ]
    )
    for row in rows:
        writer.writerow(
            [
                row["display_title"],
                row["status"],
                row["watched_count"],
                row["available_episode_count"] or "",
                row["total_episodes"] or "",
                row["last_watched_at"] or "",
                row["notes"] or "",
            ]
        )
    return output.getvalue()


def cmd_export(args: argparse.Namespace) -> int:
    with initialize() as conn:
        if args.format == "json":
            text = json.dumps(export_data(conn), indent=2, sort_keys=True)
        elif args.format == "csv":
            text = _csv_export(list_anime(conn))
        else:
            print(f"unsupported export format: {args.format}", file=sys.stderr)
            return 1
    if args.output:
        Path(args.output).expanduser().write_text(text, encoding="utf-8")
        print(args.output)
    else:
        print(text)
    return 0


def cmd_import(args: argparse.Namespace) -> int:
    source = Path(args.path).expanduser()
    if not source.exists():
        print(f"import file not found: {source}", file=sys.stderr)
        return 1
    try:
        data = json.loads(source.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"invalid JSON: {exc}", file=sys.stderr)
        return 1
    with initialize() as conn:
        result = import_data(conn, data)
    print(f"imported/updated {result['anime']} anime and {result['episodes']} episode rows")
    return 0


def _default_ani_cli_history_path() -> Path:
    hist_dir = os.environ.get("ANI_CLI_HIST_DIR")
    if hist_dir:
        return Path(hist_dir).expanduser() / "ani-hsts"
    state_home = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local" / "state"))).expanduser()
    return state_home / "ani-cli" / "ani-hsts"


def _history_total_episodes(title: str) -> int | None:
    match = re.search(r"\((\d+)\s+episodes?\)\s*$", title, flags=re.I)
    return int(match.group(1)) if match else None


def _episode_range(total: int | None, current_episode: str, max_episodes: int) -> list[str]:
    if total is None or total <= 0 or total > max_episodes:
        return [current_episode]
    return [str(number) for number in range(1, total + 1)]


def cmd_import_history(args: argparse.Namespace) -> int:
    path = Path(args.path).expanduser() if args.path else _default_ani_cli_history_path()
    if not path.exists():
        print(f"ani-cli history not found: {path}", file=sys.stderr)
        return 1
    imported = []
    skipped = 0
    with initialize() as conn:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line.strip():
                continue
            parts = line.split("\t", 2)
            if len(parts) != 3:
                skipped += 1
                continue
            current_episode, _source_id, source_title = parts
            display_title = clean_display_title(source_title)
            if args.search and args.search.casefold() not in display_title.casefold():
                continue
            total = _history_total_episodes(source_title)
            anime, _created = get_or_create_anime(conn, display_title, source_title=source_title, status="watching")
            episode_keys = _episode_range(total, current_episode, args.max_episodes)
            upsert_episodes(conn, anime["id"], episode_keys, source_label="ani-cli-history")
            updates: dict[str, Any] = {"status": "watching"}
            if total is not None:
                updates["total_episodes"] = total
            update_anime_fields(conn, anime["id"], **updates)
            if not args.no_mark_watched:
                mark_episode(conn, anime["id"], current_episode, watched=True)
            imported.append([display_title, current_episode, len(episode_keys), total or "?"])
    if imported:
        print(_table(["Title", "Watched Episode", "Episode Rows", "Total"], imported))
    else:
        print("No matching ani-cli history entries imported.")
    if skipped:
        print(f"Skipped {skipped} malformed history line(s).")
    return 0


def cmd_backup(args: argparse.Namespace) -> int:
    paths = get_paths()
    with initialize():
        pass
    dest = backup_database(paths.db_path, Path(args.output_dir).expanduser() if args.output_dir else None)
    print(dest)
    return 0


def cmd_restore(args: argparse.Namespace) -> int:
    source = Path(args.path).expanduser()
    if not source.exists():
        print(f"backup not found: {source}", file=sys.stderr)
        return 1
    restore_database(source, get_paths().db_path)
    print(f"restored: {source}")
    return 0


def cmd_config_get(args: argparse.Namespace) -> int:
    try:
        print(get_config_value(args.key))
    except KeyError:
        print(f"config key not found: {args.key}", file=sys.stderr)
        return 1
    return 0


def cmd_config_set(args: argparse.Namespace) -> int:
    try:
        value = set_config_value(args.key, args.value)
    except KeyError as exc:
        print(f"invalid config key: {exc}", file=sys.stderr)
        return 1
    print(f"{args.key} = {value}")
    return 0


def cmd_repair(args: argparse.Namespace) -> int:
    with initialize() as conn:
        report = repair_database(conn, fix=args.yes)
    for key, value in report.items():
        print(f"{key}: {value}")
    if not args.yes:
        print("Re-run with --yes to apply safe fixes.")
    return 0


def cmd_install_desktop_entry(args: argparse.Namespace) -> int:
    app_dir = Path.home() / ".local" / "share" / "applications"
    app_dir.mkdir(parents=True, exist_ok=True)
    gui = shutil.which("ani-watch-gui") or str(Path.home() / ".local" / "bin" / "ani-watch-gui")
    dest = app_dir / "ani-watch.desktop"
    dest.write_text(
        "\n".join(
            [
                "[Desktop Entry]",
                "Type=Application",
                "Name=ani-watchlist",
                "Comment=Local ani-cli watchlist",
                f"Exec={gui}",
                "Terminal=false",
                "Categories=AudioVideo;Utility;",
                "",
            ]
        ),
        encoding="utf-8",
    )
    print(dest)
    return 0


def cmd_logs(args: argparse.Namespace) -> int:
    log_path = get_paths().log_dir / "hook-errors.log"
    if not log_path.exists():
        print("No hook error log found.")
        return 0
    lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    if args.tail is not None:
        lines = lines[-args.tail :]
    print("\n".join(lines))
    return 0


def _party_launch_media(media: WatchPartyMedia, ipc_path: str) -> None:
    launch_episode(
        media.allanime_title or media.anime_title,
        media.episode,
        mode=media.mode,
        allanime_id=media.allanime_id,
        mpv_ipc_path=ipc_path,
    )


def _party_state_target_position(playback_state: dict[str, Any]) -> float:
    try:
        position = float(playback_state.get("position_seconds") or 0.0)
    except (TypeError, ValueError):
        position = 0.0
    if playback_state.get("paused"):
        return max(0.0, position)
    updated = str(playback_state.get("updated_at") or "")
    try:
        parsed = datetime.fromisoformat(updated.replace("Z", "+00:00"))
    except ValueError:
        return max(0.0, position)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    elapsed = max(0.0, (datetime.now(timezone.utc) - parsed.astimezone(timezone.utc)).total_seconds())
    return max(0.0, position + elapsed)


def _party_apply_playback_state(
    controller: MpvIpcController,
    playback_state: dict[str, Any],
    *,
    wait_for_socket: bool = False,
) -> None:
    deadline = time.monotonic() + 45
    while wait_for_socket and time.monotonic() < deadline and not controller.available():
        time.sleep(0.5)
    position = _party_state_target_position(playback_state)
    paused = bool(playback_state.get("paused"))
    if paused:
        controller.pause()
    controller.seek(position)
    if paused:
        controller.pause()
    else:
        controller.play()


def cmd_party_join(args: argparse.Namespace) -> int:
    try:
        client = WatchPartyRemoteClient(args.link, args.username)
        payload = client.join()
    except WatchPartyError as exc:
        print(f"watch party join failed: {exc}", file=sys.stderr)
        return 1
    state = payload.get("state") if isinstance(payload.get("state"), dict) else {}
    media_payload = state.get("media") if isinstance(state.get("media"), dict) else {}
    media = WatchPartyMedia.from_json(media_payload)
    print(f"Joined: {media.party_title}")
    print(f"Anime: {media.anime_title}")
    print(f"Episode: {media.episode} ({media.mode})")
    ipc_path = party_ipc_path(f"cli-{client.participant_id or 'guest'}")
    controller = MpvIpcController(ipc_path)
    if not args.no_launch:
        try:
            _party_launch_media(media, ipc_path)
        except LaunchError as exc:
            try:
                client.leave()
            except WatchPartyError:
                pass
            print(f"ani-cli launch failed: {exc}", file=sys.stderr)
            return 1
        playback_state = state.get("playback_state") if isinstance(state.get("playback_state"), dict) else None
        if playback_state is not None:
            try:
                _party_apply_playback_state(controller, playback_state, wait_for_socket=True)
            except Exception as exc:
                print(f"warning: initial playback sync failed: {exc}", file=sys.stderr)
    if args.once:
        return 0
    print("Waiting for host controls. Press Ctrl-C to leave.")
    try:
        while True:
            payload = client.poll_events()
            events = payload.get("events") if isinstance(payload.get("events"), list) else []
            for event in events:
                if not isinstance(event, dict):
                    continue
                event_type = str(event.get("event_type") or "")
                event_payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
                try:
                    if event_type in {"play", "pause", "seek", "relative_seek", "playback_state"}:
                        playback_state = event_payload.get("playback_state") if isinstance(event_payload.get("playback_state"), dict) else None
                        if playback_state is not None:
                            _party_apply_playback_state(controller, playback_state)
                        elif event_type == "play":
                            controller.play()
                        elif event_type == "pause":
                            controller.pause()
                        elif event_type == "seek":
                            controller.seek(float(event_payload.get("position_seconds") or 0))
                        elif event_type == "relative_seek":
                            controller.relative_seek(float(event_payload.get("delta_seconds") or 0))
                    elif event_type == "stop":
                        controller.stop()
                    elif event_type in {"next_episode", "previous_episode"}:
                        media_payload = event_payload.get("media") if isinstance(event_payload.get("media"), dict) else None
                        if media_payload is None:
                            continue
                        controller.stop()
                        media = WatchPartyMedia.from_json(media_payload)
                        ipc_path = party_ipc_path(f"cli-{client.participant_id or 'guest'}-{media.episode}")
                        controller = MpvIpcController(ipc_path)
                        _party_launch_media(media, ipc_path)
                        playback_state = event_payload.get("playback_state") if isinstance(event_payload.get("playback_state"), dict) else None
                        if playback_state is not None:
                            _party_apply_playback_state(controller, playback_state, wait_for_socket=True)
                    elif event_type == "participant_kicked":
                        participant = event_payload.get("participant") if isinstance(event_payload.get("participant"), dict) else {}
                        if participant.get("participant_id") == client.participant_id:
                            controller.stop()
                            print("You were removed from the watch party.")
                            return 0
                    elif event_type == "party_ended":
                        controller.stop()
                        print("The host ended the watch party.")
                        return 0
                except Exception as exc:
                    print(f"warning: could not apply {event_type}: {exc}", file=sys.stderr)
    except KeyboardInterrupt:
        print()
    finally:
        try:
            client.leave()
        except WatchPartyError:
            pass
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ani-watch")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("list")
    p.add_argument("--status", choices=STATUSES)
    p.add_argument("--search")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("show")
    p.add_argument("title")
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("mark")
    p.add_argument("title")
    p.add_argument("episode")
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--watched", action="store_true")
    group.add_argument("--unwatched", action="store_true")
    p.set_defaults(func=cmd_mark)

    p = sub.add_parser("status")
    p.add_argument("title")
    p.add_argument("status", choices=STATUSES)
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("add")
    p.add_argument("title")
    p.add_argument("--status", choices=STATUSES, default="watching")
    p.add_argument("--episodes", help="comma-separated episode keys")
    p.add_argument("--notes")
    p.set_defaults(func=cmd_add)

    p = sub.add_parser("delete")
    p.add_argument("title")
    p.set_defaults(func=cmd_delete)

    sub.add_parser("dashboard").set_defaults(func=cmd_dashboard)

    p = sub.add_parser("next")
    p.add_argument("title")
    p.set_defaults(func=cmd_next)

    p = sub.add_parser("continue")
    p.add_argument("--limit", type=int, default=10)
    p.set_defaults(func=cmd_continue)

    discover = sub.add_parser("discover")
    discover_sub = discover.add_subparsers(dest="discover_command", required=True)
    p = discover_sub.add_parser("trending")
    p.add_argument("--refresh", action="store_true")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=cmd_discover_trending)

    p = sub.add_parser("schedule")
    p.add_argument("--refresh", action="store_true")
    p.set_defaults(func=cmd_schedule)

    sub.add_parser("duplicates").set_defaults(func=cmd_duplicates)

    p = sub.add_parser("merge")
    p.add_argument("target_title")
    p.add_argument("source_title")
    p.add_argument("--yes", action="store_true")
    p.set_defaults(func=cmd_merge)

    p = sub.add_parser("refresh-metadata")
    p.add_argument("title")
    p.set_defaults(func=cmd_refresh_metadata)

    metadata = sub.add_parser("metadata")
    metadata_sub = metadata.add_subparsers(dest="metadata_command", required=True)
    p = metadata_sub.add_parser("search")
    p.add_argument("title")
    p.set_defaults(func=cmd_metadata_search)
    p = metadata_sub.add_parser("set")
    p.add_argument("title")
    p.add_argument("--anilist-id", type=int, required=True)
    p.set_defaults(func=cmd_metadata_set)
    p = metadata_sub.add_parser("refresh")
    p.add_argument("title")
    p.set_defaults(func=cmd_metadata_refresh)

    p = sub.add_parser("events")
    p.add_argument("title", nargs="?")
    p.add_argument("--recent", type=int)
    p.set_defaults(func=cmd_events)

    p = sub.add_parser("gui")
    p.add_argument("--check", action="store_true")
    p.set_defaults(func=cmd_gui)

    p = sub.add_parser("doctor")
    p.add_argument("--no-network", action="store_true")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("export")
    p.add_argument("--format", choices=("json", "csv"), default="json")
    p.add_argument("--output")
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("import")
    p.add_argument("path")
    p.set_defaults(func=cmd_import)

    p = sub.add_parser("import-history")
    p.add_argument("--path", help="path to ani-cli ani-hsts file")
    p.add_argument("--search", help="only import history titles containing this text")
    p.add_argument("--max-episodes", type=int, default=200)
    p.add_argument("--no-mark-watched", action="store_true")
    p.set_defaults(func=cmd_import_history)

    p = sub.add_parser("backup")
    p.add_argument("--output-dir")
    p.set_defaults(func=cmd_backup)

    p = sub.add_parser("restore")
    p.add_argument("path")
    p.set_defaults(func=cmd_restore)

    config = sub.add_parser("config")
    config_sub = config.add_subparsers(dest="config_command", required=True)
    p = config_sub.add_parser("get")
    p.add_argument("key")
    p.set_defaults(func=cmd_config_get)
    p = config_sub.add_parser("set")
    p.add_argument("key")
    p.add_argument("value")
    p.set_defaults(func=cmd_config_set)

    p = sub.add_parser("repair")
    p.add_argument("--yes", action="store_true")
    p.set_defaults(func=cmd_repair)

    sub.add_parser("install-desktop-entry").set_defaults(func=cmd_install_desktop_entry)

    p = sub.add_parser("logs")
    p.add_argument("--tail", nargs="?", const=100, type=int)
    p.set_defaults(func=cmd_logs)

    party = sub.add_parser("party")
    party_sub = party.add_subparsers(dest="party_command", required=True)
    p = party_sub.add_parser("join")
    p.add_argument("link")
    p.add_argument("--username", default=os.environ.get("USER", "Guest"))
    p.add_argument("--no-launch", action="store_true", help="join and listen without launching ani-cli")
    p.add_argument("--once", action="store_true", help="join, print room details, and exit")
    p.set_defaults(func=cmd_party_join)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
