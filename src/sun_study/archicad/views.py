"""Layer combinations and views, so a sheet shows the instant it claims to.

The problem this solves
-----------------------
A layer's visibility is not a property of the layer. It is a property of the
**layer combination** in force, and a layer this tool has just created is
hidden in every combination the project already had -- so a run reports
hundreds of fills drawn and the plan does not change, on screen or on a sheet.
Creating the layer with ``isHidden: false`` does not help: the active
combination overrides it the moment anybody selects one.

A Drawing placed from a View inherits that View's layer combination, so the
same thing decides what a *sheet* shows. Without a combination of its own,
every drawing on the sun-study layout shows the plan and none of the study.

What this makes
---------------
One layer combination per instant, built from the project's **current** layer
states so the plan looks the way it does now, with the sun-study layers
overridden: the instant being drawn visible, the other instants hidden. Then
one View per storey per instant, pinned to that combination.

That is what lets a sheet say "09:00" and be true.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from sun_study.archicad.connection import ArchicadConnection, ArchicadError
from sun_study.archicad.layers import read_layers
from sun_study.archicad.layout import NavigatorItem, _walk
from sun_study.archicad.series import ensure_model_database

__all__ = [
    "VIEW_PREFIX",
    "ModelSource",
    "StoreyView",
    "ensure_layer_combination",
    "three_d_sources",
    "views_for_sources",
    "views_for_storeys",
]

#: On every view, layout and layer combination this tool makes. A project
#: carries hundreds of navigator items and the ones a study adds have to be
#: obvious at a glance -- and sortable together -- rather than reading as
#: somebody's stray copy of a storey.
VIEW_PREFIX = "SS"


@dataclass(frozen=True)
class StoreyView:
    """A View created for one storey, at one instant."""

    storey_index: int
    name: str
    navigator_id: str


def _all_layers(connection: ArchicadConnection) -> list[dict[str, Any]]:
    """Every layer with its identifier, name and current state.

    A combination is a statement about the *model*, so the model has to be
    the database current when its layers are read. Iterating over six sheets
    used to build the second combination from the first sheet's layout --
    ``layout_from_views`` leaves one current, and a layout answers with its
    own combination (D63) -- so each sheet inherited what the sheet before it
    happened to show.
    """
    ensure_model_database(connection)
    found = read_layers(connection)
    if not found:
        raise ArchicadError("The project reports no layers at all.")
    return [
        {
            "attributeId": {"guid": state.identifier},
            "name": state.name,
            "isHidden": state.hidden,
            "isLocked": state.locked,
            "isWireframe": state.wireframe,
            "intersectionGroupNr": state.intersection_group,
        }
        for state in found
    ]


def tool_layers(connection: ArchicadConnection, prefix: str) -> list[str]:
    """Every layer whose name starts with the tool's prefix.

    A combination has to say something about *all* of them, not just the ones
    the call knows about. Built from what the layer list actually holds, so a
    layer added by a later feature is hidden by the earlier features'
    combinations without anybody remembering to add it.
    """
    response = connection.run_tapir("GetAttributesByType", {"attributeType": "Layer"})
    attributes = response.get("attributes") if isinstance(response, dict) else None
    if not isinstance(attributes, list):
        return []
    tidy = prefix.casefold()
    return [
        str(entry.get("name", ""))
        for entry in attributes
        if isinstance(entry, dict) and str(entry.get("name", "")).casefold().startswith(tidy)
    ]


def ensure_layer_combination(
    connection: ArchicadConnection,
    name: str,
    *,
    show: Iterable[str],
    hide: Iterable[str],
) -> str:
    """Create or overwrite a layer combination. Returns its name.

    Built from the layers as they stand, so everything the reader expects to
    see on a plan stays as it is; only the named layers are forced. Overwrites
    on purpose -- the combination belongs to this tool and is regenerated
    every run, exactly like the fills it exists to reveal.
    """
    visible = {entry.casefold() for entry in show}
    invisible = {entry.casefold() for entry in hide}

    layers: list[dict[str, Any]] = []
    for layer in _all_layers(connection):
        key = layer["name"].casefold()
        hidden = layer["isHidden"]
        if key in visible:
            hidden = False
        elif key in invisible:
            hidden = True
        layers.append(
            {
                "attributeId": layer["attributeId"],
                "isHidden": hidden,
                # Never locked: a locked layer cannot be edited, and somebody
                # will want to move a label.
                "isLocked": layer["isLocked"] and key not in visible,
                "isWireframe": layer["isWireframe"],
                "intersectionGroupNr": layer["intersectionGroupNr"],
            }
        )

    response = connection.run_tapir(
        "CreateLayerCombinations",
        {
            "layerCombinationDataArray": [{"name": name, "layers": layers}],
            "overwriteExisting": True,
        },
    )
    _check(response, "CreateLayerCombinations")
    return name


def remove_previous(connection: ArchicadConnection, prefix: str = VIEW_PREFIX) -> tuple[int, int]:
    """Delete the views and layouts an earlier run made. ``(gone, left)``.

    Every run makes a fresh set, so without this a project collects a layout
    and six views per instant per run and nobody can tell which is current.
    Only items whose name starts with the tool's own prefix are touched, which
    is what the prefix is for.

    **Layouts first, then views, and the outcome is counted.** A View that a
    placed Drawing still points at cannot be deleted, and Archicad does not
    say so: ``DeleteNavigatorItems`` reports success and the view stays.
    Deleting both in one request left the project holding 36 views under 18
    names -- the previous run's, kept alive by its own drawings, beside the
    new ones. The count comes from reading the tree again rather than from the
    length of the request, for the same reason.
    """
    gone = 0
    for map_id in ("LayoutBook", "PublicViewMap"):
        doomed = _named(connection, map_id, prefix)
        if not doomed:
            continue
        connection.run_tapir(
            "DeleteNavigatorItems",
            {"navigatorItemIds": [{"navigatorItemId": {"guid": item}} for item in doomed]},
        )
        gone += len(doomed) - len(_named(connection, map_id, prefix))

    left = sum(
        len(_named(connection, map_id, prefix)) for map_id in ("LayoutBook", "PublicViewMap")
    )
    return gone, left


def _named(connection: ArchicadConnection, map_id: str, prefix: str) -> list[str]:
    """Navigator item ids in one map whose name carries the prefix.

    Matched on the name alone. A view created in the View Map keeps the *kind*
    of the thing it was copied from -- a storey view reports as ``StoryItem``,
    not ``ViewItem`` -- so filtering by kind here silently finds nothing.
    """
    try:
        response = connection.run_tapir("GetNavigatorItemTree", {"navigatorMapId": map_id})
    except ArchicadError:
        return []
    root = response.get("navigatorItemTree") if isinstance(response, dict) else None
    if not isinstance(root, dict):
        return []
    return [item.identifier for item in _walk(root) if item.name.startswith(f"{prefix} ")]


def next_view_folder(connection: ArchicadConnection, stem: str = f"{VIEW_PREFIX} Sun Study") -> str:
    """The name for this run's folder: ``stem fix 01``, then ``fix 02``.

    A new one each run rather than a reused one, because neither a folder nor
    a view can be deleted through the API -- so overwriting in place would
    leave the old views inside the same folder, indistinguishable from the new
    ones. Numbering them makes the current set obvious and the stale ones easy
    to delete by hand, which is the only way they can go.
    """
    used = set()
    for name in _by_name(connection, "PublicViewMap"):
        if name.startswith(f"{stem} fix "):
            tail = name[len(f"{stem} fix ") :].strip()
            if tail.isdigit():
                used.add(int(tail))
    return f"{stem} fix {max(used, default=0) + 1:02d}"


def ensure_view_folder(connection: ArchicadConnection, name: str) -> str:
    """One folder in the View Map to keep this tool's views in. Returns its id.

    Not decoration. ``CreateViewsInViewMap`` with no parent puts the view at
    the View Map **root**, and it brings the source's ancestry with it -- so
    copying a storey recreates the project folder and the Stories folder above
    it, every time. Three runs left the project looking like it contained
    itself three times over, which is what a reader sees before they see any
    sun study at all.
    """
    existing = _by_name(connection, "PublicViewMap")
    if name in existing:
        return existing[name]

    response = connection.run_tapir("CreateViewMapFolder", {"folderName": name})
    identifier = (
        (response.get("navigatorItemId") or {}).get("guid") if isinstance(response, dict) else None
    )
    if not identifier:
        raise ArchicadError(f"CreateViewMapFolder returned no folder id: {response!r}")
    return str(identifier)


def views_for_storeys(
    connection: ArchicadConnection,
    storeys: Sequence[NavigatorItem],
    *,
    combination: str,
    suffix: str,
    drawing_scale: float,
    zoom: tuple[float, float, float, float] | None = None,
    folder: str | None = None,
    prefix: str = VIEW_PREFIX,
) -> list[StoreyView]:
    """One independent View per storey, pinned to a layer combination.

    Independent rather than a clone: a clone follows its Project Map source,
    including its layer combination, so pinning one would either fail or drag
    every other view of that storey with it.
    """
    if not storeys:
        return []

    # Reused only *within this run's folder*. A View cannot be deleted --
    # ``DeleteNavigatorItems`` reports success and leaves it -- so reuse is
    # what stops the View Map growing without limit. Matching by name across
    # the whole map instead left a run's views scattered through the folders
    # of earlier runs, which is worse than a few stale ones: the current set
    # was no longer in one place.
    home = folder or f"{prefix} Sun Study"
    wanted = [f"{prefix} {item.name} {suffix}" for item in storeys]
    already = _by_name_under(connection, "PublicViewMap", home)
    missing = [
        (item, name) for item, name in zip(storeys, wanted, strict=True) if name not in already
    ]

    if missing:
        parent = ensure_view_folder(connection, home)
        response = connection.run_tapir(
            "CreateViewsInViewMap",
            {
                "viewsData": [
                    {
                        "navigatorItemId": {"guid": item.identifier},
                        "name": name,
                        "parentNavigatorItemId": {"guid": parent},
                    }
                    for item, name in missing
                ]
            },
        )
        created = _check(response, "CreateViewsInViewMap")
        made = [str((entry.get("navigatorItemId") or {}).get("guid", "")) for entry in created]
        if len(made) != len(missing) or not all(made):
            raise ArchicadError(
                f"CreateViewsInViewMap returned {len(made)} views for "
                f"{len(missing)} storeys; the lists must be parallel."
            )
        already.update({name: guid for (_, name), guid in zip(missing, made, strict=True)})

    identifiers = [already[name] for name in wanted]

    # The layer combination, and the extent -- but deliberately *not* the
    # scale. Setting the view to the same denominator as the Drawing looked
    # like the tidy thing to do and is not: the two compound rather than
    # cancel, and a 1:200 view placed at 1:200 came out at 20000% and far
    # wider than the sheet. The view keeps the scale it inherits from the
    # storey it copies, which is what produced correctly sized drawings.
    #
    # The zoom is worth pinning. A view inherits whatever the storey happened
    # to be zoomed to, so a drawing made from it crops the building wherever
    # somebody last left the screen.
    settings: dict[str, Any] = {
        "layerCombination": combination,
        # The *view* carries the scale and the Drawing is placed at 100%.
        # Setting both to 200 multiplies them: the sheet came back at 20000%
        # and many times the page.
        "drawingScale": int(drawing_scale),
        # Zeroed explicitly. A view inherits the storey's rotation, and this
        # project's is turned to true north, so every drawing arrived at
        # 279.9 degrees -- the same angle as the project's north.
        "rotation": 0,
    }
    if zoom is not None:
        x_min, y_min, x_max, y_max = zoom
        settings["zoom"] = {"xMin": x_min, "yMin": y_min, "xMax": x_max, "yMax": y_max}
        settings["saveZoom"] = True

    connection.run_tapir(
        "SetViewSettings",
        {
            "navigatorItemIdsWithViewSettings": [
                {"navigatorItemId": {"guid": identifier}, "viewSettings": settings}
                for identifier in identifiers
            ]
        },
    )

    return [
        StoreyView(
            storey_index=item.storey_index if item.storey_index is not None else 0,
            name=f"{prefix} {item.name} {suffix}",
            navigator_id=identifier,
        )
        for item, identifier in zip(storeys, identifiers, strict=True)
    ]


def _by_name_under(connection: ArchicadConnection, map_id: str, folder_name: str) -> dict[str, str]:
    """Items inside one named folder, keyed by name.

    Scoped rather than global so a run only reuses its own folder's views.
    Returns nothing when the folder does not exist yet, which is the ordinary
    case for a fresh run.
    """
    try:
        response = connection.run_tapir("GetNavigatorItemTree", {"navigatorMapId": map_id})
    except ArchicadError:
        return {}
    root = response.get("navigatorItemTree") if isinstance(response, dict) else None
    if not isinstance(root, dict):
        return {}

    def find(node: dict[str, Any]) -> dict[str, Any] | None:
        if str(node.get("name", "")) == folder_name:
            return node
        for wrapper in node.get("children") or []:
            child = (wrapper or {}).get("navigatorItem") if isinstance(wrapper, dict) else None
            if isinstance(child, dict):
                found = find(child)
                if found is not None:
                    return found
        return None

    folder = find(root)
    if folder is None:
        return {}
    found: dict[str, str] = {}
    for wrapper in folder.get("children") or []:
        child = (wrapper or {}).get("navigatorItem") if isinstance(wrapper, dict) else None
        if isinstance(child, dict):
            identifier = (child.get("navigatorItemId") or {}).get("guid")
            if identifier:
                found.setdefault(str(child.get("name", "")), str(identifier))
    return found


def _by_name(connection: ArchicadConnection, map_id: str) -> dict[str, str]:
    """Navigator items in one map, keyed by name. First one wins on a clash."""
    try:
        response = connection.run_tapir("GetNavigatorItemTree", {"navigatorMapId": map_id})
    except ArchicadError:
        return {}
    root = response.get("navigatorItemTree") if isinstance(response, dict) else None
    if not isinstance(root, dict):
        return {}
    found: dict[str, str] = {}
    for item in _walk(root):
        found.setdefault(item.name, item.identifier)
    return found


def _check(response: Any, command: str) -> list[dict[str, Any]]:
    """Per-item results, with any per-item error raised."""
    if not isinstance(response, dict):
        raise ArchicadError(f"{command} returned {response!r}")
    for key in ("navigatorItems", "attributeIds", "executionResults"):
        items = response.get(key)
        if isinstance(items, list):
            for entry in items:
                if isinstance(entry, dict) and "error" in entry:
                    error = entry["error"] or {}
                    raise ArchicadError(
                        f"{command} failed for one item: "
                        f"{error.get('message', 'no message')} (code {error.get('code')})"
                    )
            return [entry for entry in items if isinstance(entry, dict)]
    return []


@dataclass(frozen=True)
class ModelSource:
    """A Project Map item worth making a view of: the 3D window, or a document."""

    identifier: str
    name: str
    kind: str
    """The navigator item's own type, e.g. ``AxonometryItem``."""


