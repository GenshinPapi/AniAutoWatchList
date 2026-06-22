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


def test_build_ani_cli_command_adds_dub_option() -> None:
    command = launcher.build_ani_cli_command("Frieren", "12", ani_cli="/home/me/.local/bin/ani-cli", mode="dub")

    assert command == [
        "/home/me/.local/bin/ani-cli",
        "--no-detach",
        "--select-nth",
        "1",
        "--episode",
        "12",
        "--dub",
        "Frieren",
    ]


def test_build_ani_cli_command_can_target_allanime_id() -> None:
    command = launcher.build_ani_cli_command(
        "Berserk (25 episodes)",
        "3",
        ani_cli="/home/me/.local/bin/ani-cli",
        allanime_id="berserk-id",
    )

    assert command == [
        "/home/me/.local/bin/ani-cli",
        "--no-detach",
        "--allanime-id",
        "berserk-id",
        "--episode",
        "3",
        "Berserk (25 episodes)",
    ]


def test_build_ani_cli_command_rejects_unknown_mode() -> None:
    try:
        launcher.build_ani_cli_command("Frieren", "12", ani_cli="/home/me/.local/bin/ani-cli", mode="raw")
    except launcher.LaunchError as exc:
        assert "unsupported playback mode" in str(exc)
    else:
        raise AssertionError("expected LaunchError")


def test_clean_ani_cli_search_title_removes_episode_count_and_source_id() -> None:
    assert launcher.clean_ani_cli_search_title("One Piece (1P) (1161 episodes)") == "One Piece"


def test_clean_ani_cli_search_title_preserves_meaningful_year_suffix() -> None:
    assert launcher.clean_ani_cli_search_title("Fruits Basket (2019) (25 episodes)") == "Fruits Basket (2019)"


def test_title_variants_strip_stacked_content_labels_from_existing_rows() -> None:
    assert launcher.ani_cli_title_variants("Overflow [18 ] [18+]", None, None) == ["Overflow"]


def test_choose_search_title_prefers_cleaned_source_title() -> None:
    title = launcher.choose_ani_cli_search_title("ONE PIECE", "One Piece (1P) (1161 episodes)")

    assert title == "One Piece"


def test_choose_search_title_ignores_unrelated_stale_source_title() -> None:
    title = launcher.choose_ani_cli_search_title("Berserk", "Berserk of Gluttony")

    assert title == "Berserk"


def test_resolve_allanime_launch_target_prefers_specific_metadata_variant(monkeypatch) -> None:
    calls: list[str] = []

    def fake_request(variables: dict[str, object], query: str, *, timeout: int = 12) -> dict[str, object]:
        assert query == launcher.ALLANIME_SEARCH_GQL
        search = variables["search"]
        assert isinstance(search, dict)
        query_text = str(search["query"])
        calls.append(query_text)
        if query_text == "Baki":
            return {
                "data": {
                    "shows": {
                        "edges": [
                            {
                                "_id": "new-baki",
                                "name": "Baki",
                                "englishName": "Baki",
                                "nativeName": "",
                                "availableEpisodes": {"sub": 39},
                            }
                        ]
                    }
                }
            }
        return {
            "data": {
                "shows": {
                    "edges": [
                        {
                            "_id": "original-baki",
                            "name": "Baki the Grappler",
                            "englishName": "Grappler Baki",
                            "nativeName": "",
                            "availableEpisodes": {"sub": 48},
                        }
                    ]
                }
            }
        }

    monkeypatch.setattr(launcher, "_allanime_api_request", fake_request)
    payload = {
        "title": {"english": "Baki", "romaji": "Grappler Baki", "userPreferred": "Grappler Baki"},
        "synonyms": ["Baki the Grappler"],
    }

    target = launcher.resolve_allanime_launch_target("Baki", "Baki", payload, total_episodes=48)

    assert target is not None
    assert target.show_id == "original-baki"
    assert target.title == "Grappler Baki (Baki the Grappler) (48 episodes)"
    assert "Grappler+Baki" in calls


