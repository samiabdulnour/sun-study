"""Writing sunlight results back into an Archicad project as Zone properties.

The point of the whole tool is here. A compliance table typed into a Word
document is a snapshot that is wrong the moment the massing moves; the same
numbers living on the Zones are a native Archicad schedule that updates when
the study is re-run, and that a reviewer can interrogate element by element.

What gets written
-----------------
One property group, ``Sun Study``, holding one property per column of the ADG
table -- see ``APARTMENT_PROPERTIES``. Creating them is a separate, explicit
step (``sun-study init-properties``) rather than something a results run does
behind the user's back, because adding property definitions changes the
project file for everyone on a Teamwork job.

Two decisions worth knowing about
---------------------------------
**Numbers are safe; booleans are not.** ``SetPropertyValuesOfElements`` takes a
*display string* and Archicad parses it. Tapir pins the conversion units
itself -- ``PropertyConversionUtils`` in ``PropertyCommands.cpp`` hard-codes a
``.`` decimal delimiter, metres, square metres and decimal degrees -- so a
``number`` written as ``"2.35"`` is unambiguous whatever the project's unit
preferences say. Nothing in the sources states what string a ``boolean``
property parses from, so the pass/fail columns are ``string`` properties
holding ``Yes``/``No``. A string set from a string cannot be misparsed. See
decision D21.

**An empty result is a failure, not a success.** Tapir builds one result slot
per requested value and fills it in a loop over the properties Archicad
actually returned for that element. A property that is not *available* for the
element -- the usual cause being an unclassified Zone -- is never returned, so
its slot stays an empty object rather than becoming an error. Treating ``{}``
as success would silently report a full write of a project where nothing
landed.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any

from sun_study.archicad.connection import ArchicadConnection, ArchicadError
from sun_study.archicad.read import NULL_GUID, elements_by_ifc_ids
from sun_study.rules.assessment import BuildingAssessment

__all__ = [
    "APARTMENT_PROPERTIES",
    "PROPERTY_GROUP_NAME",
    "PropertyDetail",
    "PropertySpec",
    "WriteReport",
    "all_properties",
    "existing_properties",
    "init_properties",
    "write_assessment",
]

PROPERTY_GROUP_NAME = "Sun Study"


@dataclass(frozen=True)
class PropertySpec:
    """One column of the schedule this tool creates."""

    name: str
    data_type: str
    """A ``PropertyDataType`` from Tapir's common schema."""
    description: str


APARTMENT_PROPERTIES: tuple[PropertySpec, ...] = (
    PropertySpec(
        "Living Room Sunlight (h)",
        "number",
        "Hours of direct sunlight reaching the living room windows, "
        "area-weighted across the sampled window area.",
    ),
    PropertySpec(
        "Private Open Space Sunlight (h)",
        "number",
        "Hours of direct sunlight reaching the private open space. "
        "Left undefined where the apartment has none.",
    ),
    PropertySpec(
        "Governing Sunlight (h)",
        "number",
        "The figure the verdict was taken on, after the ruleset's continuity "
        "and living-room/open-space readings were applied.",
    ),
    PropertySpec(
        "Meets Minimum",
        "string",
        "Yes when this apartment reaches the ruleset's minimum sunlight hours.",
    ),
    PropertySpec(
        "No Direct Sunlight",
        "string",
        "Yes when this apartment receives no direct sunlight at all during the assessment window.",
    ),
    PropertySpec(
        "Counted in Compliance",
        "string",
        "No when the ruleset excludes this apartment from the compliance "
        "denominator. Excluded apartments still carry their measured hours.",
    ),
    PropertySpec(
        "Sun Study Note",
        "string",
        "Why an apartment was excluded or flagged, where the ruleset gave a reason.",
    ),
    PropertySpec(
        "Sun Study Ruleset",
        "string",
        "The ruleset and version the verdict came from, and the area variant.",
    ),
    PropertySpec(
        "Sun Study Run",
        "string",
        "When these values were written. A value older than the last massing "
        "change is stale and must not be quoted.",
    ),
)


