"""Reading geometry out of a Rhino ``.3dm`` file.

**Validation path only.** Decision D20: the product ingests IFC. This module
exists so the reference comparison in ``docs/validation.md`` can run on the
*exact* geometry the Grasshopper/Ladybug study used, rather than on an IFC
re-export of it. A re-export introduces geometry differences that show up as a
systematic offset indistinguishable from an engine error, which would weaken
the one check the whole project's credibility rests on.

``rhino3dm`` is therefore a dev-group dependency, like ``pvlib``, and is
imported lazily so a runtime-only install neither needs it nor breaks without
it. No Rhino installation or licence is involved: ``rhino3dm`` is a pure
openNURBS wheel.

Getting geometry out of a Brep
------------------------------
Rhino files store NURBS Breps, and ``rhino3dm`` cannot tessellate them -- that
needs Rhino's meshing kernel. It can, however, reach the **render mesh Rhino
already cached per Brep face**, which is what the viewport draws.

The API for this is not where you would look: ``Brep`` has no ``GetMesh``, but
``BrepFace`` does. On the reference model that recovers 25,825 of 25,845 faces
(99.92%), and the resulting area matches the published figure for the analysed
facade to 6 parts in 10 million. Faces with no cached mesh are counted and
reported rather than passed over.

What the file does *not* contain
--------------------------------
Neither latitude, longitude nor a meaningful north. ``EarthAnchorPoint`` is
unset on every file examined and ``ModelNorth`` is Rhino's default ``(0,1,0)``,
which is indistinguishable from a deliberate "project Y is north". Those live
in the Grasshopper definition, not the model, so they must be supplied
explicitly -- the same rule ``ingest.ifc`` applies, for the same reason.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import numpy.typing as npt

from sun_study.core.geometry import TriangleMesh

FloatArray = npt.NDArray[np.float64]

__all__ = ["RhinoElement", "RhinoModel", "RhinoUnavailableError", "read_3dm"]

# Rhino writes this sentinel for an unset EarthAnchorPoint latitude/longitude.
_UNSET = 1e300


class RhinoUnavailableError(Exception):
    """``rhino3dm`` is not installed."""


def _rhino3dm() -> Any:
    try:
        import rhino3dm
    except ImportError as exc:  # pragma: no cover - exercised by the bare CI job
        raise RhinoUnavailableError(
            "Reading .3dm needs the optional 'rhino3dm' package, which is a "
            "validation-only dependency. Install the dev group with 'uv sync', "
            "or use the IFC path for a normal run."
        ) from exc
    return rhino3dm


@dataclass(frozen=True)
class RhinoElement:
    """One Rhino object, triangulated, in model units."""

    object_id: str
    name: str
    layer: str
    mesh: TriangleMesh

    @property
    def centroid(self) -> FloatArray:
        return np.asarray(self.mesh.vertices.mean(axis=0), dtype=np.float64)


@dataclass(frozen=True)
class RhinoModel:
    """Everything read out of one ``.3dm``."""

    path: Path
    unit_system: str
    length_unit_scale: float
    elements: tuple[RhinoElement, ...]
    layers: tuple[str, ...]
    faces_without_mesh: int = 0
    has_geolocation: bool = False
    model_north: tuple[float, float, float] = (0.0, 1.0, 0.0)
    extras: dict[str, Any] = field(default_factory=dict)

    def on_layers(self, *fragments: str) -> tuple[RhinoElement, ...]:
        """Elements whose layer path contains any of ``fragments``."""
        return tuple(e for e in self.elements if any(f in e.layer for f in fragments))

    def mesh_on_layers(self, *fragments: str) -> TriangleMesh:
        return TriangleMesh.concatenate([e.mesh for e in self.on_layers(*fragments)])

    def describe(self) -> str:
        located = "geolocated" if self.has_geolocation else "NO geolocation (must be supplied)"
        return (
            f"{self.path.name} [Rhino] units {self.unit_system} "
            f"(scale {self.length_unit_scale:g} -> m), {located}\n"
            f"  {len(self.elements)} elements, "
            f"{sum(e.mesh.triangle_count for e in self.elements)} triangles, "
            f"{len(self.layers)} layers, "
            f"{self.faces_without_mesh} brep faces without a cached mesh"
        )


# Rhino UnitSystem -> metres. Only the ones an architectural model plausibly
# uses; anything else raises rather than guessing a scale.
_UNIT_SCALE = {
    "Millimeters": 0.001,
    "Centimeters": 0.01,
    "Meters": 1.0,
    "Kilometers": 1000.0,
    "Inches": 0.0254,
    "Feet": 0.3048,
}


def _mesh_to_triangles(mesh: Any) -> tuple[FloatArray, npt.NDArray[np.int64]]:
    """Rhino mesh to vertices and triangles, splitting quads."""
    vertices = np.array([[p.X, p.Y, p.Z] for p in mesh.Vertices], dtype=np.float64)
    faces: list[tuple[int, int, int]] = []
    for index in range(len(mesh.Faces)):
        face = mesh.Faces[index]
        a, b, c, d = face[0], face[1], face[2], face[3]
        faces.append((a, b, c))
        if d != c:
            # Rhino marks a triangle by repeating the last index; anything else
            # is a quad and needs splitting.
            faces.append((a, c, d))
    return vertices, np.array(faces, dtype=np.int64).reshape(-1, 3)


def _object_meshes(geometry: Any, rhino: Any) -> Iterator[Any]:
    """Every Rhino mesh belonging to an object, Brep render meshes included."""
    if isinstance(geometry, rhino.Mesh):
        yield geometry
        return
    if isinstance(geometry, rhino.Brep):
        for face in geometry.Faces:
            cached = face.GetMesh(rhino.MeshType.Render)
            if cached is not None and len(cached.Faces) > 0:
                yield cached
            else:
                yield None


def read_3dm(
    path: str | Path,
    *,
    layer_fragments: Sequence[str] | None = None,
    scale_to_metres: bool = True,
) -> RhinoModel:
    """Read a ``.3dm`` into triangulated elements.

    ``layer_fragments`` restricts the read to layers whose full path contains
    one of them, which matters on a real working file: the reference model
    carries drawing linework, text, hatches and layout furniture that would
    otherwise be loaded as geometry and analysed as if it were a building.
    """
    rhino = _rhino3dm()
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"No Rhino file at {path}")

    document = rhino.File3dm.Read(str(path))
    if document is None:
        raise ValueError(f"{path} could not be read as a Rhino 3dm file")

    unit_system = str(document.Settings.ModelUnitSystem).rsplit(".", 1)[-1]
    if unit_system not in _UNIT_SCALE:
        raise ValueError(
            f"{path} declares unit system {unit_system!r}, which has no known "
            f"conversion to metres. Add it to _UNIT_SCALE rather than assuming one."
        )
    scale = _UNIT_SCALE[unit_system] if scale_to_metres else 1.0

    layers = {i: layer.FullPath for i, layer in enumerate(document.Layers)}
    anchor = document.Settings.EarthAnchorPoint
    latitude = float(anchor.EarthBasepointLatitude)
    longitude = float(anchor.EarthBasepointLongitude)
    located = abs(latitude) < 90.0 and abs(longitude) < 180.0 and abs(latitude) < _UNSET

    elements: list[RhinoElement] = []
    missing = 0

    for obj in document.Objects:
        layer = layers.get(obj.Attributes.LayerIndex, "")
        if layer_fragments and not any(f in layer for f in layer_fragments):
            continue

        vertex_groups, face_groups, offset = [], [], 0
        for mesh in _object_meshes(obj.Geometry, rhino):
            if mesh is None:
                missing += 1
                continue
            vertices, faces = _mesh_to_triangles(mesh)
            if not len(faces):
                continue
            vertex_groups.append(vertices * scale)
            face_groups.append(faces + offset)
            offset += len(vertices)

        if not face_groups:
            continue

        elements.append(
            RhinoElement(
                object_id=str(obj.Attributes.Id),
                name=str(obj.Attributes.Name or ""),
                layer=layer,
                mesh=TriangleMesh(
                    np.concatenate(vertex_groups).astype(np.float64),
                    np.concatenate(face_groups).astype(np.int64),
                ),
            )
        )

    north = anchor.ModelNorth
    return RhinoModel(
        path=path,
        unit_system=unit_system,
        length_unit_scale=scale,
        elements=tuple(elements),
        layers=tuple(sorted(layers.values())),
        faces_without_mesh=missing,
        has_geolocation=located,
        model_north=(float(north.X), float(north.Y), float(north.Z)),
        extras={
            "latitude": latitude if located else None,
            "longitude": longitude if located else None,
        },
    )
