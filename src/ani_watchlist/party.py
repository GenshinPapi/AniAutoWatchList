from __future__ import annotations

import json
import os
import platform
import queue
import re
import secrets
import shutil
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .paths import get_paths
from .store import now_iso


DEFAULT_PARTY_HOST = "127.0.0.1"
CLOUDFLARED_URL_RE = re.compile(r"https://[-a-z0-9]+\.trycloudflare\.com", re.IGNORECASE)
PARTY_LINK_URL_RE = re.compile(r"https?://[^\s<>'\"]+", re.IGNORECASE)
CLOUDFLARED_DOWNLOAD_BASE_URL = "https://github.com/cloudflare/cloudflared/releases/latest/download"
CLOUDFLARED_MIN_DOWNLOAD_BYTES = 100_000
PARTY_LINK_RESOLVER_USER_AGENT = "ani-watchlist-party-link-resolver/0.1"
MAX_EVENT_HISTORY = 400
MAX_RECENT_ACTIVITY = 80
MAX_CHAT_MESSAGE_LENGTH = 500
PARTY_USER_COLORS = (
    "#ff7a45",
    "#60a5fa",
    "#34d399",
    "#f472b6",
    "#fbbf24",
    "#a78bfa",
    "#22d3ee",
    "#fb7185",
    "#84cc16",
    "#c084fc",
)
VISIBLE_PARTY_EVENT_TYPES = {
    "party_started",
    "participant_joined",
    "participant_left",
    "participant_updated",
    "participant_kicked",
    "chat_message",
    "play",
    "pause",
    "seek",
    "relative_seek",
    "next_episode",
    "previous_episode",
    "party_ended",
}


@dataclass(frozen=True)
class WatchPartyMedia:
    party_title: str
    anime_title: str
    source_title: str | None
    episode: str
    mode: str = "sub"
    allanime_id: str | None = None
    allanime_title: str | None = None
    total_episodes: int | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "party_title": self.party_title,
            "anime_title": self.anime_title,
            "source_title": self.source_title,
            "episode": self.episode,
            "mode": self.mode,
            "allanime_id": self.allanime_id,
            "allanime_title": self.allanime_title,
            "total_episodes": self.total_episodes,
        }

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> "WatchPartyMedia":
        return cls(
            party_title=str(payload.get("party_title") or "Watch Party"),
            anime_title=str(payload.get("anime_title") or "Unknown title"),
            source_title=str(payload["source_title"]) if payload.get("source_title") else None,
            episode=str(payload.get("episode") or "1"),
            mode=str(payload.get("mode") or "sub"),
            allanime_id=str(payload["allanime_id"]) if payload.get("allanime_id") else None,
            allanime_title=str(payload["allanime_title"]) if payload.get("allanime_title") else None,
            total_episodes=_int_or_none(payload.get("total_episodes")),
        )


@dataclass(frozen=True)
class WatchPartyParticipant:
    participant_id: str
    username: str
    joined_at: str
    last_seen_at: str
    color: str = ""
    kicked: bool = False

    def to_json(self) -> dict[str, Any]:
        return {
            "participant_id": self.participant_id,
            "username": self.username,
            "joined_at": self.joined_at,
            "last_seen_at": self.last_seen_at,
            "color": self.color,
            "kicked": self.kicked,
        }


