from __future__ import annotations

from ani_watchlist.db import initialize
from ani_watchlist.hook import run
from ani_watchlist.store import episodes_for_anime, get_anime


def test_fake_hook_playback_flow_marks_watched(app_env):
    run(["launch", "--argv-json", '["ani-cli", "Demo Show"]'])
    run(["title-selected", "--title", "Demo Show"])
    run(["episodes-listed", "--title", "Demo Show", "--episodes-json", '["1", "2", "3"]'])
    run(["playback-started", "--title", "Demo Show", "--episode", "2"])
    run(
        [
            "playback-finished",
            "--title",
            "Demo Show",
            "--episode",
            "2",
            "--exit-code",
            "0",
            "--duration-seconds",
            "130",
        ]
    )

    conn = initialize()
    anime = get_anime(conn, "Demo Show")
    assert anime is not None
    episodes = {row["episode_key"]: row for row in episodes_for_anime(conn, anime["id"])}
    assert episodes["2"]["watched"] == 1
    events = list(conn.execute("SELECT event_type FROM watch_events ORDER BY id"))
    assert [row["event_type"] for row in events] == [
        "launch",
        "title_selected",
        "episodes_listed",
        "playback_started",
        "playback_finished",
    ]
