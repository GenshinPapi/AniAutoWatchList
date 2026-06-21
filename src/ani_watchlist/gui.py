from __future__ import annotations

import argparse
import json
import os
import re
import threading
import webbrowser
from datetime import datetime, timedelta, timezone
from math import ceil
from time import monotonic, sleep
from textwrap import wrap
import tkinter as tk
from tkinter import messagebox, simpledialog, ttk

try:
    from PIL import Image, ImageDraw, ImageTk
except Exception:  # pragma: no cover - optional GUI enhancement
    Image = None
    ImageDraw = None
    ImageTk = None

from .availability import refresh_available_episodes_for_anime
from .config import load_config
from .db import initialize
from .discovery import (
    POPULAR_FILTERS,
    POPULAR_GENRE_ALL_LABEL,
    append_discovery_media_page,
    load_discovery,
    popular_filter_label,
    refresh_discovery,
    refresh_popular,
    related_media,
    search_media,
)
from .launcher import (
    LaunchError,
    allanime_episode_available,
    choose_ani_cli_search_title,
    launch_episode,
    resolve_allanime_launch_target,
)
from .metadata import refresh_metadata_for_anime, select_match, selected_metadata_payload, store_selected_metadata_payload
from .party import (
    MpvIpcController,
    WatchPartyError,
    WatchPartyMedia,
    WatchPartyRemoteClient,
    party_ipc_path,
    start_host_session,
)
from .providers.anilist import AniListProvider
from .timefmt import local_time
from .updater import UpdateInfo, check_for_update, launch_update
from .store import (
    STATUSES,
    clean_display_title,
    delete_anime,
    episodes_for_anime,
    get_anime_by_anilist_id,
    get_anime_by_id,
    get_or_create_anime,
    list_anime,
    mark_episode,
    merge_safe_duplicates,
    next_unwatched_episode,
    status_counts,
    update_anime_fields,
    upsert_episodes,
    watch_events,
    watched_episode_count,
)


STATUS_LABELS = {
    "watching": "Watching",
    "completed": "Completed",
    "dropped": "Dropped",
    "on_hold": "On Hold",
    "plan_to_watch": "Plan to Watch",
}

COLORS = {
    "bg": "#101114",
    "panel": "#17191e",
    "panel_alt": "#1f2229",
    "border": "#2d323b",
    "text": "#f1f3f6",
    "muted": "#a9b0bb",
    "accent": "#f47521",
    "accent_hover": "#ff8a3d",
    "danger": "#d44b4b",
    "entry": "#0f1115",
}

CARD_W = 178
DISCOVERY_CARD_W = 190
DISCOVERY_CARD_H = 376
DISCOVERY_GRID_W = DISCOVERY_CARD_W + 16
DISCOVERY_TITLE_LINES = 3
DISCOVERY_TITLE_CHARS = 24
DISCOVERY_PAGE_SIZE = 20
SEARCH_SUGGESTION_LIMIT = 8
SEARCH_RESULT_LIMIT = 50
SEARCH_DEBOUNCE_MS = 350
COVER_W = 142
COVER_H = 204
DETAIL_COVER_W = 170
DETAIL_COVER_H = 244
WATCHLIST_AUTO_REFRESH_MS = 30_000
IDLE_PROMPT_AFTER_MS = 4 * 60 * 60 * 1000
IDLE_CLOSE_GRACE_MS = 30 * 60 * 1000
IDLE_CHECK_INTERVAL_MS = 60_000
IDLE_ACTIVITY_THROTTLE_MS = 1000
PARTY_HOST_REFRESH_MS = 10_000
PARTY_HOST_STATE_SYNC_MS = 2_000
PARTY_INITIAL_SYNC_TIMEOUT_SECONDS = 45.0
IDLE_ACTIVITY_EVENTS = ("<KeyPress>", "<ButtonPress>", "<MouseWheel>", "<Button-4>", "<Button-5>", "<Motion>")
NESTED_MOUSEWHEEL_WIDGET_CLASSES = {"Listbox", "Scrollbar", "Text", "Treeview", "TScrollbar"}
SCROLL_EDGE_EPSILON = 0.001
WATCHED_ICON = "✅"
UNWATCHED_ICON = "❌"
ADULT_TITLE_LABEL_RE = re.compile(r"\[\s*(?:18\s*\+?|adult)\s*\]", re.IGNORECASE)

DISCOVERY_MEDIA_PAGES = {
    "trending": {
        "nav": "Trending",
        "title": "Trending",
        "empty": "Trending data will appear here after AniList refreshes.",
    },
    "top_airing": {
        "nav": "Top Airing",
        "title": "Top Airing",
        "empty": "Top airing anime will appear here after AniList refreshes.",
    },
    "popular": {
        "nav": "Most Popular",
        "title": "Most Popular",
        "empty": "Most popular anime will appear here after AniList refreshes.",
    },
}


def split_display_title(title: str) -> tuple[str, str | None]:
    title = clean_display_title(title)
    if title.endswith(")") and " (" in title:
        primary, secondary = title.rsplit(" (", 1)
        primary = primary.strip()
        secondary = secondary[:-1].strip()
        if primary and secondary and primary.casefold() != secondary.casefold():
            return primary, secondary
    return title, None


def metadata_payload_is_adult(payload: dict[str, object] | None) -> bool:
    return isinstance(payload, dict) and payload.get("isAdult") is True


def title_has_adult_label(title: object) -> bool:
    return bool(ADULT_TITLE_LABEL_RE.search(str(title or "")))