def test_resolve_allanime_launch_target_rejects_broad_spinoff_match(monkeypatch) -> None:
    def fake_request(variables: dict[str, object], query: str, *, timeout: int = 12) -> dict[str, object]:
        return {
            "data": {
                "shows": {
                    "edges": [
                        {
                            "_id": "gluttony",
                            "name": "Berserk of Gluttony",
                            "englishName": "Berserk of Gluttony",
                            "nativeName": "",
                            "availableEpisodes": {"sub": 12},
                        }
                    ]
                }
            }
        }

    monkeypatch.setattr(launcher, "_allanime_api_request", fake_request)

    target = launcher.resolve_allanime_launch_target("Berserk", "Berserk of Gluttony", None, total_episodes=25)

    assert target is None


def test_resolve_allanime_launch_target_prefers_clear_long_running_match(monkeypatch) -> None:
    def fake_request(variables: dict[str, object], query: str, *, timeout: int = 12) -> dict[str, object]:
        if query == launcher.ALLANIME_SEARCH_GQL:
            return {
                "data": {
                    "shows": {
                        "edges": [
                            {
                                "_id": "one-piece-main",
                                "name": "1P",
                                "englishName": "One Piece",
                                "nativeName": "ONE PIECE",
                                "availableEpisodes": {"sub": 1163},
                            },
                            {
                                "_id": "one-piece-special",
                                "name": "One Piece Special",
                                "englishName": "One Piece Special",
                                "nativeName": "ONE PIECE",
                                "availableEpisodes": {"sub": 5},
                            },
                        ]
                    }
                }
            }
        return {"data": {"show": {"availableEpisodesDetail": {}}}}

    monkeypatch.setattr(launcher, "_allanime_api_request", fake_request)

    target = launcher.resolve_allanime_launch_target("One Piece")
    availability = launcher.allanime_available_episode_keys("One Piece")

    assert target is not None
    assert target.show_id == "one-piece-main"
    assert availability is not None
    assert availability.target.show_id == "one-piece-main"
    assert len(availability.episode_keys) == 1163


def test_resolve_allanime_launch_target_still_rejects_close_episode_count_ties(monkeypatch) -> None:
    def fake_request(variables: dict[str, object], query: str, *, timeout: int = 12) -> dict[str, object]:
        return {
            "data": {
                "shows": {
                    "edges": [
                        {
                            "_id": "candidate-a",
                            "name": "Shared Title A",
                            "englishName": "Shared Title",
                            "nativeName": "Shared Title",
                            "availableEpisodes": {"sub": 26},
                        },
                        {
                            "_id": "candidate-b",
                            "name": "Shared Title B",
                            "englishName": "Shared Title",
                            "nativeName": "Shared Title",
                            "availableEpisodes": {"sub": 24},
                        },
                    ]
                }
            }
        }

    monkeypatch.setattr(launcher, "_allanime_api_request", fake_request)

    assert launcher.resolve_allanime_launch_target("Shared Title") is None


def test_allanime_episode_available_checks_first_matching_mode_result(monkeypatch) -> None:
    calls: list[tuple[dict[str, object], str]] = []

    def fake_request(variables: dict[str, object], query: str, *, timeout: int = 12) -> dict[str, object]:
        calls.append((variables, query))
        if query == launcher.ALLANIME_SEARCH_GQL:
            return {
                "data": {
                    "shows": {
                        "edges": [
                            {"_id": "skip", "availableEpisodes": {"dub": 0}},
                            {"_id": "show-id", "availableEpisodes": {"dub": 2}},
                        ]
                    }
                }
            }
        return {"data": {"show": {"availableEpisodesDetail": {"dub": ["1", 2.0]}}}}

    monkeypatch.setattr(launcher, "_allanime_api_request", fake_request)

    assert launcher.allanime_episode_available("Love Flops", "2", mode="dub") is True
    assert calls[0][0]["translationType"] == "dub"
    assert calls[0][0]["search"] == {
        "allowAdult": True,
        "allowUnknown": True,
        "query": "Love+Flops",
    }
    assert calls[1][0] == {"showId": "show-id"}


def test_allanime_episode_available_returns_false_when_mode_missing(monkeypatch) -> None:
    def fake_request(variables: dict[str, object], query: str, *, timeout: int = 12) -> dict[str, object]:
        return {"data": {"shows": {"edges": [{"_id": "show-id", "availableEpisodes": {"dub": 0}}]}}}

    monkeypatch.setattr(launcher, "_allanime_api_request", fake_request)

    assert launcher.allanime_episode_available("Overflow", "1", mode="dub") is False


