"""The window, tested without one on screen.

Two things are worth testing here and the rest is Tk's problem. The command
line the ticked boxes mean -- because a wrong flag is a wrong study, silently
-- and the probe's habit of answering with what it *could* read rather than
raising, because a project that will not give up one list is still worth
offering the other five for.

``Window`` is built against a withdrawn root: real widgets, never mapped, so
the argv it produces is the argv a colleague's click produces.
"""

from __future__ import annotations

import sys
import tkinter as tk
from itertools import pairwise
from pathlib import Path
from typing import Any

import pytest

from sun_study.app import probe, window
from sun_study.app.runner import CLI_MARKER, child_environment, command_prefix
from sun_study.archicad.connection import DEFAULT_TIMEOUT_SECONDS, Instance


@pytest.fixture
def hidden_window(monkeypatch: pytest.MonkeyPatch) -> Any:
    """A real Window over a fake project, never shown."""
    monkeypatch.setattr(
        probe,
        "running",
        lambda: [
            Instance(port=19723, project="SAMPLE"),
            Instance(port=19724, project="EXAMPLE"),
        ],
    )
    monkeypatch.setattr(
        probe,
        "options",
        lambda port: probe.ProjectOptions(
            project="SAMPLE",
            tapir="1.5.7",
            layers=(
                "01 | Wall.External",
                "01 | Floor.Structural",
                "05 | Dims/Notes.DA",
                "06 | Zone.Units",
            ),
            zone_layers=("05 | Dims/Notes.DA", "06 | Zone.Balcony", "06 | Zone.Units"),
            # The reference project's own shape: dwellings, balconies and
            # storage cupboards, all Zones, all on one layer, told apart only
            # by a name and a size.
            zone_kinds=(
                probe.ZoneKind(layer="06 | Zone.Units", label="G08", count=15, area_m2=74.0),
                probe.ZoneKind(layer="06 | Zone.Units", label="BY", count=20, area_m2=11.0),
                probe.ZoneKind(layer="06 | Zone.Units", label="SC", count=6, area_m2=2.5),
                probe.ZoneKind(layer="05 | Dims/Notes.DA", label="GFA", count=37, area_m2=310.0),
            ),
            combinations=("01 | Plans - DA", "12 | IFC ARCH. EXPORT"),
            masters=("A4", "DA A1 - VERTICAL COVER/NO SCALE", "DA A1 - VERTICAL - No Scale"),
            subsets=("ADG DIAGRAMS", "PLANS", "SHADOW DIAGRAMS"),
        ),
    )
    try:
        root = tk.Tk()
    except tk.TclError:  # pragma: no cover - a machine with no display
        pytest.skip("no display for Tk")
    root.withdraw()
    made = window.Window(root)
    yield made
    root.destroy()


def flag(args: list[str], name: str) -> list[str]:
    """Every value given for one repeated option."""
    return [value for before, value in pairwise(args) if before == name]


def test_the_project_fills_in_what_nobody_should_have_to_type(hidden_window: Any) -> None:
    """The point of the window. Each of these is a property of the project,
    and a person typing one is the commonest way a run measures the wrong
    thing."""
    assert hidden_window.apartments.get() == "06 | Zone.Units"
    assert hidden_window.combination.get() == "12 | IFC ARCH. EXPORT"
    assert hidden_window.shadow_subset.get() == "SHADOW DIAGRAMS"
    assert hidden_window.adg_subset.get() == "ADG DIAGRAMS"


def test_the_no_scale_master_beats_the_no_scale_cover_sheet(hidden_window: Any) -> None:
    """Both say no scale and one of them is a cover sheet. A diagram shrunk to
    fit the page is at no stated scale, which is why no-scale is right -- but
    not the cover."""
    assert hidden_window.master.get() == "DA A1 - VERTICAL - No Scale"


def test_the_facade_run_forces_the_zone_layers_into_the_export(hidden_window: Any) -> None:
    """Without an IfcSpace there is nothing to fit the skin onto the building
    against, and no IFC combination shows the zone layers -- the failure this
    produces is 'the skin cannot be placed', several minutes in."""
    (facade,) = hidden_window.jobs()
    assert facade.args[0] == "massing"
    assert flag(facade.args, "--require-layer") == ["06 | Zone.Balcony", "06 | Zone.Units"]


