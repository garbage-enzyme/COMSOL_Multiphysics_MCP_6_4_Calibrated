"""Field-model and controller state-machine tests."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path

from comsol_mcp.knowledge.lexical_manual import build_index_from_records
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
        self.target = Path("C:/settings.json")

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
        if not self.confirmations:
            raise AssertionError("unexpected confirmation dialog")
        return self.confirmations.pop(0)

    def info(self, *, title: str, message: str) -> None:
        self.infos.append((title, message))

    def error(self, *, title: str, message: str) -> None:
        self.errors.append((title, message))

    def ask_directory(self, *, title: str) -> str:
        return self.directory

    def ask_file(self, *, title: str) -> str:
        return self.file

    def ask_save_file(self, *, title: str) -> str:
        return self.file


def _controller(*, dialogs: FakeDialogs | None = None, discover=None):
    model = SettingsFormModel(default_settings_document())
    store = FakeStore()
    kwargs = {"dialogs": dialogs or FakeDialogs()}
    if discover is not None:
        kwargs["discover"] = discover
    return SettingsController(model, store, **kwargs), store, kwargs["dialogs"]


def _shortcut_receipt(state: str, *, success: bool) -> dict:
    return {
        "success": success,
        "state": state,
        "contains_local_path": False,
    }


def test_every_settings_leaf_has_one_typed_field_binding() -> None:
    keys = [field.key for field in FIELDS]

    assert len(keys) == len(set(keys))
    assert set(keys) == leaf_keys(default_settings_document())
    assert next(field for field in FIELDS if field.key == "gui.language").choices == GUI_LANGUAGES
    assert next(field for field in FIELDS if field.key == "profile.name").choices == PROFILE_NAMES
    assert next(field for field in FIELDS if field.key == "paths.model_read_roots").kind == "roots"
    assert next(field for field in FIELDS if field.key == "lexical_docs.index_path").kind == (
        "save_file"
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


def test_invalid_profile_remains_visible_instead_of_saving_a_default() -> None:
    document = default_settings_document()
    document["profile"]["name"] = "invalid-profile"

    model = SettingsFormModel.from_raw(document)

    assert model.valid is False
    assert get_value(model.document, "profile.name") == "invalid-profile"
    assert set(model.errors) == {"profile.name"}


def test_roots_tokens_are_normalized_without_discarding_other_raw_values(
    tmp_path: Path, monkeypatch
) -> None:
    local = tmp_path / "local"
    explicit = str((tmp_path / "explicit").resolve())
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    document = default_settings_document()
    document["paths"]["model_read_roots"] = [
        "%LOCALAPPDATA%/models",
        explicit,
    ]

    model = SettingsFormModel.from_raw(document)

    assert get_value(model.document, "paths.model_read_roots") == [
        str(local / "models"),
        explicit,
    ]


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
    assert len(dialogs.infos) == 1


def test_gui_language_and_scale_are_immediate_without_restart_notice_or_pending_state() -> None:
    controller, store, dialogs = _controller()

    controller.update("gui.language", "en")
    controller.update("gui.scale", "125")

    assert controller.model.dirty is True
    assert controller.model.restart_required is False
    assert dialogs.infos == []
    assert controller.apply() is True
    assert controller.restart_pending is False
    assert len(store.saved) == 1

    controller.update("profile.name", "basic_fem")
    assert controller.model.restart_required is True
    assert len(dialogs.infos) == 1


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
    controller.browse_save_file("lexical_docs.index_path")

    assert controller.model.document == before


def test_manual_search_can_be_enabled_before_index_generation(tmp_path: Path) -> None:
    controller, _store, dialogs = _controller()
    missing = tmp_path / "missing.sqlite3"
    controller.model.update("lexical_docs.index_path", str(missing))

    assert controller.update("lexical_docs.enabled", True) is True
    assert get_value(controller.model.document, "lexical_docs.enabled") is True
    assert not dialogs.errors

    build_index_from_records(
        [
            {
                "source": "manual.pdf",
                "module": "manual",
                "page": 1,
                "heading": "Heading",
                "text": "searchable content",
            }
        ],
        missing,
    )

    assert get_value(controller.model.document, "lexical_docs.enabled") is True


def test_semantic_enable_is_editable_before_assets_are_selected(tmp_path: Path) -> None:
    controller, _store, dialogs = _controller()

    assert controller.update("semantic_docs.enabled", True) is True
    assert not dialogs.errors

    index = tmp_path / "manuals.sqlite3"
    build_index_from_records([], index)
    semantic_root = tmp_path / "semantic-index"
    model_path = tmp_path / "model"
    semantic_root.mkdir()
    model_path.mkdir()
    controller.model.update("lexical_docs.index_path", str(index))
    controller.model.update("lexical_docs.enabled", True)
    controller.model.update("semantic_docs.root", str(semantic_root))
    controller.model.update("semantic_docs.model_path", str(model_path))

    assert controller.update("semantic_docs.enabled", True) is True
    assert get_value(controller.model.document, "semantic_docs.enabled") is True


def test_generate_index_starts_one_background_task_from_form_paths(tmp_path: Path) -> None:
    pdf_root = tmp_path / "pdf"
    pdf_root.mkdir()
    index = tmp_path / "manuals.sqlite3"
    calls = []

    class FakeTask:
        def __init__(self, **kwargs):
            calls.append(kwargs)
            self.started = False

        def start(self):
            self.started = True

    model = SettingsFormModel(default_settings_document())
    model.update("manuals.root", str(pdf_root))
    model.update("lexical_docs.index_path", str(index))
    controller = SettingsController(
        model,
        FakeStore(),
        dialogs=FakeDialogs(),
        index_task_factory=FakeTask,
    )

    task = controller.start_manual_index_build()

    assert task is not None and task.started is True
    assert calls == [{"pdf_root": str(pdf_root), "index_path": str(index)}]


def test_generate_index_resolves_folder_to_default_sqlite_name(tmp_path: Path) -> None:
    pdf_root = tmp_path / "pdf"
    pdf_root.mkdir()
    index_folder = tmp_path / "index-folder"
    index_folder.mkdir()
    calls = []

    class FakeTask:
        def __init__(self, **kwargs):
            calls.append(kwargs)

        def start(self):
            return None

    model = SettingsFormModel(default_settings_document())
    model.update("manuals.root", str(pdf_root))
    model.update("lexical_docs.index_path", str(index_folder))
    controller = SettingsController(
        model,
        FakeStore(),
        dialogs=FakeDialogs(),
        index_task_factory=FakeTask,
    )

    assert controller.start_manual_index_build() is not None
    target = index_folder / "lexical_manuals.sqlite3"
    assert calls == [{"pdf_root": str(pdf_root), "index_path": str(target)}]
    assert get_value(controller.model.document, "lexical_docs.index_path") == str(target)


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
    assert len(dialogs.infos) == 1


def test_manual_auto_detect_keeps_the_single_dirty_notice(tmp_path: Path) -> None:
    detected_root = tmp_path / "COMSOL64" / "Multiphysics"
    detected_java = detected_root / "java" / "win64" / "jre"
    result = DiscoveryResult(detected_root, detected_java, "comsol_bundled")
    controller, _store, dialogs = _controller(discover=lambda **_kwargs: result)

    controller.auto_detect(manual=True)

    assert controller.model.dirty is True
    assert len(dialogs.infos) == 1


def test_auto_detect_fills_manuals_root_only_when_pdf_exists(tmp_path: Path) -> None:
    detected_root = tmp_path / "COMSOL64" / "Multiphysics"
    detected_java = detected_root / "java" / "win64" / "jre"
    (detected_root / "doc").mkdir(parents=True)
    (detected_root / "doc" / "manual.pdf").write_bytes(b"%PDF-test")
    result = DiscoveryResult(detected_root, detected_java, "comsol_bundled")
    controller, _store, dialogs = _controller(discover=lambda **_kwargs: result)

    controller.auto_detect(manual=False)

    assert get_value(controller.model.document, "manuals.root") == str(detected_root / "doc")
    assert dialogs.errors == []

    empty_root = tmp_path / "Empty" / "Multiphysics"
    empty_java = empty_root / "java" / "win64" / "jre"
    empty_root.mkdir(parents=True)
    result = DiscoveryResult(empty_root, empty_java, "comsol_bundled")
    controller, _store, dialogs = _controller(discover=lambda **_kwargs: result)
    controller.auto_detect(manual=False)
    assert get_value(controller.model.document, "manuals.root") is None
    assert dialogs.errors == []


def test_external_conflict_is_terminal_and_localized() -> None:
    controller, store, dialogs = _controller()
    store.ownership.conflict = True

    assert controller.poll_conflict() is True
    assert len(dialogs.errors) == 1
    assert dialogs.errors[0] == (
        controller.text("Settings conflict"),
        controller.text(
            "The settings file changed outside this editor. Close this window and reopen settings."
        ),
    )


def test_create_shortcut_confirms_before_replacing_foreign_item() -> None:
    dialogs = FakeDialogs([True])
    controller, store, _dialogs = _controller(dialogs=dialogs)
    calls: list[dict] = []

    def create(**kwargs):
        calls.append(kwargs)
        if kwargs.get("replace_existing"):
            return _shortcut_receipt("replaced", success=True)
        return {
            **_shortcut_receipt("confirmation_required", success=False),
            "existing_kind": "foreign",
        }

    controller._create_shortcut = create
    controller.create_desktop_shortcut()

    assert calls == [
        {"settings_path": store.target},
        {"settings_path": store.target, "replace_existing": True},
    ]
    assert len(dialogs.confirm_calls) == 1
    assert len(dialogs.infos) == 1
    assert dialogs.errors == []


def test_create_shortcut_decline_and_foreign_remove_are_non_destructive() -> None:
    dialogs = FakeDialogs([False])
    controller, store, _dialogs = _controller(dialogs=dialogs)
    create_calls: list[dict] = []
    remove_calls: list[dict] = []

    def create(**kwargs):
        create_calls.append(kwargs)
        return {
            **_shortcut_receipt("confirmation_required", success=False),
            "existing_kind": "foreign",
        }

    def remove(**kwargs):
        remove_calls.append(kwargs)
        return _shortcut_receipt("foreign_preserved", success=False)

    controller._create_shortcut = create
    controller._remove_shortcut = remove
    controller.create_desktop_shortcut()
    controller.remove_desktop_shortcut()

    assert create_calls == [{"settings_path": store.target}]
    assert remove_calls == [{"settings_path": store.target}]
    assert dialogs.infos == []
    assert len(dialogs.errors) == 1


def test_save_apply_and_initialization_never_create_a_shortcut() -> None:
    controller, _store, _dialogs = _controller()
    shortcut_calls: list[bool] = []
    controller._create_shortcut = lambda **_kwargs: shortcut_calls.append(True)

    assert controller.apply() is True
    controller.update("ownership.owner", "changed")
    assert controller.apply() is True

    assert shortcut_calls == []
