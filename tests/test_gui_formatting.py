from __future__ import annotations

from types import SimpleNamespace

import pytest

from ani_watchlist.db import initialize
from ani_watchlist.gui import (
    IDLE_CLOSE_GRACE_MS,
    IDLE_PROMPT_AFTER_MS,
    UNWATCHED_ICON,
    WATCHED_ICON,
    WATCHLIST_AUTO_REFRESH_MS,
    WatchlistApp,
    cloud_button_presentation,
    discovery_page_count,
    discovery_page_items,
    discovery_status_button_text,
    discovery_title_preview,
    idle_prompt_due,
    metadata_payload_is_adult,
    scroll_units_from_mousewheel,
    split_display_title,
    title_has_adult_label,
    widget_class_owns_mousewheel,
    yview_can_scroll,
)
from ani_watchlist.store import get_anime, get_or_create_anime, update_anime_fields
from ani_watchlist.updater import UpdateInfo, UpdateLaunchResult


def test_split_display_title_hides_alternate_title_by_default() -> None:
    primary, alternate = split_display_title("Wistoria: Wand and Sword (Tsue to Tsurugi no Wistoria)")

    assert primary == "Wistoria: Wand and Sword"
    assert alternate == "Tsue to Tsurugi no Wistoria"


def test_split_display_title_leaves_plain_titles_alone() -> None:
    primary, alternate = split_display_title("Cowboy Bebop")

    assert primary == "Cowboy Bebop"
    assert alternate is None


def test_episode_status_icons_are_distinct() -> None:
    assert WATCHED_ICON != UNWATCHED_ICON


def test_scroll_units_support_common_mousewheel_events() -> None:
    assert scroll_units_from_mousewheel(SimpleNamespace(delta=120)) == -1
    assert scroll_units_from_mousewheel(SimpleNamespace(delta=-240)) == 2
    assert scroll_units_from_mousewheel(SimpleNamespace(delta=1)) == -1
    assert scroll_units_from_mousewheel(SimpleNamespace(num=4)) == -1
    assert scroll_units_from_mousewheel(SimpleNamespace(num=5)) == 1


def test_nested_scroll_widgets_own_mousewheel_events() -> None:
    for widget_class in ("Listbox", "Scrollbar", "Text", "Treeview", "TScrollbar"):
        assert widget_class_owns_mousewheel(widget_class)
    for widget_class in ("Canvas", "Entry", "Frame", "Label"):
        assert not widget_class_owns_mousewheel(widget_class)


def test_yview_edge_detection_allows_page_scroll_at_nested_edges() -> None:
    assert yview_can_scroll((0.25, 0.75), 1)
    assert yview_can_scroll((0.25, 0.75), -1)
    assert not yview_can_scroll((0.0, 0.5), -1)
    assert not yview_can_scroll((0.5, 1.0), 1)
    assert not yview_can_scroll((0.0, 1.0), 1)


def test_discovery_title_preview_preserves_start_of_long_titles() -> None:
    preview = discovery_title_preview(
        "This Is an Extremely Long Anime Title That Would Otherwise Overrun the Trending Card",
        max_lines=2,
        line_chars=22,
    )

    assert preview.startswith("This Is an Extremely")
    assert preview.endswith("...")
    assert len(preview.splitlines()) == 2


def test_watchlist_auto_refresh_is_thirty_seconds() -> None:
    assert WATCHLIST_AUTO_REFRESH_MS == 30_000


def test_cloud_button_presentation_has_accessible_connection_states() -> None:
    assert cloud_button_presentation("connected") == ("Cloud ✓", "CloudConnected.TMenubutton")
    assert cloud_button_presentation("disconnected") == ("Cloud !", "CloudDisconnected.TMenubutton")
    assert cloud_button_presentation("checking") == ("Cloud ...", "CloudChecking.TMenubutton")


def test_cloud_connection_state_updates_button_style_and_error() -> None:
    app = object.__new__(WatchlistApp)
    updates: list[dict[str, str]] = []
    app.cloud_button = SimpleNamespace(configure=lambda **kwargs: updates.append(kwargs))
    app.cloud_connection_checked_at = None

    app.set_cloud_connection_state("connected")
    assert app.cloud_connection_state == "connected"
    assert app.cloud_connection_error is None
    assert app.cloud_connection_checked_at is not None
    assert updates[-1] == {"text": "Cloud ✓", "style": "CloudConnected.TMenubutton"}

    app.set_cloud_connection_state("disconnected", error="offline")
    assert app.cloud_connection_error == "offline"
    assert updates[-1] == {"text": "Cloud !", "style": "CloudDisconnected.TMenubutton"}


