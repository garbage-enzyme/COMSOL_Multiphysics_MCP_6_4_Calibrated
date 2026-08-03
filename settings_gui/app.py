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
    "shared_server": "Shared",
    "evidence": "Evidence",
    "semantic": "Docs",
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
        self.root_lists: dict[str, tk.Listbox] = {}
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
            self.root_lists.clear()
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
        if self.controller.model.dirty or self.controller.restart_pending:
            self.banner.pack(fill="x", pady=(0, 8))

        self.notebook = ttk.Notebook(outer)
        self.notebook.pack(fill="both", expand=True)
        self.tabs = {}
        for tab_id in TAB_IDS:
            tab = ScrollableTab(self.notebook)
            self.tabs[tab_id] = tab
            self.notebook.add(tab, text=_(TAB_TITLES[tab_id]))
        for field in FIELDS:
            self._add_field(self.tabs[field.tab].content, field)
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
            else:
                command = partial(self.controller.browse_directory, field.key)
            ttk.Button(controls, text=self.controller.text("Browse"), command=command).pack(
                side="left"
            )
            if field.nullable:
                ttk.Button(
                    controls,
                    text=self.controller.text("Clear"),
                    command=lambda key=field.key: self.controller.clear(key),
                ).pack(side="left", padx=(6, 0))
            if field.key == "comsol.installation_root":
                ttk.Button(
                    controls,
                    text=self.controller.text("Auto-detect"),
                    command=lambda: self.controller.auto_detect(manual=True),
                ).pack(side="left", padx=(6, 0))
        self.variables[field.key] = variable
        help_label = ttk.Label(
            frame,
            text=self.controller.text(field.help_id),
            style="Help.TLabel",
            wraplength=620,
            justify="left",
        )
        help_label.grid(row=1, column=1, columnspan=2, sticky="w", pady=(4, 0))
        self.help_labels[field.key] = help_label
        error = self.controller.model.errors.get(field.key, "")
        error_label = ttk.Label(
            frame,
            text=self.controller.text(error) if error else "",
            style="Error.TLabel",
            wraplength=620,
            justify="left",
        )
        error_label.grid(row=2, column=1, columnspan=2, sticky="w", pady=(2, 0))
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
        ttk.Label(parent, text="Copyright (c) 2025").pack(anchor="w", pady=(12, 0))
        ttk.Label(parent, text=_("Repositories and acknowledgements"), font="TkHeadingFont").pack(
            anchor="w",
            pady=(18, 6),
        )
        for label, url in FIXED_LINKS:
            ttk.Button(
                parent,
                text=_(label),
                command=lambda fixed=url: webbrowser.open(fixed),
            ).pack(anchor="w", pady=3)

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
            state = "normal" if self.controller.model.valid else "disabled"
            if self.save_button is not None:
                self.save_button.configure(state=state)
            if self.apply_button is not None:
                self.apply_button.configure(state=state)
            if self.banner is not None and self.notebook is not None:
                if self.controller.model.dirty or self.controller.restart_pending:
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
        store.save(default_settings_document(user_root=target.parent))
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
