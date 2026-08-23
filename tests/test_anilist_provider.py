from __future__ import annotations

import io
import urllib.error

import pytest

from ani_watchlist.config import AniListConfig
from ani_watchlist.providers.anilist import (
    AniListProvider,
    _AniListCircuitBreaker,
    _AniListRateLimiter,
    display_title_from_media,
    search_title_variants,
)


def test_search_title_variants_split_parenthetical_titles() -> None:
    assert search_title_variants("Frieren: Beyond Journey's End (Sousou no Frieren)") == [
        "Frieren: Beyond Journey's End (Sousou no Frieren)",
        "Frieren: Beyond Journey's End",
        "Sousou no Frieren",
    ]


def test_search_uses_title_variants_and_deduplicates(app_env) -> None:
    calls = []
    payload = {
        "id": 21,
        "title": {"english": "ONE PIECE", "romaji": "ONE PIECE", "userPreferred": "ONE PIECE"},
        "synonyms": [],
        "episodes": None,
        "coverImage": {},
    }

    class FakeAniListProvider(AniListProvider):
        def _request(self, query, variables):  # noqa: ANN001
            assert "isAdult" in query
            assert "format" in query
            assert "averageScore" in query
            assert "isAdult:" not in query
            calls.append(variables["search"])
            if variables["search"] == "One Piece":
                return {"Page": {"media": [payload]}}
            return {"Page": {"media": []}}

    matches = FakeAniListProvider().search_title("One Piece (1P)")

    assert calls[:2] == ["One Piece (1P)", "One Piece"]
    assert len(matches) == 1
    assert matches[0].media_id == "21"
    assert matches[0].confidence_score >= 0.9


def test_metadata_queries_request_anilist_average_score(app_env) -> None:
    class FakeAniListProvider(AniListProvider):
        def _request(self, query, variables):  # noqa: ANN001
            assert "averageScore" in query
            if "id_in" in query:
                return {"Page": {"media": [{"id": media_id, "averageScore": 80} for media_id in variables["ids"]]}}
            return {"Media": {"id": variables["id"], "averageScore": 88}}

    provider = FakeAniListProvider()

    assert provider.get_media("21")["averageScore"] == 88
    assert [item["averageScore"] for item in provider.get_media_batch([21, 22])] == [80, 80]


def test_trending_query_returns_media_payloads(app_env) -> None:
    class FakeAniListProvider(AniListProvider):
        def _request(self, query, variables):  # noqa: ANN001
            assert "TRENDING_DESC" in query
            assert variables == {"page": 1, "perPage": 2}
            return {"Page": {"media": [{"id": 1}, {"id": 2}, {"id": 3}]}}

    assert FakeAniListProvider().get_trending_anime(limit=2) == [{"id": 1}, {"id": 2}]


def test_request_includes_anilist_http_error_body(app_env, monkeypatch) -> None:
    body = (
        b'{"errors":[{"message":"The AniList API has been temporarily disabled due to severe stability issues.",'
        b'"status":403}]}'
    )

    def fake_urlopen(request, timeout):  # noqa: ANN001
        raise urllib.error.HTTPError(request.full_url, 403, "Forbidden", None, io.BytesIO(body))

    monkeypatch.setattr("ani_watchlist.providers.anilist.urllib.request.urlopen", fake_urlopen)
    monkeypatch.setattr("ani_watchlist.providers.anilist._ANILIST_CIRCUIT_BREAKER", _AniListCircuitBreaker())

    with pytest.raises(RuntimeError, match="temporarily disabled"):
        AniListProvider().get_trending_anime(limit=1)


def test_temporary_403_opens_anilist_circuit_breaker(app_env, monkeypatch) -> None:
    now = 100.0
    breaker = _AniListCircuitBreaker(clock=lambda: now)
    limiter = _AniListRateLimiter(clock=lambda: now, sleeper=lambda seconds: None, safety_seconds=0)
    calls = 0
    body = (
        b'{"errors":[{"message":"The AniList API has been temporarily disabled due to severe stability issues.",'
        b'"status":403}]}'
    )

    def fake_urlopen(request, timeout):  # noqa: ANN001
        nonlocal calls
        calls += 1
        raise urllib.error.HTTPError(request.full_url, 403, "Forbidden", None, io.BytesIO(body))

    monkeypatch.setattr("ani_watchlist.providers.anilist.urllib.request.urlopen", fake_urlopen)
    monkeypatch.setattr("ani_watchlist.providers.anilist._ANILIST_CIRCUIT_BREAKER", breaker)
    monkeypatch.setattr("ani_watchlist.providers.anilist._ANILIST_RATE_LIMITER", limiter)
    provider = AniListProvider(AniListConfig(temporary_block_cooldown_seconds=300))

    with pytest.raises(RuntimeError, match="temporarily disabled"):
        provider.get_trending_anime(limit=1)
    assert breaker.remaining_seconds() == pytest.approx(300)

    with pytest.raises(RuntimeError, match="request skipped"):
        provider.get_trending_anime(limit=1)
    assert calls == 1


