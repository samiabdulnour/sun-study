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
layout in the project.

One module because five others need the same string and none of them should
have to import another to get it.

Changing it
-----------
``14`` is right for a project whose groups end at 13. Another office numbers
differently, so this is the one place to change, and everything -- layers,
combinations, surfaces, views, layouts, and the search that cleans up after a
run -- follows from it.
"""

from __future__ import annotations

#: Leads the name of everything this tool creates. No trailing space: the
#: callers add one, because the search that finds the tool's own work matches
#: ``f"{TOOL_PREFIX} "`` and a prefix with the space baked in would also match
#: a layer somebody named ``14 |Something``.
TOOL_PREFIX = "14 |"

#: The group the layers belong to, under the prefix. Layers are named
#: ``group.part`` on the reference project -- ``01 | Wall.External``,
#: ``06 | Zone.Units`` -- so the study's follow: ``14 | Sun Study.Results``.
LAYER_GROUP = f"{TOOL_PREFIX} Sun Study"


def layer(part: str) -> str:
    """One of the tool's layers, named the way the project names layers."""
    return f"{LAYER_GROUP}.{part}"


def named(what: str) -> str:
    """A combination, view, layout or surface this tool makes."""
    return f"{TOOL_PREFIX} {what}"
