"""The whole chain through the CLI, on the fixture.

Milestone M3's acceptance criterion is per-apartment results for the fixture
building, so this exercises the actual entry point a person types rather than
the library underneath it.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from sun_study.cli import app
from sun_study.disclaimer import STATUS
from sun_study.ingest.scene import SceneConfig
from sun_study.pipeline import PipelineResult, run_assessment

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
SAMPLE = FIXTURES / "sample_building.ifc"

runner = CliRunner()


@pytest.fixture(scope="module")
def result() -> PipelineResult:
    return run_assessment(SAMPLE, timezone="Australia/Sydney")


# ---------------------------------------------------------------------------
# The pipeline.
# ---------------------------------------------------------------------------
def test_the_fixture_is_assessed_end_to_end(result: PipelineResult) -> None:
    assessment = result.assessment
    assert assessment.counted_total == 4
    assert assessment.meeting_minimum == 3
    assert assessment.compliant_share == pytest.approx(0.75)
    assert assessment.meets_minimum_share  # 75% >= 70%
    assert assessment.within_no_sunlight_cap  # 0% <= 15%
    assert assessment.complies


def test_the_failing_apartment_is_the_most_shaded_one(result: PipelineResult) -> None:
    """L00-A is on the shaded side of the lower storey, under a balcony."""
    by_name = {a.apartment_name: a for a in result.assessment.apartments}
    assert not by_name["Apartment L00-A"].meets_minimum
    assert all(
        by_name[n].meets_minimum for n in ("Apartment L00-B", "Apartment L01-A", "Apartment L01-B")
    )


def test_the_governing_figure_is_the_worse_of_the_two(result: PipelineResult) -> None:
    """L00-A's balcony clears 2 hours but its living room does not, and the
    apartment is governed by the living room."""
    apartment = next(
        a for a in result.assessment.apartments if a.apartment_name == "Apartment L00-A"
    )
    assert apartment.open_space_minutes is not None
    assert apartment.open_space_minutes > 120.0
    assert apartment.living_room_minutes < 120.0
    assert apartment.governing_minutes == apartment.living_room_minutes


def test_thirty_seven_sun_positions_are_used(result: PipelineResult) -> None:
    assert result.sun_position_count == 37
    assert result.assessment_date.isoformat() == "2024-06-21"


def test_the_area_selects_the_published_threshold(result: PipelineResult) -> None:
    """Sydney Metro is the 2 hour criterion, everywhere else is 3.

    This fixture does *not* discriminate between them: its governing figures
    are 106, 207, 256 and 360 minutes, so none falls in the 120 to 180 band
    where the two criteria disagree, and the verdict is 75% either way. That is
    a property of the fixture, not evidence that the area setting works, so it
    is stated here rather than left for someone to misread as coverage. The
    behavioural test is
    ``test_the_three_hour_area_fails_what_the_two_hour_area_passes``.
    """
    outside = run_assessment(SAMPLE, timezone="Australia/Sydney", area="other")

    assert result.assessment.minimum_minutes == 120.0
    assert outside.assessment.minimum_minutes == 180.0
    assert outside.assessment.area_key == "other"
    assert "All other areas" in outside.assessment.area_label

    governing = sorted(a.governing_minutes for a in outside.assessment.apartments)
    assert not any(120.0 <= value < 180.0 for value in governing), (
        f"the fixture now has an apartment between the two thresholds ({governing}); "
        f"this test's premise no longer holds and it should assert the difference"
    )
    # The threshold changed but the geometry did not, so the durations are the same.
    assert governing == pytest.approx(
        sorted(a.governing_minutes for a in result.assessment.apartments)
    )


def test_the_run_keeps_when_the_sun_arrived_not_only_how_long(result: PipelineResult) -> None:
    """The per-instant series is the drawing series' only source.

    It used to be computed inside ``_durations`` and dropped on the floor, so
    a study could say an apartment gets 106 minutes and nothing at all about
    which 106.
    """
    series = result.instants
    assert series is not None
    assert len(series.times) == result.sun_position_count == 37
    assert series.times[0].strftime("%H:%M") == "09:00"
    assert series.times[-1].strftime("%H:%M") == "15:00"

    assert series.living_share.shape == (len(series.apartment_ids), 37)
    assert ((series.living_share >= 0.0) & (series.living_share <= 1.0)).all(), (
        "a share is a fraction of an element's area, so it cannot leave [0, 1]"
    )
    assert set(series.apartment_ids) == {a.apartment_id for a in result.assessment.apartments}

    # The shaded ground-floor apartment is dark at some instant and lit at
    # another; a series that never changes would be a constant redrawn 37 times.
    by_name = {a.apartment_name: a.apartment_id for a in result.assessment.apartments}
    row = series.apartment_ids.index(by_name["Apartment L00-A"])
    assert series.living_share[row].min() < series.living_share[row].max()


def test_the_sun_patch_moves_across_the_floor_through_the_day() -> None:
    """The patch is the deliverable's actual subject.

    A patch that never moved would mean the instant index was being ignored,
    which is exactly the bug a still picture of a moving thing hides best.
    """
    result = run_assessment(
        SAMPLE,
        timezone="Australia/Sydney",
        scene_config=SceneConfig(timezone="Australia/Sydney", floor_patch_spacing_m=0.25),
    )
    series = result.instants
    assert series is not None
    assert series.floor_sunlit is not None

    morning = series.patches_at(0)
    afternoon = series.patches_at(len(series.times) - 1)
    assert morning and afternoon
    assert morning != afternoon, "the sun does not stand still between 9 and 3"

    assert series.floor_positions is not None
    lit_area = sum(r.area_m2 for rectangles in morning.values() for r in rectangles)
    floor_area = len(series.floor_positions) * 0.25**2
    assert 0 < lit_area < floor_area, "a patch cannot exceed the floor it lies on"


def test_no_patch_is_computed_when_none_was_asked_for(result: PipelineResult) -> None:
    """The default run must not pay for a drawing it was not asked to make."""
    assert result.instants is not None
    assert result.instants.floor_sunlit is None
    assert result.instants.patches_at(0) == {}


def test_a_mismatched_timezone_is_rejected() -> None:
    """Two different zones in one run would be silently wrong."""
    with pytest.raises(ValueError, match="two different zones"):
        run_assessment(
            SAMPLE,
            timezone="Australia/Sydney",
            scene_config=SceneConfig(timezone="Europe/London"),
        )


# ---------------------------------------------------------------------------
# The command line.
# ---------------------------------------------------------------------------
def test_run_prints_the_disclaimer_and_the_numbers(tmp_path: Path) -> None:
    invocation = runner.invoke(app, ["run", str(SAMPLE), "--timezone", "Australia/Sydney"])
    assert invocation.exit_code == 0, invocation.output

    assert STATUS in invocation.output
    assert "must not be used as a compliance figure" in invocation.output
    # Site, ruleset and interpretation are all echoed before any result.
    assert "Australia/Sydney" in invocation.output
    assert "true north bearing of model +Y 30.000 deg" in invocation.output
    assert "nsw_adg@1.0.0" in invocation.output
    assert "cumulative duration, trapezoidal weighting" in invocation.output
    assert "Apartment L00-A" in invocation.output
    assert "COMPLIES" in invocation.output


def test_run_writes_csv_and_json(tmp_path: Path) -> None:
    csv_path, json_path = tmp_path / "out.csv", tmp_path / "out.json"
    invocation = runner.invoke(
        app,
        [
            "run",
            str(SAMPLE),
            "--timezone",
            "Australia/Sydney",
            "--csv",
            str(csv_path),
            "--json",
            str(json_path),
        ],
    )
    assert invocation.exit_code == 0, invocation.output
    assert csv_path.is_file() and json_path.is_file()

    rows = [
        line
        for line in csv_path.read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#")
    ]
    table = list(csv.DictReader(rows))
    assert len(table) == 4
    assert {row["apartment_name"] for row in table} == {
        "Apartment L00-A",
        "Apartment L00-B",
        "Apartment L01-A",
        "Apartment L01-B",
    }
    assert all(row["ruleset"] == "nsw_adg@1.0.0" for row in table)

    document = json.loads(json_path.read_text(encoding="utf-8"))
    assert document["summary"]["complies"] is True
    assert document["summary"]["compliant_share"] == pytest.approx(0.75)
    assert document["header"]["status"] == STATUS
    assert "4A-1" in document["header"]["ruleset"]["minimum_sunlight_citation"]


def test_run_reports_a_missing_north_direction_clearly(tmp_path: Path) -> None:
    """The most likely real-world failure, and the message names the fix."""
    import ifcopenshell

    model = ifcopenshell.open(str(SAMPLE))
    for context in model.by_type("IfcGeometricRepresentationContext", include_subtypes=False):
        context.TrueNorth = None
    broken = tmp_path / "no_north.ifc"
    model.write(str(broken))

    invocation = runner.invoke(app, ["run", str(broken), "--timezone", "Australia/Sydney"])
    assert invocation.exit_code == 2
    assert "North Direction" in invocation.output


def test_run_rejects_an_unknown_area() -> None:
    invocation = runner.invoke(
        app, ["run", str(SAMPLE), "--timezone", "Australia/Sydney", "--area", "atlantis"]
    )
    assert invocation.exit_code == 2
    assert "Unknown area" in invocation.output


def test_info_echoes_the_model_without_assessing() -> None:
    invocation = runner.invoke(app, ["info", str(SAMPLE), "--timezone", "Australia/Sydney"])
    assert invocation.exit_code == 0, invocation.output
    assert "unit scale 0.001" in invocation.output
    assert "30.000 deg" in invocation.output
    assert "COMPLIES" not in invocation.output


def test_rulesets_command_lists_thresholds_and_source() -> None:
    invocation = runner.invoke(app, ["rulesets"])
    assert invocation.exit_code == 0, invocation.output
    assert "nsw_adg@1.0.0" in invocation.output
    assert "120 min" in invocation.output
    assert "180 min" in invocation.output
    assert "planning.nsw.gov.au" in invocation.output


def test_a_wrong_living_room_name_is_visible_not_silent() -> None:
    """Zero assessed apartments must be obvious in the output, because a
    building with no living rooms reads as trivially compliant."""
    invocation = runner.invoke(
        app,
        ["run", str(SAMPLE), "--timezone", "Australia/Sydney", "--living-room", "Kitchen"],
    )
    assert invocation.exit_code == 0, invocation.output
    assert "living rooms matched by [Kitchen]" in invocation.output
    assert "0 window samples" in invocation.output
    assert "DOES NOT COMPLY" in invocation.output, (
        "an empty assessment must not report as compliant"
    )


def test_the_zone_name_filter_reaches_the_scene() -> None:
    """``--apartment-zone-name`` narrows the denominator.

    The option existed on both commands and was dropped between them and the
    config, so a run that named one zone silently assessed all four. Nothing
    in the output said so -- a filter that does not filter reads exactly like
    a project with more apartments than you thought.
    """
    invocation = runner.invoke(
        app,
        [
            "run",
            str(SAMPLE),
            "--timezone",
            "Australia/Sydney",
            "--apartment-zone-name",
            "Apartment L00-A",
        ],
    )
    assert invocation.exit_code == 0, invocation.output
    assert "named ['Apartment L00-A']" in invocation.output
    assert "0/1 apartments" in invocation.output, "the other three must be out of the count"


def test_filtering_the_apartments_does_not_change_their_numbers(result: PipelineResult) -> None:
    """Narrowing the denominator must not move the numerator.

    Windows and balconies used to be resolved against the *surviving* spaces,
    so filtering to one apartment let it inherit its neighbours' glazing: the
    fixture's L00-A read 106 minutes in a full run and 202 in a filtered one,
    which is the difference between failing 4A-1 and passing it.
    """
    alone = run_assessment(
        SAMPLE,
        timezone="Australia/Sydney",
        scene_config=SceneConfig(
            timezone="Australia/Sydney", apartment_zone_names=("Apartment L00-A",)
        ),
    )
    (only,) = alone.assessment.apartments
    full = next(a for a in result.assessment.apartments if a.apartment_name == only.apartment_name)
    assert only.living_room_minutes == pytest.approx(full.living_room_minutes)
    assert only.open_space_minutes == pytest.approx(full.open_space_minutes)


# ---------------------------------------------------------------------------
# The Archicad commands, with nothing listening.
#
# These cannot reach an Archicad in CI, so what they pin is the behaviour a
# person actually hits first: running the command with Archicad shut, or on
# the wrong port. That has to be a named, actionable failure rather than a
# traceback, because it is the single most common way the tool is used wrong.
# ---------------------------------------------------------------------------
CLOSED_PORT = ["--port", "1"]


@pytest.fixture
def no_archicad_anywhere(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin "nothing is listening" against the machine, not only against a port.

    A dead port is not enough on its own: ``_connect`` falls through to the one
    Archicad that is running, so on a machine with the reference project open
    these tests reached it -- and ``init-properties`` wrote nine property
    definitions into that live project. A test must never touch the model
    somebody has on screen, so the scan is stubbed empty.
    """
    import sun_study.cli as cli

    monkeypatch.setattr(cli, "find_instances", lambda *a, **k: ())


