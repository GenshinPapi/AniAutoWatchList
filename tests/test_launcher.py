from __future__ import annotations

from ani_watchlist import launcher


def test_build_ani_cli_command_uses_episode_option() -> None:
    command = launcher.build_ani_cli_command("Frieren", "12", ani_cli="/home/me/.local/bin/ani-cli")

    assert command == ["/home/me/.local/bin/ani-cli", "--episode", "12", "Frieren"]


def test_terminal_command_prefers_x_terminal(monkeypatch) -> None:
    def fake_which(name: str) -> str | None:
        if name == "x-terminal-emulator":
            return "/usr/bin/x-terminal-emulator"
        return None

    monkeypatch.setattr(launcher.shutil, "which", fake_which)

    command, used_terminal = launcher.build_terminal_command(["/home/me/.local/bin/ani-cli", "--episode", "1", "Test"])

    assert used_terminal is True
    assert command == ["/usr/bin/x-terminal-emulator", "-e", "/home/me/.local/bin/ani-cli", "--episode", "1", "Test"]


def test_launch_episode_resolves_ani_cli_and_does_not_block(monkeypatch) -> None:
    seen: dict[str, object] = {}

    def fake_which(name: str) -> str | None:
        if name == "ani-cli":
            return "/home/me/.local/bin/ani-cli"
        return None

    class FakeProcess:
        pid = 4321

    def fake_popen(command: list[str], *, start_new_session: bool):
        seen["command"] = command
        seen["start_new_session"] = start_new_session
        return FakeProcess()

    monkeypatch.setattr(launcher.shutil, "which", fake_which)
    monkeypatch.setattr(launcher.subprocess, "Popen", fake_popen)

    result = launcher.launch_episode("One Piece", "1090")

    assert result.command == ["/home/me/.local/bin/ani-cli", "--episode", "1090", "One Piece"]
    assert result.pid == 4321
    assert result.used_terminal is False
    assert seen["start_new_session"] is True
