from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class MetadataSearchResult:
    provider: str
    media_id: str
    title: str
    confidence_score: float
    payload: dict[str, Any]


class MetadataProvider(Protocol):
    name: str

    def search_title(self, title: str) -> list[MetadataSearchResult]:
        ...

    def get_media(self, media_id: str) -> dict[str, Any]:
        ...

    def get_cover(self, media_id: str) -> str | None:
        ...


class AvailabilityProvider(Protocol):
    name: str

    def get_external_links(self, media_id: str) -> list[dict[str, Any]]:
        ...

    def get_legal_availability(self, media_id: str) -> list[dict[str, Any]]:
        ...


class PlaybackProvider(Protocol):
    name: str

    def play(self, media_id: str, episode_key: str) -> None:
        """No production third-party streaming playback providers are implemented."""
        ...
