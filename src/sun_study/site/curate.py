"""Curation: the office's legend categories, and how raw data maps onto them.

The reference sheets are curated by hand: LEP zoning is the base, and named
institutions -- schools, hospitals, civic buildings -- are coloured by their
function regardless of zone and labelled by name. These are the rules that
reproduce that, and the palette they are drawn in, read off the office's own
Oatley legend.

Colours are here rather than in the drawing code because they are what the
legend *says*, not how Archicad paints it. The add-on's ``CreateFills`` takes
an RGB directly (D82), so no pen-table matching stands between this table and
the sheet.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = [
    "CATEGORIES",
    "Category",
    "category",
    "is_laneway",
    "keyword_category",
    "rgb",
    "short_street_name",
    "zone_label",
    "zone_to_category",
]


@dataclass(frozen=True)
class Category:
    id: str
    label: str
    fill: str
    """``#rrggbb``."""
    stroke: str | None = None
    """Outline colour for the categories the Oatley legend outlines."""


#: The office palette, per the Oatley (2546) legend.
CATEGORIES: tuple[Category, ...] = (
    Category("site", "SITE", "#e8402a"),
    Category("medical", "MEDICAL", "#f27ba9", "#e5237e"),
    Category("education", "EDUCATION", "#93b7f2", "#2e6be6"),
    Category("childCare", "CHILD CARE / AGED CARE", "#f4f8ff", "#2e6be6"),
    Category("mixedUse", "MIXED USE CLUSTER RESIDENTIAL/COMMERCIAL", "#e957c9"),
    Category("retail", "ACTIVE RETAIL", "#e4d3f0"),
    Category("openSpace", "OPEN SPACE", "#e6f0d8"),
    Category("community", "COMMUNITY HUB", "#e0f2eb", "#a5c3d8"),
    Category("r2", "R2 LOW DENSITY RESIDENTIAL", "#fce9e8"),
    Category("r3", "R3 MEDIUM DENSITY RESIDENTIAL", "#f7c1bd"),
    Category("r4", "R4 HIGH DENSITY RESIDENTIAL", "#f29e97"),
    Category("localCentre", "LOCAL CENTRE", "#f0b3c8"),
    Category("commercial", "COMMERCIAL CENTRE", "#d9c2ee"),
    Category("industrial", "GENERAL INDUSTRIAL", "#c9a8ec"),
    Category("infrastructure", "INFRASTRUCTURE FACILITY", "#f0e2e6", "#ef8b1d"),
    Category("water", "WATERWAY", "#bcdcf0"),
    Category("railway", "RAILWAY TRACKS", "#f3c488", "#ef8b1d"),
    Category("heritage", "GENERAL HERITAGE SITE", "#c8a165"),
)

_BY_ID = {c.id: c for c in CATEGORIES}


def category(identifier: str) -> Category:
    return _BY_ID[identifier]


def rgb(colour: str) -> tuple[float, float, float]:
    """``#rrggbb`` to channels in 0..1, which is what the add-on takes."""
    value = colour.lstrip("#")
    return int(value[0:2], 16) / 255.0, int(value[2:4], 16) / 255.0, int(value[4:6], 16) / 255.0


def zone_to_category(code: str | None) -> str | None:
    """NSW LEP zone code to a category; ``None`` leaves the ground uncoloured."""
    c = (code or "").upper()
    if c.startswith("R2"):
        return "r2"
    if c[:2] in ("R1", "R3"):
        return "r3"
    if c.startswith("R4"):
        return "r4"
    if c.startswith("R5"):
        return "r2"
    if c in ("B1", "B2", "E1"):
        return "localCentre"
    if c in ("B3", "E2", "E3"):
        return "commercial"
    if c in ("B4", "MU1", "B"):
        return "mixedUse"
    if c.startswith(("IN", "E4", "E5")):
        return "industrial"
    if c.startswith("SP"):
        return "infrastructure"
    if c.startswith("RE"):
        return "openSpace"
    if c.startswith("W"):
        return "water"
    if len(c) == 2 and c[0] == "C" and c[1].isdigit():
        return "openSpace"
    if c.startswith("RU"):
        return "r2"
    return None


