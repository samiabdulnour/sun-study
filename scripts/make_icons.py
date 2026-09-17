"""Draw the window's icons, once, into PNGs the app ships.

Why a generator and not a folder of hand-drawn files
----------------------------------------------------

The icons are geometry, not artwork: a sheet is the same five points in
fourteen of them, the sun is the same disc at the same place in nine, and a
shadow is the same grey wedge thrown the same way every time. Drawn by hand
that consistency lasts until the third person edits one of them. Written down
as coordinates it cannot drift, and a change to the ink colour is one line
rather than sixty files.

It runs at development time, not in the app. Pillow is a dev dependency and is
excluded from the bundle -- ``Loriini.spec`` says so -- because the app only
ever needs the PNGs, which Tk reads by itself. So this script is run when an
icon changes, and its output is committed.

    uv run python scripts/make_icons.py

The drawing space is 32x32, the same numbers as the design boards, with two
units of clear margin. Everything is supersampled eight times and resampled
down, which is what gives a 16 px icon clean diagonals.

Two rules are enforced here rather than remembered:

* **Sizes below 24 drop their detail.** A shape marked ``detail=True`` is not
  drawn at 16 or 20 -- the folded corner, the sun's rays, the grid on a facade.
  Kept, they turn to mud; the drawing has to survive a tab strip. Its opposite,
  ``coarse=True``, is drawn *only* at the small sizes: a sheet that has lost
  its fold needs its square corner back, so the small icon is a plain
  rectangle rather than one with an unexplained notch in it.
* **Small sizes thicken their ink.** A 1.3 unit edge at 16 px is two thirds of
  a pixel and disappears, so every stroke is multiplied by the size's own
  factor.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from pathlib import Path

from PIL import Image, ImageDraw

# -- the palette, as the design boards set it ------------------------------
#
# Two accents and no more: blue is the interface and anything the person
# chose, ochre is the sun and what the sun reaches. Everything built stays
# grey, so a colour in an icon always means something.

INK = "#3C3C3A"
PAPER = "#FFFFFF"
FOLD = "#E6E5DF"
FACE = "#F4F3EE"
GREY_L = "#DEDDD6"
GREY = "#C6C5BE"
GREY_D = "#9A9992"
GROUND = "#EDEBE4"
BLUE = "#1E63C8"
BLUE_T = "#C3D7F5"
BLUE_D = "#14417F"
SUN = "#D98A1E"
SUN_T = "#F6E2BE"
RED = "#C0392B"
#: The cast shadow: grey, and see-through, and never outlined. A contour on a
#: shadow reads as a second building -- the same mistake the drawings make
#: when the fill is not on pen 0.
SHADOW = (143, 142, 135, 115)

#: The box every icon is drawn in, and how far past it nothing may go.
BOX = 32

#: How much bigger the working canvas is than the biggest output. Eight is
#: enough that a 32 px diagonal has no visible steps and small enough that
#: sixty icons render in under a second.
SUPERSAMPLE = 8

#: Sizes the app asks for, and how much to thicken the ink at each. A tab
#: strip shows 20, a row label 16 or 20, a button 16; 24 and 32 are there for
#: a high-DPI screen, which Tk picks up by itself.
SIZES: dict[int, float] = {16: 1.9, 20: 1.6, 24: 1.3, 32: 1.0}

#: Below this, a shape marked ``detail`` is left out.
DETAIL_FLOOR = 24


# -- the shape vocabulary --------------------------------------------------


@dataclass(frozen=True)
class Poly:
    """A closed outline, filled or not.

    ``width`` is in drawing units, so it scales with the icon rather than
    with the canvas, and it is multiplied again by the size's own factor.
    """

    points: tuple[tuple[float, float], ...]
    fill: str | tuple[int, int, int, int] | None = None
    outline: str | None = INK
    width: float = 1.3
    detail: bool = False
    coarse: bool = False

    def draw(self, pen: ImageDraw.ImageDraw, k: float, ink: float) -> None:
        points = [(x * k, y * k) for x, y in self.points]
        if self.fill is not None:
            pen.polygon(points, fill=self.fill)
        if self.outline is not None:
            # Drawn as a closed line rather than as the polygon's own
            # outline: ``joint="curve"`` is what keeps a mitre from spiking
            # off the corner of a sheet at this line weight.
            pen.line(
                [*points, points[0]],
                fill=self.outline,
                width=max(1, round(self.width * k * ink)),
                joint="curve",
            )


def rect(
    x: float,
    y: float,
    w: float,
    h: float,
    fill: str | tuple[int, int, int, int] | None = None,
    outline: str | None = INK,
    width: float = 1.3,
    detail: bool = False,
    coarse: bool = False,
) -> Poly:
    """A rectangle, given as x, y, width, height rather than four corners.

    A function rather than a subclass: a frozen dataclass with its own
    ``__init__`` works only because of a detail of how ``dataclass`` decides
    not to overwrite one, and a set of icons is not the place to rest on that.
    """
    return Poly(
        ((x, y), (x + w, y), (x + w, y + h), (x, y + h)),
        fill=fill,
        outline=outline,
        width=width,
        detail=detail,
        coarse=coarse,
    )


@dataclass(frozen=True)
class Disc:
    """A filled circle. The sun, a badge, a pin's hole, Run's own button."""

    cx: float
    cy: float
    r: float
    fill: str | tuple[int, int, int, int]
    outline: str | None = None
    width: float = 1.3
    detail: bool = False
    coarse: bool = False

    def draw(self, pen: ImageDraw.ImageDraw, k: float, ink: float) -> None:
        box = [
            ((self.cx - self.r) * k, (self.cy - self.r) * k),
            ((self.cx + self.r) * k, (self.cy + self.r) * k),
        ]
        pen.ellipse(
            box, fill=self.fill, outline=self.outline, width=max(1, round(self.width * k * ink))
        )


@dataclass(frozen=True)
class Line:
    """One or more strokes: a glazing line, a dimension, a red mark."""

    points: tuple[tuple[float, float], ...]
    fill: str = INK
    width: float = 1.3
    detail: bool = False
    coarse: bool = False

    def draw(self, pen: ImageDraw.ImageDraw, k: float, ink: float) -> None:
        pen.line(
            [(x * k, y * k) for x, y in self.points],
            fill=self.fill,
            width=max(1, round(self.width * k * ink)),
            joint="curve",
        )


@dataclass(frozen=True)
class Arc:
    """An open arc. Refresh, and nothing else so far."""

    cx: float
    cy: float
    r: float
    start: float
    end: float
    fill: str = BLUE
    width: float = 1.3
    detail: bool = False
    coarse: bool = False

    def draw(self, pen: ImageDraw.ImageDraw, k: float, ink: float) -> None:
        box = [
            ((self.cx - self.r) * k, (self.cy - self.r) * k),
            ((self.cx + self.r) * k, (self.cy + self.r) * k),
        ]
        pen.arc(
            box, self.start, self.end, fill=self.fill, width=max(1, round(self.width * k * ink))
        )


Shape = Poly | Disc | Line | Arc


@dataclass
class Icon:
    """One icon: what it is called, what it says, and how it is drawn."""

    name: str
    says: str
    shapes: list[Shape] = field(default_factory=list)


def only_big(shapes: list[Shape]) -> list[Shape]:
    """These shapes at 24 and up, and not at all below it."""
    return [replace(shape, detail=True) for shape in shapes]


def only_small(shapes: list[Shape]) -> list[Shape]:
    """These shapes only below 24, in place of a drawing that turns to mud.

    Four icons need this rather than the size ladder alone: a document frame
    with a massing inside it, four apartment zones, a twenty per cent dot
    field and a four-segment scale bar all become one dark smudge at 16 px.
    Each gets a coarser twin that says the same thing with half the parts.
    """
    return [replace(shape, coarse=True) for shape in shapes]


# -- pieces used by more than one icon ------------------------------------
#
# Written as functions because the same sheet appears in fourteen icons and
# the same block in nine. A sheet that is not the same sheet everywhere is the
# fastest way to make a set look homemade.


def sheet(x: float = 7, y: float = 3, w: float = 18, h: float = 26, fold: float = 4) -> list[Shape]:
    """A portrait sheet with the folded corner bottom-right.

    The fold is ``detail``: at 16 px it is one grey pixel that reads as dirt,
    and the sheet is still a sheet without it.
    """
    right, bottom = x + w, y + h
    return [
        Poly(
            (
                (x, y),
                (right, y),
                (right, bottom - fold),
                (right - fold, bottom),
                (x, bottom),
            ),
            fill=PAPER,
            detail=True,
        ),
        Poly(
            ((right - fold, bottom - fold), (right, bottom - fold), (right - fold, bottom)),
            fill=FOLD,
            detail=True,
        ),
        # The square corner the fold was cut from, put back for the sizes that
        # dropped the fold: a notch with no fold in it reads as damage.
        rect(x, y, w, h, fill=PAPER, coarse=True),
    ]


def block(
    cx: float = 16, top: float = 5, half: float = 10.5, rise: float = 5.8, tall: float = 8.4
) -> list[Shape]:
    """A massing block, seen from the same corner in every icon.

    One axis for the whole set: if a block in one icon is not the same block
    in the next, the set stops being a set.
    """
    mid = top + rise * 2
    return [
        Poly(
            ((cx, top), (cx + half, top + rise), (cx, mid), (cx - half, top + rise)),
            fill=FACE,
        ),
        Poly(
            (
                (cx - half, top + rise),
                (cx, mid),
                (cx, mid + tall),
                (cx - half, top + rise + tall),
            ),
            fill=GREY,
        ),
        Poly(
            (
                (cx + half, top + rise),
                (cx, mid),
                (cx, mid + tall),
                (cx + half, top + rise + tall),
            ),
            fill=GREY_L,
        ),
    ]


def ground(
    cy: float = 20.5, half_w: float = 13.5, half_h: float = 7.5, cx: float = 16, fill: str = GROUND
) -> list[Shape]:
    """The ground plane, as a plan seen along the same axis as the blocks."""
    return [
        Poly(
            (
                (cx - half_w, cy),
                (cx, cy - half_h),
                (cx + half_w, cy),
                (cx, cy + half_h),
            ),
            fill=fill,
        )
    ]


def sun(
    cx: float = 25, cy: float = 6.5, r: float = 3.0, rays: int = 8, colour: str = SUN
) -> list[Shape]:
    """The sun, top-right, with its rays marked ``detail``.

    Always the same corner. An icon whose light comes from somewhere else
    reads as a different subject.
    """
    out: list[Shape] = [Disc(cx, cy, r, fill=colour)]
    for i in range(rays):
        angle = math.tau * i / rays
        inner, outer = r + 1.5, r + 3.1
        out.append(
            Line(
                (
                    (cx + math.cos(angle) * inner, cy + math.sin(angle) * inner),
                    (cx + math.cos(angle) * outer, cy + math.sin(angle) * outer),
                ),
                fill=colour,
                width=1.3,
                detail=True,
            )
        )
    return out


def pin(cx: float = 16, cy: float = 8.6, r: float = 4.4) -> list[Shape]:
    """A map pin: the one place an address becomes a point."""
    return [
        Poly(
            (
                (cx - r, cy),
                (cx - r * 0.78, cy - r * 0.78),
                (cx, cy - r),
                (cx + r * 0.78, cy - r * 0.78),
                (cx + r, cy),
                (cx + r * 0.45, cy + r * 0.9),
                (cx, cy + r * 1.95),
                (cx - r * 0.45, cy + r * 0.9),
            ),
            fill=BLUE,
            outline=None,
        ),
        Disc(cx, cy, 1.7, fill=PAPER),
    ]


def footprint(
    cx: float, cy: float, half_w: float = 3.3, half_h: float = 1.9, fill: str = GREY
) -> list[Shape]:
    """A building in plan, on the ground plane's axis."""
    return [
        Poly(
            (
                (cx - half_w, cy),
                (cx, cy - half_h),
                (cx + half_w, cy),
                (cx, cy + half_h),
            ),
            fill=fill,
            width=1.1,
        )
    ]


