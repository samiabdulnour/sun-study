"""Element IDs on the fills, so an Archicad Schedule can total their area.

The point of these is a number, which makes the failure mode a number that is
wrong rather than a drawing that looks wrong. Two traps matter more than
anything else here and both are about *what is not tagged*: a legend swatch or
a site boundary caught by the same filter would be added to the area, and
nothing on the schedule would say so.

The third is ordering. A create command answers in the order it was asked, and
that order is the only link between a hatch and the hour it was drawn for -- so
a length mismatch has to be refused, not trimmed to fit.
"""

from __future__ import annotations

from typing import Any

from sun_study.archicad import naming
from sun_study.archicad.connection import ArchicadConnection
from sun_study.archicad.ids import (
    NOT_STAMPED,
    SHADOW,
    SOLAR,
    combined,
    element_id_property,
    fill_id,
    group_by_value,
    stamp_element_ids,
    stamp_in_order,
)


class PropertyTransport:
    """Answers the two commands stamping needs, and records what was written."""

    def __init__(self, *, editable: bool = True, fails: bool = False) -> None:
        self.editable = editable
        self.fails = fails
        self.written: list[dict[str, Any]] = []

    def send(self, payload: dict[str, Any]) -> dict[str, Any]:
        name = payload["parameters"]["addOnCommandId"]["commandName"]
        given = payload["parameters"].get("addOnCommandParameters") or {}
        return {"succeeded": True, "result": {"addOnCommandResponse": self._answer(name, given)}}

    def _answer(self, command: str, given: dict[str, Any]) -> dict[str, Any]:
        if command == "GetAddOnVersion":
            return {"version": "1.5.7"}
        if command == "GetAllProperties":
            return {
                "properties": [
                    {
                        "propertyId": {"guid": "prop-element-id"},
                        "propertyGroupName": "General Parameters",
                        "propertyName": "Element ID",
                        "propertyIsEditable": self.editable,
                    },
                    {
                        "propertyId": {"guid": "prop-hotlink"},
                        "propertyGroupName": "General Parameters",
                        "propertyName": "Hotlink and Element ID",
                        "propertyIsEditable": False,
                    },
                ]
            }
        if command == "SetPropertyValuesOfElements":
            values = given["elementPropertyValues"]
            self.written.extend(values)
            return {
                "executionResults": [
                    {"error": {"message": "layer is locked"}} if self.fails else {"success": True}
                    for _ in values
                ]
            }
        raise AssertionError(f"unscripted command {command!r}")


def connect(**kwargs: Any) -> tuple[ArchicadConnection, PropertyTransport]:
    transport = PropertyTransport(**kwargs)
    return ArchicadConnection(transport), transport


def elements(count: int) -> list[dict[str, Any]]:
    return [{"elementId": {"guid": f"fill-{index}"}} for index in range(count)]


# -- the vocabulary --------------------------------------------------------


def test_the_study_word_is_in_every_id_because_that_is_what_a_schedule_filters_on() -> None:
    assert SOLAR in fill_id(SOLAR, "2-3 hrs")
    assert SHADOW in fill_id(SHADOW, "JUNE 21 -9AM", "ADDITIONAL")


def test_an_id_leads_with_the_tool_not_with_the_layer_prefix() -> None:
    """One filter finds everything the tool drew. The layer prefix files
    output inside an office's layer numbering, and '14 |' in an element ID
    reads as a mistake."""
    made = fill_id(SOLAR, "2-3 hrs")

    assert made.startswith(naming.GROUP_WORD.upper())
    assert naming.prefix() not in made


def test_the_detail_is_kept_so_one_schedule_breaks_down_by_hour() -> None:
    """Without it a schedule can total the study and nothing else."""
    assert (
        fill_id(SHADOW, "JUNE 21 -9AM", "EXISTING")
        == "SOLAR ANALYSIS / SHADOW / JUNE 21 -9AM / EXISTING"
    )


def test_an_empty_part_is_dropped_rather_than_left_as_a_gap() -> None:
    assert fill_id(SOLAR, "", "  ") == "SOLAR ANALYSIS / SOLAR"


# -- what must not be totalled --------------------------------------------


def test_a_fill_marked_none_is_created_and_never_tagged() -> None:
    """The trap this whole scheme turns on. A legend swatch or a site boundary
    caught by 'ID contains SHADOW' would be added to the shadow area, and
    nothing on the schedule would say so."""
    connection, transport = connect()

    report = stamp_in_order(
        connection,
        elements(4),
        [fill_id(SHADOW, "9AM"), None, fill_id(SHADOW, "10AM"), None],
    )

    assert report.written == 2
    assert [entry["propertyValue"]["value"] for entry in transport.written] == [
        "SOLAR ANALYSIS / SHADOW / 9AM",
        "SOLAR ANALYSIS / SHADOW / 10AM",
    ]


def test_the_untagged_ones_are_not_counted_as_failures() -> None:
    """They were never meant to be tagged, so a run that skipped them is
    complete and must not print a warning about it."""
    connection, _ = connect()

    report = stamp_in_order(connection, elements(3), [fill_id(SOLAR, "2-3 hrs"), None, None])

    assert report.complete
    assert "WARNING" not in report.describe()


# -- ordering --------------------------------------------------------------


