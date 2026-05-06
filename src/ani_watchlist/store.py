from __future__ import annotations

import json
import re
import shutil
import sqlite3
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Iterable

STATUSES = ("watching", "completed", "dropped", "on_hold", "plan_to_watch")


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def clean_display_title(title: str) -> str:
    title = title.replace("+", " ").strip()
    title = re.sub(r"\s+", " ", title)
    title = re.sub(r"\s*\(\s*\d+(?:\.\d+)?\s+episodes?\s*\)\s*$", "", title, flags=re.I)
    return title.strip() or "Unknown title"


def canonicalize_title(title: str) -> str:
    title = clean_display_title(title).casefold()
    title = title.replace("'", "").replace(chr(0x2019), "")
    title = re.sub(r"[^0-9a-z]+", " ", title)
    return re.sub(r"\s+", " ", title).strip()


def episode_key(value: Any) -> str:
    if isinstance(value, dict):
        for key in ("episode_key", "key", "episode", "episode_number", "number", "id"):
            if value.get(key) is not None:
                return str(value[key]).strip()
        return json.dumps(value, sort_keys=True)
    return str(value).strip()


def episode_number(value: Any) -> str | None:
    if isinstance(value, dict):
        for key in ("episode_number", "number", "episode"):
            if value.get(key) is not None:
                return str(value[key]).strip()
    text = str(value).strip()
    return text or None


def episode_title(value: Any) -> str | None:
    if isinstance(value, dict) and value.get("title") is not None:
        return str(value["title"]).strip() or None
    return None


def get_anime(conn: sqlite3.Connection, title: str) -> sqlite3.Row | None:
    canonical = canonicalize_title(title)
    return conn.execute(
        "SELECT * FROM anime WHERE canonical_title = ? OR source_title = ? OR display_title = ? ORDER BY id LIMIT 1",
        (canonical, title, title),
    ).fetchone()


