"""Single-window Tk/ttk settings editor."""

from __future__ import annotations

import tkinter as tk
import webbrowser
from functools import partial
from pathlib import Path
from tkinter import ttk
from typing import Any

from comsol_mcp import __version__
from comsol_mcp.settings import (
    SETTINGS_READABLE_VERSIONS,
    SETTINGS_SCHEMA,
    SettingsError,
    SettingsLocation,
    default_settings_document,
    normalize_settings_document,
    resolve_settings_location,
)
from comsol_mcp.settings_gui_handshake import publish_handshake
from comsol_mcp.settings_gui_launcher import GuiAlreadyRunning, SettingsGuiInstanceLock

from . import GUI_RELEASE
from .constants import APP_NAME, LOCK_POLL_MS
from .controller import SettingsController
from .dialogs import Dialogs
from .fonts import apply_locale_font
from .i18n import Translator, language_option_labels, scale_option_labels
from .manual_index import ManualIndexBuildTask
from .model import (
    FIELDS,
    TAB_IDS,
    FieldDescriptor,
    SettingsFormModel,
    field_key_for_error,
    get_value,
    profile_help_id,
)
from .storage import (
    DamagedSettings,
    SettingsStore,
    ensure_default_directories,
    ensure_settings_parent,
)
from .windows_lock import SettingsConflict

TAB_TITLES = {
    "general": "General",
    "profile": "Profile",
    "runtime": "Runtime",
    "comsol_java": "COMSOL/Java",
    "evidence": "Evidence",
    "docs": "Docs",
    "ownership": "Owner",
    "about": "About",
}
FIXED_LINKS = (
    ("This repository", "https://github.com/garbage-enzyme/COMSOL_Multiphysics_MCP_6_4_Calibrated"),
    ("Thanks: upstream project", "https://github.com/wjc9011/COMSOL_Multiphysics_MCP"),
    ("Thanks: Ching-Chiang project", "https://github.com/Ching-Chiang/comsol-mcp"),
    (
        "MIT License",
        "https://github.com/garbage-enzyme/COMSOL_Multiphysics_MCP_6_4_Calibrated/blob/main/LICENSE",
    ),
)
ICON_PATH = Path(__file__).resolve().parent / "assets" / "comsol_mcp.ico"
INDEX_DIALOG_WIDTH = 620
INDEX_DIALOG_HEIGHT = 260
INDEX_DIALOG_MARGIN = 48
INDEX_DIALOG_SOURCE_CHARS = 96
DOC_SECTIONS = (
    (
        "Manual sources and lexical search",
        ("manuals.root", "lexical_docs.enabled", "lexical_docs.index_path"),
    ),
    (
        "Optional semantic search",
        ("semantic_docs.enabled", "semantic_docs.root", "semantic_docs.model_path"),
    ),
)


def _apply_window_icon(root: tk.Tk) -> bool:
    try:
        root.iconbitmap(default=str(ICON_PATH))
    except OSError, tk.TclError:
        return False
    return True


def _load_startup_document(store: SettingsStore) -> dict[str, Any]:
    document = store.load()
    if document.get("schema_name") != SETTINGS_SCHEMA:
        raise DamagedSettings("settings schema identity is unsupported")
    if document.get("schema_version") not in SETTINGS_READABLE_VERSIONS:
        raise DamagedSettings("settings schema version is unsupported")
    report = normalize_settings_document(document)
    if any(field_key_for_error(item["path"]) is None for item in report["errors"]):
        raise DamagedSettings("settings contain an unsupported structure")
    return document


class ScrollableTab(ttk.Frame):
    def __init__(self, parent) -> None:
        super().__init__(parent)
        self.canvas = tk.Canvas(self, highlightthickness=0, borderwidth=0)
        self.scrollbar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.content = ttk.Frame(self.canvas, padding=(20, 16, 20, 20))
        self.window = self.canvas.create_window((0, 0), window=self.content, anchor="nw")
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.scrollbar.grid(row=0, column=1, sticky="ns")
        self.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)
        self.content.bind("<Configure>", self._content_changed)
        self.canvas.bind("<Configure>", self._canvas_changed)

    def _content_changed(self, _event=None) -> None:
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _canvas_changed(self, event) -> None:
        self.canvas.itemconfigure(self.window, width=event.width)


