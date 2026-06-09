from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 fallback
    tomllib = None  # type: ignore[assignment]

from .paths import get_paths


DEFAULT_CONFIG_TEXT = """[tracking]
mark_watched_after_seconds = 0

[playback]
skip_intro_outro = false

[metadata]
search_on_new_title = true
auto_link_confidence = 0.86

[anilist]
enabled = true
endpoint = "https://graphql.anilist.co"
timeout_seconds = 8
"""


@dataclass(frozen=True)
class TrackingConfig:
    mark_watched_after_seconds: int = 0


@dataclass(frozen=True)
class PlaybackConfig:
    skip_intro_outro: bool = False


@dataclass(frozen=True)
class MetadataConfig:
    search_on_new_title: bool = True
    auto_link_confidence: float = 0.86


@dataclass(frozen=True)
class AniListConfig:
    enabled: bool = True
    endpoint: str = "https://graphql.anilist.co"
    timeout_seconds: int = 8


@dataclass(frozen=True)
class AppConfig:
    tracking: TrackingConfig = TrackingConfig()
    playback: PlaybackConfig = PlaybackConfig()
    metadata: MetadataConfig = MetadataConfig()
    anilist: AniListConfig = AniListConfig()


def ensure_config(path: Path | None = None) -> Path:
    cfg_path = path or get_paths().config_path
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    if not cfg_path.exists():
        cfg_path.write_text(DEFAULT_CONFIG_TEXT, encoding="utf-8")
    return cfg_path


def load_raw_config(path: Path | None = None) -> dict[str, Any]:
    cfg_path = ensure_config(path)
    if tomllib is None:
        return {}
    with cfg_path.open("rb") as fh:
        return tomllib.load(fh)


def load_config(path: Path | None = None) -> AppConfig:
    raw = load_raw_config(path)

    tracking = raw.get("tracking", {})
    playback = raw.get("playback", {})
    metadata = raw.get("metadata", {})
    anilist = raw.get("anilist", {})
    return AppConfig(
        tracking=TrackingConfig(
            mark_watched_after_seconds=int(tracking.get("mark_watched_after_seconds", 0)),
        ),
        playback=PlaybackConfig(
            skip_intro_outro=bool(playback.get("skip_intro_outro", False)),
        ),
        metadata=MetadataConfig(
            search_on_new_title=bool(metadata.get("search_on_new_title", True)),
            auto_link_confidence=float(metadata.get("auto_link_confidence", 0.86)),
        ),
        anilist=AniListConfig(
            enabled=bool(anilist.get("enabled", True)),
            endpoint=str(anilist.get("endpoint", "https://graphql.anilist.co")),
            timeout_seconds=int(anilist.get("timeout_seconds", 8)),
        ),
    )


def get_config_value(dotted_key: str, path: Path | None = None) -> Any:
    raw = load_raw_config(path)
    value: Any = raw
    for part in dotted_key.split("."):
        if not isinstance(value, dict) or part not in value:
            raise KeyError(dotted_key)
        value = value[part]
    return value


def _parse_value(value: str) -> Any:
    lowered = value.strip().lower()
    if lowered in {"true", "false"}:
        return lowered == "true"
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        pass
    return value


def _format_toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return str(value)
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def write_raw_config(raw: dict[str, Any], path: Path | None = None) -> Path:
    cfg_path = ensure_config(path)
    lines: list[str] = []
    for section in ("tracking", "playback", "metadata", "anilist"):
        values = raw.get(section, {})
        if not isinstance(values, dict):
            continue
        lines.append(f"[{section}]")
        for key, value in values.items():
            lines.append(f"{key} = {_format_toml_value(value)}")
        lines.append("")
    cfg_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return cfg_path


def set_config_value(dotted_key: str, value: str, path: Path | None = None) -> Any:
    parts = dotted_key.split(".")
    if len(parts) != 2:
        raise KeyError("config keys must use section.key form")
    raw = load_raw_config(path)
    section, key = parts
    raw.setdefault(section, {})
    if not isinstance(raw[section], dict):
        raise KeyError(section)
    parsed = _parse_value(value)
    raw[section][key] = parsed
    write_raw_config(raw, path)
    return parsed
