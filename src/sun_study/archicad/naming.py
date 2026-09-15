"""What this tool calls the things it makes, in one place.

Why a module of its own
-----------------------
Every layer, layer combination, surface, view and layout the tool creates
carries one prefix, and that prefix does two jobs at once.

It **files the output inside the office's own numbering**. On the reference
project the layer groups run ``00 |`` to ``13 |``, each with a divider layer of
its own (``13 ------------------------------ HOTLINKS``), and the layer
combinations follow the same scheme. Output called ``SS Sun Study 09:00``
sorts nowhere and reads as somebody's initials; ``14 | Sun Study 09:00`` sits
at the end of a list a colleague already knows how to read.

And it is **how a rerun finds its own work**. ``remove_previous`` deletes the
navigator items whose name starts with it, so the prefix is the whole
guarantee that a run clears its own sheets and not the practice's. That is why
it cannot simply be dropped: an empty prefix matches every view and every
layout in the project, and ``set_prefix`` refuses one for that reason.

One module because six others need the same string and none of them should
have to import another to get it.

Why it is a function and not a constant
---------------------------------------
``14`` is right for a project whose groups end at 13 and wrong for the next
office, so it is chosen per run -- ``--layer-prefix``, or the field in the
window. That makes a module-level ``TOOL_PREFIX = "14 |"`` a trap rather than
a convenience: every ``FACADE_LAYER = layer("Facade")`` elsewhere would freeze
the default at import, before a command line has been read, and the run would
draw on one layer while its clean-up looked at another. So the prefix is read
through a call, every time, and the modules that used to hold a derived
constant hold a function instead.

It is process-wide state, set once before any work, rather than an argument
threaded through forty call sites -- the same shape as the setting it
represents. A run measures one project with one prefix; nothing here is
re-entrant and nothing needs to be.
"""

from __future__ import annotations

#: What the prefix is when nobody says otherwise. Right for the reference
#: project, whose layer groups end at 13.
DEFAULT_PREFIX = "14 |"

#: And one number per tool, because the four of them are four drawings read by
#: different people. Filing them all under ``14 |`` put the apartment results,
#: the shadow diagram, the sun views and the site analysis in one run of the
#: layer list, where the only thing telling them apart was the group word
#: after the number -- and a colleague looking for the shadow sheets had to
#: know to read past ``14 |`` to find them.
#:
#: The solar analysis keeps ``14 |`` so nothing anybody has already drawn
#: moves; the other three step up from it. Each is only a *default*:
#: ``--layer-prefix`` still wins, and an office whose own groups run past 17
#: says so once per tool rather than being argued with.
#:
#: They are deliberately separate constants rather than a dict keyed by
#: command name. The command names are the CLI's business and change with it;
#: what a number means -- "this is the shadow diagram's" -- does not.
SOLAR_PREFIX = DEFAULT_PREFIX
SHADOW_PREFIX = "15 |"
SUN_VIEW_PREFIX = "16 |"
SITE_PREFIX = "17 |"

#: And one number that is *not* the tool's, which is why it is kept apart from
#: the four above. The context a study stands in -- the neighbours and the
#: ground under them -- belongs to the site group the office template already
#: has, not to a group the sun study invented: on the reference project that
#: is ``03 | Site Context.3D``, sitting under ``03 ---------- SITE`` with the
#: boundaries, the setbacks and the survey mesh.
#:
#: Writing the fetched context there rather than onto a ``LORIINI`` layer of
#: its own means a colleague finds the neighbours where neighbours live, and
#: the layer that is already in the template is *used* rather than duplicated
#: -- ``ensure_layer`` finds it by name and only creates when there is nothing
#: to find. Point this at a group the project does not have, ``50 |``, and the
#: layers are made there instead; that is the same call either way.
#:
#: The part after the number is the template's own wording and is not settable.
#: The number is, because it is the one thing that differs between offices.
CONTEXT_PREFIX = "03 |"

#: What the context parts are called under it. ``Future buildings`` is the
#: spelling the shadow tool's own worked example already uses, so a legend row
#: written from that example finds the geometry without being told twice.
CONTEXT_PART = "Site Context.3D"
FUTURE_PART = "Site Context.Future buildings"

_context_prefix = CONTEXT_PREFIX

#: The word between the prefix and the part, in a layer name. Not settable:
#: it is what the tool *is*, while the prefix is where the office keeps it.
GROUP_WORD = "Solar Analysis"

#: And what the *shadow* study is, which is a different drawing answering a
#: different question. A shadow diagram is about the neighbourhood -- what the
#: proposal darkens that was not dark before -- while the solar analysis is
#: about the apartments inside it. Filing both under one word made a layer
#: list where the two could not be told apart, and a reader looking for the
#: shadow sheets had to know they were kept under something else.
SHADOW_WORD = "Shadow Diagram"

