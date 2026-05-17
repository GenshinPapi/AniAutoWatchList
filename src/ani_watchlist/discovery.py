from __future__ import annotations

import json
import sqlite3
from datetime import datetime, time, timedelta, timezone
from typing import Any

from .config import AppConfig, load_config
from .providers.anilist import AniListProvider, display_title_from_media
from .store import now_iso


TRENDING_CACHE_KEY = "anilist_trending_v3"
TOP_AIRING_CACHE_KEY = "anilist_top_airing_v3"
POPULAR_CACHE_KEY = "anilist_popular_v5"
SCHEDULE_CACHE_KEY = "anilist_schedule_week_v2"
DISCOVERY_CACHE_KEYS = (TRENDING_CACHE_KEY, TOP_AIRING_CACHE_KEY, POPULAR_CACHE_KEY, SCHEDULE_CACHE_KEY)
MEDIA_LIST_BATCH_PAGES = 2
MEDIA_LIST_PAGE_SIZE = 50
MEDIA_LIST_BATCH_LIMIT = MEDIA_LIST_BATCH_PAGES * MEDIA_LIST_PAGE_SIZE
EMPTY_MEDIA_LIST = {
    "items": [],
    "error": None,
    "fetched_at": None,
    "next_page": None,
    "has_more": False,
}


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _today_local() -> str:
    return datetime.now().date().isoformat()


def _next_local_midnight_iso() -> str:
    tomorrow = datetime.combine(datetime.now().date() + timedelta(days=1), time.min)
    return tomorrow.astimezone().isoformat(timespec="seconds")


def _local_midnight_timestamp(offset_days: int = 0) -> int:
    day = datetime.now().date() + timedelta(days=offset_days)
    local_midnight = datetime.combine(day, time.min).astimezone()
    return int(local_midnight.timestamp())


def _cover_url(media: dict[str, Any]) -> str | None:
    cover = media.get("coverImage") or {}
    return cover.get("extraLarge") or cover.get("large") or cover.get("medium")


def _normalize_media(media: dict[str, Any], provider: AniListProvider | None = None) -> dict[str, Any]:
    media_id = str(media.get("id") or media.get("mediaId") or "")
    cover_url = _cover_url(media)
    cover_path = None
    if provider is not None and media_id and cover_url:
        try:
            cover_path = provider.cache_cover(media_id, str(cover_url))
        except Exception:
            cover_path = None
    title = media.get("title") or {}
    return {
        "id": int(media_id) if media_id.isdigit() else media_id,
        "display_title": display_title_from_media(media),
        "english_title": title.get("english"),
        "romaji_title": title.get("romaji"),
        "native_title": title.get("native"),
        "episodes": media.get("episodes"),
        "status": media.get("status"),
        "format": media.get("format"),
        "is_adult": media.get("isAdult"),
        "season": media.get("season"),
        "season_year": media.get("seasonYear"),
        "average_score": media.get("averageScore"),
        "popularity": media.get("popularity"),
        "trending": media.get("trending"),
        "cover_url": cover_url,
        "cover_path": cover_path,
        "banner_image": media.get("bannerImage"),
        "site_url": media.get("siteUrl"),
        "next_airing_episode": media.get("nextAiringEpisode"),
    }


def _normalize_schedule_item(item: dict[str, Any], provider: AniListProvider | None = None) -> dict[str, Any]:
    media = item.get("media") or {}
    normalized_media = _normalize_media(media, provider)
    airing_at = item.get("airingAt")
    local_day = "-"
    local_time = "-"
    if airing_at is not None:
        parsed = datetime.fromtimestamp(int(airing_at), tz=timezone.utc).astimezone()
        local_day = parsed.date().isoformat()
        local_time = parsed.strftime("%H:%M")
    return {
        "id": item.get("id"),
        "airing_at": airing_at,
        "time_until_airing": item.get("timeUntilAiring"),
        "episode": item.get("episode"),
        "media_id": item.get("mediaId") or normalized_media.get("id"),
        "local_day": local_day,
        "local_time": local_time,
        "media": normalized_media,
    }