def test_automatic_cloud_connection_check_turns_button_green(app_env, monkeypatch) -> None:
    app = object.__new__(WatchlistApp)
    app.shutting_down = False
    app.cloud_connection_check_running = False
    app.cloud_operation_running = False
    app.cloud_connection_check_job = "scheduled"
    app.cloud_button = SimpleNamespace(configure=lambda **_kwargs: None)
    app.cloud_connection_checked_at = None
    checks: list[str] = []

    class FakeProvider:
        def is_connected(self) -> bool:
            return True

        def synchronize_backups(self, _conn, _directory):
            checks.append("synchronized")
            return SimpleNamespace(files=())

    class ImmediateThread:
        def __init__(self, *, target, daemon):
            self.target = target
            self.daemon = daemon

        def start(self) -> None:
            self.target()

    monkeypatch.setattr("ani_watchlist.gui.GoogleDriveBackupProvider", FakeProvider)
    monkeypatch.setattr("ani_watchlist.gui.threading.Thread", ImmediateThread)
    monkeypatch.setattr(
        "ani_watchlist.gui.load_config",
        lambda: SimpleNamespace(cloud=SimpleNamespace(google_drive_auto_backup=True)),
    )
    monkeypatch.setattr("ani_watchlist.gui.record_cloud_backup_status", lambda **kwargs: kwargs)
    app.run_on_ui = lambda callback: callback()
    app.library_render_signature = object()
    app.activity_signature = object()
    app.current_page = "detail"
    app.refresh_dashboard = lambda: None

    app.start_google_drive_connection_check()

    assert checks == ["synchronized"]
    assert app.cloud_connection_check_job is None
    assert app.cloud_connection_check_running is False
    assert app.cloud_connection_state == "connected"
    assert app.cloud_connection_error is None


def test_automatic_cloud_connection_check_turns_button_red_without_token(monkeypatch) -> None:
    app = object.__new__(WatchlistApp)
    app.shutting_down = False
    app.cloud_connection_check_running = False
    app.cloud_operation_running = False
    app.cloud_connection_check_job = "scheduled"
    app.cloud_button = SimpleNamespace(configure=lambda **_kwargs: None)
    app.cloud_connection_checked_at = None

    class FakeProvider:
        def is_connected(self) -> bool:
            return False

    monkeypatch.setattr("ani_watchlist.gui.GoogleDriveBackupProvider", FakeProvider)

    app.start_google_drive_connection_check()

    assert app.cloud_connection_state == "disconnected"
    assert app.cloud_connection_error == "Google Drive is not connected."


def test_idle_watchdog_uses_four_hour_prompt_and_thirty_minute_close() -> None:
    assert IDLE_PROMPT_AFTER_MS == 4 * 60 * 60 * 1000
    assert IDLE_CLOSE_GRACE_MS == 30 * 60 * 1000


def test_idle_prompt_due_uses_elapsed_user_activity_time() -> None:
    last_activity = 10_000

    assert idle_prompt_due(last_activity, last_activity + IDLE_PROMPT_AFTER_MS)
    assert not idle_prompt_due(last_activity, last_activity + IDLE_PROMPT_AFTER_MS - 1)


def test_metadata_payload_is_adult_only_for_explicit_anilist_adult_flag() -> None:
    assert metadata_payload_is_adult({"isAdult": True}) is True
    assert metadata_payload_is_adult({"isAdult": False}) is False
    assert metadata_payload_is_adult(None) is False


def test_title_has_adult_label_handles_clean_and_legacy_labels() -> None:
    assert title_has_adult_label("Bible Black [18+]") is True
    assert title_has_adult_label("Bible Black [18 ]") is True
    assert title_has_adult_label("Cowboy Bebop") is False


