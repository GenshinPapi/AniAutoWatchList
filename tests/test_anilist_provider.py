from __future__ import annotations

from ani_watchlist.providers.anilist import AniListProvider, display_title_from_media, search_title_variants


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


def test_trending_query_returns_media_payloads(app_env) -> None:
    class FakeAniListProvider(AniListProvider):
        def _request(self, query, variables):  # noqa: ANN001
            assert "TRENDING_DESC" in query
            assert variables == {"page": 1, "perPage": 2}
            return {"Page": {"media": [{"id": 1}, {"id": 2}, {"id": 3}]}}

    assert FakeAniListProvider().get_trending_anime(limit=2) == [{"id": 1}, {"id": 2}]


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
