"""Build the window into one .exe a colleague can double-click.

    uv sync            # once, to install PyInstaller
    uv run python scripts/build_app.py

Leaves ``dist/Loriini.exe``. Copy that anywhere; it needs no Python and no
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

import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from sun_study import AUTHOR, PRODUCT, __version__

ROOT = Path(__file__).resolve().parent.parent
#: What the built file is called. Deliberately not ``PRODUCT``: everything
#: this tool makes inside a project is named "Sun Study" -- the layers, the
#: views, the layouts, the combinations -- so an application called the same
#: thing is one more "Sun Study" in a folder that already has thirty. The
#: program gets its own name; the drawings keep theirs.
NAME = "Loriini"

#: The application icon, in the one format Windows takes for an executable.
#: Built by scripts/make_icon.py from the artwork. A single .ico carrying
#: every size Windows asks for -- 16 through 256 --
#: because it is asked for at all of them: 16 in the title bar, 32 in the
#: task bar, 48 in a folder, 256 in the large-icon view and the Alt-Tab
#: switcher. An .ico with only the big one in it is resampled down to a smear
#: at the size somebody actually sees most often.
ICON = ROOT / "assets" / "loriini.ico"

#: Windows reads this out of the .exe for its Properties > Details tab, and
#: shows it in the UAC prompt and the task manager. Without it the file claims
#: nothing at all: no author, no version, no description -- which is how a
#: 66 MB executable arriving by email looks like something to delete.
#:
#: The shape is PyInstaller's, which is a Python literal rather than a file
#: format, and the four-part version numbers must be integers.
VERSION_INFO = f"""VSVersionInfo(
  ffi=FixedFileInfo(
    filevers=(0, 0, 1, 0), prodvers=(0, 0, 1, 0),
    mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable('040904B0', [
        StringStruct('CompanyName', {AUTHOR!r}),
        StringStruct('FileDescription',
                     'Direct sunlight hours from an Archicad model'),
        StringStruct('FileVersion', {__version__!r}),
        StringStruct('InternalName', {PRODUCT!r}),
        StringStruct('LegalCopyright', 'Created by {AUTHOR}'),
        StringStruct('OriginalFilename', '{NAME}.exe'),
        StringStruct('ProductName', {PRODUCT!r}),
        StringStruct('ProductVersion', {__version__!r}),
      ])
    ]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
"""


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

    if not ICON.is_file():
        # Named rather than shrugged at. PyInstaller falls back to its own
        # icon without comment, and the first anybody knows of a missing file
        # is a 69 MB executable arriving with somebody else's logo on it.
        print(f"No icon at {ICON}.", file=sys.stderr)
        return 2

    version_file = ROOT / "build" / "version_info.txt"
    version_file.parent.mkdir(parents=True, exist_ok=True)
    version_file.write_text(VERSION_INFO, encoding="utf-8")

    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--version-file",
        str(version_file),
        "--noconfirm",
        "--clean",
        "--onefile",
        "--windowed",
        "--name",
        NAME,
        # Two separate jobs, and both are wanted. --icon carves it into the
        # .exe as a Windows resource, which is what Explorer, the task bar and
        # the Alt-Tab switcher read. It does *not* reach Tk: the window would
        # still come up wearing the Tk feather, so the same file is carried
        # inside as data for the window to load at startup.
        "--icon",
        str(ICON),
        "--add-data",
        f"{ICON}{os.pathsep}assets",
        # Everything of ifcopenshell: submodules, data and binaries. Not
        # --collect-data, which was the first guess and is not enough --
        # ifcopenshell imports its schema rules by name at read time, so a
        # static scan never sees ifcopenshell.express.rules, and the packaged
        # app got as far as opening an IFC before dying on a missing
        # ifc2x3.exp. Measured, twice.
        "--collect-all",
        "ifcopenshell",
        # Likewise the tz database. Windows ships no system one, which is why
        # tzdata is a runtime dependency rather than a nicety.
        "--collect-data",
        "tzdata",
        # The rulesets. --collect-submodules gathers code; a YAML file is not
        # code, and nothing imports it, so without this the packaged app
        # builds cleanly and dies on the first run with "No ruleset at ...".
        "--collect-data",
        "sun_study",
        # Pillow builds the icon and nothing imports it at runtime, but it is
        # in the environment, so a dependency scan finds it and bundles 8 MB
        # of image codecs into a program that draws no images.
        "--exclude-module",
        "PIL",
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
