"""An address string to its point(s), through the NSW address register.

NSW addressing has quirks a naive lookup gets wrong, and each is handled
here because getting it wrong draws a plausible sheet of the wrong site:

* a ranged parent -- "212 Bondi Rd" is stored only as "212-218 BONDI ROAD";
* unit noise -- "212/115 BONDI ROAD" is unit 212 at number 115, not 212;
* suffixes and units -- "212A", and "5/212 Bondi Rd" means parent 212;
* locality mismatch -- the official suburb is not always the one typed.

The query shapes are measured, not guessed. Anchoring on the street number
puts the query on the layer's address index (about 0.7 s); an OR of two
prefixes makes the optimizer abandon it (49 s), and an unanchored street
wildcard is a full scan (31 s).
"""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from sun_study.site.arcgis import query_where
from sun_study.site.geo import Point, lonlat_to_mercator

__all__ = [
    "ADDRESS_POINT",
    "Geocode",
    "ParsedAddress",
    "covers",
    "expand_numbers",
    "geocode",
    "parse_address",
    "suggest",
]

ADDRESS_POINT = (
    "https://portal.spatial.nsw.gov.au/server/rest/services/"
    "NSW_Geocoded_Addressing_Theme/FeatureServer/1"
)

STREET_TYPES: dict[str, str] = {
    "st": "STREET",
    "rd": "ROAD",
    "ave": "AVENUE",
    "av": "AVENUE",
    "hwy": "HIGHWAY",
    "pde": "PARADE",
    "dr": "DRIVE",
    "ln": "LANE",
    "pl": "PLACE",
    "cres": "CRESCENT",
    "cr": "CRESCENT",
    "ct": "COURT",
    "bvd": "BOULEVARD",
    "blvd": "BOULEVARD",
    "tce": "TERRACE",
    "esp": "ESPLANADE",
    "cct": "CIRCUIT",
    "gr": "GROVE",
    "cl": "CLOSE",
    "wy": "WAY",
}
TYPE_WORDS = frozenset(
    {
        *STREET_TYPES.values(),
        "STREET",
        "ROAD",
        "AVENUE",
        "HIGHWAY",
        "PARADE",
        "DRIVE",
        "LANE",
        "PLACE",
        "CRESCENT",
        "COURT",
        "BOULEVARD",
        "TERRACE",
        "ESPLANADE",
        "CIRCUIT",
        "GROVE",
        "CLOSE",
        "WAY",
        "MALL",
        "ROW",
        "WALK",
        "PLAZA",
        "CIRCLE",
        "RISE",
        "SQUARE",
    }
)

_NUMBER = re.compile(r"^\d+[A-Z]?(?:-\d+[A-Z]?)?$")
_RANGE = re.compile(r"^(\d+)[A-Z]?-(\d+)[A-Z]?$")


@dataclass(frozen=True)
class ParsedAddress:
    numbers: tuple[str, ...]
    street: str
    """``BONDI ROAD``"""
    locality: str
    """``BONDI``, or empty."""


@dataclass(frozen=True)
class Geocode:
    points: tuple[Point, ...]
    """Web Mercator, one per matched address point."""
    centre: Point
    matched: tuple[str, ...]
    """The register's own spelling of each address found."""
    query: str

    @property
    def lonlat_points(self) -> tuple[tuple[float, float], ...]:
        from sun_study.site.geo import mercator_to_lonlat

        return tuple(mercator_to_lonlat(x, y) for x, y in self.points)