def test_an_annotation_layer_that_happens_to_hold_a_zone_is_not_exported(
    hidden_window: Any,
) -> None:
    """``05 | Dims/Notes.DA`` carries 37 Zones on the reference project. They
    are somebody's schedule, not the building."""
    (facade,) = hidden_window.jobs()
    assert "05 | Dims/Notes.DA" not in flag(facade.args, "--require-layer")


def test_the_floors_tick_is_what_puts_the_decks_in(hidden_window: Any) -> None:
    """Horizontal faces take more sun than any wall, so a study without them
    shows the least-lit half of the building."""
    (with_floors,) = hidden_window.jobs()
    assert "--model-flat" in with_floors.args

    hidden_window.do_floors.set(False)
    (without,) = hidden_window.jobs()
    assert "--no-model-flat" in without.args and "--model-flat" not in without.args


def test_the_plans_run_files_its_sheets_where_the_practice_keeps_them(
    hidden_window: Any,
) -> None:
    hidden_window.do_facade.set(False)
    hidden_window.do_plans.set(True)
    (plans,) = hidden_window.jobs()

    assert plans.args[0] == "archicad-run"
    assert "--draw" in plans.args and "--sheet" in plans.args
    assert flag(plans.args, "--layout-subset") == ["SHADOW DIAGRAMS"]
    assert flag(plans.args, "--adg-subset") == ["ADG DIAGRAMS"]
    assert flag(plans.args, "--plan-instant") == ["09:00", "12:00", "15:00"]


def test_the_chosen_archicad_is_the_one_the_study_runs_against(hidden_window: Any) -> None:
    """Two projects open is ordinary, and each instance gets its own port, so
    the default is right only for whichever started first."""
    hidden_window.instance.current(1)
    (facade,) = hidden_window.jobs()
    assert flag(facade.args, "--port") == ["19724"]


def test_both_ticked_runs_both_in_order(hidden_window: Any) -> None:
    hidden_window.do_plans.set(True)
    assert [job.args[0] for job in hidden_window.jobs()] == ["massing", "archicad-run"]


def test_the_communal_study_needs_no_apartment_and_no_glazing(hidden_window: Any) -> None:
    """The point of that study: a Zone round an outdoor area and nothing else.

    None of the apartment machinery may reach the command line, or a
    playground gets assessed as a flat with no living room.
    """
    hidden_window.do_facade.set(False)
    hidden_window.communal.set("06 | Zone.Balcony")
    hidden_window.communal_names.insert(0, "PLAYGROUND, COURTYARD")
    hidden_window.do_communal.set(True)
    (communal,) = hidden_window.jobs()

    assert communal.args[0] == "massing"
    assert flag(communal.args, "--zone-layer") == ["06 | Zone.Balcony"]
    assert flag(communal.args, "--zone-name") == ["PLAYGROUND", "COURTYARD"]
    assert "--zone-sheet" in communal.args
    for unwanted in (
        "--apartment-zone-layer",
        "--apartment-zone-name",
        "--open-space-zone-layer",
        "--livable-suffix",
    ):
        assert unwanted not in communal.args, unwanted


def test_the_communal_study_carries_a_second_zone_layer_for_the_fit(
    hidden_window: Any,
) -> None:
    """One zone layer gives one pair, and a plan transform needs two."""
    hidden_window.do_facade.set(False)
    hidden_window.communal.set("06 | Zone.Balcony")
    hidden_window.do_communal.set(True)
    (communal,) = hidden_window.jobs()
    assert flag(communal.args, "--require-layer"), "nothing to fit the drawing against"


def test_the_communal_study_carries_its_window_and_its_threshold(
    hidden_window: Any,
) -> None:
    """The default is 8 to 3 with a 2 hour split, which is what was asked for."""
    hidden_window.do_facade.set(False)
    hidden_window.communal.set("06 | Zone.Balcony")
    hidden_window.do_communal.set(True)
    (communal,) = hidden_window.jobs()

    assert flag(communal.args, "--window-start") == ["08:00"]
    assert flag(communal.args, "--window-end") == ["15:00"]
    assert flag(communal.args, "--zone-hours") == ["2"]
    assert "--zone-stats" in communal.args