def three_d_sources(connection: ArchicadConnection) -> list[ModelSource]:
    """The project's 3D windows and 3D Documents, from the Project Map.

    Both are worth offering and they are not the same thing. The 3D window is
    live: it shows the model as it is now, from any angle, and a view of it
    inherits nothing but what is pinned to it. A 3D Document is a *drawing*
    made from a 3D view, with its own pen and fill overrides and its own
    dimensions, which is what an office puts on a sheet.

    A 3D Document cannot be created through this add-on -- there is no command
    for it -- so what is offered here is the ones the project already has.
    """
    try:
        response = connection.run_tapir(
            "GetNavigatorItemTree", {"navigatorMapId": "ProjectMap"}
        )
    except ArchicadError:
        return []
    root = response.get("navigatorItemTree") if isinstance(response, dict) else None
    if not isinstance(root, dict):
        return []

    wanted = {"PerspectiveItem", "AxonometryItem", "DocumentFrom3DItem"}
    found: list[ModelSource] = []

    def walk(node: dict[str, Any]) -> None:
        kind = str(node.get("type", ""))
        identifier = str((node.get("navigatorItemId") or {}).get("guid", ""))
        if kind in wanted and identifier:
            found.append(ModelSource(identifier, str(node.get("name", "")), kind))
        for wrapper in node.get("children") or []:
            child = (wrapper or {}).get("navigatorItem") if isinstance(wrapper, dict) else None
            walk(child if isinstance(child, dict) else wrapper)

    walk(root.get("rootItem", root))
    return found


