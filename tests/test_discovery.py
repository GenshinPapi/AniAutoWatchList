from __future__ import annotations

from ani_watchlist.discovery import (
    SCHEDULE_CACHE_KEY,
    TRENDING_CACHE_KEY,
    cache_fetched_today,
    load_discovery,
    refresh_schedule,
    refresh_trending,
)
from ani_watchlist.config import AniListConfig, AppConfig
from ani_watchlist.db import initialize
from ani_watchlist.providers.anilist import AniListProvider


class FakeDiscoveryProvider(AniListProvider):
    def get_trending_anime(self, limit: int = 20):  # noqa: ANN201
        return [
            {
                "id": 21,
                "title": {"english": "ONE PIECE", "romaji": "ONE PIECE", "userPreferred": "ONE PIECE"},
                "episodes": None,
                "status": "RELEASING",
                "trending": 100,
                "averageScore": 88,
                "coverImage": {"large": "https://example.invalid/one-piece.jpg"},
                "siteUrl": "https://anilist.co/anime/21",
            }
        ][:limit]

    def get_airing_schedule(self, start_timestamp: int, end_timestamp: int, *, limit: int = 140):  # noqa: ANN201
        return [
            {
                "id": 1,
                "airingAt": start_timestamp + 3600,
                "episode": 3,
                "mediaId": 21,
                "media": self.get_trending_anime(1)[0],
            }
        ]

    def cache_cover(self, media_id: str, url: str) -> str:
        return f"/tmp/anilist-{media_id}.jpg"


TEST_CONFIG = AppConfig(anilist=AniListConfig(enabled=True))


def test_refresh_trending_caches_once_per_day(app_env) -> None:
    conn = initialize()
    payload = refresh_trending(conn, TEST_CONFIG, force=True, provider=FakeDiscoveryProvider(), limit=10)

    assert payload["items"][0]["display_title"] == "ONE PIECE"
    assert payload["items"][0]["cover_path"] == "/tmp/anilist-21.jpg"
    assert cache_fetched_today(conn, TRENDING_CACHE_KEY) is True

    discovery = load_discovery(conn)
    assert discovery["trending"]["items"][0]["id"] == 21


def test_refresh_schedule_caches_local_day(app_env) -> None:
    conn = initialize()
    payload = refresh_schedule(conn, TEST_CONFIG, force=True, provider=FakeDiscoveryProvider(), days=7)

    assert payload["items"][0]["episode"] == 3
    assert payload["items"][0]["media"]["display_title"] == "ONE PIECE"
    assert payload["items"][0]["local_day"] != "-"
    assert cache_fetched_today(conn, SCHEDULE_CACHE_KEY) is True
