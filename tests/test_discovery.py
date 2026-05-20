from __future__ import annotations

from ani_watchlist.discovery import (
    MEDIA_LIST_BATCH_LIMIT,
    POPULAR_CACHE_KEY,
    SCHEDULE_CACHE_KEY,
    TOP_AIRING_CACHE_KEY,
    TRENDING_CACHE_KEY,
    append_discovery_media_page,
    cache_fetched_today,
    load_discovery,
    normalize_popular_filter,
    normalize_popular_genre,
    popular_filter_label,
    popular_cache_key,
    refresh_discovery,
    refresh_popular,
    refresh_schedule,
    search_media,
    refresh_top_airing,
    refresh_trending,
)
from ani_watchlist.config import AniListConfig, AppConfig
from ani_watchlist.db import initialize
from ani_watchlist.providers.anilist import AniListProvider


class FakeDiscoveryProvider(AniListProvider):
    def _media_items(
        self,
        kind: str,
        start: int,
        count: int,
        genre: str | None = None,
        tag: str | None = None,
    ) -> list[dict[str, object]]:
        base = {
            "trending": (21, "ONE PIECE", "ONE PIECE", "RELEASING", 100),
            "top_airing": (22, "Frieren", "Sousou no Frieren", "RELEASING", 999),
            "popular": (23, "Fullmetal Alchemist: Brotherhood", "Hagane no Renkinjutsushi", "FINISHED", 9999),
        }[kind]
        media_id, english, romaji, status, metric = base
        items: list[dict[str, object]] = []
        for offset in range(count):
            filter_offset = 5000 if genre else 8000 if tag else 0
            current_id = media_id + filter_offset + start + offset
            items.append(
                {
                    "id": current_id,
                    "title": {"english": english, "romaji": romaji, "userPreferred": romaji},
                    "episodes": None,
                    "status": status,
                    "trending": metric if kind == "trending" else None,
                    "popularity": metric if kind != "trending" else None,
                    "averageScore": 88,
                    "coverImage": {"large": f"https://example.invalid/{current_id}.jpg"},
                    "siteUrl": f"https://anilist.co/anime/{current_id}",
                }
            )
        return items

    def get_trending_anime(self, limit: int = 20):  # noqa: ANN201
        return self._media_items("trending", 0, limit)

    def get_top_airing_anime(self, limit: int = 20):  # noqa: ANN201
        return self._media_items("top_airing", 0, limit)

    def get_popular_anime(self, limit: int = 20):  # noqa: ANN201
        return self._media_items("popular", 0, limit)

    def get_trending_anime_batch(self, *, start_page: int = 1, page_count: int = 2, per_page: int = 50):  # noqa: ANN201
        start = (start_page - 1) * per_page
        count = page_count * per_page
        return {"items": self._media_items("trending", start, count), "next_page": start_page + page_count}

    def get_top_airing_anime_batch(self, *, start_page: int = 1, page_count: int = 2, per_page: int = 50):  # noqa: ANN201
        start = (start_page - 1) * per_page
        count = page_count * per_page
        return {"items": self._media_items("top_airing", start, count), "next_page": start_page + page_count}

    def get_popular_anime_batch(
        self,
        *,
        start_page: int = 1,
        page_count: int = 2,
        per_page: int = 50,
        genre: str | None = None,
        tag: str | None = None,
    ):  # noqa: ANN201
        start = (start_page - 1) * per_page
        count = page_count * per_page
        return {"items": self._media_items("popular", start, count, genre, tag), "next_page": start_page + page_count}

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

    def search_anime_media(self, search: str, limit: int = 50):  # noqa: ANN201
        assert search == "One Piece"
        return self._media_items("trending", 0, min(limit, 3))

    def cache_cover(self, media_id: str, url: str) -> str:
        return f"/tmp/anilist-{media_id}.jpg"


TEST_CONFIG = AppConfig(anilist=AniListConfig(enabled=True))


def test_refresh_trending_caches_once_per_day(app_env) -> None:
    conn = initialize()
    payload = refresh_trending(conn, TEST_CONFIG, force=True, provider=FakeDiscoveryProvider(), limit=10)

    assert payload["items"][0]["display_title"] == "ONE PIECE"
    assert payload["items"][0]["metadata_payload"]["title"]["romaji"] == "ONE PIECE"
    assert payload["items"][0]["cover_path"] == "/tmp/anilist-21.jpg"
    assert len(payload["items"]) == 10
    assert payload["has_more"] is True
    assert payload["next_page"] == 2
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