def views_for_sources(
    connection: ArchicadConnection,
    sources: Sequence[tuple[ModelSource, str]],
    *,
    combination: str,
    folder: str,
    drawing_scale: float | None = None,
) -> list[StoreyView]:
    """One independent View per source, pinned to a layer combination.

    The same shape as ``views_for_storeys`` and for the same reasons -- reuse
    by name inside this run's folder, because a View cannot be deleted -- but
    without the plan settings. A 3D view has no plan rotation to zero and no
    plan extent to pin: what it is looking at belongs to the 3D window, not to
    the view, and forcing a zoom on it would be meaningless.

    ``storey_index`` on the result is always zero. These are views of the whole
    model, which is not on a storey.
    """
    if not sources:
        return []

    already = _by_name_under(connection, "PublicViewMap", folder)
    missing = [(source, name) for source, name in sources if name not in already]

    if missing:
        parent = ensure_view_folder(connection, folder)
        response = connection.run_tapir(
            "CreateViewsInViewMap",
            {
                "viewsData": [
                    {
                        "navigatorItemId": {"guid": source.identifier},
                        "name": name,
                        "parentNavigatorItemId": {"guid": parent},
                    }
                    for source, name in missing
                ]
            },
        )
        created = _check(response, "CreateViewsInViewMap")
        made = [str((entry.get("navigatorItemId") or {}).get("guid", "")) for entry in created]
        if len(made) != len(missing) or not all(made):
            raise ArchicadError(
                f"CreateViewsInViewMap returned {len(made)} views for "
                f"{len(missing)} sources; the lists must be parallel."
            )
        already.update({name: guid for (_, name), guid in zip(missing, made, strict=True)})

    identifiers = [already[name] for _, name in sources]
    settings: dict[str, Any] = {"layerCombination": combination}
    if drawing_scale is not None:
        # The view carries the scale and the Drawing goes on at 100%, exactly
        # as on the plans: set on both and they multiply.
        settings["drawingScale"] = int(drawing_scale)

    connection.run_tapir(
        "SetViewSettings",
        {
            "navigatorItemIdsWithViewSettings": [
                {"navigatorItemId": {"guid": identifier}, "viewSettings": settings}
                for identifier in identifiers
            ]
        },
    )

    return [
        StoreyView(storey_index=0, name=name, navigator_id=identifier)
        for (_, name), identifier in zip(sources, identifiers, strict=True)
    ]
