"""What the open project can tell the window, so nobody has to type it.

Every list here is read from Archicad rather than configured. The alternative
is a settings file per project, which goes stale the first time somebody
renames a layer and is then wrong in a way nothing reports.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sun_study.archicad.connection import (
    ArchicadConnection,
    ArchicadError,
    HttpTransport,
    Instance,
    TapirUnavailableError,
    find_instances,
)
from sun_study.archicad.layers import read_layers
from sun_study.archicad.layout import _walk, master_layouts
from sun_study.archicad.series import ensure_model_database

#: Floor area, in square metres, at or above which a zone is a dwelling
#: rather than something a dwelling contains. The ADG's smallest apartment is
#: a 35 m2 studio, so 30 leaves room for a zone drawn to the inside face
#: without admitting a balcony.
DWELLING_AREA_M2 = 30.0

#: And the range a private open space falls in. The ADG's balcony minimums run
#: 4 m2 for a studio to 12 m2 for three bedrooms; the floor keeps a storage
#: cupboard or a riser out, which is neither a dwelling nor somebody's balcony.
OPEN_SPACE_AREA_M2 = 4.0


@dataclass(frozen=True)
class ZoneKind:
    """One name the project's zones go by, on one layer, and how big they are.

    The window offers these instead of asking somebody to type a zone name.
    Which zones are dwellings is not something the layer alone can say: one
    project carries 15 apartments named ``G08``, 20 balconies named ``BY`` and
    the storage cupboards, all Zones, all on ``06 | Zone.Units``. Counting the
    balconies as apartments is silent -- they come out as flats with no living
    room and a third of the building fails -- so the names have to be
    separable, and a list read from the project is the only way somebody can
    separate them without knowing the model.

    Both of Archicad's fields are offered as labels because the IFC export
    puts one in ``Name`` and the other in ``LongName`` and which is which
    varies by translator. The assessment matches either, so a person picking
    from this list is right whichever way round their project has it.
    """

    layer: str
    label: str
    count: int
    area_m2: float
    """The median of that name's zones, which is what says what they are."""

    @property
    def dwelling(self) -> bool:
        return self.area_m2 >= DWELLING_AREA_M2

    @property
    def open_space(self) -> bool:
        return OPEN_SPACE_AREA_M2 <= self.area_m2 < DWELLING_AREA_M2

    def described(self) -> str:
        """For the line under the field, so a guess can be checked."""
        return f"{self.label} ({self.count} x {self.area_m2:.0f} m2)"


@dataclass(frozen=True)
class ProjectOptions:
    """The choices this project offers, each read from it."""

    project: str = ""
    layers: tuple[str, ...] = ()
    zone_layers: tuple[str, ...] = ()
    """Layers that carry Zones -- the apartment picker's candidates."""

    zone_kinds: tuple[ZoneKind, ...] = ()
    """What those zones are called, and how big they are, per layer."""

    combinations: tuple[str, ...] = ()
    masters: tuple[str, ...] = ()
    subsets: tuple[str, ...] = ()
    """Layout Book subsets, where the sheets can be filed."""

    storeys: tuple[int, ...] = ()

    tapir: str = ""
    """The add-on's version, when it answered."""

    tapir_missing: bool = False
    """Archicad is there and the add-on is not.

    Worth its own flag rather than a line in ``problems``. Nothing in this
    tool works without it -- 116 of its 124 Archicad calls are Tapir commands
    -- so it is not a degraded run, it is no run at all, and the window says
    so instead of offering a Run button that cannot work.
    """

    problems: tuple[str, ...] = field(default_factory=tuple)
    """What could not be read. Shown rather than raised: a project missing one
    list is still worth offering the other five for."""

    @property
    def reachable(self) -> bool:
        return bool(self.project)


def running() -> list[Instance]:
    """Every Archicad on this machine, with the project each has open.

    Listed rather than assumed. Archicad hands each instance its own port, so
    the default is right only for whichever started first, and a colleague
    with two projects open would otherwise measure the wrong building.
    """
    try:
        return list(find_instances())
    except ArchicadError:
        return []