def test_a_count_mismatch_is_refused_rather_than_trimmed_to_fit() -> None:
    """IDs written against a shifted list would put '9AM EXISTING' on a fill
    drawn for three in the afternoon, and a schedule would total it without
    complaint. That is worse than no ID at all."""
    connection, transport = connect()

    report = stamp_in_order(connection, elements(2), [fill_id(SHADOW, "9AM")] * 3)

    assert report.written == 0
    assert not transport.written, "nothing may be written on a mismatch"
    assert "no fill can be matched" in report.describe()


def test_ids_are_written_in_the_order_the_fills_were_asked_for() -> None:
    connection, transport = connect()

    stamp_in_order(
        connection, elements(3), [fill_id(SHADOW, hour) for hour in ("9AM", "10AM", "11AM")]
    )

    assert [entry["elementId"]["guid"] for entry in transport.written] == [
        "fill-0",
        "fill-1",
        "fill-2",
    ]


# -- when it cannot be done ------------------------------------------------


def test_the_read_only_hotlink_id_is_never_mistaken_for_the_writable_one() -> None:
    """'Hotlink and Element ID' contains 'Element ID' and cannot be written."""
    connection, _ = connect()

    assert element_id_property(connection) == "prop-element-id"


def test_a_project_that_will_not_give_up_the_property_loses_ids_not_drawings() -> None:
    connection, transport = connect(editable=False)

    report = stamp_element_ids(connection, [(elements(1)[0], fill_id(SOLAR, "2-3 hrs"))])

    assert report.written == 0
    assert not transport.written
    assert "no writable" in report.describe()
    assert "drawings are unaffected" in report.describe()


def test_a_refused_write_is_reported_with_the_reason() -> None:
    """'The schedule is empty' is otherwise a mystery at the far end."""
    connection, _ = connect(fails=True)

    report = stamp_in_order(connection, elements(2), [fill_id(SOLAR, "a"), fill_id(SOLAR, "b")])

    assert report.written == 0
    assert "layer is locked" in report.describe()


# -- several passes --------------------------------------------------------


def test_a_drawing_made_an_instant_at_a_time_reports_one_total() -> None:
    connection, _ = connect()
    per_instant = [
        stamp_in_order(connection, elements(2), [fill_id(SHADOW, hour)] * 2)
        for hour in ("9AM", "10AM", "11AM")
    ]

    together = combined(per_instant)

    assert together.written == 6
    assert together.attempted == 6
    assert together.complete


def test_no_passes_at_all_is_the_nothing_report() -> None:
    assert combined([]) == NOT_STAMPED


# -- grouping --------------------------------------------------------------


class GroupTransport(PropertyTransport):
    """Adds the grouping command to the property one."""

    def __init__(self, *, supported: bool = True, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.supported = supported
        self.groups: list[dict[str, Any]] = []

    def _answer(self, command: str, given: dict[str, Any]) -> dict[str, Any]:
        if command == "CreateGroups":
            groups = given["elementGroups"]
            self.groups.extend(groups)
            if not self.supported:
                return {
                    "groupGuids": [
                        {"error": {"message": "not supported in Archicad 25 or older"}}
                        for _ in groups
                    ]
                }
            return {"groupGuids": [{"groupId": {"guid": f"g{n}"}} for n in range(len(groups))]}
        return super()._answer(command, given)


def grouped(**kwargs: Any) -> tuple[ArchicadConnection, GroupTransport]:
    transport = GroupTransport(**kwargs)
    return ArchicadConnection(transport), transport


def test_one_group_per_category_not_one_per_fill() -> None:
    """The point: clicking any fill of a band picks up the whole band."""
    connection, transport = grouped()
    band = fill_id(SOLAR, "2-3 hrs")
    other = fill_id(SOLAR, "3-4 hrs")

    report = group_by_value(
        connection, list(zip(elements(5), [band, band, other, band, other], strict=True))
    )

    assert report.groups == 2, "two bands, two groups"
    assert report.elements == 5
    sizes = sorted(len(entry["elements"]) for entry in transport.groups)
    assert sizes == [2, 3]


def test_the_fills_stay_separate_elements_so_each_keeps_its_own_area() -> None:
    """Grouping is not merging, and that is the whole reason it is the right
    answer here: merged into one element the schedule would lose the per-band
    area, which is the number this was all for."""
    connection, transport = grouped()
    band = fill_id(SOLAR, "2-3 hrs")

    group_by_value(connection, list(zip(elements(3), [band] * 3, strict=True)))

    (only,) = transport.groups
    assert len(only["elements"]) == 3, "three elements in the group, not one merged shape"


def test_an_untagged_fill_joins_no_group() -> None:
    """A legend swatch is not part of any category, for the same reason it
    carries no ID."""
    connection, transport = grouped()

    report = group_by_value(
        connection, list(zip(elements(3), [fill_id(SOLAR, "a"), None, None], strict=True))
    )

    assert report.elements == 1
    assert sum(len(entry["elements"]) for entry in transport.groups) == 1


def test_an_archicad_too_old_to_group_still_gets_its_drawings() -> None:
    """``CreateGroups`` is an Archicad 26 API and answers with an error from
    inside a successful response on anything older."""
    connection, _ = grouped(supported=False)

    report = group_by_value(
        connection, list(zip(elements(2), [fill_id(SOLAR, "a")] * 2, strict=True))
    )

    assert report.groups == 0
    assert not report.complete
    assert "not supported" in report.describe()
    assert "Nothing is missing from the drawing" in report.describe()


def test_nothing_to_group_says_nothing() -> None:
    """A report line for zero groups is noise in a run that draws nothing."""
    connection, _ = grouped()

    assert group_by_value(connection, []).describe() == ""
