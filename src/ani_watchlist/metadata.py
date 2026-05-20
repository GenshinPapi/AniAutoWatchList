from __future__ import annotations

import json
import sqlite3
from typing import Any

from .config import AppConfig
from .paths import get_paths
from .providers.anilist import AniListProvider, search_title_variants, title_with_content_labels
from .providers.base import MetadataSearchResult
from .store import canonicalize_title, get_anime_by_id, now_iso, update_anime_fields


def _cover_url(payload: dict[str, Any]) -> str | None:
    cover = payload.get("coverImage") or {}
    return cover.get("extraLarge") or cover.get("large") or cover.get("medium")


def _display_title(payload: dict[str, Any], current_title: str | None = None) -> str | None:
    title = payload.get("title") or {}
    english = (title.get("english") or "").strip()
    romaji = (title.get("romaji") or "").strip()
    preferred = (title.get("userPreferred") or "").strip()
    native = (title.get("native") or "").strip()
    if english and romaji and english.casefold() != romaji.casefold():
        return title_with_content_labels(f"{english} ({romaji})", payload)
    if english:
        return title_with_content_labels(english, payload)
    if current_title and current_title != "Unknown title":
        labeled_current = title_with_content_labels(current_title, payload)
        return labeled_current if labeled_current != current_title else None
    fallback = preferred or romaji or native or None
    return title_with_content_labels(fallback, payload) if fallback else None


def _display_title_exact_match(match: MetadataSearchResult, title: str) -> bool:
    display_title = _display_title(match.payload) or match.title
    display_canonical = canonicalize_title(display_title)
    return any(display_canonical == canonicalize_title(variant) for variant in search_title_variants(title))


def placeholder_cover_path() -> str:
    cover_dir = get_paths().cover_dir
    cover_dir.mkdir(parents=True, exist_ok=True)
    dest = cover_dir / "placeholder.png"
    if dest.exists():
        return str(dest)
    try:
        from PIL import Image, ImageDraw

        image = Image.new("RGB", (320, 460), "#2f3136")
        draw = ImageDraw.Draw(image)
        draw.rectangle((14, 14, 306, 446), outline="#8f9aa6", width=3)
        draw.text((104, 214), "No Cover", fill="#f2f4f8")
        image.save(dest)
    except Exception:
        dest.write_bytes(
            b"P3\n2 2\n255\n47 49 54 47 49 54\n47 49 54 47 49 54\n"
        )
    return str(dest)