@pytest.mark.parametrize("command", ["archicad-info", "init-properties"])
@pytest.mark.usefixtures("no_archicad_anywhere")
def test_archicad_commands_fail_helpfully_with_nothing_listening(command: str) -> None:
    invocation = runner.invoke(app, [command, *CLOSED_PORT])
    assert invocation.exit_code == 2, invocation.output
    assert "Could not reach Archicad" in invocation.output
    assert "JSON" in invocation.output, "the message must name the setting to check"


@pytest.mark.usefixtures("no_archicad_anywhere")
def test_archicad_run_fails_helpfully_with_nothing_listening() -> None:
    invocation = runner.invoke(
        app, ["archicad-run", "--timezone", "Australia/Sydney", *CLOSED_PORT]
    )
    assert invocation.exit_code == 2, invocation.output
    assert "Could not reach Archicad" in invocation.output


def test_archicad_run_does_not_write_back_unless_asked() -> None:
    """Write-back edits a file colleagues share, so it must stay opt-in.

    Pinned on the signature rather than on the help text: a default that flips
    to True is the kind of change that reads as harmless in a diff and is not.
    """
    import inspect

    from sun_study.cli import archicad_run

    assert inspect.signature(archicad_run).parameters["write"].default is False


def test_the_archicad_commands_are_registered() -> None:
    names = {command.name for command in app.registered_commands}
    assert {"archicad-info", "init-properties", "archicad-run"} <= names


