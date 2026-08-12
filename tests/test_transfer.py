from __future__ import annotations

import json
import xml.etree.ElementTree as ET

from ani_watchlist.db import initialize
from ani_watchlist.store import episodes_for_anime, get_anime, get_or_create_anime, mark_episode, update_anime_fields, upsert_episodes
from ani_watchlist.transfer import WatchlistTransferError, export_watchlist_text, import_watchlist_text, write_auto_backup_files


def _anime_xml(title: str, *, mal_id: int = 0, status: str = "Watching", watched: int = 0, total: int = 0) -> str:
    return f"""
    <anime>
      <series_animedb_id>{mal_id}</series_animedb_id>
      <series_title>{title}</series_title>
      <series_type>TV</series_type>
      <series_episodes>{total}</series_episodes>
      <my_watched_episodes>{watched}</my_watched_episodes>
      <my_start_date>2026-05-01</my_start_date>
      <my_finish_date>2026-05-02</my_finish_date>
      <my_status>{status}</my_status>
      <my_comments>portable note</my_comments>
    </anime>
    """


def test_xml_export_uses_mal_style_fields(app_env) -> None:
    conn = initialize()
    anime, _ = get_or_create_anime(conn, "Cowboy Bebop")
    upsert_episodes(conn, anime["id"], ["1", "2"], source_label="test")
    mark_episode(conn, anime["id"], "1", watched=True)
    update_anime_fields(conn, anime["id"], status="watching", total_episodes=26, notes="see you space cowboy")
    conn.execute(
        """
        INSERT INTO metadata_matches(
            anime_id, provider, provider_media_id, confidence_score, selected, payload_json, created_at
        ) VALUES (?, 'anilist', '1', 1.0, 1, ?, '2026-05-04T00:00:00+00:00')
        """,
        (anime["id"], json.dumps({"id": 1, "idMal": 1, "format": "TV"}, sort_keys=True)),
    )

    root = ET.fromstring(export_watchlist_text(conn, "xml"))
    exported = root.find("anime")

    assert exported is not None
    assert exported.findtext("series_animedb_id") == "1"
    assert exported.findtext("series_title") == "Cowboy Bebop"
    assert exported.findtext("series_episodes") == "26"
    assert exported.findtext("my_watched_episodes") == "1"
    assert exported.findtext("my_status") == "Watching"
    assert exported.findtext("my_comments") == "see you space cowboy"
    assert exported.findtext("update_on_import") == "1"


def test_xml_export_resolves_missing_mal_id(app_env) -> None:
    conn = initialize()
    anime, _ = get_or_create_anime(conn, "Frieren: Beyond Journey's End")
    update_anime_fields(conn, anime["id"], anilist_id=154587, total_episodes=28)

    root = ET.fromstring(
        export_watchlist_text(
            conn,
            "xml",
            mal_id_resolver=lambda anilist_id: {"id": anilist_id, "idMal": 52991, "format": "TV"},
        )
    )
    exported = root.find("anime")

    assert exported is not None
    assert exported.findtext("series_animedb_id") == "52991"
    assert exported.findtext("update_on_import") == "1"


def test_xml_export_can_skip_entries_without_mal_id(app_env) -> None:
    conn = initialize()
    with_id, _ = get_or_create_anime(conn, "Known MAL Show")
    update_anime_fields(conn, with_id["id"], anilist_id=1)
    without_id, _ = get_or_create_anime(conn, "No MAL Show")
    update_anime_fields(conn, without_id["id"], anilist_id=2)

    root = ET.fromstring(
        export_watchlist_text(
            conn,
            "xml",
            mal_id_resolver=lambda anilist_id: {"idMal": 10} if anilist_id == 1 else {},
            skip_missing_mal_ids=True,
        )
    )
    exported_titles = [anime.findtext("series_title") for anime in root.findall("anime")]
    exported_ids = [anime.findtext("series_animedb_id") for anime in root.findall("anime")]

    assert exported_titles == ["Known MAL Show"]
    assert exported_ids == ["10"]
    assert root.find("myinfo/user_total_anime").text == "1"


def test_xml_export_refuses_empty_mal_targeted_file(app_env) -> None:
    conn = initialize()
    get_or_create_anime(conn, "No MAL Show")

    try:
        export_watchlist_text(conn, "xml", skip_missing_mal_ids=True)
    except WatchlistTransferError as exc:
        assert "No entries have MAL AnimeDB IDs" in str(exc)
    else:
        raise AssertionError("expected WatchlistTransferError")


