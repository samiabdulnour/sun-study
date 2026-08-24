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

#: The word between the prefix and the part, in a layer name. Not settable:
#: it is what the tool *is*, while the prefix is where the office keeps it.
GROUP_WORD = "Sun Study"

#: No trailing space: the callers add one, because the search that finds the
#: tool's own work matches ``f"{prefix()} "`` and a prefix with the space baked
#: in would also match a layer somebody named ``14 |Something``.
_prefix = DEFAULT_PREFIX


def set_prefix(value: str | None) -> str:
    """Choose the prefix for everything this run creates. Returns what was set.

    ``None`` or an unset option leaves the default alone, so a caller can pass
    an optional flag straight through.

    Whitespace is collapsed rather than preserved: a name copied out of
    Archicad brings a double space with it often enough, and ``14  |`` would
    make every name this run creates invisible to the search that cleans it up
    next time.

    An empty prefix is refused. It is not a tidier name, it is
    ``remove_previous`` matching every view and every layout in the project --
    the practice's drawings deleted by a sun study's clean-up.
    """
    global _prefix
    if value is None:
        return _prefix
    chosen = " ".join(value.split())
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


def group() -> str:
    """The group the tool's layers belong to, under the prefix.

    Layers are named ``group.part`` on the reference project -- ``01 |
    Wall.External``, ``06 | Zone.Units`` -- so the study's follow:
    ``14 | Sun Study.Results``.
    """
    return f"{_prefix} {GROUP_WORD}"


def layer(part: str) -> str:
    """One of the tool's layers, named the way the project names layers."""
    return f"{group()}.{part}"


def named(what: str) -> str:
    """A combination, view, layout or surface this tool makes."""
    return f"{_prefix} {what}"
