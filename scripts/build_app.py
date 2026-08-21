"""Build the window into one .exe a colleague can double-click.

    uv sync            # once, to install PyInstaller
    uv run python scripts/build_app.py

Leaves ``dist/Sun Study.exe``. Copy that anywhere; it needs no Python and no
install, only an Archicad with the Tapir add-on running on the same machine.

Why one file and no console
---------------------------
``--onefile`` because the thing being handed over is a file, not a folder
somebody has to keep together, and half a copied folder fails in a way nobody
can read. ``--windowed`` because a console flashing up behind every run looks
like an error to a person who does not use one -- the window re-launches
itself for the study and hides that child too (see ``app/runner.py``).

The cost is a slower first start: a one-file build unpacks itself to a temp
directory each time. Measured at a few seconds against a study that takes
minutes, which is the right way round.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NAME = "Sun Study"


def main() -> int:
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print(
            "PyInstaller is not installed. It is in the dev dependency group:\n"
            "    uv sync\n"
            "then run this again.",
            file=sys.stderr,
        )
        return 2

    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onefile",
        "--windowed",
        "--name",
        NAME,
        # ifcopenshell loads its schema data at import time and PyInstaller
        # cannot see a data file nobody imports, so it is named here. Without
        # it the packaged app starts and fails on the first IFC it reads.
        "--collect-data",
        "ifcopenshell",
        # Likewise the tz database. Windows ships no system one, which is why
        # tzdata is a runtime dependency rather than a nicety.
        "--collect-data",
        "tzdata",
        # Typer's runtime lives behind lazy imports that a static scan misses.
        "--collect-submodules",
        "typer",
        "--collect-submodules",
        "sun_study",
        str(ROOT / "src" / "sun_study" / "app" / "__main__.py"),
    ]
    print(" ".join(command))
    finished = subprocess.run(command, cwd=ROOT, check=False)
    if finished.returncode == 0:
        built = ROOT / "dist" / f"{NAME}.exe"
        size = f"{built.stat().st_size / 1e6:.0f} MB" if built.exists() else "?"
        print(f"\nbuilt {built}  ({size})")
    return finished.returncode


if __name__ == "__main__":
    raise SystemExit(main())
