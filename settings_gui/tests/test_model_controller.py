"""Field-model and controller state-machine tests."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path

from comsol_mcp.settings import GUI_LANGUAGES, default_settings_document
from comsol_mcp.tools.catalog import PROFILE_NAMES
from settings_gui.comsol_discovery import DiscoveryResult
from settings_gui.controller import SettingsController
from settings_gui.model import FIELDS, SettingsFormModel, get_value, leaf_keys


class FakeOwnership:
    def __init__(self) -> None:
        self.conflict = False

    def verify_unchanged(self) -> None:
        if self.conflict:
            raise RuntimeError("changed")


class FakeStore:
    def __init__(self) -> None:
        self.saved: list[dict] = []
        self.ownership = FakeOwnership()
        self.closed = False

    def save(self, document: dict) -> str:
        self.saved.append(deepcopy(document))
        return "0" * 64

    def close(self) -> None:
        self.closed = True


class FakeDialogs:
    def __init__(self, confirmations: list[bool] | None = None) -> None:
        self.confirmations = list(confirmations or [])
        self.confirm_calls: list[tuple[str, str]] = []
        self.infos: list[tuple[str, str]] = []
        self.errors: list[tuple[str, str]] = []
        self.directory = ""
        self.file = ""

    def confirm(self, *, title: str, message: str) -> bool:
        self.confirm_calls.append((title, message))
        return self.confirmations.pop(0) if self.confirmations else True

    def info(self, *, title: str, message: str) -> None:
        self.infos.append((title, message))

    def error(self, *, title: str, message: str) -> None:
        self.errors.append((title, message))

    def ask_directory(self, *, title: str) -> str:
        return self.directory

    def ask_file(self, *, title: str) -> str:
        return self.file


def _controller(*, dialogs: FakeDialogs | None = None, discover=None):
    model = SettingsFormModel(default_settings_document())
    store = FakeStore()
    kwargs = {"dialogs": dialogs or FakeDialogs()}
    if discover is not None:
        kwargs["discover"] = discover
    return SettingsController(model, store, **kwargs), store, kwargs["dialogs"]


def test_every_settings_leaf_has_one_typed_field_binding() -> None:
    keys = [field.key for field in FIELDS]

    assert len(keys) == len(set(keys))
    assert set(keys) == leaf_keys(default_settings_document())
    assert next(field for field in FIELDS if field.key == "gui.language").choices == GUI_LANGUAGES
    assert next(field for field in FIELDS if field.key == "profile.name").choices == PROFILE_NAMES
    assert next(field for field in FIELDS if field.key == "paths.model_read_roots").kind == "roots"
    assert (
        next(field for field in FIELDS if field.key == "semantic_docs.lexical_index").kind == "file"
    )


def test_invalid_raw_value_is_preserved_until_corrected() -> None:
    model = SettingsFormModel(default_settings_document())

    model.update("runtime.directory", "relative")
    assert get_value(model.document, "runtime.directory") == "relative"
    assert model.valid is False
    assert model.canonical is None
    assert model.errors["runtime.directory"] == (
        "Enter an ASCII-only full path, or leave this setting empty."
    )

    model.update("runtime.directory", None)
    assert model.valid is True
    assert model.canonical is not None
    assert get_value(model.canonical, "runtime.directory") is None


def test_official_legacy_comments_remain_non_editable_metadata() -> None:
    document = default_settings_document()
    document["_comment"] = "Shared COMSOL MCP startup settings."
    document["profile"]["_comment"] = "Static tool profile."

    model = SettingsFormModel.from_raw(document)

    assert model.valid is True
    assert model.dirty is False
    assert model.errors == {}
    assert model.canonical == default_settings_document()
    assert "_comment" not in model.document
    assert "_comment" not in model.document["profile"]


def test_gui_scale_is_a_closed_choice() -> None:
    field = next(field for field in FIELDS if field.key == "gui.scale")
    assert field.kind == "choice"
    assert field.choices == ("system", "100", "125", "150", "200")

    document = default_settings_document()
    document["gui"]["scale"] = "175"
    model = SettingsFormModel.from_raw(document)

    assert model.valid is False
    assert set(model.errors) == {"gui.scale"}


def test_non_ascii_runtime_and_artifact_paths_are_rejected_before_save(tmp_path: Path) -> None:
    model = SettingsFormModel(default_settings_document())

    model.update("runtime.directory", str(tmp_path / "运行"))
    model.update("runtime.jobs_directory", str(tmp_path / "任务"))
    model.update("paths.artifact_write_root", str(tmp_path / "产物"))

    assert model.valid is False
    assert set(model.errors) == {
        "runtime.directory",
        "runtime.jobs_directory",
        "paths.artifact_write_root",
    }
    assert set(model.errors.values()) == {
        "Enter an ASCII-only full path, or leave this setting empty."
    }


def test_invalid_root_element_maps_to_the_visible_roots_control() -> None:
    document = default_settings_document()
    document["paths"]["model_read_roots"] = ["relative"]

    model = SettingsFormModel.from_raw(document)

    assert set(model.errors) == {"paths.model_read_roots"}
    assert model.errors["paths.model_read_roots"] == "Enter a valid absolute path."


def test_canonical_values_keep_json_types(tmp_path: Path) -> None:
    model = SettingsFormModel(default_settings_document())
    root = str(tmp_path.resolve())

    model.update("paths.model_read_roots", [root])
    model.update("shared_server.enabled", True)
    model.update("semantic_docs.model_path", None)

    assert model.canonical is not None
    assert get_value(model.canonical, "paths.model_read_roots") == [root]
    assert get_value(model.canonical, "shared_server.enabled") is True
    assert get_value(model.canonical, "semantic_docs.model_path") is None


def test_dirty_notice_apply_and_next_dirty_cycle() -> None:
    controller, store, dialogs = _controller()

    controller.update("profile.name", "basic_fem")
    controller.update("ownership.owner", "operator")
    assert len(dialogs.infos) == 1
    assert controller.model.dirty is True

    assert controller.apply() is True
    assert controller.model.dirty is False
    assert controller.restart_pending is True
    assert len(store.saved) == 1

    controller.update("ownership.owner", "operator-2")
    assert len(dialogs.infos) == 2


def test_evidence_disable_decline_and_cancel_decline_preserve_state() -> None:
    dialogs = FakeDialogs([False, False])
    controller, _store, _dialogs = _controller(dialogs=dialogs)
    key = "evidence_integrity.checks.outcome_contract_validation"

    assert controller.update(key, False) is False
    assert get_value(controller.model.document, key) is True

    controller.update("ownership.owner", "changed")
    closed = []
    controller.on_close = lambda: closed.append(True)
    controller.cancel()
    assert closed == []


def test_language_switch_preserves_values_and_validation() -> None:
    controller, _store, _dialogs = _controller()
    controller.update("runtime.directory", "relative")

    controller.update("gui.language", "zh-tw")

    assert controller.translator.language == "zh-tw"
    assert controller.text("Apply") == "套用"
    assert get_value(controller.model.document, "runtime.directory") == "relative"
    assert controller.model.valid is False


def test_browser_cancellation_has_no_effect() -> None:
    controller, _store, _dialogs = _controller()
    before = deepcopy(controller.model.document)

    controller.browse_directory("runtime.directory")
    controller.browse_file("semantic_docs.lexical_index")

    assert controller.model.document == before


def test_auto_detect_decline_is_atomic(tmp_path: Path) -> None:
    detected_root = tmp_path / "COMSOL64" / "Multiphysics"
    detected_java = detected_root / "java" / "win64" / "jre"
    result = DiscoveryResult(detected_root, detected_java, "comsol_bundled")
    dialogs = FakeDialogs([False])
    controller, _store, _dialogs = _controller(
        dialogs=dialogs,
        discover=lambda **_kwargs: result,
    )
    controller.model.update("comsol.installation_root", str(tmp_path / "existing"))
    controller.model.update("java.java_home", str(tmp_path / "old-java"))
    controller.model.update("java.jdk_home", str(tmp_path / "old-jdk"))
    before = deepcopy(controller.model.document)

    controller.auto_detect(manual=True)

    assert controller.model.document == before
    assert len(dialogs.confirm_calls) == 1


def test_auto_detect_fills_empty_values_without_replace_prompt(tmp_path: Path) -> None:
    detected_root = tmp_path / "COMSOL64" / "Multiphysics"
    detected_java = detected_root / "java" / "win64" / "jre"
    result = DiscoveryResult(detected_root, detected_java, "comsol_bundled")
    controller, _store, dialogs = _controller(discover=lambda **_kwargs: result)

    controller.auto_detect(manual=False)

    assert get_value(controller.model.document, "comsol.installation_root") == str(detected_root)
    assert get_value(controller.model.document, "java.java_home") == str(detected_java)
    assert get_value(controller.model.document, "java.jdk_home") == str(detected_java)
    assert dialogs.confirm_calls == []
    assert dialogs.infos == []
    assert controller.model.dirty is True

    controller.update("ownership.owner", "operator")
    assert dialogs.infos == []


def test_manual_auto_detect_keeps_the_single_dirty_notice(tmp_path: Path) -> None:
    detected_root = tmp_path / "COMSOL64" / "Multiphysics"
    detected_java = detected_root / "java" / "win64" / "jre"
    result = DiscoveryResult(detected_root, detected_java, "comsol_bundled")
    controller, _store, dialogs = _controller(discover=lambda **_kwargs: result)

    controller.auto_detect(manual=True)

    assert controller.model.dirty is True
    assert len(dialogs.infos) == 1


def test_external_conflict_is_terminal_and_localized() -> None:
    controller, store, dialogs = _controller()
    store.ownership.conflict = True

    assert controller.poll_conflict() is True
    assert len(dialogs.errors) == 1
    assert "changed" not in dialogs.errors[0][1]