def test_rate_limiter_spaces_anilist_requests_below_30_per_minute() -> None:
    now = 100.0
    sleeps: list[float] = []

    def clock() -> float:
        return now

    def sleeper(seconds: float) -> None:
        nonlocal now
        sleeps.append(seconds)
        now += seconds

    limiter = _AniListRateLimiter(clock=clock, sleeper=sleeper, safety_seconds=0.05)

    limiter.wait(30)
    limiter.wait(30)
    limiter.wait(30)

    assert sleeps == pytest.approx([2.05, 2.05])


def test_search_anime_media_uses_search_match_query(app_env) -> None:
    class FakeAniListProvider(AniListProvider):
        def _request(self, query, variables):  # noqa: ANN001
            assert "SEARCH_MATCH" in query
            assert variables == {"page": 1, "perPage": 3, "search": "One Piece"}
            return {"Page": {"media": [{"id": 21}, {"id": 22}, {"id": 23}, {"id": 24}]}}

    assert FakeAniListProvider().search_anime_media("One Piece", limit=3) == [{"id": 21}, {"id": 22}, {"id": 23}]


def test_get_related_anime_filters_season_relations(app_env) -> None:
    class FakeAniListProvider(AniListProvider):
        def _request(self, query, variables):  # noqa: ANN001
            assert "relations" in query
            assert "relationType(version: 2)" in query
            if variables != {"id": 101280}:
                return {"Media": {"relations": {"edges": []}}}
            return {
                "Media": {
                    "relations": {
                        "edges": [
                            {"relationType": "SEQUEL", "node": {"id": 116742, "type": "ANIME", "title": {"romaji": "Season 2"}}},
                            {"relationType": "ADAPTATION", "node": {"id": 1, "type": "MANGA", "title": {"romaji": "Manga"}}},
                            {"relationType": "CHARACTER", "node": {"id": 2, "type": "ANIME", "title": {"romaji": "Character"}}},
                        ]
                    }
                }
            }

    related = FakeAniListProvider().get_related_anime(101280)

    assert related == [{"id": 116742, "type": "ANIME", "title": {"romaji": "Season 2"}, "relationType": "SEQUEL"}]


def test_get_related_anime_follows_sequel_chain(app_env) -> None:
    class FakeAniListProvider(AniListProvider):
        def _request(self, query, variables):  # noqa: ANN001
            assert "relations" in query
            edges_by_id = {
                101280: [
                    {"relationType": "SIDE_STORY", "node": {"id": 106509, "type": "ANIME", "title": {"romaji": "OVA"}}},
                    {"relationType": "SEQUEL", "node": {"id": 161802, "type": "ANIME", "title": {"romaji": "Visions"}}},
                ],
                161802: [
                    {"relationType": "PREQUEL", "node": {"id": 101280, "type": "ANIME", "title": {"romaji": "Season 1"}}},
                    {"relationType": "SEQUEL", "node": {"id": 108511, "type": "ANIME", "title": {"romaji": "Season 2"}}},
                ],
                108511: [
                    {"relationType": "SEQUEL", "node": {"id": 116742, "type": "ANIME", "title": {"romaji": "Season 2 Part 2"}}},
                ],
                116742: [],
            }
            return {"Media": {"relations": {"edges": edges_by_id.get(variables["id"], [])}}}

    related = FakeAniListProvider().get_related_anime(101280)

    assert [item["id"] for item in related] == [106509, 161802, 108511, 116742]
    assert [item["relationType"] for item in related] == ["SIDE_STORY", "SEQUEL", "SEQUEL", "SEQUEL"]


def test_popular_query_can_filter_currently_airing(app_env) -> None:
    class FakeAniListProvider(AniListProvider):
        def _request(self, query, variables):  # noqa: ANN001
            assert "POPULARITY_DESC" in query
            assert "status: RELEASING" in query
            assert "isAdult:" not in query
            assert variables == {"page": 1, "perPage": 2}
            return {"Page": {"media": [{"id": 21}, {"id": 22}, {"id": 23}]}}

    assert FakeAniListProvider().get_top_airing_anime(limit=2) == [{"id": 21}, {"id": 22}]


def test_popular_query_all_time_omits_status_filter(app_env) -> None:
    class FakeAniListProvider(AniListProvider):
        def _request(self, query, variables):  # noqa: ANN001
            assert "POPULARITY_DESC" in query
            assert "status:" not in query
            assert "isAdult:" not in query
            assert variables == {"page": 1, "perPage": 1}
            return {"Page": {"media": [{"id": 21}, {"id": 22}]}}

    assert FakeAniListProvider().get_popular_anime(limit=1) == [{"id": 21}]


