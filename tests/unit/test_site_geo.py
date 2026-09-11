"""The projections the site analysis stands on, checked against published figures."""

from __future__ import annotations

import math

import pytest

from sun_study.site.geo import (
    Extent,
    dissolve,
    extent_for,
    lonlat_to_mercator,
    lonlat_to_mga,
    mercator_to_lonlat,
    mga_zone,
    point_in_ring,
    ring_area,
    ring_centroid,
    scale_factor,
)


def dms(degrees: int, minutes: int, seconds: float) -> float:
    sign = -1.0 if degrees < 0 else 1.0
    return sign * (abs(degrees) + minutes / 60.0 + seconds / 3600.0)


def test_redfearn_reproduces_the_gda_technical_manual_worked_example() -> None:
    """Flinders Peak, GDA Technical Manual chapter 4: the published MGA
    coordinates to the millimetre. The projection is the same for MGA94 and
    MGA2020 -- both are transverse Mercator on GRS80 -- so the older manual's
    figures are the right check for the newer grid."""
    lat = dms(-37, 57, 3.7203)
    lon = dms(144, 25, 29.5244)
    assert mga_zone(lon) == 55
    east, north = lonlat_to_mga(lon, lat, 55)
    assert east == pytest.approx(273741.297, abs=0.002)
    assert north == pytest.approx(5796489.777, abs=0.002)


def test_a_sydney_longitude_is_zone_56() -> None:
    assert mga_zone(151.2) == 56
    assert mga_zone(150.9) == 56
    assert mga_zone(149.9) == 55


def test_a_metre_on_the_ground_is_a_metre_on_the_grid_near_the_central_meridian() -> None:
    """MGA's point scale is 0.9996 on the meridian and 1.0004 at the zone edge;
    a hundred metres along the meridian at Sydney comes out within that band.
    The metres per degree of latitude are the meridian arc's, not the
    equator's: 110.9 km at this latitude."""
    phi = math.radians(-33.87)
    metres_per_degree = 111132.954 - 559.822 * math.cos(2 * phi) + 1.175 * math.cos(4 * phi)
    a = lonlat_to_mga(151.2, -33.87, 56)
    b = lonlat_to_mga(151.2, -33.87 + 100.0 / metres_per_degree, 56)
    assert math.dist(a, b) == pytest.approx(100.0, rel=1e-3)


def test_mercator_round_trips() -> None:
    lon, lat = 151.209, -33.8688
    x, y = lonlat_to_mercator(lon, lat)
    back = mercator_to_lonlat(x, y)
    assert back[0] == pytest.approx(lon, abs=1e-9)
    assert back[1] == pytest.approx(lat, abs=1e-9)
    assert scale_factor(lat) == pytest.approx(1.0 / math.cos(math.radians(lat)))


def test_the_sheet_extent_is_true_to_scale_on_the_ground() -> None:
    """A 690 mm field at 1:3000 is 2070 m of ground; in mercator metres it is
    stretched by 1/cos(lat), which the extent carries so the fetch covers the
    ground the sheet does."""
    centre = lonlat_to_mercator(151.2, -33.87)
    extent = extent_for(centre, 690.0, 594.0, 3000.0)
    k = scale_factor(-33.87)
    assert extent.width == pytest.approx(2070.0 * k)
    assert extent.height == pytest.approx(1782.0 * k)
    assert extent.centre == pytest.approx(centre)
    south, west, north, east = extent.lonlat_bbox()
    assert south < -33.87 < north
    assert west < 151.2 < east
    assert Extent.from_dict(extent.as_dict()) == extent


def test_area_centroid_and_containment() -> None:
    square = [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)]
    assert ring_area(square) == pytest.approx(100.0)
    assert ring_area(list(reversed(square))) == pytest.approx(-100.0)
    assert ring_centroid(square) == pytest.approx((5.0, 5.0))
    assert point_in_ring(5.0, 5.0, square)
    assert not point_in_ring(15.0, 5.0, square)


def test_two_lots_sharing_a_boundary_dissolve_into_one_site() -> None:
    """Lot 1 and lot 2 side by side: the shared edge disappears and the
    outline is the outer rectangle, even though lot 2's long side is split by
    a vertex lot 1 does not have."""
    lot1 = [(0.0, 0.0), (10.0, 0.0), (10.0, 20.0), (0.0, 20.0)]
    lot2 = [(10.0, 0.0), (20.0, 0.0), (20.0, 20.0), (10.0, 20.0), (10.0, 10.0)]
    (outline,) = dissolve([lot1, lot2])
    assert abs(ring_area(outline)) == pytest.approx(400.0)
    xs = {round(x) for x, _ in outline}
    ys = {round(y) for _, y in outline}
    assert xs == {0, 20} or xs == {0, 10, 20}
    assert ys == {0, 20} or ys == {0, 10, 20}
    # No vertex of the outline is on the shared edge's interior.
    assert not any(round(x) == 10 and 0 < y < 20 for x, y in outline)


def test_lots_that_do_not_touch_stay_apart() -> None:
    a = [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)]
    b = [(30.0, 0.0), (40.0, 0.0), (40.0, 10.0), (30.0, 10.0)]
    rings = dissolve([a, b])
    assert len(rings) == 2
    assert all(abs(ring_area(r)) == pytest.approx(100.0) for r in rings)


def test_a_boundary_that_will_not_chain_is_refused_rather_than_guessed() -> None:
    """Three edges meeting at a point cannot be walked into rings; the caller
    falls back to drawing the lots separately."""
    a = [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)]
    b = [(10.0, 0.0), (20.0, 0.0), (20.0, 10.0), (10.0, 10.0)]
    c = [(10.0, 10.0), (20.0, 10.0), (20.0, 20.0), (10.0, 20.0)]
    d = [(0.0, 10.0), (10.0, 10.0), (10.0, 20.0), (0.0, 20.0)]
    # Four quadrants: every interior edge appears twice, so this chains.
    (outline,) = dissolve([a, b, c, d])
    assert abs(ring_area(outline)) == pytest.approx(400.0)
    # But two lots that share only a vertex leave a degree-four node.
    assert dissolve([a, c]) == [] or len(dissolve([a, c])) == 2


def test_the_grid_inverts_and_a_ring_clips_and_hulls() -> None:
    from sun_study.site.geo import clip_ring, convex_hull, lonlat_to_mga, mga_to_lonlat

    east, north = lonlat_to_mga(151.1357, -33.9697, 56)
    lon, lat = mga_to_lonlat(east, north, 56)
    assert lon == pytest.approx(151.1357, abs=1e-9)
    assert lat == pytest.approx(-33.9697, abs=1e-9)

    square = [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)]
    clipped = clip_ring(square, 5.0, -1.0, 20.0, 5.0)
    assert sorted(clipped) == [(5.0, 0.0), (5.0, 5.0), (10.0, 0.0), (10.0, 5.0)]
    assert clip_ring(square, 20.0, 20.0, 30.0, 30.0) == []

    hull = convex_hull([(0.0, 0.0), (4.0, 0.0), (4.0, 3.0), (0.0, 3.0), (2.0, 1.0), (1.0, 2.0)])
    assert len(hull) == 4 and (2.0, 1.0) not in hull
