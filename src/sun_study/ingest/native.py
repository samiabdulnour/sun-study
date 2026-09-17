"""The model read straight out of Archicad, without an IFC in the middle.

Why this exists
---------------
The IFC route works and is not going away -- it is how a file on disk is
analysed, and ``--ifc-in`` snapshots are how a run is repeated cheaply. What it
is not is a thing an office can be handed. On the reference project the export
came to **692 MB**, of which roughly nine tenths was ``IfcPropertySingleValue``
and ``IfcQuantityLength`` that nothing here reads, and the only cure is the
"Properties to export" setting inside Archicad's IFC translator.

That setting cannot be reached from an add-on. Every IFC function the API
offers reads, converts, or opens a dialog for a person: there is no call that
creates or edits a translator. So an IFC-shaped answer always ends in "set this
up by hand on every machine", which is not something that ships.

The geometry, though, can be read directly. ``API_BodyType`` carries a
``parent`` -- the floor plan element the body was converted from -- and that
header holds the element's guid, its layer and its storey, which is exactly
what this package needs. The add-on walks the 3D model, triangulates it and
writes the result here; nothing is serialised that the analysis does not read.

What comes across, and what does not
------------------------------------
Triangles, per element, with the element's guid, layer, storey and type. No
properties, no quantities, no classifications, no materials. The analysis has
never read any of those.

**Zones are deliberately absent.** A Zone's outline, name, storey and category
already come from Archicad directly through ``archicad.read.zones`` -- the
drawing side has always read them that way -- and asking for the same fact
twice is how the two answers get to disagree. A native run pairs the triangles
here with the Zones read there.

The file is a private format between one add-on and one reader, versioned so
that a mismatched pair says so instead of reading rubbish. It is not a model
interchange format and should never become one: the moment it needs to carry
anything the analysis does not use, IFC is the better answer.
"""

from __future__ import annotations

import struct
from pathlib import Path

import numpy as np
import numpy.typing as npt

from sun_study.core.geometry import TriangleMesh
from sun_study.ingest.ifc import IfcElement, IfcModel

FloatArray = npt.NDArray[np.float64]

__all__ = ["MAGIC", "VERSION", "NativeModelError", "read_native_model"]

#: First eight bytes of the file. Checked rather than assumed, so a truncated
#: download or a path pointing at the wrong thing fails here with a sentence
#: instead of somewhere downstream as a geometry error.
MAGIC = b"LORIINIM"

#: The format's version. Version 3 is the first whose geometry is placed
#: through each body's own transform: before it, library parts -- every
#: window and door -- arrived in their own local frames, clustered around
#: z = 0 whatever storey they belonged to. A file written by an older add-on
#: is refused rather than read, because that geometry is wrong in a way
#: nothing downstream could detect.
#:
#: The add-on writes it and this refuses anything else:
#: an add-on and a reader that disagree about the layout would otherwise read
#: each other's bytes as coordinates, which produces a model rather than an
#: error and is the worst outcome available.
VERSION = 3

#: Triangle coordinates are float32. At a kilometre from the project origin
#: that resolves to well under a tenth of a millimetre, which is finer than the
#: 1 mm the shadow tracer works to and far finer than anything a sheet shows.
#: It also halves the file against float64 for no accuracy that matters.
_TRIANGLE = np.dtype(np.float32)


class NativeModelError(Exception):
    """The file is not one of ours, or not one this version can read."""


def _text(data: bytes, at: int) -> tuple[str, int]:
    """One length-prefixed UTF-8 string, and where the next field starts."""
    (length,) = struct.unpack_from("<I", data, at)
    at += 4
    return data[at : at + length].decode("utf-8", errors="replace"), at + length


def read_native_model(path: str | Path, timezone_hint: str = "") -> IfcModel:
    """Read a model the add-on wrote, as the same ``IfcModel`` IFC produces.

    The return type is the point. Everything downstream -- the scene, the
    occluders, the sampling, the drawing -- consumes ``IfcModel`` and knows
    nothing about where it came from, so a second source is a reader and not a
    second pipeline. ``ingest.ifc`` remains the one that reads files people
    exchange; this one reads what the add-on hands over.

    ``timezone_hint`` is unused and accepted so the two readers can be called
    alike; the orientation travels in the file.
    """
    blob = Path(path).read_bytes()
    if len(blob) < 24 or blob[:8] != MAGIC:
        raise NativeModelError(
            f"{path} is not a Loriini model file: it does not start with {MAGIC!r}. "
            f"If this was written by an older add-on, rebuild it; if it is an IFC, "
            f"pass it as --ifc-in instead."
        )
    version, latitude, longitude, north_deg, elevation, count = struct.unpack_from(
        "<Idddd I", blob, 8
    )
    if version != VERSION:
        raise NativeModelError(
            f"{path} is version {version} and this reads version {VERSION}. The add-on "
            f"and the tool have to be updated together; rebuild whichever is older."
        )

    at = 8 + struct.calcsize("<Idddd I")
    elements: list[IfcElement] = []
    for _ in range(count):
        guid, at = _text(blob, at)
        layer, at = _text(blob, at)
        kind, at = _text(blob, at)
        name, at = _text(blob, at)
        # The Zone's own name, which is not its element ID: on one project the
        # ID is "Communal Open Space" and the name is "NON RESIDENTIAL", and it
        # is the name a study selects on. Archicad's IFC export makes the same
        # split between Name and LongName, and `scene._named_one_of` checks
        # both because which one holds what varies by translator.
        long_name, at = _text(blob, at)
        storey, at = _text(blob, at)
        (triangles,) = struct.unpack_from("<I", blob, at)
        at += 4

        floats = triangles * 9
        vertices = np.frombuffer(blob, dtype=_TRIANGLE, count=floats, offset=at).astype(np.float64)
        at += floats * _TRIANGLE.itemsize

        # Already triangles, so the faces are simply consecutive triples. The
        # add-on fans Archicad's *convex* polygons, where a fan is exact; a fan
        # over a general polygon would fill its holes, and a wall's window
        # would then stop the sunlight coming through it.
        mesh = TriangleMesh(
            vertices.reshape(-1, 3),
            np.arange(triangles * 3, dtype=np.int64).reshape(-1, 3),
        )
        elements.append(
            IfcElement(
                global_id=guid,
                ifc_class=kind,
                name=name,
                long_name=long_name,
                predefined_type="",
                storey=storey or None,
                mesh=mesh,
                layer=layer,
            )
        )

    return IfcModel(
        path=Path(path),
        schema="LORIINI",
        latitude_deg=latitude,
        longitude_deg=longitude,
        true_north_bearing_deg=north_deg,
        site_elevation_m=elevation,
        length_unit_scale=1.0,
        elements=tuple(elements),
    )
