"""The days a solar study is made for, by name.

A ruleset fixes one day: the ADG's is midwinter, 21 June, the worst case and
the one the controls turn on. A council asks for the equinox and midsummer
alongside it on a shadow diagram, and a designer wants the same three days on
the apartment plans, the facade bands and the sun views to see the year rather
than its worst hour. So every solar tool takes a day, and this is where the
names live, once, so ``winter`` on the command line, in the window and on a
sheet all mean the same date.

The dates are the drawing convention -- the 21st of June, September and
December -- not the astronomical instants, which drift a day or two either
way. A council that asks for the 22nd says so with the ``MM-DD`` form, which
every tool also takes.
"""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sun_study.rules.ruleset import Ruleset

#: Name -> (MM-DD, what a sheet calls it). Order is the order a window lists
#: them in: the assessment day first.
ASSESSMENT_DAYS: dict[str, tuple[str, str]] = {
    "winter": ("06-21", "Winter solstice, 21 June"),
    "equinox": ("09-21", "Spring equinox, 21 September"),
    "summer": ("12-21", "Summer solstice, 21 December"),
}

#: Other words for the same three days, as people type them.
_ALIASES = {
    "midwinter": "winter",
    "winter solstice": "winter",
    "june": "winter",
    "spring": "equinox",
    "spring equinox": "equinox",
    "september": "equinox",
    "midsummer": "summer",
    "summer solstice": "summer",
    "december": "summer",
}

#: What the option's help says. Built once so the four commands agree.
DAY_HELP = (
    "The day to study: winter (21 June, the ruleset's own), equinox "
    "(21 September) or summer (21 December), or any MM-DD. Every name, "
    "layer and property a run other than the ruleset's day makes carries "
    "the day, so the two do not overwrite each other."
)


def parse_day(text: str) -> str:
    """``winter``, ``equinox``, ``summer`` or ``MM-DD`` -> ``MM-DD``.

    Raises ``ValueError`` with the whole vocabulary in the message, because
    the caller is an option parser and the message is what the user sees.
    """
    wanted = " ".join(text.split()).lower()
    wanted = _ALIASES.get(wanted, wanted)
    if wanted in ASSESSMENT_DAYS:
        return ASSESSMENT_DAYS[wanted][0]
    try:
        month, day = (int(piece) for piece in wanted.split("-"))
        dt.date(2024, month, day)  # a leap year, so 02-29 is a day
    except ValueError as bad:
        names = ", ".join(ASSESSMENT_DAYS)
        raise ValueError(f"A day is one of {names}, or MM-DD. {text!r} is neither.") from bad
    return f"{month:02d}-{day:02d}"


def day_name(mmdd: str) -> str:
    """The short name for a date, or the date itself when it has none."""
    for name, (known, _) in ASSESSMENT_DAYS.items():
        if known == mmdd:
            return name
    return mmdd


def day_title(mmdd: str) -> str:
    """What a sheet or a summary calls the day: ``Winter solstice, 21 June``."""
    for known, title in ASSESSMENT_DAYS.values():
        if known == mmdd:
            return title
    month, day = (int(piece) for piece in mmdd.split("-"))
    return dt.date(2024, month, day).strftime(f"{day} %B")


def day_tag(mmdd: str) -> str:
    """The short stamp a name carries for the day: ``21 Jun``, ``21 Dec``."""
    month, day = (int(piece) for piece in mmdd.split("-"))
    return dt.date(2024, month, day).strftime(f"{day} %b")


def with_day(ruleset: Ruleset, mmdd: str) -> Ruleset:
    """The same rules, assessed on another day.

    A ruleset is frozen, so this is a copy. Nothing else changes: the window,
    the step and the thresholds are the ruleset's, which is why a caller
    that reports the result must say the day is not the ruleset's own.
    """
    if mmdd == ruleset.assessment.date:
        return ruleset
    return ruleset.model_copy(
        update={"assessment": ruleset.assessment.model_copy(update={"date": mmdd})}
    )
