"""The layer state an export needs, set by the tool instead of by hand.

Why this exists
---------------
The IFC translator exports what the current layer combination *shows*. That
makes the combination somebody happened to leave active a silent input to
every number the study produces -- D52, where a site-plan combination gave an
export of 386 walls, 92 windows and no ``IfcSpace`` at all, and the run said
so several minutes later in the wrong words.

D52 concluded that nothing could be done from here, and that was wrong in a
particular way worth naming. It is true that a layer *combination* cannot be
activated: ``SetLayerCombination``, ``ApplyLayerCombination``, ``OpenView``,
``ActivateNavigatorItem`` and ``SetCurrentWindow`` are all unregistered on
Tapir 1.5.7. But a combination is only a set of per-layer visibilities, and
those *are* writable -- ``CreateLayers`` with ``overwriteExisting`` sets them,
which is how the facade skin already borrows a hidden layer. So the tool can
have the state a combination would have given it without activating one.

What is done
------------
``GetLayerCombinations`` reads any combination the project has, layer by
layer, so an office's own export combination can be applied by name without
Archicad being asked to switch to it. Failing that the tool uses its own,
which it creates once and reuses: **every layer visible and unlocked**, minus
whatever ``--hide-layer`` names.

Everything is snapshotted first and put back afterwards, in a ``finally``.
The point is that the export does not depend on what was on screen, and the
corollary is that the session must not depend on the export either.

Reuse, never recreate
---------------------
``CreateLayerCombinations`` makes one. ``DeleteAttributes`` will not remove
one: it accepts ``LayerCombination`` as a type -- a wrong spelling is refused
by name -- and then answers ``Attribute not found`` for an id that
``GetAttributesByType`` had just handed over. So a combination is looked up by
name and reused, exactly as layouts and views are, because a project that
accumulates one per run is worse than one that keeps a single stale one.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass

from sun_study.archicad.connection import ArchicadConnection, ArchicadError

__all__ = [
    "EXPORT_COMBINATION",
    "LayerPlan",
    "LayerState",
    "combination_states",
    "ensure_combination",
    "export_state",
    "read_layers",
    "shown_for_export",
]

#: The combination the tool makes when it is not pointed at one of the
#: project's own. Prefixed like everything else this tool leaves behind, so a
#: person can find all of it in one search.
EXPORT_COMBINATION = "SS Sun Study Export"


@dataclass(frozen=True)
class LayerState:
    """One layer, and whether it is switched off or locked right now."""

    identifier: str
    name: str
    hidden: bool = False
    locked: bool = False


@dataclass(frozen=True)
class LayerPlan:
    """What was changed for the export, and what it was changed from."""

    combination: str
    """Where the state came from: a project combination, or the tool's own."""

    shown: tuple[str, ...]
    """Layers switched on that had been off."""

    unlocked: tuple[str, ...]
    hidden: tuple[str, ...]
    """Layers switched off that had been on -- what ``--hide-layer`` named."""

    total: int = 0

    changed: int = 0
    """How many layers were touched -- counted once each.

    Not the three lists added up: a layer that was both hidden and locked
    appears in two of them, and summing gave "set 153 of 142 layers".
    """

    def describe(self) -> str:
        if not self.changed:
            return (
                f"  layers already as {self.combination!r} wants them; "
                f"the export does not depend on what was on screen"
            )
        lines = [
            f"  set {self.changed} of {self.total} layers to {self.combination!r} "
            f"for the export, and put every one of them back afterwards"
        ]
        for label, names in (
            ("switched on", self.shown),
            ("unlocked", self.unlocked),
            ("switched off", self.hidden),
        ):
            if names:
                more = f" and {len(names) - 6} more" if len(names) > 6 else ""
                shown = ", ".join(names[:6]) + more
                lines.append(f"    {label}: {shown}")
        return "\n".join(lines)


