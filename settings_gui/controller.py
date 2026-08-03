"""GUI state machine and side-effect coordination."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .comsol_discovery import DiscoveryResult, discover_environment
from .dialogs import Dialogs
from .i18n import Translator
from .model import SettingsFormModel, get_value
from .storage import SettingsStore


class SettingsController:
    def __init__(
        self,
        model: SettingsFormModel,
        store: SettingsStore,
        *,
        dialogs: Dialogs | None = None,
        discover: Callable[..., DiscoveryResult] = discover_environment,
    ) -> None:
        self.model = model
        self.store = store
        self.dialogs = dialogs or Dialogs()
        self.discover = discover
        self.translator = Translator(model.language)
        self.restart_pending = False
        self._dirty_notice_shown = False
        self.on_refresh: Callable[[], None] = lambda: None
        self.on_close: Callable[[], None] = lambda: None

    def text(self, message: str) -> str:
        return self.translator(message)

    def update(self, key: str, value: Any, *, show_dirty_notice: bool = True) -> bool:
        previous_dirty = self.model.dirty
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
        if self.model.dirty and not previous_dirty and not self._dirty_notice_shown:
            self._dirty_notice_shown = True
            if show_dirty_notice:
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
        self.restart_pending = True
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

    def clear(self, key: str) -> None:
        self.update(key, None)

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


__all__ = ["SettingsController"]