def test_discovery_paging_splits_items_into_twenty_item_pages() -> None:
    items = list(range(45))

    assert discovery_page_count(len(items)) == 3
    assert discovery_page_items(items, 0) == list(range(20))
    assert discovery_page_items(items, 1) == list(range(20, 40))
    assert discovery_page_items(items, 2) == list(range(40, 45))


def test_discovery_status_button_has_compact_labels_for_every_watchlist_status() -> None:
    assert discovery_status_button_text(None) == "Add..."
    assert discovery_status_button_text("watching") == "Watching"
    assert discovery_status_button_text("completed") == "Completed"
    assert discovery_status_button_text("dropped") == "Dropped"
    assert discovery_status_button_text("on_hold") == "On Hold"
    assert discovery_status_button_text("plan_to_watch") == "Planned"


def test_discovery_status_action_adds_and_moves_one_watchlist_entry(app_env) -> None:
    app = object.__new__(WatchlistApp)
    app.conn = initialize()
    app.current_page = "trending"
    messages: list[dict[str, str]] = []
    refreshed: list[int] = []
    app.discovery_status_labels = {
        "trending": SimpleNamespace(configure=lambda **kwargs: messages.append(kwargs)),
    }
    app.start_episode_availability_refresh = lambda anime_id: refreshed.append(anime_id)
    item = {
        "id": 1,
        "display_title": "Cowboy Bebop",
        "episodes": 26,
        "cover_url": "https://example.test/bebop.jpg",
    }

    added = app.add_discovery_to_watchlist(item, "watching")
    moved = app.add_discovery_to_watchlist(item, "on_hold")

    rows = list(app.conn.execute("SELECT * FROM anime"))
    assert len(rows) == 1
    assert int(added["id"]) == int(moved["id"])
    assert moved["status"] == "on_hold"
    assert moved["anilist_id"] == 1
    assert moved["total_episodes"] == 26
    assert messages[0]["text"] == "Cowboy Bebop added to Watching."
    assert messages[1]["text"] == "Cowboy Bebop moved to On Hold."
    assert refreshed == [int(added["id"]), int(added["id"])]

    with pytest.raises(ValueError, match="invalid watchlist status"):
        app.add_discovery_to_watchlist(item, "invalid")


def test_discovery_card_navigation_only_opens_titles_in_watchlist(app_env) -> None:
    app = object.__new__(WatchlistApp)
    app.conn = initialize()
    opened: list[int] = []
    app.open_detail = lambda anime_id: opened.append(anime_id)
    item = {"id": 999, "display_title": "A Place Further than the Universe"}

    assert app.open_discovery_watchlist_item(item) is None
    anime, _created = get_or_create_anime(app.conn, str(item["display_title"]), status="plan_to_watch")
    update_anime_fields(app.conn, anime["id"], anilist_id=item["id"])

    assert app.discovery_watchlist_anime(item)["id"] == anime["id"]
    assert app.open_discovery_watchlist_item(item) == "break"
    assert opened == [int(anime["id"])]


def test_discovery_lookup_falls_back_to_matching_local_title(app_env) -> None:
    app = object.__new__(WatchlistApp)
    app.conn = initialize()
    anime, _created = get_or_create_anime(app.conn, "Frieren: Beyond Journey's End")

    matched = app.discovery_watchlist_anime(
        {"id": 154587, "display_title": "Frieren: Beyond Journey's End"}
    )

    assert matched is not None
    assert matched["id"] == anime["id"]
    assert get_anime(app.conn, "Frieren: Beyond Journey's End")["anilist_id"] is None


def test_ani_cli_only_update_prompt_can_launch_managed_update(monkeypatch) -> None:
    app = object.__new__(WatchlistApp)
    app.update_checking = True
    prompts: list[tuple[str, str]] = []
    info_messages: list[tuple[str, str]] = []
    launches: list[bool] = []

    def fake_askyesno(title: str, message: str) -> bool:
        prompts.append((title, message))
        return True

    def fake_launch_update() -> UpdateLaunchResult:
        launches.append(True)
        return UpdateLaunchResult(command=["bash", "-lc", "true"], pid=123, used_terminal=True)

    monkeypatch.setattr("ani_watchlist.gui.messagebox.askyesno", fake_askyesno)
    monkeypatch.setattr("ani_watchlist.gui.messagebox.showinfo", lambda title, message: info_messages.append((title, message)))
    monkeypatch.setattr("ani_watchlist.gui.messagebox.showwarning", lambda *args, **kwargs: None)
    monkeypatch.setattr("ani_watchlist.gui.launch_update", fake_launch_update)

    app.finish_update_check(
        None,
        UpdateInfo(
            update_available=True,
            local_version="4.14.1",
            remote_version="4.14.2",
            local_commit="abc123",
            remote_commit="def456",
            remote_message="upstream ani-cli fix",
        ),
    )

    assert app.update_checking is False
    assert launches == [True]
    assert prompts and prompts[0][0] == "ani-cli update available"
    assert "Update AniAutoWatchList now?" in prompts[0][1]
    assert "bundled ani-cli fixes are installed safely" in prompts[0][1]
    assert info_messages and info_messages[0][0] == "Update started"


