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
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sun_study.archicad.connection import ArchicadConnection, ArchicadError
from sun_study.archicad.read import NULL_GUID, elements_by_ifc_ids
from sun_study.rules.assessment import BuildingAssessment

__all__ = [
    "APARTMENT_PROPERTIES",
    "PROPERTY_GROUP_NAME",
    "ApartmentMatch",
    "PropertyDetail",
    "PropertySpec",
    "WriteReport",
    "all_properties",
    "default_property_value",
    "ensure_property_group",
    "enum_values",
    "existing_properties",
    "init_properties",
    "match_apartments",
    "property_groups",
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


def enum_values(
    connection: ArchicadConnection, identifiers: Sequence[str]
) -> dict[str, tuple[str, ...]]:
    """Property identifier -> the display strings its enumeration accepts.

    Needed before anything can be written to an existing enumerated property.
    ``SetPropertyValuesOfElements`` takes a display string, and for an
    enumeration Archicad matches it against the defined values -- so ``"Yes"``
    written to a property whose values are ``Y`` and ``N`` does not land. It
    also does not error in a way a careless caller would notice, which is why
    the values are read rather than assumed.

    ``API.GetDetailsOfProperties`` is one of Archicad's own commands, not a
    Tapir one; ``GetAllProperties`` reports that a property *is* an enumeration
    but not what it may contain. Results are matched on the identifier they
    come back with rather than on position, so a partial or reordered response
    cannot quietly attach one property's values to another.
    """
    if not identifiers:
        return {}

    response = connection.run_official(
        "API.GetDetailsOfProperties",
        {"properties": [{"propertyId": {"guid": guid}} for guid in identifiers]},
    )
    rows = response.get("propertyDefinitions") if isinstance(response, dict) else None
    if not isinstance(rows, list):
        raise ArchicadError(f"GetDetailsOfProperties returned no definitions: {response!r}")

    found: dict[str, tuple[str, ...]] = {}
    for row in rows:
        definition = (row or {}).get("propertyDefinition") if isinstance(row, dict) else None
        if not isinstance(definition, dict):
            continue
        identifier = (definition.get("propertyId") or {}).get("guid")
        if not identifier:
            continue
        values = [
            str((item or {}).get("enumValue", {}).get("displayValue", ""))
            for item in definition.get("possibleEnumValues") or []
        ]
        found[str(identifier)] = tuple(value for value in values if value)
    return found


def existing_properties(connection: ArchicadConnection) -> dict[str, str]:
    """Property name -> identifier, for the properties in this tool's group."""
    return {
        entry.name: entry.identifier
        for entry in all_properties(connection)
        if entry.group == PROPERTY_GROUP_NAME
    }


def default_property_value(data_type: str) -> dict[str, Any]:
    """A property definition's starting value, which Archicad insists on.

    Omitting ``defaultValue`` looks harmless -- the schema does not require it
    -- but Tapir then sets the variant's status to ``API_VariantStatusNull``
    and ``ACAPI_Property_CreatePropertyDefinition`` rejects the whole
    definition with ``APIERR_BADVALUE`` (-2130313104, 0x81060070).

    That was found the hard way: nine definitions refused on a real project
    with one identical code, and a probe that varied the name, the type, the
    availability and ``isEditable`` failed every time, because the missing
    default was the one thing all of them shared.

    An empty string and a zero are the neutral starting points. They are never
    read -- every property is overwritten with a measured value or left
    deliberately undefined -- so the choice only has to be valid, not
    meaningful.
    """
    blank: dict[str, Any] = {"type": data_type, "status": "normal", "value": ""}
    if data_type in {"number", "length", "area", "volume", "angle"}:
        blank["value"] = 0.0
    elif data_type == "integer":
        blank["value"] = 0
    elif data_type == "boolean":
        blank["value"] = False
    return {"basicDefaultValue": blank}


def _availability_items(classifications: dict[str, set[str]]) -> list[str]:
    items: set[str] = set()
    for values in classifications.values():
        items.update(value for value in values if value != NULL_GUID)
    return sorted(items)


def property_groups(connection: ArchicadConnection) -> dict[str, str]:
    """Group name -> identifier, for the project's user-defined groups.

    ``GetAllProperties`` cannot answer this: it reports the group *name* of
    each property, so a group holding no properties yet is invisible. That
    gap is why an earlier version created the group blind and ignored the
    outcome, which on a re-run makes a second group with the same name and
    leaves Archicad's by-name lookup picking whichever it finds first.

    ``API.GetAllPropertyGroupIds`` and ``API.GetPropertyGroups`` are two of
    Archicad's own commands and answer it directly, so groups can be addressed
    by identifier and created only when genuinely absent.
    """
    listed = connection.run_official("API.GetAllPropertyGroupIds", {"propertyType": "UserDefined"})
    identifiers = listed.get("propertyGroupIds") if isinstance(listed, dict) else None
    if not isinstance(identifiers, list) or not identifiers:
        return {}

    response = connection.run_official("API.GetPropertyGroups", {"propertyGroupIds": identifiers})
    groups = response.get("propertyGroups") if isinstance(response, dict) else None
    if not isinstance(groups, list):
        raise ArchicadError(f"GetPropertyGroups returned no groups: {response!r}")

    found: dict[str, str] = {}
    for entry in groups:
        group = (entry or {}).get("propertyGroup") if isinstance(entry, dict) else None
        if not isinstance(group, dict):
            continue
        identifier = (group.get("propertyGroupId") or {}).get("guid")
        name = group.get("name")
        if identifier and name:
            found[str(name)] = str(identifier)
    return found


def ensure_property_group(connection: ArchicadConnection) -> str:
    """This tool's property group, created only if it is genuinely missing."""
    existing = property_groups(connection).get(PROPERTY_GROUP_NAME)
    if existing is not None:
        return existing

    response = connection.run_tapir(
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
    created = response.get("propertyGroupIds") if isinstance(response, dict) else None
    if isinstance(created, list) and created and isinstance(created[0], dict):
        if "error" in created[0]:
            error = created[0]["error"] or {}
            raise ArchicadError(
                f"Could not create the {PROPERTY_GROUP_NAME!r} property group: "
                f"{error.get('message', 'no message')} (code {error.get('code', 'none')})"
            )
        identifier = (created[0].get("propertyGroupId") or {}).get("guid")
        if identifier:
            return str(identifier)

    resolved = property_groups(connection).get(PROPERTY_GROUP_NAME)
    if resolved is None:
        raise ArchicadError(
            f"Created the {PROPERTY_GROUP_NAME!r} property group but Archicad does "
            f"not list it, so property definitions have nowhere to go."
        )
    return resolved


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

    group_id = ensure_property_group(connection)

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
                        "defaultValue": default_property_value(spec.data_type),
                        "availability": [
                            {"classificationItemId": {"guid": item}} for item in availability
                        ],
                        # By identifier, not name. Tapir resolves a name by
                        # scanning the group list and taking the first match,
                        # so two groups called the same thing -- which an
                        # earlier version of this could create -- would send
                        # definitions to whichever came first.
                        "group": {"propertyGroupId": {"guid": group_id}},
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

    # The code is the whole diagnostic here. Tapir's message for a rejected
    # definition is the fixed string "failed to create the property" -- it
    # passes Archicad's own GSErrCode through as the *code*, and dropping that
    # leaves nine identical lines that say nothing about why.
    problems = [
        f"{spec.name} ({spec.data_type}): "
        f"{(item.get('error') or {}).get('message', 'unknown error')} "
        f"[code {(item.get('error') or {}).get('code', 'none')}]"
        for spec, item in zip(missing, created, strict=True)
        if isinstance(item, dict) and "error" in item
    ]
    if problems:
        raise ArchicadError(
            f"Archicad refused to create {len(problems)} of {len(missing)} property "
            f"definitions in group {PROPERTY_GROUP_NAME!r} "
            f"(id {group_id}, availability {len(availability)} classification "
            f"items):\n  " + "\n  ".join(problems)
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


@dataclass(frozen=True)
class ApartmentMatch:
    """Which Archicad element each assessed apartment belongs to.

    Computed once and handed to everything that needs it, so the schedule and
    the coloured plan cannot disagree about which apartment is which. Two
    independent joins over the same data would agree almost always, and the
    time they did not would be a diagram whose colours belonged to the
    neighbours.
    """

    by_apartment: dict[str, str]
    unmatched: tuple[str, ...]
    """Apartments in the results with no element in the project."""
    ambiguous: tuple[str, ...]
    """Apartments matching more than one element, so the target is unclear."""


def match_apartments(
    connection: ArchicadConnection, assessment: BuildingAssessment
) -> ApartmentMatch:
    """Join assessed apartments onto Archicad elements by IFC GlobalId.

    An apartment that matches nothing, or that matches several elements, is
    reported rather than guessed at: both mean the export and the project have
    drifted apart, and putting a number on the wrong Zone is worse than
    putting none.
    """
    found = elements_by_ifc_ids(
        connection, [apartment.apartment_id for apartment in assessment.apartments]
    )

    matched: dict[str, str] = {}
    unmatched: list[str] = []
    ambiguous: list[str] = []
    for apartment in assessment.apartments:
        guids = found.get(apartment.apartment_id, [])
        label = apartment.apartment_name or apartment.apartment_id
        if not guids:
            unmatched.append(label)
        elif len(guids) > 1:
            ambiguous.append(label)
        else:
            matched[apartment.apartment_id] = guids[0]

    return ApartmentMatch(matched, tuple(unmatched), tuple(ambiguous))


def write_assessment(
    connection: ArchicadConnection,
    assessment: BuildingAssessment,
    *,
    match: ApartmentMatch | None = None,
    run_stamp: str | None = None,
) -> WriteReport:
    """Write one building assessment onto the project's Zones.

    ``match`` is computed if not supplied. Pass the same one used for drawing
    so both describe the same apartments.
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

    resolved = match if match is not None else match_apartments(connection, assessment)
    unmatched = list(resolved.unmatched)
    ambiguous = list(resolved.ambiguous)

    payload: list[dict[str, Any]] = []
    labels: list[str] = []
    written_zones: list[str] = []
    skipped = 0

    for apartment in assessment.apartments:
        guid = resolved.by_apartment.get(apartment.apartment_id)
        if guid is None:
            continue
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
