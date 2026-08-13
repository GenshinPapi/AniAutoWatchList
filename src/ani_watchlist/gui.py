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
from tkinter import filedialog, messagebox, simpledialog, ttk

try:
    from PIL import Image, ImageDraw, ImageTk
except Exception:  # pragma: no cover - optional GUI enhancement
    Image = None
    ImageDraw = None
    ImageTk = None

from .availability import refresh_available_episodes_for_anime
from .cloud import (
    CloudBackupError,
    GoogleDriveBackupProvider,
    load_cloud_backup_status,
    record_cloud_backup_status,
)
from .config import load_config, set_config_value
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
    AllAnimeRateLimitError,
    LaunchError,
    allanime_episode_available,
    choose_ani_cli_search_title,
    launch_episode,
    resolve_allanime_launch_target,
)
from .metadata import (
    BulkMetadataRefreshResult,
    refresh_all_metadata,
    refresh_metadata_for_anime,
    select_match,
    selected_metadata_payload,
    store_selected_metadata_payload,
)
from .paths import get_paths
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
from .transfer import (
    JSON_FILETYPES,
    XML_FILETYPES,
    WatchlistTransferError,
    anilist_mal_id_resolver,
    export_watchlist_text,
    import_watchlist_file,
    import_watchlist_text,
    write_auto_backup_files,
)
from .updater import UpdateInfo, check_ani_cli_update, check_for_update, launch_update, project_root
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

