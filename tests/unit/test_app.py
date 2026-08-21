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

import tkinter as tk
from itertools import pairwise
from typing import Any

import pytest

from sun_study.app import probe, window
from sun_study.app.runner import CLI_MARKER, child_environment, command_prefix
from sun_study.archicad.connection import Instance


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


def test_nothing_ticked_asks_for_nothing(hidden_window: Any) -> None:
    hidden_window.do_facade.set(False)
    hidden_window.do_plans.set(False)
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
            kinds = (
                widgets.Combobox | widgets.Entry | widgets.Checkbutton | widgets.Button
            )
            if isinstance(child, kinds):
                found.append(child)
            found.extend(controls(child))
        return found

    bare = [
        str(widget)
        for widget in controls(hidden_window.root)
        if not widget.bind("<Enter>")
    ]
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
    monkeypatch.setattr(
        probe, "options", lambda port: probe.ProjectOptions(tapir_missing=True)
    )
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
