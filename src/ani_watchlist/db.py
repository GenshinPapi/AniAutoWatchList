from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Iterable

from .paths import get_paths


LATEST_SCHEMA_VERSION = 3


MIGRATIONS: dict[int, str] = {
    1: """
    CREATE TABLE IF NOT EXISTS anime (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        canonical_title TEXT NOT NULL UNIQUE,
        display_title TEXT NOT NULL,
        source_title TEXT NOT NULL,
        anilist_id INTEGER,
        status TEXT NOT NULL DEFAULT 'watching'
            CHECK (status IN ('watching', 'completed', 'dropped', 'on_hold', 'plan_to_watch')),
        total_episodes INTEGER,
        available_episode_count INTEGER,
        cover_url TEXT,
        cover_path TEXT,
        notes TEXT,
        sort_order INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        last_watched_at TEXT
    );

    CREATE TABLE IF NOT EXISTS episodes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        anime_id INTEGER NOT NULL REFERENCES anime(id) ON DELETE CASCADE,
        episode_key TEXT NOT NULL,
        episode_number TEXT,
        title TEXT,
        watched INTEGER NOT NULL DEFAULT 0 CHECK (watched IN (0, 1)),
        watched_at TEXT,
        first_started_at TEXT,
        last_started_at TEXT,
        source_label TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(anime_id, episode_key)
    );

    CREATE TABLE IF NOT EXISTS watch_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        anime_id INTEGER REFERENCES anime(id) ON DELETE SET NULL,
        episode_id INTEGER REFERENCES episodes(id) ON DELETE SET NULL,
        event_type TEXT NOT NULL
            CHECK (event_type IN (
                'launch',
                'title_selected',
                'episodes_listed',
                'playback_started',
                'playback_finished',
                'playback_failed'
            )),
        payload_json TEXT NOT NULL,
        created_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS metadata_matches (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        anime_id INTEGER NOT NULL REFERENCES anime(id) ON DELETE CASCADE,
        provider TEXT NOT NULL DEFAULT 'anilist',
        provider_media_id TEXT NOT NULL,
        confidence_score REAL NOT NULL,
        selected INTEGER NOT NULL DEFAULT 0 CHECK (selected IN (0, 1)),
        payload_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE(anime_id, provider, provider_media_id)
    );

    CREATE INDEX IF NOT EXISTS idx_anime_status_sort ON anime(status, sort_order, display_title);
    CREATE INDEX IF NOT EXISTS idx_episodes_anime ON episodes(anime_id, episode_key);
    CREATE INDEX IF NOT EXISTS idx_watch_events_created ON watch_events(created_at);
    CREATE INDEX IF NOT EXISTS idx_metadata_matches_anime ON metadata_matches(anime_id, provider);
    """,
    2: """
    CREATE TABLE IF NOT EXISTS discovery_cache (
        cache_key TEXT PRIMARY KEY,
        payload_json TEXT NOT NULL,
        fetched_at TEXT NOT NULL,
        expires_at TEXT NOT NULL
    );

    CREATE INDEX IF NOT EXISTS idx_discovery_cache_expires ON discovery_cache(expires_at);
    """,
    3: """
    CREATE INDEX IF NOT EXISTS idx_episodes_anime_watched ON episodes(anime_id, watched);
    CREATE INDEX IF NOT EXISTS idx_episodes_watched ON episodes(watched);
    CREATE INDEX IF NOT EXISTS idx_watch_events_anime_created ON watch_events(anime_id, created_at DESC, id DESC);
    """,
}


def connect(path: Path | None = None) -> sqlite3.Connection:
    db_path = Path(path) if path is not None else get_paths().db_path
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _applied_versions(conn: sqlite3.Connection) -> set[int]:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    return {int(row["version"]) for row in conn.execute("SELECT version FROM schema_migrations")}


def migrate(conn: sqlite3.Connection) -> None:
    applied = _applied_versions(conn)
    for version in sorted(MIGRATIONS):
        if version in applied:
            continue
        with conn:
            conn.executescript(MIGRATIONS[version])
            conn.execute(
                "INSERT INTO schema_migrations(version, applied_at) VALUES (?, datetime('now'))",
                (version,),
            )


def initialize(path: Path | None = None) -> sqlite3.Connection:
    conn = connect(path)
    migrate(conn)
    return conn


def current_version(conn: sqlite3.Connection) -> int:
    versions: Iterable[sqlite3.Row] = conn.execute("SELECT version FROM schema_migrations ORDER BY version DESC")
    row = next(iter(versions), None)
    return int(row["version"]) if row else 0
