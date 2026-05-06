from __future__ import annotations

from ani_watchlist.gui import UNWATCHED_ICON, WATCHED_ICON, split_display_title


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
