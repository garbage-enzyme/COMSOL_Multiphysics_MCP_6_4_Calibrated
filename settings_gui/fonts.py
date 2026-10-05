"""System-font policy with deterministic CJK fallbacks."""

from __future__ import annotations

import os
import tkinter.font as tkfont
from tkinter import TclError


def apply_locale_font(root, language: str) -> str:
    current = tkfont.nametofont("TkDefaultFont", root=root).actual("family")
    families = set(tkfont.families(root))
    preferred = {
        "zh-cn": "Microsoft YaHei UI",
        "zh-tw": "Microsoft JhengHei UI",
    }.get(language)
    if os.name != "nt" and language in {"zh-cn", "zh-tw"}:
        preferred = next(
            (
                name
                for name in (
                    "Noto Sans CJK SC" if language == "zh-cn" else "Noto Sans CJK TC",
                    "WenQuanYi Zen Hei",
                    preferred,
                )
                if name in families
            ),
            None,
        )
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
                tkfont.nametofont(name, root=root).configure(family=selected)
            except TclError:
                continue
    return selected


__all__ = ["apply_locale_font"]
