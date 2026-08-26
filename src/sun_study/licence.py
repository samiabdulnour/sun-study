"""When this build stops working, and how it says so.

Sun Study goes out to offices as a packaged executable, and this is the term
on that build. Past the date below it refuses to run and asks whoever has it
to come back for a current one.

Why a build expires at all
--------------------------
It is a PROTOTYPE, and it says so on every sheet it draws. The disclaimer is
the honest version of that while somebody is reading it; this is the version
that still works in two years, when the executable has been copied to a
network drive, the person who was told what it is has moved on, and it is
producing ADG figures for a DA nobody here has seen. An unmaintained tool
that quietly keeps answering is worse than one that stops.

What is deliberately not here
-----------------------------
Nothing counts down. There is no banner, no nag, no line in ``--help``, and
nothing in the window while the build is inside its term -- the tool behaves
exactly as though this module did not exist. That is the point: the term is
not the subject of the software, and a warning every run would be noise on a
tool somebody uses twice a week for a year.

What *is* here is the message it gives when it fires, and it names the reason.
Refusing to run is a decision this tool is entitled to make; refusing to run
while pretending to be broken is not. The person it stops is an architect on a
deadline, and the difference between "the licence ran out on 1 January 2027"
and an inscrutable failure is a phone call versus a day lost to a bug hunt.

How much protection this is
---------------------------
A speed bump. It reads the system clock, so winding the clock back defeats it,
and a PyInstaller bundle can be unpacked by anyone who wants to. It is here to
stop a stale build being used by accident, which is the thing that actually
happens. It is not a copy protection scheme and should not be sold as one.
"""

from __future__ import annotations

import datetime as dt

from sun_study import AUTHOR, PRODUCT

__all__ = ["EXPIRES", "LicenceExpiredError", "check", "has_expired"]

#: The last day this build runs. Chosen as a term, not a countdown -- see the
#: module docstring for why a prototype gets one at all.
EXPIRES = dt.date(2027, 1, 1)


class LicenceExpiredError(Exception):
    """Raised at the entry point when the build is past its term.

    Its own type, and not an ``ArchicadError`` or a ``typer.Exit``, because
    the two entry points report it differently -- the command line to stderr,
    the window in a dialog box, neither having a console the other can use.
    """


def has_expired(today: dt.date | None = None) -> bool:
    """Whether this build is past its term.

    ``today`` is injectable so the behaviour either side of the date can be
    tested without touching the machine's clock.
    """
    return (today or dt.date.today()) >= EXPIRES


def check(today: dt.date | None = None) -> None:
    """Let the run proceed, or raise ``LicenceExpiredError``. Silent while valid."""
    if has_expired(today):
        raise LicenceExpiredError(explain())


def explain() -> str:
    """What the person holding an expired build is told.

    Names the date and who to ask, and nothing else. There is no fault to
    report and no setting to change, so anything further would only read as an
    error to be worked around.
    """
    return (
        f"{PRODUCT}'s licence period ended on {EXPIRES:%d %B %Y} and this "
        f"build no longer runs. Nothing is wrong with the model or the "
        f"project -- the build itself has reached the end of its term. "
        f"Contact {AUTHOR} for a current one."
    )
