"""The legend's rules: which zone is which colour, and what a name says a place is."""

from __future__ import annotations

from sun_study.site.curate import (
    CATEGORIES,
    category,
    is_laneway,
    keyword_category,
    rgb,
    short_street_name,
    zone_label,
    zone_to_category,
)


def test_zone_codes_map_to_the_office_categories() -> None:
    assert zone_to_category("R2") == "r2"
    assert zone_to_category("R3") == "r3"
    assert zone_to_category("R4") == "r4"
    assert zone_to_category("E1") == "localCentre"
    assert zone_to_category("B4") == "mixedUse"
    assert zone_to_category("E4") == "industrial"
    assert zone_to_category("SP2") == "infrastructure"
    assert zone_to_category("RE1") == "openSpace"
    assert zone_to_category("C2") == "openSpace"
    assert zone_to_category("W1") == "water"
    assert zone_to_category(None) is None
    assert zone_to_category("XYZ") is None


def test_names_decide_institutions_because_type_codes_do_not() -> None:
    assert keyword_category("Campsie Public School") == "education"
    assert keyword_category("Canterbury Hospital") == "medical"
    assert keyword_category("Little Learners Early Learning Centre") == "childCare"
    assert keyword_category("St Mel's Catholic Church") == "community"
    assert keyword_category("Campsie Centre Shopping Plaza") == "retail"
    assert keyword_category("Some Warehouse") is None


def test_every_category_has_a_colour_the_add_on_can_take() -> None:
    for cat in CATEGORIES:
        r, g, b = rgb(cat.fill)
        assert 0.0 <= r <= 1.0 and 0.0 <= g <= 1.0 and 0.0 <= b <= 1.0
    assert rgb("#ffffff") == (1.0, 1.0, 1.0)
    assert category("site").label == "SITE"


def test_street_names_are_abbreviated_the_way_the_sheets_print_them() -> None:
    assert short_street_name("Campsie Street") == "CAMPSIE ST"
    assert short_street_name("Canterbury Road") == "CANTERBURY RD"
    assert short_street_name("Street Lane") == "STREET LN".replace("STREET", "ST")


def test_a_lane_is_told_apart_from_a_street() -> None:
    assert is_laneway(5, None, "Dickson Lane")
    assert is_laneway(7, None, "Hall Street")
    assert is_laneway(5, 1, "Hall Street")
    assert not is_laneway(5, 2, "Hall Street")
    assert zone_label("R3") == "R3 MEDIUM DENSITY RESIDENTIAL"
    assert zone_label("B2") == "E1 LOCAL CENTRE"
