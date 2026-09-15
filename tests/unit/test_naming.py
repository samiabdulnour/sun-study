"""The prefix every name this tool creates is built from.

Two things are worth holding here. That a prefix chosen on the command line
reaches *every* name -- layers, combinations, surfaces, views, layouts -- and
not merely the ones somebody remembered to thread it through. And that no
module captures it at import, because that failure is invisible: the run draws
on the layer the flag asked for and cleans up the one the constant froze, and
both names look right in isolation.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from pathlib import Path

import pytest

from sun_study.archicad import naming
from sun_study.archicad.draw import default_layer_name
from sun_study.archicad.layers import export_combination

PACKAGE_ROOT = Path(__file__).resolve().parents[2] / "src" / "sun_study"


@pytest.fixture(autouse=True)
def restored() -> Iterator[None]:
    """The prefixes are process-wide, so a test that changes one puts it back.

    Without this, one test naming its layers ``ZZ |`` would leave every later
    test in the session measuring a project it never described. Both of them,
    because the site group the context is filed under is process-wide in the
    same way and leaks in the same way.
    """
    before, context_before = naming.prefix(), naming.context_prefix()
    yield
    naming.set_prefix(before)
    naming.set_context_prefix(context_before)


def test_the_default_is_the_reference_projects_next_free_group() -> None:
    """``14`` because that project's layer groups run 00 to 13. Right there,
    a guess anywhere else, which is the whole reason for the flag."""
    assert naming.prefix() == "14 |"
    assert naming.layer("Results") == "14 | Solar Analysis.Results"
    assert naming.named("Solar Analysis 09:00") == "14 | Solar Analysis 09:00"


def test_a_chosen_prefix_reaches_every_kind_of_name() -> None:
    """One flag, and everything the run leaves behind carries it -- so a
    person can find all of it, and delete all of it, with one search."""
    naming.set_prefix("ZZ |")

    assert naming.prefix() == "ZZ |"
    assert naming.group() == "ZZ | Solar Analysis"
    assert naming.layer("Results") == "ZZ | Solar Analysis.Results"
    assert naming.named("Solar Model") == "ZZ | Solar Model"
    # The names other modules hand out, which used to be constants.
    assert default_layer_name() == "ZZ | Solar Analysis.Results"
    assert export_combination() == "ZZ | Solar Analysis Export"


def test_an_empty_prefix_is_refused_because_it_matches_everything() -> None:
    """``remove_previous`` deletes the navigator items whose name starts with
    the prefix. An empty one is not a tidier name, it is the practice's
    drawings deleted by a sun study's clean-up."""
    for attempt in ("", "   ", "\t"):
        with pytest.raises(ValueError, match="cannot be empty"):
            naming.set_prefix(attempt)
    assert naming.prefix() == "14 |"


def test_whitespace_is_collapsed_because_a_copied_name_brings_it_along() -> None:
    """``14  |`` would name everything this run makes and match nothing the
    next run searches for."""
    assert naming.set_prefix("  14   |  ") == "14 |"
    assert naming.layer("Facade") == "14 | Solar Analysis.Facade"


def test_an_unset_option_leaves_the_prefix_alone() -> None:
    """So a caller can pass an optional flag straight through."""
    naming.set_prefix("ZZ |")
    assert naming.set_prefix(None) == "ZZ |"


def _module_level_names(tree: ast.Module) -> Iterator[ast.AST]:
    """Every expression evaluated at import, skipping function and class bodies."""
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            continue
        yield from ast.walk(node)


def test_no_module_builds_a_name_at_import_time() -> None:
    """The trap this design exists to remove.

    ``FACADE_LAYER = naming.layer("Facade")`` at module level is evaluated
    before any command line is read, so it is always the *default* prefix. A
    run told to use another would then draw on one layer and search another
    for its own work, and every name involved looks correct on its own.

    Checked by walking the source rather than by discipline, because the
    failure produces no error and no wrong-looking name.
    """
    offenders: list[str] = []
    for path in sorted(PACKAGE_ROOT.rglob("*.py")):
        if path.name == "naming.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in _module_level_names(tree):
            if not isinstance(node, ast.Call):
                continue
            function = node.func
            called = (
                function.attr
                if isinstance(function, ast.Attribute)
                else function.id
                if isinstance(function, ast.Name)
                else ""
            )
            if called in {"layer", "named", "group", "prefix"}:
                offenders.append(f"{path.relative_to(PACKAGE_ROOT)}:{node.lineno} {called}(...)")

    assert not offenders, (
        "A name built from the layer prefix at import time is frozen at the "
        "default, whatever --layer-prefix says. Move it into a function:\n  "
        + "\n  ".join(offenders)
    )


