"""Packaged Windows application icon contract."""

from __future__ import annotations

import struct
from pathlib import Path

ICON_PATH = Path(__file__).resolve().parents[1] / "assets" / "comsol_mcp.ico"
EXPECTED_SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _ico_entries(raw: bytes) -> list[tuple[int, int, bytes]]:
    reserved, kind, count = struct.unpack_from("<HHH", raw)
    assert reserved == 0
    assert kind == 1
    assert count == len(EXPECTED_SIZES)
    entries = []
    for index in range(count):
        width, height, _colors, _reserved, _planes, depth, size, offset = struct.unpack_from(
            "<BBBBHHII", raw, 6 + 16 * index
        )
        entries.append((width or 256, height or 256, raw[offset : offset + size]))
        assert depth == 32
    return entries


def test_icon_is_compressed_transparent_multisize_ico() -> None:
    raw = ICON_PATH.read_bytes()
    entries = _ico_entries(raw)

    assert tuple(width for width, _height, _payload in entries) == EXPECTED_SIZES
    assert all(width == height for width, height, _payload in entries)
    assert all(payload.startswith(PNG_SIGNATURE) for _width, _height, payload in entries)
    assert all(payload[25] == 6 for _width, _height, payload in entries)
    assert len(raw) < 128 * 1024
