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
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sun_study.archicad.connection import ArchicadConnection, ArchicadError, CommandFailedError
from sun_study.core.orientation import SiteOrientation
from sun_study.ingest.ifc import IfcModel

__all__ = [
    "ASSUMED_NORTH_SENSE",
    "ArchicadZone",
    "GeoLocation",
    "GeoreferencingMismatchError",
    "ProjectInfo",
    "cross_check_georeferencing",
    "elements_by_ifc_ids",
    "export_ifc",
    "north_bearing_deg",
    "project_info",
    "read_geo_location",
    "zones",
]

#: How ``GetGeoLocation``'s ``north`` angle is read as a compass bearing.
#:
#: **This is an assumption, not a verified fact.** Tapir returns
#: ``API_PlaceInfo::north`` verbatim and documents it only as "north direction
#: in radians"; nothing in the add-on sources, its schemas or its Grasshopper
#: components states the sense. ``+1`` reads the angle as already clockwise
#: from true north, so it is a bearing after converting to degrees.
#:
#: Nothing in this tool depends on the guess being right. North comes from the
#: IFC export, and this value is used only by ``cross_check_georeferencing``,
#: which fails loudly on disagreement. If the sense is in fact the opposite,
#: the cross-check on a project with a non-zero north will fail by exactly
#: twice the angle, which the error message says to look for.
ASSUMED_NORTH_SENSE = 1.0

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
    """Archicad's north angle as a compass bearing, under ``ASSUMED_NORTH_SENSE``.

    Read the constant's docstring before trusting the number: this is the
    unverified part of the adapter, and it is deliberately used only for a
    cross-check.
    """
    return (ASSUMED_NORTH_SENSE * math.degrees(north_radians)) % 360.0


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

    ``north_radians`` is stored raw rather than converted on the way in. The
    conversion is the uncertain step, so it stays visible at the point of use.
    """

    latitude_deg: float
    longitude_deg: float
    altitude_m: float
    north_radians: float

    @property
    def assumed_north_bearing_deg(self) -> float:
        return north_bearing_deg(self.north_radians)

    def describe(self) -> str:
        return (
            f"lat {self.latitude_deg:.6f} lon {self.longitude_deg:.6f} "
            f"altitude {self.altitude_m:.3f} m, north {self.north_radians:.6f} rad "
            f"({self.assumed_north_bearing_deg:.3f} deg as a bearing, assumed sense)"
        )


@dataclass(frozen=True)
class ArchicadZone:
    """One Zone element, enough of it to join to a result and write back."""

    guid: str
    name: str
    number: str
    storey_index: int | None = None

    @property
    def label(self) -> str:
        """What a human would call it on a drawing."""
        return f"{self.number} {self.name}".strip() or self.guid


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


def cross_check_georeferencing(location: GeoLocation, model: IfcModel) -> None:
    """Fail unless the live project and its IFC export place the site alike.

    This is the guard that makes the whole adapter safe to trust. The IFC is
    the source of truth for every number the analysis uses; Archicad's own
    answer is here purely to catch the case where the export lost, rounded
    away or re-interpreted the georeferencing.
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

    live_north = location.assumed_north_bearing_deg
    exported_north = model.true_north_bearing_deg % 360.0
    delta = abs((live_north - exported_north + 180.0) % 360.0 - 180.0)
    if delta > NORTH_TOLERANCE_DEG:
        hint = ""
        mirrored = abs((-live_north - exported_north + 180.0) % 360.0 - 180.0)
        if mirrored <= NORTH_TOLERANCE_DEG:
            hint = (
                "\n  The two agree once the sign is flipped, so ASSUMED_NORTH_SENSE "
                "in sun_study.archicad.read is wrong. The analysis is unaffected -- "
                "it uses the IFC value -- but please report this so the constant "
                "can be corrected."
            )
        problems.append(
            f"true north: Archicad says {live_north:.3f} deg as a bearing, the "
            f"IFC export says {exported_north:.3f} deg (difference {delta:.3f} deg)"
            f"{hint}"
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
        result.append(
            ArchicadZone(
                guid=guid,
                name=str(detail.get("name", "")),
                number=str(detail.get("numberStr", "")),
                storey_index=int(floor) if isinstance(floor, (int, float)) else None,
            )
        )
    return tuple(sorted(result, key=lambda zone: (zone.number, zone.name, zone.guid)))


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
        lines.append(f"  location: {read_geo_location(connection).describe()}")
    except ArchicadError as exc:  # pragma: no cover - needs a live Archicad
        lines.append(f"  location: unavailable ({exc})")
    return "\n".join(lines)
