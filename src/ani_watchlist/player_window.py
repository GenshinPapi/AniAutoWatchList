"""Play an episode inside AniAutoWatchList instead of a terminal.

The window embeds mpv the way the watch party does (``--wid``) and drives it over mpv's IPC socket,
so play/pause, seeking, fullscreen, and moving to the previous or next episode all happen in one
place. ani-cli still resolves the stream and records progress through its hooks. It runs for as
long as the episode plays (``--exit-after-play``), which tells the window when an episode ended and
whether ani-cli gave up before the player ever started.
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
import threading
import tkinter as tk
from dataclasses import dataclass, field
from pathlib import Path
from tkinter import ttk
from typing import Any, Callable

from .launcher import LaunchError, launch_episode
from .party import MpvIpcController, party_ipc_path


PLAYER_POLL_INTERVAL_SECONDS = 0.5
PLAYER_STOP_TIMEOUT_SECONDS = 4.0
PLAYER_KILL_TIMEOUT_SECONDS = 2.0
PLAYER_SEEK_SECONDS = 10
# mpv's own terminal output would only fill the log with status lines nobody reads
PLAYER_MPV_EXTRA_ARGS = "--no-terminal"
PLAYER_LOG_NAME = "player.log"
PLAYER_LOG_READ_LIMIT = 65536
TERMINAL_ESCAPE_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07|\x1b[78=>]")
# ani-cli's own chatter, and warnings graphics and video libraries print once mpv starts
NOISE_LINE_RE = re.compile(
    r"^(checking dependencies|tput:|/|libegl warning|mesa|libva|failed to open vdpau|fontconfig|attention:|\[)",
    re.IGNORECASE,
)
LINKS_FETCHED_RE = re.compile(r"^(\S+) Links Fetched$")
PRIMARY_BUTTON_LABELS = {
    "loading": "Pause",
    "playing": "Pause",
    "paused": "Play",
    "finished": "Replay",
    "failed": "Retry",
}


def format_playback_clock(seconds: object) -> str:
    """Seconds as m:ss style clock text, or dashes while the player cannot say."""
    try:
        total = int(float(seconds))  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return "--:--"
    if total < 0:
        return "--:--"
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def ani_cli_status_line(output: str) -> str | None:
    """The latest line of ani-cli output worth showing, without its terminal escape codes."""
    for raw in reversed(output.splitlines()):
        cleaned = TERMINAL_ESCAPE_RE.sub("", raw)
        segments = [segment.strip() for segment in cleaned.split("\r") if segment.strip()]
        if not segments:
            continue
        line = segments[-1]
        if NOISE_LINE_RE.match(line):
            continue
        return line
    return None


def ani_cli_provider(output: str) -> str | None:
    """The provider of the last "<provider> Links Fetched" line in ani-cli output."""
    provider = None
    for raw in output.splitlines():
        line = TERMINAL_ESCAPE_RE.sub("", raw).split("\r")[-1].strip()
        match = LINKS_FETCHED_RE.match(line)
        if match:
            provider = match.group(1)
    return provider


def neighbor_episode(episodes: list[str], current: str, direction: int) -> str | None:
    """The episode before (-1) or after (1) the current one in the watchlist's order."""
    if current not in episodes:
        return None
    index = episodes.index(current) + direction
    if 0 <= index < len(episodes):
        return episodes[index]
    return None


def embed_window_id(widget: tk.Misc) -> int | None:
    """The X11 window id mpv can draw into, or None to let mpv open a window of its own."""
    if os.environ.get("ANI_WATCH_PLAYER_DISABLE_EMBED") == "1":
        return None
    try:
        if str(widget.tk.call("tk", "windowingsystem")).casefold() != "x11":
            return None
        widget.update_idletasks()
        window_id = int(widget.winfo_id())
    except (tk.TclError, TypeError, ValueError):
        return None
    return window_id if window_id > 0 else None


@dataclass(frozen=True)
class PlayerShow:
    anime_id: int
    title: str
    launch_title: str
    mode: str
    allanime_id: str | None = None


@dataclass
class PlayerSession:
    episode: str
    ipc_path: str
    log_path: Path
    controller: MpvIpcController
    process: Any = None
    player_started: bool = False
    stop_requested: bool = False
    ended: bool = False
    last_status: str | None = None
    provider: str | None = None
    stop_polling: threading.Event = field(default_factory=threading.Event)