def zone_label(code: str | None) -> str | None:
    """The zone as a neighbour box names it: ``R3 MEDIUM DENSITY RESIDENTIAL``."""
    c = (code or "").upper()
    if c.startswith("R2"):
        return "R2 LOW DENSITY RESIDENTIAL"
    if c.startswith("R3"):
        return "R3 MEDIUM DENSITY RESIDENTIAL"
    if c.startswith("R4"):
        return "R4 HIGH DENSITY RESIDENTIAL"
    if c.startswith("R1"):
        return "R1 GENERAL RESIDENTIAL"
    if c in ("B2", "E1"):
        return "E1 LOCAL CENTRE"
    if c in ("B4", "MU1"):
        return "MU1 MIXED USE"
    if c.startswith("SP"):
        return "SP2 INFRASTRUCTURE"
    if c.startswith("RE"):
        return "RE1 PUBLIC RECREATION"
    if c.startswith(("E4", "IN")):
        return "E4 GENERAL INDUSTRIAL"
    return c or None


_KEYWORDS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "childCare",
        re.compile(
            r"KINDERGARTEN|PRE-?SCHOOL|CHILD CARE|CHILDCARE|EARLY LEARNING|EARLY CHILDHOOD|"
            r"AGED CARE|NURSING HOME|RETIREMENT|CONVALESCENT"
        ),
    ),
    ("education", re.compile(r"SCHOOL|COLLEGE|TAFE|UNIVERSITY|GRAMMAR")),
    ("medical", re.compile(r"HOSPITAL|NURSING|MEDICAL|SURGERY|HEALTH|AMBULANCE|HOSPICE")),
    ("retail", re.compile(r"SHOPPING|PLAZA|\bMALL\b|MARKETPLACE|\bMETRO\b|CENTRE POINT")),
    (
        "community",
        re.compile(
            r"CHURCH|CATHEDRAL|MOSQUE|MASJID|TEMPLE|SYNAGOGUE|LIBRARY|COMMUNITY|COUNCIL|CIVIC|"
            r"TOWN HALL|POLICE|FIRE STATION|\bSES\b|COURT HOUSE|MUSEUM|AQUATIC|LEISURE CENTRE|"
            r"\bRSL\b|BOWLING|SENIOR CITIZENS"
        ),
    ),
)


def keyword_category(name: str) -> str | None:
    """A named building complex by what its name says it is.

    The register's type codes are unreliable; the names are consistently
    descriptive. ``None`` means not worth showing.
    """
    upper = name.upper()
    for identifier, pattern in _KEYWORDS:
        if pattern.search(upper):
            return identifier
    return None


_ABBREVIATIONS = (
    ("STREET", "ST"),
    ("ROAD", "RD"),
    ("AVENUE", "AVE"),
    ("PARADE", "PDE"),
    ("HIGHWAY", "HWY"),
    ("DRIVE", "DR"),
    ("LANE", "LN"),
    ("PLACE", "PL"),
    ("CRESCENT", "CRES"),
    ("BOULEVARD", "BVD"),
    ("TERRACE", "TCE"),
    ("COURT", "CT"),
)


def short_street_name(name: str) -> str:
    label = name.upper()
    for full, short in _ABBREVIATIONS:
        label = re.sub(rf"\b{full}\b", short, label, count=1)
    return label


def is_laneway(hierarchy: int, lanes: int | None, name: str) -> bool:
    """A lane gets a plain label; a street gets the arrow band.

    Neither field NSW publishes decides this alone -- Dickson Lane is a
    ``LocalRoad`` exactly like Hall Street -- so the rule combines them.
    """
    if hierarchy >= 7:
        return True
    if re.search(r"\b(LANE|LN|WALK|PATH|TRACK|STEPS|ARCADE|CLOSE)\b", name, re.IGNORECASE):
        return True
    return lanes == 1
