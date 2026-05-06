from __future__ import annotations

import os
import platform
import shutil
import sys
from pathlib import Path

from .config import load_config
from .db import LATEST_SCHEMA_VERSION, current_version, initialize
from .paths import get_paths
from .providers.anilist import AniListProvider


def _ok(value: bool) -> str:
    return "OK" if value else "WARN"


def run_doctor(check_network: bool = True) -> tuple[int, list[str]]:
    paths = get_paths()
    paths.ensure()
    config = load_config()
    lines: list[str] = []
    code = 0

    lines.append(f"OK Python version: {platform.python_version()} ({sys.executable})")

    try:
        with initialize() as conn:
            version = current_version(conn)
    except Exception as exc:
        return 1, lines + [f"WARN database open/migration failed: {exc}"]

    checks = [
        ("database", paths.db_path, paths.db_path.exists(), os.R_OK | os.W_OK),
        ("config", paths.config_path, paths.config_path.exists(), os.R_OK | os.W_OK),
        ("cover cache", paths.cover_dir, paths.cover_dir.exists(), os.R_OK | os.W_OK),
        ("logs", paths.log_dir, paths.log_dir.exists(), os.R_OK | os.W_OK),
    ]
    for label, path, exists, mode in checks:
        lines.append(f"{_ok(exists)} {label}: {path}")
        if not exists:
            code = 1
        writable = os.access(path, mode)
        lines.append(f"{_ok(writable)} {label} readable/writable: {path}")
        if not writable:
            code = 1
    current = version == LATEST_SCHEMA_VERSION
    lines.append(f"{_ok(current)} schema version: {version} (latest {LATEST_SCHEMA_VERSION})")
    if not current:
        code = 1

    first_ani_cli = shutil.which("ani-cli")
    expected = Path.home() / ".local" / "bin" / "ani-cli"
    shadows_system = first_ani_cli == str(expected)
    lines.append(f"{_ok(shadows_system)} ani-cli first on PATH: {first_ani_cli or 'not found'}")
    if not shadows_system:
        code = 1
    if expected.exists():
        try:
            text = expected.read_text(encoding="utf-8", errors="ignore")
            hook_terms = [
                "ani_watch_hook launch",
                "title-selected",
                "episodes-listed",
                "playback-started",
                "playback-finished",
            ]
            has_hook = all(term in text for term in hook_terms)
        except OSError:
            has_hook = False
        lines.append(f"{_ok(has_hook)} modified ani-cli has hook integration: {expected}")
        if not has_hook:
            code = 1
    else:
        lines.append(f"WARN modified ani-cli missing: {expected}")
        code = 1

    hook_path = shutil.which("ani-watch-hook")
    gui_path = shutil.which("ani-watch-gui")
    lines.append(f"{_ok(bool(hook_path))} ani-watch-hook on PATH: {hook_path or 'not found'}")
    lines.append(f"{_ok(bool(gui_path))} ani-watch-gui on PATH: {gui_path or 'not found'}")
    if not hook_path or not gui_path:
        code = 1
    lines.append("OK current-shell refresh tip: if this terminal ran ani-cli before install, run `hash -r` and retry `type -a ani-cli`")

    originals = [
        path
        for path in ("/usr/local/bin/ani-cli", "/usr/bin/ani-cli", "/bin/ani-cli")
        if Path(path).exists() and str(Path(path)) != str(expected)
    ]
    lines.append(f"{_ok(bool(originals))} original/system ani-cli candidates: {', '.join(originals) or 'none found'}")
    if not originals:
        code = 1

    try:
        import tkinter  # noqa: F401

        lines.append("OK tkinter GUI dependency available")
    except Exception as exc:
        lines.append(f"WARN tkinter GUI dependency unavailable: {exc}")
        code = 1

    try:
        import PIL  # noqa: F401

        lines.append("OK Pillow available for cover thumbnails")
    except Exception:
        lines.append("WARN Pillow not installed; GUI still opens but cover images are text-only")

    if check_network and config.anilist.enabled:
        try:
            provider = AniListProvider(config.anilist)
            results = provider.search_title("Cowboy Bebop")
            lines.append(f"{_ok(bool(results))} AniList metadata lookup returned {len(results)} result(s)")
            if not results:
                code = 1
        except Exception as exc:
            lines.append(f"WARN AniList metadata lookup failed: {exc}")
            code = 1
    else:
        lines.append("OK AniList metadata lookup skipped")

    return code, lines