def test_a_window_nobody_can_parse_is_left_to_the_ruleset(hidden_window: Any) -> None:
    """Passing half a window would move the study by an unstated amount.

    The ruleset's own hours are the safe way to be wrong, and the run says
    which window it used either way.
    """
    hidden_window.do_facade.set(False)
    hidden_window.communal.set("06 | Zone.Balcony")
    hidden_window.do_communal.set(True)
    for typed in ("", "08:00", "morning", "8-", "-15:00"):
        hidden_window.communal_window.delete(0, "end")
        hidden_window.communal_window.insert(0, typed)
        (communal,) = hidden_window.jobs()
        assert "--window-start" not in communal.args, typed
        assert "--window-end" not in communal.args, typed


def test_the_window_is_written_the_way_people_say_it(hidden_window: Any) -> None:
    hidden_window.do_facade.set(False)
    hidden_window.communal.set("06 | Zone.Balcony")
    hidden_window.do_communal.set(True)
    hidden_window.communal_window.delete(0, "end")
    hidden_window.communal_window.insert(0, "08:00 to 15:00")
    (communal,) = hidden_window.jobs()
    assert flag(communal.args, "--window-start") == ["08:00"]
    assert flag(communal.args, "--window-end") == ["15:00"]


def test_the_apartment_study_keeps_the_adg_window(hidden_window: Any) -> None:
    """4A-1 is 9 to 3. A communal window typed in this app must not move it."""
    hidden_window.do_facade.set(False)
    hidden_window.do_plans.set(True)
    hidden_window.communal_window.delete(0, "end")
    hidden_window.communal_window.insert(0, "06:00-18:00")
    (plans,) = hidden_window.jobs()
    assert "--window-start" not in plans.args
    assert "--window-end" not in plans.args


def test_all_three_studies_run_in_order(hidden_window: Any) -> None:
    hidden_window.do_plans.set(True)
    hidden_window.do_communal.set(True)
    assert [job.args[0] for job in hidden_window.jobs()] == [
        "massing",
        "archicad-run",
        "massing",
    ]


def test_nothing_ticked_asks_for_nothing(hidden_window: Any) -> None:
    hidden_window.do_facade.set(False)
    hidden_window.do_plans.set(False)
    hidden_window.do_communal.set(False)
    assert hidden_window.jobs() == []


def test_every_control_explains_itself(hidden_window: Any) -> None:
    """Most of these settings fail quietly -- a facade study missing its slab
    layers reports the least-lit half of the building rather than an error --
    so a control nobody can interpret is a real defect, not a polish item.
    A tooltip binds <Enter>, which is what this looks for."""
    from tkinter import ttk as widgets

    def controls(parent: tk.Misc) -> list[tk.Widget]:
        found: list[tk.Widget] = []
        for child in parent.winfo_children():
            kinds = widgets.Combobox | widgets.Entry | widgets.Checkbutton | widgets.Button
            if isinstance(child, kinds):
                found.append(child)
            found.extend(controls(child))
        return found

    bare = [str(widget) for widget in controls(hidden_window.root) if not widget.bind("<Enter>")]
    assert not bare, f"controls with no explanation: {bare}"


def test_the_hover_text_appears_and_goes_away(hidden_window: Any) -> None:
    """Without the delay, dragging across the form flashes six of these and
    reads as a fault rather than as help."""
    from sun_study.app.window import Tooltip

    tip = Tooltip(hidden_window.apartments, "which layer the apartments are on")
    # Captured rather than asserted in sequence: three asserts on one attribute
    # read to mypy as a contradiction, since it cannot see that _show and _hide
    # change it.
    before = tip.visible
    tip._show()
    shown = tip.visible
    tip._hide()
    after = tip.visible

    assert (before, shown, after) == (False, True, False)


# -- the add-on everything depends on -------------------------------------


