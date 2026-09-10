"""Fetch an Archicad API Development Kit and unpack it.

Only ever run by CI. The kit is a 200 MB licensed download from Graphisoft
that has no business in a public repository, and Graphisoft publish it as a
GitHub release asset, so the build fetches the exact version it needs and
throws it away afterwards.

Deliberately plain: the standard library only, no third-party HTTP client, so
it runs on a fresh runner before anything has been installed.
"""

from __future__ import annotations

import shutil
import sys
import urllib.request
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory


def download_and_unzip(url: str, into: Path) -> None:
    """Put the contents of the zip at ``url`` inside ``into``.

    An existing folder is removed first. A half-unpacked kit from an
    interrupted run is the kind of thing that fails much later, in a linker
    error naming a module nobody has heard of.
    """
    if into.exists():
        shutil.rmtree(into)
    into.mkdir(parents=True)

    with TemporaryDirectory() as scratch:
        archive = Path(scratch) / "devkit.zip"
        print(f"Downloading {url}")
        urllib.request.urlretrieve(url, archive)
        print(f"Unpacking into {into}")
        with zipfile.ZipFile(archive) as bundle:
            bundle.extractall(into)

    # The layout is checked here rather than trusted, because everything
    # downstream depends on it and the failure otherwise surfaces as a cmake
    # error about a path that means nothing to a reader.
    marker = into / "Support" / "Inc" / "ACAPinc.h"
    if not marker.exists():
        raise SystemExit(f"{marker} is missing, so this does not look like an API Development Kit.")
    print(f"Ready: {marker}")


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("usage: download_and_unzip.py <url> <target folder>", file=sys.stderr)
        return 2
    download_and_unzip(argv[1], Path(argv[2]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
