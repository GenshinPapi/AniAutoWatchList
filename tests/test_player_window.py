from __future__ import annotations

import os
import queue
import signal
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from ani_watchlist import player_window
from ani_watchlist.player_window import (
    EpisodePlayer,
    PlayerShow,
    ani_cli_provider,
    ani_cli_status_line,
    embed_window_id,
    format_playback_clock,
    neighbor_episode,
)


COLORS = {"bg": "#101114", "text": "#f1f3f6", "muted": "#a9b0bb", "border": "#2d323b", "danger": "#d44b4b"}

# what ani-cli prints while it fails over, escape codes and all, plus the warnings the
# graphics libraries add once mpv starts
ANI_CLI_OUTPUT = (
    "\x1b[2K\r\x1b[1;34mChecking dependencies...\x1b[0m\n"
    "\x1b[A\x1b[K\x1b7\x1b[2K\r\x1b[1;33manidb.app playback failed: anidb.app did not return a matching anime."
    " Trying hianime.at...\x1b[0m\n"
    "\x1b[1;32mhianime.at\x1b[0m Links Fetched\n"
    "libEGL warning: DRI3 error: Could not get DRI3 device\n"
    "Failed to open VDPAU backend libvdpau_nvidia.so: cannot open shared object file: No such file or directory\n"
)
NOT_RELEASED = "\x1b[2K\r\x1b[1;34mChecking dependencies...\x1b[0m\n\x1b[2K\r\x1b[1;31mEpisode not released!\x1b[0m\n"
SHOW = PlayerShow(anime_id=7, title="Overgeared", launch_title="Overgeared (Tempal: Item no Chikara) (4 episodes)", mode="sub", allanime_id="abc")
# far above any kernel's pid_max, so a stray signal can never reach a real process group
IMPOSSIBLE_PID = 2**30


def test_format_playback_clock_reads_like_a_player() -> None:
    assert format_playback_clock(0) == "00:00"
    assert format_playback_clock(83.9) == "01:23"
    assert format_playback_clock(1420) == "23:40"
    assert format_playback_clock(3725) == "1:02:05"
    for unknown in (None, "nope", float("nan"), -1):
        assert format_playback_clock(unknown) == "--:--"


def test_status_line_skips_escape_codes_and_library_noise() -> None:
    assert ani_cli_status_line(ANI_CLI_OUTPUT) == "hianime.at Links Fetched"
    assert ani_cli_status_line(NOT_RELEASED) == "Episode not released!"
    assert ani_cli_status_line("\x1b[2K\r\x1b[1;34mChecking dependencies...\x1b[0m\n/usr/bin/dmenu\n") is None


def test_provider_comes_from_the_links_fetched_line() -> None:
    assert ani_cli_provider(ANI_CLI_OUTPUT) == "hianime.at"
    assert ani_cli_provider(NOT_RELEASED) is None


def test_neighbor_episode_moves_through_the_watchlist_order() -> None:
    episodes = ["1", "2", "3"]
    assert neighbor_episode(episodes, "2", 1) == "3"
    assert neighbor_episode(episodes, "2", -1) == "1"
    assert neighbor_episode(episodes, "3", 1) is None
    assert neighbor_episode(episodes, "1", -1) is None
    assert neighbor_episode(episodes, "9", 1) is None


def test_embedding_can_be_switched_off(monkeypatch) -> None:
    monkeypatch.setenv("ANI_WATCH_PLAYER_DISABLE_EMBED", "1")
    assert embed_window_id(object()) is None


