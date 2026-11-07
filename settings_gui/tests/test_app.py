"""Bounded real-Tk construction smoke tests."""

from __future__ import annotations

import os
import subprocess
import sys
import tkinter as tk
from copy import deepcopy
from pathlib import Path

import pytest

import settings_gui.app as app_module
from comsol_mcp.settings import (
    SETTINGS_SCHEMA,
    SETTINGS_VERSION,
    SettingsLocation,
    default_settings_document,
    normalize_settings_document,
)
from settings_gui.app import (
    ICON_PATH,
    ManualIndexProgressDialog,
    SettingsApplication,
    _initial_auto_detect,
    _load_startup_document,
    _prepare_store,
)
from settings_gui.comsol_discovery import DiscoveryResult
from settings_gui.controller import SettingsController
from settings_gui.model import TAB_IDS, SettingsFormModel, get_value
from settings_gui.storage import DamagedSettings

pytestmark = pytest.mark.skipif(
    os.name != "nt" and not os.environ.get("DISPLAY"),
    reason="Real Tk tests require an X display on POSIX; run with xvfb-run",
)


class FakeOwnership:
    def verify_unchanged(self) -> None:
        return None


class FakeStore:
    def __init__(self, document: dict | None = None) -> None:
        self.document = deepcopy(document or default_settings_document())
        self.saved: list[dict] = []
        self.closed = False
        self.ownership = FakeOwnership()

    def load(self) -> dict:
        return deepcopy(self.document)

    def save(self, document: dict) -> str:
        self.saved.append(deepcopy(document))
        return "0" * 64

    def close(self) -> None:
        self.closed = True


class QuietDialogs:
    def confirm(self, *, title: str, message: str) -> bool:
        return True

    def info(self, *, title: str, message: str) -> None:
        return None

    def error(self, *, title: str, message: str) -> None:
        return None

    def ask_directory(self, *, title: str) -> str:
        return ""

    def ask_file(self, *, title: str) -> str:
        return ""

    def ask_save_file(self, *, title: str) -> str:
        return ""

    def rebuild_or_exit(self, *, title: str, message: str) -> bool:
        return True


def _application(document: dict | None = None, *, scaling: float | None = None):
    root = tk.Tk()
    root.withdraw()
    if scaling is not None:
        root.tk.call("tk", "scaling", scaling)
    store = FakeStore(document)
    model = SettingsFormModel.from_raw(store.document)
    controller = SettingsController(model, store, dialogs=QuietDialogs())
    app = SettingsApplication(root, controller)
    root.update_idletasks()
    assert ICON_PATH.is_file()
    assert app.icon_loaded is True
    return root, app, controller, store


def _field_keys(app: SettingsApplication, tab_id: str) -> list[str]:
    """Setting keys rendered on one tab, in creation (display) order.

    Labels show human-readable titles (batch I*) and feature toggles sit in
    a labelled frame, so keys are read from the help-label registry and
    filtered by the tab that contains each widget.
    """
    content = app.tabs[tab_id].content

    def inside(widget) -> bool:
        while widget is not None:
            if widget is content:
                return True
            widget = widget.master
        return False

    return [key for key, label in app.help_labels.items() if inside(label)]


def _boolean_widget(app: SettingsApplication, key: str):
    field_frame = app.help_labels[key].master
    return next(
        child for child in field_frame.winfo_children() if child.winfo_class() == "TCheckbutton"
    )


def test_roots_field_refreshes_visible_validation_error() -> None:
    root, app, controller, store = _application()
    try:
        controller.update("paths.model_read_roots", ["relative"])

        assert app.error_labels["paths.model_read_roots"].cget("text")
    finally:
        app.close()


