"""sun-study: direct sunlight hours from an Archicad model.

Layering, enforced by ``tests/unit/test_architecture.py``:

    archicad -> report -> ingest -> rules -> core

``core`` is pure geometry and astronomy: no I/O, no Archicad, no IFC, and
therefore fully testable without an Archicad licence. Everything that talks to
Archicad lives in the ``archicad`` package and nowhere else.
"""

from sun_study.disclaimer import DISCLAIMER, STATUS

__all__ = [
    "AUTHOR",
    "COPYRIGHT",
    "DISCLAIMER",
    "PRODUCT",
    "STATUS",
    "STOP_FILE_VAR",
    "__version__",
]
__version__ = "0.0.1"

#: Who wrote it. Carried into the window, the packaged executable's file
#: properties and every sheet the tool draws, from here, so the three cannot
#: drift apart -- the same reason the disclaimer has a module of its own.
AUTHOR = "Sami Abdulnour"
PRODUCT = "Sun Study"

#: The copyright line, kept beside the author for the same reason: the window,
#: the executable's file properties and every sheet must say the same thing.
#: Clause 2(e) of the licence requires that it not be removed from either the
#: software or its output.
COPYRIGHT = "Copyright (c) 2026 Sami Abdulnour. All rights reserved."

#: Where a run is told to look for its stop request. The window writes the
#: file; the run watches for it and raises, so its ``finally`` blocks put the
#: project's layer state back.
#:
#: A file rather than a signal because the packaged app has no console, and a
#: console control event -- the only thing a Windows process can be *asked* to
#: stop with -- cannot be sent by a process that has none. The alternative is
#: ``TerminateProcess``, which runs nothing and leaves somebody's project
#: showing the export's layers. It lives here because the two halves that need
#: the name must not import each other: the window would pull in the whole
#: command line, numpy and all, to start up.
STOP_FILE_VAR = "SUN_STUDY_STOP_FILE"
