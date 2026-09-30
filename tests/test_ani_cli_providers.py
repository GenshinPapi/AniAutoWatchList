from __future__ import annotations

import base64
import html
import http.server
import json
import shutil
import subprocess
import threading
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


def playback_provider_order() -> list[str]:
    source = ani_cli_source()
    line = next(line for line in source.splitlines() if line.startswith("playback_providers="))
    return line.split("=", 1)[1].strip().strip('"').split()


def test_playback_tries_anidb_before_hianime() -> None:
    source = ani_cli_source()
    # the original two providers keep the front of the failover chain, in their original order
    assert playback_provider_order()[:2] == ["anidb", "hianime"]
    body = source.split("get_episode_url() {", 1)[1].split("\n}\n", 1)[0]
    assert '"${_playback_provider}_select_episode_url"' in body
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


def png_wrapped_segment(offset: int = 252) -> bytes:
    """A decoy PNG plus padding in front of a real transport stream, as hianime.at serves it."""
    decoy = b"\x89PNG\r\n\x1a\n" + bytes(62)
    padding = bytes(offset - len(decoy))
    packets = b"".join(b"\x47" + bytes([index % 251]) * 187 for index in range(30))
    return decoy + padding + packets


@pytest.mark.skipif(shutil.which("sh") is None, reason="POSIX shell not available")
def test_hls_payload_offset_finds_a_transport_stream_behind_a_decoy_header(
    ani_cli_functions: Path, tmp_path: Path
) -> None:
    segment = tmp_path / "wrapped.ts"
    segment.write_bytes(png_wrapped_segment())
    assert run_function(ani_cli_functions, f'hls_payload_offset "{segment}"').strip() == "252"


@pytest.mark.skipif(shutil.which("sh") is None, reason="POSIX shell not available")
def test_hls_payload_offset_reports_zero_for_a_plain_transport_stream(
    ani_cli_functions: Path, tmp_path: Path
) -> None:
    segment = tmp_path / "plain.ts"
    segment.write_bytes(png_wrapped_segment()[252:])
    assert run_function(ani_cli_functions, f'hls_payload_offset "{segment}"').strip() == "0"


@pytest.mark.skipif(shutil.which("sh") is None, reason="POSIX shell not available")
def test_hls_payload_offset_says_nothing_without_a_transport_stream(
    ani_cli_functions: Path, tmp_path: Path
) -> None:
    # fragmented mp4 and truncated downloads must both be left to the player
    fmp4 = tmp_path / "init.m4s"
    fmp4.write_bytes(b"\x00\x00\x00\x18ftypiso5" + bytes(2000))
    assert run_function(ani_cli_functions, f'hls_payload_offset "{fmp4}"').strip() == ""
    short = tmp_path / "short.ts"
    short.write_bytes(png_wrapped_segment()[252:652])
    assert run_function(ani_cli_functions, f'hls_payload_offset "{short}"').strip() == ""


MEDIA_PLAYLIST = "\n".join(
    [
        "#EXTM3U",
        "#EXT-X-VERSION:3",
        "#EXT-X-PLAYLIST-TYPE:VOD",
        "#EXTINF:10.01,",
        "seg_00000.ts",
        "#EXTINF:8.5,",
        "https://other.example/seg_00001.ts",
        "#EXT-X-ENDLIST",
    ]
)


@pytest.mark.skipif(shutil.which("sh") is None, reason="POSIX shell not available")
def test_hls_segment_urls_resolves_relative_entries(ani_cli_functions: Path) -> None:
    output = run_function(
        ani_cli_functions,
        f"printf '%s' '{MEDIA_PLAYLIST}' | hls_segment_urls 'https://cdn.example/v/abc/1080/index.m3u8'",
    )
    assert output.splitlines() == [
        "https://cdn.example/v/abc/1080/seg_00000.ts",
        "https://other.example/seg_00001.ts",
    ]


@pytest.mark.skipif(shutil.which("sh") is None, reason="POSIX shell not available")
def test_hls_byterange_playlist_skips_the_decoy_header_on_every_segment(ani_cli_functions: Path) -> None:
    output = run_function(
        ani_cli_functions,
        "printf '%s' '"
        + MEDIA_PLAYLIST
        + "' | hls_byterange_playlist 'https://cdn.example/v/abc/1080/index.m3u8' 252 \"$(printf '1000\\n2000')\"",
    )
    assert output.splitlines() == [
        "#EXTM3U",
        # byte ranges are a version 4 tag, so the version has to be raised
        "#EXT-X-VERSION:4",
        "#EXT-X-PLAYLIST-TYPE:VOD",
        "#EXTINF:10.01,",
        "#EXT-X-BYTERANGE:748@252",
        "https://cdn.example/v/abc/1080/seg_00000.ts",
        "#EXTINF:8.5,",
        "#EXT-X-BYTERANGE:1748@252",
        "https://other.example/seg_00001.ts",
        "#EXT-X-ENDLIST",
    ]


@pytest.mark.skipif(shutil.which("sh") is None, reason="POSIX shell not available")
def test_hls_local_playlist_is_only_offered_to_players_that_can_open_a_file(
    ani_cli_functions: Path,
) -> None:
    script = """
for player_function in mpv "mpv.exe" flatpak_mpv vlc iina download syncplay android_mpv android_vlc iSH catt debug; do
    if hls_local_playlist_supported; then printf '%s yes\\n' "$player_function"; else printf '%s no\\n' "$player_function"; fi
done
"""
    results = dict(line.split() for line in run_function(ani_cli_functions, script).splitlines())
    assert results == {
        "mpv": "yes",
        "mpv.exe": "yes",
        "flatpak_mpv": "yes",
        "vlc": "yes",
        "iina": "yes",
        "download": "yes",
        "syncplay": "yes",
        "android_mpv": "no",
        "android_vlc": "no",
        "iSH": "no",
        "catt": "no",
        "debug": "no",
    }


def test_playback_repairs_image_wrapped_segments_before_reaching_the_player() -> None:
    source = ani_cli_source()
    body = source.split("play_episode() {", 1)[1].split("\n}\n", 1)[0]
    assert body.index("get_episode_url") < body.index("hls_localize_playlist")
    assert body.index("hls_localize_playlist") < body.index('case "$player_function"')
    # every mpv-derived player needs the nested protocols allowed for a local playlist
    for branch in ("mpv*)", "flatpak_mpv)", "*iina*)", "*yncpla*)"):
        section = body.split(branch, 1)[1].split(";;", 1)[0]
        assert "hls_lavf_flag" in section, f"{branch} does not pass the local playlist flag"
    assert 'hls_fix_enabled="${ANI_CLI_HLS_FIX:-1}"' in source


class StreamHandler(http.server.BaseHTTPRequestHandler):
    """Serves canned playlist and segment bodies and records the headers each request carried."""

    def do_GET(self) -> None:  # noqa: N802 - http.server naming
        self.server.seen.append(
            {
                "path": self.path,
                "referer": self.headers.get("Referer"),
                "user_agent": self.headers.get("User-Agent"),
                "range": self.headers.get("Range"),
            }
        )
        status, body = self.server.routes.get(self.path, (404, b"missing"))
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        pass


