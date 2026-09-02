"""Which element belongs to which legend row.

A shadow diagram's rows are found either by layer or by Element ID, and which
of the two a practice has filled in is not something the tool gets to assume.
Measured on the Crows Nest model: 17,276 of 22,513 solids carry no Element ID
at all, and every one of the six massings is separated by layer instead --
``03 | Site context.TOD Buildings``, ``03 | Site Context.Existing Buildings To
Stay``. On another model the layers are shared and the ID is all there is.
So both routes, and these are the tests that say so.
"""

from __future__ import annotations

from sun_study.core.geometry import TriangleMesh
from sun_study.ingest.ifc import IfcElement
from sun_study.ingest.scene import _claims


def element(name: str = "", layer: str = "") -> IfcElement:
    return IfcElement(
        global_id="0",
        ifc_class="IfcBuildingElementProxy",
        name=name,
        long_name="",
        predefined_type="",
        storey=None,
        mesh=TriangleMesh.empty(),
        layer=layer,
    )


def test_a_layer_name_claims_the_elements_on_it() -> None:
    """The Crows Nest route. No Element ID anywhere on these solids."""
    tod = element(layer="03 | Site context.TOD Buildings")

    assert _claims(tod, ["03 | Site context.TOD Buildings"])
    assert not _claims(tod, ["03 | Site Context.Existing Buildings To Stay"])


def test_a_layer_is_matched_whole_and_not_by_prefix() -> None:
    """``...TOD Buildings`` must not quietly sweep in ``...TOD Buildings 2D``.
    A layer name is a thing that exists exactly."""
    two_d = element(layer="03 | Site context.TOD Buildings 2D")

    assert not _claims(two_d, ["03 | Site context.TOD Buildings"])


def test_an_element_id_is_matched_by_prefix() -> None:
    """The other route. One massing is TOD-01, TOD-02, TOD-PODIUM, and listing
    them individually is a rule nobody keeps current."""
    assert _claims(element(name="TOD-PODIUM"), ["TOD-"])
    assert _claims(element(name="TOD-01"), ["TOD-"])
    assert not _claims(element(name="SEARS-01"), ["TOD-"])


def test_matching_survives_case_and_stray_spacing() -> None:
    """A layer name typed at a command line will not match ``03 |  Site
    context.TOD Buildings`` byte for byte, and failing on that is a trap."""
    messy = element(layer="03 |  Site   context.TOD Buildings")

    assert _claims(messy, ["03 | site context.tod buildings"])


def test_an_empty_selector_claims_nothing_rather_than_everything() -> None:
    """A trailing comma in ``--shadow-source "TOD=a,b,"`` is a typo, not an
    instruction to sweep the whole model into one legend row."""
    assert not _claims(element(name="anything", layer="any layer"), ["", "   "])


# -- what the export carries ----------------------------------------------


def test_only_starts_from_nothing_and_shows_what_it_is_given() -> None:
    """The default shows everything, which is right for an apartment run and
    wasteful for a shadow diagram: on Crows Nest it switched on 167 of 195
    layers and handed over 22,513 solids to cast shadows off about eight
    thousand -- '00 | Temp Delete' and the 2D site context among them."""
    from sun_study.archicad.layers import LayerState, _with

    layers = [
        LayerState(identifier="a", name="03 | Site context.TOD Buildings", hidden=True),
        LayerState(identifier="b", name="00 | Temp Delete", hidden=False),
        LayerState(identifier="c", name="03 | Site.Mesh", hidden=False),
    ]
    everything_off = dict.fromkeys((layer.identifier for layer in layers), True)

    wanted = _with(everything_off, layers, shown=["03 | Site context.TOD Buildings"])

    assert wanted["a"] is False, "the named layer is on"
    assert wanted["b"] is True, "a temp layer that was on is off"
    assert wanted["c"] is True, "the site mesh cannot reach the occluders"