def read_layers(connection: ArchicadConnection) -> list[LayerState]:
    """Every layer in the project, with its current visibility.

    Two calls, because neither answers alone: ``GetAttributesByType``
    enumerates but reports only id, index and name -- its ``isHidden`` is
    absent, which is why reading it alone says nothing is hidden on a project
    where plenty is -- and ``GetLayers`` carries ``isHidden`` and ``isLocked``
    but requires the ids, so it cannot be used to search.
    """
    response = connection.run_tapir("GetAttributesByType", {"attributeType": "Layer"})
    attributes = response.get("attributes") if isinstance(response, dict) else None
    if not isinstance(attributes, list):
        raise ArchicadError(f"GetAttributesByType returned no layer list: {response!r}")

    known = [
        (str((entry.get("attributeId") or {}).get("guid", "")), str(entry.get("name", "")))
        for entry in attributes
        if isinstance(entry, dict) and (entry.get("attributeId") or {}).get("guid")
    ]
    if not known:
        return []

    states = connection.run_tapir(
        "GetLayers",
        {"attributeIds": [{"attributeId": {"guid": guid}} for guid, _ in known]},
    )
    rows = states.get("layers") if isinstance(states, dict) else None
    if not isinstance(rows, list) or len(rows) != len(known):
        raise ArchicadError(
            f"GetLayers answered for {len(rows) if isinstance(rows, list) else 0} of "
            f"{len(known)} layers; the lists must be parallel."
        )

    found: list[LayerState] = []
    for (guid, name), row in zip(known, rows, strict=True):
        attribute = row.get("layerAttribute") if isinstance(row, dict) else None
        if not isinstance(attribute, dict):
            attribute = row if isinstance(row, dict) else {}
        found.append(
            LayerState(
                identifier=guid,
                name=str(attribute.get("name", name)),
                hidden=bool(attribute.get("isHidden", False)),
                locked=bool(attribute.get("isLocked", False)),
            )
        )
    return found


def combination_states(connection: ArchicadConnection, name: str) -> dict[str, bool] | None:
    """A named combination's per-layer visibility, or ``None`` if there is no
    such combination.

    This is what makes an office's own export combination usable without
    Archicad being able to activate it: the states are read out of the
    combination and written onto the layers directly.
    """
    listing = connection.run_tapir(
        "GetAttributesByType", {"attributeType": "LayerCombination"}
    )
    attributes = listing.get("attributes") if isinstance(listing, dict) else None
    if not isinstance(attributes, list):
        return None

    tidy = " ".join(name.split()).casefold()
    match = next(
        (
            entry
            for entry in attributes
            if isinstance(entry, dict)
            and " ".join(str(entry.get("name", "")).split()).casefold() == tidy
        ),
        None,
    )
    if match is None:
        return None

    response = connection.run_tapir(
        "GetLayerCombinations", {"attributes": [{"attributeId": match["attributeId"]}]}
    )
    rows = response.get("layerCombinations") if isinstance(response, dict) else None
    first = rows[0] if isinstance(rows, list) and rows else None
    body = (first or {}).get("layerCombination") if isinstance(first, dict) else None
    layers = (body or {}).get("layers") if isinstance(body, dict) else None
    if not isinstance(layers, list):
        return None

    return {
        str((entry.get("attributeId") or {}).get("guid", "")): bool(entry.get("isHidden", False))
        for entry in layers
        if isinstance(entry, dict) and (entry.get("attributeId") or {}).get("guid")
    }


def shown_for_export(
    layers: Sequence[LayerState], hide: Sequence[str] = ()
) -> dict[str, bool]:
    """The tool's own base: everything on, except what was named off.

    Show-everything rather than preserve-what-is-there, because the two fail
    in different directions and only one of them is visible afterwards. A
    layer wrongly left *off* is absent from the export and silently changes
    the answer -- no zones, no context, a building with no walls -- and the
    only trace is a number that looks reasonable. A layer wrongly left *on*
    adds geometry the run then reports: the element counts, the parked-geometry
    warning and ``--exclude-above`` all describe it.

    It is a blunt base, and deliberately so. An office that keeps a curated
    export combination should be pointed at it instead, which is what
    ``--layer-combination`` is for; this is what a project without one gets.

    ``hide`` is for the layers that are noise in a sun study and clutter in a
    drawing of one -- furniture, joinery, solid-operation bodies, annotation.
    They are a project's own business, so they are named rather than guessed
    at from layer names.
    """
    return _with(
        dict.fromkeys((layer.identifier for layer in layers), False), layers, off=hide
    )


def _with(
    base: Mapping[str, bool],
    layers: Sequence[LayerState],
    *,
    shown: Sequence[str] = (),
    off: Sequence[str] = (),
) -> dict[str, bool]:
    """``base``, with the named layers forced on and then forced off.

    Named layers are matched the way a person copies them: stripped and
    case-folded. Archicad layer names carry trailing spaces more often than
    anyone expects, and a padded listing hides them completely.
    """
    def tidy(name: str) -> str:
        return " ".join(name.split()).casefold()

    on_names, off_names = {tidy(n) for n in shown}, {tidy(n) for n in off}
    states = dict(base)
    for layer in layers:
        key = tidy(layer.name)
        if key in on_names:
            states[layer.identifier] = False
        if key in off_names:
            states[layer.identifier] = True
    return states