def test_a_tool_that_says_nothing_gets_its_own_number() -> None:
    """The four drawings file under four numbers, and the command says which
    it is. Before this they shared one, and the layer list ran them together
    with only the group word after the number telling them apart."""
    assert naming.set_prefix(None, default=naming.SHADOW_PREFIX) == "15 |"
    assert naming.layer("9AM", naming.SHADOW_WORD) == "15 | Shadow Diagram.9AM"

    assert naming.set_prefix(None, default=naming.SUN_VIEW_PREFIX) == "16 |"
    assert naming.set_prefix(None, default=naming.SITE_PREFIX) == "17 |"

    # The solar analysis keeps the number it has always had, so nothing
    # anybody has already drawn moves.
    assert naming.SOLAR_PREFIX == naming.DEFAULT_PREFIX == "14 |"


def test_the_run_beats_the_tools_own_number() -> None:
    """``--layer-prefix`` is still the answer for an office whose own groups
    run past 17. The default is only what happens when nobody says."""
    assert naming.set_prefix("25 |", default=naming.SHADOW_PREFIX) == "25 |"
    assert naming.layer("9AM", naming.SHADOW_WORD) == "25 | Shadow Diagram.9AM"


def test_neither_a_choice_nor_a_default_leaves_the_prefix_alone() -> None:
    """The shape the optional flag relies on: a command with nothing to say
    about the prefix does not reset it."""
    naming.set_prefix("ZZ |")
    assert naming.set_prefix(None) == "ZZ |"


def test_an_empty_prefix_is_still_refused_even_beside_a_default() -> None:
    """The default does not rescue it. An empty prefix matches every layout in
    the project, and a clean-up that matched them all is the failure the
    refusal exists for -- a tool default standing in quietly would hide the
    typo rather than report it."""
    with pytest.raises(ValueError, match="cannot be empty"):
        naming.set_prefix("   ", default=naming.SHADOW_PREFIX)


def test_the_four_numbers_are_four() -> None:
    """Two tools sharing a number would put two drawings on one layer and let
    each one's clean-up delete the other's sheets."""
    numbers = [
        naming.SOLAR_PREFIX,
        naming.SHADOW_PREFIX,
        naming.SUN_VIEW_PREFIX,
        naming.SITE_PREFIX,
    ]
    assert len(set(numbers)) == len(numbers), numbers


def test_every_command_that_sets_the_prefix_says_which_tool_it_is() -> None:
    """A command that calls ``set_prefix`` without its own number falls back
    to whatever the module default is -- which is the solar analysis's 14 --
    and files its drawing on top of another tool's layers.

    Walked rather than trusted, for the same reason as the import-time check
    above: the run succeeds, the names look right, and the only symptom is two
    drawings sharing a number.
    """
    tree = ast.parse((PACKAGE_ROOT / "cli.py").read_text(encoding="utf-8"), filename="cli.py")
    silent: list[str] = []
    seen = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        function = node.func
        if not (isinstance(function, ast.Attribute) and function.attr == "set_prefix"):
            continue
        seen += 1
        if not any(keyword.arg == "default" for keyword in node.keywords):
            silent.append(f"cli.py:{node.lineno}")

    assert seen, "no set_prefix call found in cli.py -- has it moved?"
    listed = "".join(f"{chr(10)}  {where}" for where in silent)
    assert not silent, (
        "These set the layer prefix without naming the tool they belong to, "
        f"so they take the solar analysis number:{listed}"
    )


def test_the_context_goes_in_the_offices_own_site_group() -> None:
    """Not one of the tool's four numbers. The neighbours and the ground under
    them are the neighbourhood, not the study, and the template already has a
    group for them -- on the reference project, under '03 ---- SITE' beside
    the boundaries and the survey mesh."""
    assert naming.context_layer() == "03 | Site Context.3D"
    assert naming.future_layer() == "03 | Site Context.Future buildings"


