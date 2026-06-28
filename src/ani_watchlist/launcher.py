from __future__ import annotations

import json
import os
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
CONTENT_LABEL_SUFFIX_RE = re.compile(r"\s*\[[^\]]+\]\s*$")
ALLANIME_API = "https://api.allanime.day/api"
ALLANIME_REFERER = "https://allmanga.to"
ALLANIME_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) Gecko/20100101 Firefox/121.0"
ALLANIME_SEARCH_GQL = "query( $search: SearchInput $limit: Int $page: Int $translationType: VaildTranslationTypeEnumType $countryOrigin: VaildCountryOriginEnumType ) { shows( search: $search limit: $limit page: $page translationType: $translationType countryOrigin: $countryOrigin ) { edges { _id name englishName nativeName availableEpisodes __typename } }}"
ALLANIME_EPISODES_GQL = "query ($showId: String!) { show( _id: $showId ) { _id availableEpisodesDetail }}"
DEFAULT_MPV_EXTRA_ARGS = "--video-sync=display-resample"


@dataclass(frozen=True)
class LaunchResult:
    command: list[str]
    pid: int
    used_terminal: bool


@dataclass(frozen=True)
class AllAnimeLaunchTarget:
    show_id: str
    title: str
    episode_count: int
    score: float
    query: str


@dataclass(frozen=True)
class AllAnimeEpisodeAvailability:
    target: AllAnimeLaunchTarget
    episode_keys: tuple[str, ...]


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


def _without_content_labels(title: str) -> str:
    cleaned = title.strip()
    while cleaned:
        without_label = CONTENT_LABEL_SUFFIX_RE.sub("", cleaned).strip()
        if without_label == cleaned:
            return cleaned
        cleaned = without_label
    return cleaned


