from __future__ import annotations

import json
from pathlib import Path

import pytest

from ani_watchlist.cloud import (
    CloudBackupError,
    GoogleDriveBackupProvider,
    load_cloud_backup_status,
    record_cloud_backup_status,
)


DESKTOP_CLIENT = {
    "installed": {
        "client_id": "test.apps.googleusercontent.com",
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "client_secret": "not-a-real-secret",
        "redirect_uris": ["http://localhost"],
    }
}


def test_install_client_config_requires_desktop_credentials_and_stores_private_file(app_env, tmp_path) -> None:
    source = tmp_path / "client.json"
    source.write_text(json.dumps(DESKTOP_CLIENT), encoding="utf-8")
    target = tmp_path / "private" / "google-drive-client.json"
    provider = GoogleDriveBackupProvider(client_config_path=target, token_path=tmp_path / "token.json")

    assert provider.install_client_config(source) == target
    assert json.loads(target.read_text(encoding="utf-8")) == DESKTOP_CLIENT
    assert target.stat().st_mode & 0o777 == 0o600

    source.write_text(json.dumps({"web": DESKTOP_CLIENT["installed"]}), encoding="utf-8")
    with pytest.raises(CloudBackupError, match="Desktop app"):
        provider.install_client_config(source)


def test_builtin_client_config_removes_per_user_file_requirement(app_env, tmp_path) -> None:
    builtin = tmp_path / "_google_drive_oauth_client.json"
    builtin.write_text(json.dumps(DESKTOP_CLIENT), encoding="utf-8")
    custom = tmp_path / "config" / "google-drive-client.json"
    provider = GoogleDriveBackupProvider(
        client_config_path=custom,
        builtin_client_config_path=builtin,
        token_path=tmp_path / "token.json",
    )

    assert provider.has_client_config() is True
    assert provider.has_builtin_client_config() is True
    assert provider.client_config_source() == "built-in"
    assert provider.resolved_client_config_path() == builtin
    assert provider._load_client_config()["installed"]["client_id"] == DESKTOP_CLIENT["installed"]["client_id"]


def test_custom_client_config_overrides_builtin_for_development(app_env, tmp_path) -> None:
    builtin_payload = json.loads(json.dumps(DESKTOP_CLIENT))
    builtin_payload["installed"]["client_id"] = "builtin.apps.googleusercontent.com"
    custom_payload = json.loads(json.dumps(DESKTOP_CLIENT))
    custom_payload["installed"]["client_id"] = "custom.apps.googleusercontent.com"
    builtin = tmp_path / "_google_drive_oauth_client.json"
    custom = tmp_path / "google-drive-client.json"
    builtin.write_text(json.dumps(builtin_payload), encoding="utf-8")
    custom.write_text(json.dumps(custom_payload), encoding="utf-8")
    provider = GoogleDriveBackupProvider(
        client_config_path=custom,
        builtin_client_config_path=builtin,
        token_path=tmp_path / "token.json",
    )

    assert provider.client_config_source() == "custom"
    assert provider.resolved_client_config_path() == custom
    assert provider._load_client_config()["installed"]["client_id"] == "custom.apps.googleusercontent.com"


def test_connection_check_requires_token_and_uses_read_only_listing(app_env, tmp_path, monkeypatch) -> None:
    provider = GoogleDriveBackupProvider(
        client_config_path=tmp_path / "client.json",
        token_path=tmp_path / "token.json",
    )
    with pytest.raises(CloudBackupError, match="not connected"):
        provider.test_connection()

    provider.token_path.write_text("{}", encoding="utf-8")
    expected = (object(),)
    monkeypatch.setattr(provider, "list_backups", lambda: expected)

    assert provider.test_connection() == expected