def ensure_combination(
    connection: ArchicadConnection, name: str, states: Mapping[str, bool]
) -> bool:
    """Record the export state as a real Layer Combination. ``True`` if made.

    Not how the state is applied -- nothing here can activate a combination --
    but worth writing anyway. It gives the choice a name in the Layer Settings
    dialog, so a person can see exactly what the tool exported with, and select
    it by hand if they want the model on screen to match.

    Reused by name and never deleted: ``DeleteAttributes`` refuses a
    ``LayerCombination``, answering "Attribute not found" for an id the
    project had just reported.
    """
    listing = connection.run_tapir(
        "GetAttributesByType", {"attributeType": "LayerCombination"}
    )
    attributes = listing.get("attributes") if isinstance(listing, dict) else None
    tidy = " ".join(name.split()).casefold()
    if isinstance(attributes, list) and any(
        isinstance(entry, dict)
        and " ".join(str(entry.get("name", "")).split()).casefold() == tidy
        for entry in attributes
    ):
        return False

    try:
        connection.run_tapir(
            "CreateLayerCombinations",
            {
                "layerCombinationDataArray": [
                    {
                        "name": name,
                        "layers": [
                            {
                                "attributeId": {"guid": guid},
                                "isHidden": hidden,
                                "isLocked": False,
                                "isWireframe": False,
                                "intersectionGroupNr": 1,
                            }
                            for guid, hidden in states.items()
                        ],
                    }
                ]
            },
        )
    except ArchicadError:
        # A combination is a convenience here, not the mechanism. Failing to
        # record one is no reason to fail an export that does not need it.
        return False
    return True


def _write(connection: ArchicadConnection, layers: Sequence[LayerState]) -> None:
    """Set visibility and lock on existing layers, by name.

    ``CreateLayers`` with ``overwriteExisting`` is the only way in -- there is
    no ``SetLayers`` -- which is why writing a layer's state looks like
    creating one and is not.
    """
    if not layers:
        return
    connection.run_tapir(
        "CreateLayers",
        {
            "layerDataArray": [
                {"name": state.name, "isHidden": state.hidden, "isLocked": state.locked}
                for state in layers
            ],
            "overwriteExisting": True,
        },
    )


@contextmanager
def export_state(
    connection: ArchicadConnection,
    *,
    combination: str | None = None,
    require: Sequence[str] = (),
    hide: Sequence[str] = (),
    record_as: str = EXPORT_COMBINATION,
) -> Iterator[LayerPlan]:
    """Hold the project at the export's layer state, then put it back.

    Composed rather than chosen, in three steps, because no single combination
    a project already has is the right one. The base is ``combination`` -- one
    of the project's own, usually its IFC export combination, which is an
    office's own account of what belongs in an export -- or everything shown
    if none is named. Then ``require`` is forced on and ``hide`` forced off.

    The middle step is what makes the first usable. On the reference project
    *neither* export combination shows the ``06 | Zone.*`` layers, so either
    one used as-is gives an export with no ``IfcSpace`` in it -- D52's failure,
    reached by following the office's own settings. The zone layers, and the
    neighbouring context, are what the study needs rather than what a drawing
    needs, and the run is what knows the difference.

    The restore is unconditional. A run that rearranges what somebody sees and
    leaves it that way is worse than one that refuses to touch it -- this tool
    spent three sessions re-selecting a combination it had itself clobbered.
    """
    before = read_layers(connection)
    source = record_as

    if combination:
        base = combination_states(connection, combination)
        if base is None:
            raise ArchicadError(
                f"The project has no layer combination called {combination!r}. "
                f"Leave --layer-combination off to use the tool's own, which is "
                f"every layer on except those named with --hide-layer."
            )
        source = f"{combination} + what the study needs"
    else:
        base = dict.fromkeys((state.identifier for state in before), False)

    wanted = _with(base, before, shown=require, off=hide)
    if not combination:
        ensure_combination(connection, record_as, wanted)

    moves = [
        (
            old,
            LayerState(
                identifier=old.identifier,
                name=old.name,
                hidden=wanted.get(old.identifier, old.hidden),
                locked=False,
            ),
        )
        for old in before
    ]
    changed = [
        (old, new)
        for old, new in moves
        if (new.hidden, new.locked) != (old.hidden, old.locked)
    ]
    plan = LayerPlan(
        combination=source,
        shown=tuple(new.name for old, new in changed if old.hidden and not new.hidden),
        unlocked=tuple(new.name for old, new in changed if old.locked and not new.locked),
        hidden=tuple(new.name for old, new in changed if new.hidden and not old.hidden),
        total=len(before),
        changed=len(changed),
    )

    _write(connection, [new for _, new in changed])
    try:
        yield plan
    finally:
        # Only what was touched, and back to exactly what it was.
        _write(connection, [old for old, _ in changed])