def plug(live: bool = True) -> list[Shape]:
    """The Archicad link: seated and blue, or drained and struck out."""
    body = BLUE_T if live else FACE
    cap = BLUE if live else FOLD
    edge = INK if live else GREY_D
    out: list[Shape] = [
        Line(((12, 4), (12, 11)), fill=edge, width=1.8),
        Line(((20, 4), (20, 11)), fill=edge, width=1.8),
        rect(8.5, 11, 15, 7.5, fill=body, outline=edge),
        Line(((16, 18.5), (16, 22)), fill=edge, width=1.8),
        rect(11.5, 22, 9, 5.5, fill=cap, outline=edge, width=1.2),
    ]
    if not live:
        out.append(Line(((6, 27), (26, 6)), fill=RED, width=2.6))
    return out


def tick(colour: str = BLUE) -> list[Shape]:
    """The check that means saved."""
    return [Line(((10.5, 16.5), (14.1, 20.1), (22, 12)), fill=colour, width=2.6)]


def cross(colour: str = RED) -> list[Shape]:
    """The strike that means forgotten."""
    return [
        Line(((11.5, 12), (20.5, 21)), fill=colour, width=2.6),
        Line(((20.5, 12), (11.5, 21)), fill=colour, width=2.6),
    ]


def north() -> list[Shape]:
    """A north arrow, in interface blue because it is not a thing built."""
    return [Poly(((19.4, 5.6), (21.7, 11.5), (19.4, 10.0), (17.1, 11.5)), fill=BLUE, outline=None)]


# -- the icons -------------------------------------------------------------
#
# Named for the thing, not for the tab, so a setting that moves between tabs
# keeps its icon. ``says`` is what the drawing is of, and it is there to be
# argued with: an icon nobody can describe in a line is an icon that will not
# survive a tab strip.


