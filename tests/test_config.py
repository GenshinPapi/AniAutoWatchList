from __future__ import annotations

from ani_watchlist.config import load_config, set_config_value


def test_cloud_config_defaults_when_existing_config_has_no_cloud_section(app_env) -> None:
    config = load_config()

    assert config.cloud.google_drive_auto_backup is False
    assert config.cloud.google_drive_timeout_seconds == 20


def test_cloud_config_can_enable_auto_backup_without_losing_existing_sections(app_env) -> None:
    set_config_value("cloud.google_drive_auto_backup", "true")
    config = load_config()

    assert config.cloud.google_drive_auto_backup is True
    assert config.metadata.search_on_new_title is False
    assert config.anilist.enabled is False
