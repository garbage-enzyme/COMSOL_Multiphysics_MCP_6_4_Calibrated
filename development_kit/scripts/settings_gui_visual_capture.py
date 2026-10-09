"""Capture the real Tk Settings GUI across locale, DPI, and state scenarios."""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import tkinter as tk
from copy import deepcopy
from pathlib import Path
from tkinter import ttk

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PIL import Image, ImageGrab

from comsol_mcp.settings import default_settings_document
from settings_gui.app import SettingsApplication
from settings_gui.controller import SettingsController
from settings_gui.model import TAB_IDS, SettingsFormModel


class _Ownership:
    def verify_unchanged(self) -> None:
        return None


class _Store:
    def __init__(self) -> None:
        self.ownership = _Ownership()

    def save(self, _document: dict) -> str:
        return "0" * 64

    def close(self) -> None:
        return None


class _Dialogs:
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


def _document(language: str, state: str) -> dict:
    document = deepcopy(default_settings_document())
    document["gui"]["language"] = language
    if state == "invalid":
        document["runtime"]["directory"] = "relative-path"
    elif state == "long_paths":
        suffix = "/".join(["Long COMSOL Installation Folder"] * 7)
        document["comsol"]["installation_root"] = (
            f"C:/{suffix}" if os.name == "nt" else f"/opt/{suffix}"
        )
        document["java"]["java_home"] = (
            f"C:/{suffix}/java/win64/jre" if os.name == "nt" else f"/opt/{suffix}/java/linux64/jre"
        )
        document["java"]["jdk_home"] = (
            f"C:/{suffix}/java/win64/jre" if os.name == "nt" else f"/opt/{suffix}/java/linux64/jre"
        )
    elif state == "docs":
        document["manuals"]["root"] = (
            "C:/COMSOL64/Multiphysics/doc" if os.name == "nt" else "/opt/comsol/doc"
        )
        document["lexical_docs"] = {
            "enabled": True,
            "index_path": "D:/comsol_docs_fts/manuals.sqlite3"
            if os.name == "nt"
            else "/tmp/visual-docs/manuals.sqlite3",
        }
        document["semantic_docs"] = {
            "enabled": True,
            "root": "D:/comsol_docs_semantic" if os.name == "nt" else "/tmp/visual-semantic",
            "model_path": "D:/models/semantic-search"
            if os.name == "nt"
            else "/tmp/visual-models/semantic-search",
        }
    return document


def _grab_x11_window(root: tk.Tk) -> Image.Image:
    """Capture this mapped Tk window on a rootless WSLg X server."""
    from ctypes.util import find_library

    class XImage(ctypes.Structure):
        _fields_ = [
            ("width", ctypes.c_int),
            ("height", ctypes.c_int),
            ("xoffset", ctypes.c_int),
            ("format", ctypes.c_int),
            ("data", ctypes.c_void_p),
            ("byte_order", ctypes.c_int),
            ("bitmap_unit", ctypes.c_int),
            ("bitmap_bit_order", ctypes.c_int),
            ("bitmap_pad", ctypes.c_int),
            ("depth", ctypes.c_int),
            ("bytes_per_line", ctypes.c_int),
            ("bits_per_pixel", ctypes.c_int),
            ("red_mask", ctypes.c_ulong),
            ("green_mask", ctypes.c_ulong),
            ("blue_mask", ctypes.c_ulong),
            ("obdata", ctypes.c_void_p),
            ("functions", ctypes.c_void_p * 6),
        ]

    library = find_library("X11")
    if not library:
        raise RuntimeError("X11 capture library is unavailable")
    x11 = ctypes.CDLL(library)
    x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
    x11.XOpenDisplay.restype = ctypes.c_void_p
    x11.XCloseDisplay.argtypes = [ctypes.c_void_p]
    x11.XGetImage.argtypes = [
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_uint,
        ctypes.c_uint,
        ctypes.c_ulong,
        ctypes.c_int,
    ]
    x11.XGetImage.restype = ctypes.POINTER(XImage)
    x11.XDestroyImage.argtypes = [ctypes.POINTER(XImage)]
    display = x11.XOpenDisplay(None)
    if not display:
        raise RuntimeError("X11 capture display is unavailable")
    pointer = None
    try:
        pointer = x11.XGetImage(
            display,
            root.winfo_id(),
            0,
            0,
            root.winfo_width(),
            root.winfo_height(),
            ctypes.c_ulong(-1),
            2,
        )
        if not pointer:
            raise RuntimeError("Tk window capture failed")
        value = pointer.contents
        if (
            value.bits_per_pixel != 32
            or value.byte_order != 0
            or (value.red_mask, value.green_mask, value.blue_mask) != (0xFF0000, 0xFF00, 0xFF)
            or not 0 < value.bytes_per_line * value.height <= 32 * 1024 * 1024
        ):
            raise RuntimeError("X11 capture pixel format or size is unsupported")
        raw = ctypes.string_at(value.data, value.bytes_per_line * value.height)
        return Image.frombytes(
            "RGB", (value.width, value.height), raw, "raw", "BGRX", value.bytes_per_line, 1
        )
    finally:
        if pointer:
            x11.XDestroyImage(pointer)
        x11.XCloseDisplay(display)