def test_upload_creates_missing_backups_and_updates_existing_one(app_env, tmp_path, monkeypatch) -> None:
    provider = GoogleDriveBackupProvider(
        client_config_path=tmp_path / "client.json",
        token_path=tmp_path / "token.json",
    )
    local_json = tmp_path / "jsonbackup.json"
    local_xml = tmp_path / "xmlbackup.xml"
    local_json.write_text('{"anime": []}', encoding="utf-8")
    local_xml.write_text("<myanimelist />", encoding="utf-8")
    calls: list[tuple[str, str, bytes | None, dict[str, str]]] = []

    def fake_request(url, *, method="GET", body=None, headers=None, retry_auth=True):
        calls.append((url, method, body, dict(headers or {})))
        if method == "GET":
            return json.dumps(
                {
                    "files": [
                        {
                            "id": "existing-json",
                            "name": "jsonbackup.json",
                            "modifiedTime": "2026-01-01T00:00:00Z",
                        }
                    ]
                }
            ).encode()
        if method == "PATCH":
            return json.dumps({"id": "existing-json", "name": "jsonbackup.json", "size": "13"}).encode()
        return json.dumps({"id": "new-xml", "name": "xmlbackup.xml", "size": "15"}).encode()

    monkeypatch.setattr(provider, "_authorized_request", fake_request)
    result = provider.upload_backups({"json": local_json, "xml": local_xml})

    assert [item.name for item in result.files] == ["jsonbackup.json", "xmlbackup.xml"]
    assert [call[1] for call in calls] == ["GET", "PATCH", "POST"]
    assert "uploadType=media" in calls[1][0]
    assert calls[1][2] == b'{"anime": []}'
    assert calls[1][3]["Content-Type"] == "application/json"
    assert "appDataFolder" in calls[2][2].decode("utf-8")
    assert b"<myanimelist />" in calls[2][2]


def test_download_uses_latest_named_backup(app_env, tmp_path, monkeypatch) -> None:
    provider = GoogleDriveBackupProvider(
        client_config_path=tmp_path / "client.json",
        token_path=tmp_path / "token.json",
    )
    calls: list[str] = []

    def fake_request(url, **_kwargs):
        calls.append(url)
        if "/files?" in url:
            return json.dumps(
                {
                    "files": [
                        {"id": "old", "name": "jsonbackup.json", "modifiedTime": "2025-01-01T00:00:00Z"},
                        {"id": "new", "name": "jsonbackup.json", "modifiedTime": "2026-01-01T00:00:00Z"},
                    ]
                }
            ).encode()
        return b'{"format": "ani-watchlist"}'

    monkeypatch.setattr(provider, "_authorized_request", fake_request)

    assert provider.download_backup("json") == '{"format": "ani-watchlist"}'
    assert "/files/new?alt=media" in calls[-1]


def test_upload_lists_remote_backups_only_once_when_drive_folder_is_empty(app_env, tmp_path, monkeypatch) -> None:
    provider = GoogleDriveBackupProvider(
        client_config_path=tmp_path / "client.json",
        token_path=tmp_path / "token.json",
    )
    local_json = tmp_path / "jsonbackup.json"
    local_xml = tmp_path / "xmlbackup.xml"
    local_json.write_text("{}", encoding="utf-8")
    local_xml.write_text("<myanimelist />", encoding="utf-8")
    methods: list[str] = []

    def fake_request(_url, *, method="GET", **_kwargs):
        methods.append(method)
        if method == "GET":
            return b'{"files": []}'
        name = "jsonbackup.json" if methods.count("POST") == 1 else "xmlbackup.xml"
        return json.dumps({"id": name, "name": name}).encode()

    monkeypatch.setattr(provider, "_authorized_request", fake_request)

    provider.upload_backups({"json": local_json, "xml": local_xml})

    assert methods == ["GET", "POST", "POST"]


def test_cloud_status_preserves_last_success_when_later_attempt_fails(app_env) -> None:
    record_cloud_backup_status(success=True)
    first = load_cloud_backup_status()
    record_cloud_backup_status(success=False, error="offline")
    latest = load_cloud_backup_status()

    assert first["last_success_at"]
    assert latest["last_success_at"] == first["last_success_at"]
    assert latest["success"] is False
    assert latest["error"] == "offline"
