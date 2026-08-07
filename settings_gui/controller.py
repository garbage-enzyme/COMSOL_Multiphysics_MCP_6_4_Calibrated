"""GUI state machine and side-effect coordination."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from .comsol_discovery import DiscoveryResult, discover_environment
from .desktop_shortcut import create_desktop_shortcut, remove_desktop_shortcut
from .dialogs import Dialogs
from .i18n import Translator
from .manual_index import ManualIndexBuildTask
from .model import SettingsFormModel, get_value
from .storage import SettingsStore

DEFAULT_LEXICAL_INDEX_NAME = "lexical_manuals.sqlite3"


def resolve_lexical_index_target(value: str | Path) -> Path:
    """Resolve a selected SQLite file or an existing destination folder."""
    target = Path(value)
    if not target.is_absolute() or not str(target).isascii():
        raise ValueError("lexical index destination must be an ASCII-only absolute path")
    if target.exists():
        if target.is_dir():
            return target / DEFAULT_LEXICAL_INDEX_NAME
        if target.is_file():
            return target
        raise ValueError("lexical index destination is not a regular file or folder")
    if not target.suffix:
        return target / DEFAULT_LEXICAL_INDEX_NAME
    return target


def discover_manuals_root(comsol_root: Path) -> Path | None:
    """Return COMSOL's manuals folder only when it actually contains a PDF."""
    candidate = comsol_root / "doc"
    if not candidate.is_dir():
        return None
    try:
        next(candidate.rglob("*.pdf"))
    except StopIteration:
        return None
    except OSError:
        return None
    return candidate


