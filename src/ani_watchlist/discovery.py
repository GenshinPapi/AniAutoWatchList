from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, time, timedelta, timezone
from typing import Any

from .config import AppConfig, load_config
from .providers.anilist import AniListProvider, display_title_from_media
from .store import now_iso


TRENDING_CACHE_KEY = "anilist_trending_v3"
TOP_AIRING_CACHE_KEY = "anilist_top_airing_v3"
POPULAR_CACHE_KEY = "anilist_popular_v7"
SCHEDULE_CACHE_KEY = "anilist_schedule_week_v2"
DISCOVERY_CACHE_KEYS = (TRENDING_CACHE_KEY, TOP_AIRING_CACHE_KEY, POPULAR_CACHE_KEY, SCHEDULE_CACHE_KEY)
MEDIA_LIST_BATCH_PAGES = 2
MEDIA_LIST_PAGE_SIZE = 50
MEDIA_LIST_BATCH_LIMIT = MEDIA_LIST_BATCH_PAGES * MEDIA_LIST_PAGE_SIZE
POPULAR_GENRE_ALL_LABEL = "All genres"
POPULAR_GENRES = (
    "Action",
    "Adventure",
    "Comedy",
    "Drama",
    "Ecchi",
    "Fantasy",
    "Hentai",
    "Horror",
    "Mahou Shoujo",
    "Mecha",
    "Music",
    "Mystery",
    "Psychological",
    "Romance",
    "Sci-Fi",
    "Slice of Life",
    "Sports",
    "Supernatural",
    "Thriller",
)
POPULAR_TAGS = (
    "Isekai",
    "Harem",
    "Shounen",
    "Seinen",
    "Shoujo",
    "Josei",
    "Iyashikei",
    "Cute Girls Doing Cute Things",
    "Boys' Love",
    "Girls' Love",
    "Aliens",
    "Animals",
    "Assassins",
    "Battle Royale",
    "Body Horror",
    "Crime",
    "Cyberpunk",
    "Demons",
    "Dragons",
    "Dungeon",
    "Ghost",
    "Gods",
    "Guns",
    "Historical",
    "Idol",
    "Kaiju",
    "Martial Arts",
    "Military",
    "Monster Girl",
    "Ninja",
    "Pirates",
    "Post-Apocalyptic",
    "Robots",
    "Samurai",
    "School",
    "Space",
    "Super Power",
    "Survival",
    "Time Manipulation",
    "Vampire",
    "Video Games",
    "Virtual World",
    "War",
    "Witch",
    "Yandere",
    "Zombie",
)
POPULAR_FILTERS = (*POPULAR_GENRES, *POPULAR_TAGS)
RELATION_LABELS = {
    "PREQUEL": "Prequel",
    "SEQUEL": "Sequel",
    "PARENT": "Parent",
    "SIDE_STORY": "Side Story",
    "SPIN_OFF": "Spin-Off",
}
EMPTY_MEDIA_LIST = {
    "items": [],
    "error": None,
    "fetched_at": None,
    "next_page": None,
    "has_more": False,
}


def normalize_popular_filter(value: str | None) -> tuple[str | None, str | None]:
    cleaned = str(value or "").strip()
    if not cleaned or cleaned.casefold() == POPULAR_GENRE_ALL_LABEL.casefold():
        return None, None
    for genre in POPULAR_GENRES:
        if cleaned.casefold() == genre.casefold():
            return "genre", genre
    for tag in POPULAR_TAGS:
        if cleaned.casefold() == tag.casefold():
            return "tag", tag
    return "tag", cleaned


def normalize_popular_genre(genre: str | None) -> str | None:
    filter_type, filter_value = normalize_popular_filter(genre)
    return filter_value if filter_type == "genre" else None


def popular_filter_label(value: str | None) -> str | None:
    return normalize_popular_filter(value)[1]


def popular_cache_key(filter_value: str | None = None) -> str:
    filter_type, normalized = normalize_popular_filter(filter_value)
    if filter_type is None or normalized is None:
        return POPULAR_CACHE_KEY
    slug = re.sub(r"[^0-9a-z]+", "_", normalized.casefold()).strip("_")
    return f"{POPULAR_CACHE_KEY}_{filter_type}_{slug or 'filter'}"


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


def _metadata_payload(media: dict[str, Any]) -> dict[str, Any]:
    return {
        key: media.get(key)
        for key in (
            "id",
            "title",
            "synonyms",
            "episodes",
            "status",
            "format",
            "isAdult",
            "season",
            "seasonYear",
            "coverImage",
            "siteUrl",
            "nextAiringEpisode",
        )
        if key in media
    }


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
        "metadata_payload": _metadata_payload(media),
        "relation_type": media.get("relationType"),
        "relation_label": RELATION_LABELS.get(str(media.get("relationType") or "")),
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


