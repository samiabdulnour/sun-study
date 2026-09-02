"""An ID on every fill this tool draws, so a Schedule can add up its area.

The drawings say how much sun something gets. The *numbers* behind them have
until now left the project by a different door -- a CSV, or the run's own log
-- and a figure that lives outside the file it describes is a figure somebody
has to reconcile by hand, or scale off a drawing with a rule.

Archicad already has the machinery for this and it is a Schedule: list every
Fill whose ID contains ``SOLAR``, group by ID, sum ``Area``. That works
because ``Area`` is a built-in property of a Fill and ``Element ID`` is a
writable one, so the only thing missing was for the tool to fill the ID in.

The vocabulary
--------------
``SUN STUDY / SOLAR / 2-3 hrs``
``SUN STUDY / SHADOW / JUNE 21 9AM / ADDITIONAL``

Three things are doing work in that shape.

``SUN STUDY`` first, so one filter finds everything the tool drew and nothing
else. It is the group word from ``naming`` rather than the layer prefix: a
layer prefix files output inside an office's layer numbering, and ``14 |`` in
an element ID reads as a mistake.

``SOLAR`` or ``SHADOW`` second, because that is the split a reader wants
first and a Schedule filters on a substring.

Then whatever separates one fill from the next *within* that study -- the
band, or the hour and whose shadow it is. Without it a schedule can total the
study and nothing else; with it, the same schedule breaks down by band or by
hour, which is the table anybody actually wants.

Slashes because they read as a hierarchy and Archicad's ID field takes them
without complaint. Nothing parses these back: they are for a person and for a
schedule's grouping, and a tool that round-tripped its own IDs would be one
rename away from measuring the wrong fills.

Never fatal
-----------
A project whose ``Element ID`` cannot be written -- locked, or a teamwork
reservation nobody holds -- still gets its drawings. The fills are correct and
the sheets are correct; what is missing is a convenience. So this reports and
never raises, and the report says plainly how many IDs landed, because
"the schedule is empty" is otherwise a mystery at the far end.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from sun_study.archicad import naming
from sun_study.archicad.connection import ArchicadConnection, ArchicadError
from sun_study.archicad.write import all_properties

__all__ = [
    "GROUPING_MINIMUM_TAPIR_VERSION",
    "NOT_GROUPED",
    "NOT_STAMPED",
    "SHADOW",
    "SOLAR",
    "GroupReport",
    "StampReport",
    "combined",
    "element_id_property",
    "fill_id",
    "group_by_value",
    "stamp_element_ids",
    "stamp_in_order",
]

#: The word that says which study a fill belongs to. Upper case because an
#: Archicad ID is read in a schedule column beside other upper-case IDs, and
#: because a filter somebody types by hand is easier to get right in one case.
SOLAR = "SOLAR"
SHADOW = "SHADOW"

#: The built-in property being written. Matched by exact name: the project
#: also carries ``Hotlink and Element ID``, which is read-only and would
#: otherwise match a "contains" test and be silently skipped by Archicad.
ELEMENT_ID = "Element ID"

#: How many values go in one request. The same batch the fills use, and for
#: the same reason: a shadow series is thousands of elements and one payload
#: carrying all of them is JSON Archicad has to parse in a single bite.
BATCH = 500


def fill_id(study: str, *parts: str) -> str:
    """The ID one fill carries. ``SUN STUDY / SHADOW / JUNE 21 9AM / EXISTING``.

    Empty parts are dropped rather than left as an empty segment, so a caller
    with nothing to say about a band does not produce ``SOLAR //``.
    """
    words = [naming.GROUP_WORD.upper(), study, *(part.strip() for part in parts if part.strip())]
    return " / ".join(words)


@dataclass(frozen=True)
class StampReport:
    """How many IDs landed, and why any did not."""

    written: int
    attempted: int
    problem: str = ""

    @property
    def complete(self) -> bool:
        return self.written == self.attempted and not self.problem

    def describe(self) -> str:
        if self.complete:
            return (
                f"  tagged {self.written} fills with an Element ID -- schedule them "
                f"by filtering ID for '{SOLAR}' or '{SHADOW}' and summing Area"
            )
        return (
            f"  WARNING: {self.written} of {self.attempted} fills got an Element ID"
            + (f" -- {self.problem}" if self.problem else "")
            + ". The drawings are unaffected, but a Schedule filtered on the ID will "
            "not total the whole study."
        )


#: Nothing was tagged, and nothing was meant to be. A named constant rather
#: than a call in a dataclass default, which is both a lint error and a way to
#: get a shared mutable default -- and this one is genuinely shared, since a
#: report that never reached the stamping step is the same report every time.
NOT_STAMPED = StampReport(0, 0)


def element_id_property(connection: ArchicadConnection) -> str | None:
    """The identifier of the writable built-in ``Element ID``, or ``None``.

    ``None`` rather than an exception: a project that will not give it up
    should lose its schedule, not its drawings.
    """
    try:
        found = all_properties(connection)
    except ArchicadError:
        return None
    for detail in found:
        if detail.name == ELEMENT_ID and detail.editable:
            return detail.identifier
    return None


def stamp_element_ids(
    connection: ArchicadConnection,
    assignments: Sequence[tuple[dict[str, Any], str]],
) -> StampReport:
    """Write an ID onto each created element. Never raises.

    ``assignments`` pairs what a create command handed back -- entries
    carrying an ``elementId`` -- with the ID that element should wear. Pairing
    is the caller's job because only the caller knows which fill was which:
    the create commands answer in the order they were given, and that order is
    the only link between a hatch and the band it was drawn for.
    """
    if not assignments:
        return StampReport(0, 0)

    property_id = element_id_property(connection)
    if property_id is None:
        return StampReport(
            0,
            len(assignments),
            f"the project has no writable {ELEMENT_ID!r} property to write to",
        )

    payload = [
        {
            "elementId": element["elementId"],
            "propertyId": {"guid": property_id},
            "propertyValue": {"value": value},
        }
        for element, value in assignments
        if element.get("elementId")
    ]
    if not payload:
        return StampReport(0, len(assignments), "none of the created fills reported an id")

    written = 0
    problems: list[str] = []
    for start in range(0, len(payload), BATCH):
        chunk = payload[start : start + BATCH]
        try:
            response = connection.run_tapir(
                "SetPropertyValuesOfElements", {"elementPropertyValues": chunk}
            )
        except ArchicadError as refused:
            problems.append(str(refused))
            continue
        results = response.get("executionResults") if isinstance(response, dict) else None
        if not isinstance(results, list) or len(results) != len(chunk):
            problems.append(
                "SetPropertyValuesOfElements answered with a list of a different "
                "length, so it cannot be said which IDs landed"
            )
            continue
        for result in results:
            if not isinstance(result, dict):
                continue
            if result.get("success"):
                written += 1
            elif result.get("error"):
                problems.append(str((result["error"] or {}).get("message", "unknown error")))

    return StampReport(
        written=written,
        attempted=len(payload),
        problem="; ".join(sorted(set(problems))[:3]),
    )


def stamp_in_order(
    connection: ArchicadConnection,
    made: Sequence[dict[str, Any]],
    values: Sequence[str | None],
) -> StampReport:
    """Pair what was created with what it should be called, positionally.

    The create commands answer in the order they were given, and that order is
    the only link between a hatch and the band or hour it was drawn for. So a
    length mismatch is not a small problem to paper over: IDs written against
    a shifted list would put "9AM EXISTING" on a fill drawn for three in the
    afternoon, and a schedule would total it without complaint. That is worse
    than no ID at all, so it is refused outright and said out loud.

    ``None`` in ``values`` means "created, but never to be totalled" -- a
    legend swatch, a site boundary. Those are dropped rather than tagged, so
    that a schedule filtering the study word gets the measured area and
    nothing else.
    """
    if len(made) != len(values):
        return StampReport(
            0,
            sum(1 for value in values if value is not None),
            f"Archicad returned {len(made)} elements for {len(values)} fills, so no "
            f"fill can be matched to what it was drawn for",
        )
    return stamp_element_ids(
        connection,
        [
            (element, value)
            for element, value in zip(made, values, strict=True)
            if value is not None
        ],
    )


def combined(reports: Iterable[StampReport]) -> StampReport:
    """One report for several passes -- a drawing made an instant at a time."""
    listed = list(reports)
    if not listed:
        return NOT_STAMPED
    return StampReport(
        written=sum(report.written for report in listed),
        attempted=sum(report.attempted for report in listed),
        problem="; ".join(sorted({report.problem for report in listed if report.problem})[:3]),
    )


#: The add-on version ``CreateGroups`` needs. Grouping is an Archicad 26 API
#: -- the command answers ``APIERR_NOTSUPPORTED`` on 25 and older, from inside
#: a successful response -- so it is asked for and not assumed.
GROUPING_MINIMUM_TAPIR_VERSION = (1, 5, 7)


@dataclass(frozen=True)
class GroupReport:
    """How many groups were made, and why any were not."""

    groups: int
    elements: int
    problem: str = ""

    @property
    def complete(self) -> bool:
        return not self.problem

    def describe(self) -> str:
        if not self.elements:
            return ""
        if self.complete:
            return (
                f"  grouped them into {self.groups} groups, one per category -- "
                f"clicking any fill selects the whole band"
            )
        return (
            f"  fills were drawn but not grouped: {self.problem}. "
            "Nothing is missing from the drawing; selecting one fill selects one fill."
        )


#: Nothing was grouped, and nothing was meant to be. Named for the same
#: reason as ``NOT_STAMPED``: a call in a dataclass default is a lint error.
NOT_GROUPED = GroupReport(0, 0)


def group_by_value(
    connection: ArchicadConnection,
    assignments: Sequence[tuple[dict[str, Any], str | None]],
) -> GroupReport:
    """One Archicad Group per distinct ID, so a category selects as one thing.

    This is what "merge them" turns into once the drawing side is understood.
    Fills cannot be merged: ``CreateHatches`` takes *a single contour and no
    holes* -- its own schema says so, and refuses unknown fields -- so two
    apartments, or a shadow and the courtyard inside it, can never be one
    element however much they belong together. Grouping is the thing Archicad
    actually offers for "these belong together": they select, move and delete
    as one, and stay separate elements so every one of them keeps its own
    Area for the schedule. Merging would have destroyed exactly the number
    this was all for.

    Keyed on the same string the Element ID carries, so the grouping and the
    schedule always agree about what a category is. Fills marked ``None`` --
    a legend swatch, a site boundary -- are left ungrouped, for the same
    reason they are left untagged: they are not part of any category.

    Never raises. A project that will not group still has its drawings.
    """
    wanted: dict[str, list[dict[str, Any]]] = {}
    for element, value in assignments:
        if value is None or not element.get("elementId"):
            continue
        wanted.setdefault(value, []).append(element)
    if not wanted:
        return GroupReport(0, 0)

    total = sum(len(members) for members in wanted.values())
    try:
        connection.require_tapir_at_least(
            GROUPING_MINIMUM_TAPIR_VERSION, "CreateGroups, which groups the fills,"
        )
        response = connection.run_tapir(
            "CreateGroups",
            {
                "elementGroups": [
                    {"elements": [{"elementId": member["elementId"]} for member in members]}
                    for members in wanted.values()
                ]
            },
        )
    except ArchicadError as refused:
        return GroupReport(0, total, str(refused))

    results = response.get("groupGuids") if isinstance(response, dict) else None
    if not isinstance(results, list):
        return GroupReport(0, total, "CreateGroups returned no group list")

    made = sum(1 for entry in results if isinstance(entry, dict) and entry.get("groupId"))
    problems = sorted(
        {
            str((entry["error"] or {}).get("message", "unknown error"))
            for entry in results
            if isinstance(entry, dict) and entry.get("error")
        }
    )
    return GroupReport(made, total, "; ".join(problems[:3]))
