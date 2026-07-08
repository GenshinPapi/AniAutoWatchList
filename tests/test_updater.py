from __future__ import annotations

import base64
from pathlib import Path

from ani_watchlist import updater


def test_parse_version_from_package_source() -> None:
    assert updater.parse_version('__version__ = "1.2.3"') == "1.2.3"
    assert updater.parse_version("no version here") is None


def test_parse_ani_cli_version_and_upstream_commit() -> None:
    source = 'version_number="4.14.1"\nani_watch_upstream_commit="b8032b72901721a1ce859ca2816e8e2c914bc616"\n'

    assert updater.parse_ani_cli_version(source) == "4.14.1"
    assert updater.parse_ani_cli_upstream_commit(source) == "b8032b72901721a1ce859ca2816e8e2c914bc616"


def test_version_from_github_contents_payload() -> None:
    source = b'"""Package."""\n\n__version__ = "1.2.4"\n'
    payload = {"encoding": "base64", "content": base64.b64encode(source).decode("ascii")}

    assert updater.version_from_content_payload(payload) == "1.2.4"


def test_ani_cli_version_from_github_contents_payload() -> None:
    source = b'#!/bin/sh\nversion_number="4.14.1"\n'
    payload = {"encoding": "base64", "content": base64.b64encode(source).decode("ascii")}

    assert updater.ani_cli_version_from_content_payload(payload) == "4.14.1"


def test_ani_cli_values_from_github_contents_payload() -> None:
    source = b'#!/bin/sh\nversion_number="4.14.2"\nani_watch_upstream_commit="def4567"\n'
    payload = {"encoding": "base64", "content": base64.b64encode(source).decode("ascii")}

    assert updater.ani_cli_values_from_content_payload(payload) == ("4.14.2", "def4567")


def test_local_ani_cli_values_read_bundled_marker(tmp_path: Path) -> None:
    script_dir = tmp_path / "ani-cli"
    script_dir.mkdir()
    (script_dir / "ani-cli").write_text(
        'version_number="4.14.1"\nani_watch_upstream_commit="b8032b72901721a1ce859ca2816e8e2c914bc616"\n',
        encoding="utf-8",
    )

    version, commit = updater.local_ani_cli_values(tmp_path)

    assert version == "4.14.1"
    assert commit == "b8032b72901721a1ce859ca2816e8e2c914bc616"


def test_update_info_detects_remote_commit_change() -> None:
    info = updater.update_info_from_values(
        local_version="0.1.0",
        remote_version_value="0.1.0",
        local_commit="abc123",
        remote_commit="def456",
        remote_url="https://example.invalid/commit/def456",
        remote_message="new commit",
    )

    assert info.update_available is True
    assert info.reason == "commit"
    assert info.remote_message == "new commit"


def test_update_info_falls_back_to_version_when_no_local_commit() -> None:
    info = updater.update_info_from_values(
        local_version="0.1.0",
        remote_version_value="0.1.1",
        local_commit=None,
        remote_commit="def456",
    )

    assert info.update_available is True
    assert info.reason == "version"


def test_check_ani_cli_update_uses_remote_bundled_patch(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(updater, "local_ani_cli_values", lambda root: ("4.14.1", "abc123"))
    monkeypatch.setattr(updater, "remote_ani_cli_values", lambda *, timeout=8: ("4.14.2", "def456"))
    monkeypatch.setattr(
        updater,
        "remote_git_commit",
        lambda *, timeout=8: ("app456", "https://example.invalid/app456", "bundle ani-cli"),
    )

    info = updater.check_ani_cli_update(tmp_path)

    assert info.update_available is True
    assert info.local_commit == "abc123"
    assert info.remote_commit == "def456"
    assert info.remote_version == "4.14.2"
    assert info.remote_message == "bundle ani-cli"


def test_check_ani_cli_update_ignores_matching_bundled_patch(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(updater, "local_ani_cli_values", lambda root: ("4.14.1", "abc123"))
    monkeypatch.setattr(updater, "remote_ani_cli_values", lambda *, timeout=8: ("4.14.1", "abc123"))
    monkeypatch.setattr(
        updater,
        "remote_git_commit",
        lambda *, timeout=8: ("app456", "https://example.invalid/app456", "unrelated app update"),
    )

    info = updater.check_ani_cli_update(tmp_path)

    assert info.update_available is False
    assert info.reason is None


def test_build_update_command_runs_pull_then_installer(tmp_path: Path) -> None:
    command = updater.build_update_command(tmp_path)

    assert command[:3] == ["bash", "-lc", updater.UPDATE_SCRIPT]
    assert command[-1] == str(tmp_path)
    assert "git pull --ff-only origin main" in command[2]
    assert "scripts/install-user.sh" in command[2]
    assert "relaunch ani-watch-gui" in command[2]


def test_build_update_terminal_command_uses_detected_terminal(monkeypatch) -> None:
    def fake_which(name: str) -> str | None:
        if name == "x-terminal-emulator":
            return "/usr/bin/x-terminal-emulator"
        return None

    monkeypatch.setattr(updater.shutil, "which", fake_which)

    command, used_terminal = updater.build_update_terminal_command(["bash", "-lc", "true"])

    assert used_terminal is True
    assert command == ["/usr/bin/x-terminal-emulator", "-e", "bash", "-lc", "true"]