class SettingsController:
    def __init__(
        self,
        model: SettingsFormModel,
        store: SettingsStore,
        *,
        dialogs: Dialogs | None = None,
        discover: Callable[..., DiscoveryResult] = discover_environment,
        create_shortcut: Callable[..., dict[str, Any]] = create_desktop_shortcut,
        remove_shortcut: Callable[..., dict[str, Any]] = remove_desktop_shortcut,
        index_task_factory: Callable[..., ManualIndexBuildTask] = ManualIndexBuildTask,
    ) -> None:
        self.model = model
        self.store = store
        self.dialogs = dialogs or Dialogs()
        self.discover = discover
        self._create_shortcut = create_shortcut
        self._remove_shortcut = remove_shortcut
        self._index_task_factory = index_task_factory
        self.translator = Translator(model.language)
        self.restart_pending = False
        self._dirty_notice_shown = False
        self.on_refresh: Callable[[], None] = lambda: None
        self.on_close: Callable[[], None] = lambda: None

    def text(self, message: str) -> str:
        return self.translator(message)

    def update(self, key: str, value: Any, *, show_dirty_notice: bool = True) -> bool:
        if key.startswith("evidence_integrity.checks.") and value is False:
            if not self.dialogs.confirm(
                title=self.text("Disable evidence check?"),
                message=self.text(
                    "Disabling an evidence check makes future results not fully verified. Continue?"
                ),
            ):
                return False
        self.model.update(key, value)
        if key == "gui.language" and value != self.translator.language:
            self.translator = Translator(str(value))
        if (
            self.model.restart_required
            and not self.restart_pending
            and not self._dirty_notice_shown
            and show_dirty_notice
        ):
            self._dirty_notice_shown = True
            self.dialogs.info(
                title=self.text("Restart required"),
                message=self.text(
                    "Changes take effect only after restarting Codex or the owning MCP client."
                ),
            )
        self.on_refresh()
        return True

    def apply(self) -> bool:
        self.model.validate()
        if not self.model.valid or self.model.canonical is None:
            self.dialogs.error(
                title=self.text("Invalid settings"),
                message=self.text("Correct every highlighted field before saving."),
            )
            self.on_refresh()
            return False
        restart_required = self.model.restart_required
        try:
            self.store.save(self.model.canonical)
        except Exception:
            self.dialogs.error(
                title=self.text("Save failed"),
                message=self.text(
                    "Settings were not saved. Close conflicting editors and try again."
                ),
            )
            return False
        self.model.mark_saved()
        self.restart_pending = self.restart_pending or restart_required
        self._dirty_notice_shown = False
        self.on_refresh()
        return True

    def save_and_exit(self) -> None:
        if self.apply():
            self.on_close()

    def cancel(self) -> None:
        if self.model.dirty and not self.dialogs.confirm(
            title=self.text("Discard changes?"),
            message=self.text("Close without saving the current edits?"),
        ):
            return
        self.on_close()

    def browse_directory(self, key: str) -> None:
        value = self.dialogs.ask_directory(title=self.text("Choose folder"))
        if value:
            self.update(key, value)

    def browse_file(self, key: str) -> None:
        value = self.dialogs.ask_file(title=self.text("Choose file"))
        if value:
            self.update(key, value)

    def browse_save_file(self, key: str) -> None:
        value = self.dialogs.ask_save_file(title=self.text("Choose index file"))
        if value:
            self.update(key, value)

    def clear(self, key: str) -> None:
        self.update(key, None)

    def start_manual_index_build(self) -> ManualIndexBuildTask | None:
        pdf_root = get_value(self.model.document, "manuals.root")
        index_path = get_value(self.model.document, "lexical_docs.index_path")
        if not pdf_root or not index_path:
            self.dialogs.error(
                title=self.text("Generate manual index"),
                message=self.text("Choose both the PDF folder and SQLite index file first."),
            )
            return None
        try:
            target = resolve_lexical_index_target(index_path)
        except OSError, ValueError:
            self.dialogs.error(
                title=self.text("Generate manual index"),
                message=self.text(
                    "Index generation could not start. Check the selected paths and permissions."
                ),
            )
            return None
        if target.exists() and not self.dialogs.confirm(
            title=self.text("Replace existing index?"),
            message=self.text(
                "A validated new index will atomically replace the existing file. Continue?"
            ),
        ):
            return None
        try:
            if str(target) != str(index_path):
                self.update("lexical_docs.index_path", str(target))
            task = self._index_task_factory(pdf_root=pdf_root, index_path=str(target))
            task.start()
        except OSError, RuntimeError, ValueError:
            self.dialogs.error(
                title=self.text("Generate manual index"),
                message=self.text(
                    "Index generation could not start. Check the selected paths and permissions."
                ),
            )
            return None
        return task

    def auto_detect(self, *, manual: bool) -> None:
        current_root = get_value(self.model.document, "comsol.installation_root")
        try:
            result = self.discover(
                selected_root=current_root if current_root and not manual else None
            )
        except OSError, ValueError:
            self.dialogs.error(
                title=self.text("Auto-detect"),
                message=self.text(
                    "The configured COMSOL root is not a supported 6.4 installation."
                ),
            )
            return
        if result.ambiguous_roots:
            self.dialogs.info(
                title=self.text("Auto-detect"),
                message=self.text(
                    "Multiple COMSOL 6.4 installations were found. Choose one manually."
                ),
            )
            return
        if result.comsol_root is None:
            if manual:
                self.dialogs.info(
                    title=self.text("Auto-detect"),
                    message=self.text("No supported COMSOL 6.4 installation was found."),
                )
            return
        proposal: dict[str, str] = {"comsol.installation_root": str(result.comsol_root)}
        if result.java_home is not None:
            proposal["java.java_home"] = str(result.java_home)
            proposal["java.jdk_home"] = str(result.java_home)
        manuals_root = discover_manuals_root(result.comsol_root)
        if manuals_root is not None:
            proposal["manuals.root"] = str(manuals_root)
        overwritten = [
            key
            for key, value in proposal.items()
            if get_value(self.model.document, key) not in (None, value)
        ]
        if overwritten and not self.dialogs.confirm(
            title=self.text("Replace existing values?"),
            message=self.text("Auto-detect would replace these settings: {keys}").format(
                keys=", ".join(overwritten)
            ),
        ):
            return
        for key, value in proposal.items():
            self.update(key, value, show_dirty_notice=manual)

    def poll_conflict(self) -> bool:
        try:
            self.store.ownership.verify_unchanged()
        except Exception:
            self.dialogs.error(
                title=self.text("Settings conflict"),
                message=self.text(
                    "The settings file changed outside this editor. "
                    "Close this window and reopen settings."
                ),
            )
            return True
        return False

    def create_desktop_shortcut(self) -> None:
        try:
            result = self._create_shortcut(settings_path=self.store.target)
            if result.get("state") == "confirmation_required":
                if not self.dialogs.confirm(
                    title=self.text("Replace existing desktop shortcut?"),
                    message=self.text("A different shortcut already uses this name. Replace it?"),
                ):
                    return
                result = self._create_shortcut(
                    settings_path=self.store.target,
                    replace_existing=True,
                )
        except OSError, RuntimeError, ValueError:
            result = {"success": False}
        if result.get("success") is True:
            self.dialogs.info(
                title=self.text("Desktop shortcut ready"),
                message=self.text("The desktop shortcut now opens this exact settings file."),
            )
            return
        self.dialogs.error(
            title=self.text("Desktop shortcut could not be created"),
            message=self.text("The existing Desktop item was preserved."),
        )

    def remove_desktop_shortcut(self) -> None:
        try:
            result = self._remove_shortcut(settings_path=self.store.target)
        except OSError, RuntimeError, ValueError:
            result = {"success": False}
        if result.get("success") is True:
            message = (
                "No owned desktop shortcut was found."
                if result.get("state") == "not_found"
                else "The owned desktop shortcut was removed."
            )
            self.dialogs.info(
                title=self.text("Desktop shortcut removed"),
                message=self.text(message),
            )
            return
        self.dialogs.error(
            title=self.text("Desktop shortcut not removed"),
            message=self.text(
                "The Desktop item is not owned by this application and was preserved."
            ),
        )


__all__ = ["SettingsController"]