def sections() -> list[Icon]:
    """The seven tabs, two tiers. One picture of each tab's output."""
    return [
        Icon(
            "general",
            "a sheet with its title block lit",
            [
                *sheet(),
                Line(((10, 7.5), (22, 7.5)), fill=GREY, width=1.4, detail=True),
                Line(((10, 11), (18, 11)), fill=GREY, width=1.4, detail=True),
                rect(10, 17.5, 12, 5, fill=BLUE_T, outline=BLUE, width=1.2),
            ],
        ),
        Icon(
            "site",
            "an address dropped on a site plan, neighbours either side",
            [
                *ground(),
                *footprint(8.6, 20.2),
                *footprint(23.4, 20.2),
                *pin(),
            ],
        ),
        Icon(
            "solar",
            "sun striking a massing, rays drawn as rays",
            [
                *sun(cx=25, cy=6.5, rays=4),
                Line(((21.6, 10.6), (18.6, 12.3)), fill=SUN, width=1.5),
                Line(((24.6, 12.3), (21.6, 14.0)), fill=SUN, width=1.5),
                *block(cx=14, top=12, half=8.5, rise=4.7, tall=5.6),
            ],
        ),
        Icon(
            "model",
            "the massing alone, gridded on top -- no sun in this tab",
            [
                *block(),
                Line(((10.75, 7.9), (21.25, 13.7)), fill=GREY_D, width=0.9, detail=True),
                Line(((21.25, 7.9), (10.75, 13.7)), fill=GREY_D, width=0.9, detail=True),
            ],
        ),
        Icon(
            "analysis",
            "sun through glazing, landing on a floor",
            [
                *sheet(x=4, w=20),
                rect(7, 8, 14, 13, fill=PAPER, width=1.2),
                Poly(((20, 10), (12, 21), (20, 21)), fill=SUN_T, outline=None),
                Line(((21, 9.5), (21, 21.5)), fill=BLUE, width=2.0),
                rect(7, 8, 14, 13, fill=None, width=1.2),
                Disc(27.5, 5, 2.4, fill=SUN),
            ],
        ),
        Icon(
            "shadow",
            "a footprint throwing grey away from the sun",
            [
                *sheet(x=3, w=20),
                Poly(((9, 17), (17, 17), (12.6, 24.4), (4.6, 24.4)), fill=SHADOW, outline=None),
                rect(9, 9.5, 8, 7.5, fill=PAPER, width=1.2),
                Disc(26.8, 5.4, 2.6, fill=SUN),
            ],
        ),
        Icon(
            "views",
            "a document frame aimed down the ray",
            [
                Disc(6.5, 6.0, 2.9, fill=SUN),
                Line(((9.8, 8.6), (13.4, 11.7)), fill=SUN, width=1.5),
                Poly(((13.9, 12.1), (10.8, 11.5), (11.7, 9.6)), fill=SUN, outline=None),
                rect(11, 12.5, 18.5, 15, fill=PAPER, outline=BLUE, width=1.5),
                *only_big(block(cx=20, top=15.5, half=6, rise=3.3, tall=4)),
                # Small, the massing inside the frame is a smudge. One grey
                # mark keeps the reading -- something is being looked at.
                *only_small([rect(16, 17, 8.5, 6.5, fill=GREY, width=1.1)]),
            ],
        ),
    ]


def studies() -> list[Icon]:
    """The six things Run actually runs. Shadow and views reuse their tab's."""
    return [
        Icon(
            "facade",
            "every skin cell banded by hours",
            [
                rect(5, 8, 17, 18, fill=PAPER),
                rect(5.7, 8.7, 15.6, 5.3, fill=SUN, outline=None),
                rect(5.7, 14.3, 15.6, 5.3, fill=SUN_T, outline=None),
                rect(5.7, 19.9, 15.6, 5.4, fill=BLUE_T, outline=None),
                Line(((10.9, 8.7), (10.9, 25.3)), fill=PAPER, width=0.7, detail=True),
                Line(((16.1, 8.7), (16.1, 25.3)), fill=PAPER, width=0.7, detail=True),
                rect(5, 8, 17, 18, fill=None),
                *sun(cx=26.6, cy=5.4, r=2.6, rays=4),
            ],
        ),
        Icon(
            "apartments",
            "one dwelling lit through its living-room glass",
            [
                *sheet(x=3, w=20),
                *only_big(
                    [
                        rect(6, 7, 7, 6.5, fill=PAPER, width=1.1),
                        rect(13, 7, 7, 6.5, fill=PAPER, width=1.1),
                        rect(6, 13.5, 7, 6.5, fill=PAPER, width=1.1),
                        rect(13, 13.5, 7, 6.5, fill=BLUE_T, outline=BLUE, width=1.4),
                        Poly(((19.4, 14.4), (13.8, 19.6), (19.4, 19.6)), fill=SUN_T, outline=None),
                        Line(((20, 13.7), (20, 20.3)), fill=SUN, width=1.6),
                    ]
                ),
                # Four zones at 16 px is a grid of grey. Two says the same
                # thing: some dwellings, and the one being counted.
                *only_small(
                    [
                        rect(6, 8, 7, 12, fill=PAPER, width=1.1),
                        rect(13, 8, 8, 12, fill=BLUE_T, outline=BLUE, width=1.4),
                    ]
                ),
                Disc(26.8, 5.4, 2.6, fill=SUN),
            ],
        ),
        Icon(
            "communal",
            "sample dots over the ground, and the patch that clears",
            [
                *ground(cy=19.5, half_w=14, half_h=8),
                Poly(((9.5, 19.5), (16, 15.8), (22.5, 19.5), (16, 23.2)), fill=SUN_T, outline=None),
                Disc(16, 14.6, 0.85, fill=INK, detail=True),
                Disc(11.2, 17.4, 0.85, fill=INK),
                Disc(20.8, 17.4, 0.85, fill=INK),
                Disc(16, 19.5, 0.85, fill=INK),
                Disc(6.4, 20.2, 0.85, fill=INK, detail=True),
                Disc(25.6, 20.2, 0.85, fill=INK, detail=True),
                Disc(11.2, 22.3, 0.85, fill=INK),
                Disc(20.8, 22.3, 0.85, fill=INK),
                Disc(16, 24.9, 0.85, fill=INK, detail=True),
                Disc(26.8, 5.4, 2.6, fill=SUN),
            ],
        ),
        Icon(
            "site_analysis",
            "a lot boundary and a north arrow -- the one study with no sun",
            [
                *sheet(x=4, w=20),
                Poly(
                    ((7, 15), (13.5, 11.6), (21, 15.6), (19.4, 24), (8.2, 24)),
                    fill=GROUND,
                    width=1.2,
                ),
                Line(((7.5, 18.6), (19.9, 19.8)), fill=GREY, width=1.0, detail=True),
                *north(),
            ],
        ),
    ]


