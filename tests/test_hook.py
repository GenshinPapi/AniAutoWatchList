from __future__ import annotations

from ani_watchlist.db import initialize
from ani_watchlist.hook import run
from ani_watchlist.store import episodes_for_anime, get_anime


def test_hook_parses_episode_list(app_env):
    assert run(["episodes-listed", "--title", "Test Show", "--episodes-json", '["1", "2"]']) == 0
    conn = initialize()
    anime = get_anime(conn, "Test Show")
    assert anime is not None
    assert anime["available_episode_count"] == 2
    assert [row["episode_key"] for row in episodes_for_anime(conn, anime["id"])] == ["1", "2"]


def test_hook_short_success_does_not_mark_watched(app_env):
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
