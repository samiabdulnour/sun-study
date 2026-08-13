"""Reading geometry and metadata out of an IFC file.

Everything that knows what IFC is lives here. ``core`` never sees an
``ifcopenshell`` object; it receives arrays and plain dataclasses.

Two units, and they are not the same
------------------------------------
This is the trap in this module. ``ifcopenshell``'s geometry iterator returns
vertices already converted to **SI metres**, whatever the file declares --
verified against the fixture, which is authored in millimetres and yields a
2.4 m window. Raw *attribute* values such as ``IfcSite.RefElevation`` are in the
project's declared units and are **not** converted.

So ``calculate_unit_scale`` must be applied to attributes and never to
geometry. Applying it to both shrinks a building by a factor of a thousand;
applying it to neither puts the site 20 km up. ``test_ifc_ingest.py`` pins both
halves.

Georeferencing is not optional
------------------------------
``ifcopenshell.util.geolocation.get_true_north`` returns ``0`` when
``TrueNorth`` is absent, and its body swallows exceptions and returns ``0`` as
well. That is precisely the "project Y axis is north" default the brief
forbids, so this module checks for the attribute itself and raises. The helper
is still used for the conversion once presence is established, because its sign
convention is documented and tested.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import ifcopenshell
import ifcopenshell.geom
import ifcopenshell.util.element
import ifcopenshell.util.geolocation
import ifcopenshell.util.shape
import ifcopenshell.util.unit
import numpy as np
import numpy.typing as npt

from sun_study.core.geometry import TriangleMesh
from sun_study.core.orientation import SiteOrientation

FloatArray = npt.NDArray[np.float64]

__all__ = [
    "OCCLUDER_CLASSES",
    "GeoreferencingError",
    "IfcElement",
    "IfcModel",
    "read_ifc",
]

# Brief section 5.2. The subject building is both analysed and an occluder:
# self-shading from balconies above, fins and reveals is the whole point.
OCCLUDER_CLASSES = (
    "IfcWall",
    "IfcSlab",
    "IfcRoof",
    "IfcColumn",
    "IfcRailing",
    "IfcCurtainWall",
    "IfcBuildingElementProxy",
    "IfcPlate",
    "IfcMember",
    "IfcBeam",
)


class GeoreferencingError(Exception):
    """The file cannot be placed on the earth, so no analysis is possible.

    Deliberately fatal and deliberately specific: the message names the
    Archicad setting the human has to change.
    """


@dataclass(frozen=True)
class IfcElement:
    """One IFC product with its triangulated geometry, in metres.

    ``vertices`` and ``faces`` are in the project coordinate frame. Tying that
    frame to the compass is ``SiteOrientation``'s job, not this dataclass's.
    """

    global_id: str
    ifc_class: str
    name: str
    long_name: str
    """``LongName`` where the entity has one, otherwise empty.

    For ``IfcSpace`` this is where the room's purpose lives -- "Living Room" --
    while ``Name`` is usually the apartment or zone number. Classifying rooms
    on ``Name`` alone silently matches nothing, which reads as "no living rooms
    in this building" rather than as an error. See decision D6.
    """
    predefined_type: str
    storey: str | None
    mesh: TriangleMesh

    @property
    def centroid(self) -> FloatArray:
        return np.asarray(self.mesh.vertices.mean(axis=0), dtype=np.float64)

    @property
    def bounds(self) -> tuple[FloatArray, FloatArray]:
        return (
            np.asarray(self.mesh.vertices.min(axis=0), dtype=np.float64),
            np.asarray(self.mesh.vertices.max(axis=0), dtype=np.float64),
        )


@dataclass(frozen=True)
class IfcModel:
    """Everything read out of one IFC file."""

    path: Path
    schema: str
    latitude_deg: float
    longitude_deg: float
    true_north_bearing_deg: float
    site_elevation_m: float
    length_unit_scale: float
    elements: tuple[IfcElement, ...]
    space_boundaries: dict[str, str] = field(default_factory=dict)
    """Window GlobalId -> space GlobalId, from ``IfcRelSpaceBoundary``."""
    site_rotation_deg: float = 0.0
    """How far ``IfcSite``'s placement turns the project frame, degrees CCW.

    Not used by the analysis, which reads world coordinates and takes its
    bearing from ``TrueNorth`` alone. It exists so a *second* statement of the
    georeferencing -- Archicad's live one -- can be compared against this file
    without the comparison depending on which IFC model-position option the
    export used.

    Archicad's "Survey Point" export writes the north rotation here and leaves
    ``TrueNorth`` at ``(0,1)``, because the world coordinates it produces are
    already true-north-aligned. Its "Project Origin" export does the opposite.
    Both are correct and self-consistent; only their sum is comparable to
    anything outside the file. See ``archicad.read.cross_check_georeferencing``.
    """

    def orientation(self, timezone: str) -> SiteOrientation:
        """Bind the file's georeferencing to an explicitly chosen timezone.

        IFC carries latitude and longitude but no IANA timezone, so decision D3
        makes the zone required configuration. It is supplied here rather than
        defaulted inside ``read_ifc`` so that a ``SiteOrientation`` without a
        real timezone never exists in the first place.
        """
        return SiteOrientation(
            latitude_deg=self.latitude_deg,
            longitude_deg=self.longitude_deg,
            timezone=timezone,
            true_north_bearing_deg=self.true_north_bearing_deg,
        )

    def of_class(self, *ifc_classes: str) -> tuple[IfcElement, ...]:
        wanted = set(ifc_classes)
        return tuple(element for element in self.elements if element.ifc_class in wanted)

    def by_id(self, global_id: str) -> IfcElement | None:
        return next((e for e in self.elements if e.global_id == global_id), None)

    def occluder_mesh(self, *, include_spaces: bool = False) -> TriangleMesh:
        """Every solid element merged into one triangle soup.

        ``IfcSpace`` is excluded by default: a Zone is a void, and treating it
        as an occluder would have every apartment shade itself completely.
        """
        skip = {"IfcSpace"} if not include_spaces else set()
        return TriangleMesh.concatenate([e.mesh for e in self.elements if e.ifc_class not in skip])

    def describe(self, timezone: str | None = None) -> str:
        counts: dict[str, int] = {}
        for element in self.elements:
            counts[element.ifc_class] = counts.get(element.ifc_class, 0) + 1
        summary = ", ".join(f"{name} {count}" for name, count in sorted(counts.items()))
        located = (
            self.orientation(timezone).describe()
            if timezone
            else (
                f"lat {self.latitude_deg:.6f} lon {self.longitude_deg:.6f} "
                f"true north bearing of model +Y {self.true_north_bearing_deg % 360.0:.3f} deg "
                f"(timezone not yet supplied)"
            )
        )
        return (
            f"{self.path.name} [{self.schema}] "
            f"unit scale {self.length_unit_scale:g} -> m, "
            f"site elevation {self.site_elevation_m:.3f} m\n"
            f"  {located}\n"
            f"  {summary}"
        )


def _resolve_true_north(model: ifcopenshell.file) -> float:
    """The true-north bearing of model +Y, degrees clockwise from true north.

    ``ifcopenshell``'s ``yaxis2angle`` returns "rotate project north
    anticlockwise by this angle to reach true north". Rotating true north
    *clockwise* by the same angle therefore lands on project north, and a
    clockwise angle from true north is exactly a compass bearing -- so the two
    numbers are equal. ``test_orientation.py`` and ``test_ifc_ingest.py`` pin
    this at the cardinal directions rather than trusting the paragraph.
    """
    contexts = model.by_type("IfcGeometricRepresentationContext", include_subtypes=False)
    for context in contexts:
        if context.TrueNorth is not None:
            ratios = context.TrueNorth.DirectionRatios
            if len(ratios) < 2:
                raise GeoreferencingError(
                    f"IfcGeometricRepresentationContext TrueNorth has {len(ratios)} "
                    f"direction ratios; two are needed to define a bearing."
                )
            return float(
                ifcopenshell.util.geolocation.yaxis2angle(float(ratios[0]), float(ratios[1]))
            )

    raise GeoreferencingError(
        "No TrueNorth on any IfcGeometricRepresentationContext. Set the North "
        "Direction in Archicad (Options > Project Preferences > Project Location) "
        "and re-export. Assuming the project Y axis points north would silently "
        "rotate every result."
    )


def _resolve_site_rotation(model: ifcopenshell.file) -> float:
    """How far ``IfcSite``'s own placement turns the project frame, degrees CCW.

    Archicad's "Survey Point" IFC export puts the north rotation here rather
    than in ``TrueNorth``, so a file can be perfectly georeferenced while
    ``TrueNorth`` reads ``(0,1)``. The analysis never needs this -- world
    coordinates already have it baked in -- but the cross-check against
    Archicad's live answer does, because Archicad reports north in the
    *project* frame and this is what separates the two.

    Returns 0 when the site has no placement or an unrotated one, which is
    both the common case and the safe default: a missing rotation reduces the
    cross-check to comparing ``TrueNorth`` alone, which is what it did before.
    """
    for site in model.by_type("IfcSite"):
        placement = getattr(site, "ObjectPlacement", None)
        relative = getattr(placement, "RelativePlacement", None)
        direction = getattr(relative, "RefDirection", None)
        ratios = getattr(direction, "DirectionRatios", None)
        if ratios is None or len(ratios) < 2:
            continue
        return float(np.degrees(np.arctan2(float(ratios[1]), float(ratios[0]))))
    return 0.0


def _resolve_location(model: ifcopenshell.file, unit_scale: float) -> tuple[float, float, float]:
    """Latitude and longitude in decimal degrees, and site elevation in metres."""
    sites = model.by_type("IfcSite")
    if not sites:
        raise GeoreferencingError(
            "No IfcSite in the file, so the project has no location. Set the "
            "Project Location in Archicad and re-export."
        )

    located = [s for s in sites if s.RefLatitude is not None and s.RefLongitude is not None]
    if not located:
        raise GeoreferencingError(
            f"IfcSite present ({len(sites)} of them) but none carries RefLatitude and "
            f"RefLongitude. Set the Project Location in Archicad (Options > Project "
            f"Preferences > Project Location) and re-export. Sun position cannot be "
            f"computed without a latitude, and guessing one is not acceptable."
        )
    if len(located) > 1:
        raise GeoreferencingError(
            f"{len(located)} IfcSite entities carry a location. Which one governs is "
            f"ambiguous, so the run is stopped rather than picking one."
        )

    site = located[0]
    latitude = ifcopenshell.util.geolocation.dms2dd(*site.RefLatitude)
    longitude = ifcopenshell.util.geolocation.dms2dd(*site.RefLongitude)

    # RefElevation is an IfcLengthMeasure, so it is in project units and needs
    # the scale that geometry has already had applied to it.
    elevation = float(site.RefElevation or 0.0) * unit_scale
    return float(latitude), float(longitude), elevation


def _storey_name(product: Any) -> str | None:
    container = ifcopenshell.util.element.get_container(product)
    if container is None:
        return None
    name = getattr(container, "Name", None)
    return str(name) if name is not None else None


def _iterate_shapes(
    model: ifcopenshell.file, include: Sequence[str] | None
) -> Iterator[tuple[Any, TriangleMesh]]:
    """Triangulated geometry per product, in world coordinates and metres."""
    settings = ifcopenshell.geom.settings()
    # World coordinates: placements are baked into the vertices, so downstream
    # code never has to walk an IfcLocalPlacement chain.
    settings.set("use-world-coords", True)
    # Left at its default of False, which yields SI metres. Setting it True
    # would return project units and silently break every distance.
    settings.set("convert-back-units", False)

    iterator = ifcopenshell.geom.iterator(
        settings, model, 1, include=list(include) if include else None
    )
    if not iterator.initialize():
        return

    while True:
        shape = iterator.get()
        vertices = np.asarray(
            ifcopenshell.util.shape.get_vertices(shape.geometry), dtype=np.float64
        )
        faces = np.asarray(ifcopenshell.util.shape.get_faces(shape.geometry), dtype=np.int64)
        if len(faces):
            yield model.by_guid(shape.guid), TriangleMesh(vertices, faces)
        if not iterator.next():
            break


def _read_space_boundaries(model: ifcopenshell.file) -> dict[str, str]:
    """Window GlobalId -> space GlobalId, where the exporter provided it.

    Archicad only writes these when space boundary export is enabled, so this
    is frequently empty and the caller must be able to cope. See
    ``ingest.scene`` for the geometric fallback.
    """
    boundaries: dict[str, str] = {}
    for boundary in model.by_type("IfcRelSpaceBoundary"):
        element = boundary.RelatedBuildingElement
        space = boundary.RelatingSpace
        if element is None or space is None:
            continue
        if element.is_a("IfcWindow"):
            boundaries[element.GlobalId] = space.GlobalId
    return boundaries


def read_ifc(path: str | Path, *, include: Sequence[str] | None = None) -> IfcModel:
    """Read an IFC file into plain geometry and metadata.

    Raises ``GeoreferencingError`` when latitude, longitude or true north is
    missing. That is not a fallback-worthy condition: every one of them silently
    rotates or relocates the results, and a wrong answer here looks exactly like
    a right one.
    """
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"No IFC file at {path}")

    model = ifcopenshell.open(str(path))
    unit_scale = float(ifcopenshell.util.unit.calculate_unit_scale(model))

    bearing = _resolve_true_north(model)
    latitude, longitude, elevation = _resolve_location(model, unit_scale)

    elements = []
    for product, mesh in _iterate_shapes(model, include):
        if product is None:
            continue
        elements.append(
            IfcElement(
                global_id=product.GlobalId,
                ifc_class=product.is_a(),
                name=str(product.Name or ""),
                long_name=str(getattr(product, "LongName", None) or ""),
                predefined_type=str(getattr(product, "PredefinedType", None) or ""),
                storey=_storey_name(product),
                mesh=mesh,
            )
        )

    return IfcModel(
        path=path,
        schema=str(model.schema),
        latitude_deg=latitude,
        longitude_deg=longitude,
        true_north_bearing_deg=bearing,
        site_elevation_m=elevation,
        length_unit_scale=unit_scale,
        elements=tuple(elements),
        space_boundaries=_read_space_boundaries(model),
        site_rotation_deg=_resolve_site_rotation(model),
    )