@pytest.fixture
def stream_server():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), StreamHandler)
    server.routes = {}
    server.seen = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


def server_url(server, path: str) -> str:
    return f"http://127.0.0.1:{server.server_address[1]}{path}"


TS_BYTES = b"".join(b"\x47" + bytes([index % 251]) * 187 for index in range(30))


def serve_hls(server, segment_status: int = 200, segment_body: bytes = TS_BYTES) -> str:
    server.routes["/v/master.m3u8"] = (
        200,
        b"#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=2300000,RESOLUTION=1280x720\n720/index.m3u8\n",
    )
    server.routes["/v/720/index.m3u8"] = (
        200,
        b"#EXTM3U\n#EXT-X-VERSION:3\n#EXTINF:10.0,\nseg0.ts\n#EXTINF:10.0,\nseg1.ts\n#EXT-X-ENDLIST\n",
    )
    server.routes["/v/720/seg0.ts"] = (segment_status, segment_body)
    return server_url(server, "/v/master.m3u8")


def reachability(functions: Path, episode: str, referer: str = "") -> tuple[str, str]:
    output = run_function(
        functions,
        f"""
playback_check_enabled=1
episode='{episode}'
m3u8_refr='{referer}'
refr_flag=''
if playback_stream_reachable; then printf 'ok\\n'; else printf 'fail\\n'; fi
printf '%s' "$playback_check_error"
""",
    )
    status, _, error = output.partition("\n")
    return status, error


@pytest.mark.skipif(shutil.which("curl") is None, reason="curl not available")
def test_stream_check_follows_master_and_variant_to_the_first_segment(ani_cli_functions: Path, stream_server) -> None:
    master = serve_hls(stream_server)
    assert reachability(ani_cli_functions, master, "https://embed.example/") == ("ok", "")
    paths = [request["path"] for request in stream_server.seen]
    assert paths == ["/v/master.m3u8", "/v/720/index.m3u8", "/v/720/seg0.ts"]
    # the check asks exactly like the player will: its referer and mpv's own user agent
    assert {request["referer"] for request in stream_server.seen} == {"https://embed.example/"}
    assert {request["user_agent"] for request in stream_server.seen} == {"libmpv"}
    assert stream_server.seen[-1]["range"] == "bytes=0-4095"


@pytest.mark.skipif(shutil.which("curl") is None, reason="curl not available")
def test_stream_check_rejects_a_media_host_that_refuses_the_player(ani_cli_functions: Path, stream_server) -> None:
    master = serve_hls(stream_server, segment_status=403, segment_body=b"forbidden")
    assert reachability(ani_cli_functions, master) == ("fail", "the media host answered HTTP 403")


@pytest.mark.skipif(shutil.which("curl") is None, reason="curl not available")
def test_stream_check_rejects_a_web_page_served_as_video(ani_cli_functions: Path, stream_server) -> None:
    master = serve_hls(stream_server, segment_body=b"<!DOCTYPE html><html><title>Just a moment...</title></html>")
    assert reachability(ani_cli_functions, master) == ("fail", "the media host returned a web page instead of video")


@pytest.mark.skipif(shutil.which("curl") is None, reason="curl not available")
def test_stream_check_rejects_a_playlist_that_is_not_hls(ani_cli_functions: Path, stream_server) -> None:
    stream_server.routes["/v/master.m3u8"] = (200, b"<html>maintenance</html>")
    status, error = reachability(ani_cli_functions, server_url(stream_server, "/v/master.m3u8"))
    assert (status, error) == ("fail", "the playlist host did not return HLS")


@pytest.mark.skipif(shutil.which("curl") is None, reason="curl not available")
def test_stream_check_range_reads_a_plain_mp4(ani_cli_functions: Path, stream_server) -> None:
    stream_server.routes["/files/episode.mp4"] = (200, b"\x00\x00\x00\x18ftypmp42" + bytes(4000))
    assert reachability(ani_cli_functions, server_url(stream_server, "/files/episode.mp4")) == ("ok", "")
    assert stream_server.seen[0]["range"] == "bytes=0-4095"


def test_stream_check_reports_a_dead_host(ani_cli_functions: Path) -> None:
    status, error = reachability(ani_cli_functions, "http://127.0.0.1:9/nothing/master.m3u8")
    assert (status, error) == ("fail", "the playlist host did not answer")


def test_stream_check_leaves_non_http_targets_alone(ani_cli_functions: Path) -> None:
    # local rewritten playlists and player-resolved pages cannot be judged here
    assert reachability(ani_cli_functions, "/tmp/ani-cli-hls.x/stream.m3u8") == ("ok", "")


@pytest.mark.skipif(shutil.which("sh") is None, reason="POSIX shell not available")
def test_hls_resolve_url_handles_every_relative_form(ani_cli_functions: Path) -> None:
    base = "https://cdn.example/v/abc/master.m3u8"
    script = f"""
for entry in 'https://other.example/x.m3u8' '//edge.example/y.ts' '/root/z.ts' '720/index.m3u8'; do
    hls_resolve_url '{base}' "$entry"; printf '\\n'
done
"""
    assert run_function(ani_cli_functions, script).splitlines() == [
        "https://other.example/x.m3u8",
        "https://edge.example/y.ts",
        "https://cdn.example/root/z.ts",
        "https://cdn.example/v/abc/720/index.m3u8",
    ]


@pytest.mark.skipif(shutil.which("sh") is None, reason="POSIX shell not available")
def test_hls_first_entry_prefers_the_init_segment_of_fragmented_mp4(ani_cli_functions: Path) -> None:
    fmp4 = '#EXTM3U\n#EXT-X-MAP:URI="init.mp4"\n#EXTINF:6.0,\nseg-1.m4s\n'
    assert run_function(ani_cli_functions, f"printf '%s' '{fmp4}' | hls_first_entry") == "init.mp4\n"
    assert run_function(ani_cli_functions, f"printf '%s' '{MEDIA_PLAYLIST}' | hls_first_entry") == "seg_00000.ts\n"


FAILOVER_PRELUDE = """
ep_list='1
2'
ep_no=2
quality=best
playback_providers='anidb hianime'
anidb_enabled=1
hianime_enabled=1
playback_check_enabled=1
"""


def run_failover(functions: Path, stubs: str, after: str = "") -> subprocess.CompletedProcess[str]:
    script = f". {functions}\n{FAILOVER_PRELUDE}\n{stubs}\nget_episode_url\n{after}"
    return subprocess.run(["sh", "-c", script], capture_output=True, text=True, timeout=60)


@pytest.mark.skipif(shutil.which("sh") is None, reason="POSIX shell not available")
def test_failover_moves_to_the_next_provider_and_explains_why(ani_cli_functions: Path) -> None:
    result = run_failover(
        ani_cli_functions,
        """
anidb_select_episode_url() { anidb_last_error="anidb.app is down for maintenance"; return 1; }
hianime_select_episode_url() { episode="https://cdn.example/720.m3u8"; m3u8_refr="https://embed.example/"; subtitle="https://cdn.example/en.vtt"; return 0; }
playback_stream_reachable() { playback_check_error=""; return 0; }
""",
        "printf '%s|%s|%s' \"$episode\" \"$stream_referer\" \"$stream_subtitle\"",
    )
    assert result.returncode == 0, result.stderr
    assert "anidb.app playback failed: anidb.app is down for maintenance. Trying hianime.at..." in result.stderr
    assert result.stdout == "https://cdn.example/720.m3u8|https://embed.example/|https://cdn.example/en.vtt"