class ManualIndexProgressDialog:
    """Modal progress view backed by one isolated index-build process."""

    def __init__(
        self,
        parent: tk.Tk,
        controller: SettingsController,
        task: ManualIndexBuildTask,
        *,
        on_close,
    ) -> None:
        self.parent = parent
        self.controller = controller
        self.task = task
        self.on_close = on_close
        self.window = tk.Toplevel(parent)
        self.window.title(controller.text("Generate manual index"))
        self.window.transient(parent)
        self.window.resizable(True, False)
        self.window.protocol("WM_DELETE_WINDOW", self.cancel)
        self.window.bind("<Escape>", lambda _event: self.cancel())
        self.window.grab_set()
        self._size_and_position()
        outer = ttk.Frame(self.window)
        outer.pack(fill="both", expand=True)
        footer = ttk.Frame(outer, padding=(18, 8, 18, 16))
        footer.pack(side="bottom", fill="x")
        body = ttk.Frame(outer, padding=(18, 16, 18, 6))
        body.pack(side="top", fill="both", expand=True)
        self.stage = tk.StringVar(value=controller.text("Starting index worker..."))
        self.detail = tk.StringVar(value="0%")
        self.percent = tk.IntVar(value=0)
        ttk.Label(body, textvariable=self.stage, wraplength=540).pack(anchor="w")
        ttk.Progressbar(
            body,
            maximum=100,
            variable=self.percent,
            mode="determinate",
        ).pack(fill="x", pady=(12, 6))
        ttk.Label(body, textvariable=self.detail, wraplength=540).pack(anchor="w")
        self.cancel_button = ttk.Button(
            footer,
            text=controller.text("Cancel"),
            command=self.cancel,
        )
        self.cancel_button.pack(side="right")
        self._terminal = False
        self._after_id = self.window.after(100, self.poll)

    def _size_and_position(self) -> None:
        """Keep the fixed footer reachable within the current monitor bounds."""
        self.parent.update_idletasks()
        screen_width = max(1, self.window.winfo_screenwidth())
        screen_height = max(1, self.window.winfo_screenheight())
        width = min(INDEX_DIALOG_WIDTH, max(360, screen_width - INDEX_DIALOG_MARGIN * 2))
        height = min(INDEX_DIALOG_HEIGHT, max(180, screen_height - INDEX_DIALOG_MARGIN * 2))
        parent_x = self.parent.winfo_rootx()
        parent_y = self.parent.winfo_rooty()
        parent_width = max(self.parent.winfo_width(), width)
        parent_height = max(self.parent.winfo_height(), height)
        x = max(0, min(parent_x + (parent_width - width) // 2, screen_width - width))
        y = max(0, min(parent_y + (parent_height - height) // 2, screen_height - height))
        self.window.geometry(f"{width}x{height}+{x}+{y}")
        self.window.minsize(min(width, 420), min(height, 180))

    @staticmethod
    def _display_source(value: object) -> str:
        source = str(value)
        if len(source) <= INDEX_DIALOG_SOURCE_CHARS:
            return source
        return "..." + source[-(INDEX_DIALOG_SOURCE_CHARS - 3) :]

    def cancel(self) -> None:
        if self._terminal:
            return
        self.cancel_button.configure(state="disabled")
        self.stage.set(self.controller.text("Cancelling safely..."))
        self.task.cancel()

    def poll(self) -> None:
        for event in self.task.drain_events():
            kind = event.get("event")
            if kind == "progress":
                percent = max(0, min(int(event.get("percent", 0)), 100))
                self.percent.set(percent)
                stage = str(event.get("stage", "working")).replace("_", " ").title()
                self.stage.set(self.controller.text(stage))
                self.detail.set(
                    self.controller.text(
                        "{percent}% — {files}/{total_files} PDFs, "
                        "{pages}/{total_pages} pages{source}"
                    ).format(
                        percent=percent,
                        files=int(event.get("processed_files", 0)),
                        total_files=int(event.get("total_files", 0)),
                        pages=int(event.get("processed_pages", 0)),
                        total_pages=int(event.get("total_pages", 0)),
                        source=(
                            " — " + self._display_source(event["current_source"])
                            if event.get("current_source")
                            else ""
                        ),
                    )
                )
            elif kind == "result":
                self._terminal = True
                self.percent.set(100)
                self.controller.dialogs.info(
                    title=self.controller.text("Manual index ready"),
                    message=self.controller.text(
                        "Indexed {pdfs} PDFs and {pages} pages. Corpus fingerprint: {fingerprint}"
                    ).format(
                        pdfs=int(event.get("pdf_count", 0)),
                        pages=int(event.get("page_count", 0)),
                        fingerprint=str(event.get("corpus_fingerprint", "")),
                    ),
                )
                self.close()
                return
            elif kind == "error":
                self._terminal = True
                self.controller.dialogs.error(
                    title=self.controller.text("Index generation failed"),
                    message=self.controller.text(
                        str(event.get("message", "Index generation failed."))
                    ),
                )
                self.close()
                return
            elif kind == "cancelled":
                self._terminal = True
                self.controller.dialogs.info(
                    title=self.controller.text("Index generation cancelled"),
                    message=self.controller.text(
                        "Temporary files were removed and the previous index was preserved."
                    ),
                )
                self.close()
                return
        if not self.task.running and not self._terminal:
            self._terminal = True
            self.controller.dialogs.error(
                title=self.controller.text("Index generation failed"),
                message=self.controller.text(
                    "The index worker stopped without a completion receipt."
                ),
            )
            self.close()
            return
        self._after_id = self.window.after(100, self.poll)

    def close(self) -> None:
        if self._after_id is not None:
            try:
                self.window.after_cancel(self._after_id)
            except tk.TclError:
                pass
            self._after_id = None
        try:
            self.window.grab_release()
        except tk.TclError:
            pass
        self.window.destroy()
        self.on_close()


class SettingsApplication:
    def __init__(
        self,
        root: tk.Tk,
        controller: SettingsController,
    ) -> None:
        self.root = root
        self.controller = controller
        self.dialogs = controller.dialogs
        self.notebook: ttk.Notebook | None = None
        self.tabs: dict[str, ScrollableTab] = {}
        self.variables: dict[str, tk.Variable] = {}
        self.help_labels: dict[str, ttk.Label] = {}
        self.error_labels: dict[str, ttk.Label] = {}
        self.entries: dict[str, ttk.Entry] = {}
        self.field_widgets: dict[str, list[Any]] = {}
        self.root_lists: dict[str, tk.Listbox] = {}
        self.shortcut_buttons: dict[str, ttk.Button] = {}
        self.fixed_link_buttons: list[ttk.Button] = []
        self.index_dialog: ManualIndexProgressDialog | None = None
        self.banner: ttk.Label | None = None
        self.save_button: ttk.Button | None = None
        self.apply_button: ttk.Button | None = None
        self.outer: ttk.Frame | None = None
        self._system_scaling = float(self.root.tk.call("tk", "scaling"))
        self._built_language = ""
        self._built_scale = ""
        self._selected_tab = 0
        self._refreshing = False
        self._conflict_after_id: str | None = None
        self.controller.on_refresh = self._controller_refresh
        self.controller.on_close = self.close
        self.icon_loaded = _apply_window_icon(self.root)
        self._configure_root()
        self.refresh()
        self.root.protocol("WM_DELETE_WINDOW", self.controller.cancel)
        self._conflict_after_id = self.root.after(LOCK_POLL_MS, self._poll_conflict)

    def _configure_root(self) -> None:
        self._apply_scale(self.controller.model.scale)
        self.root.title(APP_NAME)
        self.root.geometry("1120x780")
        self.root.minsize(960, 640)
        apply_locale_font(self.root, self.controller.model.language)
        style = ttk.Style(self.root)
        style.configure("Invalid.TEntry", foreground="#a40000", fieldbackground="#fff2f2")
        style.configure("Restart.TLabel", foreground="#7a3d00", padding=(12, 8))
        style.configure("Help.TLabel", foreground="#555555")
        style.configure("Error.TLabel", foreground="#a40000")

    def _apply_scale(self, scale: str) -> None:
        value = self._system_scaling if scale == "system" else (float(scale) / 100.0) * 96.0 / 72.0
        self.root.tk.call("tk", "scaling", value)

    def refresh(self) -> None:
        if self._refreshing:
            return
        self._refreshing = True
        try:
            if self.notebook is not None:
                try:
                    self._selected_tab = self.notebook.index(self.notebook.select())
                except tk.TclError:
                    self._selected_tab = 0
                for child in self.root.winfo_children():
                    child.destroy()
            self.variables.clear()
            self.help_labels.clear()
            self.error_labels.clear()
            self.entries.clear()
            self.field_widgets.clear()
            self.root_lists.clear()
            self.shortcut_buttons.clear()
            self.fixed_link_buttons.clear()
            apply_locale_font(self.root, self.controller.model.language)
            self._build()
        finally:
            self._refreshing = False

    def _build(self) -> None:
        _ = self.controller.text
        outer = ttk.Frame(self.root, padding=(12, 10, 12, 10))
        self.outer = outer
        self._built_language = self.controller.model.language
        self._built_scale = self.controller.model.scale
        outer.pack(fill="both", expand=True)
        title_row = ttk.Frame(outer)
        title_row.pack(fill="x", pady=(0, 8))
        ttk.Label(title_row, text=_("COMSOL MCP Settings"), font="TkHeadingFont").pack(side="left")
        ttk.Label(title_row, text=f"{GUI_RELEASE}  |  {__version__}").pack(side="right")

        self.banner = ttk.Label(
            outer,
            text=_("Changes take effect only after restarting Codex or the owning MCP client."),
            style="Restart.TLabel",
            anchor="w",
        )
        if self.controller.model.restart_required or self.controller.restart_pending:
            self.banner.pack(fill="x", pady=(0, 8))

        self.notebook = ttk.Notebook(outer)
        self.notebook.pack(fill="both", expand=True)
        self.tabs = {}
        for tab_id in TAB_IDS:
            tab = ScrollableTab(self.notebook)
            self.tabs[tab_id] = tab
            self.notebook.add(tab, text=_(TAB_TITLES[tab_id]))
        docs_keys = {key for _title, keys in DOC_SECTIONS for key in keys}
        for field in FIELDS:
            if field.key not in docs_keys:
                self._add_field(self.tabs[field.tab].content, field)
        docs_parent = self.tabs["docs"].content
        for title, keys in DOC_SECTIONS:
            row = docs_parent.grid_size()[1]
            ttk.Label(
                docs_parent,
                text=_(title),
                font="TkHeadingFont",
            ).grid(row=row, column=0, sticky="w", pady=(4, 12))
            for key in keys:
                self._add_field(docs_parent, next(field for field in FIELDS if field.key == key))
        self._build_about(self.tabs["about"].content)
        try:
            self.notebook.select(min(self._selected_tab, len(TAB_IDS) - 1))
        except tk.TclError:
            pass

        actions = ttk.Frame(outer, padding=(0, 10, 0, 0))
        actions.pack(fill="x", side="bottom", before=self.notebook)
        self.save_button = ttk.Button(
            actions,
            text=_("Save and Exit"),
            command=self.controller.save_and_exit,
        )
        self.save_button.pack(side="left")
        self.apply_button = ttk.Button(actions, text=_("Apply"), command=self.controller.apply)
        self.apply_button.pack(side="left", padx=(8, 0))
        ttk.Button(actions, text=_("Cancel"), command=self.controller.cancel).pack(side="right")
        state = "normal" if self.controller.model.valid else "disabled"
        self.save_button.configure(state=state)
        self.apply_button.configure(state=state)
        self._refresh_state()

    def _start_manual_index_build(self) -> None:
        if self.index_dialog is not None:
            return
        task = self.controller.start_manual_index_build()
        if task is None:
            return
        self.index_dialog = ManualIndexProgressDialog(
            self.root,
            self.controller,
            task,
            on_close=lambda: setattr(self, "index_dialog", None),
        )

    def _add_field(self, parent: ttk.Frame, field: FieldDescriptor) -> None:
        row = parent.grid_size()[1]
        frame = ttk.Frame(parent, padding=(0, 0, 0, 14))
        frame.grid(row=row, column=0, sticky="ew")
        parent.columnconfigure(0, weight=1)
        frame.columnconfigure(1, weight=1)
        ttk.Label(frame, text=field.key, wraplength=330, justify="left").grid(
            row=0,
            column=0,
            sticky="nw",
            padx=(0, 16),
        )
        value = get_value(self.controller.model.document, field.key)
        if field.kind == "readonly":
            variable = tk.StringVar(value="" if value is None else str(value))
            widget = ttk.Entry(frame, textvariable=variable, state="readonly")
            widget.grid(row=0, column=1, sticky="ew")
        elif field.kind == "choice":
            if field.key == "gui.language":
                labels = language_option_labels(self.controller.translator)
                reverse_labels = {label: key for key, label in labels.items()}
                variable = tk.StringVar(value=labels.get(str(value), str(value)))
                choices = tuple(labels[key] for key in field.choices)

                def changed(*_args) -> None:
                    self._changed(field.key, reverse_labels[variable.get()])

            elif field.key == "gui.scale":
                labels = scale_option_labels(self.controller.translator)
                reverse_labels = {label: key for key, label in labels.items()}
                variable = tk.StringVar(value=labels.get(str(value), str(value)))
                choices = tuple(labels[key] for key in field.choices)

                def changed(*_args) -> None:
                    self._changed(field.key, reverse_labels[variable.get()])

            else:
                variable = tk.StringVar(value=str(value))
                choices = field.choices

                def changed(*_args) -> None:
                    self._changed(field.key, variable.get())

            widget = ttk.Combobox(
                frame,
                textvariable=variable,
                values=choices,
                state="readonly",
            )
            widget.grid(row=0, column=1, sticky="ew")
            variable.trace_add("write", changed)
        elif field.kind == "boolean":
            variable = tk.BooleanVar(value=bool(value))
            widget = ttk.Checkbutton(
                frame,
                variable=variable,
                command=lambda key=field.key, var=variable: self._changed(key, var.get()),
            )
            widget.grid(row=0, column=1, sticky="w")
            self.field_widgets[field.key] = [widget]
        elif field.kind == "roots":
            variable = tk.StringVar(value="")
            self._add_roots(frame, field, list(value))
        else:
            variable = tk.StringVar(value="" if value is None else str(value))
            widget = ttk.Entry(
                frame,
                textvariable=variable,
                style="Invalid.TEntry" if field.key in self.controller.model.errors else "TEntry",
            )
            widget.grid(row=0, column=1, sticky="ew")
            self.entries[field.key] = widget
            control_widgets: list[Any] = [widget]
            variable.trace_add(
                "write",
                lambda *_args, key=field.key, var=variable, nullable=field.nullable: self._changed(
                    key,
                    None if nullable and not var.get().strip() else var.get(),
                ),
            )
            controls = ttk.Frame(frame)
            controls.grid(row=0, column=2, sticky="e", padx=(8, 0))
            if field.kind == "file":
                command = partial(self.controller.browse_file, field.key)
            elif field.kind == "save_file":
                command = partial(self.controller.browse_save_file, field.key)
            else:
                command = partial(self.controller.browse_directory, field.key)
            browse_button = ttk.Button(
                controls, text=self.controller.text("Browse"), command=command
            )
            browse_button.pack(side="left")
            control_widgets.append(browse_button)
            if field.nullable:
                clear_button = ttk.Button(
                    controls,
                    text=self.controller.text("Clear"),
                    command=lambda key=field.key: self.controller.clear(key),
                )
                clear_button.pack(side="left", padx=(6, 0))
                control_widgets.append(clear_button)
            if field.key == "comsol.installation_root":
                ttk.Button(
                    controls,
                    text=self.controller.text("Auto-detect"),
                    command=lambda: self.controller.auto_detect(manual=True),
                ).pack(side="left", padx=(6, 0))
            self.field_widgets[field.key] = control_widgets
        self.variables[field.key] = variable
        help_id = profile_help_id(value) if field.key == "profile.name" else field.help_id
        content_row = 1
        if field.key == "lexical_docs.index_path":
            action = ttk.Frame(frame)
            action.grid(row=1, column=1, columnspan=2, sticky="w", pady=(2, 0))
            ttk.Button(
                action,
                text=self.controller.text("Generate Index"),
                command=self._start_manual_index_build,
            ).pack(anchor="w")
            content_row = 2
        help_label = ttk.Label(
            frame,
            text=self.controller.text(help_id),
            style="Help.TLabel",
            wraplength=620,
            justify="left",
        )
        help_label.grid(row=content_row, column=1, columnspan=2, sticky="w", pady=(4, 0))
        self.help_labels[field.key] = help_label
        error = self.controller.model.errors.get(field.key, "")
        error_label = ttk.Label(
            frame,
            text=self.controller.text(error) if error else "",
            style="Error.TLabel",
            wraplength=620,
            justify="left",
        )
        error_label.grid(row=content_row + 1, column=1, columnspan=2, sticky="w", pady=(2, 0))
        self.error_labels[field.key] = error_label

    def _add_roots(self, frame: ttk.Frame, field: FieldDescriptor, values: list[str]) -> None:
        container = ttk.Frame(frame)
        container.grid(row=0, column=1, columnspan=2, sticky="ew")
        container.columnconfigure(0, weight=1)
        listbox = tk.Listbox(container, height=4, exportselection=False)
        listbox.grid(row=0, column=0, rowspan=2, sticky="ew")
        for value in values:
            listbox.insert("end", value)
        ttk.Button(
            container,
            text=self.controller.text("Add folder"),
            command=lambda key=field.key, box=listbox: self._add_root(key, box),
        ).grid(row=0, column=1, sticky="ew", padx=(8, 0))
        ttk.Button(
            container,
            text=self.controller.text("Remove"),
            command=lambda key=field.key, box=listbox: self._remove_root(key, box),
        ).grid(row=1, column=1, sticky="ew", padx=(8, 0), pady=(6, 0))
        self.root_lists[field.key] = listbox

    def _add_root(self, key: str, listbox: tk.Listbox) -> None:
        value = self.dialogs.ask_directory(title=self.controller.text("Choose folder"))
        if not value:
            return
        values = list(get_value(self.controller.model.document, key))
        values.append(value)
        self.controller.update(key, values)
        self._reload_listbox(key, listbox)

    def _remove_root(self, key: str, listbox: tk.Listbox) -> None:
        selection = listbox.curselection()
        if not selection:
            return
        values = list(get_value(self.controller.model.document, key))
        values.pop(selection[0])
        self.controller.update(key, values)
        self._reload_listbox(key, listbox)

    def _reload_listbox(self, key: str, listbox: tk.Listbox) -> None:
        listbox.delete(0, "end")
        for value in get_value(self.controller.model.document, key):
            listbox.insert("end", value)

    def _changed(self, key: str, value: Any) -> None:
        if self._refreshing:
            return
        try:
            current = get_value(self.controller.model.document, key)
        except KeyError:
            current = object()
        if current != value:
            self.controller.update(key, value)

    def _build_about(self, parent: ttk.Frame) -> None:
        _ = self.controller.text
        ttk.Label(parent, text=_("COMSOL MCP Settings"), font="TkHeadingFont").pack(anchor="w")
        ttk.Label(parent, text=f"{_('GUI release')}: {GUI_RELEASE}").pack(anchor="w", pady=(12, 0))
        ttk.Label(parent, text=f"{_('Installed package version')}: {__version__}").pack(anchor="w")
        ttk.Label(parent, text=_("Desktop shortcut"), font="TkHeadingFont").pack(
            anchor="w",
            pady=(18, 6),
        )
        shortcut_actions = ttk.Frame(parent)
        shortcut_actions.pack(anchor="w")
        self.shortcut_buttons["create"] = ttk.Button(
            shortcut_actions,
            text=_("Create desktop shortcut"),
            command=self.controller.create_desktop_shortcut,
        )
        self.shortcut_buttons["create"].pack(side="left")
        self.shortcut_buttons["remove"] = ttk.Button(
            shortcut_actions,
            text=_("Remove desktop shortcut"),
            command=self.controller.remove_desktop_shortcut,
        )
        self.shortcut_buttons["remove"].pack(side="left", padx=(8, 0))
        ttk.Label(parent, text=_("Repositories and acknowledgements"), font="TkHeadingFont").pack(
            anchor="w",
            pady=(18, 6),
        )
        repository_row = ttk.Frame(parent)
        repository_row.pack(anchor="w")
        self.fixed_link_buttons = []
        for index, (label, url) in enumerate(FIXED_LINKS[:3]):
            button = ttk.Button(
                repository_row,
                text=_(label),
                command=lambda fixed=url: webbrowser.open(fixed),
            )
            button.pack(side="left", padx=(0 if index == 0 else 8, 0))
            self.fixed_link_buttons.append(button)
        ttk.Label(parent, text="Copyright © 2026 garbage-enzyme").pack(
            anchor="w",
            pady=(12, 6),
        )
        license_label, license_url = FIXED_LINKS[3]
        license_button = ttk.Button(
            parent,
            text=_(license_label),
            command=lambda: webbrowser.open(license_url),
        )
        license_button.pack(anchor="w")
        self.fixed_link_buttons.append(license_button)

    def _controller_refresh(self) -> None:
        scale_changed = self.controller.model.scale != self._built_scale
        if scale_changed:
            self._apply_scale(self.controller.model.scale)
        if self.controller.model.language != self._built_language or scale_changed:
            self.refresh()
            return
        self._refresh_state()

    def _refresh_state(self) -> None:
        self._refreshing = True
        try:
            for field in FIELDS:
                if field.kind == "roots":
                    error = self.controller.model.errors.get(field.key, "")
                    if field.key in self.help_labels:
                        self.help_labels[field.key].configure(
                            text=self.controller.text(field.help_id)
                        )
                    if field.key in self.error_labels:
                        self.error_labels[field.key].configure(
                            text=self.controller.text(error) if error else ""
                        )
                    continue
                variable = self.variables.get(field.key)
                if variable is None:
                    continue
                value = get_value(self.controller.model.document, field.key)
                if field.key == "gui.language":
                    rendered: Any = language_option_labels(self.controller.translator).get(
                        str(value), str(value)
                    )
                elif field.key == "gui.scale":
                    rendered = scale_option_labels(self.controller.translator).get(
                        str(value), str(value)
                    )
                else:
                    rendered = (
                        bool(value)
                        if field.kind == "boolean"
                        else ("" if value is None else str(value))
                    )
                if variable.get() != rendered:
                    variable.set(rendered)
                help_id = profile_help_id(value) if field.key == "profile.name" else field.help_id
                if field.key in self.help_labels:
                    self.help_labels[field.key].configure(text=self.controller.text(help_id))
                error = self.controller.model.errors.get(field.key, "")
                if field.key in self.error_labels:
                    self.error_labels[field.key].configure(
                        text=self.controller.text(error) if error else ""
                    )
                if field.key in self.entries:
                    self.entries[field.key].configure(style="Invalid.TEntry" if error else "TEntry")
                if field.key in self.field_widgets:
                    enabled = not (
                        field.key == "lexical_docs.index_path"
                        and not bool(
                            get_value(self.controller.model.document, "lexical_docs.enabled")
                        )
                    ) and not (
                        field.key in {"semantic_docs.root", "semantic_docs.model_path"}
                        and not bool(
                            get_value(self.controller.model.document, "semantic_docs.enabled")
                        )
                    )
                    for widget in self.field_widgets[field.key]:
                        widget.configure(state="normal" if enabled else "disabled")
            state = "normal" if self.controller.model.valid else "disabled"
            if self.save_button is not None:
                self.save_button.configure(state=state)
            if self.apply_button is not None:
                self.apply_button.configure(state=state)
            if self.banner is not None and self.notebook is not None:
                if self.controller.model.restart_required or self.controller.restart_pending:
                    if not self.banner.winfo_manager():
                        self.banner.pack(fill="x", pady=(0, 8), before=self.notebook)
                elif self.banner.winfo_manager():
                    self.banner.pack_forget()
        finally:
            self._refreshing = False

    def _poll_conflict(self) -> None:
        if not self.root.winfo_exists():
            return
        if self.controller.poll_conflict():
            if self.save_button is not None:
                self.save_button.configure(state="disabled")
            if self.apply_button is not None:
                self.apply_button.configure(state="disabled")
            return
        self._conflict_after_id = self.root.after(LOCK_POLL_MS, self._poll_conflict)

    def close(self) -> None:
        if self.index_dialog is not None:
            self.index_dialog.task.cancel()
        if self._conflict_after_id is not None:
            try:
                self.root.after_cancel(self._conflict_after_id)
            except tk.TclError:
                pass
            self._conflict_after_id = None
        self.controller.store.close()
        self.root.destroy()


def _prepare_store(
    root: tk.Tk,
    dialogs: Dialogs,
    *,
    location: SettingsLocation | None = None,
) -> SettingsStore | None:
    _ = Translator("zh_CN")
    location = location or resolve_settings_location()
    target = location.writable_path
    if location.setup_required:
        if not dialogs.rebuild_or_exit(
            title=APP_NAME,
            message=_(
                "No writable settings file exists. Rebuild canonical settings now?\n\n"
                "Choose No to exit without writing."
            ),
        ):
            return None
        try:
            ensure_settings_parent(target)
            ensure_default_directories(target.parent)
        except SettingsConflict:
            dialogs.error(
                title=APP_NAME,
                message=_("Settings could not be prepared safely."),
            )
            return None
    store = SettingsStore(target)
    try:
        store.open()
    except SettingsConflict:
        dialogs.error(
            title=APP_NAME,
            message=_("Another settings editor is active. Close it and reopen settings."),
        )
        return None
    if location.setup_required:
        try:
            store.save(default_settings_document(user_root=target.parent))
        except Exception:
            store.close()
            raise
    return store


def _initial_auto_detect(
    application: SettingsApplication,
    controller: SettingsController,
) -> None:
    controller.auto_detect(manual=False)
    if application.notebook is not None:
        application.notebook.select(TAB_IDS.index("comsol_java"))


def run() -> int:
    try:
        root = tk.Tk()
    except tk.TclError:
        publish_handshake("gui_runtime_unavailable")
        return 2
    root.withdraw()
    dialogs = Dialogs()
    default_translator = Translator("zh_CN")
    instance_lock: SettingsGuiInstanceLock | None = None
    try:
        location = resolve_settings_location()
        instance_lock = SettingsGuiInstanceLock(location.writable_path).acquire()
    except GuiAlreadyRunning:
        if not publish_handshake("already_running"):
            dialogs.error(
                title=APP_NAME,
                message=default_translator(
                    "Another settings editor is active. Close it and reopen settings."
                ),
            )
        root.destroy()
        return 0
    except OSError, SettingsError:
        publish_handshake("launch_failed")
        dialogs.error(
            title=APP_NAME,
            message=default_translator("Settings could not be prepared safely."),
        )
        root.destroy()
        return 2
    publish_handshake("ready")
    try:
        store = _prepare_store(root, dialogs, location=location)
    except OSError, SettingsError:
        publish_handshake("launch_failed")
        dialogs.error(
            title=APP_NAME,
            message=default_translator("Settings could not be prepared safely."),
        )
        root.destroy()
        instance_lock.close()
        return 2
    if store is None:
        root.destroy()
        instance_lock.close()
        return 1
    try:
        try:
            raw = _load_startup_document(store)
        except DamagedSettings, FileNotFoundError:
            if not dialogs.rebuild_or_exit(
                title=APP_NAME,
                message=default_translator(
                    "Settings are missing or damaged. Preserve them and rebuild defaults?"
                ),
            ):
                store.close()
                root.destroy()
                return 1
            store.ownership.release_target_handle()
            store.rebuild()
            raw = _load_startup_document(store)
        model = SettingsFormModel.from_raw(raw)
        controller = SettingsController(model, store, dialogs=dialogs)
        application = SettingsApplication(root, controller)
        root.deiconify()
        root.lift()
        if get_value(model.document, "comsol.installation_root") is None:
            root.after_idle(lambda: _initial_auto_detect(application, controller))
        root.mainloop()
        return 0
    except Exception:
        store.close()
        root.destroy()
        raise
    finally:
        instance_lock.close()


__all__ = [
    "ICON_PATH",
    "SettingsApplication",
    "_apply_window_icon",
    "_initial_auto_detect",
    "_load_startup_document",
    "run",
]
