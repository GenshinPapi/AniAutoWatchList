from __future__ import annotations

from types import SimpleNamespace

from ani_watchlist.gui import (
    UNWATCHED_ICON,
    WATCHED_ICON,
    WATCHLIST_AUTO_REFRESH_MS,
    discovery_page_count,
    discovery_page_items,
    discovery_title_preview,
    scroll_units_from_mousewheel,
    split_display_title,
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


def test_discovery_paging_splits_items_into_twenty_item_pages() -> None:
    items = list(range(45))

    assert discovery_page_count(len(items)) == 3
    assert discovery_page_items(items, 0) == list(range(20))
    assert discovery_page_items(items, 1) == list(range(20, 40))
    assert discovery_page_items(items, 2) == list(range(40, 45))
