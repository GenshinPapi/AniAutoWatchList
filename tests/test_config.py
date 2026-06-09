from __future__ import annotations

from ani_watchlist.config import get_config_value, load_config, set_config_value


def test_playback_skip_intro_outro_config_persists(app_env) -> None:
    assert load_config().playback.skip_intro_outro is False

    set_config_value("playback.skip_intro_outro", "true")

    assert load_config().playback.skip_intro_outro is True
    assert get_config_value("playback.skip_intro_outro") is True
