"""Reading Rhino ``.3dm`` geometry, the validation-only ingest path.

Fixtures are synthetic and written by ``rhino3dm`` at test time, because the
real reference model is client geometry and cannot enter a public repository.

One path is therefore *not* covered here: recovering the cached render mesh
from a NURBS Brep. Synthesising a Brep with a populated render mesh needs
Rhino's meshing kernel, which ``rhino3dm`` does not have. That path is verified
instead against the reference model, where it recovers 25,825 of 25,845 faces
and reproduces the published analysed-facade area of 17,780.02 m2 to within
0.013 m2 -- recorded in docs/validation.md rather than asserted in CI.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

rhino3dm = pytest.importorskip(
    "rhino3dm", reason="rhino3dm is an optional validation-only dependency"
)

from sun_study.core.sampling import FaceSelection, triangle_samples  # noqa: E402
from sun_study.ingest.rhino import read_3dm  # noqa: E402


def write_box_3dm(
    path: Path,
    *,
    size: tuple[float, float, float] = (4.0, 3.0, 2.0),
    unit_system: object | None = None,  # None means metres; see below
    layer_name: str = "3D_ Input::20_ Building",
    extra_layer: str | None = None,
) -> Path:
    """A .3dm holding one box mesh on a named layer."""
    model = rhino3dm.File3dm()
    # A fresh File3dm defaults to millimetres, so writing metre coordinates into
    # one without setting this makes every length 1000x too small -- and every
    # area a million times too small, which is how this helper first failed.
    model.Settings.ModelUnitSystem = (
        rhino3dm.UnitSystem.Meters if unit_system is None else unit_system
    )

    layer = rhino3dm.Layer()
    layer.Name = layer_name
    model.Layers.Add(layer)
    if extra_layer:
        other = rhino3dm.Layer()
        other.Name = extra_layer
        model.Layers.Add(other)

    x, y, z = size
    corners = [
        (0, 0, 0), (x, 0, 0), (x, y, 0), (0, y, 0),
        (0, 0, z), (x, 0, z), (x, y, z), (0, y, z),
    ]  # fmt: skip
    mesh = rhino3dm.Mesh()
    for corner in corners:
        mesh.Vertices.Add(*corner)
    # Quads, so the quad-splitting path is exercised.
    for quad in [
        (0, 3, 2, 1), (4, 5, 6, 7), (0, 1, 5, 4),
        (2, 3, 7, 6), (1, 2, 6, 5), (3, 0, 4, 7),
    ]:  # fmt: skip
        mesh.Faces.AddFace(*quad)

    attributes = rhino3dm.ObjectAttributes()
    attributes.LayerIndex = 0
    attributes.Name = "test-box"
    model.Objects.Add(mesh, attributes)

    model.Write(str(path), 7)
    return path


def test_reads_a_mesh_and_splits_quads(tmp_path: Path) -> None:
    model = read_3dm(write_box_3dm(tmp_path / "box.3dm"))
    assert len(model.elements) == 1
    # Six quads become twelve triangles.
    assert model.elements[0].mesh.triangle_count == 12
    assert model.elements[0].name == "test-box"


def test_total_area_matches_the_box(tmp_path: Path) -> None:
    """Area is the property every square-metre figure downstream depends on."""
    model = read_3dm(write_box_3dm(tmp_path / "box.3dm", size=(4.0, 3.0, 2.0)))
    mesh = model.mesh_on_layers("20_ Building")
    samples = triangle_samples(
        mesh.triangles(), ["b"] * mesh.triangle_count, spacing_m=0.5, surface_offset_m=0.0
    )
    expected = 2 * (4 * 3) + 2 * (4 * 2) + 2 * (3 * 2)  # 24 + 16 + 12
    assert samples.total_area_m2 == pytest.approx(expected, rel=1e-12)


def test_vertical_faces_are_the_facade(tmp_path: Path) -> None:
    model = read_3dm(write_box_3dm(tmp_path / "box.3dm", size=(4.0, 3.0, 2.0)))
    mesh = model.mesh_on_layers("20_ Building")
    walls = triangle_samples(
        mesh.triangles(),
        ["b"] * mesh.triangle_count,
        spacing_m=0.5,
        faces=FaceSelection.VERTICAL,
        surface_offset_m=0.0,
    )
    assert walls.total_area_m2 == pytest.approx(2 * (4 * 2) + 2 * (3 * 2))  # 28


def test_millimetre_files_are_scaled_to_metres(tmp_path: Path) -> None:
    """The Archicad-to-Rhino route often lands in millimetres.

    Reading it unscaled makes a 4 m box 4 km across, and every shadow with it.
    """
    path = write_box_3dm(
        tmp_path / "mm.3dm",
        size=(4000.0, 3000.0, 2000.0),
        unit_system=rhino3dm.UnitSystem.Millimeters,
    )
    model = read_3dm(path)
    assert model.length_unit_scale == pytest.approx(0.001)

    extent = model.elements[0].mesh.vertices.max(axis=0) - model.elements[0].mesh.vertices.min(
        axis=0
    )
    np.testing.assert_allclose(extent, [4.0, 3.0, 2.0], rtol=1e-9)


def test_scaling_can_be_turned_off(tmp_path: Path) -> None:
    path = write_box_3dm(
        tmp_path / "mm.3dm", size=(4000.0, 3000.0, 2000.0),
        unit_system=rhino3dm.UnitSystem.Millimeters,
    )  # fmt: skip
    model = read_3dm(path, scale_to_metres=False)
    assert model.elements[0].mesh.vertices.max() == pytest.approx(4000.0)


def test_layer_filtering_keeps_only_what_was_asked_for(tmp_path: Path) -> None:
    """A real working file carries linework, text and layout furniture.

    Loading all of it would analyse drawing annotation as though it were
    building fabric.
    """
    path = write_box_3dm(
        tmp_path / "layers.3dm",
        layer_name="3D_ Input::21_IB_ Resi",
        extra_layer="Layout::Printed Lines",
    )
    assert len(read_3dm(path, layer_fragments=("21_IB_ Resi",)).elements) == 1
    assert len(read_3dm(path, layer_fragments=("Layout",)).elements) == 0
    assert len(read_3dm(path).elements) == 1


def test_missing_geolocation_is_reported_not_guessed(tmp_path: Path) -> None:
    """Rhino leaves EarthAnchorPoint unset, and its default north is (0,1,0).

    Both are indistinguishable from a deliberate setting, so the reader states
    that the file has no location rather than supplying one. Every file in the
    reference project is like this: the location lives in Grasshopper.
    """
    model = read_3dm(write_box_3dm(tmp_path / "box.3dm"))
    assert model.has_geolocation is False
    assert model.extras["latitude"] is None
    assert "NO geolocation" in model.describe()


def test_unknown_units_raise_rather_than_assume(tmp_path: Path) -> None:
    path = write_box_3dm(tmp_path / "odd.3dm", unit_system=rhino3dm.UnitSystem.Microinches)
    with pytest.raises(ValueError, match="no known conversion"):
        read_3dm(path)


def test_missing_file_raises() -> None:
    with pytest.raises(FileNotFoundError):
        read_3dm(Path("does_not_exist.3dm"))


def test_on_layers_matches_by_fragment(tmp_path: Path) -> None:
    path = write_box_3dm(tmp_path / "box.3dm", layer_name="3D_ Input::30_ Context::301_ Context")
    model = read_3dm(path)
    assert len(model.on_layers("301_ Context")) == 1
    assert len(model.on_layers("21_IB_ Resi")) == 0
    assert model.mesh_on_layers("nope").triangle_count == 0
