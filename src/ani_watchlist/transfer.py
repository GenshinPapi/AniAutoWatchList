from __future__ import annotations

import json
import os
import re
import sqlite3
import xml.etree.ElementTree as ET
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

from .store import STATUSES, clean_display_title, export_data, import_data, list_anime, now_iso
from .config import load_config
from .paths import get_paths
from .providers.anilist import AniListProvider


WatchlistFormat = Literal["json", "xml"]
ImportMode = Literal["sync", "replace", "merge"]
MalIdResolver = Callable[[int], int | dict[str, Any] | None]

JSON_FILETYPES = (("JSON files", "*.json"), ("All files", "*.*"))
XML_FILETYPES = (("XML files", "*.xml"), ("All files", "*.*"))
WATCHLIST_FILETYPES = (("Watchlist files", "*.json *.xml"), ("JSON files", "*.json"), ("XML files", "*.xml"), ("All files", "*.*"))
AUTO_BACKUP_FILENAMES: dict[WatchlistFormat, str] = {
    "json": "jsonbackup.json",
    "xml": "xmlbackup.xml",
}

MAL_STATUS_BY_LOCAL = {
    "watching": "Watching",
    "completed": "Completed",
    "dropped": "Dropped",
    "on_hold": "On-Hold",
    "plan_to_watch": "Plan to Watch",
}
LOCAL_STATUS_BY_MAL = {
    "watching": "watching",
    "completed": "completed",
    "dropped": "dropped",
    "on-hold": "on_hold",
    "on hold": "on_hold",
    "on_hold": "on_hold",
    "plan to watch": "plan_to_watch",
    "plantowatch": "plan_to_watch",
    "ptw": "plan_to_watch",
}


class WatchlistTransferError(ValueError):
    pass


def infer_watchlist_format(path: str | Path, explicit_format: str | None = None) -> WatchlistFormat:
    if explicit_format:
        cleaned = explicit_format.casefold().strip()
        if cleaned in {"json", "xml"}:
            return cleaned  # type: ignore[return-value]
        raise WatchlistTransferError(f"unsupported watchlist format: {explicit_format}")
    suffix = Path(path).suffix.casefold()
    if suffix == ".json":
        return "json"
    if suffix == ".xml":
        return "xml"
    raise WatchlistTransferError("could not infer watchlist format; choose JSON or XML")


def export_watchlist_text(
    conn: sqlite3.Connection,
    export_format: WatchlistFormat,
    *,
    mal_id_resolver: MalIdResolver | None = None,
    skip_missing_mal_ids: bool = False,
) -> str:
    if export_format == "json":
        payload = export_data(conn)
        payload["format"] = "ani-watchlist"
        payload["version"] = 2
        payload["exported_at"] = now_iso()
        return json.dumps(payload, indent=2, sort_keys=True)
    if export_format == "xml":
        return export_watchlist_xml(
            conn,
            mal_id_resolver=mal_id_resolver,
            skip_missing_mal_ids=skip_missing_mal_ids,
        )
    raise WatchlistTransferError(f"unsupported export format: {export_format}")


def import_watchlist_text(
    conn: sqlite3.Connection,
    text: str,
    import_format: WatchlistFormat,
    *,
    mode: ImportMode = "sync",
    prefer_newer: bool = False,
) -> dict[str, int]:
    if mode not in {"sync", "replace", "merge"}:
        raise WatchlistTransferError(f"unsupported import mode: {mode}")
    if import_format == "json":
        try:
            raw_payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise WatchlistTransferError(f"invalid JSON: {exc}") from exc
        if not isinstance(raw_payload, dict):
            raise WatchlistTransferError("JSON watchlist import must be an object")
        payload = raw_payload.get("data") if isinstance(raw_payload.get("data"), dict) else raw_payload
    elif import_format == "xml":
        payload = parse_watchlist_xml(text)
    else:
        raise WatchlistTransferError(f"unsupported import format: {import_format}")

    if mode == "replace":
        clear_watchlist(conn)
    return import_data(
        conn,
        payload,
        update_existing=mode in {"replace", "merge"},
        prefer_newer=prefer_newer,
    )