def scroll_units_from_mousewheel(event) -> int:
    button_number = getattr(event, "num", None)
    if button_number == 4:
        return -1
    if button_number == 5:
        return 1
    delta = int(getattr(event, "delta", 0) or 0)
    if delta == 0:
        return 0
    steps = max(1, abs(delta) // 120)
    return -steps if delta > 0 else steps


def discovery_title_preview(title: str, *, max_lines: int = DISCOVERY_TITLE_LINES, line_chars: int = DISCOVERY_TITLE_CHARS) -> str:
    title = " ".join(str(title).split())
    if not title or max_lines <= 0:
        return ""
    line_chars = max(1, line_chars)
    lines = wrap(title, width=line_chars, break_long_words=True, break_on_hyphens=True)
    if len(lines) <= max_lines:
        return "\n".join(lines)
    preview = lines[:max_lines]
    preview[-1] = preview[-1][: max(0, line_chars - 3)].rstrip() + "..."
    return "\n".join(preview)


def discovery_page_count(item_count: int, *, page_size: int = DISCOVERY_PAGE_SIZE) -> int:
    page_size = max(1, int(page_size))
    return max(1, (max(0, int(item_count)) + page_size - 1) // page_size)


def discovery_page_items(items: list[object], page_index: int, *, page_size: int = DISCOVERY_PAGE_SIZE) -> list[object]:
    page_size = max(1, int(page_size))
    page_index = max(0, int(page_index))
    start = page_index * page_size
    return items[start : start + page_size]


def monotonic_ms() -> int:
    return int(monotonic() * 1000)


def idle_prompt_due(last_activity_ms: int, now_ms: int, *, prompt_after_ms: int = IDLE_PROMPT_AFTER_MS) -> bool:
    return now_ms - last_activity_ms >= prompt_after_ms


def widget_class_owns_mousewheel(widget_class: str) -> bool:
    return widget_class in NESTED_MOUSEWHEEL_WIDGET_CLASSES


def yview_can_scroll(yview: tuple[float, float], scroll_units: int) -> bool:
    if scroll_units < 0:
        return yview[0] > SCROLL_EDGE_EPSILON
    if scroll_units > 0:
        return yview[1] < 1.0 - SCROLL_EDGE_EPSILON
    return False


class WatchlistApp:
    def __init__(self, root: tk.Tk, *, auto_discovery: bool = True, check_updates: bool = True):
        self.root = root
        self.root.title("ani-watchlist")
        self.root.geometry("1120x760")
        self.root.minsize(780, 540)
        self.root.configure(bg=COLORS["bg"])
        self.conn = initialize()
        merge_safe_duplicates(self.conn)
        self.shutting_down = False
        self.auto_refresh_ms = WATCHLIST_AUTO_REFRESH_MS
        self.auto_refresh_job: str | None = None
        self.idle_prompt_after_ms = IDLE_PROMPT_AFTER_MS
        self.idle_close_grace_ms = IDLE_CLOSE_GRACE_MS
        self.idle_check_interval_ms = IDLE_CHECK_INTERVAL_MS
        self.last_user_activity_ms = monotonic_ms()
        self.idle_check_job: str | None = None
        self.idle_close_job: str | None = None
        self.idle_countdown_job: str | None = None
        self.idle_prompt: tk.Toplevel | None = None
        self.idle_prompt_countdown_label: tk.Label | None = None
        self.idle_prompt_deadline_ms: int | None = None
        self.auto_discovery_enabled = auto_discovery
        self.selected_status = tk.StringVar(value="watching")
        self.search_text = tk.StringVar()
        self.global_search_text = tk.StringVar()
        self.detail_status = tk.StringVar(value=STATUS_LABELS["watching"])
        self.popular_genre = tk.StringVar(value=POPULAR_GENRE_ALL_LABEL)
        self.show_alt_title = tk.BooleanVar(value=False)
        self.selected_anime_id: int | None = None
        self.detail_primary_title = ""
        self.detail_alt_title: str | None = None
        self.current_page = "library"
        self.images: dict[str, object] = {}
        self.current_rows = []
        self.discovery_data = load_discovery(self.conn, popular_genre=self.current_popular_genre())
        self.discovery_refreshing = False
        self.discovery_loading_more: set[str] = set()
        self.discovery_error: str | None = None
        self.update_checking = False
        self.search_loading = False
        self.search_results: list[dict[str, object]] = []
        self.search_result_query = ""
        self.search_error: str | None = None
        self.search_suggestions: list[dict[str, object]] = []
        self.search_suggestion_job: str | None = None
        self.search_suggestion_generation = 0
        self.search_generation = 0
        self.related_media_items: list[dict[str, object]] = []
        self.related_loading = False
        self.related_loaded = False
        self.related_error: str | None = None
        self.related_anilist_id: int | None = None
        self.related_columns = 1
        self.episode_availability_refreshing: set[int] = set()
        self.party_host_session = None
        self.party_client: WatchPartyRemoteClient | None = None
        self.party_join_polling = False
        self.party_playback_controller: MpvIpcController | None = None
        self.party_current_media: WatchPartyMedia | None = None
        self.party_window: tk.Toplevel | None = None
        self.party_link_var = tk.StringVar()
        self.party_username_var = tk.StringVar()
        self.party_status_text = tk.StringVar(value="")
        self.party_participant_list: tk.Listbox | None = None
        self.party_participant_ids: list[str] = []
        self.party_host_refresh_job: str | None = None
        self.party_host_state_sync_job: str | None = None
        self.card_widgets: dict[int, tk.Frame] = {}
        self.grid_columns = 1
        self.discovery_pages: dict[str, tk.Frame] = {}
        self.discovery_status_labels: dict[str, tk.Label] = {}
        self.discovery_page_labels: dict[str, tk.Label] = {}
        self.discovery_prev_buttons: dict[str, ttk.Button] = {}
        self.discovery_next_buttons: dict[str, ttk.Button] = {}
        self.discovery_canvases: dict[str, tk.Canvas] = {}
        self.discovery_frames: dict[str, tk.Frame] = {}
        self.discovery_windows: dict[str, int] = {}
        self.search_columns = 1
        self.popular_genre_box: ttk.Combobox | None = None
        self.discovery_columns = {name: 1 for name in DISCOVERY_MEDIA_PAGES}
        self.discovery_page_indexes = {name: 0 for name in DISCOVERY_MEDIA_PAGES}
        self._configure_style()
        self._build()
        self.root.protocol("WM_DELETE_WINDOW", self.close_app)
        self.install_idle_watchdog()
        self.show_trending()
        if auto_discovery:
            self.start_discovery_refresh(force=False)
        self.schedule_auto_refresh()
        if check_updates and os.environ.get("ANI_WATCHLIST_SKIP_UPDATE_CHECK") != "1":
            self.root.after(1200, self.start_update_check)

    def _configure_style(self) -> None:
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("Dark.TFrame", background=COLORS["bg"])
        style.configure("Panel.TFrame", background=COLORS["panel"])
        style.configure("Card.TFrame", background=COLORS["panel"], relief="flat")
        style.configure("Dark.TLabel", background=COLORS["bg"], foreground=COLORS["text"])
        style.configure("Panel.TLabel", background=COLORS["panel"], foreground=COLORS["text"])
        style.configure("Muted.TLabel", background=COLORS["panel"], foreground=COLORS["muted"])
        style.configure("Title.TLabel", background=COLORS["bg"], foreground=COLORS["text"], font=("", 18, "bold"))
        style.configure("DetailTitle.TLabel", background=COLORS["bg"], foreground=COLORS["text"], font=("", 20, "bold"))
        style.configure(
            "Dark.TButton",
            background=COLORS["panel_alt"],
            foreground=COLORS["text"],
            bordercolor=COLORS["border"],
            focusthickness=0,
            padding=(10, 6),
        )
        style.map("Dark.TButton", background=[("active", COLORS["border"])])
        style.configure(
            "Compact.Dark.TButton",
            background=COLORS["panel_alt"],
            foreground=COLORS["text"],
            bordercolor=COLORS["border"],
            focusthickness=0,
            padding=(6, 4),
        )
        style.map("Compact.Dark.TButton", background=[("active", COLORS["border"])])
        style.configure("Accent.TButton", background=COLORS["accent"], foreground="#111111", padding=(10, 6))
        style.map("Accent.TButton", background=[("active", COLORS["accent_hover"])])
        style.configure("Accent.TMenubutton", background=COLORS["accent"], foreground="#111111", padding=(10, 6))
        style.map("Accent.TMenubutton", background=[("active", COLORS["accent_hover"])])
        style.configure(
            "Dark.TMenubutton",
            background=COLORS["panel_alt"],
            foreground=COLORS["text"],
            bordercolor=COLORS["border"],
            focusthickness=0,
            padding=(10, 6),
        )
        style.map("Dark.TMenubutton", background=[("active", COLORS["border"])])
        style.configure(
            "Dark.TCombobox",
            fieldbackground=COLORS["entry"],
            background=COLORS["panel_alt"],
            foreground=COLORS["text"],
            arrowcolor=COLORS["text"],
            selectbackground=COLORS["entry"],
            selectforeground=COLORS["text"],
            bordercolor=COLORS["border"],
            lightcolor=COLORS["border"],
            darkcolor=COLORS["border"],
        )
        style.map(
            "Dark.TCombobox",
            fieldbackground=[("readonly", COLORS["entry"])],
            foreground=[("readonly", COLORS["text"])],
            selectbackground=[("readonly", COLORS["entry"])],
            selectforeground=[("readonly", COLORS["text"])],
            background=[("readonly", COLORS["panel_alt"]), ("active", COLORS["border"])],
        )
        self.root.option_add("*TCombobox*Listbox.background", COLORS["panel_alt"])
        self.root.option_add("*TCombobox*Listbox.foreground", COLORS["text"])
        self.root.option_add("*TCombobox*Listbox.selectBackground", COLORS["accent"])
        self.root.option_add("*TCombobox*Listbox.selectForeground", "#111111")
        style.configure(
            "Dark.Treeview",
            background=COLORS["panel"],
            foreground=COLORS["text"],
            fieldbackground=COLORS["panel"],
            bordercolor=COLORS["border"],
            rowheight=26,
        )
        style.configure(
            "Dark.Treeview.Heading",
            background=COLORS["panel_alt"],
            foreground=COLORS["text"],
            bordercolor=COLORS["border"],
        )
        style.map("Dark.Treeview", background=[("selected", COLORS["accent"])], foreground=[("selected", "#111111")])

    def current_popular_genre(self) -> str | None:
        return popular_filter_label(self.popular_genre.get())

    def reload_discovery_data(self) -> None:
        self.discovery_data = load_discovery(self.conn, popular_genre=self.current_popular_genre())

    def _build(self) -> None:
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(1, weight=1)
        self.nav_bar = tk.Frame(self.root, bg=COLORS["bg"])
        self.nav_bar.grid(row=0, column=0, sticky="ew", padx=18, pady=(14, 0))
        self.nav_bar.columnconfigure(5, weight=1)
        self.nav_bar.rowconfigure(1, weight=0)
        self.nav_buttons: dict[str, tk.Button] = {}
        for idx, (page, label, command) in enumerate(
            [
                ("trending", "Trending", self.show_trending),
                ("top_airing", "Top Airing", self.show_top_airing),
                ("popular", "Most Popular", self.show_popular),
                ("schedule", "Schedule", self.show_schedule),
                ("library", "Watchlist", self.show_library),
            ]
        ):
            button = tk.Button(
                self.nav_bar,
                text=label,
                command=command,
                bg=COLORS["panel_alt"],
                fg=COLORS["text"],
                activebackground=COLORS["accent"],
                activeforeground="#111111",
                relief="flat",
                padx=16,
                pady=8,
                cursor="hand2",
            )
            button.grid(row=0, column=idx, padx=(0, 8), sticky="w")
            self.nav_buttons[page] = button
        self._build_global_search()
        self.container = tk.Frame(self.root, bg=COLORS["bg"])
        self.container.grid(row=1, column=0, sticky="nsew")
        self.container.columnconfigure(0, weight=1)
        self.container.rowconfigure(0, weight=1)
        self.library_page = tk.Frame(self.container, bg=COLORS["bg"])
        for page_name in DISCOVERY_MEDIA_PAGES:
            self.discovery_pages[page_name] = tk.Frame(self.container, bg=COLORS["bg"])
        self.trending_page = self.discovery_pages["trending"]
        self.top_airing_page = self.discovery_pages["top_airing"]
        self.popular_page = self.discovery_pages["popular"]
        self.search_page = tk.Frame(self.container, bg=COLORS["bg"])
        self.schedule_page = tk.Frame(self.container, bg=COLORS["bg"])
        self.detail_page = tk.Frame(self.container, bg=COLORS["bg"])
        self._build_library_page()
        for page_name in DISCOVERY_MEDIA_PAGES:
            self._build_discovery_page(page_name)
        self._build_search_page()
        self._build_schedule_page()
        self._build_detail_page()
        self._bind_mousewheel()

    def _build_global_search(self) -> None:
        self.global_search_box = tk.Frame(self.nav_bar, bg=COLORS["bg"])
        self.global_search_box.grid(row=0, column=5, sticky="e")
        self.global_search_entry = tk.Entry(
            self.global_search_box,
            textvariable=self.global_search_text,
            width=34,
            bg=COLORS["entry"],
            fg=COLORS["text"],
            insertbackground=COLORS["text"],
            relief="flat",
            highlightthickness=1,
            highlightbackground=COLORS["border"],
            highlightcolor=COLORS["accent"],
        )
        self.global_search_entry.grid(row=0, column=0, sticky="ew", ipady=8)
        self.global_search_entry.bind("<KeyRelease>", self.on_global_search_key)
        self.global_search_entry.bind("<Return>", lambda _event: self.submit_global_search())
        self.global_search_entry.bind("<Down>", self.focus_search_dropdown)
        self.global_search_dropdown = tk.Listbox(
            self.nav_bar,
            height=SEARCH_SUGGESTION_LIMIT,
            width=42,
            bg=COLORS["panel_alt"],
            fg=COLORS["text"],
            selectbackground=COLORS["accent"],
            selectforeground="#111111",
            relief="flat",
            highlightthickness=1,
            highlightbackground=COLORS["border"],
            activestyle="none",
        )
        self.global_search_dropdown.grid(row=1, column=5, sticky="e", pady=(2, 0))
        self.global_search_dropdown.grid_remove()
        self.global_search_dropdown.bind("<Double-1>", lambda _event: self.choose_search_suggestion())
        self.global_search_dropdown.bind("<Return>", lambda _event: self.choose_search_suggestion())
        self.global_search_dropdown.bind("<Escape>", lambda _event: self.hide_search_dropdown())

    def _bind_mousewheel(self) -> None:
        for sequence in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            self.root.bind_all(sequence, self._on_mousewheel, add="+")

    def _set_active_nav(self, page_name: str) -> None:
        for page, button in self.nav_buttons.items():
            active = page == page_name or (page == "library" and page_name == "detail")
            button.configure(
                bg=COLORS["accent"] if active else COLORS["panel_alt"],
                fg="#111111" if active else COLORS["text"],
            )

    def _hide_pages(self) -> None:
        for page in (self.library_page, *self.discovery_pages.values(), self.search_page, self.schedule_page, self.detail_page):
            page.grid_forget()

    def _build_library_page(self) -> None:
        page = self.library_page
        page.columnconfigure(0, weight=1)
        page.rowconfigure(3, weight=1)

        header = tk.Frame(page, bg=COLORS["bg"])
        header.grid(row=0, column=0, sticky="ew", padx=18, pady=(16, 8))
        header.columnconfigure(0, weight=1)
        ttk.Label(header, text="Watchlist", style="Title.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Button(header, text="Add", style="Accent.TButton", command=self.add_anime).grid(row=0, column=1, padx=(8, 0))
        ttk.Button(header, text="Refresh", style="Dark.TButton", command=self.refresh_library).grid(row=0, column=2, padx=(8, 0))

        tabs = tk.Frame(page, bg=COLORS["bg"])
        tabs.grid(row=1, column=0, sticky="ew", padx=18, pady=(0, 8))
        self.status_buttons: dict[str, tk.Button] = {}
        for idx, status in enumerate(STATUSES):
            button = tk.Button(
                tabs,
                text=STATUS_LABELS[status],
                command=lambda value=status: self.set_status_filter(value),
                bg=COLORS["panel_alt"],
                fg=COLORS["text"],
                activebackground=COLORS["accent"],
                activeforeground="#111111",
                relief="flat",
                padx=14,
                pady=8,
                cursor="hand2",
            )
            button.grid(row=0, column=idx, padx=(0, 8), sticky="w")
            self.status_buttons[status] = button

        toolbar = tk.Frame(page, bg=COLORS["bg"])
        toolbar.grid(row=2, column=0, sticky="ew", padx=18, pady=(0, 10))
        toolbar.columnconfigure(0, weight=1)
        self.search_entry = tk.Entry(
            toolbar,
            textvariable=self.search_text,
            bg=COLORS["entry"],
            fg=COLORS["text"],
            insertbackground=COLORS["text"],
            relief="flat",
            highlightthickness=1,
            highlightbackground=COLORS["border"],
            highlightcolor=COLORS["accent"],
        )
        self.search_entry.grid(row=0, column=0, sticky="ew", ipady=8)
        self.search_entry.bind("<KeyRelease>", lambda _event: self.refresh_library())
        self.dashboard_label = tk.Label(toolbar, text="", bg=COLORS["bg"], fg=COLORS["muted"], justify="right")
        self.dashboard_label.grid(row=0, column=1, padx=(14, 0), sticky="e")

        body = tk.Frame(page, bg=COLORS["bg"])
        body.grid(row=3, column=0, sticky="nsew", padx=18, pady=(0, 16))
        body.columnconfigure(0, weight=1)
        body.rowconfigure(0, weight=1)
        self.grid_canvas = tk.Canvas(body, bg=COLORS["bg"], highlightthickness=0)
        self.grid_canvas.grid(row=0, column=0, sticky="nsew")
        self.grid_scrollbar = ttk.Scrollbar(body, orient="vertical", command=self.grid_canvas.yview)
        self.grid_scrollbar.grid(row=0, column=1, sticky="ns")
        self.grid_canvas.configure(yscrollcommand=self.grid_scrollbar.set)
        self.grid_frame = tk.Frame(self.grid_canvas, bg=COLORS["bg"])
        self.grid_window = self.grid_canvas.create_window((0, 0), window=self.grid_frame, anchor="nw")
        self.grid_frame.bind("<Configure>", self._update_grid_scroll_region)
        self.grid_canvas.bind("<Configure>", self._on_grid_resize)

    def _build_discovery_page(self, page_name: str) -> None:
        page = self.discovery_pages[page_name]
        page_config = DISCOVERY_MEDIA_PAGES[page_name]
        page.columnconfigure(0, weight=1)
        page.rowconfigure(2, weight=1)

        header = tk.Frame(page, bg=COLORS["bg"])
        header.grid(row=0, column=0, sticky="ew", padx=18, pady=(16, 8))
        header.columnconfigure(0, weight=1)
        ttk.Label(header, text=page_config["title"], style="Title.TLabel").grid(row=0, column=0, sticky="w")
        controls = tk.Frame(header, bg=COLORS["bg"])
        controls.grid(row=0, column=1, sticky="e")
        control_column = 0
        if page_name == "popular":
            genre_box = ttk.Combobox(
                controls,
                textvariable=self.popular_genre,
                values=(POPULAR_GENRE_ALL_LABEL, *POPULAR_FILTERS),
                state="readonly",
                width=18,
                style="Dark.TCombobox",
            )
            genre_box.grid(row=0, column=control_column, padx=(8, 0))
            genre_box.bind("<<ComboboxSelected>>", lambda _event: self.on_popular_genre_changed())
            self.popular_genre_box = genre_box
            control_column += 1
        prev_button = ttk.Button(
            controls,
            text="Prev",
            width=6,
            style="Dark.TButton",
            command=lambda value=page_name: self.change_discovery_page(value, -1),
        )
        prev_button.grid(row=0, column=control_column, padx=(8, 0))
        control_column += 1
        page_label = tk.Label(controls, text="", bg=COLORS["bg"], fg=COLORS["muted"], width=10, anchor="center")
        page_label.grid(row=0, column=control_column, padx=(8, 0))
        control_column += 1
        next_button = ttk.Button(
            controls,
            text="Next",
            width=6,
            style="Dark.TButton",
            command=lambda value=page_name: self.change_discovery_page(value, 1),
        )
        next_button.grid(row=0, column=control_column, padx=(8, 0))
        ttk.Button(header, text="Refresh", style="Dark.TButton", command=lambda value=page_name: self.start_discovery_refresh(force=True, page_name=value)).grid(
            row=0, column=2, padx=(8, 0), sticky="e"
        )
        self.discovery_prev_buttons[page_name] = prev_button
        self.discovery_page_labels[page_name] = page_label
        self.discovery_next_buttons[page_name] = next_button
        status_label = tk.Label(header, text="", bg=COLORS["bg"], fg=COLORS["muted"], anchor="e")
        status_label.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(4, 0))
        self.discovery_status_labels[page_name] = status_label

        body = tk.Frame(page, bg=COLORS["bg"])
        body.grid(row=2, column=0, sticky="nsew", padx=18, pady=(0, 16))
        body.columnconfigure(0, weight=1)
        body.rowconfigure(0, weight=1)
        canvas = tk.Canvas(body, bg=COLORS["bg"], highlightthickness=0)
        canvas.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(body, orient="vertical", command=canvas.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        canvas.configure(yscrollcommand=scrollbar.set)
        frame = tk.Frame(canvas, bg=COLORS["bg"])
        window = canvas.create_window((0, 0), window=frame, anchor="nw")
        frame.bind("<Configure>", lambda _event, value=page_name: self._update_discovery_scroll_region(value))
        canvas.bind("<Configure>", lambda event, value=page_name: self._on_discovery_resize(value, event))
        self.discovery_canvases[page_name] = canvas
        self.discovery_frames[page_name] = frame
        self.discovery_windows[page_name] = int(window)

    def _build_search_page(self) -> None:
        page = self.search_page
        page.columnconfigure(0, weight=1)
        page.rowconfigure(1, weight=1)

        header = tk.Frame(page, bg=COLORS["bg"])
        header.grid(row=0, column=0, sticky="ew", padx=18, pady=(16, 8))
        header.columnconfigure(0, weight=1)
        self.search_title_label = ttk.Label(header, text="Search Results", style="Title.TLabel")
        self.search_title_label.grid(row=0, column=0, sticky="w")
        self.search_status_label = tk.Label(header, text="", bg=COLORS["bg"], fg=COLORS["muted"], anchor="e")
        self.search_status_label.grid(row=1, column=0, sticky="ew", pady=(4, 0))

        body = tk.Frame(page, bg=COLORS["bg"])
        body.grid(row=1, column=0, sticky="nsew", padx=18, pady=(0, 16))
        body.columnconfigure(0, weight=1)
        body.rowconfigure(0, weight=1)
        self.search_canvas = tk.Canvas(body, bg=COLORS["bg"], highlightthickness=0)
        self.search_canvas.grid(row=0, column=0, sticky="nsew")
        self.search_scrollbar = ttk.Scrollbar(body, orient="vertical", command=self.search_canvas.yview)
        self.search_scrollbar.grid(row=0, column=1, sticky="ns")
        self.search_canvas.configure(yscrollcommand=self.search_scrollbar.set)
        self.search_frame = tk.Frame(self.search_canvas, bg=COLORS["bg"])
        self.search_window = self.search_canvas.create_window((0, 0), window=self.search_frame, anchor="nw")
        self.search_frame.bind("<Configure>", self._update_search_scroll_region)
        self.search_canvas.bind("<Configure>", self._on_search_resize)

    def _build_schedule_page(self) -> None:
        page = self.schedule_page
        page.columnconfigure(0, weight=1)
        page.rowconfigure(2, weight=1)

        header = tk.Frame(page, bg=COLORS["bg"])
        header.grid(row=0, column=0, sticky="ew", padx=18, pady=(16, 8))
        header.columnconfigure(0, weight=1)
        ttk.Label(header, text="Release Schedule", style="Title.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Button(header, text="Refresh", style="Dark.TButton", command=lambda: self.start_discovery_refresh(force=True)).grid(
            row=0, column=1, padx=(8, 0), sticky="e"
        )
        self.schedule_status_label = tk.Label(header, text="", bg=COLORS["bg"], fg=COLORS["muted"], anchor="e")
        self.schedule_status_label.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(4, 0))

        body = tk.Frame(page, bg=COLORS["bg"])
        body.grid(row=2, column=0, sticky="nsew", padx=18, pady=(0, 16))
        body.columnconfigure(0, weight=1)
        body.rowconfigure(0, weight=1)
        self.schedule_canvas = tk.Canvas(body, bg=COLORS["bg"], highlightthickness=0)
        self.schedule_canvas.grid(row=0, column=0, sticky="nsew")
        self.schedule_scrollbar = ttk.Scrollbar(body, orient="vertical", command=self.schedule_canvas.yview)
        self.schedule_scrollbar.grid(row=0, column=1, sticky="ns")
        self.schedule_canvas.configure(yscrollcommand=self.schedule_scrollbar.set)
        self.schedule_frame = tk.Frame(self.schedule_canvas, bg=COLORS["bg"])
        self.schedule_window = self.schedule_canvas.create_window((0, 0), window=self.schedule_frame, anchor="nw")
        self.schedule_frame.bind("<Configure>", self._update_schedule_scroll_region)
        self.schedule_canvas.bind("<Configure>", self._on_schedule_resize)

    def _build_detail_page(self) -> None:
        outer = self.detail_page
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(0, weight=1)
        self.detail_canvas = tk.Canvas(outer, bg=COLORS["bg"], highlightthickness=0)
        self.detail_canvas.grid(row=0, column=0, sticky="nsew")
        self.detail_scrollbar = ttk.Scrollbar(outer, orient="vertical", command=self.detail_canvas.yview)
        self.detail_scrollbar.grid(row=0, column=1, sticky="ns")
        self.detail_canvas.configure(yscrollcommand=self.detail_scrollbar.set)
        self.detail_frame = tk.Frame(self.detail_canvas, bg=COLORS["bg"])
        self.detail_window = self.detail_canvas.create_window((0, 0), window=self.detail_frame, anchor="nw")
        self.detail_frame.bind("<Configure>", self._update_detail_scroll_region)
        self.detail_canvas.bind("<Configure>", self._on_detail_resize)

        page = self.detail_frame
        page.columnconfigure(0, weight=1)
        page.rowconfigure(2, weight=1)

        top = tk.Frame(page, bg=COLORS["bg"])
        top.grid(row=0, column=0, sticky="ew", padx=18, pady=(16, 10))
        top.columnconfigure(1, weight=1)
        ttk.Button(top, text="Back", style="Dark.TButton", command=self.show_library).grid(row=0, column=0, sticky="w")
        title_box = tk.Frame(top, bg=COLORS["bg"])
        title_box.grid(row=0, column=1, sticky="ew", padx=12)
        title_box.columnconfigure(0, weight=1)
        self.detail_title_label = ttk.Label(title_box, text="", style="DetailTitle.TLabel", wraplength=760)
        self.detail_title_label.grid(row=0, column=0, sticky="w")
        self.detail_alt_title_label = tk.Label(title_box, text="", bg=COLORS["bg"], fg=COLORS["muted"], anchor="w")
        self.detail_alt_title_label.grid(row=1, column=0, sticky="w", pady=(2, 0))
        self.detail_alt_title_label.grid_remove()
        self.alt_title_toggle = tk.Checkbutton(
            top,
            text="Japanese title",
            variable=self.show_alt_title,
            command=self.update_detail_title_labels,
            bg=COLORS["bg"],
            fg=COLORS["text"],
            activebackground=COLORS["bg"],
            activeforeground=COLORS["text"],
            selectcolor=COLORS["entry"],
            disabledforeground=COLORS["border"],
            relief="flat",
            cursor="hand2",
        )
        self.alt_title_toggle.grid(row=0, column=2, padx=(8, 0), sticky="e")
        self.alt_title_toggle.grid_remove()
        ttk.Button(top, text="Refresh", style="Dark.TButton", command=self.load_detail).grid(row=0, column=3, padx=(8, 0))

        summary = tk.Frame(page, bg=COLORS["bg"])
        summary.grid(row=1, column=0, sticky="ew", padx=18, pady=(0, 12))
        summary.columnconfigure(1, weight=1)

        self.detail_cover_label = tk.Label(summary, bg=COLORS["bg"], width=DETAIL_COVER_W, height=DETAIL_COVER_H)
        self.detail_cover_label.grid(row=0, column=0, rowspan=3, sticky="nw", padx=(0, 16))

        info = tk.Frame(summary, bg=COLORS["panel"], highlightthickness=1, highlightbackground=COLORS["border"])
        info.grid(row=0, column=1, sticky="nsew")
        info.columnconfigure(1, weight=1)
        self.progress_label = tk.Label(info, text="", bg=COLORS["panel"], fg=COLORS["text"], anchor="w")
        self.progress_label.grid(row=0, column=0, columnspan=2, sticky="ew", padx=12, pady=(12, 4))
        self.last_label = tk.Label(info, text="", bg=COLORS["panel"], fg=COLORS["muted"], anchor="w")
        self.last_label.grid(row=1, column=0, columnspan=2, sticky="ew", padx=12, pady=4)
        tk.Label(info, text="Status", bg=COLORS["panel"], fg=COLORS["muted"]).grid(row=2, column=0, sticky="w", padx=12, pady=4)
        self.status_box = ttk.Combobox(
            info,
            values=[STATUS_LABELS[status] for status in STATUSES],
            textvariable=self.detail_status,
            state="readonly",
            style="Dark.TCombobox",
        )
        self.status_box.grid(row=2, column=1, sticky="ew", padx=12, pady=4)
        self.status_box.bind("<<ComboboxSelected>>", lambda _event: self.save_status())
        self.anilist_label = tk.Label(info, text="", bg=COLORS["panel"], fg=COLORS["muted"], anchor="w")
        self.anilist_label.grid(row=3, column=0, columnspan=2, sticky="ew", padx=12, pady=(4, 12))
        self.launch_label = tk.Label(info, text="", bg=COLORS["panel"], fg=COLORS["muted"], anchor="w")
        self.launch_label.grid(row=4, column=0, columnspan=2, sticky="ew", padx=12, pady=(0, 12))

        actions = tk.Frame(summary, bg=COLORS["bg"])
        actions.grid(row=1, column=1, sticky="ew", pady=(10, 0))
        continue_button = ttk.Menubutton(actions, text="Continue", style="Accent.TMenubutton")
        continue_menu = tk.Menu(
            continue_button,
            tearoff=False,
            bg=COLORS["panel_alt"],
            fg=COLORS["text"],
            activebackground=COLORS["accent"],
            activeforeground="#111111",
            relief="flat",
        )
        continue_menu.add_command(label="Sub", command=lambda: self.continue_selected_episode("sub"))
        continue_menu.add_command(label="Dub", command=lambda: self.continue_selected_episode("dub"))
        continue_button.configure(menu=continue_menu)
        continue_button.grid(row=0, column=0, padx=(0, 8), pady=4, sticky="w")
        party_button = ttk.Menubutton(actions, text="Watch Party", style="Dark.TMenubutton")
        party_menu = tk.Menu(
            party_button,
            tearoff=False,
            bg=COLORS["panel_alt"],
            fg=COLORS["text"],
            activebackground=COLORS["accent"],
            activeforeground="#111111",
            relief="flat",
        )
        party_menu.add_command(label="Host Watch Party", command=self.host_watch_party)
        party_menu.add_command(label="Join Watch Party", command=self.join_watch_party)
        party_button.configure(menu=party_menu)
        party_button.grid(row=0, column=1, padx=(0, 8), pady=4, sticky="w")
        for idx, (text, command, style) in enumerate(
            [
                ("Mark Watched", lambda: self.mark_selected_episode(True), "Accent.TButton"),
                ("Mark Unwatched", lambda: self.mark_selected_episode(False), "Dark.TButton"),
                ("Add Episode", self.add_episode, "Dark.TButton"),
                ("Refresh Metadata", self.refresh_metadata, "Dark.TButton"),
                ("Choose Match", self.choose_match, "Dark.TButton"),
                ("Edit Title", self.edit_title, "Dark.TButton"),
                ("Delete", self.delete_selected, "Dark.TButton"),
            ],
            start=2,
        ):
            ttk.Button(actions, text=text, style=style, command=command).grid(
                row=idx // 4,
                column=idx % 4,
                padx=(0, 8),
                pady=4,
                sticky="w",
            )

        notes_box = tk.Frame(summary, bg=COLORS["bg"])
        notes_box.grid(row=2, column=1, sticky="ew", pady=(8, 0))
        notes_box.columnconfigure(0, weight=1)
        tk.Label(notes_box, text="Notes", bg=COLORS["bg"], fg=COLORS["muted"]).grid(row=0, column=0, sticky="w")
        ttk.Button(notes_box, text="Save Notes", style="Dark.TButton", command=self.save_notes).grid(row=0, column=1, sticky="e")
        self.notes = tk.Text(
            notes_box,
            height=4,
            wrap="word",
            bg=COLORS["entry"],
            fg=COLORS["text"],
            insertbackground=COLORS["text"],
            relief="flat",
            highlightthickness=1,
            highlightbackground=COLORS["border"],
            highlightcolor=COLORS["accent"],
        )
        self.notes.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(4, 0))

        bottom = tk.Frame(page, bg=COLORS["bg"])
        bottom.grid(row=2, column=0, sticky="nsew", padx=18, pady=(0, 16))
        bottom.columnconfigure(0, weight=3)
        bottom.columnconfigure(1, weight=1)
        bottom.rowconfigure(0, weight=1)

        ep_panel = tk.Frame(bottom, bg=COLORS["bg"])
        ep_panel.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
        ep_panel.columnconfigure(0, weight=1)
        ep_panel.rowconfigure(1, weight=1)
        tk.Label(ep_panel, text="Episodes", bg=COLORS["bg"], fg=COLORS["text"], font=("", 12, "bold")).grid(row=0, column=0, sticky="w")
        self.episode_tree = ttk.Treeview(
            ep_panel,
            columns=("watched", "episode", "started", "watched_at"),
            show="headings",
            style="Dark.Treeview",
            selectmode="browse",
        )
        for column, text, width in (
            ("watched", "Watched", 80),
            ("episode", "Episode", 90),
            ("started", "Started", 170),
            ("watched_at", "Watched At", 170),
        ):
            self.episode_tree.heading(column, text=text)
            self.episode_tree.column(column, width=width, minwidth=70, stretch=column in {"started", "watched_at"})
        self.episode_tree.grid(row=1, column=0, sticky="nsew", pady=(6, 0))
        ep_scroll = ttk.Scrollbar(ep_panel, orient="vertical", command=self.episode_tree.yview)
        ep_scroll.grid(row=1, column=1, sticky="ns", pady=(6, 0))
        self.episode_tree.configure(yscrollcommand=ep_scroll.set)
        self.episode_tree.bind("<Double-1>", self.toggle_selected_episode)

        activity_panel = tk.Frame(bottom, bg=COLORS["panel"], highlightthickness=1, highlightbackground=COLORS["border"])
        activity_panel.grid(row=0, column=1, sticky="nsew")
        activity_panel.columnconfigure(0, weight=1)
        activity_panel.rowconfigure(1, weight=1)
        tk.Label(activity_panel, text="Recent Activity", bg=COLORS["panel"], fg=COLORS["text"], font=("", 12, "bold")).grid(
            row=0, column=0, sticky="w", padx=10, pady=(10, 4)
        )
        self.activity_list = tk.Listbox(
            activity_panel,
            height=8,
            bg=COLORS["panel"],
            fg=COLORS["muted"],
            selectbackground=COLORS["accent"],
            selectforeground="#111111",
            relief="flat",
            highlightthickness=0,
        )
        self.activity_list.grid(row=1, column=0, sticky="nsew", padx=10, pady=(0, 10))

        related = tk.Frame(page, bg=COLORS["bg"])
        related.grid(row=3, column=0, sticky="ew", padx=18, pady=(0, 18))
        related.columnconfigure(0, weight=1)
        self.related_title_label = tk.Label(related, text="Related Seasons", bg=COLORS["bg"], fg=COLORS["text"], font=("", 12, "bold"))
        self.related_title_label.grid(row=0, column=0, sticky="w")
        self.related_status_label = tk.Label(related, text="", bg=COLORS["bg"], fg=COLORS["muted"], anchor="w")
        self.related_status_label.grid(row=1, column=0, sticky="ew", pady=(2, 0))
        self.related_frame = tk.Frame(related, bg=COLORS["bg"])
        self.related_frame.grid(row=2, column=0, sticky="ew", pady=(6, 0))
        self.related_frame.columnconfigure(0, weight=1)

    def _placeholder_image(self, size: tuple[int, int], title: str) -> object | None:
        if Image is None or ImageTk is None or ImageDraw is None:
            return None
        key = f"placeholder:{size[0]}x{size[1]}:{title[:24]}"
        if key in self.images:
            return self.images[key]
        image = Image.new("RGB", size, COLORS["panel_alt"])
        draw = ImageDraw.Draw(image)
        draw.rectangle((2, 2, size[0] - 3, size[1] - 3), outline=COLORS["border"], width=2)
        draw.text((12, size[1] // 2 - 8), "No Cover", fill=COLORS["muted"])
        photo = ImageTk.PhotoImage(image)
        self.images[key] = photo
        return photo

    def _image_for(self, anime_id: int, cover_path: str | None, size: tuple[int, int], title: str = "") -> object | None:
        if Image is None or ImageTk is None:
            return None
        key = f"{anime_id}:{size[0]}x{size[1]}:{cover_path or 'none'}"
        if key in self.images:
            return self.images[key]
        if not cover_path:
            return self._placeholder_image(size, title)
        try:
            image = Image.open(cover_path).convert("RGB")
            image.thumbnail(size, Image.Resampling.LANCZOS)
            canvas = Image.new("RGB", size, COLORS["panel_alt"])
            left = (size[0] - image.width) // 2
            top = (size[1] - image.height) // 2
            canvas.paste(image, (left, top))
            photo = ImageTk.PhotoImage(canvas)
            self.images[key] = photo
            return photo
        except Exception:
            return self._placeholder_image(size, title)

    def set_status_filter(self, status: str) -> None:
        self.selected_status.set(status)
        self.refresh_library()

    def _set_active_tab_styles(self) -> None:
        for status, button in self.status_buttons.items():
            active = status == self.selected_status.get()
            button.configure(
                bg=COLORS["accent"] if active else COLORS["panel_alt"],
                fg="#111111" if active else COLORS["text"],
            )

    def show_library(self) -> None:
        self.current_page = "library"
        self._set_active_nav("library")
        self._hide_pages()
        self.library_page.grid(row=0, column=0, sticky="nsew")
        self.refresh_library()

    def show_discovery_list(self, page_name: str) -> None:
        self.current_page = page_name
        self._set_active_nav(page_name)
        self._hide_pages()
        self.discovery_pages[page_name].grid(row=0, column=0, sticky="nsew")
        self.render_discovery_list(page_name)

    def show_trending(self) -> None:
        self.show_discovery_list("trending")

    def show_top_airing(self) -> None:
        self.show_discovery_list("top_airing")

    def show_popular(self) -> None:
        self.show_discovery_list("popular")

    def on_popular_genre_changed(self) -> None:
        self.discovery_page_indexes["popular"] = 0
        self.reload_discovery_data()
        if self.current_page == "popular":
            self.render_discovery_list("popular")
        popular = self.discovery_data.get("popular") or {}
        if not popular.get("items"):
            self.start_discovery_refresh(force=True, page_name="popular")

    def change_discovery_page(self, page_name: str, direction: int) -> None:
        items = list(((self.discovery_data.get(page_name) or {}).get("items")) or [])
        data = self.discovery_data.get(page_name) or {}
        page_count = discovery_page_count(len(items))
        current = max(0, min(self.discovery_page_indexes.get(page_name, 0), page_count - 1))
        requested_page = current + direction
        if requested_page >= page_count and direction > 0 and data.get("has_more"):
            self.load_more_discovery_page(page_name, requested_page)
            return
        next_page = max(0, min(requested_page, page_count - 1))
        if next_page == current:
            self.update_discovery_page_controls(page_name, len(items))
            return
        self.discovery_page_indexes[page_name] = next_page
        self.render_discovery_list(page_name)
        self.discovery_canvases[page_name].yview_moveto(0)

    def load_more_discovery_page(self, page_name: str, target_page_index: int) -> None:
        if self.discovery_refreshing or page_name in self.discovery_loading_more:
            return
        self.discovery_loading_more.add(page_name)
        self.update_discovery_page_controls(
            page_name,
            len(list(((self.discovery_data.get(page_name) or {}).get("items")) or [])),
        )
        label = self.discovery_status_labels.get(page_name)
        if label is not None:
            label.configure(text="Loading more AniList results...", fg=COLORS["muted"])
        popular_genre = self.current_popular_genre() if page_name == "popular" else None

        def worker() -> None:
            error = None
            try:
                with initialize() as conn:
                    append_discovery_media_page(conn, page_name, load_config(), genre=popular_genre)
            except Exception as exc:  # pragma: no cover - defensive UI boundary
                error = str(exc)
            self.run_on_ui(lambda: self.finish_discovery_load_more(page_name, target_page_index, error))

        threading.Thread(target=worker, daemon=True).start()

    def finish_discovery_load_more(
        self,
        page_name: str,
        target_page_index: int,
        error: str | None = None,
    ) -> None:
        self.discovery_loading_more.discard(page_name)
        self.discovery_error = error
        self.reload_discovery_data()
        items = list(((self.discovery_data.get(page_name) or {}).get("items")) or [])
        page_count = discovery_page_count(len(items))
        self.discovery_page_indexes[page_name] = max(0, min(target_page_index, page_count - 1))
        self.update_discovery_status()
        if self.current_page == page_name:
            self.render_discovery_list(page_name)
            self.discovery_canvases[page_name].yview_moveto(0)

    def show_schedule(self) -> None:
        self.current_page = "schedule"
        self._set_active_nav("schedule")
        self._hide_pages()
        self.schedule_page.grid(row=0, column=0, sticky="nsew")
        self.render_schedule()

    def open_detail(self, anime_id: int) -> None:
        self.selected_anime_id = anime_id
        self.show_alt_title.set(False)
        self.related_media_items = []
        self.related_error = None
        self.related_anilist_id = None
        self.related_loading = False
        self.related_loaded = False
        if hasattr(self, "launch_label"):
            self.launch_label.configure(text="", fg=COLORS["muted"])
        self.current_page = "detail"
        self._set_active_nav("detail")
        self._hide_pages()
        self.detail_page.grid(row=0, column=0, sticky="nsew")
        if hasattr(self, "detail_canvas"):
            self.detail_canvas.yview_moveto(0)
        self.load_detail()
        self.start_episode_availability_refresh(anime_id)

    def run_on_ui(self, callback) -> None:
        if self.shutting_down:
            return
        try:
            self.root.after(0, callback)
        except (RuntimeError, tk.TclError):
            pass

    def install_idle_watchdog(self) -> None:
        for sequence in IDLE_ACTIVITY_EVENTS:
            self.root.bind_all(sequence, self.record_user_activity, add="+")
        self.schedule_idle_check()

    def record_user_activity(self, _event=None) -> None:
        if self.shutting_down or self.idle_prompt is not None:
            return
        now = monotonic_ms()
        if now - self.last_user_activity_ms < IDLE_ACTIVITY_THROTTLE_MS:
            return
        self.last_user_activity_ms = now
        self.schedule_idle_check()

    def schedule_idle_check(self) -> None:
        if self.shutting_down:
            return
        if self.idle_check_job is not None:
            try:
                self.root.after_cancel(self.idle_check_job)
            except tk.TclError:
                pass
        now = monotonic_ms()
        remaining = self.idle_prompt_after_ms - (now - self.last_user_activity_ms)
        delay = max(1000, min(self.idle_check_interval_ms, max(1000, remaining)))
        self.idle_check_job = self.root.after(delay, self.check_idle_timeout)

    def check_idle_timeout(self) -> None:
        self.idle_check_job = None
        if self.shutting_down or self.idle_prompt is not None:
            return
        now = monotonic_ms()
        if idle_prompt_due(self.last_user_activity_ms, now, prompt_after_ms=self.idle_prompt_after_ms):
            self.show_idle_prompt()
            return
        self.schedule_idle_check()

    def show_idle_prompt(self) -> None:
        if self.shutting_down or self.idle_prompt is not None:
            return
        win = tk.Toplevel(self.root)
        win.title("Still watching?")
        win.configure(bg=COLORS["panel"])
        win.resizable(False, False)
        win.transient(self.root)
        win.protocol("WM_DELETE_WINDOW", self.acknowledge_idle_prompt)
        self.idle_prompt = win

        frame = tk.Frame(win, bg=COLORS["panel"], padx=20, pady=18)
        frame.grid(row=0, column=0, sticky="nsew")
        tk.Label(
            frame,
            text="Still watching?",
            bg=COLORS["panel"],
            fg=COLORS["text"],
            font=("", 16, "bold"),
        ).grid(row=0, column=0, columnspan=2, sticky="w")
        tk.Label(
            frame,
            text="The watchlist has been idle for a while.",
            bg=COLORS["panel"],
            fg=COLORS["muted"],
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(6, 0))
        self.idle_prompt_countdown_label = tk.Label(frame, text="", bg=COLORS["panel"], fg=COLORS["muted"])
        self.idle_prompt_countdown_label.grid(row=2, column=0, columnspan=2, sticky="w", pady=(2, 14))
        ttk.Button(frame, text="Continue", style="Accent.TButton", command=self.acknowledge_idle_prompt).grid(
            row=3, column=0, sticky="ew", padx=(0, 8)
        )
        ttk.Button(frame, text="Close Now", style="Dark.TButton", command=self.close_app).grid(row=3, column=1, sticky="ew")
        frame.columnconfigure(0, weight=1)
        frame.columnconfigure(1, weight=1)
        win.bind("<Return>", lambda _event: self.acknowledge_idle_prompt())
        win.bind("<Escape>", lambda _event: self.close_app())
        win.update_idletasks()
        self.center_idle_prompt(win)
        try:
            win.grab_set()
            win.focus_force()
        except tk.TclError:
            pass
        self.idle_prompt_deadline_ms = monotonic_ms() + self.idle_close_grace_ms
        self.update_idle_prompt_countdown()
        self.idle_close_job = self.root.after(self.idle_close_grace_ms, self.close_app)

    def center_idle_prompt(self, win: tk.Toplevel) -> None:
        self.root.update_idletasks()
        root_x = self.root.winfo_rootx()
        root_y = self.root.winfo_rooty()
        root_w = max(1, self.root.winfo_width())
        root_h = max(1, self.root.winfo_height())
        win_w = max(1, win.winfo_width())
        win_h = max(1, win.winfo_height())
        x = root_x + max(0, (root_w - win_w) // 2)
        y = root_y + max(0, (root_h - win_h) // 2)
        win.geometry(f"+{x}+{y}")

    def update_idle_prompt_countdown(self) -> None:
        if self.shutting_down or self.idle_prompt is None or self.idle_prompt_deadline_ms is None:
            return
        remaining_ms = max(0, self.idle_prompt_deadline_ms - monotonic_ms())
        minutes = max(1, ceil(remaining_ms / 60_000))
        if self.idle_prompt_countdown_label is not None:
            self.idle_prompt_countdown_label.configure(
                text=f"The GUI will close in about {minutes} minute{'s' if minutes != 1 else ''} without a response."
            )
        delay = min(60_000, max(1000, remaining_ms))
        self.idle_countdown_job = self.root.after(delay, self.update_idle_prompt_countdown)

    def acknowledge_idle_prompt(self) -> None:
        if self.shutting_down:
            return
        self.last_user_activity_ms = monotonic_ms()
        self.dismiss_idle_prompt()
        self.schedule_idle_check()

    def dismiss_idle_prompt(self) -> None:
        for attr in ("idle_close_job", "idle_countdown_job"):
            job = getattr(self, attr)
            if job is not None:
                try:
                    self.root.after_cancel(job)
                except tk.TclError:
                    pass
                setattr(self, attr, None)
        win = self.idle_prompt
        self.idle_prompt = None
        self.idle_prompt_countdown_label = None
        self.idle_prompt_deadline_ms = None
        if win is None:
            return
        try:
            win.grab_release()
        except tk.TclError:
            pass
        try:
            win.destroy()
        except tk.TclError:
            pass

    def close_app(self) -> None:
        if self.shutting_down:
            return
        self.shutting_down = True
        for attr in (
            "idle_check_job",
            "idle_close_job",
            "idle_countdown_job",
            "auto_refresh_job",
            "search_suggestion_job",
            "party_host_refresh_job",
            "party_host_state_sync_job",
        ):
            job = getattr(self, attr, None)
            if job is not None:
                try:
                    self.root.after_cancel(job)
                except tk.TclError:
                    pass
                setattr(self, attr, None)
        self.dismiss_idle_prompt()
        self.party_join_polling = False
        if self.party_client is not None:
            try:
                self.party_client.leave()
            except Exception:
                pass
            self.party_client = None
        if self.party_host_session is not None:
            try:
                self.party_host_session.close()
            except Exception:
                pass
            self.party_host_session = None
        self.destroy_party_window()
        try:
            self.conn.close()
        except Exception:
            pass
        try:
            self.root.quit()
        except tk.TclError:
            pass
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    def schedule_auto_refresh(self) -> None:
        if self.shutting_down:
            return
        self.auto_refresh_job = self.root.after(self.auto_refresh_ms, self.auto_refresh)

    def auto_refresh(self) -> None:
        if self.shutting_down:
            return
        self.auto_refresh_job = None
        try:
            if self.current_page == "library":
                self.refresh_library(preserve_scroll=True)
            elif self.current_page == "detail":
                focused = self.safe_focus_get()
                if focused is not self.notes:
                    self.load_detail()
                self.refresh_activity()
        finally:
            self.schedule_auto_refresh()

    def safe_focus_get(self):
        try:
            return self.root.focus_get()
        except (KeyError, tk.TclError):
            return None

    def refresh_library(self, preserve_scroll: bool = False) -> None:
        scroll = self.grid_canvas.yview()[0] if preserve_scroll else 0.0
        self._set_active_tab_styles()
        self.current_rows = list_anime(
            self.conn,
            status=self.selected_status.get(),
            search=self.search_text.get().strip() or None,
        )
        self.current_rows.sort(key=lambda row: (row["last_watched_at"] is None, row["last_watched_at"] or "", row["display_title"]), reverse=False)
        self.current_rows.sort(key=lambda row: row["last_watched_at"] or "", reverse=True)
        self.render_grid()
        self.refresh_dashboard()
        self.refresh_activity()
        if preserve_scroll:
            self.grid_canvas.yview_moveto(scroll)

    def render_grid(self) -> None:
        for child in self.grid_frame.winfo_children():
            child.destroy()
        self.card_widgets.clear()
        width = max(self.grid_canvas.winfo_width(), CARD_W)
        columns = max(1, width // CARD_W)
        self.grid_columns = columns
        for index, row in enumerate(self.current_rows):
            card = self.create_card(row)
            card.grid(row=index // columns, column=index % columns, padx=8, pady=8, sticky="nw")
        if not self.current_rows:
            empty = tk.Label(
                self.grid_frame,
                text="No anime in this status.",
                bg=COLORS["bg"],
                fg=COLORS["muted"],
                font=("", 13),
            )
            empty.grid(row=0, column=0, sticky="w", padx=10, pady=20)
        self._update_grid_scroll_region()

    def create_scrollable_card_title(self, parent: tk.Widget, title: str, *, cursor: str = "arrow") -> tk.Text:
        text = tk.Text(
            parent,
            height=DISCOVERY_TITLE_LINES,
            width=1,
            wrap="word",
            bg=COLORS["panel"],
            fg=COLORS["text"],
            font=("", 10, "bold"),
            relief="flat",
            highlightthickness=0,
            borderwidth=0,
            padx=0,
            pady=0,
            insertwidth=0,
            takefocus=0,
            cursor=cursor,
        )
        text.insert("1.0", " ".join(str(title).split()))
        text.configure(state="disabled")
        for sequence in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            text.bind(sequence, lambda event, widget=text: self.scroll_card_title(widget, event))
        return text

    def scroll_card_title(self, widget: tk.Text, event) -> str | None:
        self.record_user_activity(event)
        scroll_units = scroll_units_from_mousewheel(event)
        if scroll_units == 0:
            return None
        first, last = widget.yview()
        if not yview_can_scroll((first, last), scroll_units):
            return None
        widget.yview_scroll(scroll_units, "units")
        return "break"

    def create_card(self, row) -> tk.Frame:
        primary_title, _alt_title = split_display_title(row["display_title"])
        card = tk.Frame(
            self.grid_frame,
            width=CARD_W - 16,
            height=348,
            bg=COLORS["panel"],
            highlightthickness=1,
            highlightbackground=COLORS["border"],
            cursor="hand2",
        )
        card.grid_propagate(False)
        card.columnconfigure(0, weight=1)
        image = self._image_for(row["id"], row["cover_path"], (COVER_W, COVER_H), primary_title)
        cover = tk.Label(card, image=image or "", text="" if image else "No Cover", bg=COLORS["panel"], fg=COLORS["muted"], cursor="hand2")
        cover.grid(row=0, column=0, pady=(10, 8))
        title = self.create_scrollable_card_title(card, primary_title, cursor="hand2")
        title.grid(row=1, column=0, sticky="ew", padx=12)
        total = row["total_episodes"] if row["total_episodes"] is not None else "?"
        available = row["available_episode_count"] if row["available_episode_count"] is not None else "?"
        progress = tk.Label(
            card,
            text=f"{row['watched_count']}/{available}/{total}",
            bg=COLORS["panel"],
            fg=COLORS["muted"],
            anchor="w",
            cursor="hand2",
        )
        progress.grid(row=2, column=0, sticky="ew", padx=12, pady=(4, 0))
        last = tk.Label(
            card,
            text=local_time(row["last_watched_at"], date_only=True) if row["last_watched_at"] else "Not watched",
            bg=COLORS["panel"],
            fg=COLORS["muted"],
            anchor="w",
            cursor="hand2",
        )
        last.grid(row=3, column=0, sticky="ew", padx=12, pady=(0, 12))
        for widget in (card, cover, progress, last):
            widget.bind("<Button-1>", lambda _event, anime_id=row["id"]: self.open_detail(anime_id))
        title.bind("<Button-1>", lambda _event, anime_id=row["id"]: (self.open_detail(anime_id), "break")[1])
        self.card_widgets[int(row["id"])] = card
        return card

    def _update_grid_scroll_region(self, _event=None) -> None:
        self.grid_canvas.configure(scrollregion=self.grid_canvas.bbox("all"))

    def _update_discovery_scroll_region(self, page_name: str, _event=None) -> None:
        canvas = self.discovery_canvases.get(page_name)
        if canvas is not None:
            canvas.configure(scrollregion=canvas.bbox("all"))

    def _update_search_scroll_region(self, _event=None) -> None:
        self.search_canvas.configure(scrollregion=self.search_canvas.bbox("all"))

    def _update_detail_scroll_region(self, _event=None) -> None:
        self.detail_canvas.configure(scrollregion=self.detail_canvas.bbox("all"))

    def _update_schedule_scroll_region(self, _event=None) -> None:
        self.schedule_canvas.configure(scrollregion=self.schedule_canvas.bbox("all"))

    def _on_grid_resize(self, event) -> None:
        self.grid_canvas.itemconfigure(self.grid_window, width=event.width)
        columns = max(1, event.width // CARD_W)
        if columns != self.grid_columns:
            self.render_grid()

    def _on_discovery_resize(self, page_name: str, event) -> None:
        self.discovery_canvases[page_name].itemconfigure(self.discovery_windows[page_name], width=event.width)
        columns = max(1, event.width // DISCOVERY_GRID_W)
        if columns != self.discovery_columns[page_name]:
            self.render_discovery_list(page_name)

    def _on_search_resize(self, event) -> None:
        self.search_canvas.itemconfigure(self.search_window, width=event.width)
        columns = max(1, event.width // DISCOVERY_GRID_W)
        if columns != self.search_columns:
            self.render_search_results()

    def _on_detail_resize(self, event) -> None:
        self.detail_canvas.itemconfigure(self.detail_window, width=event.width)
        columns = max(1, event.width // DISCOVERY_GRID_W)
        if columns != self.related_columns:
            self.render_related_media()

    def _on_schedule_resize(self, event) -> None:
        self.schedule_canvas.itemconfigure(self.schedule_window, width=event.width)

    def _event_mousewheel_owner(self, event):
        widget = getattr(event, "widget", None)
        while widget is not None:
            try:
                if widget_class_owns_mousewheel(widget.winfo_class()):
                    return widget
                parent = widget.winfo_parent()
            except tk.TclError:
                return None
            if not parent:
                return None
            try:
                widget = widget.nametowidget(parent)
            except (KeyError, tk.TclError):
                return None
        return None

    def _widget_can_scroll_with_wheel(self, widget, scroll_units: int) -> bool:
        try:
            yview = widget.yview()
        except (AttributeError, tk.TclError):
            return True
        if not isinstance(yview, tuple) or len(yview) < 2:
            return True
        return yview_can_scroll((float(yview[0]), float(yview[1])), scroll_units)

    def _on_mousewheel(self, event) -> str | None:
        self.record_user_activity(event)
        scroll_units = scroll_units_from_mousewheel(event)
        if scroll_units == 0:
            return None
        owner = self._event_mousewheel_owner(event)
        if owner is not None and self._widget_can_scroll_with_wheel(owner, scroll_units):
            return "break"
        if self.current_page == "library":
            self.grid_canvas.yview_scroll(scroll_units, "units")
        elif self.current_page in self.discovery_canvases:
            self.discovery_canvases[self.current_page].yview_scroll(scroll_units, "units")
        elif self.current_page == "search":
            self.search_canvas.yview_scroll(scroll_units, "units")
        elif self.current_page == "detail":
            self.detail_canvas.yview_scroll(scroll_units, "units")
        elif self.current_page == "schedule":
            self.schedule_canvas.yview_scroll(scroll_units, "units")
        else:
            return None
        return "break"

    def refresh_dashboard(self) -> None:
        counts = status_counts(self.conn)
        self.dashboard_label.configure(
            text=f"Total {sum(counts.values())}   Watching {counts['watching']}   Watched eps {watched_episode_count(self.conn)}"
        )

    def refresh_activity(self) -> None:
        if not hasattr(self, "activity_list"):
            return
        self.activity_list.delete(0, tk.END)
        for event in watch_events(self.conn, recent=12):
            title, _alt_title = split_display_title(event["anime_title"] or "-")
            self.activity_list.insert(tk.END, f"{local_time(event['created_at'])}  {event['event_type']}  {title}")

    def on_global_search_key(self, event) -> None:
        if getattr(event, "keysym", "") in {"Return", "Up", "Down", "Escape"}:
            return
        query = self.global_search_text.get().strip()
        if self.search_suggestion_job is not None:
            self.root.after_cancel(self.search_suggestion_job)
            self.search_suggestion_job = None
        if len(query) < 2:
            self.hide_search_dropdown()
            return
        self.search_suggestion_job = self.root.after(SEARCH_DEBOUNCE_MS, self.start_search_suggestions)

    def cancel_search_suggestions(self) -> None:
        if self.search_suggestion_job is not None:
            self.root.after_cancel(self.search_suggestion_job)
            self.search_suggestion_job = None
        self.search_suggestion_generation += 1
        self.search_suggestions = []

    def hide_search_dropdown(self) -> str:
        self.global_search_dropdown.grid_remove()
        return "break"

    def focus_search_dropdown(self, _event=None) -> str | None:
        if not self.global_search_dropdown.winfo_ismapped() or not self.search_suggestions:
            return None
        self.global_search_dropdown.focus_set()
        self.global_search_dropdown.selection_clear(0, tk.END)
        self.global_search_dropdown.selection_set(0)
        self.global_search_dropdown.activate(0)
        return "break"

    def start_search_suggestions(self) -> None:
        self.search_suggestion_job = None
        query = self.global_search_text.get().strip()
        if len(query) < 2:
            self.hide_search_dropdown()
            return
        self.search_suggestion_generation += 1
        generation = self.search_suggestion_generation

        def worker() -> None:
            payload = search_media(query, load_config(), limit=SEARCH_SUGGESTION_LIMIT, cache_covers=False)
            self.run_on_ui(lambda: self.finish_search_suggestions(generation, query, payload))

        threading.Thread(target=worker, daemon=True).start()

    def finish_search_suggestions(self, generation: int, query: str, payload: dict[str, object]) -> None:
        if generation != self.search_suggestion_generation or query != self.global_search_text.get().strip():
            return
        if payload.get("error"):
            self.hide_search_dropdown()
            return
        self.search_suggestions = [item for item in list(payload.get("items") or []) if isinstance(item, dict)]
        self.global_search_dropdown.delete(0, tk.END)
        for item in self.search_suggestions:
            title, alt_title = split_display_title(str(item.get("display_title") or "Unknown title"))
            self.global_search_dropdown.insert(tk.END, title if alt_title is None else f"{title} ({alt_title})")
        if self.search_suggestions:
            self.global_search_dropdown.grid()
        else:
            self.hide_search_dropdown()

    def choose_search_suggestion(self) -> str:
        selection = self.global_search_dropdown.curselection()
        if not selection:
            return self.submit_global_search()
        item = self.search_suggestions[selection[0]]
        title, _alt_title = split_display_title(str(item.get("display_title") or ""))
        if title:
            self.global_search_text.set(title)
        self.hide_search_dropdown()
        return self.submit_global_search()

    def submit_global_search(self) -> str:
        query = self.global_search_text.get().strip()
        if not query:
            return "break"
        self.cancel_search_suggestions()
        self.hide_search_dropdown()
        self.current_page = "search"
        self._set_active_nav("search")
        self._hide_pages()
        self.search_page.grid(row=0, column=0, sticky="nsew")
        self.search_result_query = query
        self.search_results = []
        self.search_error = None
        self.search_loading = True
        self.search_generation += 1
        generation = self.search_generation
        self.search_canvas.yview_moveto(0)
        self.render_search_results()

        def worker() -> None:
            payload = search_media(query, load_config(), limit=SEARCH_RESULT_LIMIT, cache_covers=True)
            self.run_on_ui(lambda: self.finish_global_search(generation, query, payload))

        threading.Thread(target=worker, daemon=True).start()
        return "break"

    def finish_global_search(self, generation: int, query: str, payload: dict[str, object]) -> None:
        if generation != self.search_generation:
            return
        self.search_loading = False
        self.search_result_query = query
        self.search_results = [item for item in list(payload.get("items") or []) if isinstance(item, dict)]
        error = payload.get("error")
        if error:
            self.search_error = str(error)
            self.search_status_label.configure(text=f"Search failed: {self.search_error}", fg=COLORS["danger"])
        else:
            self.search_error = None
            count = len(self.search_results)
            self.search_status_label.configure(text=f"{count} result{'s' if count != 1 else ''} for {query}", fg=COLORS["muted"])
        if self.current_page == "search":
            self.render_search_results()

    def start_update_check(self) -> None:
        if self.update_checking:
            return
        self.update_checking = True

        def worker() -> None:
            info: UpdateInfo | None = None
            try:
                info = check_for_update()
            except Exception:
                info = None
            self.run_on_ui(lambda: self.finish_update_check(info))

        threading.Thread(target=worker, daemon=True).start()

    def finish_update_check(self, info: UpdateInfo | None) -> None:
        self.update_checking = False
        if info is None or not info.update_available:
            return
        local = info.local_commit[:7] if info.local_commit else info.local_version
        remote = info.remote_commit[:7] if info.remote_commit else info.remote_version or "latest"
        details = f"Current: {info.local_version} ({local})\nLatest: {info.remote_version or 'unknown'} ({remote})"
        if info.remote_message:
            details += f"\n\nLatest commit: {info.remote_message}"
        if not messagebox.askyesno("Update available", f"A newer AniAutoWatchList version is available.\n\n{details}\n\nUpdate now?"):
            return
        try:
            result = launch_update()
        except Exception as exc:
            messagebox.showwarning("Update failed to start", str(exc))
            return
        location = "a terminal window" if result.used_terminal else "a background process"
        messagebox.showinfo(
            "Update started",
            f"The update is running in {location}. When it finishes, close this GUI and relaunch ani-watch-gui.",
        )

    def start_discovery_refresh(self, *, force: bool = False, page_name: str | None = None) -> None:
        if self.discovery_refreshing:
            return
        popular_genre = self.current_popular_genre()
        if page_name == "popular":
            if not force and self.discovery_data.get("popular_fresh"):
                self.update_discovery_status()
                return
        else:
            discovery_fresh = all(
                self.discovery_data.get(f"{value}_fresh")
                for value in (*DISCOVERY_MEDIA_PAGES.keys(), "schedule")
            )
            if not force and discovery_fresh:
                self.update_discovery_status()
                return
        self.discovery_refreshing = True
        self.discovery_error = None
        self.update_discovery_status(refreshing=True)

        def worker() -> None:
            error = None
            try:
                with initialize() as conn:
                    if page_name == "popular":
                        refresh_popular(conn, load_config(), force=force, genre=popular_genre)
                    else:
                        refresh_discovery(conn, load_config(), force=force, popular_genre=popular_genre)
            except Exception as exc:  # pragma: no cover - defensive UI boundary
                error = str(exc)
            self.run_on_ui(lambda: self.finish_discovery_refresh(error))

        threading.Thread(target=worker, daemon=True).start()

    def finish_discovery_refresh(self, error: str | None = None) -> None:
        self.discovery_refreshing = False
        self.discovery_error = error
        self.reload_discovery_data()
        self.update_discovery_status()
        if self.current_page in DISCOVERY_MEDIA_PAGES:
            self.render_discovery_list(self.current_page)
        elif self.current_page == "schedule":
            self.render_schedule()

    def update_discovery_status(self, *, refreshing: bool = False) -> None:
        if not self.discovery_status_labels or not hasattr(self, "schedule_status_label"):
            return
        schedule = self.discovery_data.get("schedule") or {}
        if refreshing:
            text = "Refreshing AniList discovery data..."
            for label in self.discovery_status_labels.values():
                label.configure(text=text, fg=COLORS["muted"])
            self.schedule_status_label.configure(text=text, fg=COLORS["muted"])
            return
        if self.discovery_error:
            text = f"Refresh failed: {self.discovery_error}"
            for label in self.discovery_status_labels.values():
                label.configure(text=text, fg=COLORS["danger"])
            self.schedule_status_label.configure(text=text, fg=COLORS["danger"])
            return
        for page_name, label in self.discovery_status_labels.items():
            data = self.discovery_data.get(page_name) or {}
            error = data.get("error")
            label.configure(
                text=f"Last updated: {local_time(data.get('fetched_at'))}" + (f"  |  {error}" if error else ""),
                fg=COLORS["danger"] if error else COLORS["muted"],
            )
        schedule_error = schedule.get("error")
        self.schedule_status_label.configure(
            text=f"Last updated: {local_time(schedule.get('fetched_at'))}" + (f"  |  {schedule_error}" if schedule_error else ""),
            fg=COLORS["danger"] if schedule_error else COLORS["muted"],
        )

    def render_trending(self) -> None:
        self.render_discovery_list("trending")

    def update_discovery_page_controls(self, page_name: str, item_count: int) -> None:
        data = self.discovery_data.get(page_name) or {}
        page_count = discovery_page_count(item_count)
        page_index = max(0, min(self.discovery_page_indexes.get(page_name, 0), page_count - 1))
        self.discovery_page_indexes[page_name] = page_index
        has_more = bool(data.get("has_more"))
        loading_more = page_name in self.discovery_loading_more
        page_suffix = "+" if has_more else ""
        self.discovery_page_labels[page_name].configure(text=f"{page_index + 1}/{page_count}{page_suffix}")
        self.discovery_prev_buttons[page_name].configure(state="normal" if page_index > 0 else "disabled")
        can_next = page_index < page_count - 1 or has_more
        self.discovery_next_buttons[page_name].configure(state="normal" if can_next and not loading_more else "disabled")

    def render_discovery_list(self, page_name: str) -> None:
        self.update_discovery_status(refreshing=self.discovery_refreshing)
        frame = self.discovery_frames[page_name]
        canvas = self.discovery_canvases[page_name]
        for child in frame.winfo_children():
            child.destroy()
        items = list(((self.discovery_data.get(page_name) or {}).get("items")) or [])
        page_count = discovery_page_count(len(items))
        if self.discovery_page_indexes[page_name] >= page_count:
            self.discovery_page_indexes[page_name] = page_count - 1
        page_items = discovery_page_items(items, self.discovery_page_indexes[page_name])
        width = max(canvas.winfo_width(), DISCOVERY_GRID_W)
        columns = max(1, width // DISCOVERY_GRID_W)
        self.discovery_columns[page_name] = columns
        for index, item in enumerate(page_items):
            card = self.create_discovery_card(frame, item)
            card.grid(row=index // columns, column=index % columns, padx=8, pady=8, sticky="nw")
        if not items:
            empty = tk.Label(
                frame,
                text=DISCOVERY_MEDIA_PAGES[page_name]["empty"],
                bg=COLORS["bg"],
                fg=COLORS["muted"],
                font=("", 13),
            )
            empty.grid(row=0, column=0, sticky="w", padx=10, pady=20)
        self.update_discovery_page_controls(page_name, len(items))
        self._update_discovery_scroll_region(page_name)

    def render_search_results(self) -> None:
        for child in self.search_frame.winfo_children():
            child.destroy()
        title = "Search Results" if not self.search_result_query else f"Search Results: {self.search_result_query}"
        self.search_title_label.configure(text=title)
        width = max(self.search_canvas.winfo_width(), DISCOVERY_GRID_W)
        columns = max(1, width // DISCOVERY_GRID_W)
        self.search_columns = columns
        if self.search_loading:
            self.search_status_label.configure(text=f"Searching AniList for {self.search_result_query}...", fg=COLORS["muted"])
            empty_text = "Searching..."
        elif self.search_error:
            self.search_status_label.configure(text=f"Search failed: {self.search_error}", fg=COLORS["danger"])
            empty_text = "Search failed."
        else:
            empty_text = f"No results for {self.search_result_query}." if self.search_result_query else "Search results will appear here."
            if self.search_result_query and not self.search_results:
                self.search_status_label.configure(text=empty_text, fg=COLORS["muted"])
        for index, item in enumerate(self.search_results):
            card = self.create_discovery_card(self.search_frame, item)
            card.grid(row=index // columns, column=index % columns, padx=8, pady=8, sticky="nw")
        if not self.search_results:
            empty = tk.Label(
                self.search_frame,
                text=empty_text,
                bg=COLORS["bg"],
                fg=COLORS["muted"],
                font=("", 13),
            )
            empty.grid(row=0, column=0, sticky="w", padx=10, pady=20)
        self._update_search_scroll_region()

    def render_related_media(self) -> None:
        if not hasattr(self, "related_frame"):
            return
        for child in self.related_frame.winfo_children():
            child.destroy()
        width = max(self.detail_canvas.winfo_width(), DISCOVERY_GRID_W)
        columns = max(1, width // DISCOVERY_GRID_W)
        self.related_columns = columns
        if self.related_loading:
            self.related_status_label.configure(text="Loading related seasons from AniList...", fg=COLORS["muted"])
            empty_text = "Loading..."
        elif self.related_error:
            self.related_status_label.configure(text=f"Related seasons failed: {self.related_error}", fg=COLORS["danger"])
            empty_text = "Related seasons could not be loaded."
        elif not self.related_anilist_id:
            self.related_status_label.configure(text="Choose an AniList match to show related seasons.", fg=COLORS["muted"])
            empty_text = "No AniList match selected."
        else:
            count = len(self.related_media_items)
            self.related_status_label.configure(text=f"{count} related title{'s' if count != 1 else ''}", fg=COLORS["muted"])
            empty_text = "No related seasons found."
        for index, item in enumerate(self.related_media_items):
            card = self.create_discovery_card(self.related_frame, item)
            card.grid(row=index // columns, column=index % columns, padx=8, pady=8, sticky="nw")
        if not self.related_media_items:
            empty = tk.Label(
                self.related_frame,
                text=empty_text,
                bg=COLORS["bg"],
                fg=COLORS["muted"],
                font=("", 13),
            )
            empty.grid(row=0, column=0, sticky="w", padx=10, pady=12)
        self._update_detail_scroll_region()

    def create_discovery_card(self, parent: tk.Frame, item: dict[str, object]) -> tk.Frame:
        title_text, _alt_title = split_display_title(str(item.get("display_title") or "Unknown title"))
        media_id = int(item.get("id") or 0)
        card = tk.Frame(
            parent,
            width=DISCOVERY_CARD_W,
            height=DISCOVERY_CARD_H,
            bg=COLORS["panel"],
            highlightthickness=1,
            highlightbackground=COLORS["border"],
        )
        card.grid_propagate(False)
        card.columnconfigure(0, weight=1)
        card.rowconfigure(4, weight=1)
        cover_path = item.get("cover_path") if isinstance(item.get("cover_path"), str) else None
        image = self._image_for(media_id, cover_path, (COVER_W, COVER_H), title_text)
        cover = tk.Label(card, image=image or "", text="" if image else "No Cover", bg=COLORS["panel"], fg=COLORS["muted"])
        cover.grid(row=0, column=0, pady=(10, 8))
        title = self.create_scrollable_card_title(card, title_text)
        title.grid(row=1, column=0, sticky="ew", padx=12)
        relation_label = item.get("relation_label")
        score = item.get("average_score") or "-"
        trending = item.get("trending") or "-"
        meta = tk.Label(
            card,
            text=str(relation_label) if relation_label else f"Score {score}   Trend {trending}",
            bg=COLORS["panel"],
            fg=COLORS["muted"],
            anchor="w",
        )
        meta.grid(row=2, column=0, sticky="ew", padx=12, pady=(4, 0))
        next_ep = item.get("next_airing_episode") or {}
        next_text = ""
        if isinstance(next_ep, dict) and next_ep.get("episode"):
            next_text = f"Next ep {next_ep.get('episode')}"
        else:
            next_text = str(item.get("status") or "")
        info = tk.Label(card, text=next_text, bg=COLORS["panel"], fg=COLORS["muted"], anchor="w")
        info.grid(row=3, column=0, sticky="ew", padx=12)
        actions = tk.Frame(card, bg=COLORS["panel"])
        actions.grid(row=5, column=0, sticky="sew", padx=10, pady=(6, 10))
        actions.columnconfigure(0, weight=1)
        actions.columnconfigure(1, weight=1)
        ttk.Button(
            actions,
            text="Plan",
            width=7,
            style="Compact.Dark.TButton",
            command=lambda value=item: self.add_discovery_to_plan(value),
        ).grid(
            row=0, column=0, sticky="ew", padx=(0, 4)
        )
        ttk.Button(
            actions,
            text="AniList",
            width=7,
            style="Compact.Dark.TButton",
            command=lambda value=item: self.open_discovery_link(value),
        ).grid(
            row=0, column=1, sticky="ew"
        )
        return card

    def render_schedule(self) -> None:
        self.update_discovery_status(refreshing=self.discovery_refreshing)
        for child in self.schedule_frame.winfo_children():
            child.destroy()
        items = list(((self.discovery_data.get("schedule") or {}).get("items")) or [])
        grouped: dict[str, list[dict[str, object]]] = {}
        for item in items:
            grouped.setdefault(str(item.get("local_day") or "-"), []).append(item)
        today = datetime.now().date()
        for idx in range(7):
            day = today + timedelta(days=idx)
            day_key = day.isoformat()
            panel = tk.Frame(self.schedule_frame, bg=COLORS["panel"], highlightthickness=1, highlightbackground=COLORS["border"])
            panel.grid(row=0, column=idx, sticky="nsew", padx=(0, 8), pady=8)
            panel.columnconfigure(0, weight=1)
            self.schedule_frame.columnconfigure(idx, weight=1, minsize=104)
            label = "Today" if idx == 0 else day.strftime("%a")
            tk.Label(
                panel,
                text=f"{label}\n{day.strftime('%b %d')}",
                bg=COLORS["panel_alt"],
                fg=COLORS["text"],
                font=("", 10, "bold"),
                justify="left",
                anchor="w",
            ).grid(row=0, column=0, sticky="ew", padx=0, pady=(0, 4), ipady=6)
            entries = grouped.get(day_key, [])
            if not entries:
                tk.Label(panel, text="No releases", bg=COLORS["panel"], fg=COLORS["muted"], anchor="w").grid(
                    row=1, column=0, sticky="ew", padx=8, pady=6
                )
                continue
            for row_idx, item in enumerate(entries, start=1):
                self.create_schedule_row(panel, item).grid(row=row_idx, column=0, sticky="ew", padx=8, pady=(4, 6))
        self._update_schedule_scroll_region()

    def create_schedule_row(self, parent: tk.Frame, item: dict[str, object]) -> tk.Frame:
        media = item.get("media") if isinstance(item.get("media"), dict) else {}
        title_text, _alt_title = split_display_title(str(media.get("display_title") or "Unknown title"))
        frame = tk.Frame(parent, bg=COLORS["entry"], highlightthickness=1, highlightbackground=COLORS["border"])
        frame.columnconfigure(0, weight=1)
        tk.Label(frame, text=str(item.get("local_time") or "-"), bg=COLORS["entry"], fg=COLORS["accent"], anchor="w").grid(
            row=0, column=0, sticky="ew", padx=8, pady=(6, 0)
        )
        tk.Label(
            frame,
            text=title_text,
            bg=COLORS["entry"],
            fg=COLORS["text"],
            wraplength=112,
            justify="left",
            anchor="w",
        ).grid(row=1, column=0, sticky="ew", padx=8, pady=(2, 0))
        tk.Label(
            frame,
            text=f"Episode {item.get('episode') or '?'}",
            bg=COLORS["entry"],
            fg=COLORS["muted"],
            anchor="w",
        ).grid(row=2, column=0, sticky="ew", padx=8, pady=(2, 6))
        return frame

    def add_discovery_to_plan(self, item: dict[str, object]) -> None:
        title = str(item.get("display_title") or "Unknown title")
        anime = get_anime_by_anilist_id(self.conn, item.get("id"))
        if anime is None:
            anime, created = get_or_create_anime(self.conn, title, status="plan_to_watch")
        else:
            created = False
        metadata_payload = item.get("metadata_payload") if isinstance(item.get("metadata_payload"), dict) else None
        updates = {
            "anilist_id": item.get("id") or anime["anilist_id"],
            "total_episodes": item.get("episodes") or anime["total_episodes"],
            "cover_url": item.get("cover_url") or anime["cover_url"],
            "cover_path": item.get("cover_path") or anime["cover_path"],
        }
        if created:
            updates["status"] = "plan_to_watch"
        update_anime_fields(self.conn, anime["id"], **updates)
        if metadata_payload is not None:
            store_selected_metadata_payload(
                self.conn,
                anime["id"],
                item.get("id"),
                metadata_payload,
                str(item.get("cover_path")) if item.get("cover_path") else None,
            )
        if self.current_page == "search" and hasattr(self, "search_status_label"):
            label = self.search_status_label
        elif self.current_page == "detail" and hasattr(self, "related_status_label"):
            label = self.related_status_label
        else:
            label = self.discovery_status_labels.get(self.current_page) or self.discovery_status_labels.get("trending")
        if label is not None:
            label.configure(
                text=f"{split_display_title(title)[0]} {'added to' if created else 'is already in'} your watchlist.",
                fg=COLORS["muted"],
            )
        self.start_episode_availability_refresh(int(anime["id"]))

    def start_episode_availability_refresh(self, anime_id: int) -> None:
        try:
            anime_id = int(anime_id)
        except (TypeError, ValueError):
            return
        if anime_id in self.episode_availability_refreshing:
            return
        self.episode_availability_refreshing.add(anime_id)
        if self.current_page == "detail" and self.selected_anime_id == anime_id and hasattr(self, "launch_label"):
            self.launch_label.configure(text="Updating released episode list from AllAnime...", fg=COLORS["muted"])

        def worker() -> None:
            payload: dict[str, object] = {"anime_id": anime_id}
            try:
                with initialize() as conn:
                    payload.update(refresh_available_episodes_for_anime(conn, anime_id))
            except Exception as exc:  # pragma: no cover - defensive UI boundary
                payload["error"] = str(exc)
            self.run_on_ui(lambda: self.finish_episode_availability_refresh(anime_id, payload))

        threading.Thread(target=worker, daemon=True).start()

    def finish_episode_availability_refresh(self, anime_id: int, payload: dict[str, object]) -> None:
        self.episode_availability_refreshing.discard(anime_id)
        if self.current_page == "library":
            self.refresh_library(preserve_scroll=True)
        if self.current_page != "detail" or self.selected_anime_id != anime_id:
            return
        self.load_detail()
        error = payload.get("error")
        if error:
            self.launch_label.configure(text=f"Released episode update failed: {error}", fg=COLORS["danger"])
            return
        if payload.get("updated"):
            count = payload.get("episode_count")
            self.launch_label.configure(text=f"Released episode list updated: {count or '?'} available.", fg=COLORS["muted"])
        else:
            self.launch_label.configure(text="Released episode list could not be matched automatically.", fg=COLORS["muted"])

    def start_related_media_refresh(self, anime) -> None:
        media_id = anime["anilist_id"] if anime is not None else None
        if not media_id:
            self.related_anilist_id = None
            self.related_media_items = []
            self.related_error = None
            self.related_loading = False
            self.related_loaded = False
            self.render_related_media()
            return
        try:
            media_id_int = int(media_id)
        except (TypeError, ValueError):
            self.related_anilist_id = None
            self.related_media_items = []
            self.related_error = f"Invalid AniList ID: {media_id}"
            self.related_loading = False
            self.related_loaded = True
            self.render_related_media()
            return
        if self.related_anilist_id == media_id_int and (self.related_loading or self.related_loaded):
            self.render_related_media()
            return
        self.related_anilist_id = media_id_int
        self.related_media_items = []
        self.related_error = None
        self.related_loading = True
        self.related_loaded = False
        self.render_related_media()

        def worker() -> None:
            payload = related_media(media_id_int, load_config(), cache_covers=True)
            self.run_on_ui(lambda: self.finish_related_media_refresh(media_id_int, payload))

        threading.Thread(target=worker, daemon=True).start()

    def finish_related_media_refresh(self, media_id: int, payload: dict[str, object]) -> None:
        if self.related_anilist_id != media_id:
            return
        self.related_loading = False
        self.related_loaded = True
        self.related_error = str(payload.get("error")) if payload.get("error") else None
        self.related_media_items = [item for item in list(payload.get("items") or []) if isinstance(item, dict)]
        if self.current_page == "detail":
            self.render_related_media()

    def open_discovery_link(self, item: dict[str, object]) -> None:
        url = item.get("site_url")
        if not url:
            return
        webbrowser.open(str(url))

    def selected_episode_key(self) -> str | None:
        selection = self.episode_tree.selection()
        if not selection:
            return None
        return str(selection[0])

    def update_detail_title_labels(self) -> None:
        self.detail_title_label.configure(text=self.detail_primary_title)
        if self.detail_alt_title:
            self.alt_title_toggle.grid()
            self.alt_title_toggle.configure(state="normal")
            self.detail_alt_title_label.configure(text=self.detail_alt_title)
            if self.show_alt_title.get():
                self.detail_alt_title_label.grid()
            else:
                self.detail_alt_title_label.grid_remove()
            return
        self.show_alt_title.set(False)
        self.alt_title_toggle.grid_remove()
        self.detail_alt_title_label.grid_remove()

    def load_detail(self) -> None:
        if self.selected_anime_id is None:
            return
        anime = get_anime_by_id(self.conn, self.selected_anime_id)
        if anime is None:
            self.show_library()
            return
        self.detail_primary_title, self.detail_alt_title = split_display_title(anime["display_title"])
        self.update_detail_title_labels()
        self.detail_status.set(STATUS_LABELS[anime["status"]])
        image = self._image_for(anime["id"], anime["cover_path"], (DETAIL_COVER_W, DETAIL_COVER_H), self.detail_primary_title)
        self.detail_cover_label.configure(image=image or "", text="" if image else "No Cover", fg=COLORS["muted"])
        episodes = episodes_for_anime(self.conn, anime["id"])
        watched = sum(1 for episode in episodes if episode["watched"])
        available = anime["available_episode_count"] if anime["available_episode_count"] is not None else len(episodes) or "?"
        total = anime["total_episodes"] if anime["total_episodes"] is not None else "?"
        self.progress_label.configure(text=f"Progress: {watched}/{available}/{total}")
        self.last_label.configure(text=f"Last watched: {local_time(anime['last_watched_at'])}")
        self.anilist_label.configure(text=f"AniList ID: {anime['anilist_id'] or '-'}")
        if not self.launch_label.cget("text"):
            self.launch_label.configure(text="Select an episode, then Continue to open ani-cli.")
        if self.safe_focus_get() is not self.notes:
            self.notes.delete("1.0", tk.END)
            self.notes.insert("1.0", anime["notes"] or "")
        selected_episode = self.selected_episode_key()
        self.episode_tree.delete(*self.episode_tree.get_children())
        for episode in episodes:
            self.episode_tree.insert(
                "",
                "end",
                iid=episode["episode_key"],
                values=(
                    WATCHED_ICON if episode["watched"] else UNWATCHED_ICON,
                    episode["episode_key"],
                    local_time(episode["last_started_at"]),
                    local_time(episode["watched_at"]),
                ),
            )
        if selected_episode and self.episode_tree.exists(selected_episode):
            self.episode_tree.selection_set(selected_episode)
        self.refresh_activity()
        self.start_related_media_refresh(anime)

    def add_anime(self) -> None:
        title = simpledialog.askstring("Add anime", "Title:")
        if not title:
            return
        anime, _ = get_or_create_anime(self.conn, title, status=self.selected_status.get())
        self.selected_anime_id = anime["id"]
        self.show_library()
        self.open_detail(anime["id"])

    def edit_title(self) -> None:
        if self.selected_anime_id is None:
            return
        anime = get_anime_by_id(self.conn, self.selected_anime_id)
        if anime is None:
            return
        title = simpledialog.askstring("Edit title", "Title:", initialvalue=anime["display_title"])
        if not title:
            return
        update_anime_fields(self.conn, anime["id"], display_title=clean_display_title(title), source_title=title)
        self.load_detail()

    def delete_selected(self) -> None:
        if self.selected_anime_id is None:
            return
        anime = get_anime_by_id(self.conn, self.selected_anime_id)
        if anime is None:
            return
        if messagebox.askyesno("Delete anime", f"Delete {anime['display_title']}?"):
            delete_anime(self.conn, anime["id"])
            self.selected_anime_id = None
            self.show_library()

    def save_status(self) -> None:
        if self.selected_anime_id is None:
            return
        label_to_status = {label: status for status, label in STATUS_LABELS.items()}
        status = label_to_status.get(self.detail_status.get(), "watching")
        update_anime_fields(self.conn, self.selected_anime_id, status=status)
        self.selected_status.set(status)
        self.load_detail()

    def save_notes(self) -> None:
        if self.selected_anime_id is None:
            return
        update_anime_fields(self.conn, self.selected_anime_id, notes=self.notes.get("1.0", tk.END).strip())
        self.load_detail()

    def add_episode(self) -> None:
        if self.selected_anime_id is None:
            return
        episode = simpledialog.askstring("Add episode", "Episode:")
        if not episode:
            return
        upsert_episodes(self.conn, self.selected_anime_id, [episode], source_label="manual")
        self.load_detail()

    def mark_selected_episode(self, watched: bool) -> None:
        if self.selected_anime_id is None:
            return
        episode = self.selected_episode_key()
        if not episode:
            messagebox.showinfo("Episode required", "Select an episode first.")
            return
        mark_episode(self.conn, self.selected_anime_id, episode, watched)
        self.load_detail()

    def toggle_selected_episode(self, _event=None) -> None:
        if self.selected_anime_id is None:
            return
        episode = self.selected_episode_key()
        if not episode:
            return
        rows = {row["episode_key"]: row for row in episodes_for_anime(self.conn, self.selected_anime_id)}
        current = rows.get(episode)
        if current is None:
            return
        mark_episode(self.conn, self.selected_anime_id, episode, not bool(current["watched"]))
        self.load_detail()

    def continue_selected_episode(self, mode: str = "sub") -> None:
        if self.selected_anime_id is None:
            return
        anime = get_anime_by_id(self.conn, self.selected_anime_id)
        if anime is None:
            return
        episode = self.selected_episode_key()
        if not episode:
            messagebox.showinfo("Episode required", "Select an episode first.")
            return
        title = choose_ani_cli_search_title(anime["display_title"], anime["source_title"])
        launch_mode = mode.strip().casefold()
        if launch_mode not in {"sub", "dub"}:
            launch_mode = "sub"
        title_for_message, _alt_title = split_display_title(anime["display_title"])
        metadata_payload = selected_metadata_payload(self.conn, self.selected_anime_id)
        total_episodes = int(anime["total_episodes"]) if anime["total_episodes"] is not None else None

        def resolve_target(target_mode: str):
            try:
                return resolve_allanime_launch_target(
                    anime["display_title"],
                    anime["source_title"],
                    metadata_payload,
                    total_episodes=total_episodes,
                    mode=target_mode,
                )
            except LaunchError:
                return None

        target = resolve_target(launch_mode)
        if launch_mode == "dub" and target is None:
            target = resolve_target("sub")
        if target is None and (metadata_payload_is_adult(metadata_payload) or title_has_adult_label(anime["display_title"])):
            message = (
                f"AllAnime did not return a playable result for {title_for_message}. "
                "ani-cli can only launch titles available from AllAnime."
            )
            self.launch_label.configure(text=message, fg=COLORS["danger"])
            messagebox.showwarning("AllAnime result not found", message)
            return
        if launch_mode == "dub":
            self.launch_label.configure(text=f"Checking dub availability for episode {episode}...", fg=COLORS["muted"])
            self.root.update_idletasks()
            try:
                has_dub = allanime_episode_available(
                    target.title if target is not None else title,
                    episode,
                    mode="dub",
                    show_id=target.show_id if target is not None else None,
                    episode_count=target.episode_count if target is not None else None,
                )
            except LaunchError as exc:
                if not messagebox.askyesno(
                    "Dub check failed",
                    f"Could not check dub availability: {exc}\n\nTry the dub search anyway?",
                ):
                    self.launch_label.configure(text="Dub launch canceled.", fg=COLORS["muted"])
                    return
            else:
                if not has_dub:
                    if not messagebox.askyesno(
                        "Dub unavailable",
                        f"No dub was found for {title_for_message} episode {episode}.\n\nSearch sub instead?",
                    ):
                        self.launch_label.configure(
                            text=f"No dub found for {title_for_message} episode {episode}.",
                            fg=COLORS["muted"],
                        )
                        return
                    launch_mode = "sub"
                    target = resolve_target(launch_mode)
        try:
            launch_title = target.title if target is not None else title
            result = launch_episode(
                launch_title,
                episode,
                mode=launch_mode,
                allanime_id=target.show_id if target is not None else None,
            )
        except LaunchError as exc:
            self.launch_label.configure(text=f"Launch failed: {exc}", fg=COLORS["danger"])
            messagebox.showwarning("ani-cli launch failed", str(exc))
            return
        target = "terminal" if result.used_terminal else "background process"
        mode_label = "dub" if launch_mode == "dub" else "sub"
        self.launch_label.configure(
            text=f"Opened {title_for_message} episode {episode} ({mode_label}) in {target}.",
            fg=COLORS["muted"],
        )

    def detail_party_episode_key(self) -> str | None:
        if self.selected_anime_id is None:
            return None
        selected = self.selected_episode_key()
        if selected:
            return selected
        next_episode = next_unwatched_episode(self.conn, self.selected_anime_id)
        if next_episode is not None:
            episode_key = str(next_episode["episode_key"])
            if self.episode_tree.exists(episode_key):
                self.episode_tree.selection_set(episode_key)
            return episode_key
        episodes = episodes_for_anime(self.conn, self.selected_anime_id)
        if episodes:
            episode_key = str(episodes[0]["episode_key"])
            if self.episode_tree.exists(episode_key):
                self.episode_tree.selection_set(episode_key)
            return episode_key
        return None

    def ask_party_mode(self) -> str | None:
        mode = simpledialog.askstring("Watch party mode", "Playback mode: sub or dub", initialvalue="sub")
        if mode is None:
            return None
        normalized = mode.strip().casefold()
        if normalized not in {"sub", "dub"}:
            messagebox.showinfo("Playback mode", "Use sub or dub.")
            return None
        return normalized

    def host_watch_party(self) -> None:
        if self.selected_anime_id is None:
            messagebox.showinfo("Anime required", "Open an anime detail page before hosting a watch party.")
            return
        anime = get_anime_by_id(self.conn, self.selected_anime_id)
        if anime is None:
            return
        episode = self.detail_party_episode_key()
        if not episode:
            messagebox.showinfo("Episode required", "Add or select an episode before hosting a watch party.")
            return
        mode = self.ask_party_mode()
        if mode is None:
            return
        title_for_message, _alt_title = split_display_title(anime["display_title"])
        party_title = simpledialog.askstring(
            "Host watch party",
            "Party title:",
            initialvalue=f"{title_for_message} episode {episode}",
        )
        if not party_title:
            return
        username = simpledialog.askstring("Host watch party", "Your display name:", initialvalue="Host")
        if not username:
            return
        if self.party_host_session is not None:
            if not messagebox.askyesno("Watch party active", "End the current watch party and start a new one?"):
                return
            self.end_host_party(show_message=False)
        metadata_payload = selected_metadata_payload(self.conn, self.selected_anime_id)
        total_episodes = int(anime["total_episodes"]) if anime["total_episodes"] is not None else None
        target = None
        try:
            target = resolve_allanime_launch_target(
                anime["display_title"],
                anime["source_title"],
                metadata_payload,
                total_episodes=total_episodes,
                mode=mode,
            )
        except LaunchError:
            target = None
        if target is None and (metadata_payload_is_adult(metadata_payload) or title_has_adult_label(anime["display_title"])):
            message = (
                f"AllAnime did not return a playable result for {title_for_message}. "
                "Watch parties can only launch titles available from AllAnime."
            )
            self.launch_label.configure(text=message, fg=COLORS["danger"])
            messagebox.showwarning("AllAnime result not found", message)
            return
        launch_title = target.title if target is not None else choose_ani_cli_search_title(anime["display_title"], anime["source_title"])
        media = WatchPartyMedia(
            party_title=party_title,
            anime_title=anime["display_title"],
            source_title=anime["source_title"],
            episode=episode,
            mode=mode,
            allanime_id=target.show_id if target is not None else None,
            allanime_title=target.title if target is not None else launch_title,
            total_episodes=total_episodes,
        )
        self.launch_label.configure(text="Creating public watch party tunnel...", fg=COLORS["muted"])
        self.root.update_idletasks()
        try:
            session = start_host_session(media, host_username=username)
        except Exception as exc:
            messagebox.showwarning("Watch party failed", str(exc))
            return
        ipc_path = party_ipc_path(f"host-{session.room.room_id}")
        try:
            launch_episode(
                launch_title,
                episode,
                mode=mode,
                allanime_id=target.show_id if target is not None else None,
                mpv_ipc_path=ipc_path,
            )
        except LaunchError as exc:
            session.close()
            messagebox.showwarning("ani-cli launch failed", str(exc))
            return
        self.party_host_session = session
        self.party_client = None
        self.party_join_polling = False
        self.party_playback_controller = MpvIpcController(ipc_path)
        self.party_current_media = media
        self.party_link_var.set(session.share_url)
        tunnel_note = "Public tunnel active." if session.public else f"Public tunnel unavailable: {session.tunnel_error or 'unknown error'}"
        self.party_status_text.set(tunnel_note)
        self.launch_label.configure(text=f"Watch party started for episode {episode}. {tunnel_note}", fg=COLORS["muted"])
        self.show_host_party_window()
        self.publish_host_party_state()
        self.schedule_host_party_state_sync()
        self.schedule_host_party_refresh()

    def join_watch_party(self) -> None:
        link = simpledialog.askstring("Join watch party", "Paste the watch party link:")
        if not link:
            return
        username = simpledialog.askstring("Join watch party", "Your display name:", initialvalue="Guest")
        if not username:
            return
        client = WatchPartyRemoteClient(link, username)
        try:
            payload = client.join()
        except WatchPartyError as exc:
            messagebox.showwarning("Join watch party failed", str(exc))
            return
        state = payload.get("state") if isinstance(payload.get("state"), dict) else {}
        media_payload = state.get("media") if isinstance(state.get("media"), dict) else {}
        media = WatchPartyMedia.from_json(media_payload)
        ipc_path = party_ipc_path(f"join-{client.participant_id or 'guest'}")
        try:
            self.launch_party_media(media, ipc_path)
        except LaunchError as exc:
            try:
                client.leave()
            except WatchPartyError:
                pass
            messagebox.showwarning("ani-cli launch failed", str(exc))
            return
        self.party_client = client
        self.party_join_polling = True
        self.party_host_session = None
        self.party_playback_controller = MpvIpcController(ipc_path)
        self.party_current_media = media
        self.party_username_var.set(username)
        self.party_status_text.set(f"Joined {media.party_title}. Waiting for host controls.")
        self.show_joined_party_window(state)
        self.start_party_initial_sync(state)
        self.start_party_event_poll()

    def launch_party_media(self, media: WatchPartyMedia, ipc_path: str) -> None:
        launch_title = media.allanime_title or media.anime_title
        launch_episode(
            launch_title,
            media.episode,
            mode=media.mode,
            allanime_id=media.allanime_id,
            mpv_ipc_path=ipc_path,
        )

    def playback_state_target_position(self, playback_state: dict[str, object]) -> float:
        try:
            position = float(playback_state.get("position_seconds") or 0.0)
        except (TypeError, ValueError):
            position = 0.0
        if playback_state.get("paused"):
            return max(0.0, position)
        updated_at = self.parse_party_timestamp(str(playback_state.get("updated_at") or ""))
        if updated_at is None:
            return max(0.0, position)
        elapsed = max(0.0, (datetime.now(timezone.utc) - updated_at).total_seconds())
        return max(0.0, position + elapsed)

    def parse_party_timestamp(self, value: str) -> datetime | None:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    def start_party_initial_sync(self, state: dict[str, object]) -> None:
        playback_state = state.get("playback_state") if isinstance(state.get("playback_state"), dict) else None
        if playback_state is None:
            return
        self.apply_party_playback_state(playback_state, wait_for_socket=True)

    def apply_party_playback_state(self, playback_state: dict[str, object], *, wait_for_socket: bool = False) -> None:
        controller = self.party_playback_controller
        if controller is None:
            return
        target_position = self.playback_state_target_position(playback_state)
        paused = bool(playback_state.get("paused"))

        def worker() -> None:
            deadline = monotonic() + PARTY_INITIAL_SYNC_TIMEOUT_SECONDS
            while wait_for_socket and monotonic() < deadline and not controller.available():
                sleep(0.5)
            try:
                if paused:
                    controller.pause()
                controller.seek(target_position)
                if paused:
                    controller.pause()
                else:
                    controller.play()
            except Exception as exc:
                self.run_on_ui(lambda error=str(exc): self.party_status_text.set(f"Playback sync failed: {error}"))
                return
            state_text = "paused" if paused else "playing"
            self.run_on_ui(
                lambda: self.party_status_text.set(
                    f"Synced to host at {int(target_position // 60)}:{int(target_position % 60):02d} ({state_text})."
                )
            )

        if wait_for_socket:
            threading.Thread(target=worker, daemon=True).start()
        else:
            worker()

    def show_host_party_window(self) -> None:
        if self.party_host_session is None:
            return
        self.destroy_party_window()
        win = tk.Toplevel(self.root)
        win.title("Watch Party Host")
        win.configure(bg=COLORS["bg"])
        win.geometry("680x500")
        win.columnconfigure(0, weight=1)
        win.rowconfigure(4, weight=1)
        self.party_window = win
        media = self.party_current_media
        heading = media.party_title if media is not None else "Watch Party"
        tk.Label(win, text=heading, bg=COLORS["bg"], fg=COLORS["text"], font=("", 16, "bold")).grid(
            row=0, column=0, sticky="w", padx=14, pady=(14, 4)
        )
        tk.Label(win, textvariable=self.party_status_text, bg=COLORS["bg"], fg=COLORS["muted"], anchor="w").grid(
            row=1, column=0, sticky="ew", padx=14
        )
        link_row = tk.Frame(win, bg=COLORS["bg"])
        link_row.grid(row=2, column=0, sticky="ew", padx=14, pady=(10, 6))
        link_row.columnconfigure(0, weight=1)
        link_entry = tk.Entry(
            link_row,
            textvariable=self.party_link_var,
            bg=COLORS["entry"],
            fg=COLORS["text"],
            insertbackground=COLORS["text"],
            relief="flat",
        )
        link_entry.grid(row=0, column=0, sticky="ew", ipady=6)
        ttk.Button(link_row, text="Copy Link", style="Dark.TButton", command=self.copy_party_link).grid(
            row=0, column=1, padx=(8, 0)
        )
        controls = tk.Frame(win, bg=COLORS["bg"])
        controls.grid(row=3, column=0, sticky="ew", padx=14, pady=(4, 8))
        for idx, (text, command, style) in enumerate(
            [
                ("Play", lambda: self.host_party_control("play"), "Accent.TButton"),
                ("Pause", lambda: self.host_party_control("pause"), "Dark.TButton"),
                ("-10s", lambda: self.host_party_seek(-10), "Dark.TButton"),
                ("+10s", lambda: self.host_party_seek(10), "Dark.TButton"),
                ("Prev Episode", lambda: self.host_party_change_episode(-1), "Dark.TButton"),
                ("Next Episode", lambda: self.host_party_change_episode(1), "Dark.TButton"),
                ("End Party", self.end_host_party, "Dark.TButton"),
            ]
        ):
            ttk.Button(controls, text=text, style=style, command=command).grid(row=0, column=idx, padx=(0, 6), pady=4)
        participants_box = tk.Frame(win, bg=COLORS["panel"], highlightthickness=1, highlightbackground=COLORS["border"])
        participants_box.grid(row=4, column=0, sticky="nsew", padx=14, pady=(0, 14))
        participants_box.columnconfigure(0, weight=1)
        participants_box.rowconfigure(1, weight=1)
        tk.Label(participants_box, text="Participants", bg=COLORS["panel"], fg=COLORS["text"], font=("", 12, "bold")).grid(
            row=0, column=0, sticky="w", padx=10, pady=(10, 4)
        )
        self.party_participant_list = tk.Listbox(
            participants_box,
            bg=COLORS["panel"],
            fg=COLORS["text"],
            selectbackground=COLORS["accent"],
            selectforeground="#111111",
            relief="flat",
            highlightthickness=0,
        )
        self.party_participant_list.grid(row=1, column=0, sticky="nsew", padx=10, pady=(0, 8))
        action_row = tk.Frame(participants_box, bg=COLORS["panel"])
        action_row.grid(row=2, column=0, sticky="ew", padx=10, pady=(0, 10))
        ttk.Button(action_row, text="Refresh", style="Dark.TButton", command=self.render_host_party_participants).grid(
            row=0, column=0, padx=(0, 8)
        )
        ttk.Button(action_row, text="Kick Selected", style="Dark.TButton", command=self.kick_selected_party_participant).grid(row=0, column=1)
        win.protocol("WM_DELETE_WINDOW", self.destroy_party_window)
        self.render_host_party_participants()

    def show_joined_party_window(self, state: dict[str, object]) -> None:
        self.destroy_party_window()
        win = tk.Toplevel(self.root)
        win.title("Watch Party")
        win.configure(bg=COLORS["bg"])
        win.geometry("520x260")
        win.columnconfigure(0, weight=1)
        self.party_window = win
        media_payload = state.get("media") if isinstance(state.get("media"), dict) else {}
        media = WatchPartyMedia.from_json(media_payload)
        tk.Label(win, text=media.party_title, bg=COLORS["bg"], fg=COLORS["text"], font=("", 16, "bold")).grid(
            row=0, column=0, sticky="w", padx=14, pady=(14, 4)
        )
        tk.Label(
            win,
            text=f"{split_display_title(media.anime_title)[0]} episode {media.episode} ({media.mode})",
            bg=COLORS["bg"],
            fg=COLORS["muted"],
            anchor="w",
        ).grid(row=1, column=0, sticky="ew", padx=14)
        tk.Label(win, textvariable=self.party_status_text, bg=COLORS["bg"], fg=COLORS["muted"], anchor="w").grid(
            row=2, column=0, sticky="ew", padx=14, pady=(8, 0)
        )
        username_row = tk.Frame(win, bg=COLORS["bg"])
        username_row.grid(row=3, column=0, sticky="ew", padx=14, pady=(14, 6))
        username_row.columnconfigure(1, weight=1)
        tk.Label(username_row, text="Username", bg=COLORS["bg"], fg=COLORS["muted"]).grid(row=0, column=0, padx=(0, 8))
        tk.Entry(
            username_row,
            textvariable=self.party_username_var,
            bg=COLORS["entry"],
            fg=COLORS["text"],
            insertbackground=COLORS["text"],
            relief="flat",
        ).grid(row=0, column=1, sticky="ew", ipady=6)
        ttk.Button(username_row, text="Update", style="Dark.TButton", command=self.change_party_username).grid(
            row=0, column=2, padx=(8, 0)
        )
        ttk.Button(win, text="Leave Party", style="Dark.TButton", command=self.leave_joined_party).grid(
            row=4, column=0, sticky="ew", padx=14, pady=(10, 0)
        )
        win.protocol("WM_DELETE_WINDOW", self.leave_joined_party)

    def destroy_party_window(self) -> None:
        if self.party_window is None:
            return
        try:
            self.party_window.destroy()
        except tk.TclError:
            pass
        self.party_window = None
        self.party_participant_list = None

    def copy_party_link(self) -> None:
        link = self.party_link_var.get()
        if not link:
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(link)
        self.party_status_text.set("Watch party link copied.")

    def schedule_host_party_refresh(self) -> None:
        if self.shutting_down or self.party_host_session is None:
            return
        if self.party_host_refresh_job is not None:
            try:
                self.root.after_cancel(self.party_host_refresh_job)
            except tk.TclError:
                pass
        self.party_host_refresh_job = self.root.after(PARTY_HOST_REFRESH_MS, self.host_party_refresh_tick)

    def host_party_refresh_tick(self) -> None:
        self.party_host_refresh_job = None
        if self.shutting_down or self.party_host_session is None:
            return
        self.render_host_party_participants()
        self.schedule_host_party_refresh()

    def schedule_host_party_state_sync(self) -> None:
        if self.shutting_down or self.party_host_session is None:
            return
        if self.party_host_state_sync_job is not None:
            try:
                self.root.after_cancel(self.party_host_state_sync_job)
            except tk.TclError:
                pass
        self.party_host_state_sync_job = self.root.after(PARTY_HOST_STATE_SYNC_MS, self.host_party_state_sync_tick)

    def host_party_state_sync_tick(self) -> None:
        self.party_host_state_sync_job = None
        if self.shutting_down or self.party_host_session is None:
            return
        self.publish_host_party_state()
        self.schedule_host_party_state_sync()

    def cancel_host_party_jobs(self) -> None:
        for attr in ("party_host_refresh_job", "party_host_state_sync_job"):
            job = getattr(self, attr)
            if job is not None:
                try:
                    self.root.after_cancel(job)
                except tk.TclError:
                    pass
                setattr(self, attr, None)

    def publish_host_party_state(self) -> dict[str, object] | None:
        if self.party_host_session is None or self.party_playback_controller is None:
            return None
        snapshot = self.party_playback_controller.snapshot()
        if snapshot is None:
            return None
        self.party_host_session.room.update_playback_state(
            position_seconds=float(snapshot.get("position_seconds") or 0.0),
            paused=bool(snapshot.get("paused")),
            episode=self.party_current_media.episode if self.party_current_media is not None else None,
        )
        return snapshot

    def render_host_party_participants(self) -> None:
        if self.party_host_session is None or self.party_participant_list is None:
            return
        self.publish_host_party_state()
        state = self.party_host_session.room.public_state()
        participants = list(state.get("participants") or [])
        self.party_participant_ids = []
        self.party_participant_list.delete(0, tk.END)
        if not participants:
            self.party_participant_list.insert(tk.END, "No one has joined yet.")
            return
        for participant in participants:
            if not isinstance(participant, dict):
                continue
            self.party_participant_ids.append(str(participant.get("participant_id") or ""))
            joined = local_time(str(participant.get("joined_at") or ""))
            self.party_participant_list.insert(tk.END, f"{participant.get('username') or 'Guest'}  joined {joined}")

    def kick_selected_party_participant(self) -> None:
        if self.party_host_session is None or self.party_participant_list is None:
            return
        selection = self.party_participant_list.curselection()
        if not selection:
            return
        participant_ids = getattr(self, "party_participant_ids", [])
        if selection[0] >= len(participant_ids):
            return
        participant_id = participant_ids[selection[0]]
        try:
            participant = self.party_host_session.room.kick(participant_id)
        except WatchPartyError as exc:
            messagebox.showwarning("Kick failed", str(exc))
            return
        self.party_status_text.set(f"Removed {participant.username} from the watch party.")
        self.render_host_party_participants()

    def host_party_control(self, action: str, payload: dict[str, object] | None = None) -> None:
        if self.party_host_session is None:
            return
        payload = dict(payload or {})
        snapshot = self.publish_host_party_state()
        if snapshot is not None and payload.get("position_seconds") is None:
            payload["position_seconds"] = float(snapshot.get("position_seconds") or 0.0)
        self.apply_local_party_control(action, payload)
        try:
            self.party_host_session.room.control(action, payload)
        except WatchPartyError as exc:
            messagebox.showwarning("Watch party control failed", str(exc))
            return
        self.party_status_text.set(f"Sent {action.replace('_', ' ')} to the watch party.")

    def host_party_seek(self, delta_seconds: int) -> None:
        self.host_party_control("relative_seek", {"delta_seconds": delta_seconds})

    def host_party_change_episode(self, direction: int) -> None:
        if self.party_host_session is None or self.party_current_media is None or self.selected_anime_id is None:
            return
        episodes = [str(row["episode_key"]) for row in episodes_for_anime(self.conn, self.selected_anime_id)]
        if not episodes:
            messagebox.showinfo("Episode required", "No episode list is available for this anime.")
            return
        current = self.party_current_media.episode
        if current not in episodes:
            messagebox.showinfo("Episode required", f"Current party episode {current} is not in the episode list.")
            return
        next_index = episodes.index(current) + direction
        if next_index < 0 or next_index >= len(episodes):
            messagebox.showinfo("Episode unavailable", "There is no episode in that direction.")
            return
        next_episode = episodes[next_index]
        previous_controller = self.party_playback_controller
        if previous_controller is not None:
            try:
                previous_controller.stop()
            except Exception:
                pass
        media = WatchPartyMedia(
            party_title=self.party_current_media.party_title,
            anime_title=self.party_current_media.anime_title,
            source_title=self.party_current_media.source_title,
            episode=next_episode,
            mode=self.party_current_media.mode,
            allanime_id=self.party_current_media.allanime_id,
            allanime_title=self.party_current_media.allanime_title,
            total_episodes=self.party_current_media.total_episodes,
        )
        ipc_path = party_ipc_path(f"host-{self.party_host_session.room.room_id}-{next_episode}")
        try:
            self.launch_party_media(media, ipc_path)
        except LaunchError as exc:
            messagebox.showwarning("ani-cli launch failed", str(exc))
            return
        self.party_current_media = media
        self.party_playback_controller = MpvIpcController(ipc_path)
        action = "next_episode" if direction > 0 else "previous_episode"
        self.host_party_control(action, {"episode": next_episode, "media": media.to_json(), "position_seconds": 0.0})

    def apply_local_party_control(self, action: str, payload: dict[str, object]) -> None:
        controller = self.party_playback_controller
        if controller is None:
            return
        try:
            if action == "play":
                controller.play()
            elif action == "pause":
                controller.pause()
            elif action == "seek":
                controller.seek(float(payload.get("position_seconds") or 0))
            elif action == "relative_seek":
                controller.relative_seek(float(payload.get("delta_seconds") or 0))
            elif action == "stop":
                controller.stop()
        except Exception:
            self.party_status_text.set("Sent control event. Local mpv control is not ready yet.")

    def end_host_party(self, show_message: bool = True) -> None:
        self.cancel_host_party_jobs()
        session = self.party_host_session
        self.party_host_session = None
        if session is not None:
            session.close()
        self.destroy_party_window()
        if show_message:
            messagebox.showinfo("Watch party ended", "The watch party has ended.")

    def start_party_event_poll(self) -> None:
        client = self.party_client
        if client is None:
            return

        def worker() -> None:
            while self.party_join_polling and self.party_client is client:
                try:
                    payload = client.poll_events()
                except WatchPartyError as exc:
                    self.run_on_ui(lambda error=str(exc): self.party_poll_failed(error))
                    break
                self.run_on_ui(lambda value=payload: self.apply_party_events(value))

        threading.Thread(target=worker, daemon=True).start()

    def party_poll_failed(self, error: str) -> None:
        self.party_join_polling = False
        self.party_status_text.set(f"Watch party connection lost: {error}")

    def apply_party_events(self, payload: dict[str, object]) -> None:
        events = payload.get("events") if isinstance(payload.get("events"), list) else []
        for event in events:
            if not isinstance(event, dict):
                continue
            event_type = str(event.get("event_type") or "")
            event_payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
            if event_type == "participant_kicked":
                participant = event_payload.get("participant") if isinstance(event_payload.get("participant"), dict) else {}
                if self.party_client is not None and participant.get("participant_id") == self.party_client.participant_id:
                    self.party_join_polling = False
                    self.apply_local_party_control("stop", {})
                    self.party_status_text.set("You were removed from the watch party.")
                    messagebox.showinfo("Watch party", "You were removed from the watch party.")
                    return
            elif event_type == "party_ended":
                self.party_join_polling = False
                self.apply_local_party_control("stop", {})
                self.party_status_text.set("The host ended the watch party.")
                messagebox.showinfo("Watch party", "The host ended the watch party.")
                return
            elif event_type in {"play", "pause", "seek", "relative_seek", "playback_state"}:
                playback_state = event_payload.get("playback_state") if isinstance(event_payload.get("playback_state"), dict) else None
                if playback_state is not None:
                    self.apply_party_playback_state(playback_state)
                else:
                    self.apply_local_party_control(event_type, event_payload)
            elif event_type == "stop":
                self.apply_local_party_control(event_type, event_payload)
            elif event_type in {"next_episode", "previous_episode"}:
                media_payload = event_payload.get("media") if isinstance(event_payload.get("media"), dict) else None
                if media_payload is None:
                    continue
                media = WatchPartyMedia.from_json(media_payload)
                previous_controller = self.party_playback_controller
                if previous_controller is not None:
                    try:
                        previous_controller.stop()
                    except Exception:
                        pass
                ipc_path = party_ipc_path(f"join-{self.party_client.participant_id if self.party_client else 'guest'}-{media.episode}")
                try:
                    self.launch_party_media(media, ipc_path)
                except LaunchError as exc:
                    self.party_status_text.set(f"Episode launch failed: {exc}")
                    continue
                self.party_current_media = media
                self.party_playback_controller = MpvIpcController(ipc_path)
                self.party_status_text.set(f"Host changed to episode {media.episode}.")
                playback_state = event_payload.get("playback_state") if isinstance(event_payload.get("playback_state"), dict) else None
                if playback_state is not None:
                    self.apply_party_playback_state(playback_state, wait_for_socket=True)

    def change_party_username(self) -> None:
        if self.party_client is None:
            return
        username = self.party_username_var.get().strip()
        if not username:
            return
        try:
            self.party_client.set_username(username)
        except WatchPartyError as exc:
            messagebox.showwarning("Username update failed", str(exc))
            return
        self.party_status_text.set(f"Username changed to {username}.")

    def leave_joined_party(self) -> None:
        self.party_join_polling = False
        client = self.party_client
        self.party_client = None
        if client is not None:
            try:
                client.leave()
            except WatchPartyError:
                pass
        self.destroy_party_window()

    def reorder_selected(self, direction: int) -> None:
        if self.selected_anime_id is None:
            return
        anime = get_anime_by_id(self.conn, self.selected_anime_id)
        if anime is None:
            return
        rows = list_anime(self.conn, status=anime["status"])
        ids = [row["id"] for row in rows]
        if self.selected_anime_id not in ids:
            return
        idx = ids.index(self.selected_anime_id)
        new_idx = idx + direction
        if new_idx < 0 or new_idx >= len(ids):
            return
        ids[idx], ids[new_idx] = ids[new_idx], ids[idx]
        for order, anime_id in enumerate(ids, start=1):
            update_anime_fields(self.conn, anime_id, sort_order=order)
        self.load_detail()

    def refresh_metadata(self) -> None:
        if self.selected_anime_id is None:
            return
        try:
            count = len(refresh_metadata_for_anime(self.conn, self.selected_anime_id, load_config()))
        except Exception as exc:
            messagebox.showwarning("Metadata refresh failed", str(exc))
            return
        messagebox.showinfo("Metadata refreshed", f"Stored {count} AniList candidate(s).")
        self.load_detail()

    def choose_match(self) -> None:
        if self.selected_anime_id is None:
            return
        matches = list(
            self.conn.execute(
                """
                SELECT * FROM metadata_matches
                WHERE anime_id = ? AND provider = 'anilist'
                ORDER BY confidence_score DESC, id
                """,
                (self.selected_anime_id,),
            )
        )
        if not matches:
            try:
                refresh_metadata_for_anime(self.conn, self.selected_anime_id, load_config())
            except Exception as exc:
                messagebox.showwarning("Metadata refresh failed", str(exc))
                return
            matches = list(
                self.conn.execute(
                    """
                    SELECT * FROM metadata_matches
                    WHERE anime_id = ? AND provider = 'anilist'
                    ORDER BY confidence_score DESC, id
                    """,
                    (self.selected_anime_id,),
                )
            )
        if not matches:
            messagebox.showinfo("AniList matches", "No matches stored.")
            return
        win = tk.Toplevel(self.root)
        win.title("Choose AniList Match")
        win.configure(bg=COLORS["bg"])
        win.geometry("640x360")
        box = tk.Listbox(
            win,
            bg=COLORS["panel"],
            fg=COLORS["text"],
            selectbackground=COLORS["accent"],
            selectforeground="#111111",
            relief="flat",
        )
        box.pack(fill="both", expand=True, padx=10, pady=10)
        for match in matches:
            payload = json.loads(match["payload_json"])
            title = (payload.get("title") or {}).get("userPreferred") or (payload.get("title") or {}).get("romaji")
            selected = "*" if match["selected"] else " "
            box.insert(tk.END, f"{selected} {match['confidence_score']:.2f}  {match['provider_media_id']}  {title}")

        def choose() -> None:
            selection = box.curselection()
            if not selection:
                return
            match = matches[selection[0]]
            try:
                select_match(self.conn, self.selected_anime_id, match["id"], AniListProvider(load_config().anilist))
            except Exception as exc:
                messagebox.showwarning("Match selection failed", str(exc))
                return
            win.destroy()
            self.load_detail()

        ttk.Button(win, text="Select", style="Accent.TButton", command=choose).pack(fill="x", padx=10, pady=(0, 10))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ani-watch-gui")
    parser.add_argument("--check", action="store_true", help="verify GUI dependencies without opening a window")
    parser.add_argument("--smoke-test", action="store_true", help="instantiate and exercise GUI pages, then exit")
    parser.add_argument("--action-smoke-test", action="store_true", help="exercise non-dialog GUI callbacks, then exit")
    args = parser.parse_args(argv)
    if args.check:
        print("tkinter: OK")
        if Image is None or ImageTk is None:
            print("Pillow: unavailable")
            return 1
        print("Pillow: OK")
        with initialize():
            pass
        print("database: OK")
        return 0
    root = tk.Tk()
    app = WatchlistApp(
        root,
        auto_discovery=not (args.smoke_test or args.action_smoke_test),
        check_updates=not (args.smoke_test or args.action_smoke_test),
    )
    if args.smoke_test or args.action_smoke_test:
        root.update_idletasks()
        app.refresh_library()
        if app.current_rows:
            app.open_detail(int(app.current_rows[0]["id"]))
            root.update_idletasks()
            app.load_detail()
            app.refresh_activity()
            if args.action_smoke_test:
                messagebox.showinfo = lambda *args, **kwargs: None
                messagebox.showwarning = lambda *args, **kwargs: None
                globals()["refresh_metadata_for_anime"] = lambda *args, **kwargs: []
                globals()["launch_episode"] = lambda *args, **kwargs: type(
                    "SmokeLaunchResult",
                    (),
                    {"used_terminal": True},
                )()
                app.detail_status.set(STATUS_LABELS["completed"])
                app.save_status()
                app.notes.delete("1.0", tk.END)
                app.notes.insert("1.0", "GUI action smoke test")
                app.save_notes()
                children = app.episode_tree.get_children()
                if children:
                    app.episode_tree.selection_set(children[0])
                    app.continue_selected_episode()
                    app.mark_selected_episode(True)
                    app.mark_selected_episode(False)
                app.refresh_metadata()
                app.choose_match()
        app.auto_refresh()
        root.update_idletasks()
        app.close_app()
        print("GUI action smoke test: OK" if args.action_smoke_test else "GUI smoke test: OK")
        return 0
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