def _zones_by_layer(connection: ArchicadConnection) -> tuple[tuple[str, ...], tuple[ZoneKind, ...]]:
    """Which layers carry Zones, and what those zones are called. One pass.

    Layers by asking the Zones, not by name. ``06 | Zone.Units`` looks obvious
    on this project and the apartments on the next one are on a layer called
    something else entirely, while three layers here are named ``Zone.*`` and
    hold nothing.

    The names come from the same read because the second question is the one
    that decides the answer -- a layer is where the apartments are, a name is
    which of the things on it *are* apartments -- and asking Archicad for
    every zone twice, on a project with 1341 of them, is the sort of thing a
    person notices while waiting for a window to fill in.
    """
    from statistics import median

    from sun_study.archicad.read import layer_names
    from sun_study.archicad.read import zones as read_zones
    from sun_study.archicad.rooms import polygon_area

    names = layer_names(connection)
    carried: set[str] = set()
    areas: dict[tuple[str, str], list[float]] = {}
    for zone in read_zones(connection):
        # Not ``zone.layer_index or -1``: layer index 0 is a real Archicad
        # layer, and every zone on it would silently vanish from the list.
        if zone.layer_index is None:
            continue
        layer = names.get(zone.layer_index)
        if not layer:
            continue
        carried.add(layer)
        area = polygon_area(zone.outline) if zone.outline else 0.0
        for label in {zone.name.strip(), zone.number.strip()}:
            if label:
                areas.setdefault((layer, label), []).append(area)

    kinds = tuple(
        sorted(
            (
                ZoneKind(layer=layer, label=label, count=len(found), area_m2=median(found))
                for (layer, label), found in areas.items()
            ),
            # Biggest first, on each layer: the dwellings are what somebody is
            # looking for, and they are the largest thing a zone layer holds.
            key=lambda kind: (kind.layer, -kind.area_m2, kind.label),
        )
    )
    return tuple(sorted(carried)), kinds


def options(port: int) -> ProjectOptions:
    """Read every list the window offers. Never raises: a project that will
    not answer one question is still worth offering the rest of."""
    problems: list[str] = []

    def attempt(what: str, read):  # type: ignore[no-untyped-def]
        try:
            return read()
        except (ArchicadError, KeyError, ValueError, TypeError) as error:
            problems.append(f"{what}: {error}")
            return None

    try:
        connection = ArchicadConnection(HttpTransport(port=port))
        version = connection.tapir_version
        info = connection.run_tapir("GetProjectInfo", {}) or {}
        project = str(info.get("projectName") or "")
    except TapirUnavailableError:
        # Archicad answered; the add-on did not. A different failure from a
        # port with nothing on it, and a different thing to tell somebody.
        return ProjectOptions(
            tapir_missing=True,
            problems=(
                "Archicad is running but the Tapir add-on is not installed. "
                "The study cannot read or draw anything without it.",
            ),
        )
    except ArchicadError as error:
        return ProjectOptions(problems=(f"cannot reach Archicad on port {port}: {error}",))

    attempt("current database", lambda: ensure_model_database(connection))

    layers = attempt("layers", lambda: read_layers(connection)) or []
    combinations = attempt(
        "layer combinations",
        lambda: connection.run_tapir("GetAttributesByType", {"attributeType": "LayerCombination"}),
    )
    masters = attempt("master layouts", lambda: master_layouts(connection)) or []
    book = attempt(
        "layout book",
        lambda: connection.run_tapir("GetNavigatorItemTree", {"navigatorMapId": "LayoutBook"}),
    )
    zoned = attempt("zones", lambda: _zones_by_layer(connection)) or ((), ())
    zones, kinds = zoned
    storeys = attempt("storeys", lambda: connection.run_tapir("GetStories", {}))

    return ProjectOptions(
        project=project,
        tapir=version,
        layers=tuple(sorted(state.name for state in layers if state.name.strip())),
        zone_layers=tuple(zones),
        zone_kinds=tuple(kinds),
        combinations=tuple(
            sorted(
                str(entry.get("name", ""))
                for entry in ((combinations or {}).get("attributes") or [])
                if str(entry.get("name", "")).strip()
            )
        ),
        masters=tuple(item.name for item in masters),
        subsets=tuple(
            sorted(
                {
                    item.name
                    for item in _walk((book or {}).get("navigatorItemTree") or {})
                    if item.kind == "SubSetItem" and item.name.strip()
                }
            )
        ),
        storeys=tuple(
            sorted(
                int(row["index"])
                for row in ((storeys or {}).get("stories") or [])
                if isinstance(row, dict) and isinstance(row.get("index"), int)
            )
        ),
        problems=tuple(problems),
    )
