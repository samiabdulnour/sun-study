"""The term this build runs under, and what it does and does not say.

Two properties, and they pull in opposite directions, which is why both are
pinned here rather than left to whoever edits ``licence.py`` next.

*Silent while valid.* Nothing counts down. A tool that warns about its own
expiry every run is noise on a tool somebody uses twice a week for a year, and
the term is not the subject of the software.

*Plain when it fires.* The person it stops is an architect on a deadline. A
build that refuses to run and says why costs them a phone call; one that
refuses and pretends to be broken costs them a day.
"""

from __future__ import annotations

import datetime as dt
import subprocess
import sys

import pytest
from typer.testing import CliRunner

from sun_study import AUTHOR, licence
from sun_study.cli import app

runner = CliRunner()


def test_the_build_runs_up_to_the_last_day_of_its_term() -> None:
    assert not licence.has_expired(dt.date(2026, 12, 31))


def test_the_build_stops_on_the_date_itself() -> None:
    """The date is the end of the term, not the last day of it."""
    assert licence.has_expired(dt.date(2027, 1, 1))
    assert licence.has_expired(dt.date(2027, 6, 30))


def test_check_is_silent_inside_the_term() -> None:
    """Raises nothing -- the run proceeds as though this module were not
    there."""
    licence.check(dt.date(2026, 12, 31))


def test_check_raises_once_the_term_is_over() -> None:
    with pytest.raises(licence.LicenceExpiredError):
        licence.check(dt.date(2027, 1, 2))


def test_what_it_says_names_the_date_the_reason_and_who_to_ask() -> None:
    """Everything needed to act on it, and nothing that reads as a fault to be
    worked around: there is no setting to change and no bug to report."""
    said = licence.explain()

    assert "01 January 2027" in said
    assert AUTHOR in said
    assert "licence" in said.lower()
    assert "Nothing is wrong with the model" in said


# ---------------------------------------------------------------------------
# Silent while valid. The half that is easy to break by accident.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("command", ["--help", "rulesets"])
def test_nothing_on_screen_mentions_the_term(command: str) -> None:
    """A banner, a countdown or a line in --help would all be regressions.

    The tool is inside its term when this suite runs, and inside its term it
    must look exactly as it did before the term existed.
    """
    invocation = runner.invoke(app, [command])

    assert invocation.exit_code == 0
    lowered = invocation.output.lower()
    for word in ("licence", "license", "expire", "expiry", "2027"):
        assert word not in lowered, f"{word!r} reached the screen inside the term"


def test_the_expiry_is_not_advertised_in_any_command_help() -> None:
    """Not on the commands a colleague runs either -- --help is read far more
    often than it is written, and one stray line there is the whole warning
    this is meant not to give."""
    for command in ("massing", "archicad-run", "run", "info"):
        invocation = runner.invoke(app, [command, "--help"])
        assert invocation.exit_code == 0
        assert "2027" not in invocation.output
        assert "licence" not in invocation.output.lower()


# ---------------------------------------------------------------------------
# The entry points. Both doors, because the packaged build uses both.
# ---------------------------------------------------------------------------
EXPIRED_CLOCK = """
import datetime as dt
from sun_study import licence

class Frozen(dt.date):
    @classmethod
    def today(cls):
        return cls(2027, 3, 1)

dt.date = Frozen
licence.dt.date = Frozen
"""


def _run_entry_point(entry: str) -> subprocess.CompletedProcess[str]:
    """The real ``main`` of one entry point, with the clock wound forward."""
    script = f"{EXPIRED_CLOCK}\n{entry}\n"
    return subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_the_command_line_refuses_and_says_why() -> None:
    """A non-zero exit, so a scripted run cannot mistake it for a study that
    found nothing, and the reason on stderr where a script's log keeps it."""
    finished = _run_entry_point(
        "import sys; sys.argv = ['sun-study', 'rulesets']\n"
        "from sun_study.cli import main\n"
        "try:\n"
        "    main()\n"
        "except SystemExit as stop:\n"
        "    print('exit', stop.code)\n"
    )

    assert "01 January 2027" in finished.stderr
    assert AUTHOR in finished.stderr
    assert "exit 4" in finished.stdout


def test_an_expired_build_stops_before_it_opens_anything() -> None:
    """Before the banner and before any Archicad call.

    A build past its term must not get as far as reading somebody's project,
    let alone writing to it -- so the refusal happens at the door rather than
    in the middle of a run that has already rearranged their layers.
    """
    finished = _run_entry_point(
        "import sys; sys.argv = ['sun-study', 'archicad-info']\n"
        "from sun_study.cli import main\n"
        "try:\n"
        "    main()\n"
        "except SystemExit:\n"
        "    pass\n"
    )

    assert "01 January 2027" in finished.stderr
    assert "port" not in finished.stdout.lower(), "it should never have reached Archicad"


def test_the_window_child_process_refuses_too() -> None:
    """``cli.main`` is not what starts a run launched from the window, so the
    child needs its own check or the window path has none at all."""
    finished = _run_entry_point(
        "import os, sys\n"
        "os.environ['SUN_STUDY_RUN_CLI'] = '1'\n"
        "sys.frozen = True\n"
        "sys.argv = ['sun-study', 'rulesets']\n"
        "from sun_study.app.__main__ import main\n"
        "try:\n"
        "    main()\n"
        "except SystemExit as stop:\n"
        "    print('exit', stop.code)\n"
    )

    assert "01 January 2027" in finished.stderr
    assert "exit 4" in finished.stdout


def test_the_window_says_it_in_a_box_and_never_opens(monkeypatch: pytest.MonkeyPatch) -> None:
    """The half a print cannot reach.

    A gui-script has no console, so an expired build that only printed would
    look like an executable that does nothing when double-clicked -- which is
    precisely the inscrutable failure this is meant not to be.
    """
    from sun_study.app import __main__ as entry

    monkeypatch.setattr(licence, "has_expired", lambda today=None: True)

    said: list[str] = []
    monkeypatch.setattr(entry, "_say_and_stop", said.append)

    opened = []
    monkeypatch.setattr("sun_study.app.window.launch", lambda: opened.append(True), raising=False)

    entry.main()

    assert said and "01 January 2027" in said[0]
    assert not opened, "an expired build must not get as far as a window"


def test_the_window_opens_as_normal_inside_the_term(monkeypatch: pytest.MonkeyPatch) -> None:
    """The other side of it, so a broken check cannot lock everybody out."""
    from sun_study.app import __main__ as entry

    monkeypatch.setattr(licence, "has_expired", lambda today=None: False)

    opened = []
    monkeypatch.setattr("sun_study.app.window.launch", lambda: opened.append(True))

    entry.main()

    assert opened == [True]