@pytest.mark.skipif(shutil.which("sh") is None, reason="POSIX shell not available")
def test_failover_skips_a_provider_whose_stream_host_is_dead(ani_cli_functions: Path) -> None:
    result = run_failover(
        ani_cli_functions,
        """
anidb_select_episode_url() { episode="https://dead.example/a.m3u8"; return 0; }
hianime_select_episode_url() { episode="https://alive.example/b.m3u8"; return 0; }
playback_stream_reachable() {
    case "$episode" in *dead*) playback_check_error="the media host answered HTTP 403"; return 1 ;; esac
    playback_check_error=""
}
""",
        "printf '%s' \"$episode\"",
    )
    assert result.returncode == 0, result.stderr
    assert "anidb.app playback failed: the media host answered HTTP 403. Trying hianime.at..." in result.stderr
    assert result.stdout == "https://alive.example/b.m3u8"


@pytest.mark.skipif(shutil.which("sh") is None, reason="POSIX shell not available")
def test_failover_never_ends_worse_than_the_first_resolved_stream(ani_cli_functions: Path) -> None:
    result = run_failover(
        ani_cli_functions,
        """
anidb_select_episode_url() { episode="https://first.example/a.m3u8"; refr_flag="--referrer=https://first.example/"; stream_player_flags="--cache-secs=120"; return 0; }
hianime_select_episode_url() { episode="https://second.example/b.m3u8"; return 0; }
playback_stream_reachable() { playback_check_error="the media host did not answer"; return 1; }
""",
        "printf '%s|%s|%s' \"$episode\" \"$refr_flag\" \"$stream_player_flags\"",
    )
    assert result.returncode == 0, result.stderr
    assert "playing the anidb.app stream anyway" in result.stderr
    assert result.stdout == "https://first.example/a.m3u8|--referrer=https://first.example/|--cache-secs=120"


@pytest.mark.skipif(shutil.which("sh") is None, reason="POSIX shell not available")
def test_failover_names_every_provider_when_all_fail(ani_cli_functions: Path) -> None:
    result = run_failover(
        ani_cli_functions,
        """
anidb_select_episode_url() { anidb_last_error="anidb.app is down for maintenance"; return 1; }
hianime_select_episode_url() { hianime_last_error="hianime.at did not return a matching anime"; return 1; }
""",
    )
    assert result.returncode == 1
    assert (
        "Playback failed. anidb.app: anidb.app is down for maintenance. "
        "hianime.at: hianime.at did not return a matching anime." in result.stderr
    )


@pytest.mark.skipif(shutil.which("sh") is None, reason="POSIX shell not available")
def test_disabled_providers_are_skipped(ani_cli_functions: Path) -> None:
    result = run_failover(
        ani_cli_functions,
        """
anidb_enabled=0
anidb_select_episode_url() { printf 'anidb was called' >&2; return 1; }
hianime_select_episode_url() { episode="https://alive.example/b.m3u8"; return 0; }
playback_stream_reachable() { playback_check_error=""; return 0; }
""",
        "printf '%s' \"$episode\"",
    )
    assert result.returncode == 0, result.stderr
    assert "anidb was called" not in result.stderr
    assert result.stdout == "https://alive.example/b.m3u8"


