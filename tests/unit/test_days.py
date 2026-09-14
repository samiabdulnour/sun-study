"""The day a solar study is made for, and what a run on another day carries.

The ADG fixes one day. A council asks for the equinox and midsummer beside it,
and a designer wants the year, not its worst hour. So every solar tool takes a
day, and the three things worth holding are: that the words people use reach
the same date; that a run on another day is stamped on everything it makes,
so it cannot overwrite the midwinter run someone is reading; and that the
summer day, on daylight saving time, is still the clock hours a council names.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator

import pytest
from typer.testing import CliRunner

from sun_study.archicad import naming
from sun_study.archicad.draw import default_layer_name
from sun_study.archicad.write import PROPERTY_GROUP_NAME, property_group_name
from sun_study.cli import _shadow_moments, app
from sun_study.core.solar import assessment_times
from sun_study.rules.days import (
    ASSESSMENT_DAYS,
    day_name,
    day_tag,
    day_title,
    parse_day,
    with_day,
)
from sun_study.rules.ruleset import load_ruleset
from tests.unit.test_cli_end_to_end import SAMPLE


@pytest.fixture(autouse=True)
def on_the_rulesets_day() -> Iterator[None]:
    """The day stamp is process-wide, like the prefix, so it is put back."""
    yield
    naming.set_day("")


def test_the_three_days_are_the_drawing_convention() -> None:
    """The 21st of each, as the shadow diagrams have always been drawn; the
    astronomical instants drift a day either way and nobody draws to them."""
    assert [mmdd for mmdd, _ in ASSESSMENT_DAYS.values()] == ["06-21", "09-21", "12-21"]
    assert list(ASSESSMENT_DAYS) == ["winter", "equinox", "summer"]


@pytest.mark.parametrize(
    ("said", "mmdd"),
    [
        ("winter", "06-21"),
        ("Winter", "06-21"),
        ("midwinter", "06-21"),
        ("winter solstice", "06-21"),
        ("equinox", "09-21"),
        ("spring", "09-21"),
        ("Spring equinox", "09-21"),
        ("summer", "12-21"),
        ("midsummer", "12-21"),
        ("12-21", "12-21"),
        ("9-22", "09-22"),
    ],
)
def test_every_way_people_say_a_day_reaches_the_same_date(said: str, mmdd: str) -> None:
    assert parse_day(said) == mmdd


def test_a_day_that_is_none_of_those_is_refused_with_the_vocabulary() -> None:
    for attempt in ("autumn", "21 June", "13-01", "02-30", ""):
        with pytest.raises(ValueError, match="winter, equinox, summer, or MM-DD"):
            parse_day(attempt)


def test_a_date_is_named_titled_and_tagged_for_a_sheet() -> None:
    assert day_name("06-21") == "winter"
    assert day_name("09-22") == "09-22"
    assert day_title("12-21") == "Summer solstice, 21 December"
    assert day_title("09-22") == "22 September"
    assert day_tag("06-21") == "21 Jun"
    assert day_tag("12-21") == "21 Dec"


def test_another_day_changes_the_date_and_nothing_else() -> None:
    """The window, the step and the thresholds stay the ruleset's, which is
    exactly why a run on another day has to say it is not the criterion."""
    adg = load_ruleset("nsw_adg")
    summer = with_day(adg, "12-21")

    assert summer.assessment.date == "12-21"
    assert summer.assessment.date_in(2024) == dt.date(2024, 12, 21)
    assert summer.assessment.window_start == adg.assessment.window_start
    assert summer.assessment.timestep_minutes == adg.assessment.timestep_minutes
    assert summer.criteria == adg.criteria
    assert with_day(adg, adg.assessment.date) is adg


def test_midsummer_in_sydney_is_on_daylight_saving_and_still_thirty_seven_instants() -> None:
    """21 December is AEDT, UTC+11. The window is the council's clock hours,
    so 09:00 is 09:00 on the wall and 22:00 the evening before in UTC."""
    adg = load_ruleset("nsw_adg").assessment
    times = assessment_times(
        dt.date(2024, 12, 21), "Australia/Sydney", adg.start_time, adg.end_time, 10
    )
    assert len(times) == 37
    assert times[0].utcoffset() == dt.timedelta(hours=11)
    assert times[0].astimezone(dt.UTC) == dt.datetime(2024, 12, 20, 22, 0, tzinfo=dt.UTC)
    winter = assessment_times(
        dt.date(2024, 6, 21), "Australia/Sydney", adg.start_time, adg.end_time, 10
    )
    assert winter[0].utcoffset() == dt.timedelta(hours=10)


def test_a_run_on_another_day_stamps_every_name_it_makes() -> None:
    """Layers, combinations, views, layouts and the property group all carry
    the day, so a summer run stands beside the midwinter one. A name that
    already says the day, as a sun view named for its instant does, is not
    told twice."""
    assert naming.set_day("21 Dec") == "21 Dec"

    assert naming.group() == "14 | Solar Analysis 21 Dec"
    assert naming.layer("Results") == "14 | Solar Analysis 21 Dec.Results"
    assert default_layer_name() == "14 | Solar Analysis 21 Dec.Results"
    assert naming.named("Sun Views") == "14 | Sun Views 21 Dec"
    assert naming.named("Sun View 21 Dec 09:00") == "14 | Sun View 21 Dec 09:00"
    assert property_group_name() == "Solar Analysis 21 Dec"


def test_the_rulesets_own_day_leaves_every_name_as_it_always_was() -> None:
    """Every value anybody has written so far is under the unstamped names;
    the option must not move them."""
    naming.set_day("")
    assert naming.day() == ""
    assert naming.layer("Results") == "14 | Solar Analysis.Results"
    assert property_group_name() == PROPERTY_GROUP_NAME
    assert naming.set_day(None) == ""


def test_the_shadow_days_take_the_same_words() -> None:
    moments, labels = _shadow_moments("winter, summer", "9,15", 2024, "Australia/Sydney")
    assert [m.date() for m in moments] == [dt.date(2024, 6, 21)] * 2 + [dt.date(2024, 12, 21)] * 2
    assert labels == ["9AM", "3PM", "9AM", "3PM"]
    assert moments[2].utcoffset() == dt.timedelta(hours=11)


def test_a_summer_run_says_loudly_that_it_is_not_the_criterion() -> None:
    """The figure is computed and reported, because a designer asked for it,
    and the ruleset's own day is named beside it so nobody quotes a summer
    percentage as an ADG one."""
    invocation = CliRunner().invoke(
        app, ["run", str(SAMPLE), "--timezone", "Australia/Sydney", "--date", "summer"]
    )
    assert invocation.exit_code == 0, invocation.output
    assert "assessed on Summer solstice, 21 December" in invocation.output
    assert "nsw_adg itself says Winter solstice, 21 June" in invocation.output
    assert "assessed 12-21" in invocation.output


def test_the_rulesets_own_day_by_name_is_not_a_warning() -> None:
    invocation = CliRunner().invoke(
        app, ["run", str(SAMPLE), "--timezone", "Australia/Sydney", "--date", "winter"]
    )
    assert invocation.exit_code == 0, invocation.output
    assert "not that ruleset's criterion" not in invocation.output
    assert "assessed 06-21" in invocation.output


def test_a_day_nobody_can_parse_is_a_usage_error_not_a_traceback() -> None:
    invocation = CliRunner().invoke(
        app, ["run", str(SAMPLE), "--timezone", "Australia/Sydney", "--date", "autumn"]
    )
    assert invocation.exit_code == 2
    # Typer boxes the message and wraps it, so one word of it is enough.
    assert "'autumn'" in invocation.output and "MM-DD" in invocation.output
