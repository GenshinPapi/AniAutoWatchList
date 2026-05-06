from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


APP_NAME = "ani-watchlist"


def _xdg_dir(env_name: str, fallback: str) -> Path:
    return Path(os.environ.get(env_name, fallback)).expanduser()


@dataclass(frozen=True)
class AppPaths:
    data_dir: Path
    config_dir: Path
    cache_dir: Path
    state_dir: Path

    @property
    def db_path(self) -> Path:
        override = os.environ.get("ANI_WATCHLIST_DB")
        return Path(override).expanduser() if override else self.data_dir / "watchlist.sqlite3"

    @property
    def config_path(self) -> Path:
        override = os.environ.get("ANI_WATCHLIST_CONFIG")
        return Path(override).expanduser() if override else self.config_dir / "config.toml"

    @property
    def cover_dir(self) -> Path:
        override = os.environ.get("ANI_WATCHLIST_COVER_DIR")
        return Path(override).expanduser() if override else self.cache_dir / "covers"

    @property
    def log_dir(self) -> Path:
        override = os.environ.get("ANI_WATCHLIST_LOG_DIR")
        return Path(override).expanduser() if override else self.state_dir / "logs"

    def ensure(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.cover_dir.mkdir(parents=True, exist_ok=True)
        self.log_dir.mkdir(parents=True, exist_ok=True)


def get_paths() -> AppPaths:
    home = Path.home()
    return AppPaths(
        data_dir=_xdg_dir("XDG_DATA_HOME", str(home / ".local" / "share")) / APP_NAME,
        config_dir=_xdg_dir("XDG_CONFIG_HOME", str(home / ".config")) / APP_NAME,
        cache_dir=_xdg_dir("XDG_CACHE_HOME", str(home / ".cache")) / APP_NAME,
        state_dir=_xdg_dir("XDG_STATE_HOME", str(home / ".local" / "state")) / APP_NAME,
    )