def furniture() -> list[Icon]:
    """The two drawings that are not pictures of anything.

    A tick box is a tick box; there is no thing in a project it depicts, so
    rule one does not apply to it. They exist because Tk 9 draws clam's
    selected checkbutton indicator as a cross, and a cross means no. An
    interface that says "no" where it means "on" is worse than an ugly one,
    so the indicator is replaced by these.
    """
    return [
        Icon(
            "tick_off",
            "an empty box",
            [rect(5, 5, 22, 22, fill=PAPER, outline="#9A9992", width=1.7)],
        ),
        Icon(
            "tick_on",
            "a filled box with the tick knocked out of it",
            [
                rect(5, 5, 22, 22, fill=BLUE, outline=BLUE_D, width=1.7),
                Line(((10.5, 16.5), (14.5, 21), (22, 11)), fill=PAPER, width=3.0),
            ],
        ),
    ]


def controls() -> list[Icon]:
    """The run bar, and the project picker's own state.

    Run and Stop are verbs, so they are plain shapes: nothing is being
    depicted, and dressing them up as drawings would say they were.
    """
    return [
        Icon(
            "run",
            "a plain play button -- a verb, not a thing",
            [
                Disc(16, 16, 12, fill=BLUE),
                Poly(((12.8, 9.8), (22.2, 16.0), (12.8, 22.2)), fill=PAPER, outline=None),
            ],
        ),
        Icon(
            "stop",
            "a plain red square, the set's only other verb",
            [rect(7, 7, 18, 18, fill=RED, outline=None)],
        ),
        Icon("save_default", "the settings file, ticked", [*sheet(), *tick()]),
        Icon("forget", "the same file, struck out", [*sheet(), *cross()]),
        Icon("connected", "the plug seated, and blue", plug(live=True)),
        Icon("disconnected", "the same plug drained, with the red mark", plug(live=False)),
        Icon(
            "refresh",
            "look again -- a circular arrow",
            [
                Arc(16, 16, 10, 300, 210, fill=BLUE, width=2.4),
                Poly(((22.4, 3.6), (22.8, 9.2), (17.6, 7.4)), fill=BLUE, outline=None),
            ],
        ),
    ]


