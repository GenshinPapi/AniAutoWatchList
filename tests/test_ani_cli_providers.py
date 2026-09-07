from __future__ import annotations

import base64
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from ani_watchlist.updater import bundled_ani_cli_path


HOOK_TERMS = (
    "ani_watch_hook launch",
    "title-selected",
    "episodes-listed",
    "playback-started",
    "playback-finished",
)
OBFUSCATION_KEY = b"otaku-embed-v1"


def ani_cli_source() -> str:
    return bundled_ani_cli_path().read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def ani_cli_functions(tmp_path_factory) -> Path:
    """The bundled script up to its `# MAIN` section, sourceable without running anything."""
    source = ani_cli_source()
    marker = "\n# MAIN\n"
    assert marker in source, "bundled ani-cli lost its # MAIN marker"
    path = tmp_path_factory.mktemp("ani-cli") / "functions.sh"
    path.write_text(source.split(marker)[0], encoding="utf-8")
    return path


def run_function(functions: Path, script: str) -> str:
    result = subprocess.run(
        ["sh", "-c", f". {functions}\n{script}"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def obfuscate(payload: dict[str, object]) -> str:
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    key = OBFUSCATION_KEY
    mixed = bytes(byte ^ key[index % len(key)] for index, byte in enumerate(raw))
    return base64.b64encode(mixed).decode("ascii")


def test_bundled_ani_cli_keeps_hook_integration() -> None:
    source = ani_cli_source()
    for term in HOOK_TERMS:
        assert term in source, f"bundled ani-cli lost the {term} hook doctor checks for"


def test_playback_tries_anidb_before_hianime() -> None:
    source = ani_cli_source()
    body = source.split("get_episode_url() {", 1)[1].split("\n}\n", 1)[0]
    assert body.index("anidb_select_episode_url") < body.index("hianime_select_episode_url")
    assert 'anidb_enabled="${ANI_CLI_ANIDB:-1}"' in source
    assert 'hianime_enabled="${ANI_CLI_HIANIME:-1}"' in source
    assert 'hianime_base="${ANI_CLI_HIANIME_BASE:-https://hianime.at}"' in source


@pytest.mark.skipif(shutil.which("sh") is None, reason="POSIX shell not available")
def test_hianime_deobfuscate_recovers_the_player_config(ani_cli_functions: Path) -> None:
    payload = {
        "src": "https://cdn.example/v/abc/master.m3u8",
        "subtitles": [{"lang": "en", "label": "English", "default": True, "src": "https://cdn.example/en.vtt"}],
        "player": {"logo_text": "ZokoAnime", "accent": "#35d5bf"},
    }
    output = run_function(ani_cli_functions, f'hianime_deobfuscate "{obfuscate(payload)}"')
    assert json.loads(output) == payload


@pytest.mark.skipif(shutil.which("sh") is None, reason="POSIX shell not available")
def test_hianime_media_fields_prefers_the_default_english_track(ani_cli_functions: Path) -> None:
    config = json.dumps(
        {
            "src": "https://cdn.example/v/abc/master.m3u8",
            "subtitles": [
                {"lang": "en", "label": "English", "default": True, "src": "https://cdn.example/en.vtt"},
                {"lang": "en", "label": "Spanish", "default": False, "src": "https://cdn.example/es.vtt"},
            ],
        },
        separators=(",", ":"),
    )
    output = run_function(ani_cli_functions, f"hianime_media_fields '{config}'")
    assert output.splitlines() == ["https://cdn.example/v/abc/master.m3u8", "https://cdn.example/en.vtt"]


@pytest.mark.skipif(shutil.which("sh") is None, reason="POSIX shell not available")
def test_hls_playlist_links_labels_qualities_and_resolves_relative_urls(ani_cli_functions: Path) -> None:
    playlist = "\n".join(
        [
            "#EXTM3U",
            "#EXT-X-STREAM-INF:BANDWIDTH=2300000,RESOLUTION=1280x720",
            "720/index.m3u8",
            "#EXT-X-STREAM-INF:BANDWIDTH=5300000,RESOLUTION=1920x1080",
            "https://other.example/1080/index.m3u8",
        ]
    )
    output = run_function(
        ani_cli_functions,
        f"hls_playlist_links 'https://cdn.example/v/abc/master.m3u8' '{playlist}'",
    )
    assert output.splitlines() == [
        "1080p >https://other.example/1080/index.m3u8",
        "720p >https://cdn.example/v/abc/720/index.m3u8",
    ]


@pytest.mark.skipif(shutil.which("sh") is None, reason="POSIX shell not available")
def test_hianime_episode_id_matches_by_number_then_by_position(ani_cli_functions: Path) -> None:
    listing = "4402\t1\n4403\t2\n4404\t3"
    assert run_function(ani_cli_functions, f"hianime_episode_id '{listing}' 2") == "4403"
    # numbering that does not line up falls back to the nth entry
    offset = "9001\t101\n9002\t102\n9003\t103"
    assert run_function(ani_cli_functions, f"hianime_episode_id '{offset}' 3") == "9003"
