"""Deterministic CJK system-font selection tests."""

from __future__ import annotations

from settings_gui import fonts


class FakeFont:
    def __init__(self) -> None:
        self.configured: list[str] = []

    def actual(self, _key: str) -> str:
        return "Segoe UI"

    def configure(self, *, family: str) -> None:
        self.configured.append(family)


def test_simplified_and_traditional_fallbacks(monkeypatch) -> None:
    fake = FakeFont()
    monkeypatch.setattr(fonts.tkfont, "nametofont", lambda _name, **_kwargs: fake)
    monkeypatch.setattr(
        fonts.tkfont,
        "families",
        lambda _root: ("Segoe UI", "Microsoft YaHei UI", "Microsoft JhengHei UI"),
    )

    assert fonts.apply_locale_font(object(), "zh-cn") == "Microsoft YaHei UI"
    assert fonts.apply_locale_font(object(), "zh-tw") == "Microsoft JhengHei UI"
    assert "Microsoft YaHei UI" in fake.configured
    assert "Microsoft JhengHei UI" in fake.configured


def test_english_keeps_current_system_font(monkeypatch) -> None:
    fake = FakeFont()
    monkeypatch.setattr(fonts.tkfont, "nametofont", lambda _name, **_kwargs: fake)
    monkeypatch.setattr(fonts.tkfont, "families", lambda _root: ("Segoe UI",))

    assert fonts.apply_locale_font(object(), "en") == "Segoe UI"
    assert fake.configured == []


def test_font_lookup_is_bound_to_the_supplied_root(monkeypatch) -> None:
    root = object()
    fake = FakeFont()
    observed = []
    monkeypatch.setattr(
        fonts.tkfont,
        "nametofont",
        lambda name, **kwargs: observed.append((name, kwargs.get("root"))) or fake,
    )
    monkeypatch.setattr(fonts.tkfont, "families", lambda _root: ("Segoe UI",))

    fonts.apply_locale_font(root, "en")

    assert observed == [("TkDefaultFont", root)]