def _scenario_constructs_every_tab_and_field() -> None:
    root, app, controller, store = _application(scaling=2.0 * 96.0 / 72.0)
    try:
        assert app.notebook is not None
        assert len(app.notebook.tabs()) == len(TAB_IDS)
        assert app.outer is not None
        header_labels = {
            str(child.cget("text"))
            for row in app.outer.winfo_children()
            for child in row.winfo_children()
            if child.winfo_class() == "TLabel"
        }
        assert "alpha8.1  |  0.8.1" in header_labels
        # The schema identity is read-only information on the About tab
        # (batch I*): it has no editable variable, but it must be displayed.
        assert set(app.variables) == {
            "gui.language",
            "gui.scale",
            "profile.name",
            "runtime.directory",
            "runtime.jobs_directory",
            "paths.model_read_roots",
            "paths.artifact_write_root",
            "comsol.installation_root",
            "java.java_home",
            "java.jdk_home",
            "shared_server.enabled",
            "model_manager.enabled",
            "model_manager.upload_enabled",
            "discovery.pagination_enabled",
            "evidence_integrity.checks.outcome_contract_validation",
            "evidence_integrity.checks.artifact_chain_verification",
            "evidence_integrity.checks.summary_claim_verification",
            "evidence_integrity.checks.producer_driver_compatibility",
            "manuals.root",
            "lexical_docs.enabled",
            "lexical_docs.index_path",
            "semantic_docs.enabled",
            "semantic_docs.root",
            "semantic_docs.model_path",
            "ownership.owner",
        }
        assert "shared" not in TAB_IDS
        assert _field_keys(app, "profile") == [
            "profile.name",
            "shared_server.enabled",
            "discovery.pagination_enabled",
            "model_manager.enabled",
            "model_manager.upload_enabled",
        ]
        assert _field_keys(app, "docs")[:6] == [
            "manuals.root",
            "lexical_docs.enabled",
            "lexical_docs.index_path",
            "semantic_docs.enabled",
            "semantic_docs.root",
            "semantic_docs.model_path",
        ]
        lexical_frame = app.help_labels["lexical_docs.index_path"].master
        generate_button = next(
            child
            for container in lexical_frame.winfo_children()
            for child in container.winfo_children()
            if child.winfo_class() == "TButton"
            and child.cget("text") == controller.text("Generate Index")
        )
        assert int(generate_button.master.grid_info()["row"]) == 1
        about_labels = {
            str(child.cget("text"))
            for child in app.tabs["about"].content.winfo_children()
            if child.winfo_class() == "TLabel"
        }
        assert any(SETTINGS_SCHEMA in text for text in about_labels)
        assert any(SETTINGS_VERSION in text for text in about_labels)
        app.notebook.select(TAB_IDS.index("about"))
        root.geometry("960x640")
        root.attributes("-alpha", 0.0)
        root.deiconify()
        root.update_idletasks()
        root.update()
        assert app.save_button is not None
        assert set(app.shortcut_buttons) == {"create", "remove"}
        assert app.shortcut_buttons["create"].cget("text") == controller.text(
            "Create desktop shortcut"
        )
        assert app.shortcut_buttons["remove"].cget("text") == controller.text(
            "Remove desktop shortcut"
        )
        repository_buttons = app.fixed_link_buttons[:3]
        license_button = app.fixed_link_buttons[3]
        assert len({str(button.master) for button in repository_buttons}) == 1
        assert all(button.pack_info()["side"] == "left" for button in repository_buttons)
        assert license_button.master != repository_buttons[0].master
        about_children = app.tabs["about"].content.winfo_children()
        copyright_label = next(
            child
            for child in about_children
            if child.winfo_class() == "TLabel"
            and child.cget("text") == "Copyright © 2026 garbage-enzyme"
        )
        assert (
            about_children.index(repository_buttons[0].master)
            < about_children.index(copyright_label)
            < about_children.index(license_button)
        )
        actions = app.save_button.master
        assert actions.winfo_height() >= actions.winfo_reqheight()
        assert (
            actions.winfo_rooty() + actions.winfo_height()
            <= root.winfo_rooty() + root.winfo_height()
        )
    finally:
        app.close()
    assert store.closed is True


