"""Generate the fictional sample building used as an IFC ingest fixture.

Run from the repository root:

    uv run python tests/fixtures/make_sample_building.py

The repository is public, so there is no client geometry here and never should
be. This building is invented, and it is deliberately awkward in the ways real
exports are awkward:

* **Units are millimetres**, not metres, because that is what Archicad usually
  exports and a unit-scale bug is invisible until someone reads a shadow length.
* **True north is rotated 30 degrees** from project north, so a fixture run
  cannot accidentally pass with the north handling stubbed out.
* **One window has no ``IfcRelSpaceBoundary``**, so the geometric containment
  fallback is exercised by the fixture rather than only by a synthetic unit
  test.
* A context block stands to the *true* north of the subject building and
  shades its lower storey, so the expected results are not all "full sun".

GUIDs are derived from a counter rather than randomly, so regenerating the
fixture produces the same file and golden results stay stable.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import ifcopenshell
import ifcopenshell.guid
from ifcopenshell.util.geolocation import angle2yaxis, dd2dms

# --- the invented site -----------------------------------------------------
SITE_NAME = "Fictional Sample Site"
LATITUDE = -33.75  # southern hemisphere, so midwinter sun comes from the north
LONGITUDE = 151.25
ELEVATION_M = 20.0
# IfcSite.RefElevation is an IfcLengthMeasure, so it is in *project units* --
# millimetres here, not metres. Geometry from the ifcopenshell iterator is
# already converted to metres, but raw attributes like this one are not, and
# conflating the two scales the site elevation by 1000.
ELEVATION_PROJECT_UNITS = ELEVATION_M * 1000.0
TRUE_NORTH_DEG = 30.0  # rotate project north 30 deg anticlockwise to reach true north

# --- the invented building -------------------------------------------------
# Millimetres throughout, matching the file's declared unit.
STOREY_HEIGHT = 3000.0
STOREY_COUNT = 2
BUILDING_WIDTH = 20000.0
BUILDING_DEPTH = 12000.0
WALL_THICKNESS = 200.0
SLAB_THICKNESS = 250.0

WINDOW_WIDTH = 2400.0
WINDOW_HEIGHT = 1800.0
WINDOW_SILL = 900.0
WINDOW_THICKNESS = 200.0  # fills the wall thickness, so offset samples clear the facade

BALCONY_WIDTH = 3000.0
BALCONY_DEPTH = 2000.0

# The context block sits to the *true* north of the subject building, which is
# not project north; placing it along project +Y would shade nothing correctly.
#
# Its height and distance are chosen so it subtends about 18 degrees from the
# lower storey and 12 from the upper. Peak midwinter altitude here is 32.7
# degrees, so the block bites into the morning and afternoon sun without
# eliminating it -- an earlier version at 24 m and 22 m subtended 47 degrees
# and blocked every northern ray, which made all four apartments identical and
# the fixture useless for catching regressions.
CONTEXT_HEIGHT = 11000.0
CONTEXT_WIDTH = 24000.0
CONTEXT_DEPTH = 12000.0
CONTEXT_DISTANCE = 28000.0

_counter = 0


def guid() -> str:
    """A deterministic IFC GlobalId, so the fixture regenerates identically."""
    global _counter
    _counter += 1
    return ifcopenshell.guid.compress(uuid.UUID(int=_counter).hex)


class Builder:
    def __init__(self) -> None:
        self.file = ifcopenshell.file(schema="IFC4")
        self.context = self._project()

    # -- project scaffolding ------------------------------------------------
    def _project(self) -> Any:
        f = self.file
        millimetre = f.create_entity(
            "IfcSIUnit", UnitType="LENGTHUNIT", Prefix="MILLI", Name="METRE"
        )
        units = f.create_entity(
            "IfcUnitAssignment",
            Units=[
                millimetre,
                f.create_entity("IfcSIUnit", UnitType="AREAUNIT", Name="SQUARE_METRE"),
                f.create_entity("IfcSIUnit", UnitType="VOLUMEUNIT", Name="CUBIC_METRE"),
                f.create_entity("IfcSIUnit", UnitType="PLANEANGLEUNIT", Name="RADIAN"),
            ],
        )

        # TrueNorth is the whole point of the fixture. yaxis2angle reads this
        # back as "rotate project north anticlockwise by this to reach true
        # north"; angle2yaxis is its documented inverse.
        north_x, north_y = angle2yaxis(TRUE_NORTH_DEG)
        context = f.create_entity(
            "IfcGeometricRepresentationContext",
            ContextType="Model",
            CoordinateSpaceDimension=3,
            Precision=1e-5,
            WorldCoordinateSystem=f.create_entity(
                "IfcAxis2Placement3D", Location=self.point(0.0, 0.0, 0.0)
            ),
            TrueNorth=f.create_entity("IfcDirection", DirectionRatios=(north_x, north_y)),
        )
        self.body = f.create_entity(
            "IfcGeometricRepresentationSubContext",
            ContextIdentifier="Body",
            ContextType="Model",
            ParentContext=context,
            TargetView="MODEL_VIEW",
        )

        f.create_entity(
            "IfcProject",
            GlobalId=guid(),
            Name="sun-study sample building",
            RepresentationContexts=[context],
            UnitsInContext=units,
        )
        return context

    # -- primitives ---------------------------------------------------------
    def point(self, *coords: float) -> Any:
        return self.file.create_entity("IfcCartesianPoint", Coordinates=tuple(coords))

    def placement(self, x: float = 0.0, y: float = 0.0, z: float = 0.0) -> Any:
        """An axis-aligned local placement. No rotation anywhere in this
        fixture: the only rotation that matters here is true north, and mixing
        in placement rotations would blur what a failing test is telling us."""
        return self.file.create_entity(
            "IfcLocalPlacement",
            RelativePlacement=self.file.create_entity(
                "IfcAxis2Placement3D", Location=self.point(x, y, z)
            ),
        )

    def box_representation(
        self, width: float, depth: float, height: float, x: float, y: float, z: float
    ) -> Any:
        """An extruded rectangle. IfcRectangleProfileDef is centred on its
        position, so (x, y) is the centre of the footprint and z is its base."""
        f = self.file
        profile = f.create_entity(
            "IfcRectangleProfileDef",
            ProfileType="AREA",
            Position=f.create_entity(
                "IfcAxis2Placement2D", Location=f.create_entity("IfcCartesianPoint", (0.0, 0.0))
            ),
            XDim=width,
            YDim=depth,
        )
        solid = f.create_entity(
            "IfcExtrudedAreaSolid",
            SweptArea=profile,
            Position=f.create_entity("IfcAxis2Placement3D", Location=self.point(x, y, z)),
            ExtrudedDirection=f.create_entity("IfcDirection", DirectionRatios=(0.0, 0.0, 1.0)),
            Depth=height,
        )
        return f.create_entity(
            "IfcProductDefinitionShape",
            Representations=[
                f.create_entity(
                    "IfcShapeRepresentation",
                    ContextOfItems=self.body,
                    RepresentationIdentifier="Body",
                    RepresentationType="SweptSolid",
                    Items=[solid],
                )
            ],
        )

    def element(
        self,
        ifc_class: str,
        name: str,
        width: float,
        depth: float,
        height: float,
        x: float,
        y: float,
        z: float,
        **extra: Any,
    ) -> Any:
        return self.file.create_entity(
            ifc_class,
            GlobalId=guid(),
            Name=name,
            ObjectPlacement=self.placement(),
            Representation=self.box_representation(width, depth, height, x, y, z),
            **extra,
        )

    def relate(self, ifc_class: str, **attrs: Any) -> Any:
        return self.file.create_entity(ifc_class, GlobalId=guid(), **attrs)


def build() -> ifcopenshell.file:
    b = Builder()
    f = b.file

    site = f.create_entity(
        "IfcSite",
        GlobalId=guid(),
        Name=SITE_NAME,
        ObjectPlacement=b.placement(),
        CompositionType="ELEMENT",
        RefLatitude=dd2dms(LATITUDE, use_us=True),
        RefLongitude=dd2dms(LONGITUDE, use_us=True),
        RefElevation=ELEVATION_PROJECT_UNITS,
    )
    building = f.create_entity(
        "IfcBuilding",
        GlobalId=guid(),
        Name="Sample Building",
        ObjectPlacement=b.placement(),
        CompositionType="ELEMENT",
    )
    b.relate("IfcRelAggregates", RelatingObject=f.by_type("IfcProject")[0], RelatedObjects=[site])
    b.relate("IfcRelAggregates", RelatingObject=site, RelatedObjects=[building])

    storeys, spaces, elements_by_storey = [], [], []
    half_depth = BUILDING_DEPTH / 2.0

    for level in range(STOREY_COUNT):
        base = level * STOREY_HEIGHT
        storey = f.create_entity(
            "IfcBuildingStorey",
            GlobalId=guid(),
            Name=f"Level {level:02d}",
            ObjectPlacement=b.placement(),
            CompositionType="ELEMENT",
            Elevation=base,
        )
        storeys.append(storey)
        contained: list[Any] = []

        contained.append(
            b.element(
                "IfcSlab",
                f"Slab L{level:02d}",
                BUILDING_WIDTH,
                BUILDING_DEPTH,
                SLAB_THICKNESS,
                0.0,
                0.0,
                base,
                PredefinedType="FLOOR",
            )
        )
        # North facade, in project coordinates. Windows sit in this wall.
        contained.append(
            b.element(
                "IfcWall",
                f"Facade L{level:02d}",
                BUILDING_WIDTH,
                WALL_THICKNESS,
                STOREY_HEIGHT,
                0.0,
                half_depth,
                base,
            )
        )
        # Rear wall, so the building is a real occluder rather than a plane.
        contained.append(
            b.element(
                "IfcWall",
                f"Rear L{level:02d}",
                BUILDING_WIDTH,
                WALL_THICKNESS,
                STOREY_HEIGHT,
                0.0,
                -half_depth,
                base,
            )
        )

        for side, offset in (("A", -5000.0), ("B", 5000.0)):
            label = f"L{level:02d}-{side}"

            space = f.create_entity(
                "IfcSpace",
                GlobalId=guid(),
                Name=f"Apartment {label}",
                LongName="Living Room",
                ObjectPlacement=b.placement(),
                Representation=b.box_representation(
                    8000.0,
                    BUILDING_DEPTH - 2 * WALL_THICKNESS,
                    STOREY_HEIGHT - SLAB_THICKNESS,
                    offset,
                    0.0,
                    base + SLAB_THICKNESS,
                ),
                CompositionType="ELEMENT",
                PredefinedType="INTERNAL",
            )
            spaces.append((space, storey))

            window = b.element(
                "IfcWindow",
                f"Window {label}",
                WINDOW_WIDTH,
                WINDOW_THICKNESS,
                WINDOW_HEIGHT,
                offset,
                half_depth,
                base + WINDOW_SILL,
                OverallWidth=WINDOW_WIDTH,
                OverallHeight=WINDOW_HEIGHT,
            )
            contained.append(window)

            # At the apartment's own floor level. The balcony one storey up is
            # then the overhang shading this apartment's window, which is the
            # self-shading the brief cares about and gives the two storeys
            # genuinely different results.
            balcony = b.element(
                "IfcSlab",
                f"Balcony {label}",
                BALCONY_WIDTH,
                BALCONY_DEPTH,
                SLAB_THICKNESS,
                offset,
                half_depth + BALCONY_DEPTH / 2.0,
                base,
                PredefinedType="BASESLAB",
            )
            contained.append(balcony)

            # Level 00 apartment A is deliberately left without a space
            # boundary, so the geometric containment fallback is exercised by
            # a real file and not only by a synthetic unit test.
            if not (level == 0 and side == "A"):
                b.relate(
                    "IfcRelSpaceBoundary",
                    Name=f"Boundary {label}",
                    RelatingSpace=space,
                    RelatedBuildingElement=window,
                    PhysicalOrVirtualBoundary="PHYSICAL",
                    InternalOrExternalBoundary="EXTERNAL",
                )

        elements_by_storey.append((storey, contained))

    # A context block to the *true* north, which is 30 degrees off project
    # north. Placing it along project +Y would shade the wrong thing.
    import math

    bearing = math.radians(TRUE_NORTH_DEG)
    context_block = b.element(
        "IfcBuildingElementProxy",
        "Context Block",
        CONTEXT_WIDTH,
        CONTEXT_DEPTH,
        CONTEXT_HEIGHT,
        -CONTEXT_DISTANCE * math.sin(bearing),
        CONTEXT_DISTANCE * math.cos(bearing),
        0.0,
    )

    b.relate("IfcRelAggregates", RelatingObject=building, RelatedObjects=list(storeys))
    for storey, contained in elements_by_storey:
        b.relate(
            "IfcRelContainedInSpatialStructure",
            RelatedElements=contained,
            RelatingStructure=storey,
        )
    b.relate(
        "IfcRelContainedInSpatialStructure",
        RelatedElements=[context_block],
        RelatingStructure=site,
    )
    for space, storey in spaces:
        b.relate("IfcRelAggregates", RelatingObject=storey, RelatedObjects=[space])

    return f


def main() -> None:
    destination = Path(__file__).parent / "sample_building.ifc"
    model = build()

    # IfcOpenShell stamps the current time into the SPF header, which would
    # make every regeneration a diff and quietly falsify the claim above that
    # the fixture is reproducible. Pin it.
    header = model.header.file_name
    header.name = "sample_building.ifc"
    header.time_stamp = "2024-01-01T00:00:00"
    header.author = ["sun-study"]
    header.organization = ["sun-study"]
    header.authorization = "none"

    model.write(str(destination))
    print(f"wrote {destination} ({destination.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()