@pytest.mark.skipif(shutil.which("sh") is None, reason="POSIX shell not available")
def test_change_quality_keeps_the_providers_referer_and_subtitles(ani_cli_functions: Path) -> None:
    result = run_failover(
        ani_cli_functions,
        """
anidb_select_episode_url() { anidb_last_error="down"; return 1; }
hianime_select_episode_url() {
    links='1080p >https://cdn.example/1080.m3u8
720p >https://cdn.example/720.m3u8'
    select_quality best
    refr_flag="--referrer=https://embed.example/"
    m3u8_refr="https://embed.example/"
    subtitle="https://cdn.example/en.vtt"
    subs_flag="--sub-file=https://cdn.example/en.vtt"
    return 0
}
playback_stream_reachable() { playback_check_error=""; return 0; }
""",
        # what the change_quality menu entry does after playback
        "select_quality 720p\nprintf '%s|%s|%s' \"$episode\" \"$refr_flag\" \"$subs_flag\"",
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "https://cdn.example/720.m3u8|--referrer=https://embed.example/|--sub-file=https://cdn.example/en.vtt"


MEGAPLAY_KEY = "i?LMTAx0Q6,:}50U"
MEGAPLAY_IV = "W0;27ToaUpl_P%'c"


def megaplay_encrypt(payload: dict[str, object], key: str = MEGAPLAY_KEY, iv: str = MEGAPLAY_IV) -> str:
    """base64url(aes-256-cbc(json)) without padding, the way getSources ships its playlist."""
    from cryptography.hazmat.primitives import padding
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    padder = padding.PKCS7(128).padder()
    raw = padder.update(json.dumps(payload, separators=(",", ":")).encode()) + padder.finalize()
    encryptor = Cipher(algorithms.AES(key.encode().ljust(32, b"\0")), modes.CBC(iv.encode())).encryptor()
    blob = encryptor.update(raw) + encryptor.finalize()
    return base64.urlsafe_b64encode(blob).decode().rstrip("=")


def test_playback_failover_order_keeps_existing_providers_first() -> None:
    assert playback_provider_order() == ["anidb", "hianime", "megaplay", "zokoanime", "anizone", "kickassanime"]
    source = ani_cli_source()
    for variable in ("ANI_CLI_MEGAPLAY", "ANI_CLI_ZOKOANIME", "ANI_CLI_ANIZONE", "ANI_CLI_KICKASSANIME"):
        assert f'"${{{variable}:-1}}"' in source


@pytest.mark.skipif(shutil.which("openssl") is None, reason="openssl not available")
@pytest.mark.parametrize("path", ["short", "a-longer-path-that-changes-the-padding", "x" * 7])
def test_megaplay_decrypt_file_reads_the_playlist_url(ani_cli_functions: Path, path: str) -> None:
    file_url = f"https://fetch.example/anime/{path}/master.m3u8"
    blob = megaplay_encrypt({"file": file_url})
    script = f"megaplay_decrypt_file '{blob}' 'i?LMTAx0Q6,:}}50U' \"W0;27ToaUpl_P%'c\""
    assert run_function(ani_cli_functions, script).strip() == file_url


@pytest.mark.skipif(shutil.which("openssl") is None, reason="openssl not available")
def test_megaplay_decrypt_file_prints_nothing_for_a_rotated_key(ani_cli_functions: Path) -> None:
    blob = megaplay_encrypt({"file": "https://fetch.example/anime/a/master.m3u8"}, key="rotated-key-0001")
    script = f"megaplay_decrypt_file '{blob}' 'i?LMTAx0Q6,:}}50U' \"W0;27ToaUpl_P%'c\""
    assert run_function(ani_cli_functions, script).strip() == ""


def test_megaplay_source_fields_prefer_english_captions(ani_cli_functions: Path) -> None:
    sources = json.dumps(
        {
            "tracks": [
                {"file": "https://sub.example/anime/a/b/subtitles/ara-6.vtt", "label": "Arabic", "kind": "captions", "default": True},
                {"file": "https://sub.example/thumbs.vtt", "kind": "thumbnails"},
                {"file": "https://sub.example/anime/a/b/subtitles/eng-2.vtt", "label": "English", "kind": "captions"},
            ],
            "intro": {"start": 153, "end": 242},
            "enc": "abc_-",
        },
        separators=(",", ":"),
    )
    output = run_function(ani_cli_functions, f"megaplay_source_fields '{sources}'")
    assert output.splitlines() == ["abc_-", "", "https://sub.example/anime/a/b/subtitles/eng-2.vtt"]


def test_megaplay_source_fields_accept_an_unencrypted_answer(ani_cli_functions: Path) -> None:
    sources = json.dumps({"sources": [{"file": "https://fetch.example/anime/a/master.m3u8"}], "tracks": []})
    output = run_function(ani_cli_functions, f"megaplay_source_fields '{sources}'")
    assert output.splitlines() == ["", "https://fetch.example/anime/a/master.m3u8", ""]


def test_megaplay_refresh_key_reads_the_players_current_pair(ani_cli_functions: Path) -> None:
    script = """
megaplay_base='https://megaplay.example'
megaplay_curl() {
    printf '%s' "$*" >&2
    printf '%s' 'var a=[["trustAesKey","TRUST_AES_KEY"],"n3w?Key}0000000"],b=[["trustAesIv","TRUST_AES_IV"],"n3w;Iv%x0000000"];'
}
megaplay_refresh_key '<script src="/lib/newclient.min.js?v=4.21"></script>'
printf '%s|%s' "$megaplay_key" "$megaplay_iv"
"""
    result = subprocess.run(["sh", "-c", f". {ani_cli_functions}\n{script}"], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    assert result.stdout == "n3w?Key}0000000|n3w;Iv%x0000000"
    assert "https://megaplay.example/lib/newclient.min.js?v=4.21" in result.stderr


def megaplay_flow(ani_cli_functions: Path, sources_by_query: dict[str, str], page: str) -> subprocess.CompletedProcess[str]:
    """Run megaplay_select_episode_url against canned pages instead of the network."""
    cases = "\n".join(
        f"        *'getSources?{query}') printf '%s' '{body}' ;;" for query, body in sources_by_query.items()
    )
    script = f"""
megaplay_base='https://megaplay.example'
megaplay_agent='test-agent'
megaplay_key='{MEGAPLAY_KEY}'
megaplay_iv="{MEGAPLAY_IV}"
mode=sub
ep_no=5
quality=best
allanime_show_ids() {{ show_mal_id=52991; show_anilist_id=154587; }}
megaplay_curl() {{
    megaplay_last_error=""
    printf '%s\\n' "$1" >>"$MEGAPLAY_REQUESTS"
    case "$1" in
{cases}
        */stream/mal/52991/5/sub) printf '%s' '{page}' ;;
        */master.m3u8) printf '%s\\n' '#EXTM3U' '#EXT-X-STREAM-INF:BANDWIDTH=1874847,RESOLUTION=1920x1080' 'index-f1-v1-a1.m3u8' ;;
        *) return 1 ;;
    esac
}}
megaplay_select_episode_url
printf '%s\\n%s\\n%s\\n%s\\n' "$episode" "$refr_flag" "$subs_flag" "$stream_player_flags"
"""
    return subprocess.run(["sh", "-c", f". {ani_cli_functions}\n{script}"], capture_output=True, text=True, timeout=60)


PLAYER_PAGE = '<div id="megaplay-player" data-id="13457" data-realid="107877"></div>'


@pytest.mark.skipif(shutil.which("openssl") is None, reason="openssl not available")
def test_megaplay_resolves_a_mal_id_to_a_referred_variant_with_subtitles(ani_cli_functions: Path, tmp_path: Path) -> None:
    sources = json.dumps(
        {
            "tracks": [{"file": "https://slow.example/anime/aa/bb/subtitles/eng-2.vtt", "label": "English", "kind": "captions"}],
            "enc": megaplay_encrypt({"file": "https://fetch.example/anime/aa/cc/master.m3u8"}),
        }
    )
    requests = tmp_path / "requests.txt"
    result = megaplay_flow(ani_cli_functions, {"id=13457": sources}, PLAYER_PAGE)
    result = subprocess.run(
        ["sh", "-c", f"MEGAPLAY_REQUESTS={requests}; export MEGAPLAY_REQUESTS; " + result.args[2]],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        "https://fetch.example/anime/aa/cc/index-f1-v1-a1.m3u8",
        "--referrer=https://megaplay.example/",
        # the subtitle is fetched from the playlist's host rather than a slow rotating one
        "--sub-file=https://fetch.example/anime/aa/bb/subtitles/eng-2.vtt",
        "--demuxer-lavf-o-append=extension_picky=0 --cache-secs=120",
    ]
    assert requests.read_text().splitlines()[0] == "https://megaplay.example/stream/mal/52991/5/sub"


@pytest.mark.skipif(shutil.which("openssl") is None, reason="openssl not available")
def test_megaplay_asks_for_the_plain_cdn_when_offered_image_wrapped_segments(ani_cli_functions: Path, tmp_path: Path) -> None:
    wrapped = json.dumps({"tracks": [], "enc": megaplay_encrypt({"file": "https://megap.akirax.buzz/anime/aa/master.m3u8"})})
    plain = json.dumps({"tracks": [], "enc": megaplay_encrypt({"file": "https://fetch.example/anime/aa/master.m3u8"})})
    requests = tmp_path / "requests.txt"
    base = megaplay_flow(ani_cli_functions, {"id=13457": wrapped, "id=13457&s=bcdn": plain}, PLAYER_PAGE)
    result = subprocess.run(
        ["sh", "-c", f"MEGAPLAY_REQUESTS={requests}; export MEGAPLAY_REQUESTS; " + base.args[2]],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines()[0] == "https://fetch.example/anime/aa/index-f1-v1-a1.m3u8"
    assert "https://megaplay.example/stream/getSources?id=13457&s=bcdn" in requests.read_text().splitlines()


def test_megaplay_reports_a_missing_episode(ani_cli_functions: Path, tmp_path: Path) -> None:
    error_page = '<title>Error - MegaPlay</title><div class="error-code">Error Code: <span>404</span></div>'
    requests = tmp_path / "requests.txt"
    script = f"""
megaplay_base='https://megaplay.example'
mode=dub
ep_no=11
allanime_show_ids() {{ show_mal_id=59978; show_anilist_id=182255; }}
megaplay_curl() {{ megaplay_last_error=""; printf '%s\\n' "$1" >>'{requests}'; printf '%s' '{error_page}'; }}
megaplay_select_episode_url || printf '%s' "$megaplay_last_error"
"""
    result = subprocess.run(["sh", "-c", f". {ani_cli_functions}\n{script}"], capture_output=True, text=True, timeout=60)
    assert result.stdout == "megaplay.buzz has no dub source for episode 11"
    # MAL is tried first, then AniList
    assert requests.read_text().splitlines() == [
        "https://megaplay.example/stream/mal/59978/11/dub",
        "https://megaplay.example/stream/ani/182255/11/dub",
    ]


def test_zokoanime_resolves_the_hianime_player_by_mal_id(ani_cli_functions: Path, tmp_path: Path) -> None:
    config = obfuscate(
        {
            "src": "https://hls.example/v/abc/master.m3u8",
            "subtitles": [{"lang": "en", "label": "English", "default": True, "src": "https://hls.example/v/abc/subs/en.vtt"}],
        }
    )
    requests = tmp_path / "requests.txt"
    script = f"""
zokoanime_base='https://zoko.example'
scraper_agent='test-agent'
mode=dub
ep_no=3
quality=best
allanime_show_ids() {{ show_mal_id=57334; show_anilist_id=171018; }}
zokoanime_curl() {{
    zokoanime_last_error=""
    printf '%s\\n' "$*" >>'{requests}'
    case "$1" in
        */stream/mal/57334/3/dub) printf '<script>window.__P="%s";</script>' '{config}' ;;
        */master.m3u8) printf '%s\\n' '#EXTM3U' '#EXT-X-STREAM-INF:BANDWIDTH=5300000,RESOLUTION=1920x1080' '1080/index.m3u8' ;;
    esac
}}
zokoanime_select_episode_url
printf '%s\\n%s\\n%s\\n' "$episode" "$refr_flag" "$subs_flag"
"""
    result = subprocess.run(["sh", "-c", f". {ani_cli_functions}\n{script}"], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        "https://hls.example/v/abc/1080/index.m3u8",
        "--referrer=https://zoko.example/",
        "--sub-file=https://hls.example/v/abc/subs/en.vtt",
    ]
    # the playlist host wants the player site as referer
    assert "Referer: https://zoko.example/" in requests.read_text().splitlines()[1]


@pytest.mark.parametrize(
    ("card", "message"),
    [
        ("<span>// Error</span>", "zokoanime.video has no sub source for episode 9"),
        ("<span>// Encoding</span>", "zokoanime.video is still transcoding episode 9, try again in a few minutes"),
    ],
)
def test_zokoanime_explains_its_status_cards(ani_cli_functions: Path, card: str, message: str) -> None:
    script = f"""
zokoanime_base='https://zoko.example'
mode=sub
ep_no=9
allanime_show_ids() {{ show_mal_id=1; show_anilist_id=1; }}
zokoanime_curl() {{ zokoanime_last_error=""; printf '%s' '<div class="card">{card}</div>'; }}
zokoanime_select_episode_url || printf '%s' "$zokoanime_last_error"
"""
    assert run_function(ani_cli_functions, script) == message


def test_id_keyed_providers_need_the_shows_ids(ani_cli_functions: Path) -> None:
    script = """
mode=sub
ep_no=1
allanime_show_ids() { show_mal_id=""; show_anilist_id=""; return 1; }
megaplay_select_episode_url || printf '%s\\n' "$megaplay_last_error"
zokoanime_select_episode_url || printf '%s\\n' "$zokoanime_last_error"
"""
    assert run_function(ani_cli_functions, script).splitlines() == [
        "AllAnime did not return a MAL or AniList id for this show",
        "AllAnime did not return a MAL id for this show",
    ]


@pytest.mark.skipif(shutil.which("sh") is None, reason="POSIX shell not available")
def test_failover_tries_the_providers_other_qualities_before_moving_on(ani_cli_functions: Path) -> None:
    result = run_failover(
        ani_cli_functions,
        """
anidb_select_episode_url() {
    links='1080p >https://dead-host.example/1080.m3u8
720p >https://live-host.example/720.m3u8'
    episode="https://dead-host.example/1080.m3u8"
    return 0
}
hianime_select_episode_url() { printf 'hianime was called' >&2; return 1; }
playback_stream_reachable() {
    case "$episode" in *dead-host*) playback_check_error="the media host did not answer"; return 1 ;; esac
    playback_check_error=""
}
""",
        "printf '%s' \"$episode\"",
    )
    assert result.returncode == 0, result.stderr
    assert "The 1080p stream did not answer (the media host did not answer), playing 720p instead." in result.stderr
    assert "hianime was called" not in result.stderr
    assert result.stdout == "https://live-host.example/720.m3u8"


def js_string(value: object) -> str:
    """Encode JSON the way AniZone's pages embed it in a JS string literal."""
    text = json.dumps(value)
    return text.replace("\\", "\\\\").replace('"', "\\u0022").replace("'", "\\'")


ANIZIP_FRIEREN = {
    "titles": {"x-jat": "Sousou no Frieren", "en": "Frieren: Beyond Journey`s End", "ja": "葬送のフリーレン"},
    "episodes": {"1": {"airdate": "2023-09-29"}},
}

ANIZONE_SEARCH = "<script>Alpine.data('x', () => ({ items: JSON.parse('" + js_string(
    [
        {"slug": "s2slug", "main_title": "Sousou no Frieren (2026)", "title_list": {"1": "Frieren: Beyond Journey`s End Season 2"}, "start_year": 2026, "type": "TV Series"},
        {"slug": "s1slug", "main_title": "Sousou no Frieren", "title_list": {"5": "Sousou no Frieren", "1": "Frieren: Beyond Journey`s End", "8": "葬送のフリーレン"}, "start_year": 2023, "type": "TV Series"},
    ]
) + "') }))</script>"

ANIZONE_EPISODE = "<script>vidstackPlayer(JSON.parse('" + js_string(
    {
        "src": "https://cdn.example/rel/master.m3u8",
        "subtitles": [
            {"title": "English (US) (CC)", "language": "en", "default": False, "forced": "no", "file": "https://cdn.example/rel/subtitles/4_en.ass"},
            {"title": "English (US)", "language": "en", "default": True, "forced": "yes", "file": "https://cdn.example/rel/subtitles/3_en.ass"},
            {"title": "German", "language": "de", "default": False, "forced": "no", "file": "https://cdn.example/rel/subtitles/0_de.ass"},
        ],
    }
) + "'))</script>"

ANIZONE_MASTER = "\n".join(
    [
        "#EXTM3U",
        '#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="group_audio",NAME="Hindi",DEFAULT=NO,LANGUAGE="hi",URI="audio/0_hi/playlist.m3u8"',
        '#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="group_audio",NAME="English (US)",DEFAULT=NO,LANGUAGE="en",URI="audio/2_en/playlist.m3u8"',
        '#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="group_audio",NAME="Japanese",DEFAULT=YES,LANGUAGE="ja",URI="audio/3_ja/playlist.m3u8"',
        '#EXT-X-STREAM-INF:BANDWIDTH=946000,RESOLUTION=640x360,AUDIO="group_audio"',
        "video/360/playlist.m3u8",
        '#EXT-X-STREAM-INF:BANDWIDTH=3476000,RESOLUTION=1920x1080,AUDIO="group_audio"',
        "video/1080/playlist.m3u8",
    ]
)


def anizone_parse(functions: Path, command: str, stdin: str, *args: str) -> str:
    quoted = " ".join("'" + arg.replace("'", "'\\''") + "'" for arg in args)
    result = subprocess.run(
        ["sh", "-c", f". {functions}\nanizone_parse {command} {quoted}"],
        input=stdin,
        capture_output=True,
        text=True,
        timeout=60,
    )
    return result.stdout


def test_anizone_titles_lists_the_year_then_anidb_titles(ani_cli_functions: Path) -> None:
    output = anizone_parse(ani_cli_functions, "titles", json.dumps(ANIZIP_FRIEREN))
    assert output.splitlines() == ["2023", "Sousou no Frieren", "Frieren: Beyond Journey`s End", "葬送のフリーレン"]


def test_anizone_match_picks_the_season_carrying_an_exact_anidb_title(ani_cli_functions: Path) -> None:
    wanted = "2023\nSousou no Frieren\nFrieren: Beyond Journey`s End"
    # the 2026 season shares the romaji stem but not the exact title
    assert anizone_parse(ani_cli_functions, "match", ANIZONE_SEARCH, wanted).strip() == "s1slug"


def test_anizone_match_falls_back_to_a_lone_show_from_the_same_year(ani_cli_functions: Path) -> None:
    wanted = "2026\nFrieren: Something Else Entirely"
    assert anizone_parse(ani_cli_functions, "match", ANIZONE_SEARCH, wanted).strip() == "s2slug"


def test_anizone_match_refuses_to_guess(ani_cli_functions: Path) -> None:
    # an unrelated title, and a title that normalises to nothing, must never match
    assert anizone_parse(ani_cli_functions, "match", ANIZONE_SEARCH, "2019\nCowboy Bebop\n!!!").strip() == ""


def test_anizone_player_prefers_the_full_english_track(ani_cli_functions: Path) -> None:
    output = anizone_parse(ani_cli_functions, "player", ANIZONE_EPISODE)
    assert output.splitlines() == ["https://cdn.example/rel/master.m3u8", "https://cdn.example/rel/subtitles/3_en.ass"]


def audio_rendition(functions: Path, master: str, language: str) -> str:
    result = subprocess.run(
        ["sh", "-c", f". {functions}\nhls_audio_rendition {language}"], input=master, capture_output=True, text=True, timeout=60
    )
    return result.stdout.strip()


def test_hls_audio_rendition_picks_the_rendition_for_the_mode(ani_cli_functions: Path) -> None:
    assert audio_rendition(ani_cli_functions, ANIZONE_MASTER, "ja") == "audio/3_ja/playlist.m3u8"
    assert audio_rendition(ani_cli_functions, ANIZONE_MASTER, "en") == "audio/2_en/playlist.m3u8"
    japanese_only = "\n".join(line for line in ANIZONE_MASTER.splitlines() if "2_en" not in line)
    assert audio_rendition(ani_cli_functions, japanese_only, "en") == ""


def test_hls_audio_rendition_reads_three_letter_codes_and_defaults(ani_cli_functions: Path) -> None:
    master = "\n".join(
        [
            "#EXTM3U",
            '#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="stereo",NAME="English",LANGUAGE="eng",URI="eb7d/playlist.m3u8"',
            '#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="stereo",NAME="Japanese",DEFAULT=YES,LANGUAGE="jpn",URI="eb7f/playlist.m3u8"',
        ]
    )
    assert audio_rendition(ani_cli_functions, master, "en") == "eb7d/playlist.m3u8"
    assert audio_rendition(ani_cli_functions, master, "ja") == "eb7f/playlist.m3u8"
    unlabelled = '#EXTM3U\n#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="a",NAME="main",DEFAULT=YES,URI="main/playlist.m3u8"'
    assert audio_rendition(ani_cli_functions, unlabelled, "ja") == "main/playlist.m3u8"
    assert audio_rendition(ani_cli_functions, unlabelled, "en") == ""


def anizone_flow(functions: Path, mode: str, requests: Path) -> subprocess.CompletedProcess[str]:
    fixtures = functions.parent / f"anizone-{mode}"
    fixtures.mkdir(exist_ok=True)
    (fixtures / "map.json").write_text(json.dumps(ANIZIP_FRIEREN), encoding="utf-8")
    (fixtures / "search.html").write_text(ANIZONE_SEARCH, encoding="utf-8")
    (fixtures / "episode.html").write_text(ANIZONE_EPISODE, encoding="utf-8")
    (fixtures / "master.m3u8").write_text(ANIZONE_MASTER, encoding="utf-8")
    script = f"""
mode={mode}
ep_no=5
quality=best
scraper_agent='test-agent'
allanime_show_ids() {{ show_mal_id=52991; show_anilist_id=154587; }}
anizone_curl() {{
    anizone_last_error=""
    printf '%s\\n' "$1" >>'{requests}'
    case "$1" in
        *api.ani.zip/mappings?anilist_id=154587) cat '{fixtures}/map.json' ;;
        *anizone.to/anime?search=*) cat '{fixtures}/search.html' ;;
        *anizone.to/anime/s1slug/5) cat '{fixtures}/episode.html' ;;
        */rel/master.m3u8) cat '{fixtures}/master.m3u8' ;;
        *) return 1 ;;
    esac
}}
anizone_select_episode_url
printf '%s\\n%s\\n%s\\n%s\\n' "$episode" "$stream_audio_url" "$stream_master_url" "$subs_flag"
"""
    return subprocess.run(["sh", "-c", f". {functions}\n{script}"], capture_output=True, text=True, timeout=60)


def test_anizone_sub_plays_japanese_audio_with_english_subtitles(ani_cli_functions: Path, tmp_path: Path) -> None:
    requests = tmp_path / "requests.txt"
    result = anizone_flow(ani_cli_functions, "sub", requests)
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        "https://cdn.example/rel/video/1080/playlist.m3u8",
        "https://cdn.example/rel/audio/3_ja/playlist.m3u8",
        "https://cdn.example/rel/master.m3u8",
        "--sub-file=https://cdn.example/rel/subtitles/3_en.ass",
    ]
    assert requests.read_text().splitlines()[1] == "https://anizone.to/anime?search=Sousou%20no%20Frieren"


def test_anizone_dub_plays_the_english_rendition_without_subtitles(ani_cli_functions: Path, tmp_path: Path) -> None:
    result = anizone_flow(ani_cli_functions, "dub", tmp_path / "requests.txt")
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines()[:3] == [
        "https://cdn.example/rel/video/1080/playlist.m3u8",
        "https://cdn.example/rel/audio/2_en/playlist.m3u8",
        "https://cdn.example/rel/master.m3u8",
    ]
    assert result.stdout.splitlines()[3:] in ([], [""])


def test_hls_attach_audio_hands_the_player_one_video_and_one_audio(ani_cli_functions: Path, tmp_path: Path) -> None:
    script = f"""
TMPDIR='{tmp_path}'
player_function=mpv
episode='https://cdn.example/rel/video/1080/playlist.m3u8'
stream_audio_url='https://cdn.example/rel/audio/2_en/playlist.m3u8'
stream_master_url='https://cdn.example/rel/master.m3u8'
hls_attach_audio
printf '%s\\n%s\\n' "$episode" "$hls_lavf_flag"
cat "$episode"
"""
    lines = run_function(ani_cli_functions, script).splitlines()
    assert lines[0].startswith(str(tmp_path)) and lines[0].endswith("/stream.m3u8")
    assert lines[1] == "--demuxer-lavf-o-append=protocol_whitelist=file,crypto,data,http,https,tcp,tls"
    playlist = "\n".join(lines[2:])
    assert 'URI="https://cdn.example/rel/audio/2_en/playlist.m3u8"' in playlist
    assert playlist.rstrip().endswith("https://cdn.example/rel/video/1080/playlist.m3u8")
    assert playlist.count("#EXT-X-MEDIA:") == 1 and playlist.count("#EXT-X-STREAM-INF:") == 1


def test_hls_attach_audio_gives_url_only_players_the_full_master(ani_cli_functions: Path) -> None:
    script = """
player_function=android_mpv
episode='https://cdn.example/rel/video/1080/playlist.m3u8'
stream_audio_url='https://cdn.example/rel/audio/2_en/playlist.m3u8'
stream_master_url='https://cdn.example/rel/master.m3u8'
hls_attach_audio
printf '%s' "$episode"
"""
    assert run_function(ani_cli_functions, script) == "https://cdn.example/rel/master.m3u8"


def test_hls_attach_audio_leaves_ordinary_streams_alone(ani_cli_functions: Path) -> None:
    script = """
player_function=mpv
episode='https://cdn.example/v/1080/index.m3u8'
stream_audio_url=''
hls_attach_audio
printf '%s|%s' "$episode" "$hls_lavf_flag"
"""
    assert run_function(ani_cli_functions, script) == "https://cdn.example/v/1080/index.m3u8|"


def test_playback_attaches_audio_after_the_playlist_repair() -> None:
    body = ani_cli_source().split("play_episode() {", 1)[1].split("\n}\n", 1)[0]
    assert body.index("hls_localize_playlist") < body.index("hls_attach_audio") < body.index('case "$player_function"')


def show_ids_script(cache: Path, curl_body: str, calls: Path) -> str:
    return f"""
XDG_CACHE_HOME='{cache}'
allanime_api='https://api.example'
allanime_api_refr='https://ref.example'
agent='test-agent'
curl() {{ printf 'call\\n' >>'{calls}'; printf '%s' '{curl_body}'; }}
sleep() {{ printf 'slept %s\\n' "$1" >>'{calls}'; }}
"""


@pytest.mark.skipif(shutil.which("sh") is None, reason="POSIX shell not available")
def test_show_ids_are_looked_up_once_and_remembered_across_runs(ani_cli_functions: Path, tmp_path: Path) -> None:
    calls = tmp_path / "calls.txt"
    body = '{"data":{"show":{"_id":"abc","malId":"52991","aniListId":"154587"}}}'
    prelude = show_ids_script(tmp_path / "cache", body, calls)
    first = run_function(ani_cli_functions, prelude + "id=abc\nallanime_show_ids && printf '%s|%s' \"$show_mal_id\" \"$show_anilist_id\"")
    assert first == "52991|154587"
    # a later ani-cli run reads the ids back without asking AllAnime again
    second = run_function(ani_cli_functions, prelude + "id=abc\nallanime_show_ids && printf '%s|%s' \"$show_mal_id\" \"$show_anilist_id\"")
    assert second == "52991|154587"
    assert calls.read_text().splitlines() == ["call"]
    assert (tmp_path / "cache" / "ani-watchlist" / "allanime-show-ids.tsv").read_text() == "abc\t52991\t154587\n"


@pytest.mark.skipif(shutil.which("sh") is None, reason="POSIX shell not available")
def test_show_ids_wait_out_a_short_throttle_but_not_a_long_one(ani_cli_functions: Path, tmp_path: Path) -> None:
    short = tmp_path / "short.txt"
    body = '{"errors":[{"message":"too many requests, try again in 3 seconds"}]}'
    script = show_ids_script(tmp_path / "cache", body, short) + "id=abc\nallanime_show_ids || printf 'no ids'"
    assert run_function(ani_cli_functions, script) == "no ids"
    assert short.read_text().splitlines() == ["call", "slept 3", "call"]

    long = tmp_path / "long.txt"
    body = '{"errors":[{"message":"too many requests, try again in 17 seconds"}]}'
    script = show_ids_script(tmp_path / "cache", body, long) + "id=abc\nallanime_show_ids || printf 'no ids'"
    assert run_function(ani_cli_functions, script) == "no ids"
    assert long.read_text().splitlines() == ["call"]
    assert not (tmp_path / "cache" / "ani-watchlist" / "allanime-show-ids.tsv").exists()


ALLANIME_FRIEREN = json.dumps(
    {"data": {"show": {"_id": "abc", "name": "Sousou no Frieren", "englishName": "Frieren: Beyond Journey’s End", "airedStart": {"year": 2023, "month": 9}}}}
)

KAA_SEARCH = json.dumps(
    {
        "result": [
            {"slug": "sousou-no-frieren-no-mahou-561b", "title": "Sousou no Frieren: ●● no Mahou", "title_en": "Frieren Mini Anime", "year": 2023},
            {"slug": "sousou-no-frieren-2d15", "title": "Sousou no Frieren", "title_en": "Frieren: Beyond Journey's End", "year": 2023},
            {"slug": "frieren-s2-7dcd", "title": "Sousou no Frieren 2nd Season", "title_en": "Frieren: Beyond Journey's End Season 2", "year": 2026},
        ]
    }
)


def kaa_parse(functions: Path, command: str, stdin: str, *args: str) -> str:
    quoted = " ".join("'" + arg.replace("'", "'\\''") + "'" for arg in args)
    result = subprocess.run(
        ["sh", "-c", f". {functions}\nkickassanime_parse {command} {quoted}"], input=stdin, capture_output=True, text=True, timeout=60
    )
    return result.stdout


def test_kickassanime_searches_the_romaji_then_the_english_title(ani_cli_functions: Path) -> None:
    assert kaa_parse(ani_cli_functions, "queries", ALLANIME_FRIEREN).splitlines() == [
        "Sousou no Frieren",
        "Frieren: Beyond Journey’s End",
    ]


def test_kickassanime_match_needs_an_exact_title(ani_cli_functions: Path) -> None:
    # the mini anime and the second season share words and the year, but not the title
    assert kaa_parse(ani_cli_functions, "match", KAA_SEARCH, ALLANIME_FRIEREN).strip() == "sousou-no-frieren-2d15"


def test_kickassanime_match_rejects_a_remake_from_another_year(ani_cli_functions: Path) -> None:
    remake = json.dumps({"result": [{"slug": "fruits-basket-2019", "title": "Fruits Basket", "title_en": "Fruits Basket", "year": 2019}]})
    original = json.dumps({"data": {"show": {"name": "Fruits Basket", "englishName": "Fruits Basket", "airedStart": {"year": 2001}}}})
    assert kaa_parse(ani_cli_functions, "match", remake, original).strip() == ""


def test_kickassanime_episode_finds_the_slug_or_the_page_holding_it(ani_cli_functions: Path) -> None:
    listing = json.dumps(
        {
            "current_page": 1,
            "pages": [{"number": 1, "eps": [1, 2, 3]}, {"number": 12, "eps": [1092, 1092.5, 1100]}],
            "result": [{"slug": "f897b3", "episode_string": "1"}, {"slug": "a1b2c3", "episode_string": "2"}],
        }
    )
    assert kaa_parse(ani_cli_functions, "episode", listing, "2").strip() == "slug a1b2c3"
    assert kaa_parse(ani_cli_functions, "episode", listing, "1100").strip() == "page 12"
    assert kaa_parse(ani_cli_functions, "episode", listing, "1092.5").strip() == "page 12"
    assert kaa_parse(ani_cli_functions, "episode", listing, "40").strip() == ""


def test_kickassanime_server_picks_the_hls_player(ani_cli_functions: Path) -> None:
    servers = json.dumps(
        {
            "servers": [
                {"name": "BirdStream", "src": "https://krussdomi.com/cat-player/player?id=a&type=dash"},
                {"name": "VidStreaming", "src": "https://krussdomi.com/cat-player/player?id=b&source=vidstream&ln=ja-JP"},
            ]
        }
    )
    assert kaa_parse(ani_cli_functions, "server", servers).strip() == "https://krussdomi.com/cat-player/player?id=b&source=vidstream&ln=ja-JP"


KAA_PLAYER = (
    '<astro-island uid="x" component-url="/_astro/VidstackPlayer.abc.js" props="'
    + html.escape(
        json.dumps(
            {
                "manifest": [0, "https://hls.example/manifest/m1/master.m3u8"],
                "subtitles": [1, [[0, {"language": [0, "en"], "name": [0, "English"], "src": [0, "https://subs.example/m1/en.vtt"]}]]],
                "title": [0],
                "source": [0, "vidstream"],
            }
        ),
        quote=True,
    )
    + '"></astro-island>'
)


def test_kickassanime_player_decodes_astro_props(ani_cli_functions: Path) -> None:
    assert kaa_parse(ani_cli_functions, "player", KAA_PLAYER).splitlines() == [
        "https://hls.example/manifest/m1/master.m3u8",
        "https://subs.example/m1/en.vtt",
    ]


KAA_MASTER = "\n".join(
    [
        "#EXTM3U",
        '#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="stereo",NAME="English",LANGUAGE="eng",URI="aud-eng/playlist.m3u8"',
        '#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="stereo",NAME="Japanese",DEFAULT=YES,LANGUAGE="jpn",URI="aud-jpn/playlist.m3u8"',
        '#EXT-X-STREAM-INF:BANDWIDTH=13298235,RESOLUTION=1920x1080,AUDIO="stereo"',
        "vid-1080/playlist.m3u8",
    ]
)


def kaa_flow(functions: Path, mode: str, tmp_path: Path, dubbed_release: bool) -> subprocess.CompletedProcess[str]:
    fixtures = tmp_path / f"kaa-{mode}"
    fixtures.mkdir()
    (fixtures / "search.json").write_text(KAA_SEARCH, encoding="utf-8")
    (fixtures / "player.html").write_text(KAA_PLAYER, encoding="utf-8")
    (fixtures / "master.m3u8").write_text(KAA_MASTER, encoding="utf-8")
    listing = {"current_page": 1, "pages": [{"number": 1, "eps": [1, 5]}], "result": [{"slug": "e5", "episode_string": "5"}]}
    (fixtures / "listing.json").write_text(json.dumps(listing), encoding="utf-8")
    (fixtures / "empty.json").write_text(json.dumps({"current_page": 1, "pages": [], "result": []}), encoding="utf-8")
    servers = {"servers": [{"name": "VidStreaming", "src": "https://krussdomi.example/player?id=1&source=vidstream"}]}
    (fixtures / "servers.json").write_text(json.dumps(servers), encoding="utf-8")
    en_listing = "listing.json" if dubbed_release else "empty.json"
    script = f"""
mode={mode}
ep_no=5
quality=best
id=abc
scraper_agent='test-agent'
kickassanime_base='https://kaa.example'
kickassanime_origin='https://krussdomi.example'
show_titles_for=abc
show_titles_json='{ALLANIME_FRIEREN}'
curl() {{ printf 'POST %s\\n' "$*" >>'{fixtures}/requests'; cat '{fixtures}/search.json'; }}
kickassanime_curl() {{
    printf 'GET %s\\n' "$1" >>'{fixtures}/requests'
    case "$1" in
        *lang=ja-JP*) cat '{fixtures}/listing.json' ;;
        *lang=en-US*) cat '{fixtures}/{en_listing}' ;;
        */episode/ep-5-e5) cat '{fixtures}/servers.json' ;;
        *player?id=1*) cat '{fixtures}/player.html' ;;
        */master.m3u8) cat '{fixtures}/master.m3u8' ;;
        *) return 1 ;;
    esac
}}
kickassanime_select_episode_url
printf '%s\\n%s\\n%s\\n%s\\n%s\\n' "$episode" "$stream_audio_url" "$refr_flag" "$stream_player_flags" "$subs_flag"
"""
    return subprocess.run(["sh", "-c", f". {functions}\n{script}"], capture_output=True, text=True, timeout=60)


def test_kickassanime_sub_plays_japanese_audio_from_the_players_origin(ani_cli_functions: Path, tmp_path: Path) -> None:
    result = kaa_flow(ani_cli_functions, "sub", tmp_path, dubbed_release=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        "https://hls.example/manifest/m1/vid-1080/playlist.m3u8",
        "https://hls.example/manifest/m1/aud-jpn/playlist.m3u8",
        "--referrer=https://krussdomi.example/",
        "--http-header-fields=Origin:https://krussdomi.example --demuxer-lavf-o-append=extension_picky=0",
        "--sub-file=https://subs.example/m1/en.vtt",
    ]
    requests = (tmp_path / "kaa-sub" / "requests").read_text()
    assert '"query":"Sousou no Frieren"' in requests
    assert "lang=en-US" not in requests


def test_kickassanime_dub_falls_back_to_the_english_track_of_the_japanese_release(ani_cli_functions: Path, tmp_path: Path) -> None:
    result = kaa_flow(ani_cli_functions, "dub", tmp_path, dubbed_release=False)
    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    assert lines[1] == "https://hls.example/manifest/m1/aud-eng/playlist.m3u8"
    assert lines[4:] in ([], [""])
    requests = (tmp_path / "kaa-dub" / "requests").read_text()
    assert requests.index("lang=en-US") < requests.index("lang=ja-JP")


def test_anizone_match_breaks_a_shared_title_by_year(ani_cli_functions: Path) -> None:
    shared = "<script>x({ items: JSON.parse('" + js_string(
        [
            {"slug": "ova", "main_title": "Example Title", "title_list": {}, "start_year": 2019, "type": "OVA"},
            {"slug": "tv", "main_title": "Example Title", "title_list": {}, "start_year": 2021, "type": "TV Series"},
        ]
    ) + "') })</script>"
    assert anizone_parse(ani_cli_functions, "match", shared, "2021\nExample Title").strip() == "tv"
    assert anizone_parse(ani_cli_functions, "match", shared, "2019\nExample Title").strip() == "ova"
