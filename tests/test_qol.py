from __future__ import annotations

import json

from ani_watchlist.cli import main as cli_main
from ani_watchlist.config import get_config_value
from ani_watchlist.db import initialize
from ani_watchlist.hook import run
from ani_watchlist.store import (
    episodes_for_anime,
    get_anime,
    get_or_create_anime,
    likely_duplicates,
    merge_anime,
    repair_database,
    upsert_episodes,
    update_anime_fields,
)


def test_events_next_dashboard_and_config(app_env, capsys):
    run(["launch", "--argv-json", '["ani-cli", "QoL Show"]'])
    run(["episodes-listed", "--title", "QoL Show", "--episodes-json", '["1", "2"]'])
    run(["playback-started", "--title", "QoL Show", "--episode", "1"])
    run(
        [
            "playback-finished",
            "--title",
            "QoL Show",
            "--episode",
            "1",
            "--exit-code",
            "0",
            "--duration-seconds",
            "150",
        ]
    )

    assert cli_main(["events", "QoL Show"]) == 0
    assert "playback_finished" in capsys.readouterr().out
    assert cli_main(["next", "QoL Show"]) == 0
    assert "episode 2" in capsys.readouterr().out
    assert cli_main(["dashboard"]) == 0
    assert "Watched episodes" in capsys.readouterr().out
    assert cli_main(["config", "set", "tracking.mark_watched_after_seconds", "180"]) == 0
    assert get_config_value("tracking.mark_watched_after_seconds") == 180


def test_duplicates_and_merge(app_env):
    conn = initialize()
    target, _ = get_or_create_anime(conn, "Merge Show")
    source, _ = get_or_create_anime(conn, "Merge Show TV")
    upsert_episodes(conn, target["id"], ["1"])
    upsert_episodes(conn, source["id"], ["2"])

    assert likely_duplicates(conn)
    merge_anime(conn, target["id"], source["id"])

    merged = get_anime(conn, "Merge Show")
    episodes = episodes_for_anime(conn, merged["id"])
    assert [episode["episode_key"] for episode in episodes] == ["1", "2"]
    assert get_anime(conn, "Merge Show TV") is None


def test_import_json_preserves_progress(app_env, tmp_path):
    data = {
        "anime": [
            {
                "id": 1,
                "display_title": "Imported Show",
                "source_title": "Imported Show",
                "status": "completed",
                "anilist_id": 123,
                "total_episodes": 1,
                "cover_url": None,
                "cover_path": None,
                "notes": "done",
                "last_watched_at": "2026-05-04T00:00:00+00:00",
            }
        ],
        "episodes": [
            {
                "id": 1,
                "anime_id": 1,
                "episode_key": "1",
                "episode_number": "1",
                "title": None,
                "watched": 1,
                "watched_at": "2026-05-04T00:00:00+00:00",
                "first_started_at": None,
                "last_started_at": None,
                "source_label": "test",
            }
        ],
    }
    path = tmp_path / "import.json"
    path.write_text(json.dumps(data), encoding="utf-8")

    assert cli_main(["import", str(path)]) == 0
    conn = initialize()
    anime = get_anime(conn, "Imported Show")
    assert anime["status"] == "completed"
    assert anime["anilist_id"] == 123
    assert episodes_for_anime(conn, anime["id"])[0]["watched"] == 1


def test_import_history_adds_watching_entry(app_env, tmp_path):
    history = tmp_path / "ani-hsts"
    history.write_text("1\tabc123\tTsue to Tsurugi no Wistoria (12 episodes)\n", encoding="utf-8")

    assert cli_main(["import-history", "--path", str(history), "--search", "Wistoria"]) == 0

    conn = initialize()
    anime = get_anime(conn, "Tsue to Tsurugi no Wistoria")
    assert anime is not None
    assert anime["status"] == "watching"
    assert anime["total_episodes"] == 12
    episodes = episodes_for_anime(conn, anime["id"])
    assert len(episodes) == 12
    assert episodes[0]["episode_key"] == "1"
    assert episodes[0]["watched"] == 1


def test_repair_restores_started_timestamps_from_events(app_env):
    run(["playback-started", "--title", "Repair Show", "--episode", "1"])
    conn = initialize()
    anime = get_anime(conn, "Repair Show")
    episode = episodes_for_anime(conn, anime["id"])[0]
    event_started = episode["last_started_at"]
    update_anime_fields(conn, anime["id"], notes="keep")
    conn.execute("UPDATE episodes SET first_started_at = ?, last_started_at = ? WHERE id = ?", ("2099-01-01T00:00:00+00:00", "2099-01-01T00:00:00+00:00", episode["id"]))

    report = repair_database(conn, fix=True)

    fixed = episodes_for_anime(conn, anime["id"])[0]
    assert report["started_timestamps_differ_from_events"] == 1
    assert fixed["first_started_at"] == event_started
    assert fixed["last_started_at"] == event_started