def test_allanime_episode_available_can_check_direct_show_id(monkeypatch) -> None:
    calls: list[tuple[dict[str, object], str]] = []

    def fake_request(variables: dict[str, object], query: str, *, timeout: int = 12) -> dict[str, object]:
        calls.append((variables, query))
        return {"data": {"show": {"availableEpisodesDetail": {"dub": ["1", "2"]}}}}

    monkeypatch.setattr(launcher, "_allanime_api_request", fake_request)

    assert launcher.allanime_episode_available(
        "Berserk",
        "2",
        mode="dub",
        show_id="target-id",
        episode_count=25,
    )
    assert calls == [({"showId": "target-id"}, launcher.ALLANIME_EPISODES_GQL)]


def test_allanime_available_episode_keys_uses_episode_detail(monkeypatch) -> None:
    calls: list[tuple[dict[str, object], str]] = []

    def fake_request(variables: dict[str, object], query: str, *, timeout: int = 12) -> dict[str, object]:
        calls.append((variables, query))
        if query == launcher.ALLANIME_SEARCH_GQL:
            return {
                "data": {
                    "shows": {
                        "edges": [
                            {
                                "_id": "show-id",
                                "name": "Test Show",
                                "englishName": "Test Show",
                                "nativeName": "",
                                "availableEpisodes": {"sub": 3},
                            }
                        ]
                    }
                }
            }
        return {"data": {"show": {"availableEpisodesDetail": {"sub": ["1", 2.0, {"episode": "3"}]}}}}

    monkeypatch.setattr(launcher, "_allanime_api_request", fake_request)

    availability = launcher.allanime_available_episode_keys("Test Show")

    assert availability is not None
    assert availability.target.show_id == "show-id"
    assert availability.episode_keys == ("1", "2", "3")
    assert calls[1] == ({"showId": "show-id"}, launcher.ALLANIME_EPISODES_GQL)


def test_allanime_available_episode_keys_falls_back_to_count(monkeypatch) -> None:
    def fake_request(variables: dict[str, object], query: str, *, timeout: int = 12) -> dict[str, object]:
        if query == launcher.ALLANIME_SEARCH_GQL:
            return {
                "data": {
                    "shows": {
                        "edges": [
                            {
                                "_id": "show-id",
                                "name": "Test Show",
                                "englishName": "Test Show",
                                "nativeName": "",
                                "availableEpisodes": {"sub": 2},
                            }
                        ]
                    }
                }
            }
        return {"data": {"show": {"availableEpisodesDetail": {}}}}

    monkeypatch.setattr(launcher, "_allanime_api_request", fake_request)

    availability = launcher.allanime_available_episode_keys("Test Show")

    assert availability is not None
    assert availability.episode_keys == ("1", "2")


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


def test_launch_episode_can_pass_mpv_ipc_and_wid(monkeypatch) -> None:
    seen: dict[str, object] = {}

    def fake_which(name: str) -> str | None:
        if name == "ani-cli":
            return "/home/me/.local/bin/ani-cli"
        return None

    class FakeProcess:
        pid = 4322

    def fake_popen(command: list[str], *, start_new_session: bool, env: dict[str, str]):
        seen["command"] = command
        seen["start_new_session"] = start_new_session
        seen["ipc"] = env.get("ANI_WATCH_MPV_IPC")
        seen["wid"] = env.get("ANI_WATCH_MPV_WID")
        return FakeProcess()

    monkeypatch.setattr(launcher.shutil, "which", fake_which)
    monkeypatch.setattr(launcher.subprocess, "Popen", fake_popen)

    result = launcher.launch_episode(
        "One Piece",
        "1090",
        prefer_terminal=False,
        mpv_ipc_path="/tmp/party.sock",
        mpv_wid=12345,
    )

    assert result.command == [
        "/home/me/.local/bin/ani-cli",
        "--no-detach",
        "--select-nth",
        "1",
        "--episode",
        "1090",
        "One Piece",
    ]
    assert result.pid == 4322
    assert seen["ipc"] == "/tmp/party.sock"
    assert seen["wid"] == "12345"
