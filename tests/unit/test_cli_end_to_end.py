"""The whole chain through the CLI, on the fixture.

Milestone M3's acceptance criterion is per-apartment results for the fixture
building, so this exercises the actual entry point a person types rather than
the library underneath it.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from sun_study.cli import app
from sun_study.disclaimer import STATUS
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


def test_a_mismatched_timezone_is_rejected() -> None:
    """Two different zones in one run would be silently wrong."""
    from sun_study.ingest.scene import SceneConfig

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