#: No trailing space: the callers add one, because the search that finds the
#: tool's own work matches ``f"{prefix()} "`` and a prefix with the space baked
#: in would also match a layer somebody named ``14 |Something``.
_prefix = DEFAULT_PREFIX

#: The day a run is for, when it is not the ruleset's own: ``21 Dec``. Empty
#: on the assessment day, so every name the tool has always made is unchanged
#: and a summer run cannot overwrite the midwinter one somebody is reading.
_day = ""


def set_day(tag: str | None) -> str:
    """Stamp the day on everything this run creates. Returns what was set.

    Pass the empty string, or ``None``, for the ruleset's own day: the names
    then say nothing about it, as they did before the option existed.
    """
    global _day
    if tag is None:
        return _day
    _day = " ".join(tag.split())
    return _day


def day() -> str:
    """The day tag in force, or the empty string on the assessment day."""
    return _day


def _stamped(name: str) -> str:
    """``name`` with the day on the end, unless it already says the day."""
    if not _day or _day in name:
        return name
    return f"{name} {_day}"


def set_prefix(value: str | None, *, default: str | None = None) -> str:
    """Choose the prefix for everything this run creates. Returns what was set.

    ``value`` is what the run asked for and wins. ``default`` is the calling
    tool's own number -- ``SHADOW_PREFIX`` and the rest -- used when the run
    asked for nothing. With neither, the prefix already in force stays, so a
    caller can pass an optional flag straight through as before.

    The tool's default is passed in rather than read from here because this
    module does not know which tool is running, and should not have to: the
    command knows what it is, and says so at the one point where it matters.

    Whitespace is collapsed rather than preserved: a name copied out of
    Archicad brings a double space with it often enough, and ``14  |`` would
    make every name this run creates invisible to the search that cleans it up
    next time.

    An empty prefix is refused. It is not a tidier name, it is
    ``remove_previous`` matching every view and every layout in the project --
    the practice's drawings deleted by a sun study's clean-up.
    """
    global _prefix
    if value is None and default is None:
        return _prefix
    chosen = " ".join((value if value is not None else default or "").split())
    if not chosen:
        raise ValueError(
            "The layer prefix cannot be empty: it is how a rerun finds its own "
            "views and layouts to delete, and an empty one matches every item "
            "in the project. Use something short and unmistakably this tool's, "
            f"such as {DEFAULT_PREFIX!r}."
        )
    _prefix = chosen
    return chosen


def prefix() -> str:
    """Leads the name of everything this tool creates."""
    return _prefix


def set_context_prefix(value: str | None) -> str:
    """Choose the site group the context is filed under. Returns what was set.

    ``None`` leaves it alone, the same shape as ``set_prefix``, so an optional
    flag passes straight through. Empty is refused for a different reason than
    an empty tool prefix: nothing here searches by it, but ``| Site
    Context.3D`` is not a layer name anybody meant to type.
    """
    global _context_prefix
    if value is None:
        return _context_prefix
    chosen = " ".join(value.split())
    if not chosen:
        raise ValueError(
            "The context prefix cannot be empty: it is the office's own site "
            "group, the number in front of 'Site Context.3D'. Use the group "
            f"the template keeps the neighbours in, such as {CONTEXT_PREFIX!r}."
        )
    _context_prefix = chosen
    return chosen


def context_prefix() -> str:
    """The site group the context is filed under."""
    return _context_prefix


def context_layer() -> str:
    """Where the neighbouring buildings and the ground under them go."""
    return f"{_context_prefix} {CONTEXT_PART}"


def future_layer() -> str:
    """Where the envelopes the controls would allow go.

    A layer of its own beside the context, not mixed into it: a future
    neighbour is a question a saved view answers -- with, without -- and not
    something the model asserts is there.
    """
    return f"{_context_prefix} {FUTURE_PART}"


def group(word: str = GROUP_WORD) -> str:
    """The group the tool's layers belong to, under the prefix.

    Layers are named ``group.part`` on the reference project -- ``01 |
    Wall.External``, ``06 | Zone.Units`` -- so the study's follow:
    ``14 | Solar Analysis.Results``.

    ``word`` says which study, because the tool makes two that are read by
    different people for different reasons. It defaults to the solar analysis
    because most of the tool is that; the shadow drawings pass
    ``SHADOW_WORD``.

    On a day other than the ruleset's the group carries it -- ``14 | Solar
    Analysis 21 Dec.Results`` -- so both days' layers stand side by side.
    """
    return _stamped(f"{_prefix} {word}")


def layer(part: str, word: str = GROUP_WORD) -> str:
    """One of the tool's layers, named the way the project names layers."""
    return f"{group(word)}.{part}"


def named(what: str) -> str:
    """A combination, view, layout or surface this tool makes.

    Carries the day when the run is not on the ruleset's own, unless the
    name already says it, as a sun view named for its instant does.
    """
    return _stamped(f"{_prefix} {what}")