def test_search_media_normalizes_anilist_search_results(app_env) -> None:
    payload = search_media("One Piece", TEST_CONFIG, provider=FakeDiscoveryProvider(), limit=2)

    assert payload["query"] == "One Piece"
    assert payload["error"] is None
    assert len(payload["items"]) == 2
    assert payload["items"][0]["display_title"] == "ONE PIECE"
    assert payload["items"][0]["cover_path"] == "/tmp/anilist-21.jpg"


def test_search_media_can_skip_cover_cache_for_suggestions(app_env) -> None:
    payload = search_media("One Piece", TEST_CONFIG, provider=FakeDiscoveryProvider(), limit=2, cache_covers=False)

    assert len(payload["items"]) == 2
    assert payload["items"][0]["cover_path"] is None


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


def test_refresh_popular_uses_separate_cache_for_genre(app_env) -> None:
    conn = initialize()
    provider = FakeDiscoveryProvider()

    default = refresh_popular(conn, TEST_CONFIG, force=True, provider=provider, limit=10)
    action = refresh_popular(conn, TEST_CONFIG, force=True, provider=provider, limit=10, genre="action")

    assert normalize_popular_genre("action") == "Action"
    assert popular_cache_key(None) == POPULAR_CACHE_KEY
    assert popular_cache_key("Action") != POPULAR_CACHE_KEY
    assert default["genre"] is None
    assert action["genre"] == "Action"
    assert default["items"][0]["id"] == 23
    assert action["items"][0]["id"] == 5023
    assert load_discovery(conn)["popular"]["items"][0]["id"] == 23
    assert load_discovery(conn, popular_genre="Action")["popular"]["items"][0]["id"] == 5023


def test_refresh_popular_can_filter_by_genre_like_tag(app_env) -> None:
    conn = initialize()
    provider = FakeDiscoveryProvider()

    payload = refresh_popular(conn, TEST_CONFIG, force=True, provider=provider, limit=10, genre="isekai")

    assert normalize_popular_genre("Isekai") is None
    assert normalize_popular_filter("isekai") == ("tag", "Isekai")
    assert popular_filter_label("isekai") == "Isekai"
    assert popular_cache_key("Isekai") != POPULAR_CACHE_KEY
    assert payload["genre"] is None
    assert payload["tag"] == "Isekai"
    assert payload["filter_type"] == "tag"
    assert payload["filter"] == "Isekai"
    assert payload["items"][0]["id"] == 8023
    assert load_discovery(conn, popular_genre="Isekai")["popular"]["items"][0]["id"] == 8023


def test_refresh_discovery_populates_all_discovery_tabs(app_env) -> None:
    conn = initialize()

    payload = refresh_discovery(conn, TEST_CONFIG, force=True, provider=FakeDiscoveryProvider())

    assert sorted(payload) == ["popular", "schedule", "top_airing", "trending"]
    discovery = load_discovery(conn)
    assert discovery["trending_fresh"] is True
    assert discovery["top_airing_fresh"] is True
    assert discovery["popular_fresh"] is True
    assert discovery["schedule_fresh"] is True


def test_refresh_discovery_uses_hundred_item_media_batches(app_env) -> None:
    conn = initialize()

    payload = refresh_discovery(conn, TEST_CONFIG, force=True, provider=FakeDiscoveryProvider())

    assert len(payload["trending"]["items"]) == MEDIA_LIST_BATCH_LIMIT
    assert len(payload["top_airing"]["items"]) == MEDIA_LIST_BATCH_LIMIT
    assert len(payload["popular"]["items"]) == MEDIA_LIST_BATCH_LIMIT
    assert payload["trending"]["next_page"] == 3
    assert payload["top_airing"]["next_page"] == 3
    assert payload["popular"]["next_page"] == 3


def test_append_discovery_media_page_extends_existing_cache(app_env) -> None:
    conn = initialize()
    provider = FakeDiscoveryProvider()
    first = refresh_trending(conn, TEST_CONFIG, force=True, provider=provider)

    second = append_discovery_media_page(conn, "trending", TEST_CONFIG, provider=provider)

    assert len(first["items"]) == MEDIA_LIST_BATCH_LIMIT
    assert len(second["items"]) == MEDIA_LIST_BATCH_LIMIT * 2
    assert second["items"][MEDIA_LIST_BATCH_LIMIT]["id"] == 121
    assert second["next_page"] == 5