def test_a_missing_add_on_is_told_apart_from_a_dead_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Archicad answering with no add-on is a different thing from nothing
    listening, and needs a different sentence: one is "open a project", the
    other is "install this"."""
    from sun_study.archicad.connection import TapirUnavailableError

    class NoAddOn:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        @property
        def tapir_version(self) -> str:
            raise TapirUnavailableError("no add-on response")

    monkeypatch.setattr(probe, "ArchicadConnection", NoAddOn)
    found = probe.options(19723)

    assert found.tapir_missing is True
    assert found.reachable is False
    assert "Tapir" in found.problems[0]


def test_run_is_switched_off_when_the_add_on_is_absent(
    hidden_window: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """116 of the tool's 124 Archicad calls are Tapir commands, so this is not
    a degraded run, it is no run at all. Better refused here than failed
    several steps in."""
    monkeypatch.setattr(probe, "options", lambda port: probe.ProjectOptions(tapir_missing=True))
    hidden_window.refresh()

    assert str(hidden_window.go.cget("state")) == "disabled"
    assert "not installed" in hidden_window.status.cget("text")


def test_the_add_on_version_is_shown_because_support_always_asks(
    hidden_window: Any,
) -> None:
    assert "Tapir" in hidden_window.status.cget("text")


# -- picking layers instead of typing them --------------------------------

LAYERS = (
    "01 ------------------------------ STRUCTURAL/MODEL",
    "01 | Wall.External",
    "01 | Floor.Structural",
    "06 ------------------------------ ZONES",
    "06 | Zone.Units",
)


def test_the_palettes_own_dividers_are_not_offered_as_layers() -> None:
    """They sort to the top of a search, hold nothing, and are the likeliest
    thing to tick: searching "zone" offered the divider before the zones."""
    from sun_study.app.window import pickable

    assert pickable(LAYERS) == [
        "01 | Wall.External",
        "01 | Floor.Structural",
        "06 | Zone.Units",
    ]


def test_a_project_without_dividers_loses_nothing() -> None:
    from sun_study.app.window import pickable

    plain = ("Walls", "Floors")
    assert pickable(plain) == list(plain)


def test_every_word_matches_anywhere_so_nobody_has_to_recall_the_format() -> None:
    """Layer names carry a group number, a bar, a dot and a space. Making
    somebody reproduce that order is a memory test, not a search."""
    from sun_study.app.window import matching, pickable

    names = pickable(LAYERS)
    assert matching(names, "floor str") == ["01 | Floor.Structural"]
    assert matching(names, "STRUCTURAL floor") == ["01 | Floor.Structural"]
    assert matching(names, "") == names
    assert matching(names, "nothing here") == []


def test_a_tick_survives_the_search_that_hides_it(hidden_window: Any) -> None:
    """Narrowing the list must not quietly untick what scrolled out of sight,
    which is how a facade study loses a slab layer nobody noticed choosing."""
    from sun_study.app.window import LayerChooser

    chooser = LayerChooser(
        hidden_window.root,
        title="Facade layers",
        hint="pick them",
        available=["01 | Wall.External", "01 | Floor.Structural"],
        chosen=["01 | Wall.External"],
    )
    chooser.query.insert(0, "floor")
    chooser._repaint()
    assert list(chooser._boxes) == ["01 | Floor.Structural"], "the wall is filtered out"

    chooser._accept()
    assert chooser.result == ["01 | Wall.External"], "and still chosen"
    chooser.destroy()


def test_cancelling_is_told_apart_from_choosing_nothing(hidden_window: Any) -> None:
    """Both leave an empty list. Only one of them should overwrite the field."""
    from sun_study.app.window import LayerChooser

    cancelled = LayerChooser(
        hidden_window.root,
        title="Facade layers",
        hint="pick them",
        available=["01 | Wall.External"],
        chosen=["01 | Wall.External"],
    )
    cancelled.destroy()
    assert cancelled.result is None

    emptied = LayerChooser(
        hidden_window.root,
        title="Facade layers",
        hint="pick them",
        available=["01 | Wall.External"],
        chosen=["01 | Wall.External"],
    )
    emptied._clear()
    emptied._accept()
    assert emptied.result == []


def test_the_chosen_come_back_in_the_projects_order(hidden_window: Any) -> None:
    """Not in the order somebody happened to click, so the field can be read
    against the layer palette."""
    from sun_study.app.window import LayerChooser

    chooser = LayerChooser(
        hidden_window.root,
        title="Facade layers",
        hint="pick them",
        available=["01 | Wall.External", "01 | Floor.Structural", "06 | Zone.Units"],
        chosen=["06 | Zone.Units", "01 | Wall.External"],
    )
    chooser._accept()
    assert chooser.result == ["01 | Wall.External", "06 | Zone.Units"]
    chooser.destroy()


def test_a_project_that_will_not_answer_is_reported_not_raised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A window that dies on a bad port is worse than one that says so."""
    from sun_study.archicad.connection import ArchicadError

    def refuse(*_args: object, **_kwargs: object) -> None:
        raise ArchicadError("nothing listening")

    monkeypatch.setattr(probe, "ArchicadConnection", refuse)
    found = probe.options(19999)

    assert found.reachable is False
    assert found.problems and "19999" in found.problems[0]


