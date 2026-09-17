"""Reading the model the add-on writes, without an IFC in the middle.

The add-on is C++ and cannot be run from here, so the format is pinned from
both ends instead: this file writes it the way the add-on is specified to, and
the reader reads it back. That catches the failure worth catching -- the two
sides disagreeing about the layout -- without waiting ten minutes for CI to
build an .apx.

What it cannot catch is the add-on writing something else. That is what the
magic and the version are for, and what the first live run checks.
"""

from __future__ import annotations

import struct
from pathlib import Path

import numpy as np
import pytest

from sun_study.ingest.native import MAGIC, VERSION, NativeModelError, read_native_model


def _text(value: str) -> bytes:
    raw = value.encode("utf-8")
    return struct.pack("<I", len(raw)) + raw


def write_model(
    path: Path,
    elements: list[dict[str, object]],
    *,
    latitude: float = -33.84,
    longitude: float = 151.04,
    north_deg: float = 0.0,
    elevation: float = 0.0,
    version: int = VERSION,
    magic: bytes = MAGIC,
) -> Path:
    """The add-on's writer, in Python, exactly as the format is specified."""
    out = bytearray(magic)
    out += struct.pack(
        "<Idddd I", version, latitude, longitude, north_deg, elevation, len(elements)
    )
    for element in elements:
        out += _text(str(element["guid"]))
        out += _text(str(element["layer"]))
        out += _text(str(element["kind"]))
        out += _text(str(element["name"]))
        out += _text(str(element.get("long_name", "")))
        out += _text(str(element.get("storey", "")))
        triangles = np.asarray(element["triangles"], dtype=np.float32).reshape(-1, 3, 3)
        out += struct.pack("<I", len(triangles))
        out += triangles.astype(np.float32).tobytes()
    path.write_bytes(bytes(out))
    return path


