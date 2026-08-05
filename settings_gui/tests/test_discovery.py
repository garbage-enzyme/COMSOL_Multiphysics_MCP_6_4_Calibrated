"""Solver-free COMSOL 6.4 and Java discovery tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from settings_gui.comsol_discovery import (
    discover_environment,
    discover_java_home,
    registry_comsol_roots,
    validate_comsol_root,
)


def _installation(root: Path, *, with_java: bool = True) -> tuple[Path, Path]:
    (root / "bin" / "win64").mkdir(parents=True)
    (root / "plugins").mkdir()
    (root / "apiplugins").mkdir()
    (root / "bin" / "win64" / "comsol.exe").write_bytes(b"")
    (root / "bin" / "win64" / "comsolmphserver.exe").write_bytes(b"")
    java_home = root / "java" / "win64" / "jre"
    ini = "COMSOL Multiphysics 6.4\n-vm\n../../java/win64/jre/bin/server/jvm.dll\n"
    (root / "bin" / "win64" / "comsol.ini").write_text(ini, encoding="utf-8")
    if with_java:
        (java_home / "bin" / "server").mkdir(parents=True)
        (java_home / "bin" / "server" / "jvm.dll").write_bytes(b"")
        (java_home / "bin" / "java.exe").write_bytes(b"")
    return root, java_home


class FakeRegistry:
    HKEY_LOCAL_MACHINE = object()

    def __init__(self, values: dict[str, str]) -> None:
        self.values = values

    class Key:
        def __init__(self, name: str) -> None:
            self.name = name

        def __enter__(self):
            return self

        def __exit__(self, _exc_type, _exc, _traceback) -> None:
            return None

    def OpenKey(self, _hive, name: str):
        if name not in self.values:
            raise OSError("missing")
        return self.Key(name)

    def QueryValueEx(self, key, _name: str):
        return self.values[key.name], 1


def test_valid_root_and_relative_jvm_ini_select_bundled_java(tmp_path: Path) -> None:
    root, java_home = _installation(tmp_path / "COMSOL64" / "Multiphysics")

    assert validate_comsol_root(root) == root.resolve()
    assert discover_java_home(root, environ={}, which=lambda _name: None) == (
        java_home.resolve(),
        "comsol_bundled",
    )


def test_root_validation_rejects_wrong_release_metadata(tmp_path: Path) -> None:
    root, _java_home = _installation(tmp_path / "wrong")
    (root / "bin" / "win64" / "comsol.ini").write_text("unrelated", encoding="utf-8")

    with pytest.raises(ValueError, match="supported family"):
        validate_comsol_root(root)


def test_registry_candidates_are_normalized_and_deduplicated(tmp_path: Path) -> None:
    root, _java_home = _installation(tmp_path / "COMSOL64" / "Multiphysics")
    registry = FakeRegistry(
        {
            r"SOFTWARE\COMSOL\COMSOL64": str(root),
            r"SOFTWARE\WOW6432Node\COMSOL\COMSOL64": str(root),
        }
    )

    assert registry_comsol_roots(registry) == (root.resolve(),)


def test_unreadable_registry_candidate_is_ignored(tmp_path: Path, monkeypatch) -> None:
    root, _java_home = _installation(tmp_path / "COMSOL64" / "Multiphysics")
    registry = FakeRegistry({r"SOFTWARE\COMSOL\COMSOL64": str(root)})
    original_read_text = Path.read_text

    def unreadable_ini(path: Path, *args, **kwargs):
        if path.name == "comsol.ini":
            raise PermissionError("locked")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", unreadable_ini)

    assert registry_comsol_roots(registry) == ()


def test_ambiguous_registry_roots_are_never_selected(tmp_path: Path) -> None:
    first, _ = _installation(tmp_path / "one")
    second, _ = _installation(tmp_path / "two")
    registry = FakeRegistry(
        {
            r"SOFTWARE\COMSOL\COMSOL64": str(first),
            r"SOFTWARE\WOW6432Node\COMSOL\COMSOL64": str(second),
        }
    )

    result = discover_environment(registry=registry, environ={})

    assert result.comsol_root is None
    assert result.java_home is None
    assert result.ambiguous_roots == (first.resolve(), second.resolve())


def test_java_fallback_order_is_java_home_then_jdk_then_path(tmp_path: Path) -> None:
    root, _ = _installation(tmp_path / "comsol", with_java=False)
    java_home = tmp_path / "java-home"
    jdk_home = tmp_path / "jdk-home"
    path_home = tmp_path / "path-home"
    for home in (java_home, jdk_home, path_home):
        (home / "bin").mkdir(parents=True)
        (home / "bin" / "java.exe").write_bytes(b"")

    assert discover_java_home(
        root,
        environ={"JAVA_HOME": str(java_home), "JDK_HOME": str(jdk_home)},
        which=lambda _name: str(path_home / "bin" / "java.exe"),
    ) == (java_home.resolve(), "system_java_home")
    assert discover_java_home(
        root,
        environ={"JDK_HOME": str(jdk_home)},
        which=lambda _name: str(path_home / "bin" / "java.exe"),
    ) == (jdk_home.resolve(), "system_jdk_home")
    assert discover_java_home(
        root,
        environ={},
        which=lambda _name: str(path_home / "bin" / "java.exe"),
    ) == (path_home.resolve(), "system_path")


def test_quoted_java_home_is_normalized(tmp_path: Path) -> None:
    java_home = tmp_path / "Java Home"
    (java_home / "bin").mkdir(parents=True)
    (java_home / "bin" / "java.exe").write_bytes(b"")

    assert discover_java_home(
        None,
        environ={"JAVA_HOME": f'"{java_home}\\"'},
        which=lambda _name: None,
    ) == (java_home.resolve(), "system_java_home")


def test_bundled_java_wins_over_every_system_candidate(tmp_path: Path) -> None:
    root, bundled = _installation(tmp_path / "comsol")
    system = tmp_path / "system"
    (system / "bin").mkdir(parents=True)
    (system / "bin" / "java.exe").write_bytes(b"")

    assert discover_java_home(
        root,
        environ={"JAVA_HOME": str(system), "JDK_HOME": str(system)},
        which=lambda _name: str(system / "bin" / "java.exe"),
    ) == (bundled.resolve(), "comsol_bundled")
