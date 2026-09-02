#!/usr/bin/env python3
"""Render each tool's icon.svg into the icon.ico beside it.

The two desktop tools under tools/ each carry an icon drawn as an SVG, which
is the file a person edits, and ship it as a Windows icon file, which is the
one Qt loads. The .ico holds the drawing at every size Windows asks for, from
the 16 pixel title bar corner up to the 256 pixel tile, so that none of them
is a scaled copy of another. Qt reads .ico on every platform, so one file
serves Linux and macOS as well.

    python scripts/render_icons.py            rewrite both .ico files
    python scripts/render_icons.py --check    exit 1 if either is stale

The SVG is rasterised with Qt's own renderer, so this needs PySide6 and
nothing else: no Pillow, no ImageMagick, no dependency the tools do not
already pin. The .ico container is written here rather than by Qt, because
Qt's writer puts one image in a file and an icon file is only worth having for
holding several.

--check renders afresh and compares against what is committed, pixel by pixel
with a small tolerance, so that an edit to the SVG without a rerun of this
fails CI rather than shipping a drawing the window never shows. The tolerance
is there because antialiasing is allowed to differ in the last bit between
Qt builds, and a stale icon differs by a great deal more than that.
"""

from __future__ import annotations

import argparse
import os
import struct
import sys
from pathlib import Path

# Nothing here needs a screen, and asking for one would fail on a CI runner
# and flash on a desktop. Set before Qt is imported, which is what decides it.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QBuffer, QIODevice, QRectF, Qt
from PySide6.QtGui import QGuiApplication, QImage, QImageReader, QPainter
from PySide6.QtSvg import QSvgRenderer

_ROOT = Path(__file__).resolve().parent.parent

# Each tool keeps its icon beside its code, the same as its names.py.
ICONS = (
    _ROOT / "tools" / "signsim" / "signsim" / "icon.svg",
    _ROOT / "tools" / "apiclient" / "apiclient" / "icon.svg",
)

# What Windows draws an icon at: the title bar corner at 16, the taskbar at 24
# or 32 depending on the display scaling, the larger steps for the settings
# that scale everything, 64 for jump lists, and 256 for the tile and the file
# browser's largest view. The 256 goes in as a PNG, as the format allows and
# Windows expects at that size; everything smaller is a plain bitmap, which
# every reader of the format understands.
SIZES = (16, 20, 24, 32, 40, 48, 64, 256)
PNG_FROM = 256

# How far apart a channel value may be before two renders are different
# drawings rather than different antialiasing.
TOLERANCE = 8


def render(svg: Path, size: int) -> QImage:
    """Rasterise one SVG at one size, with a transparent ground."""
    renderer = QSvgRenderer(str(svg))
    if not renderer.isValid():
        raise SystemExit("%s: not an SVG Qt can read" % svg)
    image = QImage(size, size, QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    renderer.render(painter, QRectF(0, 0, size, size))
    painter.end()
    return image


def rows_of(image: QImage) -> list[bytes]:
    """Read the pixel rows, top first, as 32-bit BGRA, which is what a bitmap wants."""
    image = image.convertToFormat(QImage.Format.Format_ARGB32)
    raw = bytes(image.constBits())
    stride = image.bytesPerLine()
    width = image.width() * 4
    return [raw[y * stride : y * stride + width] for y in range(image.height())]


def as_bitmap(image: QImage) -> bytes:
    """One icon entry as a bitmap: header, colour rows bottom up, then the mask.

    The header is a BITMAPINFOHEADER with the height doubled, because the format
    counts the one-bit mask that follows the colour data as part of the same
    image. The mask marks a pixel transparent where the alpha is zero, for
    readers that predate alpha; anything newer reads the alpha and ignores it.
    """
    rows = rows_of(image)
    width, height = image.width(), image.height()
    colour = b"".join(reversed(rows))
    mask_stride = ((width + 31) // 32) * 4
    mask = bytearray()
    for row in reversed(rows):
        bits = "".join("1" if row[x * 4 + 3] == 0 else "0" for x in range(width))
        bits = bits.ljust(mask_stride * 8, "0")
        mask += int(bits, 2).to_bytes(mask_stride, "big")
    header = struct.pack(
        "<IiiHHIIiiII",
        40,
        width,
        height * 2,
        1,
        32,
        0,
        len(colour) + len(mask),
        0,
        0,
        0,
        0,
    )
    return header + colour + bytes(mask)


def as_png(image: QImage) -> bytes:
    """One icon entry as a PNG, which the format allows and Windows expects at 256."""
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    if not image.save(buffer, "PNG"):
        raise SystemExit("Qt could not encode a PNG, which it ships with")
    return bytes(buffer.data())


def pack(images: list[QImage]) -> bytes:
    """Wrap the rendered images in the icon file container."""
    entries = [as_png(image) if image.width() >= PNG_FROM else as_bitmap(image) for image in images]
    header = struct.pack("<HHH", 0, 1, len(images))
    offset = len(header) + 16 * len(images)
    directory = bytearray()
    for image, entry in zip(images, entries, strict=True):
        # A dimension of 256 is written as 0, the field being one byte wide.
        directory += struct.pack(
            "<BBBBHHII",
            image.width() % 256,
            image.height() % 256,
            0,
            0,
            1,
            32,
            len(entry),
            offset,
        )
        offset += len(entry)
    return header + bytes(directory) + b"".join(entries)


def committed(ico: Path) -> dict[int, QImage]:
    """Every image in an existing icon file, by size."""
    reader = QImageReader(str(ico))
    found: dict[int, QImage] = {}
    while True:
        image = reader.read()
        if image.isNull():
            break
        found[image.width()] = image
        if not reader.jumpToNextImage():
            break
    return found


def differ(fresh: QImage, stored: QImage) -> bool:
    """Whether two renders are different drawings rather than different antialiasing."""
    if fresh.size() != stored.size():
        return True
    for fresh_row, stored_row in zip(rows_of(fresh), rows_of(stored), strict=True):
        for a, b in zip(fresh_row, stored_row, strict=True):
            if abs(a - b) > TOLERANCE:
                return True
    return False


def stale(svg: Path, images: list[QImage]) -> str | None:
    """Why the committed icon does not match the SVG, or None if it does."""
    ico = svg.with_suffix(".ico")
    if not ico.exists():
        return "missing"
    stored = committed(ico)
    if set(stored) != set(SIZES):
        return "holds sizes %s rather than %s" % (sorted(stored), list(SIZES))
    for image in images:
        if differ(image, stored[image.width()]):
            return "differs at %d pixels" % image.width()
    return None


def main(argv: list[str] | None = None) -> int:
    """Render every icon, or check that every icon is already rendered."""
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit 1 if any icon.ico differs from what its icon.svg renders to, rather than rewriting it",
    )
    args = parser.parse_args(argv)

    QGuiApplication([])
    out_of_date = []
    for svg in ICONS:
        images = [render(svg, size) for size in SIZES]
        ico = svg.with_suffix(".ico")
        relative = ico.relative_to(_ROOT)
        if args.check:
            reason = stale(svg, images)
            if reason is not None:
                out_of_date.append("%s: %s" % (relative, reason))
            continue
        ico.write_bytes(pack(images))
        print("wrote %s" % relative)

    if out_of_date:
        for line in out_of_date:
            print(line, file=sys.stderr)
        print("run scripts/render_icons.py and commit the result", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