def test_zone_names_are_listed_most_common_first() -> None:
    """What a person reads to find out what to pass to --living-room.

    The default is "Living Room" and a real office file rarely says that, so
    a wrong guess yields zero assessed apartments -- which reads as a building
    with no living rooms rather than as a mistake.
    """
    from sun_study.archicad.read import ArchicadZone
    from sun_study.cli import _report_zone_names

    zones = (
        [ArchicadZone(f"g{i}", "LIVING", f"{i}") for i in range(5)]
        + [ArchicadZone(f"b{i}", "BED 1", f"{i}") for i in range(3)]
        + [ArchicadZone("s0", "", "9")]
    )

    runner_result = CliRunner()
    with runner_result.isolation() as (out, _err, _):
        _report_zone_names(zones, limit=2)
        printed = out.getvalue().decode()

    assert "3 distinct zone names" in printed
    lines = [line.strip() for line in printed.splitlines() if line.strip()]
    assert lines[1].startswith("LIVING"), "most common name must come first"
    assert lines[1].endswith("5")
    assert lines[2].startswith("BED 1")
    assert "and 1 more" in lines[3], "a truncated list must say so"


def test_all_zone_names_are_listed_when_the_limit_is_zero() -> None:
    from sun_study.archicad.read import ArchicadZone
    from sun_study.cli import _report_zone_names

    zones = [ArchicadZone("a", "", "1"), ArchicadZone("b", "LIVING", "2")]

    with CliRunner().isolation() as (out, _err, _):
        _report_zone_names(zones, limit=0)
        printed = out.getvalue().decode()

    assert "(unnamed)" in printed, "an unnamed zone must be visible, not dropped"
    assert "more" not in printed