#: One triangle, a metre on a side, lying flat.
FLAT = [[[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [1.0, 1.0, 0.0]]]


def test_a_written_model_reads_back_as_the_same_model_ifc_would_give(tmp_path: Path) -> None:
    """The return type is the whole point: everything downstream consumes an
    IfcModel and must not learn where the geometry came from."""
    path = write_model(
        tmp_path / "model.loriini",
        [
            {
                "guid": "AAA-1",
                "layer": "03 | Site Context.3D",
                "kind": "IfcWall",
                "name": "neighbour",
                "storey": "LEVEL 02",
                "triangles": FLAT,
            },
            {
                "guid": "BBB-2",
                "layer": "01 | Wall.External",
                "kind": "IfcWall",
                "name": "our wall",
                "storey": "GROUND",
                "triangles": [*FLAT, [[0.0, 0.0, 3.0], [1.0, 0.0, 3.0], [1.0, 1.0, 3.0]]],
            },
        ],
        latitude=-33.891599,
        longitude=151.206925,
        north_deg=41.0,
        elevation=37.003,
    )

    model = read_native_model(path)

    assert model.schema == "LORIINI"
    assert model.latitude_deg == pytest.approx(-33.891599)
    assert model.longitude_deg == pytest.approx(151.206925)
    assert model.true_north_bearing_deg == pytest.approx(41.0)
    assert model.site_elevation_m == pytest.approx(37.003)
    assert len(model.elements) == 2

    first, second = model.elements
    assert first.global_id == "AAA-1"
    assert first.layer == "03 | Site Context.3D", "the layer is what selects context from subject"
    assert first.storey == "LEVEL 02"
    assert first.mesh.triangle_count == 1
    assert second.mesh.triangle_count == 2, "several triangles on one element"


def test_the_layer_survives_because_it_is_what_every_selection_is_made_on(
    tmp_path: Path,
) -> None:
    """--subject-layer, --context-layer, --shadow-terrain and --zone-layer all
    match on it. An element arriving without its layer is an element no rule
    can reach, and the study would silently measure nothing."""
    path = write_model(
        tmp_path / "layers.loriini",
        [
            {
                "guid": f"g{index}",
                "layer": layer,
                "kind": "IfcWall",
                "name": "",
                "triangles": FLAT,
            }
            for index, layer in enumerate(("03 | Site.3D", "03 | Site Context.3D", "LORIINI"))
        ],
    )

    model = read_native_model(path)
    assert [e.layer for e in model.elements] == [
        "03 | Site.3D",
        "03 | Site Context.3D",
        "LORIINI",
    ]


def test_a_storey_that_is_blank_reads_as_no_storey_rather_than_as_one_named_nothing(
    tmp_path: Path,
) -> None:
    """`None` is what the IFC reader gives for an element on no storey, and
    `_on_storey` treats the two differently -- a blank would match a filter
    for a storey called "" and nothing else."""
    path = write_model(
        tmp_path / "homeless.loriini",
        [{"guid": "g", "layer": "L", "kind": "IfcWall", "name": "", "triangles": FLAT}],
    )
    assert read_native_model(path).elements[0].storey is None


def test_something_that_is_not_ours_is_refused_by_name(tmp_path: Path) -> None:
    """An IFC passed here, or a truncated download, must fail with a sentence
    rather than as a geometry error somewhere downstream."""
    wrong = tmp_path / "not-ours.ifc"
    wrong.write_bytes(b"ISO-10303-21;\nHEADER;\n")
    with pytest.raises(NativeModelError, match="not a Loriini model file"):
        read_native_model(wrong)


def test_a_version_this_reader_does_not_know_is_refused_rather_than_guessed(
    tmp_path: Path,
) -> None:
    """The worst outcome available is reading one layout as another: it
    produces a model rather than an error, and nothing downstream can tell."""
    path = write_model(
        tmp_path / "future.loriini",
        [{"guid": "g", "layer": "L", "kind": "IfcWall", "name": "", "triangles": FLAT}],
        version=VERSION + 1,
    )
    with pytest.raises(NativeModelError, match="version"):
        read_native_model(path)


def test_an_empty_model_is_read_and_not_mistaken_for_a_broken_file(tmp_path: Path) -> None:
    """A run whose layers matched nothing is a real answer and a common one --
    a layer named wrongly -- and the scene builder already says so far more
    usefully than a reader could."""
    path = write_model(tmp_path / "empty.loriini", [])
    model = read_native_model(path)
    assert model.elements == ()


def test_the_geometry_arrives_where_it_was_written(tmp_path: Path) -> None:
    """Coordinates are float32 on the wire. At a kilometre out that is well
    under a tenth of a millimetre, which is finer than the millimetre the
    shadow tracer works to."""
    far = [[[1000.0, 1000.0, 50.0], [1001.0, 1000.0, 50.0], [1001.0, 1001.0, 50.0]]]
    path = write_model(
        tmp_path / "far.loriini",
        [{"guid": "g", "layer": "L", "kind": "IfcWall", "name": "", "triangles": far}],
    )

    mesh = read_native_model(path).elements[0].mesh
    assert mesh.vertices[0] == pytest.approx([1000.0, 1000.0, 50.0], abs=1e-3)
    assert mesh.vertices[2] == pytest.approx([1001.0, 1001.0, 50.0], abs=1e-3)
    assert mesh.faces.tolist() == [[0, 1, 2]], "consecutive triples, because these are triangles"


def test_the_two_formats_are_told_apart_by_their_bytes_not_their_name(tmp_path: Path) -> None:
    """An extension is a hint somebody can rename; the magic is a fact.

    A path comes from a person -- typed at --ifc-in, or picked in the window --
    and one given the other kind should say what it got rather than fail inside
    a parser several frames down.
    """
    from sun_study.ingest.ifc import read_model

    # Ours, under a name that says otherwise.
    misnamed = write_model(
        tmp_path / "looks-like.ifc",
        [{"guid": "g", "layer": "L", "kind": "IfcWall", "name": "", "triangles": FLAT}],
    )
    model = read_model(misnamed)
    assert model.schema == "LORIINI", "read by its bytes, not by '.ifc'"
    assert len(model.elements) == 1

    # And something that is neither goes to the IFC reader, which owns that
    # failure and says so in its own words.
    neither = tmp_path / "rubbish.loriini"
    neither.write_bytes(b"not a model at all")
    with pytest.raises(Exception, match=r"(?i)ifc|pars|open|read"):
        read_model(neither)


def test_a_zones_own_name_travels_beside_its_element_id(tmp_path: Path) -> None:
    """They are different strings and it is the name a study selects on.

    On Silverwater the communal open space carries the element ID
    "Communal Open Space" and the name "NON RESIDENTIAL", and --zone-name is
    given the latter. Archicad's own IFC export makes the same split -- ID into
    Name, name into LongName -- and `scene._named_one_of` checks both, so the
    file carries both and keeps the mapping.

    Written the wrong way round, the study finds no Zone and reports a level
    with nothing on it rather than a filter that missed.
    """
    path = write_model(
        tmp_path / "zones.loriini",
        [
            {
                "guid": "z1",
                "layer": "07 | Fills.Areas",
                "kind": "IfcSpace",
                "name": "Communal Open Space",
                "long_name": "NON RESIDENTIAL",
                "storey": "LEVEL 02",
                "triangles": FLAT,
            }
        ],
    )

    zone = read_native_model(path).elements[0]
    assert zone.name == "Communal Open Space", "the element ID, as IFC puts in Name"
    assert zone.long_name == "NON RESIDENTIAL", "the zone name, as IFC puts in LongName"

    # And the selection the study actually makes finds it.
    from sun_study.ingest.scene import _named_one_of

    assert _named_one_of(zone, ("NON RESIDENTIAL",))
    assert not _named_one_of(zone, ("RETAIL",))
