"""The address parser and the register's quirks, without a network."""

from __future__ import annotations

from typing import Any

import pytest

from sun_study.site import geocode as module
from sun_study.site.geocode import Geocode, covers, expand_numbers, geocode, parse_address, suggest


def test_a_unit_address_resolves_to_its_parent_number() -> None:
    parsed = parse_address("5/212 Bondi Rd, Bondi NSW 2026")
    assert parsed.numbers == ("212",)
    assert parsed.street == "BONDI ROAD"
    assert parsed.locality == "BONDI"


def test_a_range_is_expanded_by_the_street_side() -> None:
    parsed = parse_address("26-30 Campsie St, Campsie NSW 2194")
    assert parsed.numbers == ("26", "28", "30", "26-30")
    assert expand_numbers(["211-215"]) == ["211", "213", "215", "211-215"]


def test_a_street_that_begins_with_a_number_keeps_its_name() -> None:
    parsed = parse_address("7 Hills Road")
    assert parsed.numbers == ("7",)
    assert parsed.street == "HILLS ROAD"


def test_nonsense_is_refused_with_an_example() -> None:
    with pytest.raises(ValueError, match="Cannot parse"):
        parse_address("Bondi Road")


def test_coverage_follows_the_registers_conventions() -> None:
    assert covers("212", "212")
    assert covers("212A", "212")
    assert covers("212-218", "214")
    assert not covers("211-215", "212"), "ranges are parity-sided"
    assert covers("134, 136, 136A, 138", "136")
    assert not covers("212/115", "212"), "a unit at another number"


def test_geocode_takes_the_exact_match_and_centres_on_it(monkeypatch: pytest.MonkeyPatch) -> None:
    asked: list[str] = []

    def fake_query(
        _url: str, where: str, _fields: str, _count: int | None = None
    ) -> dict[str, Any]:
        asked.append(where)
        if where == "address = '26 CAMPSIE STREET CAMPSIE'":
            return {
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "geometry": {"type": "Point", "coordinates": [151.1, -33.9]},
                        "properties": {"address": "26 CAMPSIE STREET CAMPSIE", "housenumber": "26"},
                    }
                ],
            }
        return {"type": "FeatureCollection", "features": []}

    monkeypatch.setattr(module, "query_where", fake_query)
    found = geocode("26 Campsie St, Campsie NSW 2194")
    assert isinstance(found, Geocode)
    assert found.matched == ("26 CAMPSIE STREET CAMPSIE",)
    assert found.lonlat_points[0] == pytest.approx((151.1, -33.9))
    # The exact query is anchored on the number, never a bare wildcard first.
    assert asked[0].startswith("address = '26 ")


def test_a_wrong_suburb_is_refused_rather_than_substituted(monkeypatch: pytest.MonkeyPatch) -> None:
    """1 George St, Sydney has matches in Glendale and Goulburn; drawing one
    of those as the site would be a wrong sheet rather than a visible failure."""

    def fake_query(
        _url: str, where: str, _fields: str, _count: int | None = None
    ) -> dict[str, Any]:
        if "GEORGE STREET%" in where and "'1 %'" in where:
            return {
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "geometry": {"type": "Point", "coordinates": [151.6, -32.9]},
                        "properties": {"address": "1 GEORGE STREET GLENDALE", "housenumber": "1"},
                    }
                ],
            }
        return {"type": "FeatureCollection", "features": []}

    monkeypatch.setattr(module, "query_where", fake_query)
    with pytest.raises(ValueError, match=r"not found in SYDNEY.*GLENDALE"):
        geocode("1 George St, Sydney NSW")


def test_suggestions_put_the_typed_street_first(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_query(
        _url: str, _where: str, _fields: str, _count: int | None = None
    ) -> dict[str, Any]:
        return {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "geometry": None,
                    "properties": {
                        "address": "212 BIRRELL STREET BONDI JUNCTION",
                        "housenumber": "212",
                    },
                },
                {
                    "type": "Feature",
                    "geometry": None,
                    "properties": {"address": "212-218 BONDI ROAD BONDI", "housenumber": "212-218"},
                },
            ],
        }

    monkeypatch.setattr(module, "query_where", fake_query)
    assert suggest("212 bondi rd")[0] == "212-218 BONDI ROAD BONDI"
    assert suggest("21") == []