def test_output_survives_a_character_the_console_cannot_encode() -> None:
    """A real Archicad project killed the CLI with UnicodeEncodeError.

    Windows consoles default to a legacy code page and Python raises on any
    character it cannot represent, aborting the command and losing everything
    already printed. The offending name was a property containing a subscript
    digit -- nobody chose it, and nobody can predict the next one, because an
    Archicad file carries names from libraries, add-ons and other templates.

    Windows is the primary deployment platform, so this has to degrade.
    """
    import io
    import sys

    from sun_study.cli import main

    legacy = io.TextIOWrapper(io.BytesIO(), encoding="cp1252", errors="strict")
    original, sys.stdout = sys.stdout, legacy
    try:
        with pytest.raises(SystemExit):
            main()  # no arguments: prints help, and reconfigures the stream
        legacy.write("subscript ₁ and a degree sign °")
    finally:
        sys.stdout = original

    assert legacy.errors == "replace", "main() must relax the stream's error handling"


def test_a_pen_override_replaces_only_the_named_band() -> None:
    from sun_study.cli import band_styles

    styles = {band.label: band.fill_pen for band in band_styles(["2-3 hrs=42"])}
    assert styles["2-3 hrs"] == 42
    assert styles["0 hrs"] != 42, "the other bands keep their defaults"


def test_a_pen_override_naming_no_band_is_an_error() -> None:
    """A silently ignored override draws the default colours and looks right.

    '2-3 hours' instead of '2-3 hrs' is the obvious typo, and nobody would
    look twice at a diagram that came out coloured.
    """
    import typer

    from sun_study.cli import band_styles

    with pytest.raises(typer.BadParameter, match="2-3 hours"):
        band_styles(["2-3 hours=42"])


def test_a_pen_override_needs_a_number() -> None:
    import typer

    from sun_study.cli import band_styles

    with pytest.raises(typer.BadParameter, match="whole-number"):
        band_styles(["2-3 hrs=orange"])


class _PenTransport:
    """Enough of a transport to answer a version check and one pen table.

    ``pens=None`` stands for a project that lists no pen table at all, which
    is the path where the CLI must warn and keep its guessed defaults rather
    than refuse to draw.
    """

    def __init__(self, pens: list[dict[str, Any]] | None) -> None:
        self.pens = pens

    def send(self, payload: dict[str, Any]) -> dict[str, Any]:
        name = payload["parameters"]["addOnCommandId"]["commandName"]
        return {"succeeded": True, "result": {"addOnCommandResponse": self._answer(name)}}

    def _answer(self, name: str) -> dict[str, Any]:
        if name == "GetAddOnVersion":
            return {"version": "1.5.7"}
        if name == "GetAttributesByType":
            if self.pens is None:
                return {"attributes": []}
            return {"attributes": [{"attributeId": {"guid": "p1"}, "index": 1, "name": "pens"}]}
        assert name == "GetPenTables", name
        return {"penTables": [{"attributeId": {"guid": "p1"}, "index": 1, "pens": self.pens}]}


def _pen(index: int, rgb: tuple[float, float, float]) -> dict[str, Any]:
    red, green, blue = rgb
    return {
        "index": index,
        "color": {"red": red, "green": green, "blue": blue},
        "width": 0.18,
        "description": "",
    }


