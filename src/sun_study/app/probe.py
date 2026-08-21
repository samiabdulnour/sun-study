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


@dataclass(frozen=True)
class ProjectOptions:
    """The choices this project offers, each read from it."""

    project: str = ""
    layers: tuple[str, ...] = ()
    zone_layers: tuple[str, ...] = ()
    """Layers that carry Zones -- the apartment picker's candidates."""

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


def _zone_layers(connection: ArchicadConnection) -> tuple[str, ...]:
    """Layers that actually carry Zones, by asking the Zones.

    Not by name. ``06 | Zone.Units`` looks obvious on this project and the
    apartments on the next one are on a layer called something else entirely,
    while three layers here are named ``Zone.*`` and hold nothing.
    """
    from sun_study.archicad.read import layer_names
    from sun_study.archicad.read import zones as read_zones

    names = layer_names(connection)
    carried: set[str] = set()
    for zone in read_zones(connection):
        # Not ``zone.layer_index or -1``: layer index 0 is a real Archicad
        # layer, and every zone on it would silently vanish from the list.
        if zone.layer_index is None:
            continue
        found = names.get(zone.layer_index)
        if found:
            carried.add(found)
    return tuple(sorted(carried))


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
    zones = attempt("zones", lambda: _zone_layers(connection)) or ()
    storeys = attempt(
        "storeys", lambda: connection.run_tapir("GetStories", {})
    )

    return ProjectOptions(
        project=project,
        tapir=version,
        layers=tuple(sorted(state.name for state in layers if state.name.strip())),
        zone_layers=tuple(zones),
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
