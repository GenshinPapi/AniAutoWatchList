from __future__ import annotations

from ani_watchlist.providers.anilist import AniListProvider, search_title_variants


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
