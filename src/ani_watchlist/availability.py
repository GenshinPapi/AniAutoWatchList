from __future__ import annotations

import sqlite3
from typing import Any

from .launcher import allanime_available_episode_keys
from .metadata import selected_metadata_payload
from .store import get_anime_by_id, update_anime_fields, upsert_episodes


def _int_or_none(value: object) -> int | None:
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def refresh_available_episodes_for_anime(
    conn: sqlite3.Connection,
    anime_id: int,
    *,
    mode: str = "sub",
) -> dict[str, Any]:
    anime = get_anime_by_id(conn, anime_id)
    if anime is None:
        raise KeyError(f"anime id not found: {anime_id}")
    metadata_payload = selected_metadata_payload(conn, anime_id)
    availability = allanime_available_episode_keys(
        anime["display_title"],
        anime["source_title"],
        metadata_payload,
        total_episodes=_int_or_none(anime["total_episodes"]),
        mode=mode,
    )
    if availability is None:
        return {
            "anime_id": anime_id,
            "episode_count": None,
            "target_title": None,
            "target_id": None,
            "updated": False,
        }
    if availability.episode_keys:
        rows = upsert_episodes(conn, anime_id, availability.episode_keys, source_label=f"allanime-{mode}")
        episode_count = len(rows)
    else:
        episode_count = availability.target.episode_count
        update_anime_fields(conn, anime_id, available_episode_count=episode_count)
    return {
        "anime_id": anime_id,
        "episode_count": episode_count,
        "target_title": availability.target.title,
        "target_id": availability.target.show_id,
        "updated": True,
    }
