"""Reading a live Archicad project through the Tapir add-on.

The geometry never comes down this wire. Archicad exports an IFC and
``ingest.ifc`` reads it, because that path is already tested against a
committed fixture and a golden file. What this module adds is the two things
IFC cannot give: a *live* project to export from, and a second, independent
statement of the georeferencing to check the export against.

Why the cross-check exists
--------------------------
``GetGeoLocation`` reports the north direction in **radians**, and the Tapir
sources do not say which sense that angle runs in -- see
``ASSUMED_NORTH_SENSE`` below. Rather than guess and hope, this module treats
the IFC's ``TrueNorth`` as the source of truth (that conversion is pinned by
tests) and uses ``GetGeoLocation`` only to *contradict* it. A disagreement is
fatal and the message names the likely cause, so a sign error surfaces as a
sign error instead of as a building rotated into the wrong hemisphere.

Commands used, with the add-on version each arrived in:

===========================  =======  ===============================
Command                      Since    Used for
===========================  =======  ===============================
``GetProjectInfo``           0.1.0    Naming the run in the header
``GetGeoLocation``           1.1.6    Cross-checking the IFC's north
``GetElementsByType``        1.0.7    Finding the Zones
``GetDetailsOfElements``     1.0.7    Zone name and number
``GetClassificationsOfElements``
                             1.0.7    Property availability, see ``write``
``GetElementsByIFCIds``      1.5.1    IFC GlobalId -> Archicad GUID
``GetIFCIdsOfElements``      1.5.1    The same join, inverted
``IFCFileOperation``         1.2.6    Exporting the model to analyse
===========================  =======  ===============================
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from sun_study.archicad.connection import ArchicadConnection, ArchicadError, CommandFailedError
from sun_study.core.orientation import SiteOrientation
from sun_study.ingest.ifc import IfcModel

__all__ = [
    "NORTH_ANGLE_OFFSET_DEG",
    "ArchicadZone",
    "GeoLocation",
    "GeoreferencingMismatchError",
    "LibraryObject",
    "ProjectInfo",
    "cross_check_georeferencing",
    "disambiguated",
    "elements_by_ifc_ids",
    "export_ifc",
    "gdl_parameters",
    "layer_names",
    "library_objects",
    "north_bearing_deg",
    "project_info",
    "read_geo_location",
    "zones",
]

#: Where Archicad's ``north`` angle points, and why the offset is 90 degrees.
#:
#: ``API_PlaceInfo::north`` is **the angle of the true-north direction measured
#: counter-clockwise from the project +X axis, in radians.** Tapir passes it
#: through undocumented, and nothing in the add-on sources says which sense it
#: runs in, so this was derived from a real project rather than assumed --
#: three independent numbers out of one Archicad 26 model, all agreeing:
#:
#: =============================  ===========================================
#: ``GetGeoLocation``             ``north = 0.856118 rad`` = 49.0518 deg
#: ``IfcSite`` ``RefDirection``   ``(0.755304, 0.655374)``
#:                                = ``(sin 49.0518, cos 49.0518)``
#: Walls, in world coordinates    40.948 deg = 90 - 49.0518
#: =============================  ===========================================
#:
#: The mirrored convention would have put those walls at 130.9 degrees, and
#: they were not there, so the sign is settled by measurement. Two further
#: checks agree: the offset makes Archicad's *default* north pi/2 rather than
#: 0, which is what "true north runs along project +Y" should report, and
#: substituting the three numbers into ``cross_check_georeferencing`` below
#: balances exactly. See decision D23.
NORTH_ANGLE_OFFSET_DEG = 90.0

#: How far the two georeferencing sources may differ before the run stops.
#: Degrees for the bearing, decimal degrees for the coordinates. Both are
#: loose enough to absorb Archicad writing a rounded value into the IFC and
#: tight enough that a real disagreement cannot hide: 0.01 degrees of latitude
#: is about 1.1 km, and 0.01 degrees of north rotates a 50 m facade by 9 mm.
NORTH_TOLERANCE_DEG = 0.01
COORDINATE_TOLERANCE_DEG = 0.01

#: Archicad answers ``GetClassificationsOfElements`` for an *unclassified*
#: element with the null GUID rather than with an error, so an element that
#: carries no classification still produces a well-formed identifier. Treating
#: it as real would offer a property to nothing and look like it had worked.
NULL_GUID = "00000000-0000-0000-0000-000000000000"


class GeoreferencingMismatchError(ArchicadError):
    """The live project and its IFC export disagree about where the site is."""


def north_bearing_deg(north_radians: float) -> float:
    """The compass bearing of the *project* +Y axis, from Archicad's north angle.

    This is the same quantity ``SiteOrientation.true_north_bearing_deg`` holds,
    but stated for the project frame rather than for whatever frame an IFC
    export happened to write. The two are only equal when the export left the
    site placement unrotated -- see ``cross_check_georeferencing``.
    """
    return (math.degrees(north_radians) - NORTH_ANGLE_OFFSET_DEG) % 360.0


@dataclass(frozen=True)
class ProjectInfo:
    """Which project answered, so a result can be traced back to a file."""

    name: str
    path: str
    is_untitled: bool
    is_teamwork: bool

    def describe(self) -> str:
        if self.is_untitled:
            return "unsaved project (results cannot be traced to a file)"
        kind = "Teamwork" if self.is_teamwork else "solo"
        return f"{self.name or Path(self.path).name} [{kind}] {self.path}"


@dataclass(frozen=True)
class GeoLocation:
    """``GetGeoLocation``'s project location, in the units Archicad reported.

    ``north_radians`` is stored raw rather than converted on the way in, so
    the raw value stays printable next to the derived one. A human comparing
    the tool's output against Archicad's dialog needs both.
    """

    latitude_deg: float
    longitude_deg: float
    altitude_m: float
    north_radians: float

    @property
    def project_north_bearing_deg(self) -> float:
        """Compass bearing of the *project* +Y axis."""
        return north_bearing_deg(self.north_radians)

    @property
    def looks_like_a_city_preset(self) -> bool:
        """Whether these coordinates were picked from a list, not surveyed.

        Archicad ships city presets, and their coordinates are whole
        arcminutes -- Sydney is exactly -33 deg 52' 00", 151 deg 13' 00".
        A surveyed site landing on a whole arcminute in *both* coordinates is
        a 1-in-3600 coincidence, so two of them together is the tell that
        Project Location was never set.

        The effect on sun angles is small -- the Sydney preset is about 11 km
        from a site in the southern suburbs, which moves solar altitude by
        under a tenth of a degree -- so this is a provenance problem before it
        is a numerical one. It still matters: a study whose stated location is
        not the site is hard to defend, and the same neglected dialog is where
        a wrong north would come from.
        """
        return _is_whole_arcminute(self.latitude_deg) and _is_whole_arcminute(self.longitude_deg)

    def describe(self) -> str:
        return (
            f"lat {self.latitude_deg:.6f} lon {self.longitude_deg:.6f} "
            f"altitude {self.altitude_m:.3f} m, north {self.north_radians:.6f} rad "
            f"(project +Y at bearing {self.project_north_bearing_deg:.3f} deg)"
        )


#: How close to a whole arcminute counts as *being* one. Coordinates make a
#: round trip through degrees-minutes-seconds and back, so an exact preset
#: arrives as -33.866667 rather than -33.8666666..., which is 2e-5 arcmin off.
#: A tenth of a milli-arcminute is about 0.2 mm on the ground -- far tighter
#: than any real survey, and loose enough to absorb that round trip.
ARCMINUTE_EPSILON = 1e-4


def _is_whole_arcminute(degrees: float) -> bool:
    arcminutes = degrees * 60.0
    return abs(arcminutes - round(arcminutes)) < ARCMINUTE_EPSILON


@dataclass(frozen=True)
class ArchicadZone:
    """One Zone element, enough of it to join to a result and draw on a plan."""

    guid: str
    name: str
    number: str
    storey_index: int | None = None
    outline: tuple[tuple[float, float], ...] = ()
    """The zone's own 2D boundary, in project coordinates and metres.

    Its real shape, not a bounding box, so a fill drawn from it lands exactly
    on the apartment. Empty when Archicad reported no outline, which is a
    reason to skip that zone rather than approximate it.
    """
    hole_count: int = 0
    """Voids in the outline -- a lift core or light well the apartment wraps.

    ``CreateHatches`` takes a single contour and no holes, so a zone with one
    can only be drawn solid, covering the void. Counted rather than ignored so
    the run can say how many diagrams are affected instead of quietly
    colouring over a lift shaft.
    """
    arc_count: int = 0
    """Curved segments in the outline, likewise flattened to straight ones."""
    layer_index: int | None = None
    """Which Archicad layer the zone sits on.

    Kept because a zone's *layer* is what says whether it is an apartment. A
    project can hold zones for GFA calculation, for fire compartments and for
    apartments all at once, all named the same, and only the layer separates
    them.
    """

    @property
    def label(self) -> str:
        """What a human would call it on a drawing.

        Not unique: a project can have hundreds of zones all called ``RESI``
        with no number. Use ``disambiguated`` wherever a list of these is
        printed, or the report collapses distinct zones into one.
        """
        return f"{self.number} {self.name}".strip() or self.guid


def disambiguated(display_by_guid: Mapping[str, str]) -> dict[str, str]:
    """Make display names unique by adding a GUID fragment to the repeats.

    Zone names are not identifiers. One project has 1341 zones of which the
    eight sampled were all called ``RESI``, which made a report of seven
    distinct failures read as "56 failures over 1 elements" and a list of six
    zones with holes read as "RESI, RESI, RESI, RESI, RESI".

    Only the colliding names are tagged, so the common case stays readable.
    """
    counts = Counter(display_by_guid.values())
    return {
        guid: display if counts[display] == 1 else f"{display} [{guid[:8]}]"
        for guid, display in display_by_guid.items()
    }


def project_info(connection: ArchicadConnection) -> ProjectInfo:
    response = connection.run_tapir("GetProjectInfo")
    return ProjectInfo(
        name=str(response.get("projectName", "")),
        path=str(response.get("projectPath", "")),
        is_untitled=bool(response.get("isUntitled", False)),
        is_teamwork=bool(response.get("isTeamwork", False)),
    )


def read_geo_location(connection: ArchicadConnection) -> GeoLocation:
    response = connection.run_tapir("GetGeoLocation")
    location = response.get("projectLocation")
    if not isinstance(location, dict):
        raise ArchicadError(
            "GetGeoLocation returned no projectLocation. Set the project "
            "location in Options > Project Preferences > Project Location."
        )
    try:
        return GeoLocation(
            latitude_deg=float(location["latitude"]),
            longitude_deg=float(location["longitude"]),
            altitude_m=float(location["altitude"]),
            north_radians=float(location["north"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ArchicadError(
            f"GetGeoLocation returned an unreadable location: {location!r}"
        ) from exc


def _signed_delta(a: float, b: float) -> float:
    """The shortest signed angle from ``b`` to ``a``, in (-180, 180]."""
    return (a - b + 180.0) % 360.0 - 180.0


def cross_check_georeferencing(location: GeoLocation, model: IfcModel) -> None:
    """Fail unless the live project and its IFC export place the site alike.

    This is the guard that makes the whole adapter safe to trust. The IFC is
    the source of truth for every number the analysis uses; Archicad's own
    answer is here purely to catch the case where the export lost, rounded
    away or re-interpreted the georeferencing.

    Comparing north takes care, and an earlier version of this got it wrong.
    Archicad reports north in the **project** frame. An IFC export need not be
    in that frame: Archicad's "Survey Point" model position rotates the
    geometry through ``IfcSite``'s placement and then writes ``TrueNorth`` as
    ``(0,1)``, because its world coordinates really are north-aligned. Naively
    comparing Archicad's angle against ``TrueNorth`` therefore rejects a
    perfectly good export -- observed on a real AC26 project, where Archicad
    said 49.05 degrees and a correct IFC said 0.

    What is comparable is the *total* rotation from the project frame to true
    north, which is the sum of the two things the file records::

        project +Y bearing  ==  TrueNorth bearing  -  site placement rotation

    That holds for both model-position options without needing to know which
    was used, because whichever one carries the angle, the sum is the same.
    """
    problems: list[str] = []

    for label, live, exported in (
        ("latitude", location.latitude_deg, model.latitude_deg),
        ("longitude", location.longitude_deg, model.longitude_deg),
    ):
        if abs(live - exported) > COORDINATE_TOLERANCE_DEG:
            problems.append(
                f"{label}: Archicad says {live:.6f}, the IFC export says "
                f"{exported:.6f} (difference {abs(live - exported):.6f} deg)"
            )

    live_north = location.project_north_bearing_deg
    exported_north = (model.true_north_bearing_deg - model.site_rotation_deg) % 360.0
    delta = _signed_delta(live_north, exported_north)

    if abs(delta) > NORTH_TOLERANCE_DEG:
        hint = ""
        if abs(_signed_delta(-live_north, exported_north)) <= NORTH_TOLERANCE_DEG:
            hint = (
                "\n  The two agree once the sign is flipped, so the north convention "
                "in sun_study.archicad.read is wrong for this Archicad build. The "
                "analysis is unaffected -- it uses the IFC value -- but please report "
                "this so NORTH_ANGLE_OFFSET_DEG can be corrected."
            )
        problems.append(
            f"true north: Archicad puts project +Y at bearing {live_north:.3f} deg; "
            f"the IFC export puts it at {exported_north:.3f} deg "
            f"(TrueNorth {model.true_north_bearing_deg % 360.0:.3f} minus site "
            f"placement {model.site_rotation_deg:.3f}), a difference of "
            f"{abs(delta):.3f} deg{hint}"
        )

    if problems:
        raise GeoreferencingMismatchError(
            "The live Archicad project and its IFC export disagree about the "
            "site. Nothing computed from this pair can be trusted:\n  "
            + "\n  ".join(problems)
            + "\n  Re-export the IFC from the currently open project, and check "
            "that the IFC translator writes the site location and true north."
        )


def orientation_from(model: IfcModel, timezone: str) -> SiteOrientation:
    """The orientation the analysis will use, which is always the IFC's.

    A one-line function that exists to make the source of truth explicit at
    every call site, rather than leaving it implied by which object happened
    to be reached for.
    """
    return model.orientation(timezone)


def export_ifc(connection: ArchicadConnection, destination: str | Path) -> Path:
    """Save the open project to an IFC file and return the path.

    Uses whichever IFC translator is currently selected in Archicad. That is
    deliberate -- the office's translator settings decide what gets exported,
    and silently overriding them would produce a model that does not match
    what the same button produces by hand. ``docs/archicad.md`` lists the
    translator settings the analysis needs.
    """
    path = Path(destination).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)

    before = path.stat().st_mtime_ns if path.exists() else None

    connection.run_tapir(
        "IFCFileOperation",
        {"method": "save", "ifcFilePath": str(path), "fileType": "ifc"},
    )

    if not path.exists():
        raise ArchicadError(
            f"Archicad reported a successful IFC export but {path} does not "
            f"exist. Check that the path is writable from Archicad's process."
        )
    if before is not None and path.stat().st_mtime_ns == before:
        raise ArchicadError(
            f"Archicad reported a successful IFC export but {path} was not "
            f"modified. It is probably a stale file from an earlier run; "
            f"delete it and try again rather than analysing old geometry."
        )
    return path


def _element_guids(response: Any, command: str) -> list[str]:
    elements = response.get("elements") if isinstance(response, dict) else None
    if not isinstance(elements, list):
        raise ArchicadError(f"{command} returned no element list: {response!r}")

    guids: list[str] = []
    for item in elements:
        try:
            guids.append(str(item["elementId"]["guid"]))
        except (KeyError, TypeError) as exc:
            raise ArchicadError(f"{command} returned a malformed element: {item!r}") from exc
    return guids


def _outline(points: Any) -> tuple[tuple[float, float], ...]:
    """A zone's 2D boundary, dropping a repeated closing point.

    Archicad's polygons are closed, and ``CreateHatches`` says explicitly not
    to repeat the first point at the end. Passing it through would put a
    zero-length edge in every fill.
    """
    if not isinstance(points, list):
        return ()

    corners: list[tuple[float, float]] = []
    for point in points:
        if not isinstance(point, dict) or "x" not in point or "y" not in point:
            return ()
        corners.append((float(point["x"]), float(point["y"])))

    if len(corners) > 1 and corners[0] == corners[-1]:
        corners.pop()
    return tuple(corners) if len(corners) >= 3 else ()


def zones(connection: ArchicadConnection) -> tuple[ArchicadZone, ...]:
    """Every Zone in the project, with its name and number.

    Zones are the write-back target: an ADG table is a schedule of apartments,
    and in Archicad an apartment is a Zone. Ordered by number then name so a
    printed list is stable between runs.
    """
    found = connection.run_tapir("GetElementsByType", {"elementType": "Zone"})
    guids = _element_guids(found, "GetElementsByType")
    if not guids:
        return ()

    details = connection.run_tapir(
        "GetDetailsOfElements",
        {"elements": [{"elementId": {"guid": guid}} for guid in guids]},
    )
    rows = details.get("detailsOfElements") if isinstance(details, dict) else None
    if not isinstance(rows, list) or len(rows) != len(guids):
        raise ArchicadError(
            f"GetDetailsOfElements returned {len(rows) if isinstance(rows, list) else 'no'} "
            f"rows for {len(guids)} zones; the lists must be parallel."
        )

    result: list[ArchicadZone] = []
    for guid, row in zip(guids, rows, strict=True):
        if not isinstance(row, dict) or "error" in row:
            # A single unreadable zone is not a reason to abandon the run, but
            # it must not silently vanish from the schedule either.
            result.append(ArchicadZone(guid=guid, name="", number=""))
            continue
        detail = row.get("details") or {}
        floor = row.get("floorIndex")
        layer = row.get("layerIndex")
        result.append(
            ArchicadZone(
                guid=guid,
                name=str(detail.get("name", "")),
                number=str(detail.get("numberStr", "")),
                storey_index=int(floor) if isinstance(floor, (int, float)) else None,
                outline=_outline(detail.get("polygonOutline")),
                hole_count=len(detail.get("holes") or []),
                arc_count=len(detail.get("polygonArcs") or []),
                layer_index=int(layer) if isinstance(layer, (int, float)) else None,
            )
        )
    return tuple(sorted(result, key=lambda zone: (zone.number, zone.name, zone.guid)))


def layer_names(connection: ArchicadConnection) -> dict[int, str]:
    """Layer index -> name, for turning a zone's ``layer_index`` into words.

    A zone's layer is what says whether it is an apartment: a project can hold
    zones for GFA calculation, for fire compartments and for apartments all at
    once, all named the same. Reporting the index alone leaves a reader to go
    and look it up, which is the point at which they stop looking.
    """
    response = connection.run_tapir("GetAttributesByType", {"attributeType": "Layer"})
    listed = response.get("attributes") if isinstance(response, dict) else None
    return {
        int(attribute["index"]): str(attribute.get("name", ""))
        for attribute in (listed or [])
        if isinstance(attribute, dict) and isinstance(attribute.get("index"), (int, float))
    }


def elements_by_ifc_ids(connection: ArchicadConnection, ifc_ids: list[str]) -> dict[str, list[str]]:
    """IFC GlobalId -> the Archicad element GUIDs carrying it.

    The list is usually one long. It can be longer when the identifier is an
    *external* one belonging to merged IFC content placed more than once, and
    it can be empty when the element no longer exists. Callers decide what to
    do with each case; this function does not pretend either away.
    """
    if not ifc_ids:
        return {}

    response = connection.run_tapir("GetElementsByIFCIds", {"ifcIds": list(ifc_ids)})
    rows = response.get("elementsByIFCIds") if isinstance(response, dict) else None
    if not isinstance(rows, list):
        raise ArchicadError(f"GetElementsByIFCIds returned no mapping: {response!r}")

    mapping: dict[str, list[str]] = {ifc_id: [] for ifc_id in ifc_ids}
    for row in rows:
        if not isinstance(row, dict) or "ifcId" not in row:
            continue
        mapping[str(row["ifcId"])] = _element_guids(row, "GetElementsByIFCIds")
    return mapping


def ifc_ids_of_elements(connection: ArchicadConnection, guids: list[str]) -> dict[str, str]:
    """Archicad GUID -> IFC identifier, for the elements that have one.

    The inverse of ``elements_by_ifc_ids``, and the more useful direction for
    diagnostics: it says what a given Zone will be called in the export before
    the export happens.
    """
    if not guids:
        return {}

    response = connection.run_tapir(
        "GetIFCIdsOfElements",
        {"elements": [{"elementId": {"guid": guid}} for guid in guids]},
    )
    rows = response.get("elementIFCIds") if isinstance(response, dict) else None
    if not isinstance(rows, list):
        raise ArchicadError(f"GetIFCIdsOfElements returned no mapping: {response!r}")

    mapping: dict[str, str] = {}
    for row in rows:
        if not isinstance(row, dict) or "error" in row:
            continue
        identifier = row.get("ifcId") or row.get("externalIFCId")
        element = row.get("elementId") or {}
        if identifier and isinstance(element, dict) and element.get("guid"):
            mapping[str(element["guid"])] = str(identifier)
    return mapping


def classification_system_ids(connection: ArchicadConnection) -> list[str]:
    """Every classification system in the project.

    ``API.GetAllClassificationSystems`` is one of Archicad's own commands, not
    a Tapir one -- Tapir has no getter for systems, only for the classification
    *of an element within a named system*, and that command needs the system
    identifiers up front.
    """
    response = connection.run_official("API.GetAllClassificationSystems")
    systems = response.get("classificationSystems") if isinstance(response, dict) else None
    if not isinstance(systems, list):
        raise ArchicadError(f"GetAllClassificationSystems returned no systems: {response!r}")
    return [
        str(system["classificationSystemId"]["guid"])
        for system in systems
        if isinstance(system, dict) and (system.get("classificationSystemId") or {}).get("guid")
    ]


def _classification_item_guid(entry: Any) -> str | None:
    """The item GUID out of one ``classificationIds`` entry, either shape.

    Tapir's published schema wraps each entry in a ``classificationId`` key,
    but ``ClassificationCommands.cpp`` emits the inner object directly. Both
    are accepted rather than betting on which one a given build sends; see
    ``docs/archicad.md``.
    """
    if not isinstance(entry, dict) or "error" in entry:
        return None
    identifier = entry.get("classificationId") if "classificationId" in entry else entry
    if not isinstance(identifier, dict):
        return None
    item = identifier.get("classificationItemId")
    if isinstance(item, dict) and item.get("guid") and str(item["guid"]) != NULL_GUID:
        return str(item["guid"])
    return None


def classification_item_names(connection: ArchicadConnection) -> dict[str, str]:
    """Classification item GUID -> a readable ``System / ID Name``.

    Availability is a list of opaque GUIDs, which makes an availability
    problem unreadable: the error says a property could not be created and the
    request behind it is a row of identifiers. Resolving them turns that into
    "available for Unclassified only", which is an answer.

    ``API.GetAllClassificationsInSystem`` is one of Archicad's own commands and
    returns a tree, so children are walked too -- an element is normally
    classified against a leaf rather than a top-level heading.
    """
    listed = connection.run_official("API.GetAllClassificationSystems")
    systems = listed.get("classificationSystems") if isinstance(listed, dict) else None
    if not isinstance(systems, list):
        return {}

    names: dict[str, str] = {}

    def walk(entries: Any, system: str) -> None:
        if not isinstance(entries, list):
            return
        for entry in entries:
            item = (entry or {}).get("classificationItem") if isinstance(entry, dict) else None
            if not isinstance(item, dict):
                continue
            identifier = (item.get("classificationItemId") or {}).get("guid")
            if identifier:
                label = " ".join(str(item.get(key, "")) for key in ("id", "name")).strip()
                names[str(identifier)] = f"{system} / {label or '(unnamed)'}"
            walk(item.get("children"), system)

    for system in systems:
        if not isinstance(system, dict):
            continue
        identifier = (system.get("classificationSystemId") or {}).get("guid")
        if not identifier:
            continue
        response = connection.run_official(
            "API.GetAllClassificationsInSystem",
            {"classificationSystemId": {"guid": identifier}},
        )
        walk(
            response.get("classificationItems") if isinstance(response, dict) else None,
            str(system.get("name", "?")),
        )
    return names


def classification_items_of(
    connection: ArchicadConnection, guids: list[str]
) -> dict[str, set[str]]:
    """Archicad GUID -> the classification item GUIDs it carries.

    A custom property in Archicad is available to an element through its
    *classification*, not its type, so ``write.init_properties`` needs this to
    know which classification items the new properties must be attached to.
    Elements with no classification in any system are simply absent from the
    result, which is what lets the caller name them in an error rather than
    failing obscurely later when a value will not stick.
    """
    if not guids:
        return {}

    systems = classification_system_ids(connection)
    if not systems:
        raise ArchicadError(
            "The project has no classification systems, so a custom property "
            "cannot be made available to anything. Load a classification "
            "system (Options > Classification Manager) and classify the Zones."
        )

    response = connection.run_tapir(
        "GetClassificationsOfElements",
        {
            "elements": [{"elementId": {"guid": guid}} for guid in guids],
            "classificationSystemIds": [
                {"classificationSystemId": {"guid": system}} for system in systems
            ],
        },
    )
    rows = response.get("elementClassifications") if isinstance(response, dict) else None
    if not isinstance(rows, list):
        raise ArchicadError(f"GetClassificationsOfElements returned no result: {response!r}")

    mapping: dict[str, set[str]] = {}
    for guid, row in zip(guids, rows, strict=False):
        if not isinstance(row, dict) or "error" in row:
            continue
        items = {
            item
            for item in (_classification_item_guid(e) for e in row.get("classificationIds") or [])
            if item is not None
        }
        if items:
            mapping[guid] = items
    return mapping


def describe_connection(connection: ArchicadConnection) -> str:
    """The banner for ``sun-study archicad-info``: what answered, and where."""
    lines = [connection.describe()]
    try:
        lines.append(f"  project: {project_info(connection).describe()}")
    except CommandFailedError as exc:  # pragma: no cover - needs a live Archicad
        lines.append(f"  project: unavailable ({exc})")
    try:
        location = read_geo_location(connection)
        lines.append(f"  location: {location.describe()}")
        if location.looks_like_a_city_preset:
            lines.append(
                "  WARNING: both coordinates are whole arcminutes, which is what a "
                "city preset looks like and what a surveyed site almost never does. "
                "Project Location has probably never been set. Sun angles barely "
                "move, but a study whose stated location is not the site is hard to "
                "defend -- and the same dialog holds the north angle."
            )
    except ArchicadError as exc:  # pragma: no cover - needs a live Archicad
        lines.append(f"  location: unavailable ({exc})")
    return "\n".join(lines)


@dataclass(frozen=True)
class LibraryObject:
    """A placed GDL object, with enough of it to find and identify a room.

    In a project where Zones are placed per *unit*, the rooms inside a unit
    exist only as library objects -- a "Room Name and Size Label" carrying the
    room's name and size. That object is the only thing in the model that says
    which part of an apartment is the living room, which is the distinction
    ADG 4A-1 turns on.
    """

    guid: str
    library_part: str
    """The library part's name, e.g. ``Room Name and Size Label 19``."""
    origin: tuple[float, float, float]
    """Placement point in project coordinates, metres."""
    storey_index: int | None = None
    layer_index: int | None = None
    parameters: tuple[tuple[str, str], ...] = ()
    """GDL parameters as ``(name, value)``, values stringified for printing."""

    def parameter(self, name: str) -> str | None:
        wanted = name.casefold()
        for key, value in self.parameters:
            if key.casefold() == wanted:
                return value
        return None


