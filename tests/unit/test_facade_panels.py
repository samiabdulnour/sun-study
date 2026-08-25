"""Upright faces gridded in their own plane, and merged back into rectangles.

The properties that matter are geometric, and none of them need Archicad: a
face has to come back facing the way it actually faces, cells have to land on
the face rather than in its bounding box, and a rectangle merged in the panel
frame has to end up in the right place in the world. Everything the drawing
does downstream is a consequence of those four.
"""

from __future__ import annotations

import numpy as np
import pytest

from sun_study.core.facade import vertical_face_panels
from sun_study.core.geometry import TriangleMesh


def box(
    dx: float, dy: float, dz: float, origin: tuple[float, float, float] = (0, 0, 0)
) -> TriangleMesh:
    """A closed axis-aligned box, wound outward."""
    x, y, z = origin
    vertices = np.array(
        [
            (x, y, z),
            (x + dx, y, z),
            (x + dx, y + dy, z),
            (x, y + dy, z),
            (x, y, z + dz),
            (x + dx, y, z + dz),
            (x + dx, y + dy, z + dz),
            (x, y + dy, z + dz),
        ],
        dtype=np.float64,
    )
    faces = np.array(
        [
            [0, 2, 1],
            [0, 3, 2],  # bottom
            [4, 5, 6],
            [4, 6, 7],  # top
            [0, 1, 5],
            [0, 5, 4],  # -y
            [1, 2, 6],
            [1, 6, 5],  # +x
            [2, 3, 7],
            [2, 7, 6],  # +y
            [3, 0, 4],
            [3, 4, 7],  # -x
        ]
    )
    return TriangleMesh(vertices, faces)


def test_both_faces_of_a_wall_come_back_facing_opposite_ways() -> None:
    panels = vertical_face_panels(box(4.0, 0.2, 3.0), "W", spacing_m=0.5)

    assert len(panels) == 2
    normals = sorted(float(panel.normal[1]) for panel in panels)
    assert normals == pytest.approx([-1.0, 1.0])
    # Every panel is upright: the normal is horizontal, the frame is not.
    for panel in panels:
        assert float(panel.normal[2]) == pytest.approx(0.0)
        assert panel.axis_v.tolist() == [0.0, 0.0, 1.0]
        assert float(panel.axis_u @ panel.normal) == pytest.approx(0.0)


def test_the_horizontal_faces_are_not_panels() -> None:
    """A roof and a floor are sun-lit too, and are not the facade."""
    panels = vertical_face_panels(box(4.0, 4.0, 3.0), "W", spacing_m=0.5)

    assert len(panels) == 4
    assert all(abs(float(panel.normal[2])) < 1e-9 for panel in panels)


def test_the_grid_covers_the_face_area() -> None:
    panels = vertical_face_panels(box(4.0, 0.2, 3.0), "W", spacing_m=0.5)
    face = max(panels, key=lambda panel: panel.area_m2)

    assert len(face) == 8 * 6
    assert face.area_m2 == pytest.approx(12.0)


def test_a_merged_rectangle_lands_back_on_the_face() -> None:
    """The panel frame is only useful if the trip back is exact."""
    panels = vertical_face_panels(box(4.0, 0.2, 3.0), "W", spacing_m=0.5)
    face = next(panel for panel in panels if panel.normal[1] < 0)

    whole = face.rectangles(np.ones(len(face), dtype=bool))
    assert len(whole) == 1
    rectangle = whole[0]
    assert rectangle.start.tolist() == pytest.approx([0.0, 0.0, 0.0])
    assert rectangle.end.tolist() == pytest.approx([4.0, 0.0, 0.0])
    assert rectangle.height_m == pytest.approx(3.0)
    assert rectangle.area_m2 == pytest.approx(12.0)


def test_a_partial_band_merges_into_as_few_rectangles_as_tile_it() -> None:
    panels = vertical_face_panels(box(4.0, 0.2, 3.0), "W", spacing_m=0.5)
    face = next(panel for panel in panels if panel.normal[1] < 0)

    # The bottom two rows: one rectangle, four metres by one.
    low = face.cell_uv[:, 1] < 1.0
    rectangles = face.rectangles(low)
    assert len(rectangles) == 1
    assert rectangles[0].height_m == pytest.approx(1.0)
    assert sum(r.area_m2 for r in rectangles) == pytest.approx(4.0)

    # A chequerboard cannot be merged, and must still tile the same area.
    columns = np.round(face.cell_uv[:, 0] / 0.5 - 0.5).astype(int)
    rows = np.round(face.cell_uv[:, 1] / 0.5 - 0.5).astype(int)
    checker = ((columns + rows) % 2).astype(bool)
    scattered = face.rectangles(checker)
    assert sum(r.area_m2 for r in scattered) == pytest.approx(0.25 * int(checker.sum()))


