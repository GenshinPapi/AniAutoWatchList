from __future__ import annotations

import json
import shutil
import subprocess
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path


TERMINAL_CANDIDATES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("x-terminal-emulator", ("-e",)),
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
printf 'Command:'
printf ' %q' "$@"
printf '\n\n'
"$@"
code=$?
if [ "$code" -ne 0 ]; then
    printf '\nani-cli exited with code %s.\n' "$code"
    printf 'Press Enter to close this terminal.'
    read -r _unused
fi
exit "$code"
""".strip()

EPISODE_COUNT_SUFFIX_RE = re.compile(r"\s*\(\s*\d+(?:\.\d+)?\s+episodes?\s*\)\s*$", re.IGNORECASE)
SHORT_SOURCE_SUFFIX_RE = re.compile(r"\s*\((?=[A-Za-z0-9_-]{1,8}\))(?=[A-Za-z0-9_-]*[A-Za-z])(?=[A-Za-z0-9_-]*\d)[A-Za-z0-9_-]+\)\s*$")
ALLANIME_API = "https://api.allanime.day/api"
ALLANIME_REFERER = "https://allmanga.to"
ALLANIME_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) Gecko/20100101 Firefox/121.0"
ALLANIME_SEARCH_GQL = "query( $search: SearchInput $limit: Int $page: Int $translationType: VaildTranslationTypeEnumType $countryOrigin: VaildCountryOriginEnumType ) { shows( search: $search limit: $limit page: $page translationType: $translationType countryOrigin: $countryOrigin ) { edges { _id name englishName nativeName availableEpisodes __typename } }}"
ALLANIME_EPISODES_GQL = "query ($showId: String!) { show( _id: $showId ) { _id availableEpisodesDetail }}"


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


def clean_ani_cli_search_title(title: str) -> str:
    cleaned = title.strip()
    if not cleaned:
        return cleaned
    had_episode_count = bool(EPISODE_COUNT_SUFFIX_RE.search(cleaned))
    cleaned = EPISODE_COUNT_SUFFIX_RE.sub("", cleaned).strip()
    if had_episode_count:
        cleaned = SHORT_SOURCE_SUFFIX_RE.sub("", cleaned).strip()
    return cleaned or title.strip()


def choose_ani_cli_search_title(display_title: str, source_title: str | None = None) -> str:
    source = clean_ani_cli_search_title(source_title or "")
    display = clean_ani_cli_search_title(display_title)
    if source:
        return source
    return display


def _validate_mode(mode: str) -> str:
    normalized = mode.strip().casefold()
    if normalized not in {"sub", "dub"}:
        raise LaunchError(f"unsupported playback mode: {mode}")
    return normalized


def build_ani_cli_command(title: str, episode: str, ani_cli: str | None = None, *, mode: str = "sub") -> list[str]:
    cleaned_title = title.strip()
    cleaned_episode = str(episode).strip()
    cleaned_mode = _validate_mode(mode)
    if not cleaned_title:
        raise LaunchError("anime title is empty")
    if not cleaned_episode:
        raise LaunchError("episode is empty")
    command = [
        ani_cli or resolve_ani_cli(),
        "--no-detach",
        "--select-nth",
        "1",
        "--episode",
        cleaned_episode,
    ]
    if cleaned_mode == "dub":
        command.append("--dub")
    command.append(cleaned_title)
    return command


def _allanime_api_request(variables: dict[str, object], query: str, *, timeout: int = 12) -> dict[str, object]:
    data = json.dumps({"variables": variables, "query": query}).encode("utf-8")
    request = urllib.request.Request(
        ALLANIME_API,
        data=data,
        headers={
            "Content-Type": "application/json",
            "Referer": ALLANIME_REFERER,
            "User-Agent": ALLANIME_AGENT,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        raise LaunchError(f"failed to check AllAnime availability: {exc}") from exc
    if isinstance(payload, dict):
        return payload
    raise LaunchError("failed to check AllAnime availability: unexpected response")


def _episode_sort_value(episode: str) -> float | None:
    try:
        return float(str(episode).strip())
    except ValueError:
        return None


def _episode_values_match(requested: str, available: object) -> bool:
    available_key = str(available).strip()
    if requested == available_key:
        return True
    requested_number = _episode_sort_value(requested)
    available_number = _episode_sort_value(available_key)
    return requested_number is not None and available_number is not None and requested_number == available_number


def allanime_episode_available(title: str, episode: str, *, mode: str = "sub", timeout: int = 12) -> bool:
    cleaned_title = title.strip()
    cleaned_episode = str(episode).strip()
    cleaned_mode = _validate_mode(mode)
    if not cleaned_title or not cleaned_episode:
        return False
    search_payload = _allanime_api_request(
        {
            "search": {
                "allowAdult": True,
                "allowUnknown": True,
                "query": cleaned_title.replace(" ", "+"),
            },
            "limit": 40,
            "page": 1,
            "translationType": cleaned_mode,
            "countryOrigin": "ALL",
        },
        ALLANIME_SEARCH_GQL,
        timeout=timeout,
    )
    edges = (((search_payload.get("data") or {}).get("shows") or {}).get("edges") or []) if isinstance(search_payload, dict) else []
    selected: dict[str, object] | None = None
    episode_count: int | None = None
    for item in edges:
        if not isinstance(item, dict):
            continue
        episodes = ((item.get("availableEpisodes") or {}).get(cleaned_mode)) if isinstance(item.get("availableEpisodes"), dict) else None
        try:
            count = int(episodes)
        except (TypeError, ValueError):
            continue
        if count <= 0:
            continue
        selected = item
        episode_count = count
        break
    if selected is None:
        return False
    show_id = selected.get("_id")
    if not show_id:
        return False
    episodes_payload = _allanime_api_request({"showId": str(show_id)}, ALLANIME_EPISODES_GQL, timeout=timeout)
    detail = (((episodes_payload.get("data") or {}).get("show") or {}).get("availableEpisodesDetail") or {}) if isinstance(episodes_payload, dict) else {}
    episode_list = detail.get(cleaned_mode) if isinstance(detail, dict) else None
    if isinstance(episode_list, list):
        return any(_episode_values_match(cleaned_episode, item) for item in episode_list)
    requested_number = _episode_sort_value(cleaned_episode)
    return requested_number is not None and episode_count is not None and 0 < requested_number <= episode_count


def terminal_args_for(terminal_name: str, terminal_path: str) -> tuple[str, ...]:
    names = {Path(terminal_name).name.casefold(), Path(terminal_path).name.casefold()}
    try:
        names.add(Path(terminal_path).resolve().name.casefold())
    except OSError:
        pass
    joined = " ".join(sorted(names))
    if "x-terminal-emulator" in joined:
        return ("-e",)
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


def launch_episode(title: str, episode: str, *, mode: str = "sub", prefer_terminal: bool = True) -> LaunchResult:
    command = build_ani_cli_command(title, episode, mode=mode)
    used_terminal = False
    if prefer_terminal:
        command, used_terminal = build_terminal_command(command)
    try:
        process = subprocess.Popen(command, start_new_session=True)
    except OSError as exc:
        raise LaunchError(str(exc)) from exc
    return LaunchResult(command=command, pid=process.pid, used_terminal=used_terminal)