def test_another_offices_site_group_moves_both_of_them() -> None:
    """One number, and the wording after it is the template's rather than
    anybody's to choose."""
    naming.set_context_prefix("50 |")

    assert naming.context_layer() == "50 | Site Context.3D"
    assert naming.future_layer() == "50 | Site Context.Future buildings"


def test_the_site_group_is_not_the_tools_own_number() -> None:
    """Changing where the study is filed must not move the neighbours, and
    changing where the neighbours are filed must not move the study. They were
    one setting before, and one number cannot mean both."""
    naming.set_prefix("15 |")
    naming.set_context_prefix("03 |")

    assert naming.context_layer().startswith("03 |")
    assert naming.layer("9AM", naming.SHADOW_WORD).startswith("15 |")

    naming.set_context_prefix("50 |")
    assert naming.layer("9AM", naming.SHADOW_WORD).startswith("15 |"), "the study did not move"


def test_an_empty_site_group_is_refused() -> None:
    """'| Site Context.3D' is not a layer name anybody meant to type."""
    with pytest.raises(ValueError, match="cannot be empty"):
        naming.set_context_prefix("  ")


def test_the_clean_up_steps_off_a_layout_before_deleting_layouts() -> None:
    """Archicad closes mid-command if the layout being deleted is the current
    database. D87 recorded that on 11 September 2026 and `_remove_layout` has
    guarded it ever since; `remove_previous` did not, and took Archicad down
    on 15 September the first time a run made layouts and then cleared them.

    The order is the whole test: the move off the layout has to come before
    the first DeleteNavigatorItems, not after it.
    """
    from sun_study.archicad.views import remove_previous
    from tests.unit.test_archicad_adapter import connect

    connection, transport = connect(
        {
            "GetCurrentWindowType": {"currentWindowType": "Layout"},
            "ChangeWindow": {"success": True},
            "GetNavigatorItemTree": {"navigatorItemTree": {"name": "root", "children": []}},
        }
    )

    remove_previous(connection, prefix="14 |")

    order = transport.commands()
    assert "ChangeWindow" in order, "it never stepped off the layout"
    moved = order.index("ChangeWindow")
    deletes = [i for i, name in enumerate(order) if name == "DeleteNavigatorItems"]
    assert all(moved < at for at in deletes), "it deleted before stepping off"
    assert transport.parameters_for("ChangeWindow") == {"windowType": "FloorPlan"}


def test_the_clean_up_steps_off_a_document_too_not_only_a_layout() -> None:
    """The narrow version of this guard tested for Layout, because that is
    where D87 met the crash. A sun-view run mends its 3D Documents and is left
    standing in one, so the window is a document when the views are deleted
    next -- the guard did not fire and Archicad closed mid-command. Three
    times: twice on Bondi, once on a clean file, always on the second of three
    dates, because the first has nothing to delete yet."""
    from sun_study.archicad.views import remove_previous
    from tests.unit.test_archicad_adapter import connect

    connection, transport = connect(
        {
            "GetCurrentWindowType": {"currentWindowType": "Document3D"},
            "ChangeWindow": {"success": True},
            "GetNavigatorItemTree": {"navigatorItemTree": {"name": "root", "children": []}},
        }
    )

    remove_previous(connection, prefix="14 |")

    assert transport.parameters_for("ChangeWindow") == {"windowType": "FloorPlan"}
    order = transport.commands()
    moved = order.index("ChangeWindow")
    assert all(moved < at for at, name in enumerate(order) if name == "DeleteNavigatorItems")


def test_the_clean_up_does_not_move_when_it_is_not_on_a_layout() -> None:
    """A run standing on the floor plan has nothing to step off, and moving
    anyway would take the database somewhere the caller did not ask for."""
    from sun_study.archicad.views import remove_previous
    from tests.unit.test_archicad_adapter import connect

    connection, transport = connect(
        {
            "GetCurrentWindowType": {"currentWindowType": "FloorPlan"},
            "GetNavigatorItemTree": {"navigatorItemTree": {"name": "root", "children": []}},
        }
    )

    remove_previous(connection, prefix="14 |")

    assert "ChangeWindow" not in transport.commands()
