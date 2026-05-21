from __future__ import annotations

from ani_watchlist.db import current_version, initialize
from ani_watchlist.store import (
    clean_display_title,
    episodes_for_anime,
    get_anime,
    get_anime_by_anilist_id,
    get_or_create_anime,
    mark_episode,
    title_match_key,
    upsert_episodes,
    update_anime_fields,
)


def test_migrations_create_required_tables(app_env):
    conn = initialize()
    tables = {
        row["name"]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    assert current_version(conn) == 2
    assert {"anime", "episodes", "watch_events", "metadata_matches", "discovery_cache"}.issubset(tables)


def test_add_anime_update_episode_list_and_mark_watched(app_env):
    conn = initialize()
    anime, created = get_or_create_anime(conn, "Frieren: Beyond Journey's End (28 episodes)")
    assert created is True
    assert anime["display_title"] == "Frieren: Beyond Journey's End"

    rows = upsert_episodes(conn, anime["id"], ["1", "2", {"episode": "3", "title": "A Journey Begins"}])
    assert len(rows) == 3
    mark_episode(conn, anime["id"], "2", watched=True)

    episodes = episodes_for_anime(conn, anime["id"])
    assert [row["episode_key"] for row in episodes] == ["1", "2", "3"]
    assert [row["watched"] for row in episodes] == [0, 1, 0]
    assert get_anime(conn, "frieren beyond journeys end") is not None


def test_clean_display_title_preserves_adult_content_label_plus() -> None:
    assert clean_display_title("Bible+Black") == "Bible Black"
    assert clean_display_title("Bible Black [18+]") == "Bible Black [18+]"


def test_status_changes(app_env):
    conn = initialize()
    anime, _ = get_or_create_anime(conn, "Cowboy Bebop")
    updated = update_anime_fields(conn, anime["id"], status="completed")
    assert updated["status"] == "completed"


def test_display_title_update_refreshes_canonical_lookup(app_env):
    conn = initialize()
    anime, _ = get_or_create_anime(conn, "Tsue to Tsurugi no Wistoria")
    update_anime_fields(conn, anime["id"], display_title="Wistoria: Wand and Sword (Tsue to Tsurugi no Wistoria)")
    assert get_anime(conn, "Wistoria Wand and Sword Tsue to Tsurugi no Wistoria") is not None


def test_source_title_still_matches_after_english_metadata_rename(app_env):
    conn = initialize()
    anime, _ = get_or_create_anime(conn, "Tsue to Tsurugi no Wistoria (12 episodes)")
    update_anime_fields(conn, anime["id"], display_title="Wistoria: Wand and Sword (Tsue to Tsurugi no Wistoria)")

    found, created = get_or_create_anime(conn, "Tsue to Tsurugi no Wistoria (12 episodes)")

    assert created is False
    assert found["id"] == anime["id"]


def test_season_title_alias_matches_existing_anime(app_env):
    conn = initialize()
    anime, _ = get_or_create_anime(conn, "Farming Life in Another World 2 (Isekai Nonbiri Nouka 2)")

    found, created = get_or_create_anime(conn, "Farming Life in Another World Season 2 (Isekai Nonbiri Nouka 2) (7 episodes)")

    assert created is False
    assert found["id"] == anime["id"]
    assert title_match_key("Farming Life in Another World Season 2") == title_match_key("Farming Life in Another World 2")


def test_anilist_lookup_returns_existing_row(app_env):
    conn = initialize()
    anime, _ = get_or_create_anime(conn, "One Piece")
    update_anime_fields(conn, anime["id"], anilist_id=21)

    assert get_anime_by_anilist_id(conn, 21)["id"] == anime["id"]


def test_display_title_update_does_not_fail_on_existing_canonical(app_env):
    conn = initialize()
    anime, _ = get_or_create_anime(conn, "Spare Me, Great Lord! (Da Wang Rao Ming)")
    existing, _ = get_or_create_anime(conn, "Dawang Raoming")

    updated = update_anime_fields(conn, anime["id"], display_title="Dawang Raoming")

    assert updated["display_title"] == "Dawang Raoming"
    assert updated["canonical_title"] == "spare me great lord da wang rao ming"
    assert existing["canonical_title"] == "dawang raoming"
