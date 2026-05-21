from __future__ import annotations

from ani_watchlist.config import set_config_value
from ani_watchlist.db import initialize
from ani_watchlist.hook import run
from ani_watchlist.store import episodes_for_anime, get_anime, get_or_create_anime, list_anime, update_anime_fields


def test_hook_parses_episode_list(app_env):
    assert run(["episodes-listed", "--title", "Test Show", "--episodes-json", '["1", "2"]']) == 0
    conn = initialize()
    anime = get_anime(conn, "Test Show")
    assert anime is not None
    assert anime["available_episode_count"] == 2
    assert [row["episode_key"] for row in episodes_for_anime(conn, anime["id"])] == ["1", "2"]


def test_hook_reuses_existing_season_alias_title(app_env):
    conn = initialize()
    existing, _ = get_or_create_anime(conn, "Farming Life in Another World 2 (Isekai Nonbiri Nouka 2)")
    update_anime_fields(conn, existing["id"], anilist_id=197824)

    assert run(
        [
            "episodes-listed",
            "--title",
            "Farming Life in Another World Season 2 (Isekai Nonbiri Nouka 2) (7 episodes)",
            "--source-title",
            "Farming Life in Another World Season 2 (Isekai Nonbiri Nouka 2) (7 episodes)",
            "--episodes-json",
            '["1", "2"]',
        ]
    ) == 0

    rows = list_anime(conn)
    assert len(rows) == 1
    assert rows[0]["id"] == existing["id"]
    assert rows[0]["available_episode_count"] == 2


def test_hook_success_marks_watched_without_duration_threshold(app_env):
    run(["playback-started", "--title", "Test Show", "--episode", "1"])
    run(
        [
            "playback-finished",
            "--title",
            "Test Show",
            "--episode",
            "1",
            "--exit-code",
            "0",
            "--duration-seconds",
            "10",
        ]
    )
    conn = initialize()
    anime = get_anime(conn, "Test Show")
    episode = episodes_for_anime(conn, anime["id"])[0]
    assert episode["first_started_at"] is not None
    assert episode["watched"] == 1
    assert episode["watched_at"] is not None


def test_configured_threshold_can_leave_short_playback_unwatched(app_env):
    set_config_value("tracking.mark_watched_after_seconds", "120")

    run(["playback-started", "--title", "Test Show", "--episode", "1"])
    run(
        [
            "playback-finished",
            "--title",
            "Test Show",
            "--episode",
            "1",
            "--exit-code",
            "0",
            "--duration-seconds",
            "10",
        ]
    )

    conn = initialize()
    anime = get_anime(conn, "Test Show")
    episode = episodes_for_anime(conn, anime["id"])[0]
    assert episode["watched"] == 0


def test_playback_finished_does_not_replace_started_timestamp(app_env):
    run(["playback-started", "--title", "Test Show", "--episode", "1"])
    conn = initialize()
    anime = get_anime(conn, "Test Show")
    before = episodes_for_anime(conn, anime["id"])[0]["last_started_at"]

    run(
        [
            "playback-finished",
            "--title",
            "Test Show",
            "--episode",
            "1",
            "--exit-code",
            "0",
            "--duration-seconds",
            "10",
        ]
    )

    episode = episodes_for_anime(conn, anime["id"])[0]
    assert episode["last_started_at"] == before
