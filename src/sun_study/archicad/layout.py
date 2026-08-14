"""Putting the drawn result on a sheet, without anybody placing a drawing.

``draw`` colours the floor plan. That is the analysis made visible, but it is
not yet a deliverable: a plan lives in the Project Map, and what gets issued is
a Layout with a Drawing on it at a stated scale. Doing that by hand is four
clicks per storey, and a study that has to be re-run after every massing change
is exactly where four clicks per storey stops being free.

The chain, all of it Tapir
--------------------------
Archicad will not let a Hatch be created into a chosen database -- ``CreateHatches``
draws into whatever is active -- so the fills cannot simply be made "in the
worksheet". The supported route is the navigator one, and it is better anyway
because the Drawing stays *linked*: re-run the study, the plan updates, and the
sheet updates with it.

===================================  =======  =====================================
``GetNavigatorItemTree``             1.1.7    Find the storeys that carry fills
``CloneProjectMapItemToViewMap``     1.1.7    Give each one a View to place
``CreateLayout``                     1.4.0    The sheet
``CreateDrawings``                   1.4.0    The plan on the sheet, at a scale
===================================  =======  =====================================

Storeys are matched by ``prefix``, which for a Story item is the floor number
as a string -- the same number ``DrawReport.storeys`` reports. Matching by name
would break on any project whose storeys are not called what the tool guessed.

What this deliberately does not do
----------------------------------
It does not touch an existing layout's contents, and it does not delete
anything. A sheet may carry a title block, notes and a revision history that
nobody wants regenerated; re-running adds a Drawing to a *new* layout rather
than rebuilding an old one. Cleaning up is a person's decision, and the run
says which layouts it made so there is something to clean up *by*.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Any

from sun_study.archicad.connection import ArchicadConnection, ArchicadError

__all__ = [
    "DEFAULT_LAYOUT_SCALE",
    "LAYOUT_MINIMUM_TAPIR_VERSION",
    "LayoutReport",
    "NavigatorItem",
    "layout_results",
    "project_map",
    "storey_items",
]

#: ``CreateLayout`` and ``CreateDrawings`` both arrived here. Gated separately,
#: like drawing is, so wanting numbers does not require a sheet-capable add-on.
LAYOUT_MINIMUM_TAPIR_VERSION = (1, 4, 0)

#: 1:200 suits an apartment-building floor plate on A1. Stated rather than
#: inherited from the view, because a diagram issued at "whatever scale the
#: view happened to be on" is not a drawing anybody can measure off.
DEFAULT_LAYOUT_SCALE = 200.0


@dataclass(frozen=True)
class NavigatorItem:
    """One entry in a navigator tree."""

    identifier: str
    name: str
    kind: str
    """``StoryItem``, ``FolderItem``, ``LayoutItem`` and so on."""
    prefix: str
    """For a Story item, the floor number as a string. Empty otherwise."""

    @property
    def storey_index(self) -> int | None:
        """The floor number, when this is a storey and the prefix parses."""
        if self.kind != "StoryItem":
            return None
        try:
            return int(self.prefix)
        except ValueError:
            return None


@dataclass(frozen=True)
class LayoutReport:
    """What was placed, and on what."""

    layout_name: str
    drawings_placed: int
    storeys: tuple[int, ...]
    scale: float
    missing_storeys: tuple[int, ...]
    """Storeys that carry fills but have no Project Map item to place."""

    @property
    def complete(self) -> bool:
        return not self.missing_storeys

    def describe(self) -> str:
        lines = [
            f"placed {self.drawings_placed} drawings at 1:{self.scale:g} on layout "
            f"{self.layout_name!r}"
        ]
        if self.storeys:
            shown = ", ".join(str(storey) for storey in self.storeys)
            lines.append(f"  storey index {shown}, one drawing each")
        if self.missing_storeys:
            shown = ", ".join(str(storey) for storey in self.missing_storeys)
            lines.append(
                f"  no Project Map storey found for index {shown}, so those fills "
                f"are drawn but not on a sheet"
            )
        lines.append("  the drawings are linked, so re-running the study updates the sheet")
        return "\n".join(lines)


def project_map(connection: ArchicadConnection) -> tuple[NavigatorItem, ...]:
    """Every item in the Project Map, flattened.

    The tree nests arbitrarily -- storeys can sit inside folders -- and nothing
    here cares about the shape, only about finding storeys. Flattening keeps
    the recursion in one place.
    """
    response = connection.run_tapir("GetNavigatorItemTree", {"navigatorMapId": "ProjectMap"})
    root = response.get("navigatorItemTree") if isinstance(response, dict) else None
    if not isinstance(root, dict):
        raise ArchicadError(f"GetNavigatorItemTree returned no tree: {response!r}")
    return tuple(_walk(root))


def _walk(item: dict[str, Any], depth: int = 0) -> Iterator[NavigatorItem]:
    """Depth-first, with a bound. A cycle here would hang the run."""
    if depth > 32:
        return
    identifier = (item.get("navigatorItemId") or {}).get("guid")
    if identifier:
        yield NavigatorItem(
            identifier=str(identifier),
            name=str(item.get("name", "")),
            kind=str(item.get("type", "")),
            prefix=str(item.get("prefix", "")),
        )
    for wrapper in item.get("children") or []:
        child = (wrapper or {}).get("navigatorItem") if isinstance(wrapper, dict) else None
        if isinstance(child, dict):
            yield from _walk(child, depth + 1)


def storey_items(
    connection: ArchicadConnection, storeys: Sequence[int]
) -> tuple[dict[int, NavigatorItem], tuple[int, ...]]:
    """The Project Map storey for each floor index, and the ones not found.

    Matched on the floor number rather than the name. Storey names are a
    practice's own business -- "Level 08", "L08", "8" -- and a tool that
    guesses at them finds nothing on the first project that names them
    differently.
    """
    by_index = {
        item.storey_index: item for item in project_map(connection) if item.storey_index is not None
    }
    found = {storey: by_index[storey] for storey in storeys if storey in by_index}
    missing = tuple(storey for storey in storeys if storey not in by_index)
    return found, missing


def layout_results(
    connection: ArchicadConnection,
    storeys: Sequence[int],
    *,
    layout_name: str,
    scale: float = DEFAULT_LAYOUT_SCALE,
    spacing_mm: float = 420.0,
) -> LayoutReport:
    """Put one linked Drawing per storey onto a new Layout.

    ``storeys`` comes straight from ``DrawReport.storeys``, so the sheet shows
    exactly the plans that carry fills and no others.
    """
    connection.require_tapir_at_least(
        LAYOUT_MINIMUM_TAPIR_VERSION, because="CreateLayout and CreateDrawings"
    )
    if not storeys:
        return LayoutReport(layout_name, 0, (), scale, ())

    items, missing = storey_items(connection, storeys)
    if not items:
        return LayoutReport(layout_name, 0, (), scale, missing)

    ordered = sorted(items)
    views = _clone_to_view_map(connection, [items[storey] for storey in ordered])
    layout = _create_layout(connection, layout_name)

    # Laid out in a row, in millimetres on the sheet. Crude on purpose: a
    # person moves them where they want them, and the Drawings stay linked
    # wherever they end up.
    drawings = [
        {
            "navigatorItemId": {"guid": view},
            "layoutDatabaseId": {"guid": layout},
            "name": f"Sun Study -- storey {storey}",
            "position": {"x": spacing_mm * (column + 1), "y": spacing_mm},
            "scale": scale,
        }
        for column, (storey, view) in enumerate(zip(ordered, views, strict=True))
    ]
    placed = connection.run_tapir("CreateDrawings", {"drawingsData": drawings})
    _check_each(placed, "elements", "CreateDrawings")

    return LayoutReport(
        layout_name=layout_name,
        drawings_placed=len(drawings),
        storeys=tuple(ordered),
        scale=scale,
        missing_storeys=missing,
    )


def _clone_to_view_map(connection: ArchicadConnection, items: Sequence[NavigatorItem]) -> list[str]:
    """A View Map clone per storey, because a Drawing is placed from a View.

    Clones rather than independent views: a clone follows its Project Map
    source, so a storey renamed later stays consistent with the sheet.
    """
    response = connection.run_tapir(
        "CloneProjectMapItemToViewMap",
        {"viewsData": [{"navigatorItemId": {"guid": item.identifier}} for item in items]},
    )
    cloned = _check_each(response, "navigatorItems", "CloneProjectMapItemToViewMap")
    identifiers = [str((entry.get("navigatorItemId") or {}).get("guid", "")) for entry in cloned]
    if len(identifiers) != len(items) or not all(identifiers):
        raise ArchicadError(
            f"CloneProjectMapItemToViewMap returned {len(identifiers)} views for "
            f"{len(items)} storeys; the lists must be parallel."
        )
    return identifiers


def _create_layout(connection: ArchicadConnection, name: str) -> str:
    response = connection.run_tapir("CreateLayout", {"layoutsData": [{"layoutName": name}]})
    databases = _check_each(response, "databases", "CreateLayout")
    first = databases[0] if databases else {}
    identifier = (first.get("databaseId") or {}).get("guid")
    if not identifier:
        raise ArchicadError(f"CreateLayout returned no database id: {response!r}")
    return str(identifier)


def _check_each(response: Any, key: str, command: str) -> list[dict[str, Any]]:
    """The per-item results, with any per-item error raised.

    These commands report failures inside a successful response, one slot per
    input -- the same shape ``draw._create`` guards against. A sheet missing
    one storey silently is worse than no sheet: the missing plan reads as a
    storey with no apartments.
    """
    items = response.get(key) if isinstance(response, dict) else None
    if not isinstance(items, list):
        raise ArchicadError(f"{command} returned no {key}: {response!r}")
    for entry in items:
        if isinstance(entry, dict) and "error" in entry:
            error = entry["error"] or {}
            raise ArchicadError(
                f"{command} failed for one item: "
                f"{error.get('message', 'no message')} (code {error.get('code')})"
            )
    return [entry for entry in items if isinstance(entry, dict)]