def _scenario_invalid_entry() -> None:
    document = default_settings_document()
    document["runtime"]["directory"] = "relative"
    root, app, controller, _store = _application(document)
    try:
        assert controller.model.valid is False
        assert app.entries["runtime.directory"].cget("style") == "Invalid.TEntry"
        assert app.save_button is not None
        assert app.apply_button is not None
        assert "disabled" in app.save_button.state()
        assert "disabled" in app.apply_button.state()
    finally:
        app.close()


def _scenario_legacy_comments() -> None:
    document = default_settings_document()
    document["_comment"] = "Shared COMSOL MCP startup settings."
    document["profile"]["_comment"] = "Static tool profile."
    root, app, controller, _store = _application(document)
    try:
        assert controller.model.valid is True
        assert app.save_button is not None
        assert app.apply_button is not None
        assert "disabled" not in app.save_button.state()
        assert "disabled" not in app.apply_button.state()
    finally:
        app.close()


def _scenario_language_rebuild() -> None:
    root, app, controller, _store = _application()
    try:
        assert app.notebook is not None
        app.notebook.select(3)
        controller.update("ownership.owner", "operator")
        app.variables["gui.language"].set("English (en)")
        root.update_idletasks()

        assert get_value(controller.model.document, "ownership.owner") == "operator"
        assert get_value(controller.model.document, "gui.language") == "en"
        assert app.variables["gui.language"].get() == "English (en)"
        assert app.notebook.tab(0, "text") == "Interface and Language"
        assert app.notebook.index(app.notebook.select()) == 3
    finally:
        app.close()


def _scenario_immediate_gui_fields_do_not_request_restart() -> None:
    document = default_settings_document()
    root, app, controller, store = _application(document)
    try:
        assert app.banner is not None and app.banner.winfo_manager() == ""

        app.variables["gui.language"].set("English (en)")
        root.update_idletasks()
        assert app.banner is not None and app.banner.winfo_manager() == ""

        app.variables["gui.scale"].set("125%")
        root.update_idletasks()
        assert app.banner is not None and app.banner.winfo_manager() == ""
        assert controller.apply() is True
        root.update_idletasks()

        assert controller.restart_pending is False
        assert controller.model.restart_required is False
        assert app.banner is not None and app.banner.winfo_manager() == ""
        assert len(store.saved) == 1
    finally:
        app.close()


def _scenario_scale_rebuild() -> None:
    document = default_settings_document()
    document["gui"]["language"] = "en"
    system_scaling = 96.0 / 72.0
    root, app, controller, _store = _application(document, scaling=system_scaling)
    try:
        assert app.notebook is not None
        observed_system_scaling = float(root.tk.call("tk", "scaling"))
        root.tk.call("tk", "scaling", 2.0 * 96.0 / 72.0)
        observed_200_scaling = float(root.tk.call("tk", "scaling"))
        root.tk.call("tk", "scaling", observed_system_scaling)
        assert float(root.tk.call("tk", "scaling")) == pytest.approx(observed_system_scaling)

        app.notebook.select(TAB_IDS.index("ownership"))
        controller.update("ownership.owner", "unsaved owner")
        app.variables["gui.scale"].set("200%")
        root.update_idletasks()

        assert get_value(controller.model.document, "gui.scale") == "200"
        assert float(root.tk.call("tk", "scaling")) == pytest.approx(observed_200_scaling)
        assert get_value(controller.model.document, "ownership.owner") == "unsaved owner"
        assert app.notebook.index(app.notebook.select()) == TAB_IDS.index("ownership")

        app.variables["gui.scale"].set("Follow system display settings (system)")
        root.update_idletasks()
        assert get_value(controller.model.document, "gui.scale") == "system"
        assert float(root.tk.call("tk", "scaling")) == pytest.approx(observed_system_scaling)
    finally:
        app.close()


