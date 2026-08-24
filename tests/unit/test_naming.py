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
    """The prefix is process-wide, so a test that changes it puts it back.

    Without this, one test naming its layers ``ZZ |`` would leave every later
    test in the session measuring a project it never described.
    """
    before = naming.prefix()
    yield
    naming.set_prefix(before)


def test_the_default_is_the_reference_projects_next_free_group() -> None:
    """``14`` because that project's layer groups run 00 to 13. Right there,
    a guess anywhere else, which is the whole reason for the flag."""
    assert naming.prefix() == "14 |"
    assert naming.layer("Results") == "14 | Sun Study.Results"
    assert naming.named("Sun Study 09:00") == "14 | Sun Study 09:00"


def test_a_chosen_prefix_reaches_every_kind_of_name() -> None:
    """One flag, and everything the run leaves behind carries it -- so a
    person can find all of it, and delete all of it, with one search."""
    naming.set_prefix("ZZ |")

    assert naming.prefix() == "ZZ |"
    assert naming.group() == "ZZ | Sun Study"
    assert naming.layer("Results") == "ZZ | Sun Study.Results"
    assert naming.named("Solar Model") == "ZZ | Solar Model"
    # The names other modules hand out, which used to be constants.
    assert default_layer_name() == "ZZ | Sun Study.Results"
    assert export_combination() == "ZZ | Sun Study Export"


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
    assert naming.layer("Facade") == "14 | Sun Study.Facade"


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
