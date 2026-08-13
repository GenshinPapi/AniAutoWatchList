from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from ani_watchlist.cloud import (
    CloudBackupFile,
    CloudBackupResult,
    CloudBackupError,
    GoogleDriveBackupProvider,
    load_cloud_backup_status,
    record_cloud_backup_status,
)
from ani_watchlist.db import initialize
from ani_watchlist.store import (
    episodes_for_anime,
    get_anime,
    get_or_create_anime,
    mark_episode,
    update_anime_fields,
    upsert_episodes,
)
from ani_watchlist.transfer import export_watchlist_text


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


def test_upload_skips_remote_files_with_identical_content(app_env, tmp_path, monkeypatch) -> None:
    provider = GoogleDriveBackupProvider(
        client_config_path=tmp_path / "client.json",
        token_path=tmp_path / "token.json",
    )
    local_json = tmp_path / "jsonbackup.json"
    local_xml = tmp_path / "xmlbackup.xml"
    local_json.write_text('{"anime": []}', encoding="utf-8")
    local_xml.write_text("<myanimelist />", encoding="utf-8")
    remote = tuple(
        CloudBackupFile(
            f"id-{path.name}",
            path.name,
            md5_checksum=hashlib.md5(path.read_bytes(), usedforsecurity=False).hexdigest(),
        )
        for path in (local_json, local_xml)
    )
    monkeypatch.setattr(provider, "list_backups", lambda: remote)
    monkeypatch.setattr(provider, "_create_file", lambda *_args: pytest.fail("unchanged file was created"))
    monkeypatch.setattr(provider, "_update_file", lambda *_args: pytest.fail("unchanged file was updated"))

    result = provider.upload_backups({"json": local_json, "xml": local_xml})

    assert result.files == remote


def test_synchronize_downloads_and_merges_before_uploading(app_env, tmp_path, monkeypatch) -> None:
    local_conn = initialize()
    get_or_create_anime(local_conn, "Local Only")
    shared_local, _ = get_or_create_anime(local_conn, "Shared Show")
    upsert_episodes(local_conn, shared_local["id"], ["1"])
    update_anime_fields(local_conn, shared_local["id"], status="watching", notes="local note")
    local_conn.execute(
        "UPDATE anime SET updated_at = '2026-01-01T00:00:00+00:00' WHERE id = ?",
        (shared_local["id"],),
    )
    local_conn.commit()

    remote_conn = initialize(tmp_path / "remote.sqlite3")
    get_or_create_anime(remote_conn, "Remote Only")
    shared_remote, _ = get_or_create_anime(remote_conn, "Shared Show")
    upsert_episodes(remote_conn, shared_remote["id"], ["1"])
    mark_episode(remote_conn, shared_remote["id"], "1", watched=True)
    update_anime_fields(remote_conn, shared_remote["id"], status="completed", notes="remote note")
    remote_conn.execute(
        "UPDATE anime SET updated_at = '2026-02-01T00:00:00+00:00' WHERE id = ?",
        (shared_remote["id"],),
    )
    remote_conn.execute(
        "UPDATE episodes SET updated_at = '2026-02-01T00:00:00+00:00' WHERE anime_id = ?",
        (shared_remote["id"],),
    )
    remote_conn.commit()
    remote_text = export_watchlist_text(remote_conn, "json")
    remote_file = CloudBackupFile(
        "remote-json",
        "jsonbackup.json",
        "2026-02-01T00:00:00Z",
        len(remote_text.encode()),
        hashlib.md5(remote_text.encode(), usedforsecurity=False).hexdigest(),
    )
    provider = GoogleDriveBackupProvider(
        client_config_path=tmp_path / "client.json",
        token_path=tmp_path / "token.json",
    )
    backup_dir = tmp_path / "checkout"
    events: list[str] = []
    uploaded_payloads: dict[str, str] = {}

    def fake_download(_remote, export_format):
        events.append("download")
        initial = json.loads((backup_dir / "jsonbackup.json").read_text(encoding="utf-8"))
        assert [row["display_title"] for row in initial["anime"]] == ["Local Only", "Shared Show"]
        assert export_format == "json"
        return remote_text

    def fake_upload(local_files, _remote_files):
        events.append("upload")
        uploaded_payloads.update(
            {export_format: Path(path).read_text(encoding="utf-8") for export_format, path in local_files.items()}
        )
        return CloudBackupResult(files=(remote_file,))

    monkeypatch.setattr(provider, "list_backups", lambda: (remote_file,))
    monkeypatch.setattr(provider, "_download_file", fake_download)
    monkeypatch.setattr(provider, "_upload_backups", fake_upload)

    result = provider.synchronize_backups(local_conn, backup_dir)

    assert events == ["download", "upload"]
    assert result.downloaded_format == "json"
    assert result.import_result["anime"] == 1
    assert {row["display_title"] for row in local_conn.execute("SELECT display_title FROM anime")} == {
        "Local Only",
        "Remote Only",
        "Shared Show",
    }
    shared = get_anime(local_conn, "Shared Show")
    assert shared["status"] == "completed"
    assert shared["notes"] == "remote note"
    assert episodes_for_anime(local_conn, shared["id"])[0]["watched"] == 1
    uploaded_titles = {row["display_title"] for row in json.loads(uploaded_payloads["json"])["anime"]}
    assert uploaded_titles == {"Local Only", "Remote Only", "Shared Show"}
    assert "<series_title>Local Only</series_title>" in uploaded_payloads["xml"]
    assert "<series_title>Remote Only</series_title>" in uploaded_payloads["xml"]


def test_cloud_status_preserves_last_success_when_later_attempt_fails(app_env) -> None:
    record_cloud_backup_status(success=True)
    first = load_cloud_backup_status()
    record_cloud_backup_status(success=False, error="offline")
    latest = load_cloud_backup_status()

    assert first["last_success_at"]
    assert latest["last_success_at"] == first["last_success_at"]
    assert latest["success"] is False
    assert latest["error"] == "offline"