def test_manual_update_button_can_launch_managed_update(monkeypatch) -> None:
    app = object.__new__(WatchlistApp)
    prompts: list[tuple[str, str]] = []
    info_messages: list[tuple[str, str]] = []
    launches: list[bool] = []

    def fake_askyesno(title: str, message: str) -> bool:
        prompts.append((title, message))
        return True

    def fake_launch_update() -> UpdateLaunchResult:
        launches.append(True)
        return UpdateLaunchResult(command=["bash", "-lc", "true"], pid=123, used_terminal=True)

    monkeypatch.setattr("ani_watchlist.gui.messagebox.askyesno", fake_askyesno)
    monkeypatch.setattr("ani_watchlist.gui.messagebox.showinfo", lambda title, message: info_messages.append((title, message)))
    monkeypatch.setattr("ani_watchlist.gui.messagebox.showwarning", lambda *args, **kwargs: None)
    monkeypatch.setattr("ani_watchlist.gui.launch_update", fake_launch_update)

    app.prompt_managed_update()

    assert launches == [True]
    assert prompts and prompts[0][0] == "Update AniAutoWatchList"
    assert "ani-cli -U" in prompts[0][1]
    assert info_messages and info_messages[0][0] == "Update started"


def test_close_app_writes_auto_backups_before_database_close(monkeypatch) -> None:
    app = object.__new__(WatchlistApp)
    events: list[str] = []
    app.shutting_down = False
    app.party_client = None
    app.party_host_session = None
    app.conn = SimpleNamespace(close=lambda: events.append("database closed"))
    app.root = SimpleNamespace(
        after_cancel=lambda _job: None,
        quit=lambda: events.append("root quit"),
        destroy=lambda: events.append("root destroyed"),
    )
    app.backup_watchlist_on_exit = lambda: events.append("backed up") or True
    app.stop_host_party_mpv_observer = lambda: None
    app.dismiss_idle_prompt = lambda: None
    app.stop_party_fullscreen_observer = lambda: None
    app.stop_party_playback = lambda: None
    app.destroy_party_window = lambda: None

    app.close_app()

    assert app.shutting_down is True
    assert events == ["backed up", "database closed", "root quit", "root destroyed"]


def test_exit_backup_writes_local_files_before_google_drive(monkeypatch, tmp_path) -> None:
    app = object.__new__(WatchlistApp)
    app.conn = object()
    events: list[str] = []
    targets = {"json": tmp_path / "jsonbackup.json", "xml": tmp_path / "xmlbackup.xml"}

    class FakeProvider:
        def is_connected(self) -> bool:
            return True

        def synchronize_backups(self, received_conn, _directory, *, local_files):
            assert received_conn is app.conn
            assert local_files == targets
            events.append("cloud")
            return SimpleNamespace(files=())

    monkeypatch.setattr(
        "ani_watchlist.gui.write_auto_backup_files",
        lambda _conn, _directory: events.append("local") or targets,
    )
    monkeypatch.setattr(
        "ani_watchlist.gui.load_config",
        lambda: SimpleNamespace(cloud=SimpleNamespace(google_drive_auto_backup=True)),
    )
    monkeypatch.setattr("ani_watchlist.gui.GoogleDriveBackupProvider", FakeProvider)
    monkeypatch.setattr(
        "ani_watchlist.gui.record_cloud_backup_status",
        lambda **kwargs: events.append("status") or kwargs,
    )

    assert app.backup_watchlist_on_exit() is True
    assert events == ["local", "cloud", "status"]