def _capture_one_impl(
    output: Path,
    *,
    language: str,
    dpi_percent: int,
    state: str,
    tab: str,
) -> dict:
    if sys.platform == "win32":
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except AttributeError, OSError:
            pass
    root = tk.Tk()
    root.withdraw()
    root.tk.call("tk", "scaling", (96.0 * dpi_percent / 100.0) / 72.0)
    if state == "invalid" and "clam" in ttk.Style(root).theme_names():
        ttk.Style(root).theme_use("clam")
    model = SettingsFormModel.from_raw(_document(language, state))
    controller = SettingsController(model, _Store(), dialogs=_Dialogs())
    if state == "evidence":
        controller.update("evidence_integrity.checks.summary_claim_verification", False)
    application = SettingsApplication(root, controller)
    if application.notebook is None:
        raise RuntimeError("Settings GUI notebook was not constructed")
    application.notebook.select(TAB_IDS.index(tab))
    width, height = (960, 640) if state == "about" else (1120, 780)
    root.geometry(f"{width}x{height}+40+40")
    root.deiconify()
    root.attributes("-topmost", True)
    root.lift()
    root.update_idletasks()
    root.update()
    time.sleep(0.08)
    root.update()
    left = root.winfo_rootx()
    top = root.winfo_rooty()
    right = left + root.winfo_width()
    bottom = top + root.winfo_height()
    image = (
        _grab_x11_window(root)
        if sys.platform == "linux"
        else ImageGrab.grab(bbox=(left, top, right, bottom), all_screens=True)
    )
    image.save(output)
    receipt = {
        "file": output.name,
        "language": language,
        "dpi_percent": dpi_percent,
        "tk_scaling": float(root.tk.call("tk", "scaling")),
        "state": state,
        "tab": tab,
        "width": image.width,
        "height": image.height,
        "sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
        "display_backend": "wslg" if os.environ.get("WSL2_GUI_APPS_ENABLED") == "1" else "native",
    }
    application.close()
    return receipt


def _capture_one(
    output: Path,
    *,
    language: str,
    dpi_percent: int,
    state: str,
    tab: str,
) -> dict:
    """Capture one scenario and always destroy a Tk root on failure."""
    roots: list[tk.Tk] = []
    original_tk = tk.Tk

    def tracked_tk(*args: object, **kwargs: object) -> tk.Tk:
        root = original_tk(*args, **kwargs)
        roots.append(root)
        return root

    tk.Tk = tracked_tk  # type: ignore[assignment]
    try:
        return _capture_one_impl(
            output,
            language=language,
            dpi_percent=dpi_percent,
            state=state,
            tab=tab,
        )
    finally:
        tk.Tk = original_tk  # type: ignore[assignment]
        for root in roots:
            try:
                root.destroy()
            except tk.TclError:
                pass


def _capture_scenarios() -> tuple[tuple[str, int, str, str], ...]:
    scenarios: list[tuple[str, int, str, str]] = []
    for dpi_percent in (100, 125, 150, 200):
        for language in ("en", "zh-cn", "zh-tw"):
            scenarios.extend(
                (
                    (language, dpi_percent, "valid", "general"),
                    (language, dpi_percent, "valid", "profile"),
                    (language, dpi_percent, "docs", "docs"),
                    (language, dpi_percent, "about", "about"),
                )
            )
    for language in ("en", "zh-cn", "zh-tw"):
        scenarios.extend(
            (
                (language, 200, "invalid", "runtime"),
                (language, 200, "long_paths", "comsol_java"),
                (language, 150, "evidence", "evidence"),
            )
        )
    return tuple(scenarios)


def capture_matrix(output_root: Path) -> dict:
    output_root.mkdir(parents=True, exist_ok=True)
    captures = []
    for language, dpi, state, tab in _capture_scenarios():
        completed = subprocess.run(  # noqa: S603
            [
                sys.executable,
                "-m",
                "development_kit.scripts.settings_gui_visual_capture",
                "--output-root",
                str(output_root),
                "--one",
                "--language",
                language,
                "--dpi-percent",
                str(dpi),
                "--state",
                state,
                "--tab",
                tab,
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if completed.returncode != 0:
            shutil.rmtree(output_root, ignore_errors=True)
            stdout = completed.stdout[-2048:]
            stderr = completed.stderr[-2048:]
            raise RuntimeError(
                "Settings GUI capture failed for "
                f"language={language}, dpi={dpi}, state={state}, tab={tab}, "
                f"exit={completed.returncode}; stdout={stdout!r}; stderr={stderr!r}"
            )
        captures.append(json.loads(completed.stdout))
    receipt = {
        "schema_name": "comsol_mcp.settings_gui_visual_matrix",
        "schema_version": "1.1.0",
        "capture_count": len(captures),
        "captures": captures,
    }
    (output_root / "visual-matrix.json").write_text(
        json.dumps(receipt, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--one", action="store_true")
    parser.add_argument("--language", choices=("en", "zh-cn", "zh-tw"))
    parser.add_argument("--dpi-percent", type=int, choices=(100, 125, 150, 200))
    parser.add_argument(
        "--state",
        choices=("valid", "invalid", "long_paths", "evidence", "docs", "about"),
    )
    parser.add_argument("--tab", choices=TAB_IDS)
    args = parser.parse_args()
    if args.one:
        if None in (args.language, args.dpi_percent, args.state, args.tab):
            parser.error("--one requires language, DPI, state, and tab")
        args.output_root.mkdir(parents=True, exist_ok=True)
        name = f"{args.dpi_percent:03d}-{args.language}-{args.state}-{args.tab}.png"
        receipt = _capture_one(
            args.output_root / name,
            language=args.language,
            dpi_percent=args.dpi_percent,
            state=args.state,
            tab=args.tab,
        )
        print(json.dumps(receipt, sort_keys=True))
        return 0
    receipt = capture_matrix(args.output_root)
    print(json.dumps({"capture_count": receipt["capture_count"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
