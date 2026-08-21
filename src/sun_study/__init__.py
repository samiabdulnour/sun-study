"""sun-study: direct sunlight hours from an Archicad model.

Layering, enforced by ``tests/unit/test_architecture.py``:

    archicad -> report -> ingest -> rules -> core

``core`` is pure geometry and astronomy: no I/O, no Archicad, no IFC, and
therefore fully testable without an Archicad licence. Everything that talks to
Archicad lives in the ``archicad`` package and nowhere else.
"""

from sun_study.disclaimer import DISCLAIMER, STATUS

__all__ = ["AUTHOR", "DISCLAIMER", "PRODUCT", "STATUS", "__version__"]
__version__ = "0.0.1"

#: Who wrote it. Carried into the window, the packaged executable's file
#: properties and every sheet the tool draws, from here, so the three cannot
#: drift apart -- the same reason the disclaimer has a module of its own.
AUTHOR = "Sami Abdulnour"
PRODUCT = "Sun Study"
