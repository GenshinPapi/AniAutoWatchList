from __future__ import annotations

import pytest


@pytest.fixture
def app_env(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    cfg_dir = tmp_path / "config" / "ani-watchlist"
    cfg_dir.mkdir(parents=True)
    cfg = cfg_dir / "config.toml"
    cfg.write_text(
        """[tracking]
mark_watched_after_seconds = 120

[metadata]
search_on_new_title = false
auto_link_confidence = 0.86

[anilist]
enabled = false
endpoint = "https://graphql.anilist.co"
timeout_seconds = 1
""",
        encoding="utf-8",
    )
    return tmp_path