class EpisodePlayer:
    """A window that plays one show's episodes with embedded mpv and its own controls."""

    def __init__(
        self,
        root: tk.Misc,
        show: PlayerShow,
        *,
        colors: dict[str, str],
        log_dir: Path,
        run_on_ui: Callable[[Callable[[], None]], None],
        episodes_for: Callable[[int], list[str]],
        on_episode_end: Callable[[int], None] | None = None,
        on_activity: Callable[[], None] | None = None,
        on_close: Callable[["EpisodePlayer"], None] | None = None,
        launcher: Callable[..., Any] = launch_episode,
    ) -> None:
        self.root = root
        self.show = show
        self.colors = colors
        self.log_dir = Path(log_dir)
        self.run_on_ui = run_on_ui
        self.episodes_for = episodes_for
        self.on_episode_end = on_episode_end or (lambda _anime_id: None)
        self.on_activity = on_activity or (lambda: None)
        self.on_close = on_close or (lambda _player: None)
        self.launcher = launcher
        self.session: PlayerSession | None = None
        self.current_episode: str | None = None
        self.pending_episode: str | None = None
        self.transitioning = False
        self.session_counter = 0
        self.state = "loading"
        self.closed = False
        self.fullscreen = False
        self.mpv_fullscreen: bool | None = None
        self._build()
        self.window_id = embed_window_id(self.video_frame)
        if self.window_id is None:
            self._note_external_player()

    # window

    def _note_external_player(self) -> None:
        tk.Label(
            self.video_frame,
            text="The video opens in its own mpv window. Use the controls below to drive it.",
            bg="#000000",
            fg=self.colors["muted"],
        ).place(relx=0.5, rely=0.5, anchor="center")

    def _build(self) -> None:
        colors = self.colors
        win = tk.Toplevel(self.root)
        win.configure(bg=colors["bg"])
        win.geometry("1280x800")
        win.minsize(640, 420)
        win.columnconfigure(0, weight=1)
        win.rowconfigure(1, weight=1)
        win.protocol("WM_DELETE_WINDOW", self.close)
        self.window = win

        header = tk.Frame(win, bg=colors["bg"])
        header.grid(row=0, column=0, sticky="ew", padx=12, pady=(10, 6))
        header.columnconfigure(0, weight=1)
        self.header = header
        self.title_label = tk.Label(header, text="", bg=colors["bg"], fg=colors["text"], font=("", 14, "bold"), anchor="w")
        self.title_label.grid(row=0, column=0, sticky="ew")
        self.episode_label = tk.Label(header, text="", bg=colors["bg"], fg=colors["muted"], anchor="w")
        self.episode_label.grid(row=1, column=0, sticky="ew")
        self.status_label = tk.Label(
            header, text="", bg=colors["bg"], fg=colors["muted"], anchor="w", justify="left", wraplength=1200
        )
        self.status_label.grid(row=2, column=0, sticky="ew", pady=(4, 0))

        panel = tk.Frame(win, bg="#000000", highlightthickness=1, highlightbackground=colors["border"])
        panel.grid(row=1, column=0, sticky="nsew", padx=12)
        panel.columnconfigure(0, weight=1)
        panel.rowconfigure(0, weight=1)
        self.video_panel = panel
        self.video_frame = tk.Frame(panel, bg="#000000", width=960, height=540)
        self.video_frame.grid(row=0, column=0, sticky="nsew")
        self.video_frame.grid_propagate(False)
        self.video_frame.bind("<Double-Button-1>", lambda _event: self.toggle_fullscreen())

        controls = tk.Frame(win, bg=colors["bg"])
        controls.grid(row=2, column=0, sticky="ew", padx=12, pady=(8, 12))
        controls.columnconfigure(5, weight=1)
        self.controls = controls

        def button(column: int, text: str, command: Callable[[], None], style: str = "Compact.Dark.TButton", width: int | None = None):
            # buttons never take keyboard focus, so space and the arrow keys always reach the player
            widget = ttk.Button(controls, text=text, style=style, command=command, takefocus=False)
            if width is not None:
                widget.configure(width=width)
            widget.grid(row=0, column=column, padx=(0, 6), sticky="w")
            return widget

        self.prev_button = button(0, "Prev", self.previous_episode)
        self.back_button = button(1, f"-{PLAYER_SEEK_SECONDS}s", lambda: self.seek(-PLAYER_SEEK_SECONDS))
        self.play_button = button(2, "Pause", self.primary_action, style="Compact.Accent.TButton", width=8)
        self.forward_button = button(3, f"+{PLAYER_SEEK_SECONDS}s", lambda: self.seek(PLAYER_SEEK_SECONDS))
        self.next_button = button(4, "Next", self.next_episode)
        self.time_label = tk.Label(controls, text="--:-- / --:--", bg=colors["bg"], fg=colors["muted"], font="TkFixedFont")
        self.time_label.grid(row=0, column=5, sticky="e", padx=12)
        self.fullscreen_button = button(6, "Fullscreen", self.toggle_fullscreen)
        self.close_button = button(7, "Close", self.close)

        win.bind("<space>", lambda _event: self.primary_action())
        win.bind("<Left>", lambda _event: self.seek(-PLAYER_SEEK_SECONDS))
        win.bind("<Right>", lambda _event: self.seek(PLAYER_SEEK_SECONDS))
        win.bind("<F11>", lambda _event: self.toggle_fullscreen())
        win.bind("<Escape>", lambda _event: self.set_fullscreen(False))

    def _set_labels(self, episode: str) -> None:
        mode = "Dub" if self.show.mode == "dub" else "Sub"
        self.title_label.configure(text=self.show.title)
        self.episode_label.configure(text=f"Episode {episode} · {mode}")
        try:
            self.window.title(f"{self.show.title} - Episode {episode}")
        except tk.TclError:
            pass

    def _set_status(self, text: str, *, error: bool = False) -> None:
        self.status_label.configure(text=text, fg=self.colors["danger"] if error else self.colors["muted"])

    def _set_state(self, state: str) -> None:
        changed = state != self.state
        self.state = state
        self.play_button.configure(
            text=PRIMARY_BUTTON_LABELS.get(state, "Play"),
            state="disabled" if state == "loading" else "normal",
        )
        seekable = "normal" if state in ("playing", "paused") else "disabled"
        self.back_button.configure(state=seekable)
        self.forward_button.configure(state=seekable)
        if changed:
            self._update_navigation()

    def _update_navigation(self) -> None:
        episode = self.current_episode
        try:
            episodes = [str(key) for key in self.episodes_for(self.show.anime_id)]
        except Exception:
            episodes = []
        for widget, direction in ((self.prev_button, -1), (self.next_button, 1)):
            available = episode is not None and neighbor_episode(episodes, episode, direction) is not None
            widget.configure(state="normal" if available else "disabled")

    def raise_window(self) -> None:
        try:
            self.window.deiconify()
            self.window.lift()
            self.window.focus_set()
        except tk.TclError:
            pass

    # playback controls

    def load(self, show: PlayerShow, episode: str) -> None:
        """Play an episode of this or another show in the same window."""
        self.show = show
        self.raise_window()
        self.play(episode)

    def play(self, episode: str) -> None:
        if self.closed:
            return
        self.current_episode = episode
        self.pending_episode = episode
        self._set_labels(episode)
        self._set_status(f"Finding a stream for episode {episode}...")
        self.time_label.configure(text="--:-- / --:--")
        self._set_state("loading")
        self._update_navigation()
        if self.transitioning:
            # the episode that is stopping starts whatever was asked for last once it is gone
            return
        self.transitioning = True
        previous, self.session = self.session, None
        self._stop_session(previous, then=self._start_pending)

    def primary_action(self) -> None:
        if self.state in ("playing", "paused"):
            self.toggle_pause()
        elif self.state in ("finished", "failed") and self.current_episode is not None:
            self.play(self.current_episode)

    def toggle_pause(self) -> None:
        session = self.session
        if session is None or self.state not in ("playing", "paused"):
            return
        resume = self.state == "paused"
        try:
            if resume:
                session.controller.play()
            else:
                session.controller.pause()
        except OSError:
            return
        self._set_state("playing" if resume else "paused")

    def seek(self, delta_seconds: float) -> None:
        session = self.session
        if session is None or self.state not in ("playing", "paused"):
            return
        try:
            session.controller.relative_seek(delta_seconds)
        except OSError:
            pass

    def next_episode(self) -> None:
        self._change_episode(1)

    def previous_episode(self) -> None:
        self._change_episode(-1)

    def _change_episode(self, direction: int) -> None:
        if self.current_episode is None:
            return
        episodes = [str(key) for key in self.episodes_for(self.show.anime_id)]
        target = neighbor_episode(episodes, self.current_episode, direction)
        if target is not None:
            self.play(target)

    def toggle_fullscreen(self) -> None:
        self.set_fullscreen(not self.fullscreen)

    def set_fullscreen(self, enabled: bool, *, update_mpv: bool = True) -> None:
        enabled = bool(enabled)
        if self.closed or self.fullscreen == enabled:
            return
        self.fullscreen = enabled
        try:
            self.window.attributes("-fullscreen", enabled)
        except tk.TclError:
            pass
        if enabled:
            self.header.grid_remove()
            self.controls.grid_remove()
            self.video_panel.grid_configure(padx=0)
            self.video_panel.configure(highlightthickness=0)
        else:
            self.header.grid()
            self.controls.grid()
            self.video_panel.grid_configure(padx=12)
            self.video_panel.configure(highlightthickness=1)
        session = self.session
        if update_mpv and session is not None and session.controller.available():
            # mpv keeps its own fullscreen flag, which its f key and double-click toggle
            try:
                session.controller.set_fullscreen(enabled)
                self.mpv_fullscreen = enabled
            except OSError:
                pass

    def close(self) -> None:
        if self.closed:
            return
        self.set_fullscreen(False, update_mpv=False)
        self.closed = True
        try:
            # the player keeps running hidden until it has quit and ani-cli has recorded progress
            self.window.withdraw()
        except tk.TclError:
            pass
        session, self.session = self.session, None
        self._stop_session(session, then=self._destroy)

    def shutdown(self) -> None:
        """Stop playback before the app exits, waiting for the player to quit."""
        self.closed = True
        session, self.session = self.session, None
        if session is not None:
            session.stop_requested = True
            self._stop_process(session)
        try:
            self.window.destroy()
        except tk.TclError:
            pass

    def _destroy(self) -> None:
        try:
            self.window.destroy()
        except tk.TclError:
            pass
        self.on_close(self)

    # sessions

    def _start_pending(self) -> None:
        self.transitioning = False
        episode, self.pending_episode = self.pending_episode, None
        if self.closed or episode is None:
            return
        self._start(episode)

    def _start(self, episode: str) -> None:
        self.session_counter += 1
        ipc_path = party_ipc_path(f"player-{os.getpid()}-{self.session_counter}")
        try:
            Path(ipc_path).unlink(missing_ok=True)
        except OSError:
            pass
        log_path = self.log_dir / PLAYER_LOG_NAME
        session = PlayerSession(episode=episode, ipc_path=ipc_path, log_path=log_path, controller=MpvIpcController(ipc_path))
        self.session = session
        self.mpv_fullscreen = None
        try:
            self.log_dir.mkdir(parents=True, exist_ok=True)
            result = self.launcher(
                self.show.launch_title,
                episode,
                mode=self.show.mode,
                allanime_id=self.show.allanime_id,
                mpv_ipc_path=ipc_path,
                mpv_wid=self.window_id,
                prefer_terminal=False,
                exit_after_play=True,
                log_path=log_path,
                extra_mpv_args=PLAYER_MPV_EXTRA_ARGS,
            )
        except (LaunchError, OSError) as exc:
            session.ended = True
            self._set_status(f"Could not start ani-cli: {exc}", error=True)
            self._set_state("failed")
            return
        session.process = getattr(result, "process", None)
        threading.Thread(target=self._poll, args=(session,), daemon=True).start()

    def _stop_session(self, session: PlayerSession | None, *, then: Callable[[], None]) -> None:
        if session is None or session.ended or session.process is None:
            if session is not None:
                session.stop_polling.set()
            then()
            return
        session.stop_requested = True

        def worker() -> None:
            self._stop_process(session)
            self.run_on_ui(lambda: self.on_episode_end(self.show.anime_id))
            self.run_on_ui(then)

        threading.Thread(target=worker, daemon=True).start()

    def _stop_process(self, session: PlayerSession) -> None:
        process = session.process
        try:
            if process is None or process.poll() is not None:
                return
            quit_sent = False
            if session.controller.available():
                # a normal quit lets ani-cli record how far the episode got
                try:
                    session.controller.stop()
                    quit_sent = True
                except OSError:
                    quit_sent = False
            if quit_sent:
                try:
                    process.wait(timeout=PLAYER_STOP_TIMEOUT_SECONDS)
                    return
                except subprocess.TimeoutExpired:
                    pass
            # still looking for a stream, or the player hung: end ani-cli and everything it started
            self._signal_group(process, signal.SIGTERM)
            try:
                process.wait(timeout=PLAYER_KILL_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                self._signal_group(process, signal.SIGKILL)
        finally:
            session.stop_polling.set()

    @staticmethod
    def _signal_group(process: Any, sig: int) -> None:
        try:
            os.killpg(process.pid, sig)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                process.send_signal(sig)
            except (ProcessLookupError, OSError):
                pass

    def _poll(self, session: PlayerSession) -> None:
        offset = 0
        partial = ""
        while not session.stop_polling.is_set():
            code = session.process.poll()
            text, offset = self._read_log(session.log_path, offset)
            # a line still being written is kept for the next read, unless ani-cli has exited
            complete, _newline, partial = (partial + text).rpartition("\n")
            if code is not None and partial:
                complete, partial = f"{complete}\n{partial}", ""
            status = ani_cli_status_line(complete) if complete else None
            provider = ani_cli_provider(complete) if complete else None
            snapshot = self._read_player(session.controller) if code is None else None
            self.run_on_ui(
                lambda snap=snapshot, line=status, found=provider, exit_code=code: self._apply(session, snap, line, found, exit_code)
            )
            if code is not None:
                return
            session.stop_polling.wait(PLAYER_POLL_INTERVAL_SECONDS)

    @staticmethod
    def _read_log(path: Path, offset: int) -> tuple[str, int]:
        try:
            with open(path, "rb") as handle:
                handle.seek(offset)
                data = handle.read(PLAYER_LOG_READ_LIMIT)
        except OSError:
            return "", offset
        return data.decode("utf-8", errors="replace"), offset + len(data)

    @staticmethod
    def _read_player(controller: MpvIpcController) -> dict[str, Any] | None:
        if not controller.available():
            return None
        try:
            position = controller.time_position()
            if position is None:
                return None
            return {
                "position": position,
                "duration": controller.get_property("duration"),
                "paused": bool(controller.get_property("pause")),
                "fullscreen": controller.get_property("fullscreen"),
            }
        except OSError:
            return None

    def _apply(
        self,
        session: PlayerSession,
        snapshot: dict[str, Any] | None,
        status: str | None,
        provider: str | None,
        code: int | None,
    ) -> None:
        if self.closed or session is not self.session or session.ended:
            return
        if status:
            session.last_status = status
        if provider:
            session.provider = provider
        if snapshot is not None:
            session.player_started = True
            paused = bool(snapshot.get("paused"))
            self._set_state("paused" if paused else "playing")
            self.time_label.configure(
                text=f"{format_playback_clock(snapshot.get('position'))} / {format_playback_clock(snapshot.get('duration'))}"
            )
            self._set_status(self._playing_text(session, paused))
            fullscreen = snapshot.get("fullscreen")
            if fullscreen is not None:
                fullscreen = bool(fullscreen)
                if self.mpv_fullscreen is None:
                    self.mpv_fullscreen = fullscreen
                elif fullscreen != self.mpv_fullscreen:
                    # f or a double-click inside the video changed mpv's flag; follow it with the window
                    self.mpv_fullscreen = fullscreen
                    self.set_fullscreen(fullscreen, update_mpv=False)
            if not paused:
                self.on_activity()
        elif not session.player_started and status:
            self._set_status(status)
        if code is not None:
            self._end(session, code)

    def _playing_text(self, session: PlayerSession, paused: bool) -> str:
        where = f" from {session.provider}" if session.provider else ""
        verb = "Paused" if paused else "Playing"
        return f"{verb} episode {session.episode}{where}."

    def _end(self, session: PlayerSession, code: int) -> None:
        session.ended = True
        session.stop_polling.set()
        self.on_episode_end(self.show.anime_id)
        if code == 0:
            self._set_state("finished")
            self._set_status(f"Finished episode {session.episode}.")
        elif session.player_started:
            self._set_state("failed")
            self._set_status(f"The player stopped unexpectedly (exit code {code}).", error=True)
        elif session.provider and LINKS_FETCHED_RE.match(session.last_status or ""):
            self._set_state("failed")
            self._set_status(
                f"Found a stream on {session.provider}, but the player could not start it (exit code {code}).", error=True
            )
        else:
            self._set_state("failed")
            self._set_status(session.last_status or f"ani-cli exited with code {code}.", error=True)