def _scenario_profile_help_changes() -> None:
    document = default_settings_document()
    document["gui"]["language"] = "en"
    document["profile"]["name"] = "wave_optics"
    document["profile"]["_comment"] = "Core profile comment from an older settings file."
    root, app, controller, _store = _application(document)
    try:
        initial = app.help_labels["profile.name"].cget("text")
        assert "optical and metasurface work" in initial
        assert "Wave Optics" in initial

        app.variables["profile.name"].set("basic_fem")
        root.update_idletasks()

        changed = app.help_labels["profile.name"].cget("text")
        assert get_value(controller.model.document, "profile.name") == "basic_fem"
        assert changed != initial
        assert "most users" in changed
    finally:
        app.close()


def _scenario_feature_checkboxes_compose() -> None:
    document = default_settings_document()
    document["gui"]["language"] = "en"
    root, app, controller, _store = _application(document)
    try:
        _boolean_widget(app, "shared_server.enabled").invoke()
        root.update_idletasks()

        assert get_value(controller.model.document, "profile.name") == "core"
        assert get_value(controller.model.document, "shared_server.enabled") is True
        assert get_value(controller.model.document, "lexical_docs.enabled") is False
        assert get_value(controller.model.document, "semantic_docs.enabled") is False
        assert app.variables["shared_server.enabled"].get() is True
        assert app.variables["lexical_docs.enabled"].get() is False
        assert app.variables["semantic_docs.enabled"].get() is False
        assert app.banner is not None and app.banner.winfo_manager() == "pack"
    finally:
        app.close()


def test_index_progress_cancel_footer_stays_visible_at_high_scaling() -> None:
    class FakeTask:
        running = True

        def __init__(self) -> None:
            self.cancelled = False
            self._events = [
                {
                    "event": "progress",
                    "percent": 50,
                    "processed_files": 1,
                    "total_files": 1,
                    "processed_pages": 1,
                    "total_pages": 1,
                    "current_source": "deep\\" + ("long-name-" * 200) + ".pdf",
                }
            ]

        def drain_events(self):
            events, self._events = self._events, []
            return events

        def cancel(self) -> None:
            self.cancelled = True

    root = tk.Tk()
    root.withdraw()
    root.tk.call("tk", "scaling", 2.6666666667)
    controller = SettingsController(
        SettingsFormModel(default_settings_document()), FakeStore(), dialogs=QuietDialogs()
    )
    task = FakeTask()
    dialog = ManualIndexProgressDialog(root, controller, task, on_close=lambda: None)
    try:
        dialog.window.deiconify()
        dialog.poll()
        root.update_idletasks()
        bottom = dialog.window.winfo_rooty() + dialog.window.winfo_height()
        assert bottom <= dialog.window.winfo_screenheight()
        assert dialog.cancel_button.winfo_manager() == "pack"
        assert dialog.cancel_button.winfo_width() > 0
        assert len(dialog.detail.get()) < 180
        dialog.cancel()
        assert task.cancelled is True
    finally:
        dialog.close()
        root.destroy()