def settings() -> list[Icon]:
    """Icons for the fields themselves, the ones set once and then resented.

    Every one of these is looked up by the field's own label, in
    ``sun_study.app.icons``, so adding one is an entry there and an entry
    here and nothing else.
    """
    return [
        Icon(
            "title_block_width",
            "the title block strip, dimensioned",
            [
                *sheet(),
                rect(10, 17.5, 12, 5, fill=BLUE_T, outline=BLUE, width=1.2),
                Line(((10, 14), (22, 14)), fill=BLUE, width=1.2),
                Line(((10, 12.4), (10, 15.6)), fill=BLUE, width=1.4),
                Line(((22, 12.4), (22, 15.6)), fill=BLUE, width=1.4),
            ],
        ),
        Icon(
            "sheets",
            "four drawings dealt onto one layout",
            [
                *sheet(),
                rect(9.5, 6, 5.6, 5.6, fill=BLUE_T, outline=BLUE, width=1.0),
                rect(16.6, 6, 5.6, 5.6, fill=BLUE_T, outline=BLUE, width=1.0),
                rect(9.5, 13.2, 5.6, 5.6, fill=BLUE_T, outline=BLUE, width=1.0),
                rect(16.6, 13.2, 5.6, 5.6, fill=BLUE_T, outline=BLUE, width=1.0),
            ],
        ),
        Icon(
            "sheets_two",
            "two layouts, half the drawings each",
            [
                Poly(((2, 4), (14, 4), (14, 23), (11, 26), (2, 26)), fill=PAPER, detail=True),
                Poly(((11, 23), (14, 23), (11, 26)), fill=FOLD, detail=True),
                rect(2, 4, 12, 22, fill=PAPER, coarse=True),
                rect(4, 7, 6, 5.4, fill=BLUE_T, outline=BLUE, width=1.0),
                rect(4, 14, 6, 5.4, fill=BLUE_T, outline=BLUE, width=1.0),
                Poly(((18, 4), (30, 4), (30, 23), (27, 26), (18, 26)), fill=PAPER, detail=True),
                Poly(((27, 23), (30, 23), (27, 26)), fill=FOLD, detail=True),
                rect(18, 4, 12, 22, fill=PAPER, coarse=True),
                rect(20, 7, 6, 5.4, fill=BLUE_T, outline=BLUE, width=1.0),
                rect(20, 14, 6, 5.4, fill=BLUE_T, outline=BLUE, width=1.0),
            ],
        ),
        Icon(
            "sheets_each",
            "a layout per drawing -- a stack of them",
            [
                rect(3, 7, 15, 19, fill=PAPER),
                rect(7, 5, 15, 19, fill=PAPER),
                Poly(((11, 3), (26, 3), (26, 19), (22, 23), (11, 23)), fill=PAPER, detail=True),
                Poly(((22, 19), (26, 19), (22, 23)), fill=FOLD, detail=True),
                rect(11, 3, 15, 20, fill=PAPER, coarse=True),
                rect(14, 7, 9, 8, fill=BLUE_T, outline=BLUE, width=1.1),
            ],
        ),
        Icon(
            "origin",
            "the site at nought, nought -- a crosshair, not a place",
            [
                Line(((16, 3), (16, 29)), fill=GREY_D, width=1.0),
                Line(((3, 16), (29, 16)), fill=GREY_D, width=1.0),
                Poly(
                    ((16, 11), (22, 14.4), (16, 17.8), (10, 14.4)),
                    fill=BLUE_T,
                    outline=BLUE,
                    width=1.4,
                ),
                Disc(16, 14.4, 1.5, fill=BLUE, detail=True),
            ],
        ),
        Icon(
            "prefix",
            "a name tag on a layer plane",
            [
                Poly(((15, 13), (25, 18.5), (15, 24), (5, 18.5)), fill=PAPER),
                Poly(
                    ((2.5, 3.5), (12, 3.5), (16.5, 7.5), (12, 11.5), (2.5, 11.5)),
                    fill=BLUE,
                    outline=BLUE_D,
                    width=1.0,
                ),
                Disc(5.6, 7.5, 1.2, fill=PAPER),
            ],
        ),
        Icon(
            "group",
            "a folder with the neighbours already in it",
            [
                Poly(((3, 7), (12, 7), (14.2, 9.8), (29, 9.8), (29, 26), (3, 26)), fill="#E9E8E1"),
                rect(3, 12, 26, 14, fill="#FBFAF7"),
                *footprint(11, 20.4, half_w=3.2, half_h=1.8),
                *footprint(21, 20.4, half_w=3.2, half_h=1.8),
            ],
        ),
        Icon(
            "wait",
            "a clock: minutes a big job is allowed",
            [
                Disc(16, 17, 9.6, fill=PAPER, outline=INK, width=1.4),
                Line(((16, 9.4), (16, 11.2)), fill=GREY_D, width=1.3, detail=True),
                Line(((23.6, 17), (21.8, 17)), fill=GREY_D, width=1.3, detail=True),
                Line(((16, 24.6), (16, 22.8)), fill=GREY_D, width=1.3, detail=True),
                Line(((8.4, 17), (10.2, 17)), fill=GREY_D, width=1.3, detail=True),
                Line(((16, 17), (16, 12)), fill=BLUE, width=1.8),
                Line(((16, 17), (20.4, 19.7)), fill=BLUE, width=1.8),
            ],
        ),
        Icon(
            "storey_datum",
            "a stack of slabs, the datum picked out and pointed at",
            [
                rect(8, 21.2, 18, 3.4, fill=GREY, width=1.2),
                rect(8, 14.8, 18, 3.4, fill=BLUE_T, outline=BLUE, width=1.5),
                rect(8, 8.4, 18, 3.4, fill=GREY, width=1.2),
                Poly(((2.5, 16.5), (5.9, 14.5), (5.9, 18.5)), fill=BLUE, outline=None),
            ],
        ),
        Icon(
            "storey_draw",
            "the same stack, with the fills on the chosen slab",
            [
                rect(8, 8.4, 18, 3.4, fill=GREY, width=1.2),
                rect(8, 14.8, 18, 3.4, fill=GREY, width=1.2),
                rect(8, 21.2, 18, 3.4, fill=BLUE_T, outline=BLUE, width=1.5),
                Disc(11, 22.9, 0.8, fill=BLUE, detail=True),
                Disc(15, 22.9, 0.8, fill=BLUE, detail=True),
                Disc(19, 22.9, 0.8, fill=BLUE, detail=True),
                Disc(23, 22.9, 0.8, fill=BLUE, detail=True),
                Poly(((2.5, 22.9), (5.9, 20.9), (5.9, 24.9)), fill=BLUE, outline=None),
            ],
        ),
        Icon(
            "year",
            "a calendar with the sun inside it",
            [
                rect(5, 7, 22, 20, fill=PAPER),
                rect(5, 7, 22, 5.2, fill=GREY),
                Line(((10.5, 4.2), (10.5, 8.6)), fill=INK, width=1.7, detail=True),
                Line(((21.5, 4.2), (21.5, 8.6)), fill=INK, width=1.7, detail=True),
                Disc(16, 19.6, 4.0, fill=SUN),
            ],
        ),
        Icon(
            "custom_day",
            "one day picked out of the month",
            [
                rect(5, 7, 22, 20, fill=PAPER),
                rect(5, 7, 22, 5.2, fill=GREY),
                Disc(10.5, 16.4, 1.3, fill=GREY, detail=True),
                Disc(16, 16.4, 1.3, fill=GREY, detail=True),
                Disc(21.5, 16.4, 1.3, fill=GREY, detail=True),
                Disc(10.5, 21.8, 1.3, fill=GREY, detail=True),
                Disc(21.5, 21.8, 1.3, fill=GREY, detail=True),
                Disc(16, 21.8, 2.6, fill=BLUE),
            ],
        ),
        Icon(
            "combination",
            "layers bracketed into one named set",
            [
                Poly(((19, 6), (28, 11), (19, 16), (10, 11)), fill=PAPER, width=1.25),
                Poly(((19, 13), (28, 18), (19, 23), (10, 18)), fill="#FBFAF7", width=1.25),
                Poly(((19, 20), (28, 25), (19, 30), (10, 25)), fill=FACE, width=1.25),
                Line(((6.6, 6.5), (4.4, 6.5), (4.4, 25.5), (6.6, 25.5)), fill=BLUE, width=1.7),
            ],
        ),
        Icon(
            "also_export",
            "a layer plane, and a plus",
            [
                Poly(((16, 14), (27, 20), (16, 26), (5, 20)), fill=PAPER),
                Line(((8, 4.5), (8, 12.5)), fill=BLUE, width=2.4),
                Line(((4, 8.5), (12, 8.5)), fill=BLUE, width=2.4),
            ],
        ),
        Icon(
            "neighbours",
            "grey buildings casting onto the blue site",
            [
                *ground(),
                Poly(
                    ((11.9, 22.4), (16.3, 24.8), (14.6, 25.8), (10.2, 23.4)),
                    fill=SHADOW,
                    outline=None,
                    detail=True,
                ),
                Poly(
                    ((20.1, 22.4), (15.7, 24.8), (17.4, 25.8), (21.8, 23.4)),
                    fill=SHADOW,
                    outline=None,
                    detail=True,
                ),
                *footprint(16, 17.4, half_w=5, half_h=2.8, fill=BLUE_T),
                *footprint(8.5, 22.4, half_w=3.3, half_h=1.9),
                *footprint(23.5, 22.4, half_w=3.3, half_h=1.9),
            ],
        ),
        Icon(
            "keep_off",
            "a layer struck out, the printer's own red mark",
            [
                Poly(((16, 11), (27, 17), (16, 23), (5, 17)), fill=FACE, outline=GREY_D),
                Line(((5.5, 26.5), (26.5, 5.5)), fill=RED, width=2.6),
            ],
        ),
        Icon(
            "facade_layers",
            "the skin, tagged, cells still empty",
            [
                rect(9, 10, 17, 17, fill=PAPER),
                Line(((14.7, 10), (14.7, 27)), fill=GREY, width=1.0, detail=True),
                Line(((20.3, 10), (20.3, 27)), fill=GREY, width=1.0, detail=True),
                Line(((9, 15.7), (26, 15.7)), fill=GREY, width=1.0, detail=True),
                Line(((9, 21.3), (26, 21.3)), fill=GREY, width=1.0, detail=True),
                Poly(
                    ((1.5, 3), (9.5, 3), (13.5, 6.5), (9.5, 10), (1.5, 10)),
                    fill=BLUE,
                    outline=BLUE_D,
                    width=1.0,
                ),
            ],
        ),
        Icon(
            "skin_cell",
            "one cell picked out and dimensioned",
            [
                rect(7, 6, 18, 18, fill=PAPER),
                Line(((13, 6), (13, 24)), fill=GREY, width=1.0, detail=True),
                Line(((19, 6), (19, 24)), fill=GREY, width=1.0, detail=True),
                Line(((7, 12), (25, 12)), fill=GREY, width=1.0, detail=True),
                Line(((7, 18), (25, 18)), fill=GREY, width=1.0, detail=True),
                rect(13, 12, 6, 6, fill=BLUE_T, outline=BLUE, width=1.4),
                Line(((13, 28), (19, 28)), fill=BLUE, width=1.2, detail=True),
                Line(((13, 26.4), (13, 29.6)), fill=BLUE, width=1.4, detail=True),
                Line(((19, 26.4), (19, 29.6)), fill=BLUE, width=1.4, detail=True),
            ],
        ),
        Icon(
            "zones",
            "zones on a layer, and which of them are the dwellings",
            [
                rect(4, 7, 11, 9, fill=PAPER, width=1.2),
                rect(15, 7, 11, 9, fill=PAPER, width=1.2),
                rect(4, 16, 11, 9, fill=PAPER, width=1.2),
                rect(15, 16, 11, 9, fill=BLUE_T, outline=BLUE, width=1.5),
            ],
        ),
        Icon(
            "balcony",
            "an open space hung off the dwelling, in sun-colour",
            [
                rect(4, 8, 15, 16, fill=PAPER),
                rect(19, 11, 8, 10, fill=SUN_T, outline=SUN, width=1.4),
                Line(((19, 11), (19, 21)), fill=BLUE, width=1.7),
            ],
        ),
        Icon(
            "glazing",
            "an opening in a wall, in plan, the glass line blue",
            [
                rect(3, 13, 26, 6, fill=GREY),
                rect(10, 13, 12, 6, fill=PAPER, width=1.2),
                Line(((10, 16), (22, 16)), fill=BLUE, width=1.8),
            ],
        ),
        Icon(
            "plan_times",
            "three hands for three times",
            [
                Disc(16, 16, 11, fill=PAPER, outline=INK, width=1.4),
                Line(((16, 16), (8.8, 11.2)), fill=BLUE, width=1.6),
                Line(((16, 16), (16, 6.8)), fill=BLUE, width=1.6),
                Line(((16, 16), (23.2, 11.2)), fill=BLUE, width=1.6),
                Disc(16, 16, 1.5, fill=BLUE, detail=True),
            ],
        ),
        Icon(
            "subset",
            "a folder of sheets -- the subset they are filed under",
            [
                Poly(
                    ((2.5, 8), (11.5, 8), (13.7, 10.8), (31, 10.8), (31, 27), (2.5, 27)),
                    fill="#E9E8E1",
                ),
                Poly(
                    ((9, 5), (20, 5), (20, 14), (17.5, 16.5), (9, 16.5)),
                    fill=PAPER,
                    width=1.2,
                    detail=True,
                ),
                rect(2.5, 13, 28.5, 14, fill="#FBFAF7"),
            ],
        ),
        Icon(
            "communal_window",
            "the stretch of the day that counts, bracketed out of it",
            [
                rect(3, 12, 26, 7, fill=PAPER),
                rect(9.5, 12, 13, 7, fill=SUN_T, outline=None),
                Line(((9.5, 12), (9.5, 19)), fill=SUN, width=1.7),
                Line(((22.5, 12), (22.5, 19)), fill=SUN, width=1.7),
                Line(((9.5, 23), (22.5, 23)), fill=SUN, width=1.3, detail=True),
                Line(((9.5, 21.6), (9.5, 24.4)), fill=SUN, width=1.5, detail=True),
                Line(((22.5, 21.6), (22.5, 24.4)), fill=SUN, width=1.5, detail=True),
            ],
        ),
        Icon(
            "threshold",
            "hours reached, against the line they have to clear",
            [
                rect(3, 15, 26, 9, fill=PAPER),
                rect(3, 15, 11, 9, fill=SUN_T, outline=None),
                Line(((14, 13), (14, 26)), fill=RED, width=2.0),
            ],
        ),
        Icon(
            "height",
            "a plane above the ground, dashed because it is not real",
            [
                *ground(cy=23, half_w=11, half_h=6, cx=17),
                Poly(
                    ((6, 14), (17, 8), (28, 14), (17, 20)), fill="#DCE8F8", outline=BLUE, width=1.3
                ),
                Line(((3, 20), (3, 14)), fill=BLUE, width=1.2, detail=True),
                Line(((1.6, 20), (4.4, 20)), fill=BLUE, width=1.4, detail=True),
                Line(((1.6, 14), (4.4, 14)), fill=BLUE, width=1.4, detail=True),
            ],
        ),
        Icon(
            "grid",
            "sample spacing, in blue because you chose it",
            [
                *ground(cy=19.5, half_w=14, half_h=8),
                Disc(16, 14.6, 0.95, fill=BLUE, detail=True),
                Disc(11.2, 17.4, 0.95, fill=BLUE),
                Disc(20.8, 17.4, 0.95, fill=BLUE),
                Disc(16, 19.5, 0.95, fill=BLUE),
                Disc(6.4, 20.2, 0.95, fill=BLUE, detail=True),
                Disc(25.6, 20.2, 0.95, fill=BLUE, detail=True),
                Disc(11.2, 22.3, 0.95, fill=BLUE),
                Disc(20.8, 22.3, 0.95, fill=BLUE),
                Disc(16, 24.9, 0.95, fill=BLUE, detail=True),
            ],
        ),
        Icon(
            "table",
            "a table on a sheet, header row blue -- a file, not a drawing",
            [
                *sheet(x=6, w=18),
                rect(8.5, 7, 13, 3.4, fill=BLUE_T, outline=BLUE, width=1.0),
                Line(((8.5, 14), (21.5, 14)), fill=GREY, width=1.0, detail=True),
                Line(((8.5, 18), (21.5, 18)), fill=GREY, width=1.0, detail=True),
                Line(((13, 10.4), (13, 22)), fill=GREY, width=1.0, detail=True),
                Line(((17.2, 10.4), (17.2, 22)), fill=GREY, width=1.0, detail=True),
            ],
        ),
        Icon(
            "folder",
            "a folder with geometry in it",
            [
                Poly(
                    ((2.5, 8), (11.5, 8), (13.7, 10.8), (31, 10.8), (31, 27), (2.5, 27)),
                    fill="#E9E8E1",
                ),
                rect(2.5, 13, 28.5, 14, fill="#FBFAF7"),
                *block(cx=16, top=14.6, half=6, rise=3.3, tall=3.4),
            ],
        ),
        Icon(
            "always_there",
            "built, solid, not up for discussion",
            block(),
        ),
        Icon(
            "being_tested",
            "the same block, blue and open -- a proposal",
            [
                Poly(
                    ((16, 5), (26.5, 10.8), (16, 16.6), (5.5, 10.8)), fill="#FBFAF7", outline=BLUE
                ),
                Poly(
                    ((5.5, 10.8), (16, 16.6), (16, 25), (5.5, 19.2)), fill="#F3F7FD", outline=BLUE
                ),
                Poly(
                    ((26.5, 10.8), (16, 16.6), (16, 25), (26.5, 19.2)), fill="#F3F7FD", outline=BLUE
                ),
            ],
        ),
        Icon(
            "ground_plane",
            "the plane the shadows land on",
            [
                *ground(cy=18, half_w=13.5, half_h=7.5),
                Line(((6, 27.5), (8.4, 25.1)), fill=GREY_D, width=1.3, detail=True),
                Line(((13, 28.5), (15.4, 26.1)), fill=GREY_D, width=1.3, detail=True),
                Line(((20, 27.5), (22.4, 25.1)), fill=GREY_D, width=1.3, detail=True),
            ],
        ),
        Icon(
            "ground_cut",
            "the red line is the cut; under it is not drawn on",
            [
                *ground(cy=13, half_w=13.5, half_h=7.5),
                Line(((2.5, 23.5), (29.5, 23.5)), fill=RED, width=2.0),
                Line(((4.5, 28.5), (8.5, 28.5)), fill=GREY_D, width=1.5, detail=True),
                Line(((11.5, 28.5), (15.5, 28.5)), fill=GREY_D, width=1.5, detail=True),
                Line(((18.5, 28.5), (22.5, 28.5)), fill=GREY_D, width=1.5, detail=True),
                Line(((25.5, 28.5), (29.5, 28.5)), fill=GREY_D, width=1.5, detail=True),
            ],
        ),
        Icon(
            "fill_favorite",
            "twenty per cent, and no outline at all",
            [
                *only_big(
                    [
                        Disc(
                            7 + 4.4 * col + (2.2 if row % 2 else 0),
                            11 + 4 * row,
                            1.05,
                            fill="#8F8E87",
                        )
                        for row in range(4)
                        for col in range(4)
                    ]
                ),
                # A 1 unit dot is a third of a pixel at 16 and vanishes
                # altogether. Nine fatter dots still read as a percentage fill.
                *only_small(
                    [
                        Disc(
                            9 + 7 * col + (3.5 if row % 2 else 0), 12 + 6 * row, 1.9, fill="#8F8E87"
                        )
                        for row in range(3)
                        for col in range(3)
                    ]
                ),
                Poly(
                    ((23.5, 3.5), (29, 3.5), (29, 12), (26.25, 9.8), (23.5, 12)),
                    fill=BLUE,
                    outline=BLUE_D,
                    width=1.0,
                ),
            ],
        ),
        Icon(
            "pen_set",
            "three weights and a colour -- what a pen set is",
            [
                Line(((5, 11), (27, 11)), fill=INK, width=1.0),
                Line(((5, 17), (27, 17)), fill=INK, width=2.2),
                Line(((5, 23), (27, 23)), fill=INK, width=3.6),
                Disc(26.6, 5.4, 2.8, fill=BLUE),
            ],
        ),
        Icon(
            "override",
            "one face repainted",
            [
                Poly(((16, 5), (26.5, 10.8), (16, 16.6), (5.5, 10.8)), fill=FACE),
                Poly(((5.5, 10.8), (16, 16.6), (16, 25), (5.5, 19.2)), fill=GREY),
                Poly(((26.5, 10.8), (16, 16.6), (16, 25), (26.5, 19.2)), fill=BLUE, outline=BLUE_D),
            ],
        ),
        Icon(
            "layers_from",
            "pointed at one plane; everything under it goes pale",
            [
                Poly(((19, 5), (28.5, 10.2), (19, 15.4), (9.5, 10.2)), fill=PAPER, width=1.25),
                Poly(
                    ((19, 12), (28.5, 17.2), (19, 22.4), (9.5, 17.2)),
                    fill=BLUE_T,
                    outline=BLUE,
                    width=1.4,
                ),
                Poly(
                    ((19, 19), (28.5, 24.2), (19, 29.4), (9.5, 24.2)),
                    fill="#EDECE6",
                    outline=GREY_D,
                    width=1.25,
                ),
                Poly(((2.5, 17.2), (6.1, 15.1), (6.1, 19.3)), fill=BLUE, outline=None),
            ],
        ),
        Icon(
            "scale",
            "a scale bar, dimensioned",
            [
                *only_big(
                    [
                        rect(4, 12, 6, 5.5, fill=INK, outline=None),
                        rect(10, 12, 6, 5.5, fill=PAPER, width=1.1),
                        rect(16, 12, 6, 5.5, fill=INK, outline=None),
                        rect(22, 12, 6, 5.5, fill=PAPER, width=1.1),
                    ]
                ),
                # Four segments at 16 px is a dark bar. Two is still a scale
                # bar, and the alternation is what says so.
                *only_small(
                    [
                        rect(4, 12, 12, 6, fill=INK, outline=None),
                        rect(16, 12, 12, 6, fill=PAPER, width=1.1),
                    ]
                ),
                Line(((4, 22), (28, 22)), fill=BLUE, width=1.2, detail=True),
                Line(((4, 20.4), (4, 23.6)), fill=BLUE, width=1.5, detail=True),
                Line(((28, 20.4), (28, 23.6)), fill=BLUE, width=1.5, detail=True),
            ],
        ),
        Icon(
            "address",
            "a globe with the address pinned on it",
            [
                Disc(16, 16, 10.5, fill=PAPER, outline=INK, width=1.4),
                Line(((5.5, 16), (26.5, 16)), fill=GREY_D, width=1.2, detail=True),
                Disc(21, 11, 2.4, fill=BLUE),
            ],
        ),
        Icon(
            "radius",
            "how far out the context model reaches",
            [
                *ground(),
                Arc(16, 20.5, 9, 0, 360, fill=BLUE, width=1.4),
                Line(((16, 20.5), (25, 20.5)), fill=BLUE, width=1.4),
            ],
        ),
        Icon(
            "setback",
            "the council's setback off the street",
            [
                Line(((4, 26), (28, 26)), fill=INK, width=2.0),
                rect(9, 9, 14, 11, fill=GREY),
                Line(((16, 20), (16, 26)), fill=BLUE, width=1.4, detail=True),
                Line(((14, 21.5), (18, 21.5)), fill=BLUE, width=1.5, detail=True),
                Line(((14, 24), (18, 24)), fill=BLUE, width=1.5, detail=True),
            ],
        ),
        Icon(
            "run_folder",
            "where the run writes what it made",
            [
                Poly(
                    ((2.5, 8), (11.5, 8), (13.7, 10.8), (31, 10.8), (31, 27), (2.5, 27)),
                    fill="#E9E8E1",
                ),
                rect(2.5, 13, 28.5, 14, fill="#FBFAF7"),
                Line(((16, 16), (16, 23)), fill=BLUE, width=2.0),
                Poly(((12.5, 20), (19.5, 20), (16, 24.5)), fill=BLUE, outline=None),
            ],
        ),
    ]