@dataclass(frozen=True)
class WriteReport:
    """What actually landed, in enough detail to catch a partial write."""

    values_written: int
    values_skipped: int
    """Undefined measurements, deliberately not written rather than zeroed."""
    zones_written: tuple[str, ...]
    zones_unmatched: tuple[str, ...]
    """Apartments in the results with no element in the project."""
    zones_ambiguous: tuple[str, ...]
    """Apartments matching more than one element, so the target is unclear."""
    failures: tuple[str, ...]

    @property
    def complete(self) -> bool:
        return not (self.zones_unmatched or self.zones_ambiguous or self.failures)

    def describe(self) -> str:
        lines = [
            f"wrote {self.values_written} property values across "
            f"{len(self.zones_written)} zones"
            + (f", {self.values_skipped} left undefined" if self.values_skipped else "")
        ]
        if self.zones_unmatched:
            lines.append(
                f"  {len(self.zones_unmatched)} apartments had no matching element: "
                + ", ".join(self.zones_unmatched[:5])
                + (" ..." if len(self.zones_unmatched) > 5 else "")
            )
        if self.zones_ambiguous:
            lines.append(
                f"  {len(self.zones_ambiguous)} apartments matched several elements "
                f"and were skipped: " + ", ".join(self.zones_ambiguous[:5])
            )
        for failure in self.failures[:10]:
            lines.append(f"  FAILED {failure}")
        if len(self.failures) > 10:
            lines.append(f"  ... and {len(self.failures) - 10} more failures")
        return "\n".join(lines)


@dataclass(frozen=True)
class PropertyDetail:
    """One property the project already defines."""

    identifier: str
    group: str
    name: str
    kind: str
    """``StaticBuiltIn``, ``DynamicBuiltIn`` or ``Custom``."""
    editable: bool
    value_type: str = ""
    """``Integer``, ``Real``, ``String``, ``Boolean``, ``Guid`` or ``Undefined``."""
    collection_type: str = ""
    """``Single``, ``List``, ``SingleChoiceEnumeration``, ``MultipleChoiceEnumeration``."""
    expression_based: bool = False
    """Computed from an expression, and therefore not writable."""

    @property
    def writable(self) -> bool:
        """Whether this tool could put a value here.

        An expression-based property derives its value and refuses to be set;
        so does one Archicad marks read-only. Both look like ordinary
        properties in a listing, and discovering the difference by attempting
        a write across 91 apartments is the expensive way to find out.
        """
        return self.editable and not self.expression_based

    def describe(self) -> str:
        parts = [self.value_type or "?", self.collection_type or "?"]
        if self.expression_based:
            parts.append("expression")
        if not self.editable:
            parts.append("read-only")
        return f"{self.name}  [{', '.join(parts)}]"


def all_properties(connection: ArchicadConnection) -> tuple[PropertyDetail, ...]:
    """Every property in the project, built-in and custom.

    ``GetAllProperties`` is the only way to look a property up by name: Tapir
    has no getter for property groups, so an empty group is invisible and
    ``CreatePropertyGroups`` has to be attempted rather than checked for.

    Listing the lot -- not just this tool's own group -- is what lets a
    practice's *existing* solar-access property be found and written to,
    rather than a parallel one being created beside it.
    """
    response = connection.run_tapir("GetAllProperties")
    entries = response.get("properties") if isinstance(response, dict) else None
    if not isinstance(entries, list):
        raise ArchicadError(f"GetAllProperties returned no property list: {response!r}")

    found: list[PropertyDetail] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        identifier = (entry.get("propertyId") or {}).get("guid")
        if not identifier:
            continue
        found.append(
            PropertyDetail(
                identifier=str(identifier),
                group=str(entry.get("propertyGroupName", "")),
                name=str(entry.get("propertyName", "")),
                kind=str(entry.get("propertyType", "")),
                editable=bool(entry.get("propertyIsEditable", False)),
                value_type=str(entry.get("propertyValueType", "")),
                collection_type=str(entry.get("propertyCollectionType", "")),
                expression_based=bool(entry.get("isExpressionBased", False)),
            )
        )
    return tuple(found)


def existing_properties(connection: ArchicadConnection) -> dict[str, str]:
    """Property name -> identifier, for the properties in this tool's group."""
    return {
        entry.name: entry.identifier
        for entry in all_properties(connection)
        if entry.group == PROPERTY_GROUP_NAME
    }


