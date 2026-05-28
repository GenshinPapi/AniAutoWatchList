from __future__ import annotations

from types import SimpleNamespace

from ani_watchlist.gui import (
    IDLE_CLOSE_GRACE_MS,
    IDLE_PROMPT_AFTER_MS,
    UNWATCHED_ICON,
    WATCHED_ICON,
    WATCHLIST_AUTO_REFRESH_MS,
    discovery_page_count,
    discovery_page_items,
    discovery_title_preview,
    idle_prompt_due,
    metadata_payload_is_adult,
    scroll_units_from_mousewheel,
    split_display_title,
    title_has_adult_label,
    widget_class_owns_mousewheel,
)


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