def import_watchlist_file(
    conn: sqlite3.Connection,
    path: str | Path,
    *,
    import_format: str | None = None,
    mode: ImportMode = "sync",
) -> dict[str, int]:
    source = Path(path).expanduser()
    if not source.exists():
        raise WatchlistTransferError(f"import file not found: {source}")
    detected = infer_watchlist_format(source, import_format)
    return import_watchlist_text(conn, source.read_text(encoding="utf-8"), detected, mode=mode)


def write_watchlist_file(
    conn: sqlite3.Connection,
    path: str | Path,
    export_format: WatchlistFormat,
    *,
    mal_id_resolver: MalIdResolver | None = None,
    skip_missing_mal_ids: bool = False,
) -> Path:
    target = Path(path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        export_watchlist_text(
            conn,
            export_format,
            mal_id_resolver=mal_id_resolver,
            skip_missing_mal_ids=skip_missing_mal_ids,
        ),
        encoding="utf-8",
    )
    return target


def write_auto_backup_files(conn: sqlite3.Connection, directory: str | Path) -> dict[WatchlistFormat, Path]:
    """Write full JSON and portable XML snapshots without a network lookup.

    Both exports are rendered before either current backup is replaced. Each
    file is then written through a temporary sibling so an interrupted write
    does not leave a partially written backup behind.
    """
    backup_dir = Path(directory).expanduser()
    backup_dir.mkdir(parents=True, exist_ok=True)
    contents: dict[WatchlistFormat, str] = {
        "json": export_watchlist_text(conn, "json"),
        "xml": export_watchlist_text(conn, "xml"),
    }
    targets: dict[WatchlistFormat, Path] = {
        export_format: backup_dir / filename
        for export_format, filename in AUTO_BACKUP_FILENAMES.items()
    }
    temporary_files: dict[WatchlistFormat, Path] = {}
    try:
        for export_format in ("json", "xml"):
            target = targets[export_format]
            temporary = target.with_name(f".{target.name}.tmp")
            with temporary.open("w", encoding="utf-8") as handle:
                handle.write(contents[export_format])
                handle.flush()
                os.fsync(handle.fileno())
            temporary_files[export_format] = temporary
        for export_format in ("json", "xml"):
            temporary_files[export_format].replace(targets[export_format])
    except OSError:
        for temporary in temporary_files.values():
            try:
                temporary.unlink()
            except OSError:
                pass
        raise
    return targets


def anilist_mal_id_resolver(conn: sqlite3.Connection | None = None) -> MalIdResolver:
    provider = AniListProvider(load_config().anilist)
    cache = _load_mal_id_cache()
    if conn is not None:
        _warm_mal_id_cache(conn, provider, cache)

    def resolve(anilist_id: int) -> int | dict[str, Any] | None:
        key = str(int(anilist_id))
        cached = cache.get(key)
        if isinstance(cached, dict) and _mal_id_from_payload(cached):
            return cached
        try:
            media = provider.get_media(key)
        except Exception:
            return None
        if _mal_id_from_payload(media):
            cache[key] = media
            _save_mal_id_cache(cache)
        return media

    return resolve


def _warm_mal_id_cache(conn: sqlite3.Connection, provider: AniListProvider, cache: dict[str, Any]) -> None:
    ids = [
        int(row["anilist_id"])
        for row in conn.execute("SELECT DISTINCT anilist_id FROM anime WHERE anilist_id IS NOT NULL")
        if str(row["anilist_id"]).strip()
    ]
    missing = [media_id for media_id in ids if not _mal_id_from_payload(cache.get(str(media_id)) if isinstance(cache.get(str(media_id)), dict) else {})]
    if not missing:
        return
    try:
        media_rows = provider.get_media_batch(missing)
    except Exception:
        return
    changed = False
    for media in media_rows:
        media_id = _int_or_none(media.get("id"))
        if media_id is None or not _mal_id_from_payload(media):
            continue
        cache[str(media_id)] = media
        changed = True
    if changed:
        _save_mal_id_cache(cache)