class FakeWidget:
    def __init__(self) -> None:
        self.options: dict[str, object] = {}
        self.mapped = True
        self.withdrawn = False
        self.destroyed = False

    def configure(self, **options: object) -> None:
        self.options.update(options)

    def cget(self, key: str) -> object:
        return self.options.get(key, "")

    def grid(self, **_options: object) -> None:
        self.mapped = True

    def grid_remove(self) -> None:
        self.mapped = False

    def grid_configure(self, **_options: object) -> None:
        pass

    def title(self, text: str) -> None:
        self.options["title"] = text

    def attributes(self, name: str, value: object) -> None:
        self.options[name] = value

    def withdraw(self) -> None:
        self.withdrawn = True

    def deiconify(self) -> None:
        self.withdrawn = False

    def lift(self) -> None:
        pass

    def focus_set(self) -> None:
        pass

    def destroy(self) -> None:
        self.destroyed = True


class HeadlessPlayer(EpisodePlayer):
    """The real player logic with stand-in widgets, so no window ever opens."""

    def _build(self) -> None:
        for name in (
            "window",
            "header",
            "title_label",
            "episode_label",
            "status_label",
            "video_panel",
            "video_frame",
            "controls",
            "prev_button",
            "back_button",
            "play_button",
            "forward_button",
            "next_button",
            "time_label",
            "fullscreen_button",
            "close_button",
        ):
            setattr(self, name, FakeWidget())

    def _note_external_player(self) -> None:
        pass


class FakeProcess:
    """ani-cli as far as the player can see it: an exit code, eventually."""

    def __init__(self) -> None:
        self.pid = IMPOSSIBLE_PID
        self.returncode: int | None = None
        self.signals: list[int] = []

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        deadline = time.monotonic() + (timeout or 0)
        while self.returncode is None:
            if time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired("ani-cli", timeout)
            time.sleep(0.01)
        return self.returncode

    def send_signal(self, sig: int) -> None:
        self.signals.append(sig)
        self.returncode = -sig


class FakeController:
    """mpv's IPC socket; it only answers once the test says mpv is running."""

    def __init__(self, socket_path: str) -> None:
        self.socket_path = socket_path
        self.running = False
        self.properties: dict[str, object] = {"time-pos": 12.0, "duration": 1420.0, "pause": False, "fullscreen": False}
        self.commands: list[object] = []
        self.process: FakeProcess | None = None

    def available(self) -> bool:
        return self.running

    def get_property(self, name: str) -> object:
        return self.properties.get(name)

    def time_position(self) -> float | None:
        return float(self.properties["time-pos"]) if self.running else None

    def play(self) -> None:
        self.properties["pause"] = False
        self.commands.append("play")

    def pause(self) -> None:
        self.properties["pause"] = True
        self.commands.append("pause")

    def relative_seek(self, delta: float) -> None:
        self.properties["time-pos"] = float(self.properties["time-pos"]) + delta
        self.commands.append(("seek", delta))

    def set_fullscreen(self, enabled: bool) -> None:
        self.properties["fullscreen"] = enabled
        self.commands.append(("fullscreen", enabled))

    def stop(self) -> None:
        # mpv quits and ani-cli exits behind it
        self.commands.append("quit")
        self.running = False
        if self.process is not None:
            self.process.returncode = 0