def test_the_child_is_told_to_be_the_command_line_and_not_to_buffer() -> None:
    """A progress line held in a pipe buffer for four minutes is worse than no
    progress line, and the window renders text rather than ANSI escapes."""
    env = child_environment()
    assert env[CLI_MARKER] == "1"
    assert env["PYTHONUNBUFFERED"] == "1"
    assert env["NO_COLOR"] == "1"


def test_unpackaged_runs_the_module_and_packaged_runs_itself(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A packaged app is one executable: sys.executable is the app, not a
    Python that can be asked for -m sun_study.cli."""
    monkeypatch.delattr("sys.frozen", raising=False)
    assert command_prefix()[1:] == ["-u", "-m", "sun_study.cli"]

    monkeypatch.setattr("sys.frozen", True, raising=False)
    assert len(command_prefix()) == 1


# ---------------------------------------------------------------------------
# Which zones are apartments, and which are somebody's balcony.
# ---------------------------------------------------------------------------
def test_the_dwellings_and_the_balconies_are_told_apart_by_size(hidden_window: Any) -> None:
    """The names are an office's own codes and mean nothing outside it. The
    sizes mean the same thing everywhere: a flat is tens of square metres, a
    balcony is a few, a storage cupboard is less."""
    assert hidden_window.apartment_names.get() == "G08"
    assert hidden_window.balconies.get() == "06 | Zone.Units"
    assert hidden_window.balcony_names.get() == "BY"


def test_the_zones_that_are_neither_are_left_out(hidden_window: Any) -> None:
    """A 2.5 m2 storage cupboard is not a dwelling and not private open space,
    and counted as either it is a wrong number rather than a missing one."""
    hidden_window.do_facade.set(False)
    hidden_window.do_plans.set(True)
    (plans,) = hidden_window.jobs()

    assert "SC" not in flag(plans.args, "--apartment-zone-name")
    assert "SC" not in flag(plans.args, "--open-space-zone-name")


def test_the_plans_run_says_which_zones_are_apartments(hidden_window: Any) -> None:
    """Without this the layer alone decides, and this layer carries 15
    dwellings, 20 balconies and the storage. Two thirds of what it would
    assess has no living room, so the building fails on zones nobody lives
    in."""
    hidden_window.do_facade.set(False)
    hidden_window.do_plans.set(True)
    (plans,) = hidden_window.jobs()

    assert flag(plans.args, "--apartment-zone-layer") == ["06 | Zone.Units"]
    assert flag(plans.args, "--apartment-zone-name") == ["G08"]


def test_the_plans_run_says_which_zones_are_the_open_space(hidden_window: Any) -> None:
    """The other half of the ADG test. With none named, every apartment is
    assessed on its living room alone and the result is worse than the
    building is."""
    hidden_window.do_facade.set(False)
    hidden_window.do_plans.set(True)
    (plans,) = hidden_window.jobs()

    assert flag(plans.args, "--open-space-zone-layer") == ["06 | Zone.Units"]
    assert flag(plans.args, "--open-space-zone-name") == ["BY"]


def test_an_unnarrowed_balcony_layer_is_not_passed_at_all(hidden_window: Any) -> None:
    """Naming the apartments' own layer as the open space, with no names to
    narrow it, would take every apartment for a balcony and leave the
    assessment with no apartments in it."""
    hidden_window.do_facade.set(False)
    hidden_window.do_plans.set(True)
    hidden_window.balcony_names.delete(0, "end")
    (plans,) = hidden_window.jobs()

    assert flag(plans.args, "--open-space-zone-layer") == []


def test_a_balcony_layer_of_its_own_needs_no_names(hidden_window: Any) -> None:
    """Where the practice keeps its balconies on a layer of their own, the
    layer is the whole answer."""
    hidden_window.do_facade.set(False)
    hidden_window.do_plans.set(True)
    hidden_window.balconies.set("06 | Zone.Balcony")
    hidden_window.balcony_names.delete(0, "end")
    (plans,) = hidden_window.jobs()

    assert flag(plans.args, "--open-space-zone-layer") == ["06 | Zone.Balcony"]


def test_the_living_room_glazing_is_only_named_when_it_is_set(hidden_window: Any) -> None:
    """It replaces the room-name route rather than narrowing it, so passing an
    empty one would be a different study, not a tidier command line."""
    hidden_window.do_facade.set(False)
    hidden_window.do_plans.set(True)
    assert flag(hidden_window.jobs()[0].args, "--livable-suffix") == []

    hidden_window.livable.insert(0, "_L")
    assert flag(hidden_window.jobs()[0].args, "--livable-suffix") == ["_L"]


# ---------------------------------------------------------------------------
# The prefix everything created is named with.
# ---------------------------------------------------------------------------
def test_both_runs_are_told_the_same_layer_prefix(hidden_window: Any) -> None:
    """One field, and every layer, view and sheet either run leaves behind
    carries it -- so all of it can be found, and deleted, in one search."""
    hidden_window.do_plans.set(True)
    facade, plans = hidden_window.jobs()

    assert flag(facade.args, "--layer-prefix") == ["14 |"]
    assert flag(plans.args, "--layer-prefix") == ["14 |"]


def test_the_archicad_wait_reaches_both_runs_in_seconds(hidden_window: Any) -> None:
    """Minutes on screen, seconds on the command line.

    The field exists because five minutes was not enough for a 455 MB export
    and there was no way to say so from the window -- the run stopped partway,
    having already done the slow part.
    """
    hidden_window.do_plans.set(True)
    hidden_window.wait_min.delete(0, "end")
    hidden_window.wait_min.insert(0, "45")
    facade, plans = hidden_window.jobs()

    assert flag(facade.args, "--timeout") == ["2700"]
    assert flag(plans.args, "--timeout") == ["2700"]


@pytest.mark.parametrize("typed", ["", "   ", "half an hour", "0", "-5"])
def test_a_wait_that_is_not_a_number_falls_back_rather_than_stopping_the_study(
    hidden_window: Any, typed: str
) -> None:
    """A mistyped wait is not a reason to refuse a run. The command line's own
    default is right for every project but the largest, so it is what an
    unreadable field means."""
    hidden_window.wait_min.delete(0, "end")
    hidden_window.wait_min.insert(0, typed)
    (facade,) = hidden_window.jobs()

    assert flag(facade.args, "--timeout") == ["1800"]


def test_reading_the_project_does_not_wait_the_exports_wait() -> None:
    """The window fills its lists in while somebody watches. Inheriting the
    half-hour meant for the export would hang it on an Archicad that is merely
    busy, so the reads keep a wait of their own."""
    assert probe.READING_SECONDS < DEFAULT_TIMEOUT_SECONDS


def test_another_offices_numbering_reaches_the_command_line(hidden_window: Any) -> None:
    """``14`` is right for a project whose layer groups end at 13. The next
    office numbers differently, which is the whole reason for the field."""
    hidden_window.prefix.delete(0, "end")
    hidden_window.prefix.insert(0, "ZZ |")
    (facade,) = hidden_window.jobs()

    assert flag(facade.args, "--layer-prefix") == ["ZZ |"]


def test_a_zone_is_offered_under_both_of_the_names_archicad_gives_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The IFC export puts one of them in ``Name`` and the other in
    ``LongName``, and which is which varies by translator. The assessment
    matches either, so offering both means a person picking from the list is
    right whichever way round their project has it."""
    from sun_study.archicad import read
    from sun_study.archicad.read import ArchicadZone

    def square(side: float) -> tuple[tuple[float, float], ...]:
        return ((0.0, 0.0), (side, 0.0), (side, side), (0.0, side))

    monkeypatch.setattr(read, "layer_names", lambda _c: {4: "06 | Zone.Units"})
    monkeypatch.setattr(
        read,
        "zones",
        lambda _c: (
            ArchicadZone(guid="a", name="3B", number="G08", outline=square(8.0), layer_index=4),
            ArchicadZone(guid="b", name="3B", number="G08", outline=square(9.0), layer_index=4),
            ArchicadZone(guid="c", name="BALC", number="BY", outline=square(3.0), layer_index=4),
            # No layer, so it cannot be attributed and is not offered.
            ArchicadZone(guid="d", name="LOOSE", number="", outline=square(9.0)),
        ),
    )

    layers, kinds = probe._zones_by_layer(object())  # type: ignore[arg-type]
    by_label = {kind.label: kind for kind in kinds}

    assert layers == ("06 | Zone.Units",)
    assert set(by_label) == {"3B", "G08", "BALC", "BY"}
    assert by_label["G08"].count == 2
    assert by_label["G08"].area_m2 == pytest.approx(72.5)  # the median of 64 and 81
    assert by_label["G08"].dwelling and not by_label["G08"].open_space
    assert by_label["BY"].open_space and not by_label["BY"].dwelling
    assert "LOOSE" not in by_label


def test_the_biggest_zones_are_listed_first_because_they_are_what_is_wanted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Somebody opening the list is looking for the apartments, and they are
    the largest thing a zone layer holds."""
    from sun_study.archicad import read
    from sun_study.archicad.read import ArchicadZone

    def square(side: float) -> tuple[tuple[float, float], ...]:
        return ((0.0, 0.0), (side, 0.0), (side, side), (0.0, side))

    monkeypatch.setattr(read, "layer_names", lambda _c: {1: "06 | Zone.Units"})
    monkeypatch.setattr(
        read,
        "zones",
        lambda _c: (
            ArchicadZone(guid="a", name="SC", number="", outline=square(1.5), layer_index=1),
            ArchicadZone(guid="b", name="G08", number="", outline=square(8.0), layer_index=1),
            ArchicadZone(guid="c", name="BY", number="", outline=square(3.0), layer_index=1),
        ),
    )

    _, kinds = probe._zones_by_layer(object())  # type: ignore[arg-type]

    assert [kind.label for kind in kinds] == ["G08", "BY", "SC"]


# ---------------------------------------------------------------------------
# Stopping a run, and what it owes the project on the way out.
# ---------------------------------------------------------------------------
#: A stand-in for a run: it holds something it must give back, and it is
#: *executing* while it waits rather than sleeping in one call. That is not
#: incidental. On Windows a pending interrupt is delivered between bytecodes,
#: so a single long ``time.sleep`` swallows it until the sleep is over --
#: measured at 10 s of 10 -- while a run doing work takes it at the next call
#: boundary, measured at 1.01 s of a 10 s wait.
HOLDS_THE_LAYERS = """
import time
from sun_study.cli import listen_for_stop

listen_for_stop()
print("holding the layer state", flush=True)
try:
    for _ in range(1200):
        time.sleep(0.05)
except KeyboardInterrupt:
    print("put the layers back", flush=True)
"""


def test_a_stopped_run_puts_the_project_back_before_it_goes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole point of the Stop button. A run holds the project at the
    export's layer state and restores it in a ``finally``; a process that is
    merely killed never reaches it, and Windows ``terminate()`` is
    ``TerminateProcess``, which kills. So the stop arrives as a signal the run
    can unwind from, and this is the proof it does.
    """
    import sys
    import time

    from sun_study.app import runner

    monkeypatch.setattr(runner, "command_prefix", lambda: [sys.executable, "-u"])
    said: list[str] = []
    ended: list[int] = []
    run = runner.Run(["-c", HOLDS_THE_LAYERS], on_line=said.append, on_done=ended.append)
    run.start()

    deadline = time.monotonic() + 30.0
    while "holding the layer state" not in said and time.monotonic() < deadline:
        time.sleep(0.05)
    assert "holding the layer state" in said, said

    asked_at = time.monotonic()
    run.stop()
    while run.running and time.monotonic() < deadline:
        time.sleep(0.05)
    took = time.monotonic() - asked_at

    assert "put the layers back" in said, said
    assert ended, "the run never reported that it had finished"
    # Comfortably inside the grace period, which is what says the run stopped
    # because it was asked rather than because it was killed at the deadline.
    assert took < runner.GRACE_SECONDS / 2, f"took {took:.1f}s to come back"


def test_the_stop_signal_becomes_an_exception_the_finally_blocks_answer_to() -> None:
    """Installed rather than relied on: neither signal's default disposition
    unwinds, so without this the handlers are the operating system's and the
    layer state is lost whichever way the run is stopped."""
    import signal

    from sun_study.cli import listen_for_stop

    before = {
        name: signal.getsignal(getattr(signal, name))
        for name in ("SIGTERM", "SIGBREAK", "SIGINT")
        if hasattr(signal, name)
    }
    try:
        listen_for_stop()
        for name in before:
            handler = signal.getsignal(getattr(signal, name))
            assert callable(handler), f"{name} left at its default"
            with pytest.raises(KeyboardInterrupt):
                handler(getattr(signal, name), None)
    finally:
        for name, handler in before.items():
            signal.signal(getattr(signal, name), handler)


# ---------------------------------------------------------------------------
# The application icon.
# ---------------------------------------------------------------------------
def test_the_icon_ships_with_every_size_windows_asks_for() -> None:
    """One .ico, seven sizes.

    Windows asks at 16 in the title bar, 32 in the task bar, 48 in a folder
    and 256 in Alt-Tab, and an .ico carrying only the big one is resampled
    down to a smear at the size somebody actually sees most often.
    """
    import struct

    found = window.icon_path()
    assert found is not None, "the icon must be in the checkout, not just the build"

    raw = found.read_bytes()
    reserved, kind, count = struct.unpack("<HHH", raw[:6])
    assert (reserved, kind) == (0, 1), "not an .ico at all"

    sizes = {struct.unpack("<BB", raw[6 + i * 16 : 8 + i * 16]) for i in range(count)}
    widths = {width or 256 for width, _ in sizes}
    assert {16, 32, 48, 256} <= widths, f"missing a size Windows asks for: {sorted(widths)}"


def test_a_checkout_with_no_icon_still_opens(monkeypatch: pytest.MonkeyPatch) -> None:
    """How the window looks, not whether the study runs."""
    monkeypatch.setattr(window, "icon_path", lambda: None)
    root = tk.Tk()
    root.withdraw()
    try:
        window.wear_the_icon(root)
    finally:
        root.destroy()


def test_the_window_is_dressed_before_it_is_shown(monkeypatch: pytest.MonkeyPatch) -> None:
    """``--icon`` at build time reaches Explorer and the task bar and never
    reaches Tk. Without this the .exe carries the right icon in the folder and
    opens a window wearing the Tk feather, which is the half a job somebody
    notices immediately.

    ``default=`` rather than a plain call, so the dialogs and message boxes
    raised later inherit it instead of each needing to be found.
    """
    worn: list[dict[str, Any]] = []

    class Fake:
        def iconbitmap(self, **kwargs: Any) -> None:
            worn.append(kwargs)

    window.wear_the_icon(Fake())  # type: ignore[arg-type]

    assert worn, "the window was never given the icon"
    assert worn[0]["default"].endswith("sun-study.ico")


def test_the_packaged_build_looks_where_it_unpacked_itself(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A --onefile build unpacks to a fresh temp directory on every start, so
    the icon is neither beside the .exe nor beside the module -- it is
    wherever ``sys._MEIPASS`` points this time."""
    bundled = tmp_path / "assets" / "sun-study.ico"
    bundled.parent.mkdir()
    bundled.touch()  # icon_path only asks whether it is there
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)

    assert window.icon_path() == bundled