def _mal_id_cache_file() -> Path:
    paths = get_paths()
    paths.cache_dir.mkdir(parents=True, exist_ok=True)
    return paths.cache_dir / "mal-id-cache.json"


def _load_mal_id_cache() -> dict[str, Any]:
    cache_file = _mal_id_cache_file()
    if not cache_file.exists():
        return {}
    try:
        payload = json.loads(cache_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _save_mal_id_cache(cache: dict[str, Any]) -> None:
    cache_file = _mal_id_cache_file()
    tmp_file = cache_file.with_suffix(cache_file.suffix + ".tmp")
    try:
        tmp_file.write_text(json.dumps(cache, indent=2, sort_keys=True), encoding="utf-8")
        tmp_file.replace(cache_file)
    except OSError:
        try:
            tmp_file.unlink()
        except OSError:
            pass


def clear_watchlist(conn: sqlite3.Connection) -> None:
    with conn:
        conn.execute("DELETE FROM watch_events")
        conn.execute("DELETE FROM metadata_matches")
        conn.execute("DELETE FROM episodes")
        conn.execute("DELETE FROM anime")


def export_watchlist_xml(
    conn: sqlite3.Connection,
    *,
    mal_id_resolver: MalIdResolver | None = None,
    skip_missing_mal_ids: bool = False,
) -> str:
    rows = list_anime(conn)
    entries: list[tuple[sqlite3.Row, dict[str, Any], int | None]] = []
    skipped_missing_mal_id = 0
    for row in rows:
        payload = _selected_anilist_payload(conn, int(row["id"]))
        payload = _payload_with_resolved_mal_id(payload, row["anilist_id"], mal_id_resolver)
        mal_id = _mal_id_for_anime(conn, int(row["id"]), payload)
        if skip_missing_mal_ids and not mal_id:
            skipped_missing_mal_id += 1
            continue
        entries.append((row, payload, mal_id))
    if skip_missing_mal_ids and rows and not entries:
        raise WatchlistTransferError(
            "No entries have MAL AnimeDB IDs. Refresh metadata or export JSON for a full AniAutoWatchList backup."
        )

    counts = {status: 0 for status in STATUSES}
    for row, _payload, _mal_id in entries:
        counts[str(row["status"])] += 1

    root = ET.Element("myanimelist")
    root.append(ET.Comment("Created by AniAutoWatchList MAL-style XML export"))
    if skipped_missing_mal_id:
        root.append(ET.Comment(f"Omitted {skipped_missing_mal_id} entr{'y' if skipped_missing_mal_id == 1 else 'ies'} without MAL AnimeDB IDs"))
    info = ET.SubElement(root, "myinfo")
    _sub_text(info, "user_id", "0")
    _sub_text(info, "user_name", "ani-watchlist")
    _sub_text(info, "user_export_type", "1")
    _sub_text(info, "user_total_anime", str(len(entries)))
    _sub_text(info, "user_total_watching", str(counts["watching"]))
    _sub_text(info, "user_total_completed", str(counts["completed"]))
    _sub_text(info, "user_total_onhold", str(counts["on_hold"]))
    _sub_text(info, "user_total_dropped", str(counts["dropped"]))
    _sub_text(info, "user_total_plantowatch", str(counts["plan_to_watch"]))

    for row, payload, mal_id in entries:
        anime = ET.SubElement(root, "anime")
        total_episodes = _int_or_zero(row["total_episodes"] or row["available_episode_count"] or _payload_value(payload, "episodes"))
        watched_count = _int_or_zero(row["watched_count"])
        started_at = _first_episode_date(conn, int(row["id"]))
        finished_at = _date_only(row["last_watched_at"]) if row["status"] == "completed" else None

        _sub_text(anime, "series_animedb_id", str(mal_id or 0))
        _sub_text(anime, "series_title", row["display_title"])
        _sub_text(anime, "series_type", _mal_series_type(payload))
        _sub_text(anime, "series_episodes", str(total_episodes))
        _sub_text(anime, "my_id", "0")
        _sub_text(anime, "my_watched_episodes", str(watched_count))
        _sub_text(anime, "my_start_date", started_at or "0000-00-00")
        _sub_text(anime, "my_finish_date", finished_at or "0000-00-00")
        _sub_text(anime, "my_rated", "")
        _sub_text(anime, "my_score", "0")
        _sub_text(anime, "my_storage", "")
        _sub_text(anime, "my_storage_value", "0.00")
        _sub_text(anime, "my_status", MAL_STATUS_BY_LOCAL.get(str(row["status"]), "Watching"))
        _sub_text(anime, "my_comments", row["notes"] or "")
        _sub_text(anime, "my_times_watched", "0")
        _sub_text(anime, "my_rewatch_value", "")
        _sub_text(anime, "my_priority", "LOW")
        _sub_text(anime, "my_tags", "")
        _sub_text(anime, "my_rewatching", "0")
        _sub_text(anime, "my_rewatching_ep", "0")
        _sub_text(anime, "my_discuss", "1")
        _sub_text(anime, "my_sns", "default")
        _sub_text(anime, "update_on_import", "1")

    ET.indent(root, space="  ")
    return "<?xml version=\"1.0\" encoding=\"UTF-8\" ?>\n" + ET.tostring(
        root,
        encoding="unicode",
        short_empty_elements=False,
    )


def parse_watchlist_xml(text: str) -> dict[str, Any]:
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise WatchlistTransferError(f"invalid XML: {exc}") from exc
    if root.tag != "myanimelist":
        raise WatchlistTransferError("XML watchlist root must be <myanimelist>")

    anime_rows: list[dict[str, Any]] = []
    episode_rows: list[dict[str, Any]] = []
    metadata_rows: list[dict[str, Any]] = []
    synthetic_id = 1
    synthetic_episode_id = 1
    for anime_node in root.findall("anime"):
        title = clean_display_title(_child_text(anime_node, "series_title"))
        if not title or title == "Unknown title":
            continue
        total_episodes = _int_or_none(_child_text(anime_node, "series_episodes"))
        watched_episodes = max(0, _int_or_zero(_child_text(anime_node, "my_watched_episodes")))
        status = _local_status_from_mal(_child_text(anime_node, "my_status"))
        start_date = _date_to_iso(_child_text(anime_node, "my_start_date"))
        finish_date = _date_to_iso(_child_text(anime_node, "my_finish_date"))
        notes = _child_text(anime_node, "my_comments") or None
        anime_id = synthetic_id
        synthetic_id += 1

        anime_rows.append(
            {
                "id": anime_id,
                "display_title": title,
                "source_title": title,
                "status": status,
                "total_episodes": total_episodes,
                "notes": notes,
                "last_watched_at": finish_date if watched_episodes else None,
            }
        )
        episode_count = max(watched_episodes, int(total_episodes or 0))
        for number in range(1, episode_count + 1):
            watched = number <= watched_episodes
            episode_rows.append(
                {
                    "id": synthetic_episode_id,
                    "anime_id": anime_id,
                    "episode_key": str(number),
                    "episode_number": str(number),
                    "watched": 1 if watched else 0,
                    "watched_at": finish_date if watched else None,
                    "first_started_at": start_date if watched else None,
                    "last_started_at": finish_date if watched else None,
                    "source_label": "mal-xml",
                }
            )
            synthetic_episode_id += 1

        mal_id = _int_or_zero(_child_text(anime_node, "series_animedb_id"))
        if mal_id > 0:
            metadata_rows.append(
                {
                    "anime_id": anime_id,
                    "provider": "myanimelist",
                    "provider_media_id": str(mal_id),
                    "confidence_score": 1.0,
                    "selected": 1,
                    "payload_json": json.dumps({"id": mal_id, "title": title}, sort_keys=True),
                    "created_at": now_iso(),
                }
            )

    return {"anime": anime_rows, "episodes": episode_rows, "metadata_matches": metadata_rows}


def _sub_text(parent: ET.Element, tag: str, value: object) -> None:
    ET.SubElement(parent, tag).text = "" if value is None else str(value)


def _child_text(parent: ET.Element, tag: str) -> str:
    child = parent.find(tag)
    return (child.text or "").strip() if child is not None else ""


def _selected_anilist_payload(conn: sqlite3.Connection, anime_id: int) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT payload_json FROM metadata_matches
        WHERE anime_id = ? AND provider = 'anilist' AND selected = 1
        ORDER BY confidence_score DESC, id
        LIMIT 1
        """,
        (anime_id,),
    ).fetchone()
    if row is None:
        return {}
    try:
        payload = json.loads(row["payload_json"])
    except (TypeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _payload_with_resolved_mal_id(
    payload: dict[str, Any],
    anilist_id: object,
    resolver: MalIdResolver | None,
) -> dict[str, Any]:
    if _mal_id_from_payload(payload) or resolver is None:
        return payload
    parsed_anilist_id = _int_or_none(anilist_id)
    if parsed_anilist_id is None:
        return payload
    try:
        resolved = resolver(parsed_anilist_id)
    except Exception:
        return payload
    resolved_payload: dict[str, Any]
    if isinstance(resolved, dict):
        resolved_payload = resolved
    else:
        resolved_id = _int_or_none(resolved)
        resolved_payload = {"idMal": resolved_id} if resolved_id else {}
    if not resolved_payload:
        return payload
    merged = dict(payload)
    for key, value in resolved_payload.items():
        if key == "idMal" or merged.get(key) in (None, "", [], {}):
            merged[key] = value
    return merged


def _mal_id_from_payload(payload: dict[str, Any]) -> int | None:
    direct = _int_or_none(payload.get("idMal"))
    if direct:
        return direct
    for link in payload.get("externalLinks") or []:
        if not isinstance(link, dict):
            continue
        site = str(link.get("site") or "").casefold()
        url = str(link.get("url") or "")
        if "myanimelist" in site or "myanimelist.net" in url:
            match = re.search(r"/anime/(\d+)", url)
            if match:
                return int(match.group(1))
    return None


def _mal_id_for_anime(conn: sqlite3.Connection, anime_id: int, payload: dict[str, Any]) -> int | None:
    payload_id = _mal_id_from_payload(payload)
    if payload_id:
        return payload_id
    row = conn.execute(
        """
        SELECT provider_media_id FROM metadata_matches
        WHERE anime_id = ? AND provider = 'myanimelist' AND selected = 1
        ORDER BY confidence_score DESC, id
        LIMIT 1
        """,
        (anime_id,),
    ).fetchone()
    return _int_or_none(row["provider_media_id"]) if row is not None else None


def _payload_value(payload: dict[str, Any], key: str) -> Any:
    return payload.get(key) if isinstance(payload, dict) else None


def _mal_series_type(payload: dict[str, Any]) -> str:
    value = str(payload.get("format") or "TV").replace("_", " ").strip().upper()
    return {
        "TV SHORT": "TV",
        "MOVIE": "Movie",
        "SPECIAL": "Special",
        "OVA": "OVA",
        "ONA": "ONA",
        "MUSIC": "Music",
    }.get(value, "TV")


def _first_episode_date(conn: sqlite3.Connection, anime_id: int) -> str | None:
    row = conn.execute(
        """
        SELECT COALESCE(MIN(first_started_at), MIN(watched_at)) AS started_at
        FROM episodes
        WHERE anime_id = ? AND (first_started_at IS NOT NULL OR watched_at IS NOT NULL)
        """,
        (anime_id,),
    ).fetchone()
    return _date_only(row["started_at"]) if row is not None else None


def _date_only(value: object) -> str | None:
    text = str(value or "").strip()
    if not text or text.startswith("0000-00-00"):
        return None
    return text[:10]


def _date_to_iso(value: object) -> str | None:
    date = _date_only(value)
    if date is None:
        return None
    return f"{date}T00:00:00+00:00"


def _int_or_zero(value: object) -> int:
    parsed = _int_or_none(value)
    return parsed if parsed is not None else 0


def _int_or_none(value: object) -> int | None:
    try:
        text = str(value).strip()
        if text == "":
            return None
        return int(float(text))
    except (TypeError, ValueError):
        return None


def _local_status_from_mal(value: object) -> str:
    status = LOCAL_STATUS_BY_MAL.get(str(value or "").strip().casefold())
    return status if status in STATUSES else "watching"
