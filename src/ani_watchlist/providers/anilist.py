from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
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
      title { romaji english native userPreferred }
      synonyms
      episodes
      status
      coverImage { extraLarge large medium color }
      siteUrl
      externalLinks { id site url type language }
    }
  }
}
"""


MEDIA_QUERY = """
query ($id: Int) {
  Media(id: $id, type: ANIME) {
    id
    title { romaji english native userPreferred }
    synonyms
    episodes
    status
    description(asHtml: false)
    coverImage { extraLarge large medium color }
    siteUrl
    externalLinks { id site url type language }
    nextAiringEpisode { airingAt episode }
  }
}
"""


def _norm(text: str) -> str:
    text = text.casefold()
    text = re.sub(r"[^0-9a-z]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _best_title(payload: dict[str, Any]) -> str:
    title = payload.get("title") or {}
    english = (title.get("english") or "").strip()
    romaji = (title.get("romaji") or "").strip()
    preferred = (title.get("userPreferred") or "").strip()
    native = (title.get("native") or "").strip()
    if english and romaji and english.casefold() != romaji.casefold():
        return f"{english} ({romaji})"
    return english or preferred or romaji or native or "Unknown title"


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
        try:
            with urllib.request.urlopen(req, timeout=self.config.timeout_seconds) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.URLError as exc:
            raise RuntimeError(f"AniList request failed: {exc}") from exc
        payload = json.loads(raw)
        if payload.get("errors"):
            raise RuntimeError(f"AniList returned errors: {payload['errors']}")
        return payload.get("data") or {}

    def search_title(self, title: str) -> list[MetadataSearchResult]:
        data = self._request(SEARCH_QUERY, {"search": title})
        media = ((data.get("Page") or {}).get("media")) or []
        results = []
        for item in media:
            results.append(
                MetadataSearchResult(
                    provider=self.name,
                    media_id=str(item["id"]),
                    title=_best_title(item),
                    confidence_score=confidence(title, item),
                    payload=item,
                )
            )
        return sorted(results, key=lambda item: item.confidence_score, reverse=True)

    def get_media(self, media_id: str) -> dict[str, Any]:
        data = self._request(MEDIA_QUERY, {"id": int(media_id)})
        media = data.get("Media")
        if not media:
            raise RuntimeError(f"AniList media not found: {media_id}")
        return media

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

    def cache_cover(self, media_id: str, url: str) -> str:
        self.cover_dir.mkdir(parents=True, exist_ok=True)
        suffix = Path(urllib.parse.urlparse(url).path).suffix or ".jpg"
        dest = self.cover_dir / f"anilist-{media_id}{suffix}"
        req = urllib.request.Request(url, headers={"User-Agent": "ani-watchlist/0.1"})
        with urllib.request.urlopen(req, timeout=self.config.timeout_seconds) as response:
            dest.write_bytes(response.read())
        return str(dest)