def _scenario_doc_path_controls_follow_feature_gates() -> None:
    document = default_settings_document()
    document["gui"]["language"] = "en"
    root, app, controller, _store = _application(document)
    try:
        controller.model.update("lexical_docs.index_path", "D:/docs/manuals.sqlite3")
        controller.model.update("semantic_docs.root", "D:/docs/semantic")
        controller.model.update("semantic_docs.model_path", "D:/docs/model")
        root.update_idletasks()
        assert all(
            "disabled" in widget.state() for widget in app.field_widgets["lexical_docs.index_path"]
        )
        assert all(
            "disabled" in widget.state()
            for key in ("semantic_docs.root", "semantic_docs.model_path")
            for widget in app.field_widgets[key]
        )

        _boolean_widget(app, "lexical_docs.enabled").invoke()
        _boolean_widget(app, "semantic_docs.enabled").invoke()
        root.update_idletasks()
        assert all(
            "disabled" not in widget.state()
            for widget in app.field_widgets["lexical_docs.index_path"]
        )
        assert all(
            "disabled" not in widget.state()
            for key in ("semantic_docs.root", "semantic_docs.model_path")
            for widget in app.field_widgets[key]
        )

        _boolean_widget(app, "lexical_docs.enabled").invoke()
        _boolean_widget(app, "semantic_docs.enabled").invoke()
        root.update_idletasks()
        assert (
            get_value(controller.model.document, "lexical_docs.index_path")
            == "D:/docs/manuals.sqlite3"
        )
        assert get_value(controller.model.document, "semantic_docs.root") == "D:/docs/semantic"
        assert all(
            "disabled" in widget.state() for widget in app.field_widgets["lexical_docs.index_path"]
        )
    finally:
        app.close()


def _scenario_initial_auto_detect() -> None:
    root = tk.Tk()
    root.withdraw()
    store = FakeStore()
    dialogs = QuietDialogs()
    infos: list[tuple[str, str]] = []
    dialogs.info = lambda **kwargs: infos.append((kwargs["title"], kwargs["message"]))
    detected_root = Path("D:/COMSOL64/Multiphysics")
    detected_java = detected_root / "java/win64/jre"
    controller = SettingsController(
        SettingsFormModel.from_raw(store.document),
        store,
        dialogs=dialogs,
        discover=lambda **_kwargs: DiscoveryResult(
            detected_root,
            detected_java,
            "comsol_bundled",
        ),
    )
    app = SettingsApplication(root, controller)
    try:
        _initial_auto_detect(app, controller)
        root.update_idletasks()

        assert app.notebook is not None
        assert app.notebook.index(app.notebook.select()) == TAB_IDS.index("comsol_java")
        assert get_value(controller.model.document, "comsol.installation_root") == str(
            detected_root
        )
        assert get_value(controller.model.document, "java.java_home") == str(detected_java)
        assert infos == []
        assert app.banner is not None and app.banner.winfo_manager() == "pack"
        controls = app.entries["comsol.installation_root"].master.winfo_children()
        button_labels = {
            child.cget("text")
            for container in controls
            for child in container.winfo_children()
            if child.winfo_class() == "TButton"
        }
        assert controller.text("Auto-detect") in button_labels
    finally:
        app.close()


def _transport_button(app: SettingsApplication, controller: SettingsController, label: str):
    wanted = controller.text(label)
    stack = [app.tabs["transport"].content]
    while stack:
        widget = stack.pop()
        if widget.winfo_class() == "TButton" and widget.cget("text") == wanted:
            return widget
        stack.extend(widget.winfo_children())
    raise AssertionError(f"button {label!r} not found")


def _patched_transport(*, observation=None, check=None):
    """Replace the host observer and binary check used by the real callbacks."""
    import settings_gui.transport as transport_module

    original = (transport_module.observe_hosts, transport_module.check_shim_binary)
    if observation is not None:
        transport_module.observe_hosts = lambda *_args, **_kwargs: observation
    if check is not None:
        transport_module.check_shim_binary = lambda *_args, **_kwargs: check
    return transport_module, original


def _restore_transport(transport_module, original) -> None:
    transport_module.observe_hosts, transport_module.check_shim_binary = original


def _verified_check():
    import settings_gui.transport as transport_module

    return transport_module.BinaryCheck(
        True,
        "verified",
        path=str(Path(sys.executable)),
        sha256="a" * 64,
        version="0.8.2-dev",
        platform="x86_64-pc-windows-msvc",
    )


def _scenario_transport_refuses_absent_binary() -> None:
    import settings_gui.transport as transport_module

    module, original = _patched_transport(observation=transport_module.HostObservation())
    root, app, controller, store = _application()
    try:
        app._transport_var.set("shim")
        app._shim_path_var.set(str(Path(sys.executable).with_name("absent-shim.exe")))
        app._shim_sha256_var.set("0" * 64)
        _transport_button(app, controller, "Confirm transport switch").invoke()
        root.update_idletasks()
        assert store.saved == []
        assert str(app._switch_status.cget("text")) == controller.text(
            "Refused: the standalone program must pass verification before switching "
            "to Rust shim mode."
        )
        assert str(app._shim_check_label.cget("text")) == controller.text("File not found.")
        assert get_value(controller.model.document, "transport") == "python"
    finally:
        app.close()
        _restore_transport(module, original)


def _scenario_transport_never_verifies_a_text_file() -> None:
    import hashlib
    import tempfile

    import settings_gui.transport as transport_module

    module, original = _patched_transport(observation=transport_module.HostObservation())
    root, app, controller, store = _application()
    with tempfile.TemporaryDirectory() as directory:
        text_file = Path(directory) / "not-a-shim.txt"
        text_file.write_text("This is not an executable.", encoding="utf-8")
        try:
            app._transport_var.set("shim")
            app._shim_path_var.set(str(text_file))
            app._shim_sha256_var.set(hashlib.sha256(text_file.read_bytes()).hexdigest())
            _transport_button(app, controller, "Check program").invoke()
            root.update_idletasks()
            label = str(app._shim_check_label.cget("text"))
            verified_prefix = controller.text(
                "Verified. SHA-256, version and platform match. Version: {version} ({platform})"
            ).split("{")[0]
            assert not label.startswith(verified_prefix), label
            assert str(app._shim_check_label.cget("foreground")) == "#b00"
            _transport_button(app, controller, "Confirm transport switch").invoke()
            root.update_idletasks()
            assert store.saved == []
            assert str(app._switch_status.cget("text")) == controller.text(
                "Refused: the standalone program must pass verification before switching "
                "to Rust shim mode."
            )
        finally:
            app.close()
            _restore_transport(module, original)


def _scenario_transport_refuses_unknown_process_state() -> None:
    import settings_gui.transport as transport_module

    observation = transport_module.HostObservation(
        unknowns=["python.exe (PID 4242): its command line cannot be read"]
    )
    module, original = _patched_transport(observation=observation, check=_verified_check())
    root, app, controller, store = _application()
    try:
        app._transport_var.set("shim")
        _transport_button(app, controller, "Confirm transport switch").invoke()
        root.update_idletasks()
        status = str(app._switch_status.cget("text"))
        assert store.saved == []
        assert status == controller.text(
            "Refused: the running state is UNKNOWN ({found}). Resolve it (or grant "
            "access) and try again."
        ).format(found="python.exe (PID 4242): its command line cannot be read"), status
    finally:
        app.close()
        _restore_transport(module, original)


def _scenario_transport_refuses_running_host() -> None:
    import settings_gui.transport as transport_module

    observation = transport_module.HostObservation(hosts=["python.exe (PID 77)"])
    module, original = _patched_transport(observation=observation, check=_verified_check())
    root, app, controller, store = _application()
    try:
        app._transport_var.set("shim")
        _transport_button(app, controller, "Confirm transport switch").invoke()
        root.update_idletasks()
        status = str(app._switch_status.cget("text"))
        assert store.saved == []
        assert status == controller.text(
            "Refused: server processes are still running ({found}). Close them and try again."
        ).format(found="python.exe (PID 77)"), status
    finally:
        app.close()
        _restore_transport(module, original)


def _scenario_transport_refuses_with_unsaved_edits() -> None:
    import settings_gui.transport as transport_module

    module, original = _patched_transport(
        observation=transport_module.HostObservation(), check=_verified_check()
    )
    root, app, controller, store = _application()
    try:
        controller.update("ownership.owner", "operator", show_dirty_notice=False)
        app._transport_var.set("shim")
        _transport_button(app, controller, "Confirm transport switch").invoke()
        root.update_idletasks()
        assert store.saved == []
        assert str(app._switch_status.cget("text")) == controller.text(
            "Save or discard the other changes before switching transport."
        )
    finally:
        app.close()
        _restore_transport(module, original)


def _scenario_transport_saves_configured_mode_only() -> None:
    import settings_gui.transport as transport_module

    module, original = _patched_transport(
        observation=transport_module.HostObservation(), check=_verified_check()
    )
    root, app, controller, store = _application()
    try:
        app._transport_var.set("shim")
        _transport_button(app, controller, "Confirm transport switch").invoke()
        root.update_idletasks()
        assert len(store.saved) == 1
        saved = store.saved[0]
        assert saved["transport"] == "shim"
        assert saved["shim_binary"] == {
            "path": str(Path(sys.executable)),
            "sha256": "a" * 64,
            "version": "0.8.2-dev",
            "platform": "x86_64-pc-windows-msvc",
        }
        runtime_directory = saved["runtime"]["directory"]
        assert "%" not in runtime_directory and Path(runtime_directory).is_absolute()
        assert normalize_settings_document(deepcopy(saved))["errors"] == []
        assert get_value(controller.model.document, "transport") == "shim"
        assert controller.model.dirty is False
        configured = str(app._transport_mode_label.cget("text"))
        active = str(app._transport_active_label.cget("text"))
        assert configured == controller.text("Configured mode: {mode}").format(mode="shim")
        # A configuration-only save never reports the requested mode as active.
        assert active == controller.text("Verified active host: none"), active
        assert "shim" not in active
        assert str(app._switch_status.cget("text")) == controller.text(
            "Saved: configured mode is now {mode}. It becomes active only after an agent "
            "client starts it; this window does not claim it is active."
        ).format(mode="shim")
    finally:
        app.close()
        _restore_transport(module, original)


_TK_SCENARIOS = {
    "construct": _scenario_constructs_every_tab_and_field,
    "initial-auto-detect": _scenario_initial_auto_detect,
    "immediate-no-restart": _scenario_immediate_gui_fields_do_not_request_restart,
    "invalid": _scenario_invalid_entry,
    "legacy-comments": _scenario_legacy_comments,
    "language": _scenario_language_rebuild,
    "profile-help": _scenario_profile_help_changes,
    "feature-checkboxes": _scenario_feature_checkboxes_compose,
    "doc-path-gates": _scenario_doc_path_controls_follow_feature_gates,
    "scale": _scenario_scale_rebuild,
    "transport-absent-binary": _scenario_transport_refuses_absent_binary,
    "transport-text-file": _scenario_transport_never_verifies_a_text_file,
    "transport-unknown": _scenario_transport_refuses_unknown_process_state,
    "transport-running": _scenario_transport_refuses_running_host,
    "transport-dirty": _scenario_transport_refuses_with_unsaved_edits,
    "transport-save": _scenario_transport_saves_configured_mode_only,
}


def _run_tk_scenario(name: str) -> None:
    completed = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "settings_gui.tests.test_app", "--tk-scenario", name],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert completed.returncode == 0, (
        f"Tk scenario {name!r} failed with exit code {completed.returncode}\n"
        f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
    )


def test_constructs_every_tab_and_field_without_mainloop() -> None:
    _run_tk_scenario("construct")


def test_invalid_entry_is_red_and_write_actions_are_disabled() -> None:
    _run_tk_scenario("invalid")


def test_official_legacy_comments_keep_write_actions_enabled() -> None:
    _run_tk_scenario("legacy-comments")


@pytest.mark.parametrize(
    "scenario",
    [
        "transport-absent-binary",
        "transport-text-file",
        "transport-unknown",
        "transport-running",
        "transport-dirty",
        "transport-save",
    ],
)
def test_transport_switch_behaviour_through_the_real_buttons(scenario: str) -> None:
    _run_tk_scenario(scenario)


def test_language_rebuild_preserves_unsaved_values_and_tab() -> None:
    _run_tk_scenario("language")


def test_immediate_gui_fields_never_show_restart_state_even_after_apply() -> None:
    _run_tk_scenario("immediate-no-restart")


def test_initial_auto_detect_reveals_paths_and_button_without_modal() -> None:
    _run_tk_scenario("initial-auto-detect")


def test_scale_rebuild_applies_immediately_and_preserves_unsaved_state() -> None:
    _run_tk_scenario("scale")


def test_profile_help_changes_with_selected_profile() -> None:
    _run_tk_scenario("profile-help")


def test_feature_checkboxes_are_independent_and_composable() -> None:
    _run_tk_scenario("feature-checkboxes")


def test_document_path_controls_follow_feature_gates_and_retain_values() -> None:
    _run_tk_scenario("doc-path-gates")


@pytest.mark.parametrize("version", ["9.0.0", 2, None])
def test_future_or_invalid_schema_version_requires_rebuild(version) -> None:
    document = default_settings_document()
    document["schema_version"] = version

    with pytest.raises(DamagedSettings, match="version"):
        _load_startup_document(FakeStore(document))


@pytest.mark.parametrize(
    "mutation",
    [
        lambda document: document.update({"unknown_group": {"value": 1}}),
        lambda document: document.update({"runtime": "not-an-object"}),
    ],
)
def test_uneditable_structure_requires_rebuild(mutation) -> None:
    document = default_settings_document()
    mutation(document)

    with pytest.raises(DamagedSettings, match="unsupported structure"):
        _load_startup_document(FakeStore(document))


def test_invalid_known_leaf_remains_editable() -> None:
    document = default_settings_document()
    document["runtime"]["directory"] = "relative"

    assert _load_startup_document(FakeStore(document)) == document


def test_first_run_writes_only_after_rebuild_confirmation(tmp_path: Path) -> None:
    target = tmp_path / "new-parent" / "settings.json"
    template = tmp_path / "bundled-settings.json"
    template.write_text("{}", encoding="utf-8")
    location = SettingsLocation(template, target, "bundled_template", True)

    declined = QuietDialogs()
    declined.rebuild_or_exit = lambda **_kwargs: False
    assert _prepare_store(object(), declined, location=location) is None
    assert target.parent.exists() is False

    store = _prepare_store(object(), QuietDialogs(), location=location)
    assert store is not None
    try:
        expected = normalize_settings_document(default_settings_document(user_root=target.parent))[
            "settings"
        ]
        assert store.load() == expected
        assert (target.parent / "models").is_dir()
        assert Path(expected["runtime"]["directory"]).is_dir()
        assert Path(expected["paths"]["artifact_write_root"]).is_dir()
    finally:
        store.close()


def test_first_run_save_failure_closes_open_store(tmp_path: Path, monkeypatch) -> None:
    target = tmp_path / "new-parent" / "settings.json"
    template = tmp_path / "bundled-settings.json"
    template.write_text("{}", encoding="utf-8")
    location = SettingsLocation(template, target, "bundled_template", True)

    class FailingStore:
        closed = False

        def __init__(self, _target):
            pass

        def open(self):
            return self

        def save(self, _document):
            raise OSError("injected save failure")

        def close(self):
            self.closed = True

    store = FailingStore(target)
    monkeypatch.setattr(app_module, "SettingsStore", lambda _target: store)

    with pytest.raises(OSError, match="injected save failure"):
        _prepare_store(object(), QuietDialogs(), location=location)

    assert store.closed is True


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] != "--tk-scenario":
        raise SystemExit("usage: python -m settings_gui.tests.test_app --tk-scenario NAME")
    try:
        scenario = _TK_SCENARIOS[sys.argv[2]]
    except KeyError as exc:
        raise SystemExit(f"unknown Tk scenario: {sys.argv[2]}") from exc
    scenario()