def library_objects(
    connection: ArchicadConnection,
    *,
    with_parameters: int = 0,
) -> tuple[LibraryObject, ...]:
    """Every placed Object, with its library part and placement point.

    ``with_parameters`` reads GDL parameters for that many objects. It is
    capped rather than automatic because a library part can carry hundreds of
    parameters and a project holds thousands of objects -- pulling all of both
    is a very large response to answer a question about one parameter.
    """
    found = connection.run_tapir("GetElementsByType", {"elementType": "Object"})
    guids = _element_guids(found, "GetElementsByType")
    if not guids:
        return ()

    details = connection.run_tapir(
        "GetDetailsOfElements",
        {"elements": [{"elementId": {"guid": guid}} for guid in guids]},
    )
    rows = details.get("detailsOfElements") if isinstance(details, dict) else None
    if not isinstance(rows, list) or len(rows) != len(guids):
        raise ArchicadError(
            f"GetDetailsOfElements returned {len(rows) if isinstance(rows, list) else 'no'} "
            f"rows for {len(guids)} objects; the lists must be parallel."
        )

    objects: list[LibraryObject] = []
    for guid, row in zip(guids, rows, strict=True):
        if not isinstance(row, dict) or "error" in row:
            continue
        detail = row.get("details") or {}
        origin = detail.get("origin") or {}
        floor = row.get("floorIndex")
        layer = row.get("layerIndex")
        objects.append(
            LibraryObject(
                guid=guid,
                library_part=str((detail.get("libPart") or {}).get("name", "")),
                origin=(
                    float(origin.get("x", 0.0)),
                    float(origin.get("y", 0.0)),
                    float(origin.get("z", 0.0)),
                ),
                storey_index=int(floor) if isinstance(floor, (int, float)) else None,
                layer_index=int(layer) if isinstance(layer, (int, float)) else None,
            )
        )

    if with_parameters > 0:
        objects = _with_gdl_parameters(connection, objects, limit=with_parameters)
    return tuple(objects)