def test_bands_are_pointed_at_the_projects_own_pens() -> None:
    """The defaults are a guess; the project's pen table is the truth."""
    from sun_study.archicad.connection import ArchicadConnection
    from sun_study.cli import resolve_bands

    connection = ArchicadConnection(
        _PenTransport([_pen(200, (8 / 255, 48 / 255, 107 / 255)), _pen(201, (1.0, 1.0, 1.0))])
    )
    styles = {band.label: band.fill_pen for band in resolve_bands(connection, None)}
    assert styles["0 hrs"] == 200, "the dark blue band takes the dark blue pen"


def test_an_explicit_pen_override_beats_the_colour_match() -> None:
    """Matching is a convenience. A person who names a pen has a reason."""
    from sun_study.archicad.connection import ArchicadConnection
    from sun_study.cli import resolve_bands

    connection = ArchicadConnection(_PenTransport([_pen(200, (8 / 255, 48 / 255, 107 / 255))]))
    styles = {band.label: band.fill_pen for band in resolve_bands(connection, ["0 hrs=42"])}
    assert styles["0 hrs"] == 42


def test_a_typo_in_pen_fails_before_the_pen_table_is_even_read() -> None:
    """An error printed under a wall of matching output reads as noise."""
    import typer

    from sun_study.archicad.connection import ArchicadConnection
    from sun_study.cli import resolve_bands

    transport = _PenTransport([_pen(1, (0.0, 0.0, 0.0))])
    with pytest.raises(typer.BadParameter):
        resolve_bands(ArchicadConnection(transport), ["2-3 hours=42"])


def test_an_unreadable_pen_table_warns_and_keeps_the_defaults() -> None:
    """An old add-on or a project with no pen table must not stop the drawing."""
    from sun_study.archicad.connection import ArchicadConnection
    from sun_study.archicad.draw import DEFAULT_BANDS
    from sun_study.cli import resolve_bands

    assert resolve_bands(ArchicadConnection(_PenTransport(None)), None) == DEFAULT_BANDS


class _LayerTransport:
    """Answers a version check and the layer list, nothing else."""

    def __init__(self, layers: dict[int, str]) -> None:
        self.layers = layers

    def send(self, payload: dict[str, Any]) -> dict[str, Any]:
        name = payload["parameters"]["addOnCommandId"]["commandName"]
        if name == "GetAddOnVersion":
            return {"succeeded": True, "result": {"addOnCommandResponse": {"version": "1.5.7"}}}
        assert name == "GetAttributesByType", name
        return {
            "succeeded": True,
            "result": {
                "addOnCommandResponse": {
                    "attributes": [
                        {"attributeId": {"guid": f"g{index}"}, "index": index, "name": label}
                        for index, label in self.layers.items()
                    ]
                }
            },
        }


def _zones_on(layer_index: int, how_many: int, first: int = 0) -> list[Any]:
    from sun_study.archicad.read import ArchicadZone

    return [
        ArchicadZone(guid=f"z{first + n}", name="RESI", number="", layer_index=layer_index)
        for n in range(how_many)
    ]


def test_zones_are_counted_per_layer_so_the_apartment_layer_is_findable() -> None:
    """A project with 1341 zones is unreadable as a list of names -- every one
    is 'RESI'. The layer is what separates apartments from GFA take-off."""
    import typer.testing

    from sun_study.archicad.connection import ArchicadConnection
    from sun_study.cli import _report_zone_layers

    connection = ArchicadConnection(_LayerTransport({1: "06 | Zone.Units", 3: "10 | Calc.GFA"}))
    runner = typer.testing.CliRunner()
    app = typer.Typer()

    @app.command()
    def show() -> None:
        _report_zone_layers(connection, _zones_on(1, 40) + _zones_on(3, 1301, first=100))

    output = runner.invoke(app, []).output
    assert "40  '06 | Zone.Units'" in output
    assert "1301  '10 | Calc.GFA'" in output


def test_each_layer_reports_the_zone_names_on_it() -> None:
    """A project-wide name tally cannot say what the apartments are called:
    on one real file the top names were annotation and area take-off, and the
    246 zones on '06 | Zone.Units' were invisible in the totals."""
    import typer.testing

    from sun_study.archicad.connection import ArchicadConnection
    from sun_study.archicad.read import ArchicadZone
    from sun_study.cli import _report_zone_layers

    def named(layer: int, label: str, how_many: int, first: int) -> list[Any]:
        return [
            ArchicadZone(guid=f"z{first + n}", name=label, number="", layer_index=layer)
            for n in range(how_many)
        ]

    connection = ArchicadConnection(_LayerTransport({1: "06 | Zone.Units", 3: "10 | Calc.GFA"}))
    app = typer.Typer()

    @app.command()
    def show() -> None:
        _report_zone_layers(
            connection,
            named(1, "S", 200, 0) + named(1, "", 46, 300) + named(3, "RESI", 25, 500),
        )

    output = typer.testing.CliRunner().invoke(app, []).output
    assert "S x200" in output
    assert "(unnamed) x46" in output, "a blank name is reported, not dropped"
    assert "RESI x25" in output


