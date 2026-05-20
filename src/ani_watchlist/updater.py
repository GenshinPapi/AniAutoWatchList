from __future__ import annotations

import base64
import json
import re
import shutil
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import __version__
from .launcher import TERMINAL_CANDIDATES, terminal_args_for


GITHUB_REPO = "GenshinPapi/AniAutoWatchList"
GITHUB_BRANCH = "main"
GITHUB_API_COMMIT_URL = f"https://api.github.com/repos/{GITHUB_REPO}/commits/{GITHUB_BRANCH}"
GITHUB_CONTENT_INIT_URL = f"https://api.github.com/repos/{GITHUB_REPO}/contents/src/ani_watchlist/__init__.py?ref={GITHUB_BRANCH}"
USER_AGENT = "ani-watchlist-update-check/0.1"
VERSION_RE = re.compile(r"__version__\s*=\s*['\"]([^'\"]+)['\"]")
UPDATE_SCRIPT = r"""
set -eu
repo=$1
cd "$repo"
printf 'Updating AniAutoWatchList from GitHub...\n\n'
git pull --ff-only origin main
scripts/install-user.sh
printf '\nUpdate complete. Close this terminal and relaunch ani-watch-gui.\n'
printf 'Press Enter to close this terminal.'
read -r _unused
""".strip()


@dataclass(frozen=True)
class UpdateInfo:
    update_available: bool
    local_version: str
    remote_version: str | None = None
    local_commit: str | None = None
    remote_commit: str | None = None
    remote_url: str | None = None
    remote_message: str | None = None
    reason: str | None = None


@dataclass(frozen=True)
class UpdateLaunchResult:
    command: list[str]
    pid: int
    used_terminal: bool


class UpdateError(RuntimeError):
    pass


class UpdateLaunchError(UpdateError):
    pass


def project_root(start: Path | None = None) -> Path:
    current = (start or Path(__file__)).resolve()
    candidates = [current if current.is_dir() else current.parent, *current.parents]
    for candidate in candidates:
        if (candidate / "pyproject.toml").exists() and (candidate / "scripts" / "install-user.sh").exists():
            return candidate
    return Path(__file__).resolve().parents[2]


def local_git_commit(root: Path | None = None, *, timeout: int = 5) -> str | None:
    repo_root = root or project_root()
    if not (repo_root / ".git").exists():
        return None
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    commit = result.stdout.strip()
    return commit or None


def _request_json(url: str, *, timeout: int) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": USER_AGENT,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        raise UpdateError(f"failed to check GitHub for updates: {exc}") from exc
    if not isinstance(payload, dict):
        raise UpdateError("failed to check GitHub for updates: unexpected response")
    return payload


def remote_git_commit(*, timeout: int = 8) -> tuple[str, str | None, str | None]:
    payload = _request_json(GITHUB_API_COMMIT_URL, timeout=timeout)
    commit = str(payload.get("sha") or "").strip()
    if not commit:
        raise UpdateError("failed to check GitHub for updates: missing commit sha")
    message = ((payload.get("commit") or {}).get("message") or "").splitlines()
    return commit, str(payload.get("html_url") or "") or None, message[0] if message else None


def parse_version(source: str) -> str | None:
    match = VERSION_RE.search(source)
    return match.group(1) if match else None


def version_from_content_payload(payload: dict[str, Any]) -> str | None:
    content = str(payload.get("content") or "")
    if not content:
        return None
    if payload.get("encoding") == "base64":
        try:
            source = base64.b64decode(content).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return None
    else:
        source = content
    return parse_version(source)


def remote_version(*, timeout: int = 8) -> str | None:
    try:
        return version_from_content_payload(_request_json(GITHUB_CONTENT_INIT_URL, timeout=timeout))
    except UpdateError:
        return None


def version_key(value: str | None) -> tuple[int, ...]:
    if not value:
        return ()
    return tuple(int(part) for part in re.findall(r"\d+", value))


def update_info_from_values(
    *,
    local_version: str,
    remote_version_value: str | None = None,
    local_commit: str | None = None,
    remote_commit: str | None = None,
    remote_url: str | None = None,
    remote_message: str | None = None,
) -> UpdateInfo:
    if local_commit and remote_commit:
        return UpdateInfo(
            update_available=local_commit != remote_commit,
            local_version=local_version,
            remote_version=remote_version_value,
            local_commit=local_commit,
            remote_commit=remote_commit,
            remote_url=remote_url,
            remote_message=remote_message,
            reason="commit" if local_commit != remote_commit else None,
        )
    newer_version = version_key(remote_version_value) > version_key(local_version)
    return UpdateInfo(
        update_available=newer_version,
        local_version=local_version,
        remote_version=remote_version_value,
        local_commit=local_commit,
        remote_commit=remote_commit,
        remote_url=remote_url,
        remote_message=remote_message,
        reason="version" if newer_version else None,
    )


def check_for_update(root: Path | None = None, *, timeout: int = 8) -> UpdateInfo:
    repo_root = root or project_root()
    local_commit = local_git_commit(repo_root)
    remote_commit, remote_url, remote_message = remote_git_commit(timeout=timeout)
    return update_info_from_values(
        local_version=__version__,
        remote_version_value=remote_version(timeout=timeout),
        local_commit=local_commit,
        remote_commit=remote_commit,
        remote_url=remote_url,
        remote_message=remote_message,
    )


def can_self_update(root: Path | None = None) -> bool:
    repo_root = root or project_root()
    return (repo_root / ".git").exists() and (repo_root / "scripts" / "install-user.sh").exists()


def build_update_command(root: Path | None = None) -> list[str]:
    repo_root = root or project_root()
    return ["bash", "-lc", UPDATE_SCRIPT, "ani-watch-update", str(repo_root)]


def build_update_terminal_command(command: list[str]) -> tuple[list[str], bool]:
    for terminal, args in TERMINAL_CANDIDATES:
        terminal_path = shutil.which(terminal)
        if terminal_path:
            terminal_args = args or terminal_args_for(terminal, terminal_path)
            return [terminal_path, *terminal_args, *command], True
    return command, False


def launch_update(root: Path | None = None, *, require_terminal: bool = True) -> UpdateLaunchResult:
    repo_root = root or project_root()
    if not can_self_update(repo_root):
        raise UpdateLaunchError("this install is not a Git checkout with scripts/install-user.sh")
    command, used_terminal = build_update_terminal_command(build_update_command(repo_root))
    if require_terminal and not used_terminal:
        raise UpdateLaunchError("no supported terminal emulator was found")
    try:
        process = subprocess.Popen(command, start_new_session=True)
    except OSError as exc:
        raise UpdateLaunchError(str(exc)) from exc
    return UpdateLaunchResult(command=command, pid=process.pid, used_terminal=used_terminal)