def gdl_parameters(
    connection: ArchicadConnection, objects: Sequence[LibraryObject]
) -> list[LibraryObject]:
    """The same objects, with their GDL parameters filled in.

    Separate from ``library_objects`` because a library part can carry
    hundreds of parameters and a project holds thousands of objects: reading
    both together is a very large response to answer a question about one
    parameter.
    """
    return _with_gdl_parameters(connection, list(objects), limit=len(objects))


def _with_gdl_parameters(
    connection: ArchicadConnection, objects: list[LibraryObject], *, limit: int
) -> list[LibraryObject]:
    """Fill in GDL parameters for the first ``limit`` objects."""
    wanted = objects[:limit]
    if not wanted:
        return objects
    response = connection.run_tapir(
        "GetGDLParametersOfElements",
        {"elements": [{"elementId": {"guid": item.guid}} for item in wanted]},
    )
    lists = response.get("gdlParametersOfElements") if isinstance(response, dict) else None
    if not isinstance(lists, list):
        return objects

    filled: list[LibraryObject] = []
    for item, entry in zip(wanted, lists, strict=False):
        parameters = (entry or {}).get("parameters") if isinstance(entry, dict) else None
        pairs = tuple(
            (str(parameter.get("name", "")), _short(parameter.get("value")))
            for parameter in (parameters or [])
            if isinstance(parameter, dict)
        )
        filled.append(replace(item, parameters=pairs))
    return filled + objects[limit:]


def _short(value: Any) -> str:
    """A GDL value as a printable string, truncated.

    Array parameters can be large and none of them are what a room name lives
    in, so they are shown as a shape rather than as a wall of numbers.
    """
    if isinstance(value, list):
        return f"<array of {len(value)}>"
    text = str(value)
    return text if len(text) <= 60 else text[:57] + "..."
