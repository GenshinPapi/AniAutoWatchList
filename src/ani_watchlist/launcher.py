from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


TERMINAL_CANDIDATES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("x-terminal-emulator", ()),
    ("gnome-terminal", ()),
    ("mate-terminal", ("-x",)),
    ("xfce4-terminal", ("-x",)),
    ("konsole", ("-e",)),
    ("xterm", ("-e",)),
    ("uxterm", ("-e",)),
    ("alacritty", ("-e",)),
    ("kitty", ("--",)),
    ("wezterm", ("start", "--")),
)

LAUNCH_WRAPPER = r"""
printf 'Launching ani-cli from ani-watchlist...\n\n'
"$@"
code=$?
if [ "$code" -ne 0 ]; then
    printf '\nani-cli exited with code %s.\n' "$code"
    printf 'Press Enter to close this terminal.'
    read -r _unused
fi
exit "$code"
""".strip()


@dataclass(frozen=True)
class LaunchResult:
    command: list[str]
    pid: int
    used_terminal: bool


class LaunchError(RuntimeError):
    pass


def resolve_ani_cli() -> str:
    path = shutil.which("ani-cli")
    if path:
        return path
    local_path = Path.home() / ".local" / "bin" / "ani-cli"
    if local_path.exists():
        return str(local_path)
    raise LaunchError("ani-cli was not found on PATH or at ~/.local/bin/ani-cli")


def build_ani_cli_command(title: str, episode: str, ani_cli: str | None = None) -> list[str]:
    cleaned_title = title.strip()
    cleaned_episode = str(episode).strip()
    if not cleaned_title:
        raise LaunchError("anime title is empty")
    if not cleaned_episode:
        raise LaunchError("episode is empty")
    return [ani_cli or resolve_ani_cli(), "--no-detach", "--episode", cleaned_episode, cleaned_title]


def terminal_args_for(terminal_name: str, terminal_path: str) -> tuple[str, ...]:
    names = {Path(terminal_name).name.casefold(), Path(terminal_path).name.casefold()}
    try:
        names.add(Path(terminal_path).resolve().name.casefold())
    except OSError:
        pass
    joined = " ".join(sorted(names))
    if "gnome-terminal" in joined:
        return ("--",)
    if "kitty" in joined:
        return ("--",)
    if "wezterm" in joined:
        return ("start", "--")
    if "mate-terminal" in joined or "xfce4-terminal" in joined:
        return ("-x",)
    return ("-e",)


def build_shell_command(command: list[str]) -> list[str]:
    return ["bash", "-lc", LAUNCH_WRAPPER, "ani-watch-launch", *command]


def build_terminal_command(command: list[str]) -> tuple[list[str], bool]:
    shell_command = build_shell_command(command)
    for terminal, args in TERMINAL_CANDIDATES:
        terminal_path = shutil.which(terminal)
        if terminal_path:
            terminal_args = args or terminal_args_for(terminal, terminal_path)
            return [terminal_path, *terminal_args, *shell_command], True
    return shell_command, False


def launch_episode(title: str, episode: str, *, prefer_terminal: bool = True) -> LaunchResult:
    command = build_ani_cli_command(title, episode)
    used_terminal = False
    if prefer_terminal:
        command, used_terminal = build_terminal_command(command)
    try:
        process = subprocess.Popen(command, start_new_session=True)
    except OSError as exc:
        raise LaunchError(str(exc)) from exc
    return LaunchResult(command=command, pid=process.pid, used_terminal=used_terminal)