def _availability_items(classifications: dict[str, set[str]]) -> list[str]:
    items: set[str] = set()
    for values in classifications.values():
        items.update(value for value in values if value != NULL_GUID)
    return sorted(items)


def init_properties(
    connection: ArchicadConnection,
    classifications: dict[str, set[str]],
) -> dict[str, str]:
    """Create this tool's property group and definitions, idempotently.

    ``classifications`` maps element GUID to the classification items it
    carries -- ``read.classification_items_of`` produces it. Archicad makes a
    custom property available to an element through its *classification*, not
    its type, so without this the properties would be created and then refuse
    every value written to them.

    Safe to run repeatedly: properties that already exist are left alone,
    including their availability. Widening availability afterwards is
    ``UpdatePropertyDefinitions``, which this deliberately does not do -- it
    would silently change a definition somebody else may have edited.
    """
    availability = _availability_items(classifications)
    if not availability:
        raise ArchicadError(
            "None of the target elements carry a classification, so a custom "
            "property cannot be made available to them. Classify the Zones "
            "(select them, then Classification and Properties > Classification) "
            "and run this again."
        )

    already = existing_properties(connection)
    missing = [spec for spec in APARTMENT_PROPERTIES if spec.name not in already]
    if not missing:
        return already

    # The group may already exist, and Tapir reports that as a per-item error
    # rather than a failed command. Attempting it unconditionally and ignoring
    # the outcome is correct: the definitions below name the group by name, so
    # either the create or the pre-existing group satisfies them.
    connection.run_tapir(
        "CreatePropertyGroups",
        {
            "propertyGroups": [
                {
                    "propertyGroup": {
                        "name": PROPERTY_GROUP_NAME,
                        "description": (
                            "Direct sunlight hours and ADG assessment written by "
                            "sun-study. Values are only as current as the run that "
                            "wrote them -- see Sun Study Run."
                        ),
                    }
                }
            ]
        },
    )

    response = connection.run_tapir(
        "CreatePropertyDefinitions",
        {
            "propertyDefinitions": [
                {
                    "propertyDefinition": {
                        "name": spec.name,
                        "description": spec.description,
                        "type": spec.data_type,
                        "isEditable": True,
                        "availability": [
                            {"classificationItemId": {"guid": item}} for item in availability
                        ],
                        "group": {"name": PROPERTY_GROUP_NAME},
                    }
                }
                for spec in missing
            ]
        },
    )

    created = response.get("propertyIds") if isinstance(response, dict) else None
    if not isinstance(created, list) or len(created) != len(missing):
        count = len(created) if isinstance(created, list) else "no"
        raise ArchicadError(
            f"CreatePropertyDefinitions returned {count} results for "
            f"{len(missing)} definitions; the lists must be parallel."
        )

    problems = [
        f"{spec.name}: {(item.get('error') or {}).get('message', 'unknown error')}"
        for spec, item in zip(missing, created, strict=True)
        if isinstance(item, dict) and "error" in item
    ]
    if problems:
        raise ArchicadError(
            "Archicad refused to create some property definitions:\n  " + "\n  ".join(problems)
        )

    return existing_properties(connection)


def _hours(minutes: float | None) -> str | None:
    """Minutes as a display string in hours, or ``None`` to leave undefined.

    Two decimals is 36 seconds, an order of magnitude finer than the timestep
    the figure was integrated at, so nothing meaningful is lost and the column
    stays readable.
    """
    return None if minutes is None else f"{minutes / 60.0:.2f}"


def _yes_no(value: bool) -> str:
    return "Yes" if value else "No"


def _values_for(
    assessment: BuildingAssessment,
    apartment: Any,
    run_stamp: str,
) -> dict[str, str | None]:
    return {
        "Living Room Sunlight (h)": _hours(apartment.living_room_minutes),
        "Private Open Space Sunlight (h)": _hours(apartment.open_space_minutes),
        "Governing Sunlight (h)": _hours(apartment.governing_minutes),
        "Meets Minimum": _yes_no(apartment.meets_minimum),
        "No Direct Sunlight": _yes_no(apartment.receives_no_sunlight),
        "Counted in Compliance": _yes_no(apartment.counted),
        "Sun Study Note": apartment.note,
        "Sun Study Ruleset": (
            f"{assessment.ruleset_identifier} / {assessment.area_key} "
            f"({assessment.minimum_minutes / 60:g}h {assessment.continuity})"
        ),
        "Sun Study Run": run_stamp,
    }