def _selected_match(conn: sqlite3.Connection, anime_id: int, provider: str = "anilist") -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT * FROM metadata_matches
        WHERE anime_id = ? AND provider = ? AND selected = 1
        ORDER BY confidence_score DESC, id
        LIMIT 1
        """,
        (anime_id, provider),
    ).fetchone()


def selected_metadata_payload(conn: sqlite3.Connection, anime_id: int, provider: str = "anilist") -> dict[str, Any] | None:
    row = _selected_match(conn, anime_id, provider)
    if row is None:
        return None
    try:
        payload = json.loads(row["payload_json"])
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _apply_selected_match(
    conn: sqlite3.Connection,
    anime_id: int,
    row: sqlite3.Row,
    provider: AniListProvider,
) -> None:
    payload = json.loads(row["payload_json"])
    cover_path = None
    if _cover_url(payload):
        try:
            cover_path = provider.cache_cover(row["provider_media_id"], _cover_url(payload))
        except Exception:
            cover_path = placeholder_cover_path()
    apply_selected_metadata(conn, anime_id, row["provider_media_id"], payload, cover_path)


def store_matches(conn: sqlite3.Connection, anime_id: int, matches: list[MetadataSearchResult]) -> None:
    ts = now_iso()
    selected_ids = {
        str(row["provider_media_id"])
        for row in conn.execute(
            "SELECT provider_media_id FROM metadata_matches WHERE anime_id = ? AND provider = 'anilist' AND selected = 1",
            (anime_id,),
        )
    }
    with conn:
        if not selected_ids:
            conn.execute("DELETE FROM metadata_matches WHERE anime_id = ? AND provider = 'anilist'", (anime_id,))
        for match in matches:
            conn.execute(
                """
                INSERT INTO metadata_matches(
                    anime_id, provider, provider_media_id, confidence_score, selected, payload_json, created_at
                ) VALUES (?, ?, ?, ?, 0, ?, ?)
                ON CONFLICT(anime_id, provider, provider_media_id) DO UPDATE SET
                    confidence_score = excluded.confidence_score,
                    payload_json = excluded.payload_json
                """,
                (
                    anime_id,
                    match.provider,
                    match.media_id,
                    match.confidence_score,
                    json.dumps(match.payload, sort_keys=True),
                    ts,
                ),
            )


def apply_selected_metadata(
    conn: sqlite3.Connection,
    anime_id: int,
    provider_media_id: str,
    payload: dict[str, Any],
    cover_path: str | None = None,
) -> None:
    anime = get_anime_by_id(conn, anime_id)
    fields: dict[str, Any] = {
        "anilist_id": int(provider_media_id),
        "total_episodes": payload.get("episodes"),
        "cover_url": _cover_url(payload),
    }
    title = _display_title(payload, anime["display_title"] if anime is not None else None)
    if title:
        fields["display_title"] = title
    if cover_path:
        fields["cover_path"] = cover_path
    update_anime_fields(conn, anime_id, **fields)


def store_selected_metadata_payload(
    conn: sqlite3.Connection,
    anime_id: int,
    provider_media_id: str | int | None,
    payload: dict[str, Any],
    cover_path: str | None = None,
) -> None:
    media_id = str(provider_media_id or payload.get("id") or "").strip()
    if not media_id:
        return
    ts = now_iso()
    with conn:
        conn.execute(
            """
            INSERT INTO metadata_matches(
                anime_id, provider, provider_media_id, confidence_score, selected, payload_json, created_at
            ) VALUES (?, 'anilist', ?, 1.0, 1, ?, ?)
            ON CONFLICT(anime_id, provider, provider_media_id) DO UPDATE SET
                confidence_score = 1.0,
                selected = 1,
                payload_json = excluded.payload_json
            """,
            (anime_id, media_id, json.dumps(payload, sort_keys=True), ts),
        )
        conn.execute(
            "UPDATE metadata_matches SET selected = 0 WHERE anime_id = ? AND provider = 'anilist' AND provider_media_id != ?",
            (anime_id, media_id),
        )
    apply_selected_metadata(conn, anime_id, media_id, payload, cover_path)


def search_and_store_matches(
    conn: sqlite3.Connection,
    anime_id: int,
    title: str,
    config: AppConfig,
    provider: AniListProvider | None = None,
) -> list[MetadataSearchResult]:
    if not config.anilist.enabled:
        return []
    provider = provider or AniListProvider(config.anilist)
    matches = provider.search_title(title)
    store_matches(conn, anime_id, matches)
    if not matches:
        return []

    anime = get_anime_by_id(conn, anime_id)
    if anime is None:
        return matches
    if anime["anilist_id"]:
        return matches
    selected = _selected_match(conn, anime_id)
    if selected is not None:
        _apply_selected_match(conn, anime_id, selected, provider)
        return matches

    best = matches[0]
    close = [match for match in matches if best.confidence_score - match.confidence_score < 0.03]
    exact_display_match = _display_title_exact_match(best, title)
    if best.confidence_score >= config.metadata.auto_link_confidence and (len(close) == 1 or exact_display_match):
        cover_path = None
        try:
            cover_path = provider.cache_cover(best.media_id, _cover_url(best.payload)) if _cover_url(best.payload) else None
        except Exception:
            cover_path = placeholder_cover_path()
        with conn:
            conn.execute(
                "UPDATE metadata_matches SET selected = CASE WHEN provider_media_id = ? THEN 1 ELSE 0 END WHERE anime_id = ? AND provider = ?",
                (best.media_id, anime_id, best.provider),
            )
        apply_selected_metadata(conn, anime_id, best.media_id, best.payload, cover_path)
    return matches


def refresh_metadata_for_anime(
    conn: sqlite3.Connection,
    anime_id: int,
    config: AppConfig,
    provider: AniListProvider | None = None,
) -> list[MetadataSearchResult]:
    anime = get_anime_by_id(conn, anime_id)
    if anime is None:
        raise KeyError(f"anime id not found: {anime_id}")
    provider = provider or AniListProvider(config.anilist)
    if anime["anilist_id"]:
        media_id = str(anime["anilist_id"])
        media = provider.get_media(media_id)
        cover_path = None
        if _cover_url(media) and not anime["cover_path"]:
            try:
                cover_path = provider.cache_cover(media_id, _cover_url(media))
            except Exception:
                cover_path = placeholder_cover_path()
        ts = now_iso()
        with conn:
            conn.execute(
                """
                INSERT INTO metadata_matches(
                    anime_id, provider, provider_media_id, confidence_score, selected, payload_json, created_at
                ) VALUES (?, 'anilist', ?, 1.0, 1, ?, ?)
                ON CONFLICT(anime_id, provider, provider_media_id) DO UPDATE SET
                    selected = 1,
                    payload_json = excluded.payload_json
                """,
                (anime_id, media_id, json.dumps(media, sort_keys=True), ts),
            )
            conn.execute(
                "UPDATE metadata_matches SET selected = 0 WHERE anime_id = ? AND provider = 'anilist' AND provider_media_id != ?",
                (anime_id, media_id),
            )
        apply_selected_metadata(conn, anime_id, media_id, media, cover_path)
        return [
            MetadataSearchResult(
                provider="anilist",
                media_id=media_id,
                title=_display_title(media) or anime["display_title"],
                confidence_score=1.0,
                payload=media,
            )
        ]
    return search_and_store_matches(conn, anime_id, anime["display_title"], config, provider)


def select_match(conn: sqlite3.Connection, anime_id: int, match_id: int, provider: AniListProvider | None = None) -> None:
    row = conn.execute(
        "SELECT * FROM metadata_matches WHERE id = ? AND anime_id = ?",
        (match_id, anime_id),
    ).fetchone()
    if row is None:
        raise KeyError(f"metadata match not found: {match_id}")
    payload = json.loads(row["payload_json"])
    cover_path = None
    if provider is not None and _cover_url(payload):
        try:
            cover_path = provider.cache_cover(row["provider_media_id"], _cover_url(payload))
        except Exception:
            cover_path = placeholder_cover_path()
    with conn:
        conn.execute(
            "UPDATE metadata_matches SET selected = 0 WHERE anime_id = ? AND provider = ?",
            (anime_id, row["provider"]),
        )
        conn.execute("UPDATE metadata_matches SET selected = 1 WHERE id = ?", (match_id,))
    apply_selected_metadata(conn, anime_id, row["provider_media_id"], payload, cover_path)


def set_anilist_id(
    conn: sqlite3.Connection,
    anime_id: int,
    anilist_id: int,
    provider: AniListProvider | None = None,
) -> None:
    provider = provider or AniListProvider()
    media = provider.get_media(str(anilist_id))
    cover_path = None
    if _cover_url(media):
        try:
            cover_path = provider.cache_cover(str(anilist_id), _cover_url(media))
        except Exception:
            cover_path = placeholder_cover_path()
    ts = now_iso()
    with conn:
        conn.execute(
            """
            INSERT INTO metadata_matches(
                anime_id, provider, provider_media_id, confidence_score, selected, payload_json, created_at
            ) VALUES (?, 'anilist', ?, 1.0, 1, ?, ?)
            ON CONFLICT(anime_id, provider, provider_media_id) DO UPDATE SET
                confidence_score = 1.0,
                selected = 1,
                payload_json = excluded.payload_json
            """,
            (anime_id, str(anilist_id), json.dumps(media, sort_keys=True), ts),
        )
        conn.execute(
            "UPDATE metadata_matches SET selected = 0 WHERE anime_id = ? AND provider = 'anilist' AND provider_media_id != ?",
            (anime_id, str(anilist_id)),
        )
    apply_selected_metadata(conn, anime_id, str(anilist_id), media, cover_path)