def test_two_zone_layers_with_equal_counts_are_flagged_as_duplicates() -> None:
    """The FUSE manual's trap: '06 | Zone.Units' duplicates the SEPP 65 zones,
    so assessing both counts every apartment twice."""
    import typer.testing

    from sun_study.archicad.connection import ArchicadConnection
    from sun_study.cli import _report_zone_layers

    connection = ArchicadConnection(
        _LayerTransport({1: "06 | Zone.SEPP 65", 2: "06 | Zone.Units", 3: "10 | Calc.GFA"})
    )
    app = typer.Typer()

    @app.command()
    def show() -> None:
        _report_zone_layers(
            connection,
            _zones_on(1, 40) + _zones_on(2, 40, first=100) + _zones_on(3, 9, first=200),
        )

    output = typer.testing.CliRunner().invoke(app, []).output
    assert "counted twice" in output
    assert "06 | Zone.SEPP 65 and 06 | Zone.Units" in output


def test_a_calc_layer_matching_a_zone_layers_count_is_not_flagged() -> None:
    """Only '06 | Zone.*' layers duplicate each other. A GFA layer that happens
    to hold the same number of zones is a coincidence, not a warning."""
    import typer.testing

    from sun_study.archicad.connection import ArchicadConnection
    from sun_study.cli import _report_zone_layers

    connection = ArchicadConnection(_LayerTransport({1: "06 | Zone.Units", 3: "10 | Calc.GFA"}))
    app = typer.Typer()

    @app.command()
    def show() -> None:
        _report_zone_layers(connection, _zones_on(1, 40) + _zones_on(3, 40, first=100))

    assert "counted twice" not in typer.testing.CliRunner().invoke(app, []).output


def test_an_unreadable_layer_list_does_not_stop_archicad_info() -> None:
    """The layer breakdown is a convenience. Losing it must not cost the
    connection check, the zone count and the classification report."""
    import typer.testing

    from sun_study.archicad.connection import ArchicadConnection

    class Broken:
        def send(self, payload: dict[str, Any]) -> dict[str, Any]:
            name = payload["parameters"]["addOnCommandId"]["commandName"]
            if name == "GetAddOnVersion":
                return {"succeeded": True, "result": {"addOnCommandResponse": {"version": "1.5.7"}}}
            return {"succeeded": False, "error": {"code": 1, "message": "nope"}}

    from sun_study.cli import _report_zone_layers

    app = typer.Typer()

    @app.command()
    def show() -> None:
        _report_zone_layers(ArchicadConnection(Broken()), _zones_on(1, 3))

    result = typer.testing.CliRunner().invoke(app, [])
    assert result.exit_code == 0
    assert "could not read the layer list" in result.output


def test_one_running_archicad_is_used_without_being_named() -> None:
    """Archicad hands each instance its own port, so the default is right only
    for whichever started first. With exactly one running there is nothing to
    choose between, and erroring out to say 'pass --port 19724' asks a person
    to type what the tool already knows."""
    import sun_study.cli as cli
    from sun_study.archicad.connection import ArchicadNotRunningError, Instance

    calls: list[int] = []

    class Reachable:
        def __init__(self, port: int = 0, **_: Any) -> None:
            calls.append(port)
            self.port = port

        def send(self, payload: dict[str, Any]) -> dict[str, Any]:
            if self.port != 19724:
                raise ArchicadNotRunningError("nothing there")
            return {"succeeded": True, "result": {"addOnCommandResponse": {"version": "1.5.7"}}}

    monkey = pytest.MonkeyPatch()
    monkey.setattr(cli, "HttpTransport", Reachable)
    monkey.setattr(cli, "find_instances", lambda *a, **k: (Instance(19724, "SAMPLE"),))
    try:
        connection = cli._connect(19723)
    finally:
        monkey.undo()

    assert connection.require_tapir() == "1.5.7"
    assert calls == [19723, 19724], "the default is tried first, then the one found"


def test_two_running_archicads_stop_the_run_rather_than_guess() -> None:
    """Picking one would be guessing which project the results belong in, and
    writing an assessment into the wrong file beats any amount of typing."""
    import typer

    import sun_study.cli as cli
    from sun_study.archicad.connection import ArchicadNotRunningError, Instance

    class Dead:
        def __init__(self, **_: Any) -> None:
            pass

        def send(self, payload: dict[str, Any]) -> dict[str, Any]:
            raise ArchicadNotRunningError("nothing there")

    monkey = pytest.MonkeyPatch()
    monkey.setattr(cli, "HttpTransport", Dead)
    monkey.setattr(
        cli,
        "find_instances",
        lambda *a, **k: (Instance(19724, "SAMPLE"), Instance(19725, "EXAMPLE")),
    )
    try:
        with pytest.raises(typer.Exit):
            cli._connect(19723)
    finally:
        monkey.undo()