class Rig:
    """A headless player with a scripted ani-cli and mpv behind it."""

    def __init__(self, tmp_path: Path, monkeypatch, *, episodes: tuple[str, ...] = ("1", "2", "3", "4"), launcher=None) -> None:
        self.calls: list[dict[str, object]] = []
        self.processes: list[FakeProcess] = []
        self.controllers: list[FakeController] = []
        self.ui: queue.Queue = queue.Queue()
        self.ended: list[int] = []
        self.activity = 0
        self.closed: list[object] = []
        self.next_output = ANI_CLI_OUTPUT

        def make_controller(socket_path: str) -> FakeController:
            controller = FakeController(socket_path)
            self.controllers.append(controller)
            return controller

        monkeypatch.setattr(player_window, "MpvIpcController", make_controller)
        # stand-in widgets have no X window for mpv to draw into
        monkeypatch.setenv("ANI_WATCH_PLAYER_DISABLE_EMBED", "1")
        self.player = HeadlessPlayer(
            object(),
            SHOW,
            colors=COLORS,
            log_dir=tmp_path,
            run_on_ui=self.ui.put,
            episodes_for=lambda _anime_id: list(episodes),
            on_episode_end=self.ended.append,
            on_activity=self.count_activity,
            on_close=self.closed.append,
            launcher=launcher or self.launch,
        )

    def count_activity(self) -> None:
        self.activity += 1

    def launch(self, title: str, episode: str, **options: object) -> SimpleNamespace:
        self.calls.append({"title": title, "episode": episode, **options})
        Path(options["log_path"]).write_text(self.next_output, encoding="utf-8")
        process = FakeProcess()
        self.processes.append(process)
        self.controllers[-1].process = process
        return SimpleNamespace(process=process)

    def pump(self, until, timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                self.ui.get(timeout=0.05)()
            except queue.Empty:
                pass
            if until():
                return True
        return until()

    def start_mpv(self) -> FakeController:
        controller = self.controllers[-1]
        controller.running = True
        assert self.pump(lambda: self.player.state == "playing")
        return controller


def test_play_runs_ani_cli_until_the_episode_ends_and_shows_progress(tmp_path, monkeypatch) -> None:
    rig = Rig(tmp_path, monkeypatch)
    rig.next_output = "\x1b[2K\r\x1b[1;33manidb.app playback failed: down. Trying hianime.at...\x1b[0m\n"
    rig.player.play("2")

    assert rig.player.status_label.cget("text") == "Finding a stream for episode 2..."
    assert rig.player.play_button.cget("state") == "disabled"
    call = rig.calls[0]
    assert (call["title"], call["episode"], call["mode"], call["allanime_id"]) == (SHOW.launch_title, "2", "sub", "abc")
    assert call["exit_after_play"] is True and call["prefer_terminal"] is False
    assert call["extra_mpv_args"] == "--no-terminal"
    assert Path(call["log_path"]).name == "player.log"
    assert str(call["mpv_ipc_path"]).endswith(".sock")
    # ani-cli's failover messages show while it is still looking for a stream
    assert rig.pump(lambda: "Trying hianime.at" in str(rig.player.status_label.cget("text")))
    assert rig.player.episode_label.cget("text") == "Episode 2 · Sub"


def test_an_episode_ani_cli_cannot_find_shows_the_error_and_offers_retry(tmp_path, monkeypatch) -> None:
    rig = Rig(tmp_path, monkeypatch)
    rig.next_output = NOT_RELEASED
    rig.player.play("4")
    rig.processes[0].returncode = 1

    assert rig.pump(lambda: rig.player.state == "failed")
    assert rig.player.status_label.cget("text") == "Episode not released!"
    assert rig.player.status_label.cget("fg") == COLORS["danger"]
    assert rig.player.play_button.cget("text") == "Retry"
    rig.player.primary_action()
    assert [call["episode"] for call in rig.calls] == ["4", "4"]


def test_the_controls_drive_mpv_and_a_finished_episode_offers_replay(tmp_path, monkeypatch) -> None:
    rig = Rig(tmp_path, monkeypatch)
    rig.player.play("2")
    mpv = rig.start_mpv()

    assert rig.player.status_label.cget("text") == "Playing episode 2 from hianime.at."
    assert rig.player.time_label.cget("text") == "00:12 / 23:40"
    assert rig.player.play_button.cget("text") == "Pause"
    assert rig.activity > 0
    rig.player.toggle_pause()
    assert mpv.commands[-1] == "pause"
    assert rig.player.play_button.cget("text") == "Play"
    rig.player.primary_action()
    assert mpv.commands[-1] == "play"
    rig.player.seek(10)
    assert mpv.commands[-1] == ("seek", 10)

    rig.processes[0].returncode = 0
    assert rig.pump(lambda: rig.player.state == "finished")
    assert rig.player.status_label.cget("text") == "Finished episode 2."
    assert rig.player.play_button.cget("text") == "Replay"
    assert rig.ended == [SHOW.anime_id]


def test_next_quits_the_playing_episode_and_quick_clicks_land_on_the_last_one(tmp_path, monkeypatch) -> None:
    rig = Rig(tmp_path, monkeypatch)
    rig.player.play("2")
    mpv = rig.start_mpv()

    rig.player.next_episode()
    rig.player.next_episode()

    assert rig.player.episode_label.cget("text") == "Episode 4 · Sub"
    assert rig.pump(lambda: len(rig.calls) == 2)
    # episode 2 quit through mpv, so ani-cli could record it, and episode 3 never started
    assert "quit" in mpv.commands
    assert rig.processes[0].returncode == 0
    assert [call["episode"] for call in rig.calls] == ["2", "4"]
    assert SHOW.anime_id in rig.ended
    assert rig.player.prev_button.cget("state") == "normal"
    assert rig.player.next_button.cget("state") == "disabled"


def test_updates_from_a_replaced_episode_are_ignored(tmp_path, monkeypatch) -> None:
    rig = Rig(tmp_path, monkeypatch)
    rig.player.play("2")
    old = rig.player.session
    rig.player.play("3")
    assert rig.pump(lambda: len(rig.calls) == 2)

    rig.player._apply(old, {"position": 50.0, "duration": 60.0, "paused": False, "fullscreen": False}, None, None, 0)

    assert rig.player.state == "loading"
    assert rig.player.episode_label.cget("text") == "Episode 3 · Sub"


def test_mpv_fullscreen_toggles_carry_over_to_the_window(tmp_path, monkeypatch) -> None:
    rig = Rig(tmp_path, monkeypatch)
    rig.player.play("2")
    mpv = rig.start_mpv()

    # f or a double-click inside the video flips mpv's own flag
    mpv.properties["fullscreen"] = True
    assert rig.pump(lambda: rig.player.fullscreen)
    assert rig.player.window.cget("-fullscreen") is True
    assert not rig.player.header.mapped and not rig.player.controls.mapped

    rig.player.set_fullscreen(False)
    assert rig.player.header.mapped and rig.player.controls.mapped
    assert mpv.commands[-1] == ("fullscreen", False)


def test_closing_mid_episode_lets_mpv_quit_before_the_window_goes(tmp_path, monkeypatch) -> None:
    rig = Rig(tmp_path, monkeypatch)
    rig.player.play("2")
    mpv = rig.start_mpv()

    rig.player.close()

    assert rig.player.window.withdrawn
    assert rig.pump(lambda: rig.closed == [rig.player])
    assert "quit" in mpv.commands
    assert rig.processes[0].returncode == 0
    assert rig.player.window.destroyed


def test_shutdown_waits_for_mpv_to_quit(tmp_path, monkeypatch) -> None:
    rig = Rig(tmp_path, monkeypatch)
    rig.player.play("2")
    mpv = rig.start_mpv()

    rig.player.shutdown()

    assert "quit" in mpv.commands
    assert rig.processes[0].returncode == 0
    assert rig.player.window.destroyed


def process_group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    return True


def test_closing_while_ani_cli_searches_ends_everything_it_started(tmp_path, monkeypatch) -> None:
    started: list[subprocess.Popen] = []

    def launch(_title: str, _episode: str, **_options: object) -> SimpleNamespace:
        # a stand-in ani-cli with a child of its own, in its own session like the real launch
        process = subprocess.Popen(["sh", "-c", "sleep 30 & sleep 30; wait"], start_new_session=True)
        started.append(process)
        return SimpleNamespace(process=process)

    rig = Rig(tmp_path, monkeypatch, launcher=launch)
    rig.player.play("2")
    process = started[0]
    try:
        rig.player.close()
        assert rig.pump(lambda: rig.closed == [rig.player], timeout=10)
        assert process.poll() is not None
        assert rig.pump(lambda: not process_group_alive(process.pid), timeout=5)
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