def _clean(text: str) -> str:
    s = text.upper().replace(",", " ")
    s = re.sub(r"\s+", " ", s).strip()
    s = re.sub(r"\b(NSW|AUSTRALIA)\b", "", s)
    s = re.sub(r"\b\d{4}\b", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    s = re.sub(r"^(UNIT|SHOP|SUITE|LEVEL|APT|APARTMENT)\s+\w+[,/]?\s+", "", s)
    return re.sub(r"^\w+/", "", s)


def _split_numbers(s: str) -> tuple[list[str], str]:
    """Consume the leading house-number tokens.

    A multi-lot parent is registered as a list -- "134, 136, 136A, 138
    SHOWGROUND ROAD" -- so there may be several, mixed with ranges. Never
    swallow the whole string: a street name can begin with a number ("7 Hills
    Road"), so at least one token is left for the street.
    """
    parts = [p for p in s.split(" ") if p]
    numbers: list[str] = []
    index = 0
    while index < len(parts) and _NUMBER.match(parts[index]):
        numbers.append(parts[index])
        index += 1
    if index >= len(parts):
        return numbers[:1], " ".join(parts[1:])
    return numbers, " ".join(parts[index:])


def expand_numbers(numbers: list[str]) -> list[str]:
    """``212-218`` becomes 212, 214, 216, 218 and the range itself."""
    out: list[str] = []
    for token in numbers:
        match = _RANGE.match(token)
        if match:
            a, b = int(match.group(1)), int(match.group(2))
            step = 2 if (b - a) % 2 == 0 else 1
            out.extend(str(n) for n in range(min(a, b), max(a, b) + 1, step))
        out.append(token)
    return list(dict.fromkeys(out))


def _street_and_locality(rest: str) -> tuple[str, str]:
    words = [STREET_TYPES.get(w.lower(), w) for w in rest.split(" ") if w]
    type_index = -1
    for i, word in enumerate(words):
        if word in TYPE_WORDS:
            type_index = i
    if type_index >= 0:
        return " ".join(words[: type_index + 1]), " ".join(words[type_index + 1 :])
    return " ".join(words), ""


def parse_address(text: str) -> ParsedAddress:
    """``5/212 Bondi Rd, Bondi NSW 2026`` -> numbers ``212``, street ``BONDI
    ROAD``, locality ``BONDI``."""
    numbers, rest = _split_numbers(_clean(text))
    if not numbers or not rest:
        raise ValueError(f'Cannot parse address: "{text}" -- use e.g. "212 Bondi Rd, Bondi NSW"')
    street, locality = _street_and_locality(rest)
    return ParsedAddress(tuple(expand_numbers(numbers)), street, locality)


def _covers_part(part: str, want: str) -> bool:
    if part == want:
        return True
    if re.sub(r"[A-Z]$", "", part) == want:
        return True
    match = _RANGE.match(part)
    if not match:
        return False
    n, a, b = int(want), int(match.group(1)), int(match.group(2))
    if n < min(a, b) or n > max(a, b):
        return False
    # Ranges are parity-sided: 211-215 must not cover 212.
    return not (a % 2 == b % 2 and n % 2 != a % 2)


def covers(house_number: str, want: str) -> bool:
    """Whether this register entry's number covers the number asked for.

    The field itself may be a list -- "134, 136, 136A, 138" -- so any one
    part covering the wanted number is a match. A unit ("3/134") never is.
    """
    if "/" in house_number:
        return False
    if not want.isdigit():
        return house_number == want
    return any(_covers_part(part, want) for part in re.split(r"[,\s]+", house_number) if part)


def _sql(s: str) -> str:
    return s.replace("'", "''")


@dataclass(frozen=True)
class _Candidate:
    address: str
    house_number: str
    lon: float
    lat: float


def _candidates(where: str, cap: int = 200) -> list[_Candidate]:
    collection = query_where(ADDRESS_POINT, where, "address,housenumber", cap)
    found: list[_Candidate] = []
    for feature in collection["features"]:
        geometry = feature.get("geometry")
        if not geometry:
            continue
        found.append(
            _Candidate(
                address=str(feature["properties"].get("address") or ""),
                house_number=str(feature["properties"].get("housenumber") or ""),
                lon=float(geometry["coordinates"][0]),
                lat=float(geometry["coordinates"][1]),
            )
        )
    return found


def _anchors(number: str) -> list[str]:
    """A bare ``1%`` matches a third of the register; anchor short numbers."""
    return [f"{number}%"] if len(number) >= 3 else [f"{number} %", f"{number}-%"]


def geocode(text: str) -> Geocode:
    parsed = parse_address(text)
    tail = f"{parsed.street} {parsed.locality}" if parsed.locality else parsed.street
    found: dict[str, _Candidate] = {}

    # Stage A: exact matches, the fast path.
    for number in parsed.numbers:
        for candidate in _candidates(f"address = '{_sql(f'{number} {tail}')}'", 5):
            found[candidate.address] = candidate

    wanted = [n for n in parsed.numbers if "-" not in n]

    def outstanding() -> list[str]:
        return [n for n in wanted if not any(covers(c.house_number, n) for c in found.values())]

    # Stage B: ranged parents and suffixed numbers whose address starts with
    # the wanted number. One query per number, never an OR.
    for number in outstanding()[:8]:
        for anchor in _anchors(number):
            pool = _candidates(
                f"address LIKE '{_sql(anchor)}' AND address LIKE '%{_sql(tail)}'"
                " AND housenumber NOT LIKE '%/%'",
                60,
            )
            for candidate in pool:
                if covers(candidate.house_number, number):
                    found[candidate.address] = candidate

    # Stage B2, unindexed: a number inside a range starts with a different
    # number -- 214 lives in "212-218" -- and only a scan finds it.
    if not found:
        pool = _candidates(f"address LIKE '%{_sql(tail)}' AND housenumber NOT LIKE '%/%'", 800)
        for number in wanted:
            for candidate in pool:
                if covers(candidate.house_number, number):
                    found[candidate.address] = candidate

    # Stage C: drop the locality, but only accept candidates that still name it.
    if not found and parsed.locality:
        matching: list[_Candidate] = []
        for number in wanted[:8]:
            for anchor in _anchors(number):
                pool = _candidates(
                    f"address LIKE '{_sql(anchor)}' AND address LIKE '%{_sql(parsed.street)}%'"
                    " AND housenumber NOT LIKE '%/%'",
                    60,
                )
                matching.extend(c for c in pool if covers(c.house_number, number))
        for candidate in matching:
            if parsed.locality in candidate.address:
                found[candidate.address] = candidate
        if not found and matching:
            near = list(dict.fromkeys(c.address for c in matching))[:5]
            raise ValueError(
                f'Address not found in {parsed.locality}: "{text}". '
                f"Same street number elsewhere in NSW: {'; '.join(near)}."
            )

    if not found:
        hint = ""
        try:
            collection = query_where(
                ADDRESS_POINT,
                f"address LIKE '%{_sql(parsed.street)}%' AND housenumber NOT LIKE '%/%'",
                "address",
                400,
            )
            want = int(re.match(r"\d+", parsed.numbers[0]).group(0))  # type: ignore[union-attr]
            near = sorted(
                {
                    str(f["properties"].get("address") or "")
                    for f in collection["features"]
                    if f["properties"].get("address")
                    and (not parsed.locality or parsed.locality in f["properties"]["address"])
                },
                key=lambda a: (
                    abs(int(re.match(r"\d+", a).group(0)) - want)  # type: ignore[union-attr]
                    if re.match(r"\d+", a)
                    else 10**9
                ),
            )[:4]
            if near:
                hint = f" Nearby on this street: {'; '.join(near)}."
        except Exception:
            pass
        raise ValueError(f'Address not found: "{text}".{hint}')

    points = tuple(lonlat_to_mercator(c.lon, c.lat) for c in found.values())
    centre = (
        sum(p[0] for p in points) / len(points),
        sum(p[1] for p in points) / len(points),
    )
    return Geocode(points=points, centre=centre, matched=tuple(found), query=text)


def suggest(text: str, limit: int = 8) -> list[str]:
    """Type-ahead: official address strings for a partial input."""
    raw = re.sub(r"\s+", " ", text.upper().replace(",", " ")).strip()
    if len(raw) < 4:
        return []
    numbers, rest = _split_numbers(_clean(raw))
    if not numbers or not rest:
        return []
    number = numbers[0]
    first = re.match(r"^(\d+)", number)
    leading = first.group(1) if first else number
    street, locality = _street_and_locality(rest)

    anchors = list(
        dict.fromkeys(_anchors(number) if number == leading else [f"{number}%", *_anchors(leading)])
    )

    def ask(anchor: str) -> list[str]:
        try:
            collection = query_where(
                ADDRESS_POINT,
                f"address LIKE '{_sql(anchor)}' AND address LIKE '%{_sql(street)}%'"
                " AND housenumber NOT LIKE '%/%'",
                "address,housenumber",
                60,
            )
        except Exception:
            return []
        return [
            str(f["properties"]["address"])
            for f in collection["features"]
            if f["properties"].get("address")
            and covers(str(f["properties"].get("housenumber") or ""), leading)
        ]

    with ThreadPoolExecutor(max_workers=max(1, len(anchors))) as pool:
        answers = list(pool.map(ask, anchors))
    found = list(dict.fromkeys(a for answer in answers for a in answer))

    def street_of(address: str) -> str:
        return re.sub(r"^\S+\s+", "", address)

    def score(address: str) -> tuple[int, int, str]:
        return (
            (0 if street_of(address).startswith(street) else 2)
            + (0 if locality and address.endswith(locality) else 1),
            len(address),
            address,
        )

    return sorted(found, key=score)[:limit]