def every_icon() -> list[Icon]:
    """The whole set, in the order the window meets it."""
    return [*sections(), *studies(), *controls(), *settings(), *furniture()]


# -- rendering -------------------------------------------------------------


def render(icon: Icon, size: int) -> Image.Image:
    """One icon at one size.

    Drawn eight times too big and resampled down, which is what makes a
    diagonal at 16 px look drawn rather than stepped.
    """
    ink = SIZES[size]
    keep_detail = size >= DETAIL_FLOOR
    canvas = BOX * SUPERSAMPLE
    image = Image.new("RGBA", (canvas, canvas), (0, 0, 0, 0))
    pen = ImageDraw.Draw(image)
    for shape in icon.shapes:
        if shape.detail and not keep_detail:
            continue
        if shape.coarse and keep_detail:
            continue
        shape.draw(pen, SUPERSAMPLE, ink)
    return image.resize((size, size), Image.Resampling.LANCZOS)


# -- the same geometry, as SVG ---------------------------------------------
#
# The Archicad add-on's palette needs SVG, not PNG: a 'GICN' resource names an
# image in ``RFIX/Images`` and Tapir -- which is the reference this project
# trusts for API questions, and which builds against the same Archicad 26 kit
# -- ships SVG there. Emitted from the same coordinates as the PNGs rather than
# traced from them, so the palette and the window cannot drift apart.