CLOUD_BUTTON_PRESENTATIONS = {
    "checking": ("Cloud ...", "CloudChecking.TMenubutton"),
    "connected": ("Cloud ✓", "CloudConnected.TMenubutton"),
    "disconnected": ("Cloud !", "CloudDisconnected.TMenubutton"),
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
PARTY_HOST_EVENT_POLL_MS = 1_000
PARTY_INITIAL_SYNC_TIMEOUT_SECONDS = 45.0
PARTY_FORCE_SYNC_TIMEOUT_SECONDS = 45.0
PARTY_FORCE_SYNC_INTERVAL_SECONDS = 1.25
PARTY_FORCE_SYNC_VERIFY_DELAY_SECONDS = 0.75
PARTY_FORCE_SYNC_TOLERANCE_SECONDS = 3.0
PARTY_HOST_END_NOTIFY_GRACE_SECONDS = 2.0
PARTY_HOST_MPV_OBSERVER_INTERVAL_SECONDS = 0.75
PARTY_HOST_MPV_SEEK_THRESHOLD_SECONDS = 3.0
PARTY_FULLSCREEN_OBSERVER_INTERVAL_SECONDS = 0.5
PARTY_SIDEBAR_WIDTH = 280
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


def cloud_button_presentation(state: str) -> tuple[str, str]:
    return CLOUD_BUTTON_PRESENTATIONS.get(state, CLOUD_BUTTON_PRESENTATIONS["disconnected"])


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
        self.metadata_refreshing = False
        self.cloud_operation_running = False
        self.cloud_connection_check_running = False
        self.cloud_connection_check_job: str | None = None
        self.cloud_connection_state = "checking"
        self.cloud_connection_error: str | None = None
        self.cloud_connection_checked_at: str | None = None
        self.search_loading = False
        self.search_results: list[dict[str, object]] = []
        self.search_result_query = ""
        self.search_error: str | None = None
        self.search_suggestions: list[dict[str, object]] = []
        self.search_suggestion_job: str | None = None
        self.library_filter_job: str | None = None
        self.search_suggestion_generation = 0
        self.search_generation = 0
        self.related_media_items: list[dict[str, object]] = []
        self.related_loading = False
        self.related_loaded = False
        self.related_error: str | None = None
        self.related_anilist_id: int | None = None
        self.related_columns = 1
        self.related_media_cache: dict[int, dict[str, object]] = {}
        self.related_render_signature: tuple[object, ...] | None = None
        self.detail_episode_signature: tuple[object, ...] | None = None
        self.activity_signature: tuple[object, ...] | None = None
        self.episode_availability_refreshing: set[int] = set()
        self.party_host_session = None
        self.party_client: WatchPartyRemoteClient | None = None
        self.party_join_polling = False
        self.party_playback_controller: MpvIpcController | None = None
        self.party_current_media: WatchPartyMedia | None = None
        self.party_window: tk.Toplevel | None = None
        self.party_header_frame: tk.Frame | None = None
        self.party_body_frame: tk.Frame | None = None
        self.party_sidebar_frame: tk.Frame | None = None
        self.party_video_panel: tk.Frame | None = None
        self.party_video_frame: tk.Frame | None = None
        self.party_link_var = tk.StringVar()
        self.party_username_var = tk.StringVar()
        self.party_chat_var = tk.StringVar()
        self.party_status_text = tk.StringVar(value="")
        self.party_host_username = "Host"
        self.party_user_colors: dict[str, str] = {}
        self.party_participant_list: tk.Listbox | None = None
        self.party_participant_ids: list[str] = []
        self.party_activity_text: tk.Text | None = None
        self.party_host_refresh_job: str | None = None
        self.party_host_state_sync_job: str | None = None
        self.party_host_event_job: str | None = None
        self.party_host_latest_sequence = 0
        self.party_host_observer_stop: threading.Event | None = None
        self.party_host_observer_thread: threading.Thread | None = None
        self.party_host_observer_ignore_until = 0.0
        self.party_force_sync_generation = 0
        self.party_fullscreen = False
        self.party_fullscreen_observer_stop: threading.Event | None = None
        self.party_fullscreen_observer_thread: threading.Thread | None = None
        self.party_mpv_fullscreen_state: bool | None = None
        self.card_widgets: dict[int, tk.Frame] = {}
        self.grid_columns = 1
        self.library_render_signature: tuple[object, ...] | None = None
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
        self.cloud_connection_check_job = self.root.after(500, self.start_google_drive_connection_check)
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
        style.configure("Compact.Accent.TButton", background=COLORS["accent"], foreground="#111111", padding=(6, 4))
        style.map("Compact.Accent.TButton", background=[("active", COLORS["accent_hover"])])
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
            "CloudChecking.TMenubutton",
            background=COLORS["panel_alt"],
            foreground=COLORS["text"],
            bordercolor=COLORS["border"],
            focusthickness=0,
            padding=(10, 6),
        )
        style.map("CloudChecking.TMenubutton", background=[("active", COLORS["border"])])
        style.configure(
            "CloudConnected.TMenubutton",
            background="#2e7d32",
            foreground="#ffffff",
            bordercolor="#43a047",
            focusthickness=0,
            padding=(10, 6),
        )
        style.map(
            "CloudConnected.TMenubutton",
            background=[("active", "#388e3c")],
            foreground=[("active", "#ffffff")],
        )
        style.configure(
            "CloudDisconnected.TMenubutton",
            background=COLORS["danger"],
            foreground="#ffffff",
            bordercolor="#e57373",
            focusthickness=0,
            padding=(10, 6),
        )
        style.map(
            "CloudDisconnected.TMenubutton",
            background=[("active", "#e05b5b")],
            foreground=[("active", "#ffffff")],
        )
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
        self.manual_update_button = tk.Button(
            self.nav_bar,
            text="Update App",
            command=self.prompt_managed_update,
            bg=COLORS["panel_alt"],
            fg=COLORS["text"],
            activebackground=COLORS["accent"],
            activeforeground="#111111",
            relief="flat",
            padx=14,
            pady=8,
            cursor="hand2",
        )
        self.manual_update_button.grid(row=0, column=6, padx=(8, 0), sticky="e")
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
        export_button = ttk.Menubutton(header, text="Export", style="Dark.TMenubutton")
        export_menu = tk.Menu(
            export_button,
            tearoff=False,
            bg=COLORS["panel_alt"],
            fg=COLORS["text"],
            activebackground=COLORS["accent"],
            activeforeground="#111111",
            relief="flat",
        )
        export_menu.add_command(label="Export JSON", command=lambda: self.export_watchlist("json"))
        export_menu.add_command(label="Export XML", command=lambda: self.export_watchlist("xml"))
        export_button.configure(menu=export_menu)
        export_button.grid(row=0, column=1, padx=(8, 0))
        import_button = ttk.Menubutton(header, text="Import", style="Dark.TMenubutton")
        import_menu = tk.Menu(
            import_button,
            tearoff=False,
            bg=COLORS["panel_alt"],
            fg=COLORS["text"],
            activebackground=COLORS["accent"],
            activeforeground="#111111",
            relief="flat",
        )
        import_menu.add_command(label="Import JSON", command=lambda: self.import_watchlist("json"))
        import_menu.add_command(label="Import XML", command=lambda: self.import_watchlist("xml"))
        import_menu.add_separator()
        import_menu.add_command(
            label="Import JSON from Google Drive",
            command=lambda: self.import_watchlist_from_google_drive("json"),
        )
        import_menu.add_command(
            label="Import XML from Google Drive",
            command=lambda: self.import_watchlist_from_google_drive("xml"),
        )
        import_button.configure(menu=import_menu)
        import_button.grid(row=0, column=2, padx=(8, 0))
        self.cloud_button = ttk.Menubutton(header, text="Cloud ...", style="CloudChecking.TMenubutton")
        cloud_menu = tk.Menu(
            self.cloud_button,
            tearoff=False,
            bg=COLORS["panel_alt"],
            fg=COLORS["text"],
            activebackground=COLORS["accent"],
            activeforeground="#111111",
            relief="flat",
        )
        cloud_menu.add_command(label="Connect Google Drive...", command=self.connect_google_drive)
        cloud_menu.add_command(
            label="Test Google Drive Connection",
            command=lambda: self.start_google_drive_connection_check(show_result=True),
        )
        cloud_menu.add_command(label="Back Up to Google Drive Now", command=self.start_google_drive_backup)
        cloud_menu.add_command(label="Google Drive Status", command=self.show_google_drive_status)
        cloud_menu.add_separator()
        cloud_menu.add_command(label="Disconnect Google Drive", command=self.disconnect_google_drive)
        if not GoogleDriveBackupProvider().has_builtin_client_config():
            cloud_menu.add_separator()
            cloud_menu.add_command(
                label="Developer: Configure Google OAuth...",
                command=self.choose_google_drive_client_config,
            )
        self.cloud_button.configure(menu=cloud_menu)
        self.cloud_button.grid(row=0, column=3, padx=(8, 0))
        cloud_token_saved = GoogleDriveBackupProvider().is_connected()
        self.set_cloud_connection_state(
            "checking" if cloud_token_saved else "disconnected",
            error=None if cloud_token_saved else "Google Drive is not connected.",
            checked=False,
        )
        ttk.Button(header, text="Add", style="Accent.TButton", command=self.add_anime).grid(row=0, column=4, padx=(8, 0))
        self.refresh_all_metadata_button = ttk.Button(
            header,
            text="Refresh Metadata",
            style="Dark.TButton",
            command=self.start_all_metadata_refresh,
        )
        self.refresh_all_metadata_button.grid(row=0, column=5, padx=(8, 0))
        ttk.Button(header, text="Refresh List", style="Dark.TButton", command=self.refresh_library).grid(
            row=0, column=6, padx=(8, 0)
        )

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
        self.search_entry.bind("<KeyRelease>", lambda _event: self.schedule_library_filter_refresh())
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
            "cloud_connection_check_job",
            "search_suggestion_job",
            "library_filter_job",
            "party_host_refresh_job",
            "party_host_state_sync_job",
            "party_host_event_job",
        ):
            job = getattr(self, attr, None)
            if job is not None:
                try:
                    self.root.after_cancel(job)
                except tk.TclError:
                    pass
                setattr(self, attr, None)
        self.backup_watchlist_on_exit()
        if self.party_client is not None:
            self.finish_joined_party("Watch party closed.", send_leave=True, show_message=False)
        self.stop_host_party_mpv_observer()
        if self.party_host_session is not None:
            session = self.party_host_session
            self.party_host_session = None
            try:
                session.close(notify_grace_seconds=0.25)
            except Exception:
                pass
        self.dismiss_idle_prompt()
        self.party_join_polling = False
        self.stop_party_fullscreen_observer()
        self.stop_party_playback()
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

    def backup_watchlist_on_exit(self) -> bool:
        try:
            targets = write_auto_backup_files(self.conn, project_root())
        except Exception as exc:
            self.log_auto_backup_failure(exc)
            return False
        if not load_config().cloud.google_drive_auto_backup:
            return True
        try:
            provider = GoogleDriveBackupProvider()
            if not provider.is_connected():
                raise CloudBackupError("Automatic cloud backup is enabled, but Google Drive is not connected.")
            result = provider.upload_backups(targets)
            record_cloud_backup_status(success=True, files=result.files)
        except Exception as exc:
            try:
                record_cloud_backup_status(success=False, error=str(exc))
            except OSError:
                pass
            self.log_auto_backup_failure(exc)
            return False
        return True

    def log_auto_backup_failure(self, error: Exception) -> None:
        try:
            paths = get_paths()
            paths.log_dir.mkdir(parents=True, exist_ok=True)
            timestamp = datetime.now(timezone.utc).astimezone().isoformat()
            with (paths.log_dir / "auto-backup-errors.log").open("a", encoding="utf-8") as handle:
                handle.write(f"{timestamp} {type(error).__name__}: {error}\n")
        except OSError:
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
                if focused is not self.notes and self.party_playback_controller is None:
                    self.load_detail()
                self.refresh_activity()
        finally:
            self.schedule_auto_refresh()

    def safe_focus_get(self):
        try:
            return self.root.focus_get()
        except (KeyError, tk.TclError):
            return None

    def schedule_library_filter_refresh(self) -> None:
        if self.shutting_down:
            return
        if self.library_filter_job is not None:
            try:
                self.root.after_cancel(self.library_filter_job)
            except tk.TclError:
                pass
            self.library_filter_job = None
        self.library_filter_job = self.root.after(SEARCH_DEBOUNCE_MS, self.run_library_filter_refresh)

    def run_library_filter_refresh(self) -> None:
        self.library_filter_job = None
        self.refresh_library()

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

    def cover_signature_marker(self, cover_path: object) -> object:
        if not isinstance(cover_path, str) or not cover_path:
            return cover_path
        cover_marker: object = cover_path
        try:
            cover_marker = (cover_path, os.path.getmtime(cover_path))
        except OSError:
            cover_marker = (cover_path, None)
        return cover_marker

    def library_row_signature(self, row) -> tuple[object, ...]:
        return (
            int(row["id"]),
            row["display_title"],
            row["status"],
            int(row["watched_count"] or 0),
            row["available_episode_count"],
            row["total_episodes"],
            row["last_watched_at"],
            self.cover_signature_marker(row["cover_path"]),
        )

    def render_grid(self) -> None:
        width = max(self.grid_canvas.winfo_width(), CARD_W)
        columns = max(1, width // CARD_W)
        signature = (columns, tuple(self.library_row_signature(row) for row in self.current_rows))
        if signature == self.library_render_signature and self.grid_frame.winfo_children():
            self.grid_columns = columns
            self._update_grid_scroll_region()
            return
        self.library_render_signature = signature
        for child in self.grid_frame.winfo_children():
            child.destroy()
        self.card_widgets.clear()
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
        text = f"Total {sum(counts.values())}   Watching {counts['watching']}   Watched eps {watched_episode_count(self.conn)}"
        if self.dashboard_label.cget("text") != text:
            self.dashboard_label.configure(text=text)

    def refresh_activity(self) -> None:
        if not hasattr(self, "activity_list"):
            return
        events = watch_events(self.conn, recent=12)
        signature = tuple(
            (
                int(event["id"]),
                event["created_at"],
                event["event_type"],
                event["anime_title"],
            )
            for event in events
        )
        if signature == self.activity_signature:
            return
        self.activity_signature = signature
        self.activity_list.delete(0, tk.END)
        for event in events:
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
            ani_cli_info: UpdateInfo | None = None
            try:
                info = check_for_update()
            except Exception:
                info = None
            try:
                ani_cli_info = check_ani_cli_update()
            except Exception:
                ani_cli_info = None
            self.run_on_ui(lambda app_info=info, bundled_info=ani_cli_info: self.finish_update_check(app_info, bundled_info))

        threading.Thread(target=worker, daemon=True).start()

    def update_details(self, label: str, info: UpdateInfo) -> str:
        local = info.local_commit[:7] if info.local_commit else info.local_version
        remote = info.remote_commit[:7] if info.remote_commit else info.remote_version or "latest"
        details = f"{label}\nCurrent: {info.local_version} ({local})\nLatest: {info.remote_version or 'unknown'} ({remote})"
        if info.remote_message:
            details += f"\nLatest commit: {info.remote_message}"
        return details

    def finish_update_check(self, info: UpdateInfo | None, ani_cli_info: UpdateInfo | None = None) -> None:
        self.update_checking = False
        app_update_available = info is not None and info.update_available
        ani_cli_update_available = ani_cli_info is not None and ani_cli_info.update_available
        if not app_update_available and not ani_cli_update_available:
            return

        sections: list[str] = []
        if app_update_available and info is not None:
            sections.append(self.update_details("AniAutoWatchList", info))
        if ani_cli_update_available and ani_cli_info is not None:
            sections.append(self.update_details("Bundled patched ani-cli", ani_cli_info))
            sections.append(
                "Use AniAutoWatchList updates for ani-cli changes. Direct ani-cli -U is disabled on the patched script so "
                "watchlist hooks, embedded-player flags, and bundled playback fixes stay installed."
            )
        details = "\n\n".join(sections)

        if not app_update_available:
            if not messagebox.askyesno(
                "ani-cli update available",
                f"A newer bundled patched ani-cli is available in AniAutoWatchList.\n\n{details}\n\n"
                "Update AniAutoWatchList now? This pulls the latest patched AniAutoWatchList release, "
                "which is how bundled ani-cli fixes are installed safely.",
            ):
                return
            self.launch_managed_update()
            return
        if not messagebox.askyesno("Update available", f"Updates are available.\n\n{details}\n\nUpdate AniAutoWatchList now?"):
            return
        self.launch_managed_update()

    def prompt_managed_update(self) -> None:
        if not messagebox.askyesno(
            "Update AniAutoWatchList",
            "Pull the latest AniAutoWatchList changes from GitHub and rerun the user installer now?\n\n"
            "Use this for bundled ani-cli fixes instead of running ani-cli -U directly.",
        ):
            return
        self.launch_managed_update()

    def launch_managed_update(self) -> None:
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

    def export_watchlist(self, export_format: str) -> None:
        extension = ".xml" if export_format == "xml" else ".json"
        filetypes = XML_FILETYPES if export_format == "xml" else JSON_FILETYPES
        target = filedialog.asksaveasfilename(
            parent=self.root,
            title=f"Export watchlist {export_format.upper()}",
            defaultextension=extension,
            filetypes=filetypes,
            initialfile=f"ani-watchlist-{datetime.now().strftime('%Y%m%d-%H%M%S')}{extension}",
        )
        if not target:
            return
        refresh_mal_ids = False
        skip_missing_mal_ids = False
        if export_format == "xml":
            refresh_mal_ids = messagebox.askyesno(
                "MAL-compatible XML",
                "MAL import requires update_on_import=1 and a real MAL AnimeDB ID for each entry.\n\n"
                "Look up missing MAL IDs from AniList and omit entries MAL cannot identify? "
                "This is recommended for MAL import. Use JSON for a full AniAutoWatchList backup.",
            )
            skip_missing_mal_ids = refresh_mal_ids
        self.start_watchlist_export(
            target,
            export_format,
            refresh_mal_ids=refresh_mal_ids,
            skip_missing_mal_ids=skip_missing_mal_ids,
        )

    def start_watchlist_export(
        self,
        target: str,
        export_format: str,
        *,
        refresh_mal_ids: bool = False,
        skip_missing_mal_ids: bool = False,
    ) -> None:
        self.dashboard_label.configure(text="Exporting watchlist...")

        def worker() -> None:
            try:
                with initialize() as conn:
                    resolver = anilist_mal_id_resolver(conn) if export_format == "xml" and refresh_mal_ids else None
                    text = export_watchlist_text(
                        conn,
                        "xml" if export_format == "xml" else "json",
                        mal_id_resolver=resolver,
                        skip_missing_mal_ids=skip_missing_mal_ids,
                    )
                with open(target, "w", encoding="utf-8") as handle:
                    handle.write(text)
            except (OSError, WatchlistTransferError) as exc:
                self.run_on_ui(lambda error=str(exc): self.finish_watchlist_export(target, error=error))
                return
            self.run_on_ui(lambda: self.finish_watchlist_export(target, error=None))

        threading.Thread(target=worker, daemon=True).start()

    def finish_watchlist_export(self, target: str, *, error: str | None) -> None:
        self.refresh_dashboard()
        if error:
            messagebox.showwarning("Watchlist export failed", error)
            return
        messagebox.showinfo("Watchlist exported", f"Exported watchlist to:\n{target}")

    def import_watchlist(self, import_format: str) -> None:
        filetypes = XML_FILETYPES if import_format == "xml" else JSON_FILETYPES
        source = filedialog.askopenfilename(
            parent=self.root,
            title=f"Import watchlist {import_format.upper()}",
            filetypes=filetypes,
        )
        if not source:
            return
        replace = messagebox.askyesnocancel(
            "Import watchlist",
            "Replace the current watchlist with this file?\n\n"
            "Yes: replace the current watchlist first.\n"
            "No: sync by adding only missing anime and leaving current entries unchanged.\n"
            "Cancel: do not import.",
        )
        if replace is None:
            return
        mode = "replace" if replace else "sync"
        try:
            result = import_watchlist_file(self.conn, source, import_format=import_format, mode=mode)
            merge_safe_duplicates(self.conn)
        except WatchlistTransferError as exc:
            messagebox.showwarning("Watchlist import failed", str(exc))
            return
        self.finish_watchlist_import(result)

    def finish_watchlist_import(self, result: dict[str, int]) -> None:
        self.selected_anime_id = None
        self.library_render_signature = None
        self.activity_signature = None
        self.show_library()
        imported = result["anime"] + result.get("updated_anime", 0)
        skipped = result.get("skipped_anime", 0)
        summary = f"Imported {imported} anime and {result['episodes']} episode row(s)."
        if skipped:
            summary += f"\nSkipped {skipped} existing anime."
        messagebox.showinfo("Watchlist imported", summary)

    def connect_google_drive(self) -> None:
        if self.cloud_operation_running:
            messagebox.showinfo("Google Drive", "A Google Drive operation is already running.")
            return
        provider = GoogleDriveBackupProvider()
        if not provider.has_client_config():
            if not messagebox.askyesno(
                "Google Drive publisher setup needed",
                "This development build does not yet include AniAutoWatchList's Google sign-in identity. "
                "Normal users will only need to sign in after the app maintainer bundles it.\n\n"
                "Configure a developer OAuth client for local testing now?",
            ):
                return
            if not self.choose_google_drive_client_config(show_success=False):
                return
        self.cloud_operation_running = True
        self.set_cloud_connection_state("checking", checked=False)
        self.dashboard_label.configure(text="Waiting for Google Drive sign-in...")

        def worker() -> None:
            try:
                provider.connect()
                error = None
            except Exception as exc:
                error = str(exc)
            self.run_on_ui(lambda: self.finish_google_drive_connect(error))

        threading.Thread(target=worker, daemon=True).start()

    def choose_google_drive_client_config(self, *, show_success: bool = True) -> bool:
        provider = GoogleDriveBackupProvider()
        if provider.is_connected():
            messagebox.showinfo(
                "Google Drive setup",
                "Disconnect Google Drive before changing its OAuth client configuration.",
            )
            return False
        if show_success or not provider.has_client_config():
            messagebox.showinfo(
                "Google Drive developer setup",
                "Choose a Desktop OAuth client JSON created by the AniAutoWatchList maintainer. "
                "This developer-only override is not required by end users after the app identity is bundled.",
            )
            source = filedialog.askopenfilename(
                parent=self.root,
                title="Choose Google Desktop OAuth client JSON",
                filetypes=JSON_FILETYPES,
            )
            if not source:
                return False
            try:
                provider.install_client_config(source)
            except CloudBackupError as exc:
                messagebox.showwarning("Google Drive setup failed", str(exc))
                return False
        if show_success:
            messagebox.showinfo("Google Drive setup", "The Google OAuth Desktop client configuration was saved.")
        return True

    def finish_google_drive_connect(self, error: str | None) -> None:
        self.cloud_operation_running = False
        self.refresh_dashboard()
        if error:
            self.set_cloud_connection_state("disconnected", error=error)
            messagebox.showwarning("Google Drive sign-in failed", error)
            return
        self.set_cloud_connection_state("checking", checked=False)
        set_config_value("cloud.google_drive_auto_backup", "true")
        messagebox.showinfo(
            "Google Drive connected",
            "Automatic JSON and XML cloud backups are enabled. An initial backup will run now.",
        )
        self.start_google_drive_backup()

    def start_google_drive_backup(self) -> None:
        if self.cloud_operation_running:
            messagebox.showinfo("Google Drive", "A Google Drive operation is already running.")
            return
        provider = GoogleDriveBackupProvider()
        if not provider.is_connected():
            if messagebox.askyesno("Google Drive", "Google Drive is not connected. Connect it now?"):
                self.connect_google_drive()
            return
        self.cloud_operation_running = True
        self.set_cloud_connection_state("checking", checked=False)
        self.dashboard_label.configure(text="Backing up watchlist locally and to Google Drive...")

        def worker() -> None:
            try:
                with initialize() as conn:
                    targets = write_auto_backup_files(conn, project_root())
                result = provider.upload_backups(targets)
                record_cloud_backup_status(success=True, files=result.files)
                error = None
            except Exception as exc:
                error = str(exc)
                try:
                    record_cloud_backup_status(success=False, error=error)
                except OSError:
                    pass
            self.run_on_ui(lambda: self.finish_google_drive_backup(error))

        threading.Thread(target=worker, daemon=True).start()

    def finish_google_drive_backup(self, error: str | None) -> None:
        self.cloud_operation_running = False
        self.refresh_dashboard()
        if error:
            self.set_cloud_connection_state("disconnected", error=error)
            messagebox.showwarning(
                "Google Drive backup failed",
                f"The local backup was kept, but the Google Drive backup failed:\n\n{error}",
            )
            return
        self.set_cloud_connection_state("connected")
        messagebox.showinfo(
            "Google Drive backup complete",
            "The JSON and XML backups are current locally and in Google Drive.",
        )

    def show_google_drive_status(self) -> None:
        provider = GoogleDriveBackupProvider()
        status = load_cloud_backup_status()
        state_labels = {
            "connected": "Good",
            "checking": "Checking now",
            "disconnected": "Disconnected or unavailable",
        }
        lines = [
            f"Connection: {state_labels.get(self.cloud_connection_state, 'Unknown')}",
            f"Automatic backup on exit: {'On' if load_config().cloud.google_drive_auto_backup else 'Off'}",
            f"App sign-in configuration: {provider.client_config_source() or 'Missing'}",
        ]
        if self.cloud_connection_checked_at:
            lines.append(f"Last connection test: {self.cloud_connection_checked_at}")
        if self.cloud_connection_error:
            lines.extend(("", f"Connection error: {self.cloud_connection_error}"))
        if status.get("last_success_at"):
            lines.append(f"Last successful cloud backup: {status['last_success_at']}")
        elif status.get("last_attempt_at"):
            lines.append("No successful cloud backup has been recorded yet.")
        else:
            lines.append("No cloud backup has been attempted yet.")
        if status.get("error"):
            lines.extend(("", f"Last error: {status['error']}"))
        messagebox.showinfo("Google Drive status", "\n".join(lines))

    def set_cloud_connection_state(
        self,
        state: str,
        *,
        error: str | None = None,
        checked: bool = True,
    ) -> None:
        self.cloud_connection_state = state if state in CLOUD_BUTTON_PRESENTATIONS else "disconnected"
        self.cloud_connection_error = error
        if checked:
            self.cloud_connection_checked_at = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
        text, style = cloud_button_presentation(self.cloud_connection_state)
        if hasattr(self, "cloud_button"):
            self.cloud_button.configure(text=text, style=style)

    def start_google_drive_connection_check(self, *, show_result: bool = False) -> None:
        self.cloud_connection_check_job = None
        if self.shutting_down or self.cloud_connection_check_running:
            return
        if self.cloud_operation_running:
            return
        provider = GoogleDriveBackupProvider()
        if not provider.is_connected():
            error = "Google Drive is not connected."
            self.set_cloud_connection_state("disconnected", error=error)
            if show_result:
                messagebox.showwarning("Google Drive connection", error)
            return
        self.cloud_connection_check_running = True
        self.set_cloud_connection_state("checking", checked=False)

        def worker() -> None:
            try:
                provider.test_connection()
                error = None
            except Exception as exc:
                error = str(exc)
            self.run_on_ui(lambda: self.finish_google_drive_connection_check(error, show_result=show_result))

        threading.Thread(target=worker, daemon=True).start()

    def finish_google_drive_connection_check(self, error: str | None, *, show_result: bool = False) -> None:
        self.cloud_connection_check_running = False
        if error:
            self.set_cloud_connection_state("disconnected", error=error)
            if show_result:
                messagebox.showwarning("Google Drive connection failed", error)
            return
        self.set_cloud_connection_state("connected")
        if show_result:
            messagebox.showinfo(
                "Google Drive connection",
                "Google Drive is connected and automatic token refresh is working.",
            )

    def disconnect_google_drive(self) -> None:
        if self.cloud_operation_running:
            messagebox.showinfo("Google Drive", "A Google Drive operation is already running.")
            return
        provider = GoogleDriveBackupProvider()
        if not provider.is_connected():
            set_config_value("cloud.google_drive_auto_backup", "false")
            self.set_cloud_connection_state("disconnected", error="Google Drive is not connected.")
            messagebox.showinfo("Google Drive", "Google Drive is already disconnected.")
            return
        if not messagebox.askyesno(
            "Disconnect Google Drive",
            "Stop automatic cloud backups and remove this app's saved Google sign-in?\n\n"
            "Existing backups in Google Drive will not be deleted.",
        ):
            return
        set_config_value("cloud.google_drive_auto_backup", "false")
        self.cloud_operation_running = True
        self.set_cloud_connection_state("checking", checked=False)
        self.dashboard_label.configure(text="Disconnecting Google Drive...")

        def worker() -> None:
            try:
                provider.disconnect()
                error = None
            except Exception as exc:
                error = str(exc)
            self.run_on_ui(lambda: self.finish_google_drive_disconnect(error))

        threading.Thread(target=worker, daemon=True).start()

    def finish_google_drive_disconnect(self, error: str | None) -> None:
        self.cloud_operation_running = False
        set_config_value("cloud.google_drive_auto_backup", "false")
        self.set_cloud_connection_state("disconnected", error=error)
        self.refresh_dashboard()
        if error:
            messagebox.showwarning("Google Drive disconnected", error)
            return
        messagebox.showinfo(
            "Google Drive disconnected",
            "Automatic cloud backups are off. Local automatic backups are unchanged.",
        )

    def import_watchlist_from_google_drive(self, import_format: str) -> None:
        if self.cloud_operation_running:
            messagebox.showinfo("Google Drive", "A Google Drive operation is already running.")
            return
        provider = GoogleDriveBackupProvider()
        if not provider.is_connected():
            if messagebox.askyesno("Google Drive", "Google Drive is not connected. Connect it now?"):
                self.connect_google_drive()
            return
        replace = messagebox.askyesnocancel(
            "Import cloud backup",
            "Replace the current watchlist with the Google Drive backup?\n\n"
            "Yes: replace the current watchlist first.\n"
            "No: sync by adding only missing anime and leaving current entries unchanged.\n"
            "Cancel: do not import.",
        )
        if replace is None:
            return
        mode = "replace" if replace else "sync"
        self.cloud_operation_running = True
        self.set_cloud_connection_state("checking", checked=False)
        self.dashboard_label.configure(text=f"Downloading {import_format.upper()} backup from Google Drive...")

        def worker() -> None:
            try:
                content = provider.download_backup(import_format)
                error = None
            except Exception as exc:
                content = None
                error = str(exc)
            self.run_on_ui(
                lambda: self.finish_google_drive_import(
                    content,
                    import_format=import_format,
                    mode=mode,
                    error=error,
                )
            )

        threading.Thread(target=worker, daemon=True).start()

    def finish_google_drive_import(
        self,
        content: str | None,
        *,
        import_format: str,
        mode: str,
        error: str | None,
    ) -> None:
        self.cloud_operation_running = False
        self.refresh_dashboard()
        if error or content is None:
            self.set_cloud_connection_state("disconnected", error=error or "The cloud backup was empty.")
            messagebox.showwarning("Google Drive import failed", error or "The cloud backup was empty.")
            return
        self.set_cloud_connection_state("connected")
        try:
            result = import_watchlist_text(self.conn, content, import_format, mode=mode)
            merge_safe_duplicates(self.conn)
        except WatchlistTransferError as exc:
            messagebox.showwarning("Google Drive import failed", str(exc))
            return
        self.finish_watchlist_import(result)

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
        width = max(self.detail_canvas.winfo_width(), DISCOVERY_GRID_W)
        columns = max(1, width // DISCOVERY_GRID_W)
        item_signature = []
        for item in self.related_media_items:
            next_ep = item.get("next_airing_episode") if isinstance(item.get("next_airing_episode"), dict) else {}
            item_signature.append(
                (
                    item.get("id"),
                    item.get("display_title"),
                    item.get("relation_label"),
                    item.get("average_score"),
                    item.get("trending"),
                    item.get("status"),
                    next_ep.get("episode") if isinstance(next_ep, dict) else None,
                    self.cover_signature_marker(item.get("cover_path")),
                )
            )
        signature = (
            columns,
            self.related_loading,
            self.related_error,
            self.related_anilist_id,
            tuple(item_signature),
        )
        if signature == self.related_render_signature and self.related_frame.winfo_children():
            self.related_columns = columns
            self._update_detail_scroll_region()
            return
        self.related_render_signature = signature
        for child in self.related_frame.winfo_children():
            child.destroy()
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
            if hasattr(self, "related_frame") and not self.related_frame.winfo_children():
                self.render_related_media()
            return
        cached_payload = self.related_media_cache.get(media_id_int)
        if cached_payload is not None:
            self.related_anilist_id = media_id_int
            self.related_loading = False
            self.related_loaded = True
            self.related_error = str(cached_payload.get("error")) if cached_payload.get("error") else None
            self.related_media_items = [item for item in list(cached_payload.get("items") or []) if isinstance(item, dict)]
            self.render_related_media()
            return
        self.related_anilist_id = media_id_int
        self.related_media_items = []
        self.related_error = None
        self.related_loading = True
        self.related_loaded = False
        self.render_related_media()

        def worker() -> None:
            payload = related_media(media_id_int, load_config(), cache_covers=True, max_depth=3, max_items=16)
            self.run_on_ui(lambda: self.finish_related_media_refresh(media_id_int, payload))

        threading.Thread(target=worker, daemon=True).start()

    def finish_related_media_refresh(self, media_id: int, payload: dict[str, object]) -> None:
        if self.related_anilist_id != media_id:
            return
        self.related_loading = False
        self.related_loaded = True
        self.related_error = str(payload.get("error")) if payload.get("error") else None
        self.related_media_items = [item for item in list(payload.get("items") or []) if isinstance(item, dict)]
        self.related_media_cache[media_id] = payload
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
        episode_signature = (
            int(anime["id"]),
            tuple(
                (
                    episode["episode_key"],
                    int(episode["watched"] or 0),
                    episode["last_started_at"],
                    episode["watched_at"],
                )
                for episode in episodes
            )
        )
        if episode_signature != self.detail_episode_signature:
            self.detail_episode_signature = episode_signature
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
            except AllAnimeRateLimitError as exc:
                self.launch_label.configure(text=str(exc), fg=COLORS["danger"])
                messagebox.showwarning("AllAnime rate limit", str(exc))
                raise
            except LaunchError:
                return None

        try:
            target = resolve_target(launch_mode)
            if launch_mode == "dub" and target is None:
                target = resolve_target("sub")
        except AllAnimeRateLimitError:
            return
        if target is None and (metadata_payload_is_adult(metadata_payload) or title_has_adult_label(anime["display_title"])):
            message = (
                f"AllAnime did not return a launchable listing for {title_for_message}. "
                "ani-cli still needs an AllAnime listing before resolving playback."
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
        except AllAnimeRateLimitError as exc:
            self.launch_label.configure(text=str(exc), fg=COLORS["danger"])
            messagebox.showwarning("AllAnime rate limit", str(exc))
            return
        except LaunchError:
            target = None
        if target is None and (metadata_payload_is_adult(metadata_payload) or title_has_adult_label(anime["display_title"])):
            message = (
                f"AllAnime did not return a launchable listing for {title_for_message}. "
                "Watch parties still need an AllAnime listing before resolving playback."
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
        self.party_host_session = session
        self.party_client = None
        self.party_join_polling = False
        self.party_playback_controller = None
        self.party_current_media = media
        self.party_link_var.set(session.share_url)
        tunnel_note = "Public tunnel active." if session.public else f"Public tunnel unavailable: {session.tunnel_error or 'unknown error'}"
        self.party_status_text.set(tunnel_note)
        self.show_host_party_window()
        ipc_path = party_ipc_path(f"host-{session.room.room_id}")
        try:
            launch_episode(
                launch_title,
                episode,
                mode=mode,
                allanime_id=target.show_id if target is not None else None,
                mpv_ipc_path=ipc_path,
                mpv_wid=self.party_embed_window_id(),
                prefer_terminal=False,
                detach_player=True,
                quiet=True,
            )
        except LaunchError as exc:
            session.close()
            self.party_host_session = None
            self.party_current_media = None
            self.destroy_party_window()
            messagebox.showwarning("ani-cli launch failed", str(exc))
            return
        self.party_playback_controller = MpvIpcController(ipc_path)
        self.launch_label.configure(text=f"Watch party started for episode {episode}. {tunnel_note}", fg=COLORS["muted"])
        self.publish_host_party_state()
        self.start_party_fullscreen_observer()
        self.start_host_party_mpv_observer()
        self.schedule_host_party_state_sync()
        self.schedule_host_party_refresh()

    def join_watch_party(self) -> None:
        link = simpledialog.askstring("Join watch party", "Paste the watch party link:")
        if not link:
            return
        username = simpledialog.askstring("Join watch party", "Your display name:", initialvalue="Guest")
        if not username:
            return
        try:
            client = WatchPartyRemoteClient(link, username)
            payload = client.join()
        except WatchPartyError as exc:
            messagebox.showwarning("Join watch party failed", str(exc))
            return
        state = payload.get("state") if isinstance(payload.get("state"), dict) else {}
        media_payload = state.get("media") if isinstance(state.get("media"), dict) else {}
        media = WatchPartyMedia.from_json(media_payload)
        self.party_client = client
        self.party_join_polling = False
        self.party_host_session = None
        self.party_playback_controller = None
        self.party_current_media = media
        self.party_username_var.set(username)
        self.party_status_text.set(f"Joined {media.party_title}. Waiting for host controls.")
        self.show_joined_party_window(state)
        ipc_path = party_ipc_path(f"join-{client.participant_id or 'guest'}")
        try:
            self.launch_party_media(media, ipc_path, embed_wid=self.party_embed_window_id())
        except LaunchError as exc:
            try:
                client.leave()
            except WatchPartyError:
                pass
            self.party_client = None
            self.destroy_party_window()
            messagebox.showwarning("ani-cli launch failed", str(exc))
            return
        self.party_playback_controller = MpvIpcController(ipc_path)
        self.party_join_polling = True
        self.start_party_fullscreen_observer()
        self.start_party_initial_sync(state)
        self.start_party_event_poll()

    def launch_party_media(self, media: WatchPartyMedia, ipc_path: str, *, embed_wid: int | None = None) -> None:
        launch_title = media.allanime_title or media.anime_title
        launch_episode(
            launch_title,
            media.episode,
            mode=media.mode,
            allanime_id=media.allanime_id,
            mpv_ipc_path=ipc_path,
            mpv_wid=embed_wid,
            prefer_terminal=False,
            detach_player=True,
            quiet=True,
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
        self.start_party_force_sync("join", state=state)

    def apply_party_playback_state(self, playback_state: dict[str, object], *, wait_for_socket: bool = False) -> None:
        controller = self.party_playback_controller
        if controller is None:
            return

        def worker() -> None:
            deadline = monotonic() + PARTY_INITIAL_SYNC_TIMEOUT_SECONDS
            while wait_for_socket and monotonic() < deadline and not controller.available():
                sleep(0.5)
            try:
                target_position, paused = self.apply_party_playback_state_to_controller(controller, playback_state)
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

    def apply_party_playback_state_to_controller(
        self,
        controller: MpvIpcController,
        playback_state: dict[str, object],
    ) -> tuple[float, bool]:
        target_position = self.playback_state_target_position(playback_state)
        paused = bool(playback_state.get("paused"))
        if paused:
            controller.pause()
        controller.seek(target_position)
        if paused:
            controller.pause()
        else:
            controller.play()
        return target_position, paused

    def start_party_force_sync(self, reason: str, *, state: dict[str, object] | None = None) -> None:
        client = self.party_client
        controller = self.party_playback_controller
        media = self.party_current_media
        if client is None or controller is None or media is None:
            return
        self.party_force_sync_generation += 1
        generation = self.party_force_sync_generation
        initial_state = state if isinstance(state, dict) else None
        self.party_status_text.set("Syncing to host...")

        def worker() -> None:
            deadline = monotonic() + PARTY_FORCE_SYNC_TIMEOUT_SECONDS
            next_state = initial_state
            last_error = ""
            while monotonic() < deadline:
                if not self.party_force_sync_active(generation, client, controller):
                    return
                if not controller.available():
                    sleep(0.35)
                    continue
                try:
                    used_initial_state = next_state is not None
                    current_state = next_state if next_state is not None else client.fetch_state()
                    next_state = None
                except WatchPartyError as exc:
                    last_error = str(exc)
                    sleep(PARTY_FORCE_SYNC_INTERVAL_SECONDS)
                    continue
                if not isinstance(current_state, dict):
                    sleep(PARTY_FORCE_SYNC_INTERVAL_SECONDS)
                    continue
                if not self.party_state_matches_current_media(current_state, media):
                    sleep(PARTY_FORCE_SYNC_INTERVAL_SECONDS)
                    continue
                playback_state = current_state.get("playback_state") if isinstance(current_state.get("playback_state"), dict) else None
                if playback_state is None:
                    sleep(PARTY_FORCE_SYNC_INTERVAL_SECONDS)
                    continue
                try:
                    target_position, paused = self.apply_party_playback_state_to_controller(controller, playback_state)
                except Exception as exc:
                    last_error = str(exc)
                    sleep(PARTY_FORCE_SYNC_INTERVAL_SECONDS)
                    continue
                sleep(PARTY_FORCE_SYNC_VERIFY_DELAY_SECONDS)
                if not self.party_force_sync_active(generation, client, controller):
                    return
                if (
                    not used_initial_state
                    and not playback_state.get("sync_pending")
                    and self.party_playback_is_close_to_host(controller, playback_state)
                ):
                    state_text = "paused" if paused else "playing"
                    self.run_on_ui(
                        lambda pos=target_position, status=state_text: self.party_status_text.set(
                            f"Synced to host at {int(pos // 60)}:{int(pos % 60):02d} ({status})."
                        )
                    )
                    return
                sleep(PARTY_FORCE_SYNC_INTERVAL_SECONDS)
            if self.party_force_sync_active(generation, client, controller):
                message = f"Host sync timed out{(': ' + last_error) if last_error else '.'}"
                self.run_on_ui(lambda value=message: self.party_status_text.set(value))

        threading.Thread(target=worker, daemon=True).start()

    def party_force_sync_active(
        self,
        generation: int,
        client: WatchPartyRemoteClient,
        controller: MpvIpcController,
    ) -> bool:
        return (
            self.party_force_sync_generation == generation
            and self.party_client is client
            and self.party_playback_controller is controller
            and self.party_join_polling
        )

    def party_state_matches_current_media(self, state: dict[str, object], media: WatchPartyMedia) -> bool:
        media_payload = state.get("media") if isinstance(state.get("media"), dict) else {}
        state_episode = str(media_payload.get("episode") or "")
        if state_episode and state_episode != str(media.episode):
            return False
        playback_state = state.get("playback_state") if isinstance(state.get("playback_state"), dict) else {}
        playback_episode = str(playback_state.get("episode") or "")
        return not playback_episode or playback_episode == str(media.episode)

    def party_playback_is_close_to_host(
        self,
        controller: MpvIpcController,
        playback_state: dict[str, object],
    ) -> bool:
        position = controller.time_position()
        if position is None:
            return False
        expected = self.playback_state_target_position(playback_state)
        if abs(position - expected) > PARTY_FORCE_SYNC_TOLERANCE_SECONDS:
            return False
        host_paused = bool(playback_state.get("paused"))
        local_paused = controller.get_property("pause")
        return local_paused is None or bool(local_paused) == host_paused

    def build_party_activity_panel(self, parent: tk.Widget, *, row: int, column: int) -> tk.Frame:
        panel = tk.Frame(parent, bg=COLORS["panel"], highlightthickness=1, highlightbackground=COLORS["border"])
        panel.grid(row=row, column=column, sticky="nsew", padx=0, pady=(8, 0))
        panel.columnconfigure(0, weight=1)
        panel.rowconfigure(1, weight=1)
        tk.Label(panel, text="Activity & Chat", bg=COLORS["panel"], fg=COLORS["text"], font=("", 11, "bold")).grid(
            row=0, column=0, sticky="w", padx=8, pady=(8, 3)
        )
        activity = tk.Text(
            panel,
            bg=COLORS["entry"],
            fg=COLORS["text"],
            insertbackground=COLORS["text"],
            relief="flat",
            wrap="word",
            height=14,
            state="disabled",
        )
        activity.grid(row=1, column=0, sticky="nsew", padx=8, pady=(0, 6))
        activity.tag_configure("party_time", foreground=COLORS["muted"])
        activity.tag_configure("party_system", foreground=COLORS["muted"])
        self.party_activity_text = activity
        chat_row = tk.Frame(panel, bg=COLORS["panel"])
        chat_row.grid(row=2, column=0, sticky="ew", padx=8, pady=(0, 8))
        chat_row.columnconfigure(0, weight=1)
        entry = tk.Entry(
            chat_row,
            textvariable=self.party_chat_var,
            bg=COLORS["entry"],
            fg=COLORS["text"],
            insertbackground=COLORS["text"],
            relief="flat",
        )
        entry.grid(row=0, column=0, sticky="ew", ipady=6)
        entry.bind("<Return>", lambda _event: self.send_party_chat_message())
        ttk.Button(chat_row, text="Send", style="Compact.Accent.TButton", command=self.send_party_chat_message).grid(
            row=0, column=1, padx=(6, 0)
        )
        return panel

    def build_party_video_panel(self, parent: tk.Widget, *, row: int, column: int) -> tk.Frame:
        panel = tk.Frame(parent, bg="#000000", highlightthickness=1, highlightbackground=COLORS["border"])
        panel.grid(row=row, column=column, sticky="nsew", padx=(0, 12), pady=0)
        panel.columnconfigure(0, weight=1)
        panel.rowconfigure(0, weight=1)
        self.party_video_panel = panel
        self.party_video_frame = tk.Frame(panel, bg="#000000", width=960, height=540)
        self.party_video_frame.grid(row=0, column=0, sticky="nsew")
        self.party_video_frame.grid_propagate(False)
        self.party_video_frame.bind("<Double-Button-1>", lambda _event: self.toggle_party_fullscreen())
        return panel

    def party_embed_window_id(self) -> int | None:
        if os.environ.get("ANI_WATCH_PARTY_DISABLE_EMBED") == "1":
            return None
        frame = self.party_video_frame
        if frame is None:
            return None
        try:
            if str(self.root.tk.call("tk", "windowingsystem")).casefold() != "x11":
                return None
            frame.update_idletasks()
            window_id = int(frame.winfo_id())
        except (tk.TclError, TypeError, ValueError):
            return None
        return window_id if window_id > 0 else None

    def build_party_participants_panel(self, parent: tk.Widget, *, row: int, column: int, host: bool) -> tk.Frame:
        panel = tk.Frame(parent, bg=COLORS["panel"], highlightthickness=1, highlightbackground=COLORS["border"])
        panel.grid(row=row, column=column, sticky="nsew", padx=0, pady=(8, 0))
        panel.columnconfigure(0, weight=1)
        panel.rowconfigure(1, weight=1)
        tk.Label(panel, text="Participants", bg=COLORS["panel"], fg=COLORS["text"], font=("", 11, "bold")).grid(
            row=0, column=0, sticky="w", padx=8, pady=(8, 3)
        )
        self.party_participant_list = tk.Listbox(
            panel,
            bg=COLORS["panel"],
            fg=COLORS["text"],
            selectbackground=COLORS["accent"],
            selectforeground="#111111",
            relief="flat",
            highlightthickness=0,
            height=7,
        )
        self.party_participant_list.grid(row=1, column=0, sticky="nsew", padx=8, pady=(0, 6))
        if host:
            action_row = tk.Frame(panel, bg=COLORS["panel"])
            action_row.grid(row=2, column=0, sticky="ew", padx=8, pady=(0, 8))
            ttk.Button(action_row, text="Refresh", style="Compact.Dark.TButton", command=self.render_host_party_participants).grid(
                row=0, column=0, padx=(0, 6)
            )
            ttk.Button(action_row, text="Kick", style="Compact.Dark.TButton", command=self.kick_selected_party_participant).grid(
                row=0, column=1
            )
        return panel

    def seed_party_activity_from_state(self, state: dict[str, object]) -> None:
        self.clear_party_activity()
        self.party_user_colors = {}
        self.remember_party_state_colors(state)
        recent_events = state.get("recent_events") if isinstance(state.get("recent_events"), list) else []
        latest_sequence = 0
        for event in recent_events:
            if not isinstance(event, dict):
                continue
            latest_sequence = max(latest_sequence, int(event.get("sequence") or 0))
            self.append_party_activity_event(event)
        if self.party_host_session is not None:
            self.party_host_latest_sequence = max(latest_sequence, int(state.get("latest_sequence") or 0))

    def clear_party_activity(self) -> None:
        if self.party_activity_text is None:
            return
        try:
            self.party_activity_text.configure(state="normal")
            self.party_activity_text.delete("1.0", tk.END)
            self.party_activity_text.configure(state="disabled")
        except tk.TclError:
            pass

    def append_party_activity(self, message: str) -> None:
        if self.party_activity_text is None or not message:
            return
        self.append_party_activity_segments([(message.rstrip(), None)])

    def append_party_activity_segments(self, segments: list[tuple[str, str | None]]) -> None:
        if self.party_activity_text is None or not segments:
            return
        try:
            self.party_activity_text.configure(state="normal")
            for text, tag in segments:
                if not text:
                    continue
                if tag:
                    self.party_activity_text.insert(tk.END, text, tag)
                else:
                    self.party_activity_text.insert(tk.END, text)
            self.party_activity_text.insert(tk.END, "\n")
            self.party_activity_text.see(tk.END)
            self.party_activity_text.configure(state="disabled")
        except tk.TclError:
            pass

    def append_party_activity_event(self, event: dict[str, object]) -> None:
        segments = self.party_event_segments(event)
        if segments:
            self.append_party_activity_segments(segments)

    def apply_party_activity_events(self, events: list[dict[str, object]]) -> None:
        for event in events:
            self.append_party_activity_event(event)

    def party_event_message(self, event: dict[str, object]) -> str:
        return "".join(text for text, _tag in self.party_event_segments(event))

    def party_event_segments(self, event: dict[str, object]) -> list[tuple[str, str | None]]:
        event_type = str(event.get("event_type") or "")
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        prefix = self.party_event_prefix(event)
        if event_type == "party_started":
            return [*prefix, *self.party_user_segments(payload, host=True, default_name="Host"), (" started the party.", None)]
        if event_type == "participant_joined":
            participant = payload.get("participant") if isinstance(payload.get("participant"), dict) else {}
            return [*prefix, *self.party_user_segments(participant, default_name="Guest"), (" joined.", None)]
        if event_type == "participant_left":
            participant = payload.get("participant") if isinstance(payload.get("participant"), dict) else {}
            return [*prefix, *self.party_user_segments(participant, default_name="Guest"), (" left.", None)]
        if event_type == "participant_updated":
            participant = payload.get("participant") if isinstance(payload.get("participant"), dict) else {}
            return [*prefix, *self.party_user_segments(participant, default_name="Guest"), (" updated their name.", None)]
        if event_type == "participant_kicked":
            participant = payload.get("participant") if isinstance(payload.get("participant"), dict) else {}
            return [*prefix, *self.party_user_segments(participant, default_name="Guest"), (" was removed.", None)]
        if event_type == "chat_message":
            user = self.party_user_segments(payload, host=bool(payload.get("host")), default_name="Guest")
            suffix = " (host)" if payload.get("host") else ""
            if suffix and user:
                user = [(user[0][0] + suffix, user[0][1])]
            return [*prefix, *user, (f": {payload.get('message') or ''}", None)]
        if event_type == "play":
            return [*prefix, *self.party_user_segments(payload, host=True, default_name="Host"), (" resumed playback.", None)]
        if event_type == "pause":
            return [*prefix, *self.party_user_segments(payload, host=True, default_name="Host"), (" paused playback.", None)]
        if event_type == "seek":
            position = self.party_event_position(payload)
            return [*prefix, *self.party_user_segments(payload, host=True, default_name="Host"), (f" jumped to {position}.", None)]
        if event_type == "relative_seek":
            delta = int(float(payload.get("delta_seconds") or 0))
            return [*prefix, *self.party_user_segments(payload, host=True, default_name="Host"), (f" skipped {delta:+d}s.", None)]
        if event_type == "next_episode":
            return [
                *prefix,
                *self.party_user_segments(payload, host=True, default_name="Host"),
                (f" moved to episode {payload.get('episode') or ''}.", None),
            ]
        if event_type == "previous_episode":
            return [
                *prefix,
                *self.party_user_segments(payload, host=True, default_name="Host"),
                (f" moved to episode {payload.get('episode') or ''}.", None),
            ]
        if event_type == "party_ended":
            return [*prefix, ("Party ended.", "party_system")]
        return []

    def party_event_prefix(self, event: dict[str, object]) -> list[tuple[str, str | None]]:
        stamp = self.party_event_time(str(event.get("created_at") or ""))
        return [(f"[{stamp}] ", "party_time")] if stamp else []

    def party_event_time(self, value: str) -> str:
        parsed = self.parse_party_timestamp(value)
        if parsed is None:
            return ""
        return parsed.astimezone().strftime("%H:%M")

    def party_user_segments(
        self,
        payload: dict[str, object],
        *,
        host: bool = False,
        default_name: str = "Guest",
    ) -> list[tuple[str, str | None]]:
        name = str(payload.get("username") or payload.get("host_username") or default_name)
        participant_id = str(payload.get("participant_id") or "")
        color = str(payload.get("color") or payload.get("host_color") or "")
        participant = payload.get("participant") if isinstance(payload.get("participant"), dict) else {}
        if participant:
            name = str(participant.get("username") or name)
            participant_id = str(participant.get("participant_id") or participant_id)
            color = str(participant.get("color") or color)
        if host:
            name = str(payload.get("host_username") or payload.get("username") or self.party_host_username or default_name)
        key = "host" if host else f"participant:{participant_id}" if participant_id else f"name:{name.casefold()}"
        if color:
            self.party_user_colors[key] = color
        color = self.party_user_colors.get(key) or color or COLORS["accent"]
        return [(name, self.party_color_tag(color))]

    def party_color_tag(self, color: str) -> str:
        text = self.party_activity_text
        cleaned = color if re.fullmatch(r"#[0-9A-Fa-f]{6}", str(color)) else COLORS["accent"]
        tag = f"party_user_{cleaned.lstrip('#').casefold()}"
        if text is not None:
            try:
                text.tag_configure(tag, foreground=cleaned)
            except tk.TclError:
                pass
        return tag

    def remember_party_state_colors(self, state: dict[str, object]) -> None:
        host_username = str(state.get("host_username") or "Host")
        self.party_host_username = host_username
        host_color = str(state.get("host_color") or "")
        if host_color:
            self.party_user_colors["host"] = host_color
        participants = state.get("participants") if isinstance(state.get("participants"), list) else []
        for participant in participants:
            if not isinstance(participant, dict):
                continue
            participant_id = str(participant.get("participant_id") or "")
            color = str(participant.get("color") or "")
            if participant_id and color:
                self.party_user_colors[f"participant:{participant_id}"] = color

    def party_event_position(self, payload: dict[str, object]) -> str:
        playback_state = payload.get("playback_state") if isinstance(payload.get("playback_state"), dict) else {}
        raw_position = playback_state.get("position_seconds") if playback_state else payload.get("position_seconds")
        try:
            position = max(0, int(float(raw_position or 0)))
        except (TypeError, ValueError):
            position = 0
        return f"{position // 60}:{position % 60:02d}"

    def build_party_header(self, win: tk.Toplevel, *, heading: str, subheading: str | None, host: bool) -> None:
        header = tk.Frame(win, bg=COLORS["bg"])
        header.grid(row=0, column=0, sticky="ew", padx=10, pady=(8, 5))
        header.columnconfigure(0, weight=1)
        header.columnconfigure(1, weight=0)
        self.party_header_frame = header
        title_box = tk.Frame(header, bg=COLORS["bg"])
        title_box.grid(row=0, column=0, sticky="ew", padx=(0, 10))
        title_box.columnconfigure(0, weight=1)
        tk.Label(title_box, text=heading, bg=COLORS["bg"], fg=COLORS["text"], font=("", 14, "bold"), anchor="w").grid(
            row=0, column=0, sticky="ew"
        )
        if subheading:
            tk.Label(title_box, text=subheading, bg=COLORS["bg"], fg=COLORS["muted"], anchor="w").grid(
                row=1, column=0, sticky="ew"
            )
        else:
            tk.Label(title_box, textvariable=self.party_status_text, bg=COLORS["bg"], fg=COLORS["muted"], anchor="w").grid(
                row=1, column=0, sticky="ew"
            )
        if host:
            link_row = tk.Frame(header, bg=COLORS["bg"])
            link_row.grid(row=0, column=1, sticky="e")
            link_row.columnconfigure(0, minsize=420)
            tk.Entry(
                link_row,
                textvariable=self.party_link_var,
                bg=COLORS["entry"],
                fg=COLORS["text"],
                insertbackground=COLORS["text"],
                relief="flat",
                width=48,
            ).grid(row=0, column=0, sticky="ew", ipady=4)
            ttk.Button(link_row, text="Copy Link", style="Compact.Dark.TButton", command=self.copy_party_link).grid(
                row=0, column=1, padx=(6, 0)
            )

    def build_party_body(self, win: tk.Toplevel) -> tuple[tk.Frame, tk.Frame]:
        body = tk.Frame(win, bg=COLORS["bg"])
        body.grid(row=1, column=0, sticky="nsew", padx=10, pady=(0, 10))
        body.columnconfigure(0, weight=1)
        body.columnconfigure(1, weight=0, minsize=PARTY_SIDEBAR_WIDTH)
        body.rowconfigure(0, weight=1)
        self.party_body_frame = body
        self.build_party_video_panel(body, row=0, column=0)
        side = tk.Frame(body, bg=COLORS["bg"], width=PARTY_SIDEBAR_WIDTH)
        side.grid(row=0, column=1, sticky="nsew")
        side.grid_propagate(False)
        side.columnconfigure(0, weight=1)
        side.rowconfigure(1, weight=3)
        side.rowconfigure(2, weight=1)
        self.party_sidebar_frame = side
        return body, side

    def configure_party_fullscreen_bindings(self) -> None:
        win = self.party_window
        if win is None:
            return
        win.bind("<F11>", lambda _event: self.toggle_party_fullscreen())
        win.bind("<Escape>", lambda _event: self.set_party_fullscreen(False))

    def toggle_party_fullscreen(self) -> None:
        self.set_party_fullscreen(not self.party_fullscreen)

    def set_party_fullscreen(self, enabled: bool, *, update_mpv: bool = True) -> None:
        win = self.party_window
        if win is None:
            return
        enabled = bool(enabled)
        if self.party_fullscreen == enabled:
            return
        self.party_fullscreen = enabled
        header = self.party_header_frame
        body = self.party_body_frame
        sidebar = self.party_sidebar_frame
        video_panel = self.party_video_panel
        try:
            win.attributes("-fullscreen", enabled)
        except tk.TclError:
            pass
        if enabled:
            win.configure(bg="#000000")
            if header is not None:
                header.grid_remove()
            if sidebar is not None:
                sidebar.grid_remove()
            if body is not None:
                body.grid_configure(padx=0, pady=0)
                body.columnconfigure(1, minsize=0)
            if video_panel is not None:
                video_panel.grid_configure(padx=0, pady=0)
        else:
            win.configure(bg=COLORS["bg"])
            if header is not None:
                header.grid()
            if sidebar is not None:
                sidebar.grid()
            if body is not None:
                body.grid_configure(padx=10, pady=(0, 10))
                body.columnconfigure(1, minsize=PARTY_SIDEBAR_WIDTH)
            if video_panel is not None:
                video_panel.grid_configure(padx=(0, 12), pady=0)
        if update_mpv and not enabled and self.party_playback_controller is not None:
            try:
                self.party_playback_controller.set_fullscreen(False)
                self.party_mpv_fullscreen_state = False
            except Exception:
                pass

    def start_party_fullscreen_observer(self) -> None:
        self.stop_party_fullscreen_observer()
        controller = self.party_playback_controller
        if controller is None:
            return
        stop_event = threading.Event()
        self.party_fullscreen_observer_stop = stop_event
        self.party_mpv_fullscreen_state = None

        def worker() -> None:
            while not stop_event.is_set():
                if not controller.available():
                    stop_event.wait(0.5)
                    continue
                try:
                    fullscreen = controller.get_property("fullscreen")
                except Exception:
                    fullscreen = None
                if fullscreen is not None:
                    enabled = bool(fullscreen)
                    if self.party_mpv_fullscreen_state is None:
                        self.party_mpv_fullscreen_state = enabled
                    elif enabled != self.party_mpv_fullscreen_state:
                        self.party_mpv_fullscreen_state = enabled
                        self.run_on_ui(lambda value=enabled: self.set_party_fullscreen(value, update_mpv=False))
                stop_event.wait(PARTY_FULLSCREEN_OBSERVER_INTERVAL_SECONDS)

        thread = threading.Thread(target=worker, daemon=True)
        self.party_fullscreen_observer_thread = thread
        thread.start()

    def stop_party_fullscreen_observer(self) -> None:
        stop_event = self.party_fullscreen_observer_stop
        self.party_fullscreen_observer_stop = None
        if stop_event is not None:
            stop_event.set()
        thread = self.party_fullscreen_observer_thread
        self.party_fullscreen_observer_thread = None
        if thread is not None and thread.is_alive():
            thread.join(timeout=1)
        self.party_mpv_fullscreen_state = None

    def show_host_party_window(self) -> None:
        if self.party_host_session is None:
            return
        self.destroy_party_window()
        win = tk.Toplevel(self.root)
        win.title("Watch Party Host")
        win.configure(bg=COLORS["bg"])
        win.geometry("1280x760")
        win.columnconfigure(0, weight=1)
        win.rowconfigure(1, weight=1)
        self.party_window = win
        media = self.party_current_media
        heading = media.party_title if media is not None else "Watch Party"
        self.build_party_header(win, heading=heading, subheading=None, host=True)
        _body, side = self.build_party_body(win)
        controls = tk.Frame(side, bg=COLORS["bg"])
        controls.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        for column in range(4):
            controls.columnconfigure(column, weight=1)
        for idx, (text, command, style) in enumerate(
            [
                ("Play", lambda: self.host_party_control("play"), "Compact.Accent.TButton"),
                ("Pause", lambda: self.host_party_control("pause"), "Compact.Dark.TButton"),
                ("-10s", lambda: self.host_party_seek(-10), "Compact.Dark.TButton"),
                ("+10s", lambda: self.host_party_seek(10), "Compact.Dark.TButton"),
                ("Prev", lambda: self.host_party_change_episode(-1), "Compact.Dark.TButton"),
                ("Next", lambda: self.host_party_change_episode(1), "Compact.Dark.TButton"),
                ("Full", self.toggle_party_fullscreen, "Compact.Dark.TButton"),
                ("End", self.end_host_party, "Compact.Dark.TButton"),
            ]
        ):
            ttk.Button(controls, text=text, style=style, command=command).grid(
                row=idx // 4, column=idx % 4, padx=(0, 5), pady=3, sticky="ew"
            )
        self.build_party_activity_panel(side, row=1, column=0)
        self.build_party_participants_panel(side, row=2, column=0, host=True)
        win.protocol("WM_DELETE_WINDOW", lambda: self.end_host_party(show_message=False))
        self.configure_party_fullscreen_bindings()
        self.seed_party_activity_from_state(self.party_host_session.room.public_state())
        self.render_host_party_participants()
        self.schedule_host_party_event_poll()

    def show_joined_party_window(self, state: dict[str, object]) -> None:
        self.destroy_party_window()
        win = tk.Toplevel(self.root)
        win.title("Watch Party")
        win.configure(bg=COLORS["bg"])
        win.geometry("1280x760")
        win.columnconfigure(0, weight=1)
        win.rowconfigure(1, weight=1)
        self.party_window = win
        media_payload = state.get("media") if isinstance(state.get("media"), dict) else {}
        media = WatchPartyMedia.from_json(media_payload)
        self.build_party_header(
            win,
            heading=media.party_title,
            subheading=f"{split_display_title(media.anime_title)[0]} episode {media.episode} ({media.mode})",
            host=False,
        )
        _body, side = self.build_party_body(win)
        username_row = tk.Frame(side, bg=COLORS["bg"])
        username_row.grid(row=0, column=0, sticky="ew", pady=(0, 8))
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
        ttk.Button(username_row, text="Update", style="Compact.Dark.TButton", command=self.change_party_username).grid(
            row=0, column=2, padx=(6, 0)
        )
        ttk.Button(username_row, text="Full", style="Compact.Dark.TButton", command=self.toggle_party_fullscreen).grid(
            row=0, column=3, padx=(6, 0)
        )
        self.build_party_activity_panel(side, row=1, column=0)
        self.build_party_participants_panel(side, row=2, column=0, host=False)
        ttk.Button(side, text="Leave Party", style="Compact.Dark.TButton", command=self.leave_joined_party).grid(
            row=3, column=0, sticky="ew", pady=(8, 0)
        )
        win.protocol("WM_DELETE_WINDOW", self.leave_joined_party)
        self.configure_party_fullscreen_bindings()
        self.seed_party_activity_from_state(state)
        self.render_party_participants_from_state(state)

    def destroy_party_window(self) -> None:
        if self.party_window is None:
            return
        self.stop_party_fullscreen_observer()
        self.set_party_fullscreen(False, update_mpv=False)
        try:
            self.party_window.destroy()
        except tk.TclError:
            pass
        self.party_window = None
        self.party_header_frame = None
        self.party_body_frame = None
        self.party_sidebar_frame = None
        self.party_video_panel = None
        self.party_video_frame = None
        self.party_participant_list = None
        self.party_activity_text = None
        self.party_fullscreen = False

    def stop_party_playback(self) -> None:
        self.party_force_sync_generation += 1
        controller = self.party_playback_controller
        self.party_playback_controller = None
        if controller is None:
            return
        try:
            controller.stop()
        except Exception:
            pass

    def close_host_session_later(
        self,
        session,
        *,
        notify_guests: bool = True,
        notify_grace_seconds: float = 0.0,
    ) -> None:
        def worker() -> None:
            try:
                session.close(notify_guests=notify_guests, notify_grace_seconds=notify_grace_seconds)
            except Exception:
                pass

        threading.Thread(target=worker, daemon=True).start()

    def finish_joined_party(
        self,
        message: str,
        *,
        send_leave: bool = False,
        show_message: bool = True,
    ) -> None:
        self.party_join_polling = False
        client = self.party_client
        self.party_client = None
        self.party_current_media = None
        self.stop_party_fullscreen_observer()
        self.stop_party_playback()
        if send_leave and client is not None:
            try:
                client.leave()
            except WatchPartyError:
                pass
        self.destroy_party_window()
        self.party_status_text.set(message)
        if show_message:
            messagebox.showinfo("Watch party", message)

    def copy_party_link(self) -> None:
        link = self.party_link_var.get()
        if not link:
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(link)
        self.party_status_text.set("Watch party link copied.")

    def send_party_chat_message(self) -> None:
        message = self.party_chat_var.get().strip()
        if not message:
            return
        self.party_chat_var.set("")
        if self.party_host_session is not None:
            self.apply_host_party_pending_events()
            try:
                event = self.party_host_session.room.send_chat(
                    message,
                    username=self.party_host_session.room.host_username,
                    host=True,
                )
            except WatchPartyError as exc:
                messagebox.showwarning("Chat failed", str(exc))
                return
            self.apply_host_party_events([event.to_json()])
            return
        if self.party_client is not None:
            try:
                self.party_client.send_chat(message)
            except WatchPartyError as exc:
                messagebox.showwarning("Chat failed", str(exc))
                return
            self.party_status_text.set("Message sent.")

    def render_party_participants(self, participants: list[object], *, empty_text: str = "No one has joined yet.") -> None:
        if self.party_participant_list is None:
            return
        self.party_participant_ids = []
        self.party_participant_list.delete(0, tk.END)
        visible = [item for item in participants if isinstance(item, dict)]
        if not visible:
            self.party_participant_list.insert(tk.END, empty_text)
            return
        for participant in visible:
            participant_id = str(participant.get("participant_id") or "")
            color = str(participant.get("color") or "")
            if participant_id and color:
                self.party_user_colors[f"participant:{participant_id}"] = color
            self.party_participant_ids.append(participant_id)
            joined = local_time(str(participant.get("joined_at") or ""))
            label = f"{participant.get('username') or 'Guest'}"
            if joined:
                label += f"  joined {joined}"
            self.party_participant_list.insert(tk.END, label)

    def render_party_participants_from_state(self, state: dict[str, object]) -> None:
        participants = state.get("participants") if isinstance(state.get("participants"), list) else []
        self.render_party_participants(participants)

    def schedule_host_party_event_poll(self) -> None:
        if self.shutting_down or self.party_host_session is None:
            return
        if self.party_host_event_job is not None:
            try:
                self.root.after_cancel(self.party_host_event_job)
            except tk.TclError:
                pass
        self.party_host_event_job = self.root.after(PARTY_HOST_EVENT_POLL_MS, self.host_party_event_tick)

    def host_party_event_tick(self) -> None:
        self.party_host_event_job = None
        if self.shutting_down or self.party_host_session is None:
            return
        self.apply_host_party_pending_events()
        self.schedule_host_party_event_poll()

    def apply_host_party_pending_events(self) -> None:
        if self.party_host_session is None:
            return
        events = self.party_host_session.room.events_since(self.party_host_latest_sequence, timeout=0.0)
        self.apply_host_party_events([event.to_json() for event in events])

    def apply_host_party_events(self, events: list[dict[str, object]]) -> None:
        if not events:
            return
        self.apply_party_activity_events(events)
        for event in events:
            self.party_host_latest_sequence = max(self.party_host_latest_sequence, int(event.get("sequence") or 0))
        if any(self.party_event_updates_participants(str(event.get("event_type") or "")) for event in events):
            self.render_host_party_participants()

    def party_event_updates_participants(self, event_type: str) -> bool:
        return event_type in {"participant_joined", "participant_left", "participant_updated", "participant_kicked"}

    def start_host_party_mpv_observer(self) -> None:
        self.stop_host_party_mpv_observer()
        controller = self.party_playback_controller
        if self.party_host_session is None or controller is None:
            return
        stop_event = threading.Event()
        self.party_host_observer_stop = stop_event

        def worker() -> None:
            last_snapshot: dict[str, object] | None = None
            last_wall = monotonic()
            while not stop_event.is_set():
                if not controller.available():
                    stop_event.wait(0.5)
                    continue
                snapshot = controller.snapshot()
                now = monotonic()
                if snapshot is None:
                    stop_event.wait(PARTY_HOST_MPV_OBSERVER_INTERVAL_SECONDS)
                    continue
                if last_snapshot is not None and now >= self.party_host_observer_ignore_until:
                    action = self.detect_host_mpv_action(last_snapshot, snapshot, now - last_wall)
                    if action is not None:
                        self.run_on_ui(
                            lambda value=action, snap=dict(snapshot): self.host_party_observed_control(value, snap)
                        )
                last_snapshot = snapshot
                last_wall = now
                stop_event.wait(PARTY_HOST_MPV_OBSERVER_INTERVAL_SECONDS)

        thread = threading.Thread(target=worker, daemon=True)
        self.party_host_observer_thread = thread
        thread.start()

    def stop_host_party_mpv_observer(self) -> None:
        stop_event = self.party_host_observer_stop
        self.party_host_observer_stop = None
        if stop_event is not None:
            stop_event.set()
        thread = self.party_host_observer_thread
        self.party_host_observer_thread = None
        if thread is not None and thread.is_alive():
            thread.join(timeout=1)

    def detect_host_mpv_action(
        self,
        previous: dict[str, object],
        current: dict[str, object],
        elapsed_seconds: float,
    ) -> str | None:
        previous_paused = bool(previous.get("paused"))
        current_paused = bool(current.get("paused"))
        if current_paused != previous_paused:
            return "pause" if current_paused else "play"
        try:
            previous_position = float(previous.get("position_seconds") or 0.0)
            current_position = float(current.get("position_seconds") or 0.0)
        except (TypeError, ValueError):
            return None
        expected_position = previous_position if previous_paused else previous_position + max(0.0, elapsed_seconds)
        if abs(current_position - expected_position) >= PARTY_HOST_MPV_SEEK_THRESHOLD_SECONDS:
            return "seek"
        return None

    def host_party_observed_control(self, action: str, snapshot: dict[str, object]) -> None:
        if self.party_host_session is None:
            return
        self.apply_host_party_pending_events()
        payload: dict[str, object] = {
            "position_seconds": float(snapshot.get("position_seconds") or 0.0),
        }
        if self.party_current_media is not None:
            payload["episode"] = self.party_current_media.episode
        try:
            event = self.party_host_session.room.control(action, payload)
        except WatchPartyError as exc:
            self.party_status_text.set(f"Host player sync failed: {exc}")
            return
        self.apply_host_party_events([event.to_json()])
        label = "pause" if action == "pause" else "play" if action == "play" else "seek"
        self.party_status_text.set(f"Synced host player {label} to the watch party.")

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
        for attr in ("party_host_refresh_job", "party_host_state_sync_job", "party_host_event_job"):
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
        self.render_party_participants(participants)

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
        self.party_host_observer_ignore_until = monotonic() + 1.25
        self.apply_local_party_control(action, payload)
        try:
            event = self.party_host_session.room.control(action, payload)
        except WatchPartyError as exc:
            messagebox.showwarning("Watch party control failed", str(exc))
            return
        self.apply_host_party_events([event.to_json()])
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
        self.stop_party_fullscreen_observer()
        self.stop_host_party_mpv_observer()
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
            self.launch_party_media(media, ipc_path, embed_wid=self.party_embed_window_id())
        except LaunchError as exc:
            messagebox.showwarning("ani-cli launch failed", str(exc))
            return
        self.party_current_media = media
        self.party_playback_controller = MpvIpcController(ipc_path)
        action = "next_episode" if direction > 0 else "previous_episode"
        self.host_party_control(
            action,
            {
                "episode": next_episode,
                "media": media.to_json(),
                "position_seconds": 0.0,
                "paused": True,
                "sync_pending": True,
            },
        )
        self.start_party_fullscreen_observer()
        self.start_host_party_mpv_observer()

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
        self.stop_party_fullscreen_observer()
        self.stop_host_party_mpv_observer()
        session = self.party_host_session
        self.party_host_session = None
        self.party_join_polling = False
        self.party_current_media = None
        self.stop_party_playback()
        if session is not None:
            try:
                session.room.end()
            except Exception:
                pass
            self.close_host_session_later(
                session,
                notify_guests=False,
                notify_grace_seconds=PARTY_HOST_END_NOTIFY_GRACE_SECONDS,
            )
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
        self.finish_joined_party(f"Watch party connection lost: {error}", send_leave=False)

    def apply_party_events(self, payload: dict[str, object]) -> None:
        events = payload.get("events") if isinstance(payload.get("events"), list) else []
        event_dicts = [event for event in events if isinstance(event, dict)]
        self.apply_party_activity_events(event_dicts)
        state = payload.get("state") if isinstance(payload.get("state"), dict) else None
        if state is not None:
            self.render_party_participants_from_state(state)
        for event in event_dicts:
            event_type = str(event.get("event_type") or "")
            event_payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
            if event_type == "participant_kicked":
                participant = event_payload.get("participant") if isinstance(event_payload.get("participant"), dict) else {}
                if self.party_client is not None and participant.get("participant_id") == self.party_client.participant_id:
                    self.finish_joined_party("You were removed from the watch party.", send_leave=False)
                    return
            elif event_type == "party_ended":
                self.finish_joined_party("The host ended the watch party.", send_leave=False)
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
                self.stop_party_fullscreen_observer()
                previous_controller = self.party_playback_controller
                if previous_controller is not None:
                    try:
                        previous_controller.stop()
                    except Exception:
                        pass
                ipc_path = party_ipc_path(f"join-{self.party_client.participant_id if self.party_client else 'guest'}-{media.episode}")
                try:
                    self.launch_party_media(media, ipc_path, embed_wid=self.party_embed_window_id())
                except LaunchError as exc:
                    self.party_status_text.set(f"Episode launch failed: {exc}")
                    continue
                self.party_current_media = media
                self.party_playback_controller = MpvIpcController(ipc_path)
                self.start_party_fullscreen_observer()
                self.party_status_text.set(f"Host changed to episode {media.episode}.")
                playback_state = event_payload.get("playback_state") if isinstance(event_payload.get("playback_state"), dict) else None
                sync_state = state if state is not None else {"media": media_payload, "playback_state": playback_state}
                self.start_party_force_sync("episode change", state=sync_state)

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
        self.finish_joined_party("Left the watch party.", send_leave=True, show_message=False)

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

    def start_all_metadata_refresh(self) -> None:
        if self.metadata_refreshing:
            return
        rows = list_anime(self.conn)
        if not rows:
            messagebox.showinfo("Refresh metadata", "The watchlist is empty.")
            return
        config = load_config()
        if not config.anilist.enabled:
            messagebox.showwarning("Refresh metadata", "AniList metadata is disabled in the application config.")
            return
        total = len(rows)
        if not messagebox.askyesno(
            "Refresh all metadata",
            f"Refresh AniList metadata and cover art for all {total} watchlist entr{'y' if total == 1 else 'ies'}?\n\n"
            "This runs in the background and may take several minutes because AniList requests are rate limited.",
        ):
            return
        self.metadata_refreshing = True
        self.refresh_all_metadata_button.configure(state="disabled")
        self.dashboard_label.configure(text=f"Refreshing metadata 0/{total}...")

        def progress(current: int, progress_total: int, title: str) -> None:
            self.run_on_ui(
                lambda: self.dashboard_label.configure(
                    text=f"Refreshing metadata {current}/{progress_total}: {split_display_title(title)[0]}"
                )
            )

        def worker() -> None:
            try:
                with initialize() as conn:
                    result = refresh_all_metadata(conn, config, progress=progress)
            except Exception as exc:
                self.run_on_ui(lambda error=str(exc): self.finish_all_metadata_refresh(None, error=error))
                return
            self.run_on_ui(lambda: self.finish_all_metadata_refresh(result, error=None))

        threading.Thread(target=worker, daemon=True).start()

    def finish_all_metadata_refresh(
        self,
        result: BulkMetadataRefreshResult | None,
        *,
        error: str | None,
    ) -> None:
        self.metadata_refreshing = False
        self.refresh_all_metadata_button.configure(state="normal")
        self.library_render_signature = None
        self.activity_signature = None
        self.refresh_library(preserve_scroll=True)
        if error:
            messagebox.showwarning("Metadata refresh failed", error)
            return
        if result is None:
            return
        lines = [
            f"Refreshed {result.refreshed} of {result.total} watchlist entries.",
            f"AniList-linked entries: {result.linked}",
        ]
        if result.unresolved:
            lines.append(f"Need manual match selection: {result.unresolved}")
        if result.failures:
            lines.append(f"Failed: {len(result.failures)}")
            lines.append("")
            lines.extend(f"• {title}: {failure}" for title, failure in result.failures[:8])
            if len(result.failures) > 8:
                lines.append(f"• …and {len(result.failures) - 8} more")
        messagebox.showinfo("Metadata refresh complete", "\n".join(lines))

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
