from __future__ import annotations

import json

from ani_watchlist.config import AppConfig, AniListConfig, MetadataConfig
from ani_watchlist.db import initialize
from ani_watchlist.metadata import refresh_all_metadata, refresh_metadata_for_anime, search_and_store_matches, store_selected_metadata_payload
from ani_watchlist.providers.base import MetadataSearchResult
from ani_watchlist.store import get_or_create_anime, update_anime_fields


class FakeProvider:
    name = "anilist"

    def __init__(self, results):
        self.results = results

    def search_title(self, title):
        return self.results

    def cache_cover(self, media_id, url):
        return f"/tmp/{media_id}.jpg"

    def get_media(self, media_id):
        for result in self.results:
            if str(result.media_id) == str(media_id):
                return result.payload
        raise RuntimeError(f"missing media: {media_id}")


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


def test_metadata_without_english_title_preserves_current_display_title(app_env):
    conn = initialize()
    anime, _ = get_or_create_anime(conn, "Spare Me, Great Lord! (Da Wang Rao Ming)")
    result = MetadataSearchResult(
        "anilist",
        "120220",
        "Dawang Raoming",
        1.0,
        payload(120220, "Dawang Raoming", 12) | {"title": {"romaji": "Dawang Raoming", "userPreferred": "Dawang Raoming"}},
    )
    config = AppConfig(metadata=MetadataConfig(search_on_new_title=True), anilist=AniListConfig(enabled=True))

    search_and_store_matches(conn, anime["id"], "Spare Me, Great Lord! (Da Wang Rao Ming)", config, FakeProvider([result]))

    stored = conn.execute("SELECT * FROM anime WHERE id = ?", (anime["id"],)).fetchone()
    assert stored["display_title"] == "Spare Me, Great Lord! (Da Wang Rao Ming)"
    assert stored["anilist_id"] == 120220


def test_metadata_appends_adult_label_to_preserved_current_title(app_env):
    conn = initialize()
    anime, _ = get_or_create_anime(conn, "Existing Adult Title")
    result = MetadataSearchResult(
        "anilist",
        "368",
        "Bible Black [18+]",
        1.0,
        payload(368, "Bible Black", 6)
        | {"title": {"romaji": "Bible Black", "userPreferred": "Bible Black"}, "isAdult": True},
    )
    config = AppConfig(metadata=MetadataConfig(search_on_new_title=True), anilist=AniListConfig(enabled=True))

    search_and_store_matches(conn, anime["id"], "Existing Adult Title", config, FakeProvider([result]))

    stored = conn.execute("SELECT * FROM anime WHERE id = ?", (anime["id"],)).fetchone()
    assert stored["display_title"] == "Existing Adult Title [18+]"


def test_store_selected_metadata_payload_preserves_discovery_match(app_env):
    conn = initialize()
    anime, _ = get_or_create_anime(conn, "Bible Black [18+]", status="plan_to_watch")
    media = payload(368, "Bible Black", 6) | {
        "title": {"romaji": "Bible Black", "userPreferred": "Bible Black"},
        "synonyms": ["Bible Black: Night of the Walpulgiss"],
        "isAdult": True,
    }

    store_selected_metadata_payload(conn, anime["id"], 368, media, "/tmp/anilist-368.jpg")

    stored = conn.execute("SELECT * FROM anime WHERE id = ?", (anime["id"],)).fetchone()
    assert stored["anilist_id"] == 368
    assert stored["display_title"] == "Bible Black [18+]"
    assert stored["cover_path"] == "/tmp/anilist-368.jpg"
    selected = conn.execute("SELECT * FROM metadata_matches WHERE anime_id = ?", (anime["id"],)).fetchone()
    assert selected["selected"] == 1
    assert json.loads(selected["payload_json"])["synonyms"] == ["Bible Black: Night of the Walpulgiss"]


def test_refresh_linked_metadata_replaces_stale_imported_cover_path(app_env, tmp_path) -> None:
    conn = initialize()
    anime, _ = get_or_create_anime(conn, "Cowboy Bebop")
    update_anime_fields(conn, anime["id"], anilist_id=1, cover_path=str(tmp_path / "missing-cover.jpg"))
    result = MetadataSearchResult("anilist", "1", "Cowboy Bebop", 1.0, payload())
    config = AppConfig(metadata=MetadataConfig(search_on_new_title=True), anilist=AniListConfig(enabled=True))

    refresh_metadata_for_anime(conn, anime["id"], config, FakeProvider([result]))

    stored = conn.execute("SELECT * FROM anime WHERE id = ?", (anime["id"],)).fetchone()
    assert stored["cover_path"] == "/tmp/1.jpg"


def test_refresh_all_metadata_links_every_confident_watchlist_entry(app_env) -> None:
    conn = initialize()
    first, _ = get_or_create_anime(conn, "Cowboy Bebop")
    second, _ = get_or_create_anime(conn, "Frieren")
    results = {
        "Cowboy Bebop": MetadataSearchResult("anilist", "1", "Cowboy Bebop", 1.0, payload()),
        "Frieren": MetadataSearchResult("anilist", "2", "Frieren", 1.0, payload(2, "Frieren", 28)),
    }

    class RoutingProvider(FakeProvider):
        def __init__(self):
            super().__init__(list(results.values()))

        def search_title(self, title):
            return [results[title]]

    config = AppConfig(metadata=MetadataConfig(search_on_new_title=True), anilist=AniListConfig(enabled=True))
    progress: list[tuple[int, int, str]] = []

    summary = refresh_all_metadata(
        conn,
        config,
        provider=RoutingProvider(),
        progress=lambda current, total, title: progress.append((current, total, title)),
    )

    assert summary.total == 2
    assert summary.refreshed == 2
    assert summary.linked == 2
    assert summary.unresolved == 0
    assert summary.failures == ()
    assert conn.execute("SELECT anilist_id FROM anime WHERE id = ?", (first["id"],)).fetchone()[0] == 1
    assert conn.execute("SELECT anilist_id FROM anime WHERE id = ?", (second["id"],)).fetchone()[0] == 2
    assert progress == [(1, 2, "Cowboy Bebop"), (2, 2, "Frieren")]


def test_refresh_all_metadata_continues_after_individual_failure(app_env) -> None:
    conn = initialize()
    get_or_create_anime(conn, "Broken Show")
    get_or_create_anime(conn, "Cowboy Bebop")
    result = MetadataSearchResult("anilist", "1", "Cowboy Bebop", 1.0, payload())

    class PartiallyFailingProvider(FakeProvider):
        def search_title(self, title):
            if title == "Broken Show":
                raise RuntimeError("temporary failure")
            return super().search_title(title)

    config = AppConfig(metadata=MetadataConfig(search_on_new_title=True), anilist=AniListConfig(enabled=True))

    summary = refresh_all_metadata(conn, config, provider=PartiallyFailingProvider([result]))

    assert summary.total == 2
    assert summary.refreshed == 1
    assert summary.linked == 1
    assert summary.unresolved == 0
    assert summary.failures == (("Broken Show", "temporary failure"),)
