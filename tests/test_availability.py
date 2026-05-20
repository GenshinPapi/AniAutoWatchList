from __future__ import annotations

from ani_watchlist import availability
from ani_watchlist.db import initialize
from ani_watchlist.launcher import AllAnimeEpisodeAvailability, AllAnimeLaunchTarget
from ani_watchlist.store import episodes_for_anime, get_anime_by_id, get_or_create_anime


def test_refresh_available_episodes_for_anime_upserts_allanime_rows(app_env, monkeypatch) -> None:
    conn = initialize()
    anime, _created = get_or_create_anime(conn, "Test Show", status="plan_to_watch")

    def fake_available_episode_keys(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        return AllAnimeEpisodeAvailability(
            target=AllAnimeLaunchTarget(
                show_id="show-id",
                title="Test Show (3 episodes)",
                episode_count=3,
                score=100.0,
                query="Test Show",
            ),
            episode_keys=("1", "2", "3"),
        )

    monkeypatch.setattr(availability, "allanime_available_episode_keys", fake_available_episode_keys)

    payload = availability.refresh_available_episodes_for_anime(conn, anime["id"])

    updated = get_anime_by_id(conn, anime["id"])
    assert payload["updated"] is True
    assert payload["episode_count"] == 3
    assert updated["available_episode_count"] == 3
    assert [row["episode_key"] for row in episodes_for_anime(conn, anime["id"])] == ["1", "2", "3"]


def test_refresh_available_episodes_for_anime_leaves_unmatched_titles_unchanged(app_env, monkeypatch) -> None:
    conn = initialize()
    anime, _created = get_or_create_anime(conn, "Missing Show", status="plan_to_watch")
    monkeypatch.setattr(availability, "allanime_available_episode_keys", lambda *args, **kwargs: None)

    payload = availability.refresh_available_episodes_for_anime(conn, anime["id"])

    updated = get_anime_by_id(conn, anime["id"])
    assert payload["updated"] is False
    assert updated["available_episode_count"] is None
    assert episodes_for_anime(conn, anime["id"]) == []
