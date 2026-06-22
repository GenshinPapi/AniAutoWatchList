from __future__ import annotations

import json
import time

import pytest

from ani_watchlist.party import (
    MpvIpcController,
    WatchPartyError,
    WatchPartyMedia,
    WatchPartyRoom,
    cloudflared_asset_name,
    parse_party_link,
    resolve_party_link,
)


class FakeMpvIpcController(MpvIpcController):
    def __init__(self, properties: dict[str, object]):
        self.properties = properties

    def available(self) -> bool:
        return True

    def get_property(self, name: str) -> object:
        return self.properties.get(name)


def media() -> WatchPartyMedia:
    return WatchPartyMedia(
        party_title="Friday Night",
        anime_title="Cowboy Bebop",
        source_title="Cowboy Bebop",
        episode="5",
        mode="sub",
        allanime_id="demo-id",
        allanime_title="Cowboy Bebop (26 episodes)",
        total_episodes=26,
    )


def test_parse_party_link_requires_room_and_invite() -> None:
    parsed = parse_party_link("https://example.test/party/room123?invite=abc")

    assert parsed == {
        "base_url": "https://example.test",
        "room_id": "room123",
        "invite": "abc",
    }

    with pytest.raises(WatchPartyError):
        parse_party_link("https://example.test/party/room123")


def test_parse_party_link_accepts_pasted_command_text() -> None:
    parsed = parse_party_link("ani-watch party join https://example.test/party/room123?invite=abc.")

    assert parsed["base_url"] == "https://example.test"
    assert parsed["room_id"] == "room123"
    assert parsed["invite"] == "abc"


def test_parse_party_link_explains_incomplete_base_url() -> None:
    with pytest.raises(WatchPartyError, match="full invite link"):
        parse_party_link("https://example.test")


def test_resolve_party_link_uses_health_share_url(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeResponse:
        def __init__(self, url: str, body: str):
            self._url = url
            self._body = body.encode("utf-8")

        def __enter__(self) -> "FakeResponse":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def geturl(self) -> str:
            return self._url

        def read(self, _size: int = -1) -> bytes:
            return self._body

    def fake_urlopen(request: object, timeout: float) -> FakeResponse:
        _ = timeout
        url = getattr(request, "full_url", str(request))
        if url == "https://room.trycloudflare.com":
            return FakeResponse(url, "<html>No invite link here.</html>")
        if url == "https://room.trycloudflare.com/api/health":
            return FakeResponse(
                url,
                json.dumps({"share_url": "https://room.trycloudflare.com/party/room123?invite=abc"}),
            )
        raise AssertionError(f"unexpected URL: {url}")

    monkeypatch.setattr("ani_watchlist.party.urllib.request.urlopen", fake_urlopen)

    parsed = resolve_party_link("https://room.trycloudflare.com", timeout=0.1)

    assert parsed["room_id"] == "room123"
    assert parsed["invite"] == "abc"


def test_party_room_join_and_control_events() -> None:
    room = WatchPartyRoom(media(), host_username="Host")
    participant, state = room.join("Guest")

    assert state["media"]["anime_title"] == "Cowboy Bebop"
    assert participant.username == "Guest"
    assert participant.color.startswith("#")
    assert state["host_color"].startswith("#")
    assert state["participants"][0]["color"] == participant.color

    room.control("pause", {})
    events = room.events_since(state["latest_sequence"])

    assert [event.event_type for event in events] == ["pause"]
    assert events[0].payload["host_color"] == state["host_color"]


def test_party_room_chat_events_and_recent_activity() -> None:
    room = WatchPartyRoom(media(), host_username="Host")
    participant, state = room.join("Guest")

    event = room.send_chat("  hello party  ", participant_id=participant.participant_id)
    host_event = room.send_chat("welcome", username="Host", host=True)
    events = room.events_since(state["latest_sequence"])
    recent_types = [item["event_type"] for item in room.public_state()["recent_events"]]

    assert event.payload["message"] == "hello party"
    assert event.payload["username"] == "Guest"
    assert event.payload["color"] == participant.color
    assert host_event.payload["host"] is True
    assert host_event.payload["color"] == room.host_color
    assert [item.event_type for item in events] == ["chat_message", "chat_message"]
    assert "chat_message" in recent_types


def test_party_room_chat_requires_participant_or_host() -> None:
    room = WatchPartyRoom(media(), host_username="Host")

    with pytest.raises(WatchPartyError, match="participant id"):
        room.send_chat("hello")

    with pytest.raises(WatchPartyError, match="empty"):
        room.send_chat("   ", username="Host", host=True)


def test_party_room_validates_invite_and_host_tokens() -> None:
    room = WatchPartyRoom(media(), host_username="Host")

    assert room.validate_invite(room.invite_token)
    assert not room.validate_invite("wrong")
    assert room.validate_host(room.host_token)
    assert not room.validate_host("wrong")

    with pytest.raises(WatchPartyError):
        room.control("unsupported", {})


def test_party_room_public_state_tracks_playback_state() -> None:
    room = WatchPartyRoom(media(), host_username="Host")

    room.update_playback_state(position_seconds=125.5, paused=True)
    state = room.public_state()["playback_state"]

    assert state["paused"] is True
    assert state["position_seconds"] == 125.5

    room.control("play", {"position_seconds": 126})
    playing_state = room.public_state()["playback_state"]

    assert playing_state["paused"] is False
    assert playing_state["position_seconds"] >= 126


def test_party_room_late_join_state_catches_up_to_playing_host() -> None:
    room = WatchPartyRoom(media(), host_username="Host")

    room.control("play", {"position_seconds": 180})
    time.sleep(0.02)
    _participant, state = room.join("Late Guest")

    playback_state = state["playback_state"]
    assert playback_state["paused"] is False
    assert playback_state["position_seconds"] > 180


def test_party_room_episode_change_can_wait_for_host_snapshot() -> None:
    room = WatchPartyRoom(media(), host_username="Host")

    room.control("next_episode", {"episode": "6", "position_seconds": 0, "paused": True, "sync_pending": True})
    pending_state = room.public_state()["playback_state"]

    assert pending_state["episode"] == "6"
    assert pending_state["paused"] is True
    assert pending_state["sync_pending"] is True

    room.update_playback_state(position_seconds=2.5, paused=False, episode="6")
    ready_state = room.public_state()["playback_state"]

    assert ready_state["sync_pending"] is False
    assert ready_state["paused"] is False
    assert ready_state["position_seconds"] >= 2.5


def test_mpv_snapshot_requires_timestamp_before_publishing_state() -> None:
    controller = FakeMpvIpcController({"pause": False})

    assert controller.snapshot() is None


def test_mpv_snapshot_includes_timestamp_and_pause_state() -> None:
    controller = FakeMpvIpcController({"time-pos": "12.5", "pause": True})

    assert controller.snapshot() == {"position_seconds": 12.5, "paused": True}


def test_cloudflared_asset_name_supports_common_linux_arches() -> None:
    assert cloudflared_asset_name("Linux", "x86_64") == "cloudflared-linux-amd64"
    assert cloudflared_asset_name("Linux", "aarch64") == "cloudflared-linux-arm64"
    assert cloudflared_asset_name("Linux", "i686") == "cloudflared-linux-386"
    assert cloudflared_asset_name("Linux", "armv7l") == "cloudflared-linux-armhf"
    assert cloudflared_asset_name("Darwin", "arm64") is None
