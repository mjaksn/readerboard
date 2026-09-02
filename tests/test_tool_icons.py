"""Pin the shape of each tool's icon file.

Each desktop tool under ``tools/`` ships an ``icon.ico`` beside its code, drawn
as the ``icon.svg`` next to it and rendered by ``scripts/render_icons.py``. That
the rendering is current is checked in CI by the script's own ``--check``, which
needs Qt. This file needs nothing, and checks the container instead: that the
file is there, that it holds the drawing at every size Windows draws an icon
at rather than at one size scaled, and that each entry is the kind of image
every reader of the format understands. The header is a few packed integers,
so reading it here is cheaper than trusting it.
"""

from __future__ import annotations

import struct
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOLS = ("signsim", "apiclient")

# The sizes scripts/render_icons.py writes, which are the ones Windows asks
# for. A dimension of 256 is stored as 0, the field being one byte wide.
SIZES = {16, 20, 24, 32, 40, 48, 64, 256}


def icon_path(tool: str) -> Path:
    return REPO_ROOT / "tools" / tool / tool / "icon.ico"


def entries(data: bytes) -> list[tuple[int, int, int, int, int]]:
    """Each directory entry as (width, height, bits per pixel, size, offset)."""
    reserved, kind, count = struct.unpack_from("<HHH", data, 0)
    assert reserved == 0
    assert kind == 1, "type 1 is an icon; 2 would be a cursor"
    found = []
    for index in range(count):
        width, height, _colours, _reserved, _planes, bits, size, offset = struct.unpack_from(
            "<BBBBHHII", data, 6 + 16 * index
        )
        found.append((width or 256, height or 256, bits, size, offset))
    return found


@pytest.mark.parametrize("tool", TOOLS)
def test_each_tool_has_an_icon_beside_its_drawing(tool: str) -> None:
    assert icon_path(tool).with_suffix(".svg").is_file()
    assert icon_path(tool).is_file()


@pytest.mark.parametrize("tool", TOOLS)
def test_the_icon_holds_every_size_windows_draws(tool: str) -> None:
    found = entries(icon_path(tool).read_bytes())
    assert {width for width, _, _, _, _ in found} == SIZES
    assert all(width == height for width, height, _, _, _ in found)
    assert all(bits == 32 for _, _, bits, _, _ in found), "every entry carries an alpha channel"


@pytest.mark.parametrize("tool", TOOLS)
def test_each_entry_is_a_bitmap_or_at_the_largest_size_a_png(tool: str) -> None:
    data = icon_path(tool).read_bytes()
    for width, height, _, size, offset in entries(data):
        assert offset + size <= len(data), "an entry runs off the end of the file"
        entry = data[offset : offset + size]
        if width == 256:
            assert entry.startswith(b"\x89PNG\r\n\x1a\n")
            continue
        # A BITMAPINFOHEADER, with the height doubled to count the mask that
        # follows the colour data.
        header_size, bitmap_width, bitmap_height = struct.unpack_from("<Iii", entry, 0)
        assert header_size == 40
        assert (bitmap_width, bitmap_height) == (width, height * 2)
