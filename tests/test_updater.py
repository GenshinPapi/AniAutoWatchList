from __future__ import annotations

from pathlib import Path

from ani_watchlist import updater


def test_parse_version_from_package_source() -> None:
    assert updater.parse_version('__version__ = "1.2.3"') == "1.2.3"
    assert updater.parse_version("no version here") is None


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