def _title_norm(title: str) -> str:
    cleaned = _without_content_labels(clean_ani_cli_search_title(title)).replace("+", " ")
    cleaned = cleaned.casefold().replace("'", "").replace(chr(0x2019), "")
    cleaned = re.sub(r"[^0-9a-z]+", " ", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


def _add_title_variant(variants: list[str], title: object) -> None:
    if title is None:
        return
    cleaned = _without_content_labels(clean_ani_cli_search_title(str(title)))
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if not cleaned:
        return
    existing = {_title_norm(item) for item in variants}

    def add(value: str) -> None:
        value = _without_content_labels(clean_ani_cli_search_title(value))
        value = re.sub(r"\s+", " ", value).strip()
        norm = _title_norm(value)
        if value and norm and norm not in existing:
            variants.append(value)
            existing.add(norm)

    add(cleaned)
    if cleaned.endswith(")") and " (" in cleaned:
        primary, secondary = cleaned.rsplit(" (", 1)
        add(primary)
        add(secondary[:-1])
    add(re.sub(r"\s*\([^)]*\)", "", cleaned).strip())


def _metadata_payload_titles(payload: dict[str, object] | None) -> list[object]:
    if not isinstance(payload, dict):
        return []
    titles: list[object] = []
    title = payload.get("title")
    if isinstance(title, dict):
        for key in ("userPreferred", "english", "romaji", "native"):
            titles.append(title.get(key))
    synonyms = payload.get("synonyms")
    if isinstance(synonyms, list):
        titles.extend(synonyms)
    return titles


def ani_cli_title_variants(
    display_title: str,
    source_title: str | None = None,
    metadata_payload: dict[str, object] | None = None,
) -> list[str]:
    variants: list[str] = []
    _add_title_variant(variants, display_title)
    for title in _metadata_payload_titles(metadata_payload):
        _add_title_variant(variants, title)

    trusted_norms = {_title_norm(item) for item in variants}
    source = clean_ani_cli_search_title(source_title or "")
    source_norm = _title_norm(source)
    if source_norm and (not trusted_norms or source_norm in trusted_norms):
        _add_title_variant(variants, source)
    return variants


def choose_ani_cli_search_title(display_title: str, source_title: str | None = None) -> str:
    source = clean_ani_cli_search_title(source_title or "")
    display = clean_ani_cli_search_title(display_title)
    if source and _title_norm(source) == _title_norm(display):
        return source
    return display


def _validate_mode(mode: str) -> str:
    normalized = mode.strip().casefold()
    if normalized not in {"sub", "dub"}:
        raise LaunchError(f"unsupported playback mode: {mode}")
    return normalized


def build_ani_cli_command(
    title: str,
    episode: str,
    ani_cli: str | None = None,
    *,
    mode: str = "sub",
    allanime_id: str | None = None,
    detach_player: bool = False,
) -> list[str]:
    cleaned_title = title.strip()
    cleaned_episode = str(episode).strip()
    cleaned_mode = _validate_mode(mode)
    cleaned_allanime_id = str(allanime_id).strip() if allanime_id is not None else ""
    if not cleaned_title:
        raise LaunchError("anime title is empty")
    if not cleaned_episode:
        raise LaunchError("episode is empty")
    command = [ani_cli or resolve_ani_cli()]
    if not detach_player:
        command.append("--no-detach")
    if cleaned_allanime_id:
        command.extend(["--allanime-id", cleaned_allanime_id])
    else:
        command.extend(["--select-nth", "1"])
    command.extend(["--episode", cleaned_episode])
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


def _episode_key_from_available(value: object) -> str:
    if isinstance(value, dict):
        for key in ("episode_key", "key", "episode", "episode_number", "number", "id"):
            if value.get(key) is not None:
                return _episode_key_from_available(value[key])
        return ""
    text = str(value).strip()
    number = _episode_sort_value(text)
    if number is not None and number.is_integer() and re.fullmatch(r"\d+(?:\.0+)?", text):
        return str(int(number))
    return text


def _episode_keys_from_count(count: int | None) -> tuple[str, ...]:
    if count is None or count <= 0:
        return ()
    return tuple(str(index) for index in range(1, count + 1))


def _dedupe_episode_keys(values: list[object]) -> tuple[str, ...]:
    keys: list[str] = []
    seen: set[str] = set()
    for value in values:
        key = _episode_key_from_available(value)
        if not key or key in seen:
            continue
        keys.append(key)
        seen.add(key)
    return tuple(
        sorted(
            keys,
            key=lambda key: (
                _episode_sort_value(key) is None,
                _episode_sort_value(key) if _episode_sort_value(key) is not None else 999999.0,
                key,
            ),
        )
    )


def _allanime_search_edges(title: str, *, mode: str, timeout: int = 12) -> list[dict[str, object]]:
    cleaned_title = title.strip()
    if not cleaned_title:
        return []
    payload = _allanime_api_request(
        {
            "search": {
                "allowAdult": True,
                "allowUnknown": True,
                "query": cleaned_title.replace(" ", "+"),
            },
            "limit": 40,
            "page": 1,
            "translationType": mode,
            "countryOrigin": "ALL",
        },
        ALLANIME_SEARCH_GQL,
        timeout=timeout,
    )
    edges = (((payload.get("data") or {}).get("shows") or {}).get("edges") or []) if isinstance(payload, dict) else []
    return [item for item in edges if isinstance(item, dict)]


def _allanime_episode_count(item: dict[str, object], mode: str) -> int:
    available = item.get("availableEpisodes")
    episodes = available.get(mode) if isinstance(available, dict) else None
    try:
        return int(episodes)
    except (TypeError, ValueError):
        return 0


def _allanime_display_title(item: dict[str, object]) -> str:
    name = str(item.get("name") or "").strip()
    english = str(item.get("englishName") or "").strip()
    if english and name and english.casefold() != name.casefold():
        return f"{english} ({name})"
    return english or name


def _allanime_title_norms(item: dict[str, object]) -> set[str]:
    values = [item.get("name"), item.get("englishName"), item.get("nativeName"), _allanime_display_title(item)]
    norms = {_title_norm(str(value)) for value in values if value}
    return {norm for norm in norms if norm}


STOP_TITLE_TOKENS = {"a", "an", "the"}


def _significant_tokens(norm: str) -> set[str]:
    tokens = set(norm.split())
    return {token for token in tokens if token not in STOP_TITLE_TOKENS} or tokens


def _candidate_title_score(candidate_norm: str, variant_norm: str, has_specific_variant: bool) -> float:
    if not candidate_norm or not variant_norm:
        return 0.0
    variant_tokens = _significant_tokens(variant_norm)
    candidate_tokens = _significant_tokens(candidate_norm)
    if candidate_norm == variant_norm:
        if has_specific_variant and len(variant_tokens) < 2:
            return 92.0
        return 100.0
    if len(variant_tokens) >= 2:
        if candidate_tokens == variant_tokens:
            return 98.0
        if variant_tokens.issubset(candidate_tokens):
            return max(86.0, 94.0 - (2.0 * len(candidate_tokens - variant_tokens)))
        if candidate_norm.startswith(f"{variant_norm} "):
            return 88.0
        if variant_norm in candidate_norm:
            return 82.0
    elif candidate_norm.startswith(f"{variant_norm} "):
        return 60.0
    return 0.0


def _score_allanime_candidate(
    item: dict[str, object],
    trusted_variant_norms: list[str],
    *,
    mode: str,
    total_episodes: int | None,
) -> float:
    candidate_norms = _allanime_title_norms(item)
    has_specific_variant = any(len(_significant_tokens(norm)) >= 2 for norm in trusted_variant_norms)
    score = max(
        (
            _candidate_title_score(candidate_norm, variant_norm, has_specific_variant)
            for candidate_norm in candidate_norms
            for variant_norm in trusted_variant_norms
        ),
        default=0.0,
    )
    episode_count = _allanime_episode_count(item, mode)
    if total_episodes is not None and total_episodes > 0 and episode_count > 0:
        if episode_count == total_episodes:
            score += 4.0
        elif abs(episode_count - total_episodes) <= 1:
            score += 2.0
        elif abs(episode_count - total_episodes) >= max(4, total_episodes // 3):
            score -= 5.0
    return score


def _has_clear_episode_count_lead(top: AllAnimeLaunchTarget, challengers: list[AllAnimeLaunchTarget]) -> bool:
    if top.score < 98.0 or top.episode_count < 24:
        return False
    for challenger in challengers:
        if challenger.show_id == top.show_id:
            continue
        required = max(challenger.episode_count * 3, challenger.episode_count + 24)
        if top.episode_count < required:
            return False
    return True


def resolve_allanime_launch_target(
    display_title: str,
    source_title: str | None = None,
    metadata_payload: dict[str, object] | None = None,
    *,
    total_episodes: int | None = None,
    mode: str = "sub",
    timeout: int = 12,
) -> AllAnimeLaunchTarget | None:
    cleaned_mode = _validate_mode(mode)
    variants = ani_cli_title_variants(display_title, source_title, metadata_payload)
    trusted_norms = [_title_norm(variant) for variant in variants]
    trusted_norms = [norm for index, norm in enumerate(trusted_norms) if norm and norm not in trusted_norms[:index]]
    if not trusted_norms:
        return None

    scored_by_id: dict[str, AllAnimeLaunchTarget] = {}
    seen_query_norms: set[str] = set()
    for query in variants:
        query_norm = _title_norm(query)
        if not query_norm or query_norm in seen_query_norms:
            continue
        seen_query_norms.add(query_norm)
        for item in _allanime_search_edges(query, mode=cleaned_mode, timeout=timeout):
            episode_count = _allanime_episode_count(item, cleaned_mode)
            show_id = str(item.get("_id") or "").strip()
            title = _allanime_display_title(item)
            if not show_id or not title or episode_count <= 0:
                continue
            score = _score_allanime_candidate(
                item,
                trusted_norms,
                mode=cleaned_mode,
                total_episodes=total_episodes,
            )
            if score <= 0:
                continue
            target = AllAnimeLaunchTarget(
                show_id=show_id,
                title=f"{title} ({episode_count} episodes)",
                episode_count=episode_count,
                score=score,
                query=query,
            )
            existing = scored_by_id.get(show_id)
            if existing is None or target.score > existing.score:
                scored_by_id[show_id] = target
    scored = list(scored_by_id.values())
    scored.sort(key=lambda item: (-item.score, -item.episode_count, item.title.casefold(), item.show_id))
    if not scored or scored[0].score < 95.0:
        return None
    close_challengers = [item for item in scored[1:] if scored[0].show_id != item.show_id and scored[0].score - item.score < 1.0]
    if close_challengers and not _has_clear_episode_count_lead(scored[0], close_challengers):
        return None
    return scored[0]


def _allanime_episode_keys_for_show(
    show_id: str,
    *,
    mode: str,
    episode_count: int | None = None,
    timeout: int = 12,
) -> tuple[str, ...]:
    if not show_id:
        return ()
    episodes_payload = _allanime_api_request({"showId": str(show_id)}, ALLANIME_EPISODES_GQL, timeout=timeout)
    detail = (((episodes_payload.get("data") or {}).get("show") or {}).get("availableEpisodesDetail") or {}) if isinstance(episodes_payload, dict) else {}
    episode_list = detail.get(mode) if isinstance(detail, dict) else None
    if isinstance(episode_list, list):
        keys = _dedupe_episode_keys(episode_list)
        if keys:
            return keys
    return _episode_keys_from_count(episode_count)


def allanime_available_episode_keys(
    display_title: str,
    source_title: str | None = None,
    metadata_payload: dict[str, object] | None = None,
    *,
    total_episodes: int | None = None,
    mode: str = "sub",
    timeout: int = 12,
) -> AllAnimeEpisodeAvailability | None:
    cleaned_mode = _validate_mode(mode)
    target = resolve_allanime_launch_target(
        display_title,
        source_title,
        metadata_payload,
        total_episodes=total_episodes,
        mode=cleaned_mode,
        timeout=timeout,
    )
    if target is None:
        return None
    try:
        episode_keys = _allanime_episode_keys_for_show(
            target.show_id,
            mode=cleaned_mode,
            episode_count=target.episode_count,
            timeout=timeout,
        )
    except LaunchError:
        episode_keys = _episode_keys_from_count(target.episode_count)
    return AllAnimeEpisodeAvailability(target=target, episode_keys=episode_keys)


def _allanime_episode_available_for_show(
    show_id: str,
    episode: str,
    *,
    mode: str,
    episode_count: int | None = None,
    timeout: int = 12,
) -> bool:
    if not show_id:
        return False
    episodes_payload = _allanime_api_request({"showId": str(show_id)}, ALLANIME_EPISODES_GQL, timeout=timeout)
    detail = (((episodes_payload.get("data") or {}).get("show") or {}).get("availableEpisodesDetail") or {}) if isinstance(episodes_payload, dict) else {}
    episode_list = detail.get(mode) if isinstance(detail, dict) else None
    if isinstance(episode_list, list):
        return any(_episode_values_match(episode, item) for item in episode_list)
    requested_number = _episode_sort_value(episode)
    return requested_number is not None and episode_count is not None and 0 < requested_number <= episode_count


def allanime_episode_available(
    title: str,
    episode: str,
    *,
    mode: str = "sub",
    show_id: str | None = None,
    episode_count: int | None = None,
    timeout: int = 12,
) -> bool:
    cleaned_title = title.strip()
    cleaned_episode = str(episode).strip()
    cleaned_mode = _validate_mode(mode)
    cleaned_show_id = str(show_id).strip() if show_id is not None else ""
    if not cleaned_episode:
        return False
    if cleaned_show_id:
        return _allanime_episode_available_for_show(
            cleaned_show_id,
            cleaned_episode,
            mode=cleaned_mode,
            episode_count=episode_count,
            timeout=timeout,
        )
    if not cleaned_title:
        return False
    selected: dict[str, object] | None = None
    selected_episode_count: int | None = None
    for item in _allanime_search_edges(cleaned_title, mode=cleaned_mode, timeout=timeout):
        count = _allanime_episode_count(item, cleaned_mode)
        if count <= 0:
            continue
        selected = item
        selected_episode_count = count
        break
    if selected is None:
        return False
    show_id = selected.get("_id")
    if not show_id:
        return False
    return _allanime_episode_available_for_show(
        str(show_id),
        cleaned_episode,
        mode=cleaned_mode,
        episode_count=selected_episode_count,
        timeout=timeout,
    )


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


def launch_episode(
    title: str,
    episode: str,
    *,
    mode: str = "sub",
    prefer_terminal: bool = True,
    allanime_id: str | None = None,
    mpv_ipc_path: str | None = None,
    mpv_wid: int | str | None = None,
    detach_player: bool = False,
    quiet: bool = False,
) -> LaunchResult:
    command = build_ani_cli_command(
        title,
        episode,
        mode=mode,
        allanime_id=allanime_id,
        detach_player=detach_player,
    )
    used_terminal = False
    if prefer_terminal:
        command, used_terminal = build_terminal_command(command)
    env = None
    mpv_extra_args = os.environ.get("ANI_WATCH_MPV_EXTRA_ARGS", DEFAULT_MPV_EXTRA_ARGS)
    if mpv_ipc_path or mpv_wid or mpv_extra_args:
        env = os.environ.copy()
        if mpv_ipc_path:
            env["ANI_WATCH_MPV_IPC"] = str(mpv_ipc_path)
        if mpv_wid:
            env["ANI_WATCH_MPV_WID"] = str(mpv_wid)
        if "ANI_WATCH_MPV_EXTRA_ARGS" not in env and mpv_extra_args:
            env["ANI_WATCH_MPV_EXTRA_ARGS"] = mpv_extra_args
    try:
        stdout = subprocess.DEVNULL if quiet else None
        stderr = subprocess.DEVNULL if quiet else None
        if env is None:
            process = subprocess.Popen(command, start_new_session=True, stdout=stdout, stderr=stderr)
        else:
            process = subprocess.Popen(command, start_new_session=True, env=env, stdout=stdout, stderr=stderr)
    except OSError as exc:
        raise LaunchError(str(exc)) from exc
    return LaunchResult(command=command, pid=process.pid, used_terminal=used_terminal)
