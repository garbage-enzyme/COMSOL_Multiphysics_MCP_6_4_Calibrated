"""Windows system-font policy with deterministic CJK fallbacks."""

from __future__ import annotations

import tkinter.font as tkfont
from tkinter import TclError


def apply_locale_font(root, language: str) -> str:
    current = tkfont.nametofont("TkDefaultFont").actual("family")
    families = set(tkfont.families(root))
    preferred = {
        "zh-cn": "Microsoft YaHei UI",
        "zh-tw": "Microsoft JhengHei UI",
    }.get(language)
    selected = preferred if preferred in families else current
    if selected != current:
        for name in (
            "TkDefaultFont",
            "TkTextFont",
            "TkMenuFont",
            "TkHeadingFont",
            "TkCaptionFont",
            "TkSmallCaptionFont",
            "TkIconFont",
            "TkTooltipFont",
        ):
            try:
                tkfont.nametofont(name).configure(family=selected)
            except TclError:
                continue
    return selected


__all__ = ["apply_locale_font"]