def test_cells_land_on_the_face_not_in_its_bounding_box() -> None:
    """An L-shaped face must not be coloured across the notch.

    Two boxes sharing a plane make one panel with a re-entrant corner. Without
    the barycentric test the lattice would fill the whole bounding box and the
    picture would claim sunlight on a face that is not there.
    """
    tall = box(2.0, 0.2, 4.0)
    short = box(2.0, 0.2, 2.0, origin=(2.0, 0.0, 0.0))
    mesh = TriangleMesh.concatenate([tall, short])

    panels = vertical_face_panels(mesh, "W", spacing_m=0.5)
    front = [panel for panel in panels if panel.normal[1] < -0.5]
    # One plane, one facing: the two boxes' front faces are the same panel.
    assert len(front) == 1
    assert front[0].area_m2 == pytest.approx(2.0 * 4.0 + 2.0 * 2.0)
    # Nothing above the short box's head.
    high = front[0].cell_uv[front[0].cell_uv[:, 1] > 2.0]
    assert float(high[:, 0].max()) < 2.0


def test_a_face_thinner_than_the_grid_is_not_drawn() -> None:
    """Documented behaviour, not an accident: no cell centre lands on it."""
    panels = vertical_face_panels(box(4.0, 0.2, 3.0), "W", spacing_m=0.5)

    # The 0.2 m ends are upright and real; at half-metre cells they vanish.
    assert all(panel.area_m2 > 1.0 for panel in panels)
    assert len(panels) == 2

    # At a fine enough grid they come back.
    fine = vertical_face_panels(box(4.0, 0.2, 3.0), "W", spacing_m=0.05)
    assert len(fine) == 4


def test_spacing_must_be_positive() -> None:
    with pytest.raises(ValueError, match="spacing_m must be positive"):
        vertical_face_panels(box(1.0, 1.0, 1.0), "W", spacing_m=0.0)


def test_a_slab_gives_a_deck_and_a_soffit_facing_opposite_ways() -> None:
    """Both horizontal faces, and neither averaged into the other.

    Grouping keyed only on the horizontal components of the normal put a
    slab's top and its underside in one group, because both have x and y of
    zero; the panel then faced an averaged nothing and was dropped.
    """
    panels = vertical_face_panels(box(6.0, 4.0, 0.2), "S", spacing_m=0.5)
    assert panels == []

    from sun_study.core.facade import face_panels

    flat = face_panels(box(6.0, 4.0, 0.2), "S", spacing_m=0.5)
    assert len(flat) == 2
    assert sorted(float(panel.normal[2]) for panel in flat) == pytest.approx([-1.0, 1.0])
    for panel in flat:
        assert panel.area_m2 == pytest.approx(24.0)
        assert not panel.rectangles(np.ones(len(panel), dtype=bool))[0].is_upright


def test_every_rectangle_is_wound_outward() -> None:
    """The corner order has to mean the same thing on any face.

    An element built from a rectangle takes its direction from the winding, so
    a face wound the wrong way would be coated on its hidden side.
    """
    from sun_study.core.facade import face_panels

    mesh = TriangleMesh.concatenate([box(6.0, 4.0, 0.2), box(4.0, 0.2, 3.0, origin=(0, 6, 0))])
    for panel in face_panels(mesh, "M", spacing_m=0.5):
        rectangle = panel.rectangles(np.ones(len(panel), dtype=bool))[0]
        corners = rectangle.corners
        wound = np.cross(corners[1] - corners[0], corners[3] - corners[0])
        wound = wound / np.linalg.norm(wound)
        assert wound.tolist() == pytest.approx(panel.normal.tolist(), abs=1e-9)


def test_a_flat_rectangle_reports_its_plan_dimensions() -> None:
    from sun_study.core.facade import face_panels

    deck = next(
        panel
        for panel in face_panels(box(6.0, 4.0, 0.2), "S", spacing_m=0.5)
        if panel.normal[2] > 0
    )
    rectangle = deck.rectangles(np.ones(len(deck), dtype=bool))[0]

    assert rectangle.width_m == pytest.approx(6.0)
    assert rectangle.height_m == pytest.approx(4.0)
    assert rectangle.base_z == pytest.approx(0.2)
    assert rectangle.area_m2 == pytest.approx(24.0)