def test_drawing_is_opt_in_like_writing() -> None:
    """Both change a colleague's project file."""
    import inspect

    from sun_study.cli import archicad_run

    assert inspect.signature(archicad_run).parameters["draw"].default is False


def test_the_self_test_uses_every_band_so_one_glance_checks_the_palette() -> None:
    """A self test that only exercised one colour would prove one third of the
    pen mapping and look like it had proved all of it."""
    from sun_study.archicad.draw import DEFAULT_BANDS, band_for
    from sun_study.archicad.read import ArchicadZone
    from sun_study.cli import _synthetic_assessment

    zones = [ArchicadZone(f"z{i}", "Living", f"{i}") for i in range(len(DEFAULT_BANDS))]
    assessment, match = _synthetic_assessment(zones, DEFAULT_BANDS)

    used = {band_for(a.governing_minutes, DEFAULT_BANDS).label for a in assessment.apartments}
    assert used == {band.label for band in DEFAULT_BANDS}
    assert match.by_apartment == {f"z{i}": f"z{i}" for i in range(len(DEFAULT_BANDS))}
    assert not match.unmatched and not match.ambiguous


def test_the_self_test_values_sit_inside_their_bands_not_on_the_edge() -> None:
    """On an edge, a rounding disagreement would look like a banding bug."""
    from sun_study.archicad.draw import DEFAULT_BANDS

    zones_count = len(DEFAULT_BANDS)
    from sun_study.archicad.read import ArchicadZone
    from sun_study.cli import _synthetic_assessment

    zones = [ArchicadZone(f"z{i}", "Living", f"{i}") for i in range(zones_count)]
    assessment, _ = _synthetic_assessment(zones, DEFAULT_BANDS)

    edges = {band.upper_minutes for band in DEFAULT_BANDS}
    assert not (edges & {a.governing_minutes for a in assessment.apartments if a.governing_minutes})


def test_the_self_test_marks_everything_as_not_a_measurement() -> None:
    from sun_study.archicad.draw import DEFAULT_BANDS
    from sun_study.archicad.read import ArchicadZone
    from sun_study.cli import _synthetic_assessment

    assessment, _ = _synthetic_assessment([ArchicadZone("z", "L", "1")], DEFAULT_BANDS)
    assert assessment.ruleset_name == "SELFTEST"
    assert all(a.note == "SELFTEST" for a in assessment.apartments)


def test_a_georeference_mismatch_can_be_overridden_but_only_deliberately() -> None:
    import inspect

    from sun_study.cli import archicad_run

    parameter = inspect.signature(archicad_run).parameters["allow_georeference_mismatch"]
    assert parameter.default is False


def test_a_layer_name_with_a_trailing_space_still_matches() -> None:
    """Archicad layer names carry trailing spaces more often than anyone
    expects, and a padded listing hides them completely -- so a name copied
    character-perfect off the screen matched nothing and the run reported a
    project with no apartments."""
    from sun_study.cli import layer_matches

    assert layer_matches("06 | Zone.SEPP 65 ", ["06 | Zone.SEPP 65"])
    assert layer_matches("06 | Zone.SEPP 65", [" 06 | Zone.SEPP 65 "])
    assert layer_matches("06 | ZONE.SEPP 65", ["06 | Zone.SEPP 65"]), "and case-insensitively"
    assert not layer_matches("06 | Zone.Units", ["06 | Zone.SEPP 65"])


def test_layer_index_zero_is_a_real_layer() -> None:
    """`names.get(zone.layer_index or -1)` was the original, and 0 or -1 is
    -1 -- so every zone on layer 0 silently became 'unknown'."""
    from sun_study.archicad.read import ArchicadZone
    from sun_study.cli import layer_of

    on_zero = ArchicadZone(guid="z", name="RESI", number="", layer_index=0)
    assert layer_of(on_zero, {0: "00 | Ground"}) == "00 | Ground"

    unreported = ArchicadZone(guid="z", name="RESI", number="", layer_index=None)
    assert layer_of(unreported, {0: "00 | Ground"}) == "(no layer reported)"


def test_a_layer_listing_quotes_the_names_it_prints() -> None:
    """So a trailing space is visible in the thing being copied."""
    import typer.testing

    from sun_study.archicad.connection import ArchicadConnection
    from sun_study.cli import _report_zone_layers

    connection = ArchicadConnection(_LayerTransport({1: "06 | Zone.Units "}))
    app = typer.Typer()

    @app.command()
    def show() -> None:
        _report_zone_layers(connection, _zones_on(1, 3))

    assert "'06 | Zone.Units '" in typer.testing.CliRunner().invoke(app, []).output


