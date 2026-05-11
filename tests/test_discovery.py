from __future__ import annotations

from ani_watchlist.discovery import (
    POPULAR_CACHE_KEY,
    SCHEDULE_CACHE_KEY,
    TOP_AIRING_CACHE_KEY,
    TRENDING_CACHE_KEY,
    cache_fetched_today,
    load_discovery,
    refresh_discovery,
    refresh_popular,
    refresh_schedule,
    refresh_top_airing,
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

    def get_top_airing_anime(self, limit: int = 20):  # noqa: ANN201
        return [
            {
                **self.get_trending_anime(1)[0],
                "id": 22,
                "title": {"english": "Frieren", "romaji": "Sousou no Frieren", "userPreferred": "Sousou no Frieren"},
                "popularity": 999,
            }
        ][:limit]

    def get_popular_anime(self, limit: int = 20, *, status: str | None = None):  # noqa: ANN201
        return [
            {
                **self.get_trending_anime(1)[0],
                "id": 23,
                "title": {"english": "Fullmetal Alchemist: Brotherhood", "romaji": "Hagane no Renkinjutsushi", "userPreferred": "Hagane no Renkinjutsushi"},
                "status": status or "FINISHED",
                "popularity": 9999,
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


def test_refresh_top_airing_and_popular_cache_once_per_day(app_env) -> None:
    conn = initialize()
    provider = FakeDiscoveryProvider()

    top_airing = refresh_top_airing(conn, TEST_CONFIG, force=True, provider=provider, limit=10)
    popular = refresh_popular(conn, TEST_CONFIG, force=True, provider=provider, limit=10)

    assert top_airing["items"][0]["display_title"] == "Frieren (Sousou no Frieren)"
    assert popular["items"][0]["display_title"] == "Fullmetal Alchemist: Brotherhood (Hagane no Renkinjutsushi)"
    assert cache_fetched_today(conn, TOP_AIRING_CACHE_KEY) is True
    assert cache_fetched_today(conn, POPULAR_CACHE_KEY) is True

    discovery = load_discovery(conn)
    assert discovery["top_airing"]["items"][0]["id"] == 22
    assert discovery["popular"]["items"][0]["id"] == 23


def test_refresh_discovery_populates_all_discovery_tabs(app_env) -> None:
    conn = initialize()

    payload = refresh_discovery(conn, TEST_CONFIG, force=True, provider=FakeDiscoveryProvider())

    assert sorted(payload) == ["popular", "schedule", "top_airing", "trending"]
    discovery = load_discovery(conn)
    assert discovery["trending_fresh"] is True
    assert discovery["top_airing_fresh"] is True
    assert discovery["popular_fresh"] is True
    assert discovery["schedule_fresh"] is True