def get_anime_by_id(conn: sqlite3.Connection, anime_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM anime WHERE id = ?", (anime_id,)).fetchone()


def get_or_create_anime(
    conn: sqlite3.Connection,
    title: str,
    source_title: str | None = None,
    status: str = "watching",
) -> tuple[sqlite3.Row, bool]:
    if status not in STATUSES:
        raise ValueError(f"invalid status: {status}")
    source = source_title or title
    canonical = canonicalize_title(title)
    source_canonical = canonicalize_title(source)
    existing = conn.execute(
        """
        SELECT * FROM anime
        WHERE canonical_title IN (?, ?)
            OR source_title IN (?, ?)
            OR display_title IN (?, ?)
        ORDER BY id
        LIMIT 1
        """,
        (canonical, source_canonical, source, clean_display_title(source), title, clean_display_title(title)),
    ).fetchone()
    if existing is not None:
        return existing, False

    ts = now_iso()
    next_sort = conn.execute("SELECT COALESCE(MAX(sort_order), 0) + 1 AS next FROM anime WHERE status = ?", (status,)).fetchone()[
        "next"
    ]
    with conn:
        cur = conn.execute(
            """
            INSERT INTO anime(
                canonical_title, display_title, source_title, status, sort_order, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (canonical, clean_display_title(title), source, status, int(next_sort), ts, ts),
        )
    return get_anime_by_id(conn, int(cur.lastrowid)), True  # type: ignore[return-value]


def update_anime_fields(conn: sqlite3.Connection, anime_id: int, **fields: Any) -> sqlite3.Row:
    allowed = {
        "canonical_title",
        "display_title",
        "source_title",
        "anilist_id",
        "status",
        "total_episodes",
        "available_episode_count",
        "cover_url",
        "cover_path",
        "notes",
        "sort_order",
        "last_watched_at",
    }
    updates = {key: value for key, value in fields.items() if key in allowed}
    if "display_title" in updates:
        updates["canonical_title"] = canonicalize_title(str(updates["display_title"]))
    if not updates:
        row = get_anime_by_id(conn, anime_id)
        if row is None:
            raise KeyError(f"anime id not found: {anime_id}")
        return row
    if "status" in updates and updates["status"] not in STATUSES:
        raise ValueError(f"invalid status: {updates['status']}")
    updates["updated_at"] = now_iso()
    columns = ", ".join(f"{key} = ?" for key in updates)
    values = list(updates.values()) + [anime_id]
    with conn:
        conn.execute(f"UPDATE anime SET {columns} WHERE id = ?", values)
    row = get_anime_by_id(conn, anime_id)
    if row is None:
        raise KeyError(f"anime id not found: {anime_id}")
    return row


def delete_anime(conn: sqlite3.Connection, title_or_id: str | int) -> bool:
    if isinstance(title_or_id, int) or str(title_or_id).isdigit():
        row = get_anime_by_id(conn, int(title_or_id))
    else:
        row = get_anime(conn, str(title_or_id))
    if row is None:
        return False
    with conn:
        conn.execute("DELETE FROM anime WHERE id = ?", (row["id"],))
    return True


def upsert_episodes(
    conn: sqlite3.Connection,
    anime_id: int,
    episodes: Iterable[Any],
    source_label: str | None = None,
) -> list[sqlite3.Row]:
    rows: list[sqlite3.Row] = []
    ts = now_iso()
    with conn:
        for item in episodes:
            key = episode_key(item)
            if not key:
                continue
            number = episode_number(item)
            title = episode_title(item)
            conn.execute(
                """
                INSERT INTO episodes(
                    anime_id, episode_key, episode_number, title, source_label, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(anime_id, episode_key) DO UPDATE SET
                    episode_number = COALESCE(excluded.episode_number, episodes.episode_number),
                    title = COALESCE(excluded.title, episodes.title),
                    source_label = COALESCE(excluded.source_label, episodes.source_label),
                    updated_at = excluded.updated_at
                """,
                (anime_id, key, number, title, source_label, ts, ts),
            )
        count = conn.execute("SELECT COUNT(*) AS count FROM episodes WHERE anime_id = ?", (anime_id,)).fetchone()[
            "count"
        ]
        conn.execute(
            "UPDATE anime SET available_episode_count = ?, updated_at = ? WHERE id = ?",
            (int(count), ts, anime_id),
        )
    for row in conn.execute("SELECT * FROM episodes WHERE anime_id = ? ORDER BY episode_key", (anime_id,)):
        rows.append(row)
    return rows


def find_episode(conn: sqlite3.Connection, anime_id: int, episode: str) -> sqlite3.Row | None:
    key = episode_key(episode)
    return conn.execute(
        """
        SELECT * FROM episodes
        WHERE anime_id = ? AND (episode_key = ? OR episode_number = ?)
        ORDER BY id LIMIT 1
        """,
        (anime_id, key, key),
    ).fetchone()


def get_or_create_episode(conn: sqlite3.Connection, anime_id: int, episode: str) -> tuple[sqlite3.Row, bool]:
    existing = find_episode(conn, anime_id, episode)
    if existing is not None:
        return existing, False
    rows = upsert_episodes(conn, anime_id, [episode])
    row = find_episode(conn, anime_id, episode)
    if row is None:
        raise RuntimeError("episode insert failed")
    return row, True


def mark_episode(conn: sqlite3.Connection, anime_id: int, episode: str, watched: bool) -> sqlite3.Row:
    row, _ = get_or_create_episode(conn, anime_id, episode)
    ts = now_iso()
    with conn:
        conn.execute(
            """
            UPDATE episodes
            SET watched = ?, watched_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (1 if watched else 0, ts if watched else None, ts, row["id"]),
        )
        if watched:
            conn.execute(
                "UPDATE anime SET last_watched_at = ?, updated_at = ? WHERE id = ?",
                (ts, ts, anime_id),
            )
    return conn.execute("SELECT * FROM episodes WHERE id = ?", (row["id"],)).fetchone()


def mark_started(conn: sqlite3.Connection, anime_id: int, episode: str) -> sqlite3.Row:
    row, _ = get_or_create_episode(conn, anime_id, episode)
    ts = now_iso()
    with conn:
        conn.execute(
            """
            UPDATE episodes
            SET first_started_at = COALESCE(first_started_at, ?),
                last_started_at = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (ts, ts, ts, row["id"]),
        )
    return conn.execute("SELECT * FROM episodes WHERE id = ?", (row["id"],)).fetchone()


def record_event(
    conn: sqlite3.Connection,
    event_type: str,
    payload: dict[str, Any],
    anime_id: int | None = None,
    episode_id: int | None = None,
) -> sqlite3.Row:
    ts = now_iso()
    with conn:
        cur = conn.execute(
            """
            INSERT INTO watch_events(anime_id, episode_id, event_type, payload_json, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (anime_id, episode_id, event_type, json.dumps(payload, sort_keys=True), ts),
        )
    return conn.execute("SELECT * FROM watch_events WHERE id = ?", (cur.lastrowid,)).fetchone()


def list_anime(conn: sqlite3.Connection, status: str | None = None, search: str | None = None) -> list[sqlite3.Row]:
    where = []
    params: list[Any] = []
    if status:
        where.append("status = ?")
        params.append(status)
    if search:
        where.append("(display_title LIKE ? OR source_title LIKE ?)")
        needle = f"%{search}%"
        params.extend([needle, needle])
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    return list(
        conn.execute(
            f"""
            SELECT
                anime.*,
                COALESCE(SUM(CASE WHEN episodes.watched = 1 THEN 1 ELSE 0 END), 0) AS watched_count
            FROM anime
            LEFT JOIN episodes ON episodes.anime_id = anime.id
            {clause}
            GROUP BY anime.id
            ORDER BY anime.status, anime.sort_order, anime.display_title
            """,
            params,
        )
    )


def episodes_for_anime(conn: sqlite3.Connection, anime_id: int) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            """
            SELECT * FROM episodes
            WHERE anime_id = ?
            ORDER BY
                CASE WHEN episode_number GLOB '[0-9]*' THEN CAST(episode_number AS REAL) ELSE 999999 END,
                episode_key
            """,
            (anime_id,),
        )
    )


def export_data(conn: sqlite3.Connection) -> dict[str, Any]:
    data: dict[str, Any] = {}
    for table in ("anime", "episodes", "watch_events", "metadata_matches"):
        data[table] = [dict(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY id")]
    return data


def watch_events(
    conn: sqlite3.Connection,
    anime_id: int | None = None,
    recent: int | None = None,
    title_hint: str | None = None,
) -> list[sqlite3.Row]:
    params: list[Any] = []
    where = ""
    if anime_id is not None:
        where = "WHERE watch_events.anime_id = ?"
        params.append(anime_id)
        if title_hint:
            where += " OR (watch_events.event_type = 'launch' AND watch_events.payload_json LIKE ?)"
            params.append(f"%{title_hint}%")
    limit = ""
    if recent is not None:
        limit = "LIMIT ?"
        params.append(int(recent))
    return list(
        conn.execute(
            f"""
            SELECT
                watch_events.*,
                anime.display_title AS anime_title,
                episodes.episode_key AS episode_key
            FROM watch_events
            LEFT JOIN anime ON anime.id = watch_events.anime_id
            LEFT JOIN episodes ON episodes.id = watch_events.episode_id
            {where}
            ORDER BY watch_events.created_at DESC, watch_events.id DESC
            {limit}
            """,
            params,
        )
    )


def watched_episode_count(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) AS count FROM episodes WHERE watched = 1").fetchone()["count"])


def status_counts(conn: sqlite3.Connection) -> dict[str, int]:
    counts = {status: 0 for status in STATUSES}
    for row in conn.execute("SELECT status, COUNT(*) AS count FROM anime GROUP BY status"):
        counts[row["status"]] = int(row["count"])
    return counts


def next_unwatched_episode(conn: sqlite3.Connection, anime_id: int) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT * FROM episodes
        WHERE anime_id = ? AND watched = 0
        ORDER BY
            CASE WHEN episode_number GLOB '[0-9]*' THEN CAST(episode_number AS REAL) ELSE 999999 END,
            episode_key
        LIMIT 1
        """,
        (anime_id,),
    ).fetchone()


def recently_watched_anime(conn: sqlite3.Connection, limit: int = 10) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            """
            SELECT * FROM anime
            WHERE last_watched_at IS NOT NULL
            ORDER BY last_watched_at DESC, updated_at DESC
            LIMIT ?
            """,
            (limit,),
        )
    )


def likely_duplicates(conn: sqlite3.Connection) -> list[tuple[str, list[sqlite3.Row]]]:
    groups: dict[str, list[sqlite3.Row]] = {}
    for row in conn.execute("SELECT * FROM anime ORDER BY display_title"):
        keys = [row["canonical_title"]]
        if row["anilist_id"] is not None:
            keys.append(f"anilist:{row['anilist_id']}")
        for key in keys:
            groups.setdefault(key, []).append(row)
    seen: set[tuple[int, ...]] = set()
    duplicates: list[tuple[str, list[sqlite3.Row]]] = []
    for key, rows in groups.items():
        ids = tuple(sorted(int(row["id"]) for row in rows))
        if len(ids) > 1 and ids not in seen:
            seen.add(ids)
            duplicates.append((key, rows))
    rows = list(conn.execute("SELECT * FROM anime ORDER BY display_title"))
    for idx, left in enumerate(rows):
        for right in rows[idx + 1 :]:
            ids = tuple(sorted((int(left["id"]), int(right["id"]))))
            if ids in seen:
                continue
            left_title = left["canonical_title"]
            right_title = right["canonical_title"]
            if not left_title or not right_title:
                continue
            if SequenceMatcher(None, left_title, right_title).ratio() >= 0.82:
                seen.add(ids)
                duplicates.append((f"similar:{left_title} ~ {right_title}", [left, right]))
    return duplicates


def merge_anime(conn: sqlite3.Connection, target_id: int, source_id: int) -> dict[str, Any]:
    if target_id == source_id:
        raise ValueError("cannot merge an anime into itself")
    target = get_anime_by_id(conn, target_id)
    source = get_anime_by_id(conn, source_id)
    if target is None or source is None:
        raise KeyError("source or target anime not found")

    episode_map: dict[int, int] = {}
    with conn:
        for source_ep in episodes_for_anime(conn, source_id):
            target_ep = find_episode(conn, target_id, source_ep["episode_key"])
            if target_ep is None:
                cur = conn.execute(
                    """
                    INSERT INTO episodes(
                        anime_id, episode_key, episode_number, title, watched, watched_at,
                        first_started_at, last_started_at, source_label, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        target_id,
                        source_ep["episode_key"],
                        source_ep["episode_number"],
                        source_ep["title"],
                        source_ep["watched"],
                        source_ep["watched_at"],
                        source_ep["first_started_at"],
                        source_ep["last_started_at"],
                        source_ep["source_label"],
                        source_ep["created_at"],
                        now_iso(),
                    ),
                )
                episode_map[int(source_ep["id"])] = int(cur.lastrowid)
            else:
                watched = bool(target_ep["watched"] or source_ep["watched"])
                watched_at = target_ep["watched_at"] or source_ep["watched_at"]
                if target_ep["watched_at"] and source_ep["watched_at"]:
                    watched_at = max(str(target_ep["watched_at"]), str(source_ep["watched_at"]))
                conn.execute(
                    """
                    UPDATE episodes
                    SET watched = ?,
                        watched_at = ?,
                        first_started_at = COALESCE(episodes.first_started_at, ?),
                        last_started_at = COALESCE(?, episodes.last_started_at),
                        title = COALESCE(episodes.title, ?),
                        source_label = COALESCE(episodes.source_label, ?),
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        1 if watched else 0,
                        watched_at if watched else None,
                        source_ep["first_started_at"],
                        source_ep["last_started_at"],
                        source_ep["title"],
                        source_ep["source_label"],
                        now_iso(),
                        target_ep["id"],
                    ),
                )
                episode_map[int(source_ep["id"])] = int(target_ep["id"])

        notes = target["notes"] or ""
        source_notes = source["notes"] or ""
        if source_notes and source_notes not in notes:
            notes = (notes + "\n\nMerged notes from " + source["display_title"] + ":\n" + source_notes).strip()
        fields: dict[str, Any] = {"notes": notes or None}
        for field in ("anilist_id", "total_episodes", "cover_url", "cover_path", "last_watched_at"):
            if target[field] is None and source[field] is not None:
                fields[field] = source[field]
        if source["last_watched_at"] and target["last_watched_at"]:
            fields["last_watched_at"] = max(str(source["last_watched_at"]), str(target["last_watched_at"]))
        update_anime_fields(conn, target_id, **fields)

        for row in conn.execute("SELECT * FROM metadata_matches WHERE anime_id = ?", (source_id,)):
            conn.execute(
                """
                INSERT INTO metadata_matches(
                    anime_id, provider, provider_media_id, confidence_score, selected, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(anime_id, provider, provider_media_id) DO UPDATE SET
                    confidence_score = MAX(metadata_matches.confidence_score, excluded.confidence_score),
                    selected = MAX(metadata_matches.selected, excluded.selected),
                    payload_json = excluded.payload_json
                """,
                (
                    target_id,
                    row["provider"],
                    row["provider_media_id"],
                    row["confidence_score"],
                    row["selected"],
                    row["payload_json"],
                    row["created_at"],
                ),
            )

        for old_episode_id, new_episode_id in episode_map.items():
            conn.execute(
                "UPDATE watch_events SET anime_id = ?, episode_id = ? WHERE anime_id = ? AND episode_id = ?",
                (target_id, new_episode_id, source_id, old_episode_id),
            )
        conn.execute("UPDATE watch_events SET anime_id = ? WHERE anime_id = ?", (target_id, source_id))
        conn.execute("DELETE FROM anime WHERE id = ?", (source_id,))

        count = conn.execute("SELECT COUNT(*) AS count FROM episodes WHERE anime_id = ?", (target_id,)).fetchone()[
            "count"
        ]
        conn.execute(
            "UPDATE anime SET available_episode_count = ?, updated_at = ? WHERE id = ?",
            (int(count), now_iso(), target_id),
        )

    return {"target_id": target_id, "source_id": source_id, "episode_count": len(episode_map)}


def import_data(conn: sqlite3.Connection, data: dict[str, Any]) -> dict[str, int]:
    anime_id_map: dict[int, int] = {}
    imported_anime = 0
    imported_episodes = 0
    with conn:
        for item in data.get("anime", []):
            title = item.get("display_title") or item.get("source_title") or item.get("canonical_title")
            if not title:
                continue
            existing = None
            if item.get("anilist_id") is not None:
                existing = conn.execute("SELECT * FROM anime WHERE anilist_id = ?", (item["anilist_id"],)).fetchone()
            if existing is None:
                existing = get_anime(conn, str(title))
            if existing is None:
                anime, created = get_or_create_anime(
                    conn,
                    str(title),
                    source_title=item.get("source_title") or str(title),
                    status=item.get("status") if item.get("status") in STATUSES else "watching",
                )
                imported_anime += 1 if created else 0
            else:
                anime = existing
            anime_id_map[int(item["id"])] = int(anime["id"])
            update_anime_fields(
                conn,
                anime["id"],
                status=item.get("status") if item.get("status") in STATUSES else anime["status"],
                anilist_id=item.get("anilist_id") or anime["anilist_id"],
                total_episodes=item.get("total_episodes") or anime["total_episodes"],
                cover_url=item.get("cover_url") or anime["cover_url"],
                cover_path=item.get("cover_path") or anime["cover_path"],
                notes=item.get("notes") or anime["notes"],
                last_watched_at=item.get("last_watched_at") or anime["last_watched_at"],
            )

        for item in data.get("episodes", []):
            anime_id = anime_id_map.get(int(item["anime_id"]))
            if anime_id is None:
                continue
            episode, _created = get_or_create_episode(conn, anime_id, str(item["episode_key"]))
            conn.execute(
                """
                UPDATE episodes
                SET episode_number = COALESCE(?, episode_number),
                    title = COALESCE(?, title),
                    watched = ?,
                    watched_at = ?,
                    first_started_at = COALESCE(first_started_at, ?),
                    last_started_at = COALESCE(?, last_started_at),
                    source_label = COALESCE(?, source_label),
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    item.get("episode_number"),
                    item.get("title"),
                    1 if item.get("watched") else episode["watched"],
                    item.get("watched_at") if item.get("watched") else episode["watched_at"],
                    item.get("first_started_at"),
                    item.get("last_started_at"),
                    item.get("source_label"),
                    now_iso(),
                    episode["id"],
                ),
            )
            imported_episodes += 1
    return {"anime": imported_anime, "episodes": imported_episodes}


def repair_database(conn: sqlite3.Connection, fix: bool = False) -> dict[str, Any]:
    report: dict[str, Any] = {}
    orphaned = list(
        conn.execute(
            """
            SELECT episodes.* FROM episodes
            LEFT JOIN anime ON anime.id = episodes.anime_id
            WHERE anime.id IS NULL
            """
        )
    )
    duplicate_groups = list(
        conn.execute(
            """
            SELECT anime_id, episode_key, COUNT(*) AS count
            FROM episodes
            GROUP BY anime_id, episode_key
            HAVING COUNT(*) > 1
            """
        )
    )
    missing_watched_at = list(conn.execute("SELECT * FROM episodes WHERE watched = 1 AND watched_at IS NULL"))
    missing_started_at = list(
        conn.execute(
            """
            SELECT * FROM episodes
            WHERE last_started_at IS NOT NULL AND first_started_at IS NULL
            """
        )
    )
    broken_covers = [
        row
        for row in conn.execute("SELECT * FROM anime WHERE cover_path IS NOT NULL")
        if not Path(row["cover_path"]).expanduser().exists()
    ]
    report.update(
        {
            "orphaned_episodes": len(orphaned),
            "duplicate_episode_groups": len(duplicate_groups),
            "watched_missing_watched_at": len(missing_watched_at),
            "started_missing_first_started_at": len(missing_started_at),
            "broken_cover_paths": len(broken_covers),
        }
    )
    if fix:
        ts = now_iso()
        with conn:
            conn.execute(
                """
                UPDATE anime
                SET available_episode_count = (
                    SELECT COUNT(*) FROM episodes WHERE episodes.anime_id = anime.id
                ),
                updated_at = ?
                """,
                (ts,),
            )
            conn.execute("DELETE FROM episodes WHERE anime_id NOT IN (SELECT id FROM anime)")
            conn.execute("UPDATE episodes SET watched_at = ? WHERE watched = 1 AND watched_at IS NULL", (ts,))
            conn.execute(
                "UPDATE episodes SET first_started_at = last_started_at WHERE last_started_at IS NOT NULL AND first_started_at IS NULL"
            )
            for row in broken_covers:
                conn.execute("UPDATE anime SET cover_path = NULL, updated_at = ? WHERE id = ?", (ts, row["id"]))
        report["fixed"] = True
    else:
        report["fixed"] = False
    return report


def backup_database(db_path: Path, dest_dir: Path | None = None) -> Path:
    dest_dir = dest_dir or db_path.parent
    dest_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = dest_dir / f"watchlist-{stamp}.sqlite3"
    shutil.copy2(db_path, dest)
    return dest


def restore_database(source: Path, db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, db_path)
