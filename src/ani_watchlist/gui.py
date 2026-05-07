from __future__ import annotations

import argparse
import json
import tkinter as tk
from tkinter import messagebox, simpledialog, ttk

try:
    from PIL import Image, ImageDraw, ImageTk
except Exception:  # pragma: no cover - optional GUI enhancement
    Image = None
    ImageDraw = None
    ImageTk = None

from .config import load_config
from .db import initialize
from .launcher import LaunchError, launch_episode
from .metadata import refresh_metadata_for_anime, select_match
from .providers.anilist import AniListProvider
from .timefmt import local_time
from .store import (
    STATUSES,
    clean_display_title,
    delete_anime,
    episodes_for_anime,
    get_anime_by_id,
    get_or_create_anime,
    list_anime,
    mark_episode,
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
COVER_W = 142
COVER_H = 204
DETAIL_COVER_W = 170
DETAIL_COVER_H = 244
WATCHED_ICON = "✅"
UNWATCHED_ICON = "❌"


def split_display_title(title: str) -> tuple[str, str | None]:
    title = clean_display_title(title)
    if title.endswith(")") and " (" in title:
        primary, secondary = title.rsplit(" (", 1)
        primary = primary.strip()
        secondary = secondary[:-1].strip()
        if primary and secondary and primary.casefold() != secondary.casefold():
            return primary, secondary
    return title, None


class WatchlistApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("ani-watchlist")
        self.root.geometry("1120x760")
        self.root.minsize(780, 540)
        self.root.configure(bg=COLORS["bg"])
        self.conn = initialize()
        self.auto_refresh_ms = 3000
        self.selected_status = tk.StringVar(value="watching")
        self.search_text = tk.StringVar()
        self.detail_status = tk.StringVar(value=STATUS_LABELS["watching"])
        self.show_alt_title = tk.BooleanVar(value=False)
        self.selected_anime_id: int | None = None
        self.detail_primary_title = ""
        self.detail_alt_title: str | None = None
        self.current_page = "library"
        self.images: dict[str, object] = {}
        self.current_rows = []
        self.card_widgets: dict[int, tk.Frame] = {}
        self.grid_columns = 1
        self._configure_style()
        self._build()
        self.show_library()
        self.schedule_auto_refresh()

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
        style.configure("Accent.TButton", background=COLORS["accent"], foreground="#111111", padding=(10, 6))
        style.map("Accent.TButton", background=[("active", COLORS["accent_hover"])])
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

    def _build(self) -> None:
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)
        self.container = tk.Frame(self.root, bg=COLORS["bg"])
        self.container.grid(row=0, column=0, sticky="nsew")
        self.container.columnconfigure(0, weight=1)
        self.container.rowconfigure(0, weight=1)
        self.library_page = tk.Frame(self.container, bg=COLORS["bg"])
        self.detail_page = tk.Frame(self.container, bg=COLORS["bg"])
        self._build_library_page()
        self._build_detail_page()

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
        self.grid_canvas.bind_all("<MouseWheel>", self._on_mousewheel)

    def _build_detail_page(self) -> None:
        page = self.detail_page
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
        for idx, (text, command, style) in enumerate(
            [
                ("Continue", self.continue_selected_episode, "Accent.TButton"),
                ("Mark Watched", lambda: self.mark_selected_episode(True), "Accent.TButton"),
                ("Mark Unwatched", lambda: self.mark_selected_episode(False), "Dark.TButton"),
                ("Add Episode", self.add_episode, "Dark.TButton"),
                ("Refresh Metadata", self.refresh_metadata, "Dark.TButton"),
                ("Choose Match", self.choose_match, "Dark.TButton"),
                ("Edit Title", self.edit_title, "Dark.TButton"),
                ("Delete", self.delete_selected, "Dark.TButton"),
            ]
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
        self.detail_page.grid_forget()
        self.library_page.grid(row=0, column=0, sticky="nsew")
        self.refresh_library()

    def open_detail(self, anime_id: int) -> None:
        self.selected_anime_id = anime_id
        self.show_alt_title.set(False)
        if hasattr(self, "launch_label"):
            self.launch_label.configure(text="", fg=COLORS["muted"])
        self.current_page = "detail"
        self.library_page.grid_forget()
        self.detail_page.grid(row=0, column=0, sticky="nsew")
        self.load_detail()

    def schedule_auto_refresh(self) -> None:
        self.root.after(self.auto_refresh_ms, self.auto_refresh)

    def auto_refresh(self) -> None:
        try:
            if self.current_page == "library":
                self.refresh_library(preserve_scroll=True)
            else:
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

    def create_card(self, row) -> tk.Frame:
        primary_title, _alt_title = split_display_title(row["display_title"])
        card = tk.Frame(
            self.grid_frame,
            width=CARD_W - 16,
            height=292,
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
        title = tk.Label(
            card,
            text=primary_title,
            bg=COLORS["panel"],
            fg=COLORS["text"],
            font=("", 10, "bold"),
            wraplength=COVER_W,
            justify="left",
            anchor="w",
            cursor="hand2",
        )
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
        last.grid(row=3, column=0, sticky="ew", padx=12)
        for widget in (card, cover, title, progress, last):
            widget.bind("<Button-1>", lambda _event, anime_id=row["id"]: self.open_detail(anime_id))
        self.card_widgets[int(row["id"])] = card
        return card

    def _update_grid_scroll_region(self, _event=None) -> None:
        self.grid_canvas.configure(scrollregion=self.grid_canvas.bbox("all"))

    def _on_grid_resize(self, event) -> None:
        self.grid_canvas.itemconfigure(self.grid_window, width=event.width)
        columns = max(1, event.width // CARD_W)
        if columns != self.grid_columns:
            self.render_grid()

    def _on_mousewheel(self, event) -> None:
        if self.current_page == "library":
            self.grid_canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

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

    def continue_selected_episode(self) -> None:
        if self.selected_anime_id is None:
            return
        anime = get_anime_by_id(self.conn, self.selected_anime_id)
        if anime is None:
            return
        episode = self.selected_episode_key()
        if not episode:
            messagebox.showinfo("Episode required", "Select an episode first.")
            return
        title = anime["source_title"] or anime["display_title"]
        try:
            result = launch_episode(title, episode)
        except LaunchError as exc:
            self.launch_label.configure(text=f"Launch failed: {exc}", fg=COLORS["danger"])
            messagebox.showwarning("ani-cli launch failed", str(exc))
            return
        title_for_message, _alt_title = split_display_title(anime["display_title"])
        target = "terminal" if result.used_terminal else "background process"
        self.launch_label.configure(
            text=f"Opened {title_for_message} episode {episode} in {target}.",
            fg=COLORS["muted"],
        )

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
    app = WatchlistApp(root)
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
        root.destroy()
        print("GUI action smoke test: OK" if args.action_smoke_test else "GUI smoke test: OK")
        return 0
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
