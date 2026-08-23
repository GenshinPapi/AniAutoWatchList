from __future__ import annotations

import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from ..config import AniListConfig
from ..paths import get_paths
from .base import MetadataSearchResult


SEARCH_QUERY = """
query ($search: String) {
  Page(page: 1, perPage: 8) {
    media(search: $search, type: ANIME, sort: SEARCH_MATCH) {
      id
      idMal
      title { romaji english native userPreferred }
      synonyms
      episodes
      status
      format
      isAdult
      averageScore
      coverImage { extraLarge large medium color }
      siteUrl
      externalLinks { id site url type language }
    }
  }
}
"""


SEARCH_MEDIA_QUERY = """
query ($page: Int, $perPage: Int, $search: String) {
  Page(page: $page, perPage: $perPage) {
    pageInfo { hasNextPage currentPage }
    media(type: ANIME, search: $search, sort: SEARCH_MATCH) {
      id
      idMal
      title { romaji english native userPreferred }
      synonyms
      episodes
      status
      format
      isAdult
      season
      seasonYear
      averageScore
      popularity
      trending
      coverImage { extraLarge large medium color }
      bannerImage
      siteUrl
      nextAiringEpisode { airingAt timeUntilAiring episode }
    }
  }
}
"""


RELATED_MEDIA_QUERY = """
query ($id: Int) {
  Media(id: $id, type: ANIME) {
    relations {
      edges {
        relationType(version: 2)
        node {
          id
          idMal
          type
          title { romaji english native userPreferred }
          synonyms
          episodes
          status
          format
          isAdult
          season
          seasonYear
          averageScore
          popularity
          trending
          coverImage { extraLarge large medium color }
          bannerImage
          siteUrl
          nextAiringEpisode { airingAt timeUntilAiring episode }
        }
      }
    }
  }
}
"""


MEDIA_QUERY = """
query ($id: Int) {
  Media(id: $id, type: ANIME) {
    id
    idMal
    title { romaji english native userPreferred }
    synonyms
    episodes
    status
    format
    isAdult
    averageScore
    description(asHtml: false)
    coverImage { extraLarge large medium color }
    siteUrl
    externalLinks { id site url type language }
    nextAiringEpisode { airingAt episode }
  }
}
"""

MEDIA_BATCH_QUERY = """
query ($ids: [Int], $page: Int, $perPage: Int) {
  Page(page: $page, perPage: $perPage) {
    media(id_in: $ids, type: ANIME) {
      id
      idMal
      episodes
      format
      averageScore
      externalLinks { id site url type language }
    }
  }
}
"""


TRENDING_QUERY = """
query ($page: Int, $perPage: Int) {
  Page(page: $page, perPage: $perPage) {
    pageInfo { hasNextPage currentPage }
    media(type: ANIME, sort: TRENDING_DESC) {
      id
      idMal
      title { romaji english native userPreferred }
      synonyms
      episodes
      status
      format
      isAdult
      season
      seasonYear
      averageScore
      popularity
      trending
      coverImage { extraLarge large medium color }
      bannerImage
      siteUrl
      nextAiringEpisode { airingAt timeUntilAiring episode }
    }
  }
}
"""


POPULAR_MEDIA_QUERY = """
query ($page: Int, $perPage: Int, $genreIn: [String], $tagIn: [String]) {
  Page(page: $page, perPage: $perPage) {
    pageInfo { hasNextPage currentPage }
    media(type: ANIME, sort: POPULARITY_DESC, genre_in: $genreIn, tag_in: $tagIn) {
      id
      idMal
      title { romaji english native userPreferred }
      synonyms
      episodes
      status
      format
      isAdult
      season
      seasonYear
      averageScore
      popularity
      trending
      coverImage { extraLarge large medium color }
      bannerImage
      siteUrl
      nextAiringEpisode { airingAt timeUntilAiring episode }
    }
  }
}
"""


TOP_AIRING_QUERY = """
query ($page: Int, $perPage: Int) {
  Page(page: $page, perPage: $perPage) {
    pageInfo { hasNextPage currentPage }
    media(type: ANIME, sort: POPULARITY_DESC, status: RELEASING) {
      id
      idMal
      title { romaji english native userPreferred }
      synonyms
      episodes
      status
      format
      isAdult
      season
      seasonYear
      averageScore
      popularity
      trending
      coverImage { extraLarge large medium color }
      bannerImage
      siteUrl
      nextAiringEpisode { airingAt timeUntilAiring episode }
    }
  }
}
"""