def cache_row(conn: sqlite3.Connection, cache_key: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM discovery_cache WHERE cache_key = ?", (cache_key,)).fetchone()


def load_cache(conn: sqlite3.Connection, cache_key: str) -> dict[str, Any] | None:
    row = cache_row(conn, cache_key)
    if row is None:
        return None
    try:
        return json.loads(row["payload_json"])
    except json.JSONDecodeError:
        return None


def cache_fetched_today(conn: sqlite3.Connection, cache_key: str) -> bool:
    row = cache_row(conn, cache_key)
    if row is None:
        return False
    parsed = _parse_iso(row["fetched_at"])
    if parsed is None:
        return False
    return parsed.astimezone().date().isoformat() == _today_local()


def set_cache(conn: sqlite3.Connection, cache_key: str, payload: dict[str, Any]) -> None:
    ts = now_iso()
    with conn:
        conn.execute(
            """
            INSERT INTO discovery_cache(cache_key, payload_json, fetched_at, expires_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(cache_key) DO UPDATE SET
                payload_json = excluded.payload_json,
                fetched_at = excluded.fetched_at,
                expires_at = excluded.expires_at
            """,
            (cache_key, json.dumps(payload, sort_keys=True), ts, _next_local_midnight_iso()),
        )


def load_discovery(conn: sqlite3.Connection) -> dict[str, Any]:
    trending = load_cache(conn, TRENDING_CACHE_KEY) or dict(EMPTY_MEDIA_LIST)
    top_airing = load_cache(conn, TOP_AIRING_CACHE_KEY) or dict(EMPTY_MEDIA_LIST)
    popular = load_cache(conn, POPULAR_CACHE_KEY) or dict(EMPTY_MEDIA_LIST)
    schedule = load_cache(conn, SCHEDULE_CACHE_KEY) or dict(EMPTY_MEDIA_LIST)
    return {
        "trending": trending,
        "top_airing": top_airing,
        "popular": popular,
        "schedule": schedule,
        "trending_fresh": cache_fetched_today(conn, TRENDING_CACHE_KEY),
        "top_airing_fresh": cache_fetched_today(conn, TOP_AIRING_CACHE_KEY),
        "popular_fresh": cache_fetched_today(conn, POPULAR_CACHE_KEY),
        "schedule_fresh": cache_fetched_today(conn, SCHEDULE_CACHE_KEY),
    }


def refresh_media_list(
    conn: sqlite3.Connection,
    cache_key: str,
    fetch_batch,
    config: AppConfig | None = None,
    *,
    force: bool = False,
    provider: AniListProvider | None = None,
    start_page: int = 1,
    batch_pages: int = MEDIA_LIST_BATCH_PAGES,
    per_page: int = MEDIA_LIST_PAGE_SIZE,
) -> dict[str, Any]:
    config = config or load_config()
    if not config.anilist.enabled:
        payload = {
            "items": [],
            "error": "AniList metadata is disabled.",
            "fetched_at": now_iso(),
            "next_page": None,
            "has_more": False,
        }
        set_cache(conn, cache_key, payload)
        return payload
    if not force and cache_fetched_today(conn, cache_key):
        return load_cache(conn, cache_key) or dict(EMPTY_MEDIA_LIST)
    provider = provider or AniListProvider(config.anilist)
    try:
        batch = fetch_batch(provider, start_page, batch_pages, per_page)
        next_page = batch.get("next_page")
        items = [_normalize_media(item, provider) for item in batch.get("items") or []]
        payload = {
            "items": items,
            "error": None,
            "fetched_at": now_iso(),
            "next_page": next_page,
            "has_more": next_page is not None,
        }
    except Exception as exc:
        existing = load_cache(conn, cache_key) or {"items": [], "fetched_at": None}
        payload = {**existing, "error": str(exc)}
    set_cache(conn, cache_key, payload)
    return payload


def append_media_list(
    conn: sqlite3.Connection,
    cache_key: str,
    fetch_batch,
    config: AppConfig | None = None,
    *,
    provider: AniListProvider | None = None,
    batch_pages: int = MEDIA_LIST_BATCH_PAGES,
    per_page: int = MEDIA_LIST_PAGE_SIZE,
) -> dict[str, Any]:
    config = config or load_config()
    existing = load_cache(conn, cache_key) or dict(EMPTY_MEDIA_LIST)
    if not config.anilist.enabled:
        payload = {**existing, "error": "AniList metadata is disabled.", "has_more": False, "next_page": None}
        set_cache(conn, cache_key, payload)
        return payload
    next_page = existing.get("next_page")
    if next_page is None:
        return {**existing, "has_more": False, "next_page": None}
    provider = provider or AniListProvider(config.anilist)
    try:
        batch = fetch_batch(provider, int(next_page), batch_pages, per_page)
        current_items = list(existing.get("items") or [])
        seen_ids = {str(item.get("id")) for item in current_items if isinstance(item, dict) and item.get("id") is not None}
        for item in batch.get("items") or []:
            normalized = _normalize_media(item, provider)
            media_id = str(normalized.get("id"))
            if media_id in seen_ids:
                continue
            current_items.append(normalized)
            seen_ids.add(media_id)
        next_page = batch.get("next_page")
        payload = {
            **existing,
            "items": current_items,
            "error": None,
            "fetched_at": now_iso(),
            "next_page": next_page,
            "has_more": next_page is not None,
        }
    except Exception as exc:
        payload = {**existing, "error": str(exc)}
    set_cache(conn, cache_key, payload)
    return payload


def _media_batch(method_name: str):
    def fetch(provider: AniListProvider, start_page: int, batch_pages: int, per_page: int) -> dict[str, Any]:
        method = getattr(provider, method_name)
        return method(start_page=start_page, page_count=batch_pages, per_page=per_page)

    return fetch


def _batch_params_for_limit(limit: int | None) -> tuple[int, int]:
    target = max(1, int(limit or MEDIA_LIST_BATCH_LIMIT))
    for per_page in range(min(MEDIA_LIST_PAGE_SIZE, target), 0, -1):
        if target % per_page == 0:
            return target // per_page, per_page
    return target, 1


def refresh_trending(
    conn: sqlite3.Connection,
    config: AppConfig | None = None,
    *,
    force: bool = False,
    provider: AniListProvider | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    batch_pages, per_page = _batch_params_for_limit(limit)
    return refresh_media_list(
        conn,
        TRENDING_CACHE_KEY,
        _media_batch("get_trending_anime_batch"),
        config,
        force=force,
        provider=provider,
        batch_pages=batch_pages,
        per_page=per_page,
    )


def refresh_top_airing(
    conn: sqlite3.Connection,
    config: AppConfig | None = None,
    *,
    force: bool = False,
    provider: AniListProvider | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    batch_pages, per_page = _batch_params_for_limit(limit)
    return refresh_media_list(
        conn,
        TOP_AIRING_CACHE_KEY,
        _media_batch("get_top_airing_anime_batch"),
        config,
        force=force,
        provider=provider,
        batch_pages=batch_pages,
        per_page=per_page,
    )


def refresh_popular(
    conn: sqlite3.Connection,
    config: AppConfig | None = None,
    *,
    force: bool = False,
    provider: AniListProvider | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    batch_pages, per_page = _batch_params_for_limit(limit)
    return refresh_media_list(
        conn,
        POPULAR_CACHE_KEY,
        _media_batch("get_popular_anime_batch"),
        config,
        force=force,
        provider=provider,
        batch_pages=batch_pages,
        per_page=per_page,
    )


MEDIA_PAGE_CONFIG = {
    "trending": (TRENDING_CACHE_KEY, _media_batch("get_trending_anime_batch")),
    "top_airing": (TOP_AIRING_CACHE_KEY, _media_batch("get_top_airing_anime_batch")),
    "popular": (POPULAR_CACHE_KEY, _media_batch("get_popular_anime_batch")),
}


def append_discovery_media_page(
    conn: sqlite3.Connection,
    page_name: str,
    config: AppConfig | None = None,
    *,
    provider: AniListProvider | None = None,
) -> dict[str, Any]:
    try:
        cache_key, fetch_batch = MEDIA_PAGE_CONFIG[page_name]
    except KeyError as exc:
        raise ValueError(f"unknown discovery page: {page_name}") from exc
    return append_media_list(conn, cache_key, fetch_batch, config, provider=provider)


def refresh_schedule(
    conn: sqlite3.Connection,
    config: AppConfig | None = None,
    *,
    force: bool = False,
    provider: AniListProvider | None = None,
    days: int = 7,
    limit: int = 140,
) -> dict[str, Any]:
    config = config or load_config()
    start = _local_midnight_timestamp(0)
    end = _local_midnight_timestamp(days)
    if not config.anilist.enabled:
        payload = {
            "items": [],
            "error": "AniList metadata is disabled.",
            "fetched_at": now_iso(),
            "start": start,
            "end": end,
            "days": days,
        }
        set_cache(conn, SCHEDULE_CACHE_KEY, payload)
        return payload
    if not force and cache_fetched_today(conn, SCHEDULE_CACHE_KEY):
        return load_cache(conn, SCHEDULE_CACHE_KEY) or {"items": [], "error": None, "fetched_at": None}
    provider = provider or AniListProvider(config.anilist)
    try:
        items = [
            _normalize_schedule_item(item, provider)
            for item in provider.get_airing_schedule(start, end, limit=limit)
        ]
        items.sort(key=lambda item: (item.get("airing_at") or 0, str((item.get("media") or {}).get("display_title") or "")))
        payload = {"items": items, "error": None, "fetched_at": now_iso(), "start": start, "end": end, "days": days}
    except Exception as exc:
        existing = load_cache(conn, SCHEDULE_CACHE_KEY) or {"items": [], "fetched_at": None, "start": start, "end": end, "days": days}
        payload = {**existing, "error": str(exc)}
    set_cache(conn, SCHEDULE_CACHE_KEY, payload)
    return payload


def refresh_discovery(
    conn: sqlite3.Connection,
    config: AppConfig | None = None,
    *,
    force: bool = False,
    provider: AniListProvider | None = None,
) -> dict[str, Any]:
    config = config or load_config()
    provider = provider or (AniListProvider(config.anilist) if config.anilist.enabled else None)
    trending = refresh_trending(conn, config, force=force, provider=provider)
    top_airing = refresh_top_airing(conn, config, force=force, provider=provider)
    popular = refresh_popular(conn, config, force=force, provider=provider)
    schedule = refresh_schedule(conn, config, force=force, provider=provider)
    return {"trending": trending, "top_airing": top_airing, "popular": popular, "schedule": schedule}
