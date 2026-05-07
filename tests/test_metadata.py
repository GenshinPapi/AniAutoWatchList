from __future__ import annotations

from ani_watchlist.config import AppConfig, AniListConfig, MetadataConfig
from ani_watchlist.db import initialize
from ani_watchlist.metadata import search_and_store_matches
from ani_watchlist.providers.base import MetadataSearchResult
from ani_watchlist.store import get_or_create_anime


class FakeProvider:
    name = "anilist"

    def __init__(self, results):
        self.results = results

    def search_title(self, title):
        return self.results

    def cache_cover(self, media_id, url):
        return f"/tmp/{media_id}.jpg"


def payload(media_id=1, title="Cowboy Bebop", episodes=26):
    return {
        "id": media_id,
        "title": {"userPreferred": title, "romaji": title},
        "episodes": episodes,
        "coverImage": {"large": "https://img.example/cover.jpg"},
    }


def test_high_confidence_match_autolinks(app_env):
    conn = initialize()
    anime, _ = get_or_create_anime(conn, "Cowboy Bebop")
    result = MetadataSearchResult("anilist", "1", "Cowboy Bebop", 1.0, payload())
    config = AppConfig(metadata=MetadataConfig(search_on_new_title=True), anilist=AniListConfig(enabled=True))

    matches = search_and_store_matches(conn, anime["id"], "Cowboy Bebop", config, FakeProvider([result]))

    assert len(matches) == 1
    stored = conn.execute("SELECT * FROM anime WHERE id = ?", (anime["id"],)).fetchone()
    assert stored["anilist_id"] == 1
    assert stored["total_episodes"] == 26
    assert stored["cover_path"] == "/tmp/1.jpg"
    selected = conn.execute("SELECT selected FROM metadata_matches").fetchone()
    assert selected["selected"] == 1


def test_low_confidence_match_is_candidate_only(app_env):
    conn = initialize()
    anime, _ = get_or_create_anime(conn, "Bebop")
    result = MetadataSearchResult("anilist", "1", "Cowboy Bebop", 0.5, payload())
    config = AppConfig(metadata=MetadataConfig(search_on_new_title=True), anilist=AniListConfig(enabled=True))

    search_and_store_matches(conn, anime["id"], "Bebop", config, FakeProvider([result]))

    stored = conn.execute("SELECT * FROM anime WHERE id = ?", (anime["id"],)).fetchone()
    assert stored["anilist_id"] is None
    selected = conn.execute("SELECT selected FROM metadata_matches").fetchone()
    assert selected["selected"] == 0


def test_metadata_display_title_prefers_english_with_romaji(app_env):
    conn = initialize()
    anime, _ = get_or_create_anime(conn, "Tsue to Tsurugi no Wistoria")
    result = MetadataSearchResult(
        "anilist",
        "174576",
        "Wistoria: Wand and Sword (Tsue to Tsurugi no Wistoria)",
        1.0,
        payload(174576, "Tsue to Tsurugi no Wistoria", 12)
        | {"title": {"english": "Wistoria: Wand and Sword", "romaji": "Tsue to Tsurugi no Wistoria"}},
    )
    config = AppConfig(metadata=MetadataConfig(search_on_new_title=True), anilist=AniListConfig(enabled=True))

    search_and_store_matches(conn, anime["id"], "Tsue to Tsurugi no Wistoria", config, FakeProvider([result]))

    stored = conn.execute("SELECT * FROM anime WHERE id = ?", (anime["id"],)).fetchone()
    assert stored["display_title"] == "Wistoria: Wand and Sword (Tsue to Tsurugi no Wistoria)"


def test_exact_display_title_can_autolink_when_other_matches_are_close(app_env):
    conn = initialize()
    anime, _ = get_or_create_anime(conn, "One Piece (1P)")
    exact = MetadataSearchResult(
        "anilist",
        "21",
        "ONE PIECE",
        1.0,
        payload(21, "ONE PIECE", None) | {"title": {"english": "ONE PIECE", "romaji": "ONE PIECE"}},
    )
    special = MetadataSearchResult(
        "anilist",
        "19123",
        "One Piece: Episode of Merry",
        1.0,
        payload(19123, "One Piece: Episode of Merry", 1),
    )
    config = AppConfig(metadata=MetadataConfig(search_on_new_title=True), anilist=AniListConfig(enabled=True))

    search_and_store_matches(conn, anime["id"], "One Piece (1P)", config, FakeProvider([exact, special]))

    stored = conn.execute("SELECT * FROM anime WHERE id = ?", (anime["id"],)).fetchone()
    assert stored["anilist_id"] == 21