AIRING_SCHEDULE_QUERY = """
query ($page: Int, $perPage: Int, $start: Int, $end: Int) {
  Page(page: $page, perPage: $perPage) {
    pageInfo { hasNextPage currentPage }
    airingSchedules(
      airingAt_greater: $start,
      airingAt_lesser: $end,
      sort: TIME
    ) {
      id
      airingAt
      timeUntilAiring
      episode
      mediaId
      media {
        id
        idMal
        title { romaji english native userPreferred }
        synonyms
        episodes
        status
        format
        isAdult
        averageScore
        popularity
        trending
        coverImage { extraLarge large medium color }
        bannerImage
        siteUrl
        nextAiringEpisode { airingAt timeUntilAiring episode }
      }
    }
  }
}
"""


SEASON_RELATION_TYPES = {
    "PREQUEL",
    "SEQUEL",
    "PARENT",
    "SIDE_STORY",
    "SPIN_OFF",
}
SEASON_CHAIN_RELATION_TYPES = {
    "PREQUEL",
    "SEQUEL",
    "PARENT",
}


def _norm(text: str) -> str:
    text = text.casefold()
    text = re.sub(r"[^0-9a-z]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def search_title_variants(title: str) -> list[str]:
    title = re.sub(r"\s*\(\s*\d+(?:\.\d+)?\s+episodes?\s*\)\s*$", "", title.strip(), flags=re.I)
    title = re.sub(r"\s+", " ", title)
    variants: list[str] = []

    def add(value: str | None) -> None:
        value = re.sub(r"\s+", " ", (value or "").strip())
        if value and value.casefold() not in {item.casefold() for item in variants}:
            variants.append(value)

    add(title)
    if title.endswith(")") and " (" in title:
        primary, secondary = title.rsplit(" (", 1)
        add(primary)
        add(secondary[:-1])
    without_parenthetical = re.sub(r"\s*\([^)]*\)", "", title).strip()
    add(without_parenthetical)
    return variants[:4]


def _best_title(payload: dict[str, Any]) -> str:
    title = payload.get("title") or {}
    english = (title.get("english") or "").strip()
    romaji = (title.get("romaji") or "").strip()
    preferred = (title.get("userPreferred") or "").strip()
    native = (title.get("native") or "").strip()
    if english and romaji and english.casefold() != romaji.casefold():
        return title_with_content_labels(f"{english} ({romaji})", payload)
    return title_with_content_labels(english or preferred or romaji or native or "Unknown title", payload)


def display_title_from_media(payload: dict[str, Any]) -> str:
    return _best_title(payload)


def title_with_content_labels(title: str, payload: dict[str, Any]) -> str:
    labels = content_labels_from_media(payload)
    if not labels:
        return title
    existing = title.casefold()
    missing = [label for label in labels if label.casefold() not in existing]
    if not missing:
        return title
    return f"{title} {' '.join(f'[{label}]' for label in missing)}"


def content_labels_from_media(payload: dict[str, Any]) -> list[str]:
    texts: list[str] = []
    title = payload.get("title") or {}
    for value in (title.get("english"), title.get("romaji"), title.get("userPreferred"), title.get("native")):
        if value:
            texts.append(str(value))
    texts.extend(str(value) for value in payload.get("synonyms") or [] if value)
    joined = " ".join(texts)
    labels: list[str] = []
    if re.search(r"\buncensored\b", joined, flags=re.I):
        labels.append("Uncensored")
    elif re.search(r"(?<!un)\bcensored\b", joined, flags=re.I):
        labels.append("Censored")
    if payload.get("isAdult") is True and not re.search(r"(\b18\+\b|\badult\b)", joined, flags=re.I):
        labels.append("18+")
    return labels


def _candidate_titles(payload: dict[str, Any]) -> list[str]:
    title = payload.get("title") or {}
    values = [title.get("userPreferred"), title.get("english"), title.get("romaji"), title.get("native")]
    values.extend(payload.get("synonyms") or [])
    return [str(value) for value in values if value]


def confidence(query: str, payload: dict[str, Any]) -> float:
    q = _norm(query)
    if not q:
        return 0.0
    scores = []
    for title in _candidate_titles(payload):
        t = _norm(title)
        if not t:
            continue
        score = SequenceMatcher(None, q, t).ratio()
        if q == t:
            score = 1.0
        elif q in t or t in q:
            score = max(score, 0.9)
        scores.append(score)
    return max(scores) if scores else 0.0


def _popular_filter_variables(genre: str | None = None, tag: str | None = None) -> dict[str, Any]:
    variables: dict[str, Any] = {}
    cleaned = str(genre or "").strip()
    if cleaned:
        variables["genreIn"] = [cleaned]
    cleaned_tag = str(tag or "").strip()
    if cleaned_tag:
        variables["tagIn"] = [cleaned_tag]
    return variables


def _http_error_detail(exc: urllib.error.HTTPError) -> str:
    try:
        raw = exc.read().decode("utf-8", errors="replace")
    except Exception:
        raw = ""
    if raw:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = None
        errors = payload.get("errors") if isinstance(payload, dict) else None
        if isinstance(errors, list):
            messages = [
                str(error.get("message")).strip()
                for error in errors
                if isinstance(error, dict) and str(error.get("message") or "").strip()
            ]
            if messages:
                return f"HTTP Error {exc.code}: {'; '.join(messages)}"
        cleaned = re.sub(r"\s+", " ", raw).strip()
        if cleaned:
            return f"HTTP Error {exc.code}: {cleaned[:300]}"
    return str(exc)


def _retry_after_seconds(headers: Any) -> float | None:
    if headers is None:
        return None
    try:
        value = headers.get("Retry-After")
    except AttributeError:
        return None
    try:
        seconds = float(str(value or "").strip())
    except ValueError:
        return None
    return seconds if seconds > 0 else None


def _is_temporary_unavailable_error(status_code: int, detail: str) -> bool:
    if status_code != 403:
        return False
    return bool(
        re.search(
            r"temporar(?:ily|y).*(disabled|unavailable|blocked)|severe stability|ongoing attack",
            detail,
            flags=re.IGNORECASE,
        )
    )


class _AniListRateLimiter:
    def __init__(
        self,
        *,
        clock=time.monotonic,
        sleeper=time.sleep,
        safety_seconds: float = 0.05,
    ) -> None:
        self._clock = clock
        self._sleeper = sleeper
        self._safety_seconds = max(0.0, float(safety_seconds))
        self._lock = threading.Lock()
        self._next_request_at = 0.0

    def wait(self, requests_per_minute: int) -> None:
        limit = max(1, int(requests_per_minute or 30))
        interval = (60.0 / limit) + self._safety_seconds
        with self._lock:
            now = self._clock()
            wait_for = max(0.0, self._next_request_at - now)
            scheduled_at = max(now, self._next_request_at)
            self._next_request_at = scheduled_at + interval
        if wait_for > 0:
            self._sleeper(wait_for)

    def defer(self, seconds: float) -> None:
        if seconds <= 0:
            return
        with self._lock:
            self._next_request_at = max(self._next_request_at, self._clock() + seconds + self._safety_seconds)


_ANILIST_RATE_LIMITER = _AniListRateLimiter()


class _AniListCircuitBreaker:
    def __init__(self, *, clock=time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._open_until = 0.0
        self._reason = ""

    def raise_if_open(self) -> None:
        with self._lock:
            remaining = self._open_until - self._clock()
            reason = self._reason
        if remaining > 0:
            seconds = max(1, int(remaining + 0.999))
            raise RuntimeError(
                "AniList request skipped: AniList recently returned a temporary availability block. "
                f"Retrying in about {seconds}s. Last error: {reason}"
            )

    def open(self, seconds: int, reason: str) -> None:
        cooldown = max(0, int(seconds or 0))
        if cooldown <= 0:
            return
        with self._lock:
            self._open_until = max(self._open_until, self._clock() + cooldown)
            self._reason = reason

    def remaining_seconds(self) -> float:
        with self._lock:
            return max(0.0, self._open_until - self._clock())


_ANILIST_CIRCUIT_BREAKER = _AniListCircuitBreaker()


class AniListProvider:
    name = "anilist"

    def __init__(self, config: AniListConfig | None = None, cover_dir: Path | None = None):
        self.config = config or AniListConfig()
        self.cover_dir = cover_dir or get_paths().cover_dir

    def _request(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps({"query": query, "variables": variables}).encode("utf-8")
        req = urllib.request.Request(
            self.config.endpoint,
            data=body,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "ani-watchlist/0.1 local metadata client",
            },
            method="POST",
        )
        _ANILIST_CIRCUIT_BREAKER.raise_if_open()
        _ANILIST_RATE_LIMITER.wait(self.config.requests_per_minute)
        try:
            with urllib.request.urlopen(req, timeout=self.config.timeout_seconds) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = _http_error_detail(exc)
            retry_after = _retry_after_seconds(exc.headers)
            if retry_after is not None:
                _ANILIST_RATE_LIMITER.defer(retry_after)
            if _is_temporary_unavailable_error(exc.code, detail):
                _ANILIST_CIRCUIT_BREAKER.open(self.config.temporary_block_cooldown_seconds, detail)
            raise RuntimeError(f"AniList request failed: {detail}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"AniList request failed: {exc}") from exc
        payload = json.loads(raw)
        if payload.get("errors"):
            raise RuntimeError(f"AniList returned errors: {payload['errors']}")
        return payload.get("data") or {}

    def search_title(self, title: str) -> list[MetadataSearchResult]:
        results_by_id: dict[str, MetadataSearchResult] = {}
        variants = search_title_variants(title)
        for variant in variants:
            data = self._request(SEARCH_QUERY, {"search": variant})
            media = ((data.get("Page") or {}).get("media")) or []
            for item in media:
                media_id = str(item["id"])
                score = max(confidence(query, item) for query in variants)
                result = MetadataSearchResult(
                    provider=self.name,
                    media_id=media_id,
                    title=_best_title(item),
                    confidence_score=score,
                    payload=item,
                )
                existing = results_by_id.get(media_id)
                if existing is None or result.confidence_score > existing.confidence_score:
                    results_by_id[media_id] = result
        results = list(results_by_id.values())
        return sorted(results, key=lambda item: item.confidence_score, reverse=True)

    def search_anime_media(self, search: str, limit: int = 50) -> list[dict[str, Any]]:
        cleaned = str(search or "").strip()
        if not cleaned:
            return []
        return self._get_media_list(SEARCH_MEDIA_QUERY, limit, variables={"search": cleaned})

    def _related_edges(self, media_id: str | int) -> list[tuple[str, dict[str, Any]]]:
        data = self._request(RELATED_MEDIA_QUERY, {"id": int(media_id)})
        media = data.get("Media") or {}
        relations = media.get("relations") or {}
        edges: list[tuple[str, dict[str, Any]]] = []
        for edge in relations.get("edges") or []:
            if not isinstance(edge, dict):
                continue
            relation_type = str(edge.get("relationType") or "").strip()
            node = edge.get("node") or {}
            if relation_type and isinstance(node, dict):
                edges.append((relation_type, node))
        return edges

    def get_related_anime(self, media_id: str | int, *, max_depth: int = 6, max_items: int = 24) -> list[dict[str, Any]]:
        root_id = str(int(media_id))
        items: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        fetched_ids: set[str] = set()
        queue: deque[tuple[str, int]] = deque([(root_id, 0)])
        while queue:
            current_id, depth = queue.popleft()
            if current_id in fetched_ids or depth > max_depth:
                continue
            fetched_ids.add(current_id)
            for relation_type, node in self._related_edges(current_id):
                if relation_type not in SEASON_RELATION_TYPES or node.get("type") != "ANIME":
                    continue
                related_id = str(node.get("id") or "").strip()
                if not related_id or related_id == root_id:
                    continue
                if related_id not in seen_ids:
                    item = dict(node)
                    item["relationType"] = relation_type
                    items.append(item)
                    seen_ids.add(related_id)
                    if len(items) >= max_items:
                        return items
                if (
                    depth < max_depth
                    and relation_type in SEASON_CHAIN_RELATION_TYPES
                    and related_id not in fetched_ids
                ):
                    queue.append((related_id, depth + 1))
        return items

    def get_media(self, media_id: str) -> dict[str, Any]:
        data = self._request(MEDIA_QUERY, {"id": int(media_id)})
        media = data.get("Media")
        if not media:
            raise RuntimeError(f"AniList media not found: {media_id}")
        return media

    def get_media_batch(self, media_ids: list[int] | tuple[int, ...] | set[int]) -> list[dict[str, Any]]:
        ids = [int(media_id) for media_id in media_ids if str(media_id).strip()]
        if not ids:
            return []
        results: list[dict[str, Any]] = []
        for index in range(0, len(ids), 50):
            chunk = ids[index : index + 50]
            data = self._request(MEDIA_BATCH_QUERY, {"ids": chunk, "page": 1, "perPage": len(chunk)})
            media = (data.get("Page") or {}).get("media")
            if isinstance(media, list):
                results.extend(item for item in media if isinstance(item, dict))
        return results

    def get_cover(self, media_id: str) -> str | None:
        media = self.get_media(media_id)
        cover = media.get("coverImage") or {}
        url = cover.get("extraLarge") or cover.get("large") or cover.get("medium")
        if not url:
            return None
        return self.cache_cover(str(media_id), str(url))

    def get_external_links(self, media_id: str) -> list[dict[str, Any]]:
        media = self.get_media(media_id)
        return list(media.get("externalLinks") or [])

    def get_legal_availability(self, media_id: str) -> list[dict[str, Any]]:
        links = self.get_external_links(media_id)
        return [link for link in links if str(link.get("type", "")).upper() == "STREAMING"]

    def _get_media_list(
        self,
        query: str,
        limit: int,
        *,
        variables: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        per_page = max(1, min(int(limit), 50))
        page = 1
        items: list[dict[str, Any]] = []
        max_pages = max(1, (int(limit) + per_page - 1) // per_page)
        while page <= max_pages and len(items) < limit:
            request_variables = {"page": page, "perPage": per_page}
            request_variables.update(variables or {})
            data = self._request(query, request_variables)
            page_data = data.get("Page") or {}
            items.extend(list(page_data.get("media") or []))
            page_info = page_data.get("pageInfo") or {}
            if not page_info or not page_info.get("hasNextPage"):
                break
            page += 1
        return items[:limit]

    def _get_media_batch(
        self,
        query: str,
        *,
        start_page: int = 1,
        page_count: int = 2,
        per_page: int = 50,
        variables: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        page = max(1, int(start_page))
        remaining_pages = max(1, int(page_count))
        per_page = max(1, min(int(per_page), 50))
        items: list[dict[str, Any]] = []
        next_page: int | None = page
        while remaining_pages > 0 and next_page is not None:
            request_variables = {"page": page, "perPage": per_page}
            request_variables.update(variables or {})
            data = self._request(query, request_variables)
            page_data = data.get("Page") or {}
            items.extend(list(page_data.get("media") or []))
            page_info = page_data.get("pageInfo") or {}
            has_next = bool(page_info.get("hasNextPage"))
            current_page = int(page_info.get("currentPage") or page)
            next_page = current_page + 1 if has_next else None
            page = next_page or current_page
            remaining_pages -= 1
        return {"items": items, "next_page": next_page}

    def get_trending_anime(self, limit: int = 20) -> list[dict[str, Any]]:
        return self._get_media_list(TRENDING_QUERY, limit)

    def get_popular_anime(
        self,
        limit: int = 20,
        *,
        genre: str | None = None,
        tag: str | None = None,
    ) -> list[dict[str, Any]]:
        return self._get_media_list(POPULAR_MEDIA_QUERY, limit, variables=_popular_filter_variables(genre, tag))

    def get_top_airing_anime(self, limit: int = 20) -> list[dict[str, Any]]:
        return self._get_media_list(TOP_AIRING_QUERY, limit)

    def get_trending_anime_batch(
        self,
        *,
        start_page: int = 1,
        page_count: int = 2,
        per_page: int = 50,
    ) -> dict[str, Any]:
        return self._get_media_batch(
            TRENDING_QUERY,
            start_page=start_page,
            page_count=page_count,
            per_page=per_page,
        )

    def get_popular_anime_batch(
        self,
        *,
        start_page: int = 1,
        page_count: int = 2,
        per_page: int = 50,
        genre: str | None = None,
        tag: str | None = None,
    ) -> dict[str, Any]:
        return self._get_media_batch(
            POPULAR_MEDIA_QUERY,
            start_page=start_page,
            page_count=page_count,
            per_page=per_page,
            variables=_popular_filter_variables(genre, tag),
        )

    def get_top_airing_anime_batch(
        self,
        *,
        start_page: int = 1,
        page_count: int = 2,
        per_page: int = 50,
    ) -> dict[str, Any]:
        return self._get_media_batch(
            TOP_AIRING_QUERY,
            start_page=start_page,
            page_count=page_count,
            per_page=per_page,
        )

    def get_airing_schedule(
        self,
        start_timestamp: int,
        end_timestamp: int,
        *,
        limit: int = 140,
    ) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        page = 1
        per_page = 50
        max_pages = max(1, (int(limit) + per_page - 1) // per_page)
        while page <= max_pages and len(items) < limit:
            data = self._request(
                AIRING_SCHEDULE_QUERY,
                {"page": page, "perPage": per_page, "start": int(start_timestamp), "end": int(end_timestamp)},
            )
            page_data = data.get("Page") or {}
            items.extend(list(page_data.get("airingSchedules") or []))
            page_info = page_data.get("pageInfo") or {}
            if not page_info.get("hasNextPage"):
                break
            page += 1
        return items[:limit]

    def cache_cover(self, media_id: str, url: str) -> str:
        self.cover_dir.mkdir(parents=True, exist_ok=True)
        suffix = Path(urllib.parse.urlparse(url).path).suffix or ".jpg"
        dest = self.cover_dir / f"anilist-{media_id}{suffix}"
        if dest.exists():
            return str(dest)
        req = urllib.request.Request(url, headers={"User-Agent": "ani-watchlist/0.1"})
        with urllib.request.urlopen(req, timeout=self.config.timeout_seconds) as response:
            dest.write_bytes(response.read())
        return str(dest)