def _paint(colour: str | tuple[int, int, int, int] | None) -> str:
    """One colour as SVG attributes. A transparent grey becomes an opacity."""
    if colour is None:
        return 'fill="none"'
    if isinstance(colour, tuple):
        red, green, blue, alpha = colour
        return f'fill="#{red:02X}{green:02X}{blue:02X}" fill-opacity="{alpha / 255:.2f}"'
    return f'fill="{colour}"'


def _svg_shape(shape: Shape, ink: float) -> str:
    """One shape as one SVG element.

    The PNG side fills a polygon and then strokes a closed line over it to get
    a mitre that does not spike; SVG says the same thing in one path with
    ``stroke-linejoin``, so that is what comes out here.
    """
    width = f'stroke-width="{shape.width * ink:.2f}"'
    if isinstance(shape, Poly):
        points = " ".join(f"{x:.2f},{y:.2f}" for x, y in shape.points)
        stroke = (
            f'stroke="{shape.outline}" {width} stroke-linejoin="round"'
            if shape.outline is not None
            else 'stroke="none"'
        )
        return f'<polygon points="{points}" {_paint(shape.fill)} {stroke}/>'
    if isinstance(shape, Disc):
        stroke = (
            f'stroke="{shape.outline}" {width}' if shape.outline is not None else 'stroke="none"'
        )
        return (
            f'<circle cx="{shape.cx:.2f}" cy="{shape.cy:.2f}" r="{shape.r:.2f}" '
            f"{_paint(shape.fill)} {stroke}/>"
        )
    if isinstance(shape, Line):
        points = " ".join(f"{x:.2f},{y:.2f}" for x, y in shape.points)
        return (
            f'<polyline points="{points}" fill="none" stroke="{shape.fill}" {width} '
            'stroke-linecap="round" stroke-linejoin="round"/>'
        )
    # An Arc. SVG has no centre-and-angles arc, so the two ends are worked out
    # and joined with an elliptical arc command. Both conventions put zero at
    # three o-clock and grow clockwise on screen, so the angles carry over.
    start, end = shape.start, shape.end
    sweep = (end - start) % 360
    if sweep == 0:
        # A full circle cannot be drawn as one arc command -- the ends would
        # coincide and nothing would be painted.
        return (
            f'<circle cx="{shape.cx:.2f}" cy="{shape.cy:.2f}" r="{shape.r:.2f}" '
            f'fill="none" stroke="{shape.fill}" {width}/>'
        )
    x1 = shape.cx + shape.r * math.cos(math.radians(start))
    y1 = shape.cy + shape.r * math.sin(math.radians(start))
    x2 = shape.cx + shape.r * math.cos(math.radians(end))
    y2 = shape.cy + shape.r * math.sin(math.radians(end))
    large = 1 if sweep > 180 else 0
    return (
        f'<path d="M {x1:.2f} {y1:.2f} A {shape.r:.2f} {shape.r:.2f} 0 {large} 1 '
        f'{x2:.2f} {y2:.2f}" fill="none" stroke="{shape.fill}" {width} '
        'stroke-linecap="round"/>'
    )