def load_discovery(conn: sqlite3.Connection, *, popular_genre: str | None = None) -> dict[str, Any]:
    trending = load_cache(conn, TRENDING_CACHE_KEY) or dict(EMPTY_MEDIA_LIST)
    top_airing = load_cache(conn, TOP_AIRING_CACHE_KEY) or dict(EMPTY_MEDIA_LIST)
    popular_key = popular_cache_key(popular_genre)
    filter_type, filter_value = normalize_popular_filter(popular_genre)
    popular = load_cache(conn, popular_key) or (
        dict(EMPTY_MEDIA_LIST)
        | {
            "filter_type": filter_type,
            "filter": filter_value,
            "genre": filter_value if filter_type == "genre" else None,
            "tag": filter_value if filter_type == "tag" else None,
        }
    )
    schedule = load_cache(conn, SCHEDULE_CACHE_KEY) or dict(EMPTY_MEDIA_LIST)
    return {
        "trending": trending,
        "top_airing": top_airing,
        "popular": popular,
        "schedule": schedule,
        "trending_fresh": cache_fetched_today(conn, TRENDING_CACHE_KEY),
        "top_airing_fresh": cache_fetched_today(conn, TOP_AIRING_CACHE_KEY),
        "popular_fresh": cache_fetched_today(conn, popular_key),
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
    extra_payload: dict[str, Any] | None = None,
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
        payload.update(extra_payload or {})
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
        payload.update(extra_payload or {})
    except Exception as exc:
        existing = load_cache(conn, cache_key) or {"items": [], "fetched_at": None}
        payload = {**existing, "error": str(exc)}
    set_cache(conn, cache_key, payload)
    return payload


def search_media(
    query: str,
    config: AppConfig | None = None,
    *,
    provider: AniListProvider | None = None,
    limit: int = 50,
    cache_covers: bool = True,
) -> dict[str, Any]:
    cleaned_query = str(query or "").strip()
    if not cleaned_query:
        return {"items": [], "error": None, "fetched_at": now_iso(), "query": cleaned_query}
    config = config or load_config()
    if not config.anilist.enabled:
        return {
            "items": [],
            "error": "AniList metadata is disabled.",
            "fetched_at": now_iso(),
            "query": cleaned_query,
        }
    provider = provider or AniListProvider(config.anilist)
    try:
        items = [
            _normalize_media(item, provider if cache_covers else None)
            for item in provider.search_anime_media(cleaned_query, limit=limit)
        ]
        return {"items": items, "error": None, "fetched_at": now_iso(), "query": cleaned_query}
    except Exception as exc:
        return {"items": [], "error": str(exc), "fetched_at": now_iso(), "query": cleaned_query}


def related_media(
    media_id: str | int | None,
    config: AppConfig | None = None,
    *,
    provider: AniListProvider | None = None,
    cache_covers: bool = True,
) -> dict[str, Any]:
    cleaned_id = str(media_id or "").strip()
    if not cleaned_id:
        return {"items": [], "error": None, "fetched_at": now_iso(), "media_id": cleaned_id}
    config = config or load_config()
    if not config.anilist.enabled:
        return {
            "items": [],
            "error": "AniList metadata is disabled.",
            "fetched_at": now_iso(),
            "media_id": cleaned_id,
        }
    provider = provider or AniListProvider(config.anilist)
    try:
        items = [
            _normalize_media(item, provider if cache_covers else None)
            for item in provider.get_related_anime(cleaned_id)
        ]
        items.sort(
            key=lambda item: (
                item.get("season_year") is None,
                item.get("season_year") or 9999,
                str(item.get("display_title") or ""),
            )
        )
        return {"items": items, "error": None, "fetched_at": now_iso(), "media_id": cleaned_id}
    except Exception as exc:
        return {"items": [], "error": str(exc), "fetched_at": now_iso(), "media_id": cleaned_id}


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


def _popular_media_batch(filter_value: str | None = None):
    filter_type, normalized = normalize_popular_filter(filter_value)

    def fetch(provider: AniListProvider, start_page: int, batch_pages: int, per_page: int) -> dict[str, Any]:
        return provider.get_popular_anime_batch(
            start_page=start_page,
            page_count=batch_pages,
            per_page=per_page,
            genre=normalized if filter_type == "genre" else None,
            tag=normalized if filter_type == "tag" else None,
        )

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
    genre: str | None = None,
) -> dict[str, Any]:
    filter_type, filter_value = normalize_popular_filter(genre)
    batch_pages, per_page = _batch_params_for_limit(limit)
    return refresh_media_list(
        conn,
        popular_cache_key(filter_value),
        _popular_media_batch(filter_value),
        config,
        force=force,
        provider=provider,
        batch_pages=batch_pages,
        per_page=per_page,
        extra_payload={
            "filter_type": filter_type,
            "filter": filter_value,
            "genre": filter_value if filter_type == "genre" else None,
            "tag": filter_value if filter_type == "tag" else None,
        },
    )


MEDIA_PAGE_CONFIG = {
    "trending": (TRENDING_CACHE_KEY, _media_batch("get_trending_anime_batch")),
    "top_airing": (TOP_AIRING_CACHE_KEY, _media_batch("get_top_airing_anime_batch")),
}


def append_discovery_media_page(
    conn: sqlite3.Connection,
    page_name: str,
    config: AppConfig | None = None,
    *,
    provider: AniListProvider | None = None,
    genre: str | None = None,
) -> dict[str, Any]:
    if page_name == "popular":
        filter_type, filter_value = normalize_popular_filter(genre)
        return append_media_list(
            conn,
            popular_cache_key(filter_value if filter_type is not None else None),
            _popular_media_batch(filter_value if filter_type is not None else None),
            config,
            provider=provider,
        )
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
    popular_genre: str | None = None,
) -> dict[str, Any]:
    config = config or load_config()
    provider = provider or (AniListProvider(config.anilist) if config.anilist.enabled else None)
    trending = refresh_trending(conn, config, force=force, provider=provider)
    top_airing = refresh_top_airing(conn, config, force=force, provider=provider)
    popular = refresh_popular(conn, config, force=force, provider=provider, genre=popular_genre)
    schedule = refresh_schedule(conn, config, force=force, provider=provider)
    return {"trending": trending, "top_airing": top_airing, "popular": popular, "schedule": schedule}
