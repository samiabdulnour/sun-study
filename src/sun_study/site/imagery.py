"""Aerial imagery from NSW SIX Maps, saved to disk with world files.

Archicad's JSON API has no command that places a picture, so the aerial is
not drawn into the worksheet: it is written beside the run as JPEG tiles,
each with a ``.jgw`` world file on the MGA grid, for File > External Content
> Place External Drawing, or for the day the add-on grows a command for it.
"""

from __future__ import annotations

import math
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sun_study.site.geo import Extent, lonlat_to_mga, mercator_to_lonlat, mga_zone
from sun_study.site.http import FetchError, fetch

__all__ = ["Aerial", "Tile", "aerial"]

EXPORT = "https://maps.six.nsw.gov.au/arcgis/rest/services/public/NSW_Imagery/MapServer/export"
MAX_PX = 4000


@dataclass(frozen=True)
class Tile:
    file: str
    left: float
    top: float
    width: float
    height: float
    """Fractions of the full extent this tile covers; row 0 is the top."""

    def as_dict(self) -> dict[str, Any]:
        return {
            "file": self.file,
            "left": self.left,
            "top": self.top,
            "width": self.width,
            "height": self.height,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Tile:
        return cls(
            str(data["file"]),
            float(data["left"]),
            float(data["top"]),
            float(data["width"]),
            float(data["height"]),
        )


@dataclass(frozen=True)
class Aerial:
    tiles: tuple[Tile, ...]
    px_width: int
    px_height: int
    folder: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "tiles": [t.as_dict() for t in self.tiles],
            "px_width": self.px_width,
            "px_height": self.px_height,
            "folder": self.folder,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Aerial:
        return cls(
            tuple(Tile.from_dict(t) for t in data.get("tiles") or []),
            int(data["px_width"]),
            int(data["px_height"]),
            str(data.get("folder") or ""),
        )


def _tile(x0: float, y0: float, x1: float, y1: float, width: int, height: int) -> bytes:
    """One export, stepped down until the service accepts the pixel count.

    The service rejects requests above a per-area ceiling that varies by
    suburb, with a 500 rather than a clamp.
    """
    w, h = width, height
    for step in range(5):
        params = {
            "bbox": f"{x0},{y0},{x1},{y1}",
            "bboxSR": "3857",
            "imageSR": "3857",
            "size": f"{round(w)},{round(h)}",
            "format": "jpg",
            "compressionQuality": "70",
            "f": "image",
        }
        try:
            body = fetch(
                f"{EXPORT}?{urllib.parse.urlencode(params)}",
                attempts=3 if step == 0 else 2,
                timeout=120.0,
                label="aerial export",
            )
            if len(body) >= 1000:
                return body
        except FetchError:
            pass
        w, h = round(w / 1.6), round(h / 1.6)
        if w < 500:
            break
    raise FetchError("aerial imagery unavailable for this extent")


def aerial(extent: Extent, target_px_width: int, folder: Path) -> Aerial:
    """Fetch the extent as tiles into ``folder`` and write their world files."""
    folder.mkdir(parents=True, exist_ok=True)
    aspect = extent.height / extent.width
    # Never beyond the imagery's native resolution (~7.5 cm in metro NSW).
    max_px = int(extent.width / 0.09)
    px_w = min(target_px_width, max_px)
    px_h = round(px_w * aspect)
    cols = math.ceil(px_w / MAX_PX)
    rows = math.ceil(px_h / MAX_PX)

    centre_lon, _ = mercator_to_lonlat(*extent.centre)
    zone = mga_zone(centre_lon)

    tiles: list[Tile] = []
    for r in range(rows):
        for c in range(cols):
            x0 = extent.xmin + extent.width * c / cols
            x1 = extent.xmin + extent.width * (c + 1) / cols
            y1 = extent.ymax - extent.height * r / rows
            y0 = extent.ymax - extent.height * (r + 1) / rows
            w, h = round(px_w / cols), round(px_h / rows)
            body = _tile(x0, y0, x1, y1, w, h)
            name = f"aerial_{r}_{c}.jpg"
            (folder / name).write_bytes(body)
            _world_file(folder / f"aerial_{r}_{c}.jgw", x0, y0, x1, y1, w, h, zone)
            tiles.append(Tile(name, c / cols, r / rows, 1 / cols, 1 / rows))
    return Aerial(tuple(tiles), px_w, px_h, str(folder))


def _world_file(
    path: Path, x0: float, y0: float, x1: float, y1: float, w: int, h: int, zone: int
) -> None:
    """A JGW on the MGA grid: pixel size, rotation terms and the top-left centre."""

    def mga(x: float, y: float) -> tuple[float, float]:
        lon, lat = mercator_to_lonlat(x, y)
        return lonlat_to_mga(lon, lat, zone)

    sw, se, nw = mga(x0, y0), mga(x1, y0), mga(x0, y1)
    a = (se[0] - sw[0]) / w
    d = (se[1] - sw[1]) / w
    b = (sw[0] - nw[0]) / h
    e = (sw[1] - nw[1]) / h
    path.write_text(
        f"{a:.6f}\n{d:.6f}\n{b:.6f}\n{e:.6f}\n{nw[0] + a / 2:.3f}\n{nw[1] + e / 2:.3f}\n",
        encoding="utf-8",
    )
