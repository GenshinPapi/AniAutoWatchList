from __future__ import annotations

import pytest

from ani_watchlist.party import (
    WatchPartyError,
    WatchPartyMedia,
    WatchPartyRoom,
    cloudflared_asset_name,
    parse_party_link,
)


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


def test_party_room_join_and_control_events() -> None:
    room = WatchPartyRoom(media(), host_username="Host")
    participant, state = room.join("Guest")

    assert state["media"]["anime_title"] == "Cowboy Bebop"
    assert participant.username == "Guest"

    room.control("pause", {})
    events = room.events_since(state["latest_sequence"])

    assert [event.event_type for event in events] == ["pause"]


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


def test_cloudflared_asset_name_supports_common_linux_arches() -> None:
    assert cloudflared_asset_name("Linux", "x86_64") == "cloudflared-linux-amd64"
    assert cloudflared_asset_name("Linux", "aarch64") == "cloudflared-linux-arm64"
    assert cloudflared_asset_name("Linux", "i686") == "cloudflared-linux-386"
    assert cloudflared_asset_name("Linux", "armv7l") == "cloudflared-linux-armhf"
    assert cloudflared_asset_name("Darwin", "arm64") is None