def test_xml_import_sync_adds_missing_and_skips_existing(app_env) -> None:
    conn = initialize()
    existing, _ = get_or_create_anime(conn, "Existing Show")
    upsert_episodes(conn, existing["id"], ["1"], source_label="manual")
    xml = f"<myanimelist>{_anime_xml('Existing Show', watched=2, total=2)}{_anime_xml('New Show', mal_id=99, watched=2, total=3)}</myanimelist>"

    result = import_watchlist_text(conn, xml, "xml", mode="sync")

    assert result["anime"] == 1
    assert result["skipped_anime"] == 1
    assert [episode["episode_key"] for episode in episodes_for_anime(conn, existing["id"])] == ["1"]
    new = get_anime(conn, "New Show")
    assert new is not None
    assert new["status"] == "watching"
    assert new["total_episodes"] == 3
    assert [episode["watched"] for episode in episodes_for_anime(conn, new["id"])] == [1, 1, 0]
    mal_match = conn.execute("SELECT * FROM metadata_matches WHERE anime_id = ? AND provider = 'myanimelist'", (new["id"],)).fetchone()
    assert mal_match["provider_media_id"] == "99"


def test_json_replace_import_preserves_metadata_and_events(app_env) -> None:
    conn = initialize()
    get_or_create_anime(conn, "Old Show")
    data = {
        "anime": [
            {
                "id": 10,
                "display_title": "Imported Show",
                "source_title": "Imported Show",
                "status": "completed",
                "anilist_id": 123,
                "total_episodes": 1,
                "notes": "done",
            }
        ],
        "episodes": [
            {
                "id": 20,
                "anime_id": 10,
                "episode_key": "1",
                "episode_number": "1",
                "watched": 1,
                "watched_at": "2026-05-04T00:00:00+00:00",
            }
        ],
        "metadata_matches": [
            {
                "anime_id": 10,
                "provider": "anilist",
                "provider_media_id": "123",
                "confidence_score": 1.0,
                "selected": 1,
                "payload_json": "{\"id\": 123}",
                "created_at": "2026-05-04T00:00:00+00:00",
            }
        ],
        "watch_events": [
            {
                "anime_id": 10,
                "episode_id": 20,
                "event_type": "playback_started",
                "payload_json": "{\"episode\": \"1\"}",
                "created_at": "2026-05-04T00:00:00+00:00",
            }
        ],
    }

    result = import_watchlist_text(conn, json.dumps(data), "json", mode="replace")

    anime = get_anime(conn, "Imported Show")
    assert result["anime"] == 1
    assert get_anime(conn, "Old Show") is None
    assert anime["status"] == "completed"
    assert episodes_for_anime(conn, anime["id"])[0]["watched"] == 1
    assert conn.execute("SELECT COUNT(*) AS count FROM metadata_matches").fetchone()["count"] == 1
    assert conn.execute("SELECT COUNT(*) AS count FROM watch_events").fetchone()["count"] == 1


def test_auto_backup_creates_full_json_and_portable_xml(app_env, tmp_path) -> None:
    conn = initialize()
    anime, _ = get_or_create_anime(conn, "Cowboy Bebop")
    upsert_episodes(conn, anime["id"], ["1", "2"], source_label="test")
    mark_episode(conn, anime["id"], "1", watched=True)

    targets = write_auto_backup_files(conn, tmp_path / "checkout")

    assert targets["json"].name == "jsonbackup.json"
    assert targets["xml"].name == "xmlbackup.xml"
    json_payload = json.loads(targets["json"].read_text(encoding="utf-8"))
    xml_root = ET.fromstring(targets["xml"].read_text(encoding="utf-8"))
    assert json_payload["format"] == "ani-watchlist"
    assert json_payload["anime"][0]["display_title"] == "Cowboy Bebop"
    assert len(json_payload["episodes"]) == 2
    assert xml_root.findtext("anime/series_title") == "Cowboy Bebop"
    assert xml_root.findtext("anime/my_watched_episodes") == "1"
    assert not list((tmp_path / "checkout").glob(".*backup.*.tmp"))


def test_auto_backup_replaces_existing_snapshots(app_env, tmp_path) -> None:
    conn = initialize()
    get_or_create_anime(conn, "First Show")
    backup_dir = tmp_path / "checkout"
    targets = write_auto_backup_files(conn, backup_dir)
    targets["json"].write_text("stale JSON", encoding="utf-8")
    targets["xml"].write_text("stale XML", encoding="utf-8")
    get_or_create_anime(conn, "Second Show")

    updated_targets = write_auto_backup_files(conn, backup_dir)

    json_titles = [row["display_title"] for row in json.loads(updated_targets["json"].read_text(encoding="utf-8"))["anime"]]
    xml_titles = [node.findtext("series_title") for node in ET.fromstring(updated_targets["xml"].read_text(encoding="utf-8")).findall("anime")]
    assert json_titles == ["First Show", "Second Show"]
    assert xml_titles == ["First Show", "Second Show"]