@dataclass(frozen=True)
class WatchPartyEvent:
    sequence: int
    event_type: str
    payload: dict[str, Any]
    created_at: str

    def to_json(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "event_type": self.event_type,
            "payload": self.payload,
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class WatchPartyTunnel:
    public_url: str
    process: subprocess.Popen[str]
    lines: tuple[str, ...] = ()

    def close(self) -> None:
        if self.process.poll() is not None:
            return
        self.process.terminate()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()


class WatchPartyError(RuntimeError):
    pass


class WatchPartyRoom:
    def __init__(
        self,
        media: WatchPartyMedia,
        *,
        host_username: str,
        room_id: str | None = None,
        invite_token: str | None = None,
        host_token: str | None = None,
    ):
        self.media = media
        self.room_id = room_id or secrets.token_urlsafe(8)
        self.invite_token = invite_token or secrets.token_urlsafe(16)
        self.host_token = host_token or secrets.token_urlsafe(24)
        self.created_at = now_iso()
        self.host_username = _clean_username(host_username)
        self.host_color = secrets.choice(PARTY_USER_COLORS)
        self.ended = False
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self._next_sequence = 0
        self._events: list[WatchPartyEvent] = []
        self._participants: dict[str, WatchPartyParticipant] = {}
        self._playback_state: dict[str, Any] = {
            "paused": False,
            "position_seconds": 0.0,
            "episode": media.episode,
            "sync_pending": False,
            "updated_at": self.created_at,
        }
        with self._condition:
            self._append_event_locked(
                "party_started",
                {
                    "host_username": self.host_username,
                    "host_color": self.host_color,
                    "media": self.media.to_json(),
                    "playback_state": dict(self._playback_state),
                },
            )

    def share_link(self, base_url: str) -> str:
        base = base_url.rstrip("/")
        query = urllib.parse.urlencode({"invite": self.invite_token})
        return f"{base}/party/{urllib.parse.quote(self.room_id)}?{query}"

    def public_state(self) -> dict[str, Any]:
        with self._lock:
            return {
                "room_id": self.room_id,
                "party_title": self.media.party_title,
                "host_username": self.host_username,
                "host_color": self.host_color,
                "created_at": self.created_at,
                "ended": self.ended,
                "media": self.media.to_json(),
                "playback_state": self._current_playback_state_locked(),
                "participants": [participant.to_json() for participant in self._participants.values() if not participant.kicked],
                "recent_events": [
                    event.to_json()
                    for event in self._events[-MAX_RECENT_ACTIVITY:]
                    if event.event_type in VISIBLE_PARTY_EVENT_TYPES
                ],
                "latest_sequence": self._next_sequence,
            }

    def validate_invite(self, invite_token: str | None) -> bool:
        return bool(invite_token and secrets.compare_digest(str(invite_token), self.invite_token))

    def validate_host(self, host_token: str | None) -> bool:
        return bool(host_token and secrets.compare_digest(str(host_token), self.host_token))

    def join(self, username: str) -> tuple[WatchPartyParticipant, dict[str, Any]]:
        with self._condition:
            if self.ended:
                raise WatchPartyError("watch party has ended")
            ts = now_iso()
            participant = WatchPartyParticipant(
                participant_id=secrets.token_urlsafe(10),
                username=_clean_username(username),
                joined_at=ts,
                last_seen_at=ts,
                color=self._next_participant_color_locked(),
            )
            self._participants[participant.participant_id] = participant
            self._append_event_locked("participant_joined", {"participant": participant.to_json()})
            state = self.public_state()
            return participant, state

    def participant(self, participant_id: str) -> WatchPartyParticipant | None:
        with self._lock:
            return self._participants.get(participant_id)

    def touch_participant(self, participant_id: str) -> WatchPartyParticipant:
        with self._condition:
            participant = self._participants.get(participant_id)
            if participant is None:
                raise WatchPartyError("participant not found")
            if participant.kicked:
                raise WatchPartyError("participant was removed from the watch party")
            updated = WatchPartyParticipant(
                participant_id=participant.participant_id,
                username=participant.username,
                joined_at=participant.joined_at,
                last_seen_at=now_iso(),
                color=participant.color,
                kicked=participant.kicked,
            )
            self._participants[participant_id] = updated
            return updated

    def set_username(self, participant_id: str, username: str) -> WatchPartyParticipant:
        with self._condition:
            participant = self.touch_participant(participant_id)
            updated = WatchPartyParticipant(
                participant_id=participant.participant_id,
                username=_clean_username(username),
                joined_at=participant.joined_at,
                last_seen_at=now_iso(),
                color=participant.color,
                kicked=participant.kicked,
            )
            self._participants[participant_id] = updated
            self._append_event_locked("participant_updated", {"participant": updated.to_json()})
            return updated

    def leave(self, participant_id: str) -> None:
        with self._condition:
            participant = self._participants.pop(participant_id, None)
            if participant is not None:
                self._append_event_locked("participant_left", {"participant": participant.to_json()})

    def kick(self, participant_id: str) -> WatchPartyParticipant:
        with self._condition:
            participant = self._participants.get(participant_id)
            if participant is None:
                raise WatchPartyError("participant not found")
            kicked = WatchPartyParticipant(
                participant_id=participant.participant_id,
                username=participant.username,
                joined_at=participant.joined_at,
                last_seen_at=now_iso(),
                color=participant.color,
                kicked=True,
            )
            self._participants[participant_id] = kicked
            self._append_event_locked("participant_kicked", {"participant": kicked.to_json()})
            return kicked

    def send_chat(
        self,
        message: str,
        *,
        participant_id: str | None = None,
        username: str | None = None,
        host: bool = False,
    ) -> WatchPartyEvent:
        message = _clean_chat_message(message)
        if not message:
            raise WatchPartyError("chat message cannot be empty")
        with self._condition:
            participant_payload = None
            if not host and not participant_id:
                raise WatchPartyError("participant id required")
            display_name = _clean_username(username or self.host_username)
            if participant_id:
                participant = self.touch_participant(participant_id)
                participant_payload = participant.to_json()
                display_name = participant.username
            color = participant_payload.get("color") if isinstance(participant_payload, dict) else self.host_color
            return self._append_event_locked(
                "chat_message",
                {
                    "message_id": secrets.token_urlsafe(8),
                    "message": message,
                    "username": display_name,
                    "color": color,
                    "host": bool(host),
                    "participant_id": participant_id or "",
                    "participant": participant_payload,
                },
            )

    def control(self, action: str, payload: dict[str, Any] | None = None) -> WatchPartyEvent:
        payload = dict(payload or {})
        action = str(action or "").strip().casefold()
        if action not in {"play", "pause", "seek", "relative_seek", "next_episode", "previous_episode", "stop"}:
            raise WatchPartyError(f"unsupported watch party control: {action}")
        with self._condition:
            payload.setdefault("host_username", self.host_username)
            payload.setdefault("host_color", self.host_color)
            if isinstance(payload.get("media"), dict):
                self.media = WatchPartyMedia.from_json(payload["media"])
            if payload.get("position_seconds") is not None:
                self._playback_state["position_seconds"] = max(0.0, float(payload.get("position_seconds") or 0))
            if payload.get("paused") is not None:
                self._playback_state["paused"] = bool(payload.get("paused"))
            if payload.get("sync_pending") is not None:
                self._playback_state["sync_pending"] = bool(payload.get("sync_pending"))
            if action == "play":
                self._playback_state["paused"] = False
                self._playback_state["sync_pending"] = False
            elif action == "pause":
                self._playback_state["paused"] = True
                self._playback_state["sync_pending"] = False
            elif action == "seek":
                self._playback_state["position_seconds"] = float(payload.get("position_seconds") or 0)
                self._playback_state["sync_pending"] = False
            elif action == "relative_seek":
                self._playback_state["position_seconds"] = max(
                    0.0,
                    float(self._playback_state.get("position_seconds") or 0) + float(payload.get("delta_seconds") or 0),
                )
                self._playback_state["sync_pending"] = False
            elif action in {"next_episode", "previous_episode"} and payload.get("episode"):
                self._playback_state["episode"] = str(payload["episode"])
            self._playback_state["updated_at"] = now_iso()
            payload.setdefault("playback_state", self._current_playback_state_locked())
            return self._append_event_locked(action, payload)

    def update_playback_state(
        self,
        *,
        position_seconds: float | None = None,
        paused: bool | None = None,
        episode: str | None = None,
        emit_event: bool = False,
    ) -> WatchPartyEvent | None:
        with self._condition:
            if position_seconds is not None:
                self._playback_state["position_seconds"] = max(0.0, float(position_seconds))
            if paused is not None:
                self._playback_state["paused"] = bool(paused)
            if episode:
                self._playback_state["episode"] = str(episode)
            self._playback_state["sync_pending"] = False
            self._playback_state["updated_at"] = now_iso()
            payload = {"playback_state": self._current_playback_state_locked()}
            if emit_event:
                return self._append_event_locked("playback_state", payload)
            self._condition.notify_all()
            return None

    def end(self) -> WatchPartyEvent:
        with self._condition:
            self.ended = True
            return self._append_event_locked("party_ended", {"ended": True})

    def events_since(self, sequence: int, *, timeout: float = 20.0) -> list[WatchPartyEvent]:
        deadline = time.monotonic() + max(0.0, timeout)
        with self._condition:
            while not self.ended and self._next_sequence <= sequence and time.monotonic() < deadline:
                self._condition.wait(timeout=max(0.0, deadline - time.monotonic()))
            return [event for event in self._events if event.sequence > sequence]

    def _current_playback_state_locked(self) -> dict[str, Any]:
        state = dict(self._playback_state)
        position = float(state.get("position_seconds") or 0.0)
        if not bool(state.get("paused")):
            updated_at = _parse_iso(str(state.get("updated_at") or ""))
            if updated_at is not None:
                elapsed = max(0.0, (datetime.now(timezone.utc) - updated_at).total_seconds())
                position += elapsed
        state["position_seconds"] = max(0.0, position)
        state["updated_at"] = now_iso()
        return state

    def _append_event_locked(self, event_type: str, payload: dict[str, Any]) -> WatchPartyEvent:
        self._next_sequence += 1
        event = WatchPartyEvent(self._next_sequence, event_type, payload, now_iso())
        self._events.append(event)
        del self._events[:-MAX_EVENT_HISTORY]
        self._condition.notify_all()
        return event

    def _next_participant_color_locked(self) -> str:
        used = {self.host_color, *(participant.color for participant in self._participants.values() if participant.color)}
        available = [color for color in PARTY_USER_COLORS if color not in used]
        return secrets.choice(available or list(PARTY_USER_COLORS))


class WatchPartyHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class WatchPartyRequestHandler(BaseHTTPRequestHandler):
    server_version = "AniWatchParty/0.1"

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(HTTPStatus.NO_CONTENT)
        self._write_cors_headers()
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        try:
            self._handle_get()
        except WatchPartyError as exc:
            self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except Exception as exc:  # pragma: no cover - defensive HTTP boundary
            self._send_json({"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_POST(self) -> None:  # noqa: N802
        try:
            self._handle_post()
        except WatchPartyError as exc:
            self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except Exception as exc:  # pragma: no cover - defensive HTTP boundary
            self._send_json({"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def log_message(self, _format: str, *_args: object) -> None:
        return

    @property
    def room(self) -> WatchPartyRoom:
        return self.server.room  # type: ignore[attr-defined]

    def _handle_get(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        parts = _path_parts(parsed.path)
        query = urllib.parse.parse_qs(parsed.query)
        if not parts:
            self._send_redirect(self.room.share_link(_request_base_url(self)))
            return
        if len(parts) == 2 and parts[0] == "party":
            self._send_html(self._landing_html(query.get("invite", [None])[0]))
            return
        if parts == ["api", "health"]:
            self._send_json(
                {
                    "ok": True,
                    "room_id": self.room.room_id,
                    "share_url": self.room.share_link(_request_base_url(self)),
                }
            )
            return
        if len(parts) == 3 and parts[:2] == ["api", "party"]:
            self._require_room(parts[2])
            self._require_invite(query.get("invite", [None])[0])
            self._send_json({"state": self.room.public_state()})
            return
        if len(parts) == 4 and parts[:2] == ["api", "party"] and parts[3] == "events":
            self._require_room(parts[2])
            self._require_invite(query.get("invite", [None])[0])
            participant_id = query.get("participant_id", [""])[0]
            self.room.touch_participant(participant_id)
            sequence = _int_or_none(query.get("since", ["0"])[0]) or 0
            events = self.room.events_since(sequence)
            self._send_json({"events": [event.to_json() for event in events], "state": self.room.public_state()})
            return
        if len(parts) == 4 and parts[:2] == ["api", "party"] and parts[3] == "participants":
            self._require_room(parts[2])
            self._require_host()
            self._send_json({"participants": self.room.public_state()["participants"]})
            return
        self._send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def _handle_post(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        parts = _path_parts(parsed.path)
        body = self._read_json()
        if len(parts) == 4 and parts[:2] == ["api", "party"] and parts[3] == "join":
            self._require_room(parts[2])
            self._require_invite(str(body.get("invite") or ""))
            participant, state = self.room.join(str(body.get("username") or "Guest"))
            self._send_json({"participant": participant.to_json(), "state": state})
            return
        if len(parts) == 4 and parts[:2] == ["api", "party"] and parts[3] == "control":
            self._require_room(parts[2])
            self._require_host()
            event = self.room.control(str(body.get("action") or ""), body)
            self._send_json({"event": event.to_json(), "state": self.room.public_state()})
            return
        if len(parts) == 4 and parts[:2] == ["api", "party"] and parts[3] == "kick":
            self._require_room(parts[2])
            self._require_host()
            participant = self.room.kick(str(body.get("participant_id") or ""))
            self._send_json({"participant": participant.to_json()})
            return
        if len(parts) == 4 and parts[:2] == ["api", "party"] and parts[3] == "chat":
            self._require_room(parts[2])
            self._require_invite(str(body.get("invite") or ""))
            participant_id = str(body.get("participant_id") or "")
            if not participant_id:
                raise WatchPartyError("participant id required")
            event = self.room.send_chat(
                str(body.get("message") or ""),
                participant_id=participant_id,
            )
            self._send_json({"event": event.to_json(), "state": self.room.public_state()})
            return
        if len(parts) == 4 and parts[:2] == ["api", "party"] and parts[3] == "end":
            self._require_room(parts[2])
            self._require_host()
            event = self.room.end()
            self._send_json({"event": event.to_json()})
            return
        if len(parts) == 5 and parts[:2] == ["api", "party"] and parts[3] == "participants":
            self._require_room(parts[2])
            self._require_invite(str(body.get("invite") or ""))
            participant_id = parts[4]
            if body.get("leave"):
                self.room.leave(participant_id)
                self._send_json({"left": True})
                return
            participant = self.room.set_username(participant_id, str(body.get("username") or "Guest"))
            self._send_json({"participant": participant.to_json()})
            return
        self._send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def _landing_html(self, invite_token: str | None) -> str:
        if not self.room.validate_invite(invite_token):
            return "<!doctype html><title>Watch Party</title><h1>Invalid watch party invite.</h1>"
        state = self.room.public_state()
        media = state["media"]
        command = f"ani-watch party join {self.room.share_link(_request_base_url(self))}"
        return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>{_html_escape(state["party_title"])}</title>
  <style>
    body {{ background:#101114; color:#f1f3f6; font-family:sans-serif; padding:2rem; }}
    code {{ background:#1f2229; padding:.4rem .55rem; display:inline-block; }}
    .muted {{ color:#a9b0bb; }}
  </style>
</head>
<body>
  <h1>{_html_escape(state["party_title"])}</h1>
  <p class="muted">Host: {_html_escape(state["host_username"])}</p>
  <p>{_html_escape(media["anime_title"])} - episode {_html_escape(media["episode"])} ({_html_escape(media["mode"])})</p>
  <p>Open AniAutoWatchList and choose <strong>Watch Party -> Join Watch Party</strong>, then paste this page URL.</p>
  <p>CLI join command:</p>
  <code>{_html_escape(command)}</code>
</body>
</html>"""

    def _require_room(self, room_id: str) -> None:
        if room_id != self.room.room_id:
            raise WatchPartyError("watch party room not found")

    def _require_invite(self, invite_token: str | None) -> None:
        if not self.room.validate_invite(invite_token):
            raise WatchPartyError("invalid watch party invite")

    def _require_host(self) -> None:
        if not self.room.validate_host(self.headers.get("X-Ani-Watch-Host-Token")):
            raise WatchPartyError("host permission required")

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length).decode("utf-8")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise WatchPartyError(f"invalid JSON body: {exc}") from exc
        return payload if isinstance(payload, dict) else {}

    def _send_json(self, payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self._write_cors_headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html: str, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = html.encode("utf-8")
        self.send_response(status)
        self._write_cors_headers()
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_redirect(self, location: str) -> None:
        self.send_response(HTTPStatus.FOUND)
        self._write_cors_headers()
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _write_cors_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Ani-Watch-Host-Token")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")


class WatchPartyLocalServer:
    def __init__(self, room: WatchPartyRoom, *, host: str = DEFAULT_PARTY_HOST, port: int = 0):
        self.room = room
        self.host = host
        self.port = port
        self.httpd: WatchPartyHTTPServer | None = None
        self.thread: threading.Thread | None = None

    def start(self) -> "WatchPartyLocalServer":
        httpd = WatchPartyHTTPServer((self.host, self.port), WatchPartyRequestHandler)
        httpd.room = self.room  # type: ignore[attr-defined]
        self.httpd = httpd
        self.port = int(httpd.server_address[1])
        self.thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        self.thread.start()
        return self

    @property
    def local_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def close(self) -> None:
        if self.httpd is not None:
            self.httpd.shutdown()
            self.httpd.server_close()
            self.httpd = None
        if self.thread is not None:
            self.thread.join(timeout=5)
            self.thread = None


@dataclass
class WatchPartyHostSession:
    room: WatchPartyRoom
    server: WatchPartyLocalServer
    share_url: str
    local_url: str
    tunnel: WatchPartyTunnel | None = None
    tunnel_error: str | None = None

    @property
    def public(self) -> bool:
        return self.tunnel is not None

    def close(self, *, notify_guests: bool = True, notify_grace_seconds: float = 0.0) -> None:
        if notify_guests:
            try:
                self.room.end()
            except Exception:
                pass
        if notify_grace_seconds > 0:
            time.sleep(notify_grace_seconds)
        if self.tunnel is not None:
            self.tunnel.close()
            self.tunnel = None
        self.server.close()


def start_host_session(
    media: WatchPartyMedia,
    *,
    host_username: str,
    use_tunnel: bool = True,
) -> WatchPartyHostSession:
    room = WatchPartyRoom(media, host_username=host_username)
    server = WatchPartyLocalServer(room).start()
    tunnel = None
    tunnel_error = None
    if use_tunnel:
        tunnel, tunnel_error = start_public_tunnel(server.local_url)
    public_base = tunnel.public_url if tunnel is not None else server.local_url
    return WatchPartyHostSession(
        room=room,
        server=server,
        share_url=room.share_link(public_base),
        local_url=room.share_link(server.local_url),
        tunnel=tunnel,
        tunnel_error=tunnel_error,
    )


def start_cloudflared_tunnel(local_url: str, *, timeout: float = 18.0) -> WatchPartyTunnel | None:
    tunnel, _error = start_public_tunnel(local_url, timeout=timeout)
    return tunnel


def start_public_tunnel(local_url: str, *, timeout: float = 24.0) -> tuple[WatchPartyTunnel | None, str | None]:
    cloudflared, error = resolve_cloudflared()
    if not cloudflared:
        return None, error
    try:
        return _start_cloudflared_process(cloudflared, local_url, timeout=timeout)
    except OSError as exc:
        return None, f"cloudflared could not start: {exc}"


def resolve_cloudflared(*, auto_download: bool = True) -> tuple[str | None, str | None]:
    override = os.environ.get("ANI_WATCH_PARTY_CLOUDFLARED")
    if override:
        path = Path(override).expanduser()
        if path.exists() and os.access(path, os.X_OK):
            return str(path), None
        return None, f"ANI_WATCH_PARTY_CLOUDFLARED is not executable: {path}"
    existing = shutil.which("cloudflared")
    if existing:
        return existing, None
    cached = cached_cloudflared_path()
    if cached.exists() and os.access(cached, os.X_OK):
        return str(cached), None
    if os.environ.get("ANI_WATCH_PARTY_NO_AUTO_CLOUDFLARED") == "1" or not auto_download:
        return None, "cloudflared is not installed and automatic download is disabled."
    asset = cloudflared_asset_name()
    if asset is None:
        return None, f"automatic cloudflared download is not supported on {platform.system()} {platform.machine()}"
    try:
        return str(download_cloudflared(asset)), None
    except Exception as exc:
        return None, f"cloudflared download failed: {exc}"


def cached_cloudflared_path() -> Path:
    return get_paths().data_dir / "bin" / "cloudflared"


def cloudflared_asset_name(system: str | None = None, machine: str | None = None) -> str | None:
    system_name = (system or platform.system()).casefold()
    machine_name = (machine or platform.machine()).casefold().replace("-", "_")
    if system_name != "linux":
        return None
    if machine_name in {"x86_64", "amd64"}:
        return "cloudflared-linux-amd64"
    if machine_name in {"aarch64", "arm64"}:
        return "cloudflared-linux-arm64"
    if machine_name in {"i386", "i686", "x86"}:
        return "cloudflared-linux-386"
    if machine_name.startswith("armv7") or machine_name in {"armhf"}:
        return "cloudflared-linux-armhf"
    if machine_name.startswith("arm"):
        return "cloudflared-linux-arm"
    return None


def download_cloudflared(asset: str, *, timeout: float = 90.0) -> Path:
    dest = cached_cloudflared_path()
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(f"{dest.name}.download")
    url = f"{CLOUDFLARED_DOWNLOAD_BASE_URL}/{asset}"
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/octet-stream",
            "User-Agent": "ani-watchlist-party-cloudflared-bootstrap/0.1",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        status = int(getattr(response, "status", 200) or 200)
        if status >= 400:
            raise WatchPartyError(f"GitHub returned HTTP {status} for {asset}")
        with tmp.open("wb") as fh:
            shutil.copyfileobj(response, fh)
    size = tmp.stat().st_size
    if size < CLOUDFLARED_MIN_DOWNLOAD_BYTES:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise WatchPartyError(f"downloaded cloudflared file was unexpectedly small ({size} bytes)")
    tmp.chmod(0o755)
    tmp.replace(dest)
    return dest


def _start_cloudflared_process(
    cloudflared: str,
    local_url: str,
    *,
    timeout: float = 24.0,
) -> tuple[WatchPartyTunnel | None, str | None]:
    process = subprocess.Popen(
        [cloudflared, "tunnel", "--url", local_url, "--no-autoupdate"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    output: queue.Queue[str] = queue.Queue()
    lines: list[str] = []

    def reader() -> None:
        if process.stdout is None:
            return
        for line in process.stdout:
            output.put(line)

    threading.Thread(target=reader, daemon=True).start()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            break
        try:
            line = output.get(timeout=0.25)
        except queue.Empty:
            continue
        lines.append(line.rstrip())
        match = CLOUDFLARED_URL_RE.search(line)
        if match:
            return WatchPartyTunnel(public_url=match.group(0).rstrip("/"), process=process, lines=tuple(lines)), None
    last_lines = "\n".join(lines[-5:]).strip()
    if process.poll() is None:
        process.terminate()
    if process.poll() is not None:
        return None, f"cloudflared exited before creating a public URL.{(' Last output: ' + last_lines) if last_lines else ''}"
    return None, f"cloudflared did not provide a public URL within {int(timeout)} seconds."


class WatchPartyRemoteClient:
    def __init__(self, link: str, username: str, *, timeout: float = 12.0):
        self.link = link
        parsed = resolve_party_link(link, timeout=timeout)
        self.base_url = parsed["base_url"]
        self.room_id = parsed["room_id"]
        self.invite_token = parsed["invite"]
        self.username = _clean_username(username)
        self.timeout = timeout
        self.participant_id: str | None = None
        self.latest_sequence = 0
        self.state: dict[str, Any] | None = None

    def join(self) -> dict[str, Any]:
        payload = self._request(
            "POST",
            f"/api/party/{urllib.parse.quote(self.room_id)}/join",
            {"username": self.username, "invite": self.invite_token},
        )
        participant = payload.get("participant") or {}
        self.participant_id = str(participant.get("participant_id") or "")
        if not self.participant_id:
            raise WatchPartyError("watch party join response did not include a participant id")
        self.state = payload.get("state") if isinstance(payload.get("state"), dict) else {}
        self.latest_sequence = int((self.state or {}).get("latest_sequence") or 0)
        return payload

    def poll_events(self, *, timeout: float = 25.0) -> dict[str, Any]:
        if not self.participant_id:
            raise WatchPartyError("not joined to a watch party")
        query = urllib.parse.urlencode(
            {
                "invite": self.invite_token,
                "participant_id": self.participant_id,
                "since": self.latest_sequence,
            }
        )
        previous_timeout = self.timeout
        self.timeout = timeout + 5
        try:
            payload = self._request("GET", f"/api/party/{urllib.parse.quote(self.room_id)}/events?{query}", None)
        finally:
            self.timeout = previous_timeout
        for event in payload.get("events") or []:
            if isinstance(event, dict):
                self.latest_sequence = max(self.latest_sequence, int(event.get("sequence") or 0))
        if isinstance(payload.get("state"), dict):
            self.state = payload["state"]
        return payload

    def fetch_state(self) -> dict[str, Any]:
        query = urllib.parse.urlencode({"invite": self.invite_token})
        payload = self._request("GET", f"/api/party/{urllib.parse.quote(self.room_id)}?{query}", None)
        state = payload.get("state") if isinstance(payload.get("state"), dict) else {}
        self.state = state
        return state

    def set_username(self, username: str) -> dict[str, Any]:
        if not self.participant_id:
            raise WatchPartyError("not joined to a watch party")
        self.username = _clean_username(username)
        return self._request(
            "POST",
            f"/api/party/{urllib.parse.quote(self.room_id)}/participants/{urllib.parse.quote(self.participant_id)}",
            {"username": self.username, "invite": self.invite_token},
        )

    def send_chat(self, message: str) -> dict[str, Any]:
        if not self.participant_id:
            raise WatchPartyError("not joined to a watch party")
        return self._request(
            "POST",
            f"/api/party/{urllib.parse.quote(self.room_id)}/chat",
            {"message": message, "participant_id": self.participant_id, "invite": self.invite_token},
        )

    def leave(self) -> None:
        if not self.participant_id:
            return
        try:
            self._request(
                "POST",
                f"/api/party/{urllib.parse.quote(self.room_id)}/participants/{urllib.parse.quote(self.participant_id)}",
                {"leave": True, "invite": self.invite_token},
            )
        finally:
            self.participant_id = None

    def _request(self, method: str, path: str, payload: dict[str, Any] | None) -> dict[str, Any]:
        data = json.dumps(payload or {}).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(
            self.base_url.rstrip("/") + path,
            data=data,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            try:
                error_payload = json.loads(exc.read().decode("utf-8"))
            except Exception:
                error_payload = {"error": str(exc)}
            raise WatchPartyError(str(error_payload.get("error") or exc)) from exc
        except OSError as exc:
            raise WatchPartyError(f"watch party request failed: {exc}") from exc
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise WatchPartyError(f"invalid watch party response: {exc}") from exc
        return parsed if isinstance(parsed, dict) else {}


class MpvIpcController:
    def __init__(self, socket_path: str | Path):
        self.socket_path = str(socket_path)

    def available(self) -> bool:
        return bool(self.socket_path and Path(self.socket_path).exists())

    def play(self) -> None:
        self._command(["set_property", "pause", False])

    def pause(self) -> None:
        self._command(["set_property", "pause", True])

    def seek(self, position_seconds: float) -> None:
        self._command(["seek", max(0.0, float(position_seconds)), "absolute+exact"])

    def relative_seek(self, delta_seconds: float) -> None:
        self._command(["seek", float(delta_seconds), "relative+exact"])

    def stop(self) -> None:
        self._command(["quit"])

    def set_property(self, name: str, value: Any) -> None:
        self._command(["set_property", name, value])

    def set_fullscreen(self, enabled: bool) -> None:
        self.set_property("fullscreen", bool(enabled))

    def get_property(self, name: str) -> Any:
        response = self._command(["get_property", name])
        if not isinstance(response, dict) or response.get("error") != "success":
            return None
        return response.get("data")

    def time_position(self) -> float | None:
        position = self.get_property("time-pos")
        try:
            return max(0.0, float(position))
        except (TypeError, ValueError):
            return None

    def snapshot(self) -> dict[str, Any] | None:
        if not self.available():
            return None
        try:
            position_seconds = self.time_position()
            paused = self.get_property("pause")
        except OSError:
            return None
        if position_seconds is None:
            return None
        return {
            "position_seconds": max(0.0, position_seconds),
            "paused": bool(paused) if paused is not None else False,
        }

    def _command(self, command: list[Any]) -> dict[str, Any] | None:
        if not self.socket_path:
            return None
        payload = (json.dumps({"command": command}) + "\n").encode("utf-8")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(2)
            client.connect(self.socket_path)
            client.sendall(payload)
            try:
                response = client.recv(4096)
            except socket.timeout:
                return None
        if not response:
            return None
        try:
            parsed = json.loads(response.decode("utf-8"))
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None


def parse_party_link(link: str) -> dict[str, str]:
    errors: list[WatchPartyError] = []
    for candidate in _party_link_candidates(str(link)):
        try:
            return _parse_party_link_url(candidate)
        except WatchPartyError as exc:
            errors.append(exc)
    if errors:
        raise errors[-1]
    raise WatchPartyError("watch party link must be an http or https URL")


def resolve_party_link(link: str, *, timeout: float = 8.0) -> dict[str, str]:
    try:
        return parse_party_link(link)
    except WatchPartyError as original_error:
        for candidate in _party_link_candidates(str(link)):
            for resolved_link in _resolve_party_link_candidates(candidate, timeout=timeout):
                try:
                    return parse_party_link(resolved_link)
                except WatchPartyError:
                    continue
        raise original_error


def _parse_party_link_url(link: str) -> dict[str, str]:
    parsed = urllib.parse.urlparse(link)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise WatchPartyError("watch party link must be an http or https URL")
    parts = _path_parts(parsed.path)
    if len(parts) != 2 or parts[0] != "party" or not parts[1]:
        raise WatchPartyError(
            "watch party link is incomplete. Paste the full invite link from the host window, "
            "including /party/... and ?invite=..."
        )
    query = urllib.parse.parse_qs(parsed.query)
    invite = query.get("invite", [""])[0]
    if not invite:
        raise WatchPartyError(
            "watch party link is missing the invite token. Paste the full invite link from the host window."
        )
    return {
        "base_url": f"{parsed.scheme}://{parsed.netloc}",
        "room_id": urllib.parse.unquote(parts[1]),
        "invite": invite,
    }


def _party_link_candidates(value: str) -> list[str]:
    candidates: list[str] = []
    raw = _clean_pasted_url(value)
    if raw:
        candidates.append(raw)
    for match in PARTY_LINK_URL_RE.finditer(value):
        candidate = _clean_pasted_url(match.group(0))
        if candidate and candidate not in candidates:
            candidates.append(candidate)
    return candidates


def _clean_pasted_url(value: str) -> str:
    cleaned = str(value).strip().strip("<>'\"`")
    while cleaned and cleaned[-1] in ".,;)]}>":
        cleaned = cleaned[:-1]
    return cleaned.strip()


def _resolve_party_link_candidates(link: str, *, timeout: float) -> list[str]:
    parsed = urllib.parse.urlparse(link)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return []
    candidates: list[str] = []
    request = urllib.request.Request(
        link,
        headers={"User-Agent": PARTY_LINK_RESOLVER_USER_AGENT},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            final_url = _clean_pasted_url(response.geturl())
            if final_url and final_url != link:
                candidates.append(final_url)
            body = response.read(65_536).decode("utf-8", errors="replace")
    except (OSError, urllib.error.URLError, ValueError):
        body = ""
    for candidate in _party_link_candidates(body):
        if candidate not in candidates:
            candidates.append(candidate)
    health_link = _resolve_party_link_from_health(parsed, timeout=timeout)
    if health_link and health_link not in candidates:
        candidates.append(health_link)
    return candidates


def _resolve_party_link_from_health(parsed: urllib.parse.ParseResult, *, timeout: float) -> str | None:
    base_url = f"{parsed.scheme}://{parsed.netloc}"
    request = urllib.request.Request(
        f"{base_url}/api/health",
        headers={
            "Accept": "application/json",
            "User-Agent": PARTY_LINK_RESOLVER_USER_AGENT,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read(65_536).decode("utf-8"))
    except (OSError, urllib.error.URLError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    share_url = payload.get("share_url")
    return str(share_url).strip() if share_url else None


def party_ipc_path(prefix: str) -> str:
    safe = re.sub(r"[^0-9A-Za-z_.-]+", "-", prefix).strip("-") or secrets.token_hex(4)
    return str(Path(os.environ.get("TMPDIR", "/tmp")) / f"ani-watch-party-{safe}.sock")


def _request_base_url(handler: BaseHTTPRequestHandler) -> str:
    host = handler.headers.get("Host") or f"127.0.0.1:{handler.server.server_address[1]}"
    scheme = "https" if handler.headers.get("X-Forwarded-Proto") == "https" or host.endswith(".trycloudflare.com") else "http"
    return f"{scheme}://{host}"


def _path_parts(path: str) -> list[str]:
    return [urllib.parse.unquote(part) for part in path.split("/") if part]


def _clean_username(username: str) -> str:
    cleaned = re.sub(r"\s+", " ", str(username or "").strip())
    return cleaned[:40] or "Guest"


def _clean_chat_message(message: str) -> str:
    cleaned = re.sub(r"[ \t\r\f\v]+", " ", str(message or ""))
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned[:MAX_CHAT_MESSAGE_LENGTH]


def _int_or_none(value: object) -> int | None:
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return parsed


def _parse_iso(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _html_escape(value: object) -> str:
    text = str(value)
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )
