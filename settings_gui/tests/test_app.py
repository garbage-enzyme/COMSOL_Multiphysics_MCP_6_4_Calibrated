"""Bounded real-Tk construction smoke tests."""

from __future__ import annotations

import os
import subprocess
import sys
import tkinter as tk
from copy import deepcopy
from pathlib import Path

import pytest

from comsol_mcp.settings import (
    SettingsLocation,
    default_settings_document,
    normalize_settings_document,
)
from settings_gui.app import (
    ICON_PATH,
    SettingsApplication,
    _initial_auto_detect,
    _load_startup_document,
    _prepare_store,
)
from settings_gui.comsol_discovery import DiscoveryResult
from settings_gui.controller import SettingsController
from settings_gui.model import TAB_IDS, SettingsFormModel, get_value
from settings_gui.storage import DamagedSettings

pytestmark = pytest.mark.skipif(os.name != "nt", reason="alpha6 GUI supports Windows only")


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


def _scenario_constructs_every_tab_and_field() -> None:
    root, app, _controller, store = _application(scaling=2.0 * 96.0 / 72.0)
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
        assert "alpha6  |  0.6.0" in header_labels
        assert set(app.variables) == {
            "schema_name",
            "schema_version",
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
            "evidence_integrity.checks.outcome_contract_validation",
            "evidence_integrity.checks.artifact_chain_verification",
            "evidence_integrity.checks.summary_claim_verification",
            "evidence_integrity.checks.producer_driver_compatibility",
            "semantic_docs.root",
            "semantic_docs.lexical_index",
            "semantic_docs.model_path",
            "ownership.owner",
        }
        app.notebook.select(TAB_IDS.index("about"))
        root.geometry("960x640")
        root.attributes("-alpha", 0.0)
        root.deiconify()
        root.update_idletasks()
        root.update()
        assert app.save_button is not None
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
        assert app.notebook.tab(0, "text") == "General"
        assert app.notebook.index(app.notebook.select()) == 3
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

        app.variables["gui.scale"].set("Follow Windows display settings (system)")
        root.update_idletasks()
        assert get_value(controller.model.document, "gui.scale") == "system"
        assert float(root.tk.call("tk", "scaling")) == pytest.approx(observed_system_scaling)
    finally:
        app.close()


def _scenario_profile_help_changes() -> None:
    document = default_settings_document()
    document["gui"]["language"] = "en"
    root, app, controller, _store = _application(document)
    try:
        initial = app.help_labels["profile.name"].cget("text")
        app.variables["profile.name"].set("wave_optics")
        root.update_idletasks()

        changed = app.help_labels["profile.name"].cget("text")
        assert get_value(controller.model.document, "profile.name") == "wave_optics"
        assert changed != initial
        assert "optical and metasurface work" in changed
        assert "Wave Optics" in changed
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


_TK_SCENARIOS = {
    "construct": _scenario_constructs_every_tab_and_field,
    "initial-auto-detect": _scenario_initial_auto_detect,
    "invalid": _scenario_invalid_entry,
    "legacy-comments": _scenario_legacy_comments,
    "language": _scenario_language_rebuild,
    "profile-help": _scenario_profile_help_changes,
    "scale": _scenario_scale_rebuild,
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


def test_language_rebuild_preserves_unsaved_values_and_tab() -> None:
    _run_tk_scenario("language")


def test_initial_auto_detect_reveals_paths_and_button_without_modal() -> None:
    _run_tk_scenario("initial-auto-detect")


def test_scale_rebuild_applies_immediately_and_preserves_unsaved_state() -> None:
    _run_tk_scenario("scale")


def test_profile_help_changes_with_selected_profile() -> None:
    _run_tk_scenario("profile-help")


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
        program_root = Path(os.environ["PROGRAMDATA"]) / "comsol_mcp"
        assert (program_root / "runtime").is_dir()
        assert (program_root / "artifacts").is_dir()
    finally:
        store.close()


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] != "--tk-scenario":
        raise SystemExit("usage: python -m settings_gui.tests.test_app --tk-scenario NAME")
    try:
        scenario = _TK_SCENARIOS[sys.argv[2]]
    except KeyError as exc:
        raise SystemExit(f"unknown Tk scenario: {sys.argv[2]}") from exc
    scenario()
