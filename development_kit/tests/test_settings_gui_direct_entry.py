"""Direct installed Settings GUI command-line contract tests."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from comsol_mcp.settings import SETTINGS_PATH_ENV
from settings_gui import __main__ as entry
from settings_gui.desktop_shortcut import encode_settings_path_token


def test_validate_only_is_path_redacted_and_imports_no_tk(
    tmp_path: Path,
    monkeypatch,
) -> None:
    target = tmp_path / "settings.json"
    output: list[str] = []
    created: list[bool] = []
    tkinter_before = "tkinter" in sys.modules
    monkeypatch.setattr(
        entry,
        "shortcut_prerequisites",
        lambda **_kwargs: {
            "ready": True,
            "desktop_available": True,
            "entry_executable_available": True,
            "icon_available": True,
            "windows_shortcut_runtime_available": True,
        },
    )
    monkeypatch.setattr(
        entry,
        "create_desktop_shortcut",
        lambda **_kwargs: created.append(True),
    )

    code = entry.run_cli(
        ["--settings-path", str(target), "--validate-only"],
        environ={},
        output=output.append,
    )

    assert code == 0
    assert not target.exists()
    assert created == []
    assert ("tkinter" in sys.modules) is tkinter_before
    receipt = json.loads(output[0])
    assert receipt["ready"] is True
    assert receipt["settings_path_override"] is True
    assert receipt["settings_path_included"] is False
    assert receipt["contains_local_path"] is False
    assert receipt["tkinter_imported"] is tkinter_before
    assert receipt["mcp_started"] is False
    assert receipt["solver_started"] is False
    assert str(target) not in output[0]


def test_direct_launch_hands_exact_path_to_gui_process(
    tmp_path: Path,
    monkeypatch,
) -> None:
    target = tmp_path / "用户 settings" / "settings.json"
    target.parent.mkdir()
    environment: dict[str, str] = {}
    observed: list[str] = []

    def fake_launch() -> int:
        observed.append(environment[SETTINGS_PATH_ENV])
        return 0

    monkeypatch.setattr(entry, "_launch_gui", fake_launch)

    code = entry.run_cli(["--settings-path", str(target)], environ=environment)

    assert code == 0
    assert observed == [str(target.resolve(strict=False))]
    assert not target.exists()


def test_shortcut_token_launch_hands_exact_unicode_path_to_gui_process(
    tmp_path: Path,
    monkeypatch,
) -> None:
    target = tmp_path / "用户 settings" / "settings.json"
    target.parent.mkdir()
    environment: dict[str, str] = {}
    observed: list[str] = []
    monkeypatch.setattr(
        entry,
        "_launch_gui",
        lambda: observed.append(environment[SETTINGS_PATH_ENV]) or 0,
    )

    code = entry.run_cli(
        ["--settings-path-token", encode_settings_path_token(target)],
        environ=environment,
    )

    assert code == 0
    assert observed == [str(target.resolve(strict=False))]
    assert not target.exists()


def test_shortcut_token_rejects_invalid_or_ambiguous_transport(tmp_path: Path) -> None:
    target = tmp_path / "settings.json"

    assert entry.run_cli(["--settings-path-token", "not+urlsafe"], environ={}) == 2
    assert (
        entry.run_cli(
            [
                "--settings-path",
                str(target),
                "--settings-path-token",
                encode_settings_path_token(target),
            ],
            environ={},
        )
        == 2
    )


def test_shortcut_actions_require_an_explicit_settings_path(monkeypatch) -> None:
    calls: list[bool] = []
    monkeypatch.setattr(
        entry,
        "create_desktop_shortcut",
        lambda **_kwargs: calls.append(True),
    )

    code = entry.run_cli(["--create-desktop-shortcut"], environ={})

    assert code == 2
    assert calls == []


def test_shortcut_action_accepts_the_exact_settings_path_token(
    tmp_path: Path, monkeypatch
) -> None:
    target = tmp_path / "settings.json"
    observed = []
    monkeypatch.setattr(
        entry,
        "create_desktop_shortcut",
        lambda **kwargs: observed.append(kwargs["settings_path"])
        or {"success": True, "state": "created"},
    )

    code = entry.run_cli(
        [
            "--settings-path-token",
            encode_settings_path_token(target),
            "--create-desktop-shortcut",
        ],
        environ={},
        output=lambda _value: None,
    )

    assert code == 0
    assert observed == [target.resolve(strict=False)]


def test_validate_only_backend_failure_is_bounded(tmp_path: Path, monkeypatch) -> None:
    target = tmp_path / "settings.json"
    output = []
    monkeypatch.setattr(
        entry,
        "_validation_receipt",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError(str(target))),
    )

    code = entry.run_cli(
        ["--settings-path", str(target), "--validate-only"],
        environ={},
        output=output.append,
    )

    assert code == 2
    assert json.loads(output[0])["state"] == "action_failed"
    assert str(target) not in output[0]


def test_explicit_dangling_settings_link_is_rejected(tmp_path: Path, monkeypatch) -> None:
    target = tmp_path / "settings.json"
    monkeypatch.setattr(entry.os.path, "lexists", lambda path: Path(path) == target)
    original_is_symlink = Path.is_symlink
    monkeypatch.setattr(
        Path,
        "is_symlink",
        lambda path: path == target or original_is_symlink(path),
    )

    assert entry.run_cli(["--settings-path", str(target), "--validate-only"], environ={}) == 2


def test_explicit_settings_path_is_absolute_bounded_and_not_a_directory(tmp_path: Path) -> None:
    assert entry.run_cli(["--settings-path", "relative.json", "--validate-only"], environ={}) == 2
    assert (
        entry.run_cli(
            ["--settings-path", "C:/" + "x" * 32768, "--validate-only"],
            environ={},
        )
        == 2
    )
    assert entry.run_cli(["--settings-path", str(tmp_path), "--validate-only"], environ={}) == 2


def test_create_and_remove_shortcut_are_explicit_cli_actions(
    tmp_path: Path,
    monkeypatch,
) -> None:
    target = tmp_path / "settings.json"
    calls: list[tuple[str, bool]] = []
    output: list[str] = []

    def fake_create(**kwargs):
        calls.append((str(kwargs["settings_path"]), kwargs["replace_existing"]))
        return {
            "success": True,
            "state": "created",
            "contains_local_path": False,
        }

    def fake_remove(**kwargs):
        calls.append((str(kwargs["settings_path"]), False))
        return {
            "success": True,
            "state": "removed",
            "contains_local_path": False,
        }

    monkeypatch.setattr(entry, "create_desktop_shortcut", fake_create)
    monkeypatch.setattr(entry, "remove_desktop_shortcut", fake_remove)

    create_code = entry.run_cli(
        [
            "--settings-path",
            str(target),
            "--create-desktop-shortcut",
            "--replace-existing-shortcut",
        ],
        environ={},
        output=output.append,
    )
    remove_code = entry.run_cli(
        ["--settings-path", str(target), "--remove-desktop-shortcut"],
        environ={},
        output=output.append,
    )

    assert create_code == 0
    assert remove_code == 0
    assert calls == [(str(target.resolve(strict=False)), True), (str(target.resolve(False)), False)]
    assert [json.loads(item)["state"] for item in output] == ["created", "removed"]


def test_shortcut_backend_failure_is_bounded_and_path_redacted(
    tmp_path: Path,
    monkeypatch,
) -> None:
    target = tmp_path / "settings.json"
    output: list[str] = []
    monkeypatch.setattr(
        entry,
        "create_desktop_shortcut",
        lambda **_kwargs: (_ for _ in ()).throw(OSError(str(target))),
    )

    code = entry.run_cli(
        ["--settings-path", str(target), "--create-desktop-shortcut"],
        environ={},
        output=output.append,
    )

    assert code == 3
    receipt = json.loads(output[0])
    assert receipt["state"] == "action_failed"
    assert receipt["contains_local_path"] is False
    assert str(target) not in output[0]