def render_svg(icon: Icon, size: int) -> str:
    """One icon as an SVG document, sized for the palette.

    The drawing stays in its own 32-unit space and the ``viewBox`` does the
    scaling, so the coordinates here are the coordinates on the design boards
    and in the PNGs.
    """
    ink = SIZES[size]
    keep_detail = size >= DETAIL_FLOOR
    body = "\n  ".join(
        _svg_shape(shape, ink)
        for shape in icon.shapes
        if not (shape.detail and not keep_detail) and not (shape.coarse and keep_detail)
    )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="no"?>\n'
        f'<svg xmlns="http://www.w3.org/2000/svg" version="1.1" '
        f'width="{size}" height="{size}" viewBox="0 0 {BOX} {BOX}">\n'
        f"  <!-- {icon.name}: {icon.says} -->\n"
        f"  {body}\n"
        "</svg>\n"
    )


#: The palette's buttons, in the order they sit in the strip: the mark that
#: just opens the window, then one per study. The value is the base file name a
#: 'GICN' resource names, and ``_<size>x<size>`` is appended the way the kit's
#: resource compiler expects it.
PALETTE_BUTTONS: dict[str, str] = {
    "solar": "LoriiniLogo",
    "facade": "LoriiniFacade",
    "apartments": "LoriiniApartments",
    "communal": "LoriiniCommunal",
    "shadow": "LoriiniShadow",
    "views": "LoriiniViews",
    "site": "LoriiniSite",
}

#: What the palette draws its buttons at. Tapir's are 24, and a palette row is
#: 28 px tall.
PALETTE_SIZE = 24


def contact_sheet(icons: list[Icon]) -> Image.Image:
    """Every icon at every size, on one page, for looking at.

    Not shipped. It exists because the only way to know whether a 16 px icon
    reads is to look at a 16 px icon, and opening sixty files one at a time is
    how a bad one gets through.
    """
    pad, gap, label = 16, 12, 130
    cell = 40
    wide = label + len(SIZES) * (cell + gap) + pad
    tall = pad * 2 + len(icons) * (cell + gap)
    page = Image.new("RGB", (wide, tall), (247, 246, 241))
    pen = ImageDraw.Draw(page)
    for row, icon in enumerate(icons):
        y = pad + row * (cell + gap)
        pen.text((pad, y + cell // 2 - 4), icon.name, fill=(42, 41, 37))
        for column, size in enumerate(sorted(SIZES)):
            x = label + column * (cell + gap)
            pen.rectangle([x, y, x + cell, y + cell], fill=(255, 255, 255), outline=(220, 218, 209))
            tile = render(icon, size)
            page.paste(tile, (x + (cell - size) // 2, y + (cell - size) // 2), tile)
    return page


def main() -> None:
    here = Path(__file__).resolve().parent.parent
    out = here / "assets" / "icons"
    out.mkdir(parents=True, exist_ok=True)

    icons = every_icon()
    names = [icon.name for icon in icons]
    if len(names) != len(set(names)):
        duplicates = sorted({n for n in names if names.count(n) > 1})
        raise SystemExit(f"Two icons share a name: {', '.join(duplicates)}")

    written = 0
    for icon in icons:
        for size in SIZES:
            render(icon, size).save(out / f"{icon.name}-{size}.png")
            written += 1

    # The add-on's palette, as SVG, from the same coordinates.
    by_name = {icon.name: icon for icon in icons}
    images = here / "archicad-addon" / "RFIX" / "Images"
    images.mkdir(parents=True, exist_ok=True)
    for name, base in PALETTE_BUTTONS.items():
        if name not in by_name:
            raise SystemExit(f"The palette wants {name!r} and no icon is called that.")
        svg = render_svg(by_name[name], PALETTE_SIZE)
        (images / f"{base}_{PALETTE_SIZE}x{PALETTE_SIZE}.svg").write_text(svg, encoding="utf-8")

    sheet_path = here / "build" / "icon-contact-sheet.png"
    sheet_path.parent.mkdir(parents=True, exist_ok=True)
    contact_sheet(icons).save(sheet_path)

    print(f"{len(icons)} icons, {written} files -> {out}")
    print(f"{len(PALETTE_BUTTONS)} palette icons -> {images}")
    print(f"contact sheet -> {sheet_path}")


if __name__ == "__main__":
    main()