def _execution_problem(result: Any, label: str) -> str | None:
    """The reason one value did not land, or ``None`` when it did.

    An empty object is the interesting case: Tapir leaves a result slot
    untouched when Archicad never returned the property for that element,
    which is what happens when the property is not available for the element's
    classification.
    """
    if isinstance(result, dict) and result.get("success") is True:
        return None
    if isinstance(result, dict) and result.get("success") is False:
        error = result.get("error") or {}
        return f"{label}: {error.get('message', 'no message')} (code {error.get('code', 'none')})"
    if isinstance(result, dict) and not result:
        return (
            f"{label}: Archicad did not report a result, which means the property "
            f"is not available for this element. Re-run init-properties after "
            f"classifying the zone."
        )
    return f"{label}: unrecognised execution result {result!r}"


def write_assessment(
    connection: ArchicadConnection,
    assessment: BuildingAssessment,
    *,
    run_stamp: str | None = None,
) -> WriteReport:
    """Write one building assessment onto the project's Zones.

    Apartments are matched to elements by the IFC GlobalId they were analysed
    under, through ``GetElementsByIFCIds``. An apartment that matches nothing,
    or that matches several elements, is reported rather than guessed at: both
    mean the export and the project have drifted apart, and writing a number
    onto the wrong Zone is worse than writing none.
    """
    stamp = run_stamp or dt.datetime.now(dt.UTC).strftime("%Y-%m-%d %H:%M UTC")
    properties = existing_properties(connection)
    unknown = [spec.name for spec in APARTMENT_PROPERTIES if spec.name not in properties]
    if unknown:
        raise ArchicadError(
            "These properties do not exist in the project yet: "
            + ", ".join(unknown)
            + ". Run 'sun-study init-properties' first."
        )

    apartment_ids = [apartment.apartment_id for apartment in assessment.apartments]
    matches = elements_by_ifc_ids(connection, apartment_ids)

    payload: list[dict[str, Any]] = []
    labels: list[str] = []
    written_zones: list[str] = []
    unmatched: list[str] = []
    ambiguous: list[str] = []
    skipped = 0

    for apartment in assessment.apartments:
        guids = matches.get(apartment.apartment_id, [])
        if not guids:
            unmatched.append(apartment.apartment_name or apartment.apartment_id)
            continue
        if len(guids) > 1:
            ambiguous.append(apartment.apartment_name or apartment.apartment_id)
            continue

        guid = guids[0]
        written_zones.append(guid)
        for name, value in _values_for(assessment, apartment, stamp).items():
            if value is None:
                skipped += 1
                continue
            payload.append(
                {
                    "elementId": {"guid": guid},
                    "propertyId": {"guid": properties[name]},
                    "propertyValue": {"value": value},
                }
            )
            labels.append(f"{apartment.apartment_name or guid} / {name}")

    if not payload:
        return WriteReport(
            values_written=0,
            values_skipped=skipped,
            zones_written=(),
            zones_unmatched=tuple(unmatched),
            zones_ambiguous=tuple(ambiguous),
            failures=(),
        )

    response = connection.run_tapir(
        "SetPropertyValuesOfElements", {"elementPropertyValues": payload}
    )
    results = response.get("executionResults") if isinstance(response, dict) else None
    if not isinstance(results, list) or len(results) != len(payload):
        raise ArchicadError(
            f"SetPropertyValuesOfElements returned "
            f"{len(results) if isinstance(results, list) else 'no'} results for "
            f"{len(payload)} values; the lists must be parallel, so it is not "
            f"possible to say which values landed."
        )

    failures = [
        problem
        for problem in (
            _execution_problem(result, label) for result, label in zip(results, labels, strict=True)
        )
        if problem is not None
    ]

    return WriteReport(
        values_written=len(payload) - len(failures),
        values_skipped=skipped,
        zones_written=tuple(written_zones),
        zones_unmatched=tuple(unmatched),
        zones_ambiguous=tuple(ambiguous),
        failures=tuple(failures),
    )