def test_zones_can_be_narrowed_by_name_as_well_as_layer() -> None:
    """A layer mixes dwellings and balconies. On one project '06 | Zone.Units'
    holds 15 units and 20 balconies, told apart only by name, and assessing a
    balcony as an apartment is silent -- it just has no living room."""
    from sun_study.archicad.read import ArchicadZone

    zones = [
        ArchicadZone(guid="u1", name="G08", number="", layer_index=1),
        ArchicadZone(guid="b1", name="BY", number="", layer_index=1),
        ArchicadZone(guid="b2", name="BY", number="", layer_index=1),
    ]
    wanted = {"g08"}
    kept = [zone for zone in zones if zone.name.strip().casefold() in wanted]

    assert [zone.guid for zone in kept] == ["u1"]


def test_the_rooms_command_takes_a_zone_name_filter() -> None:
    import inspect

    from sun_study.cli import archicad_rooms

    assert "zone_name" in inspect.signature(archicad_rooms).parameters


def test_a_write_failure_does_not_cancel_the_drawing() -> None:
    """A fill is geometry on a layer: it needs no property, no classification
    and no schedule.

    The bug: a real run asking for --write --draw against a project that had
    never had init-properties produced neither the values nor the picture, and
    the picture was the part that needed nothing. The exit code still reports
    the failure.
    """
    import inspect

    from sun_study.cli import archicad_run

    source = inspect.getsource(archicad_run)
    write_at = source.index("write_assessment(connection")
    draw_at = source.index("draw_assessment(")
    between = source[write_at:draw_at]

    assert "if not draw:" in between, (
        "the write's error path must check whether a drawing was also asked for"
    )
    assert "Continuing to the drawing" in between


# -- the statistics sheet -------------------------------------------------
# The numbers a reader quotes. Worth testing apart from the drawing, because
# deciding what belongs on the page is the interesting half and it needs no
# Archicad to check.


def _summary(monkeypatch: Any) -> list[tuple[str, str]]:
    import datetime as dt
    from types import SimpleNamespace

    from sun_study.cli import statistics_rows
    from sun_study.rules.assessment import ApartmentResult, BuildingAssessment
    from sun_study.rules.ruleset import Continuity

    def flat(name: str, living: float, outside: float | None) -> ApartmentResult:
        return ApartmentResult(
            apartment_id=name,
            apartment_name=name,
            living_room_minutes=living,
            open_space_minutes=outside,
            governing_minutes=living,
            meets_minimum=living >= 120,
            receives_no_sunlight=living <= 0,
            counted=True,
        )

    found = BuildingAssessment(
        ruleset_name="nsw_adg",
        ruleset_version="1.0.0",
        area_key="sydney_metro",
        area_label="Sydney Metropolitan Area",
        minimum_minutes=120.0,
        continuity=Continuity.CUMULATIVE,
        apartments=(flat("a", 30.0, 220.0), flat("b", 90.0, None), flat("c", 150.0, 10.0)),
        counted_total=3,
        meeting_minimum=1,
        with_no_sunlight=0,
        compliant_share=1 / 3,
        no_sunlight_share=0.0,
        required_share=0.7,
        maximum_no_sunlight_share=0.15,
    )
    result = SimpleNamespace(assessment=found, assessment_date=dt.date(2024, 6, 21))
    return statistics_rows(result)  # type: ignore[arg-type]


def test_the_summary_carries_the_settings_beside_the_result(monkeypatch: Any) -> None:
    """A share of apartments means nothing without the threshold, the date and
    the ruleset it was measured against. A sheet giving the answer alone
    invites somebody to quote it as though it were the consultant's."""
    said = dict(_summary(monkeypatch))

    assert said["Assessed on"] == "21 June 2024"
    assert said["Ruleset"] == "nsw_adg@1.0.0"
    assert said["Minimum per apartment"] == "120 min"
    assert said["Target share"] == "70% of apartments"


def test_the_summary_reports_the_verdict_and_the_counts(monkeypatch: Any) -> None:
    said = dict(_summary(monkeypatch))

    assert said["Apartments assessed"] == "3"
    assert said["Meeting the minimum"].startswith("1")
    assert said["Result"] == "DOES NOT COMPLY"


def test_the_summary_counts_apartments_with_no_open_space(monkeypatch: Any) -> None:
    """They are assessed on the living-room limb alone, which is a weaker
    result than it looks, so the count belongs on the page."""
    said = dict(_summary(monkeypatch))

    assert said["Apartments without open space"] == "1"
    assert said["Living room, range"].startswith("30 - 150")
    assert said["Private open space, range"].startswith("10 - 220")


def test_the_summary_signs_itself(monkeypatch: Any) -> None:
    """Whose tool produced the numbers, and the disclaimer, on the sheet
    itself -- a page of figures outlives the session that made it."""
    from sun_study import AUTHOR

    labels = [label for label, _ in _summary(monkeypatch)]
    assert any(AUTHOR in label for label in labels)
    assert any("PROTOTYPE" in label for label in labels)
