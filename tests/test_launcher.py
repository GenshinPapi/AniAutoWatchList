from __future__ import annotations

from ani_watchlist import launcher


def test_build_ani_cli_command_uses_episode_option() -> None:
    command = launcher.build_ani_cli_command("Frieren", "12", ani_cli="/home/me/.local/bin/ani-cli")

    assert command == [
        "/home/me/.local/bin/ani-cli",
        "--no-detach",
        "--select-nth",
        "1",
        "--episode",
        "12",
        "Frieren",
    ]


def test_clean_ani_cli_search_title_removes_episode_count_and_source_id() -> None:
    assert launcher.clean_ani_cli_search_title("One Piece (1P) (1161 episodes)") == "One Piece"


def test_clean_ani_cli_search_title_preserves_meaningful_year_suffix() -> None:
    assert launcher.clean_ani_cli_search_title("Fruits Basket (2019) (25 episodes)") == "Fruits Basket (2019)"


def test_choose_search_title_prefers_cleaned_source_title() -> None:
    title = launcher.choose_ani_cli_search_title("ONE PIECE", "One Piece (1P) (1161 episodes)")

    assert title == "One Piece"


def test_terminal_args_use_x_terminal_compatible_e_flag() -> None:
    assert launcher.terminal_args_for("x-terminal-emulator", "/usr/bin/gnome-terminal.wrapper") == ("-e",)


def test_terminal_args_detect_direct_gnome_terminal() -> None:
    assert launcher.terminal_args_for("gnome-terminal", "/usr/bin/gnome-terminal") == ("--",)


def test_terminal_command_uses_shell_wrapper(monkeypatch) -> None:
    def fake_which(name: str) -> str | None:
        if name == "x-terminal-emulator":
            return "/usr/bin/gnome-terminal.wrapper"
        return None

    monkeypatch.setattr(launcher.shutil, "which", fake_which)

    command, used_terminal = launcher.build_terminal_command(
        ["/home/me/.local/bin/ani-cli", "--no-detach", "--select-nth", "1", "--episode", "1", "Test"]
    )

    assert used_terminal is True
    assert command[:5] == ["/usr/bin/gnome-terminal.wrapper", "-e", "bash", "-lc", launcher.LAUNCH_WRAPPER]
    assert command[5:] == [
        "ani-watch-launch",
        "/home/me/.local/bin/ani-cli",
        "--no-detach",
        "--select-nth",
        "1",
        "--episode",
        "1",
        "Test",
    ]


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

    assert result.command == [
        "bash",
        "-lc",
        launcher.LAUNCH_WRAPPER,
        "ani-watch-launch",
        "/home/me/.local/bin/ani-cli",
        "--no-detach",
        "--select-nth",
        "1",
        "--episode",
        "1090",
        "One Piece",
    ]
    assert result.pid == 4321
    assert result.used_terminal is False
    assert seen["start_new_session"] is True