def test_popular_query_can_filter_by_genre(app_env) -> None:
    class FakeAniListProvider(AniListProvider):
        def _request(self, query, variables):  # noqa: ANN001
            assert "genre_in: $genreIn" in query
            assert variables == {"page": 1, "perPage": 2, "genreIn": ["Action"]}
            return {"Page": {"media": [{"id": 31}, {"id": 32}, {"id": 33}]}}

    assert FakeAniListProvider().get_popular_anime(limit=2, genre="Action") == [{"id": 31}, {"id": 32}]


def test_popular_query_can_filter_by_tag(app_env) -> None:
    class FakeAniListProvider(AniListProvider):
        def _request(self, query, variables):  # noqa: ANN001
            assert "tag_in: $tagIn" in query
            assert variables == {"page": 1, "perPage": 2, "tagIn": ["Isekai"]}
            return {"Page": {"media": [{"id": 34}, {"id": 35}, {"id": 36}]}}

    assert FakeAniListProvider().get_popular_anime(limit=2, tag="Isekai") == [{"id": 34}, {"id": 35}]


def test_popular_query_pages_until_requested_limit(app_env) -> None:
    calls = []

    class FakeAniListProvider(AniListProvider):
        def _request(self, query, variables):  # noqa: ANN001
            calls.append(variables)
            page = variables["page"]
            return {
                "Page": {
                    "pageInfo": {"hasNextPage": page == 1},
                    "media": [{"id": page * 100 + idx} for idx in range(50)],
                }
            }

    rows = FakeAniListProvider().get_popular_anime(limit=51)

    assert calls == [{"page": 1, "perPage": 50}, {"page": 2, "perPage": 50}]
    assert len(rows) == 51
    assert rows[-1]["id"] == 200


def test_media_batch_returns_next_page_for_incremental_loading(app_env) -> None:
    calls = []

    class FakeAniListProvider(AniListProvider):
        def _request(self, query, variables):  # noqa: ANN001
            assert "TRENDING_DESC" in query
            calls.append(variables)
            page = variables["page"]
            return {
                "Page": {
                    "pageInfo": {"hasNextPage": page < 4, "currentPage": page},
                    "media": [{"id": page * 100 + idx} for idx in range(variables["perPage"])],
                }
            }

    batch = FakeAniListProvider().get_trending_anime_batch(start_page=3, page_count=2, per_page=50)

    assert calls == [{"page": 3, "perPage": 50}, {"page": 4, "perPage": 50}]
    assert len(batch["items"]) == 100
    assert batch["next_page"] is None


def test_popular_media_batch_passes_genre_filter(app_env) -> None:
    class FakeAniListProvider(AniListProvider):
        def _request(self, query, variables):  # noqa: ANN001
            assert "genre_in: $genreIn" in query
            assert variables == {"page": 2, "perPage": 25, "genreIn": ["Romance"]}
            return {
                "Page": {
                    "pageInfo": {"hasNextPage": True, "currentPage": 2},
                    "media": [{"id": 41}],
                }
            }

    batch = FakeAniListProvider().get_popular_anime_batch(
        start_page=2,
        page_count=1,
        per_page=25,
        genre="Romance",
    )

    assert batch == {"items": [{"id": 41}], "next_page": 3}


def test_popular_media_batch_passes_tag_filter(app_env) -> None:
    class FakeAniListProvider(AniListProvider):
        def _request(self, query, variables):  # noqa: ANN001
            assert "tag_in: $tagIn" in query
            assert variables == {"page": 2, "perPage": 25, "tagIn": ["Isekai"]}
            return {
                "Page": {
                    "pageInfo": {"hasNextPage": True, "currentPage": 2},
                    "media": [{"id": 42}],
                }
            }

    batch = FakeAniListProvider().get_popular_anime_batch(
        start_page=2,
        page_count=1,
        per_page=25,
        tag="Isekai",
    )

    assert batch == {"items": [{"id": 42}], "next_page": 3}


def test_display_title_labels_adult_and_explicit_uncensored_variants() -> None:
    adult = {
        "title": {"english": "Bible Black", "romaji": "Bible Black"},
        "isAdult": True,
        "synonyms": [],
    }
    uncensored = {
        "title": {"english": "Example Show", "romaji": "Example Show"},
        "isAdult": False,
        "synonyms": ["Example Show Uncensored"],
    }

    assert display_title_from_media(adult) == "Bible Black [18+]"
    assert display_title_from_media(uncensored) == "Example Show [Uncensored]"


def test_airing_schedule_pages_until_limit(app_env) -> None:
    calls = []

    class FakeAniListProvider(AniListProvider):
        def _request(self, query, variables):  # noqa: ANN001
            assert "airingSchedules" in query
            calls.append(variables["page"])
            return {
                "Page": {
                    "pageInfo": {"hasNextPage": variables["page"] == 1},
                    "airingSchedules": [{"id": variables["page"]}],
                }
            }

    rows = FakeAniListProvider().get_airing_schedule(100, 200, limit=51)

    assert calls == [1, 2]
    assert rows == [{"id": 1}, {"id": 2}]
