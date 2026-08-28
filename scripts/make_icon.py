"""Build the Windows .ico from the icon artwork.

    uv run python scripts/make_icon.py

Reads ``assets/loriini-icon-1024.png`` and writes ``assets/loriini.ico``
carrying every size Windows asks for. Run it when the artwork changes; the
build script does not, because an icon changes about once a year and a build
happens all day.

Why not hand the .png straight to PyInstaller
---------------------------------------------
It accepts one, converts it with a smooth filter, and produces a smear at
16 px. Windows asks for the icon at 16 in the title bar and the task bar, at
32 and 48 in Explorer, and at 256 in the large-icon view -- so the small sizes
are the ones people actually look at, and they are the ones a smooth filter
ruins.

The artwork is pixel art on a 32 x 32 grid. That grid decides the filter:

* Where an art block lands on a whole number of output pixels -- 32, 64, 128
  and 256 -- nearest-neighbour reproduces it exactly. Anything else softens
  edges that were drawn hard on purpose.
* Where it does not -- 16, 24 and 48 -- nearest-neighbour would keep some
  blocks and drop others, which reads as a lumpy, half-eaten bird. Averaging
  over the block is the honest reduction: softer, but every block contributes.
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "assets" / "loriini-icon-1024.png"
TARGET = ROOT / "assets" / "loriini.ico"

#: Every size Windows asks for. 256 is the large-icon view and the Alt-Tab
#: switcher; 16 is the title bar and the task bar.
SIZES = (16, 24, 32, 48, 64, 128, 256)

#: The pixel-art grid the artwork is drawn on.
ART_GRID = 32


def render(source: Image.Image, size: int) -> Image.Image:
    """One size, filtered by whether the art grid divides into it."""
    crisp = size % ART_GRID == 0
    return source.resize(
        (size, size),
        Image.Resampling.NEAREST if crisp else Image.Resampling.BOX,
    )


def main() -> int:
    if not SOURCE.is_file():
        print(f"No artwork at {SOURCE}.", file=sys.stderr)
        return 1

    source = Image.open(SOURCE).convert("RGBA")
    if source.width != source.height:
        print(f"{SOURCE.name} is {source.width}x{source.height}, not square.", file=sys.stderr)
        return 1

    frames = [render(source, size) for size in SIZES]
    # Pillow writes the largest as the base and the rest as further entries.
    frames[-1].save(TARGET, format="ICO", sizes=[(s, s) for s in SIZES], append_images=frames[:-1])

    written = ", ".join(str(size) for size in SIZES)
    print(f"wrote {TARGET}  ({TARGET.stat().st_size / 1024:.1f} kB, sizes {written})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
