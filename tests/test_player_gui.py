from __future__ import annotations

from types import SimpleNamespace

import pytest

from ani_watchlist import gui
from ani_watchlist.db import initialize
from ani_watchlist.gui import WatchlistApp
from ani_watchlist.launcher import AllAnimeLaunchTarget, LaunchResult
from ani_watchlist.player_window import PlayerShow
from ani_watchlist.store import get_or_create_anime, upsert_episodes


TARGET = AllAnimeLaunchTarget(
    show_id="abc",
    title="Overgeared (Tempal: Item no Chikara) (4 episodes)",
    episode_count=4,
    score=100.0,
    query="Overgeared",
)


class FakeLabel:
    def __init__(self) -> None:
        self.options: dict[str, object] = {}

    def configure(self, **options: object) -> None:
        self.options.update(options)

    def cget(self, key: str) -> object:
        return self.options.get(key, "")


def detail_app(monkeypatch) -> WatchlistApp:
    app = object.__new__(WatchlistApp)
    app.conn = initialize()
    anime, _created = get_or_create_anime(app.conn, "Overgeared")
    upsert_episodes(app.conn, anime["id"], ["1", "2", "3"], source_label="test")
    app.selected_anime_id = int(anime["id"])
    app.selected_episode_key = lambda: "2"
    app.launch_label = FakeLabel()
    monkeypatch.setattr(gui, "resolve_allanime_launch_target", lambda *args, **kwargs: TARGET)
    return app


def test_continue_plays_in_the_player_window(app_env, monkeypatch) -> None:
    app = detail_app(monkeypatch)
    opened: list[tuple[PlayerShow, str]] = []
    app.open_player = lambda show, episode: opened.append((show, episode))
    monkeypatch.setattr(gui, "launch_episode", lambda *args, **kwargs: pytest.fail("no terminal launch expected"))

    app.continue_selected_episode("sub")

    show, episode = opened[0]
    assert episode == "2"
    assert (show.anime_id, show.title, show.launch_title, show.mode, show.allanime_id) == (
        app.selected_anime_id,
        "Overgeared",
        TARGET.title,
        "sub",
        "abc",
    )
    assert app.launch_label.cget("text") == "Playing Overgeared episode 2 (sub) in the player window."


def test_continue_in_terminal_keeps_the_classic_ani_cli_launch(app_env, monkeypatch) -> None:
    app = detail_app(monkeypatch)
    app.open_player = lambda *_args: pytest.fail("no player window expected")
    launched: list[tuple[tuple, dict]] = []

    def fake_launch(*args, **kwargs):
        launched.append((args, kwargs))
        return LaunchResult(command=["ani-cli"], pid=1, used_terminal=True)

    monkeypatch.setattr(gui, "launch_episode", fake_launch)

    app.continue_selected_episode("sub", in_terminal=True)

    assert launched == [((TARGET.title, "2"), {"mode": "sub", "allanime_id": "abc"})]
    assert app.launch_label.cget("text") == "Opened Overgeared episode 2 (sub) in terminal."


class FakePlayer:
    created: list["FakePlayer"] = []

    def __init__(self, root, show, **callbacks) -> None:
        self.show = show
        self.callbacks = callbacks
        self.closed = False
        self.state = "loading"
        self.played: list[str] = []
        self.loaded: list[tuple[PlayerShow, str]] = []
        self.raised = False
        FakePlayer.created.append(self)

    def raise_window(self) -> None:
        self.raised = True

    def play(self, episode: str) -> None:
        self.played.append(episode)

    def load(self, show: PlayerShow, episode: str) -> None:
        self.loaded.append((show, episode))


def test_open_player_reuses_the_open_window_and_replaces_a_closed_one(app_env, monkeypatch) -> None:
    FakePlayer.created = []
    monkeypatch.setattr(gui, "EpisodePlayer", FakePlayer)
    app = object.__new__(WatchlistApp)
    app.root = object()
    app.player = None
    app.conn = initialize()
    show = PlayerShow(anime_id=1, title="Overgeared", launch_title="Overgeared", mode="sub")

    app.open_player(show, "2")
    first = app.player
    app.open_player(show, "3")

    assert FakePlayer.created == [first]
    assert first.raised and first.played == ["2"]
    assert first.loaded == [(show, "3")]
    first.closed = True
    app.open_player(show, "4")
    assert app.player is not first and app.player.played == ["4"]


def test_a_late_close_does_not_forget_a_newer_player(app_env) -> None:
    app = object.__new__(WatchlistApp)
    old, new = SimpleNamespace(), SimpleNamespace()
    app.player = new

    app.player_closed(old)
    assert app.player is new
    app.player_closed(new)
    assert app.player is None


def test_episode_end_refreshes_only_that_shows_detail_page(app_env) -> None:
    app = object.__new__(WatchlistApp)
    app.shutting_down = False
    app.current_page = "detail"
    app.selected_anime_id = 7
    reloads: list[str] = []
    app.load_detail = lambda: reloads.append("reloaded")

    app.player_episode_ended(8)
    app.player_episode_ended(7)

    assert reloads == ["reloaded"]


def test_playing_keeps_the_idle_prompt_and_detail_refresh_away(app_env, monkeypatch) -> None:
    app = object.__new__(WatchlistApp)
    app.player = SimpleNamespace(closed=False, state="playing")
    app.last_user_activity_ms = 0
    monkeypatch.setattr(gui, "monotonic_ms", lambda: 123456)

    app.note_playback_activity()
    assert app.last_user_activity_ms == 123456
    assert app.player_is_playing()

    app.shutting_down = False
    app.current_page = "detail"
    app.party_playback_controller = None
    app.notes = object()
    app.safe_focus_get = lambda: None
    reloads: list[str] = []
    app.load_detail = lambda: reloads.append("reloaded")
    app.refresh_activity = lambda: None
    app.schedule_auto_refresh = lambda: None
    app.auto_refresh()
    assert reloads == []

    app.player.state = "finished"
    assert not app.player_is_playing()
    app.auto_refresh()
    assert reloads == ["reloaded"]


def test_close_app_stops_the_player_before_the_database_closes(monkeypatch) -> None:
    app = object.__new__(WatchlistApp)
    events: list[str] = []
    app.shutting_down = False
    app.party_client = None
    app.party_host_session = None
    app.player = SimpleNamespace(shutdown=lambda: events.append("player stopped"))
    app.conn = SimpleNamespace(close=lambda: events.append("database closed"))
    app.root = SimpleNamespace(
        after_cancel=lambda _job: None,
        quit=lambda: events.append("root quit"),
        destroy=lambda: events.append("root destroyed"),
    )
    app.backup_watchlist_on_exit = lambda: events.append("backed up") or True
    app.stop_host_party_mpv_observer = lambda: None
    app.dismiss_idle_prompt = lambda: None
    app.stop_party_fullscreen_observer = lambda: None
    app.stop_party_playback = lambda: None
    app.destroy_party_window = lambda: None

    app.close_app()

    assert events == ["backed up", "player stopped", "database closed", "root quit", "root destroyed"]
    assert app.player is None
