from __future__ import annotations

from types import SimpleNamespace

from ani_watchlist.gui import UNWATCHED_ICON, WATCHED_ICON, scroll_units_from_mousewheel, split_display_title


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
