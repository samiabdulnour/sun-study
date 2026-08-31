"""What a colleague set last time, kept for next time.

Half the fields in the window are a property of the *project* and are read
back out of Archicad every time it opens -- the layer that carries the
apartments, the masters, the subsets. The rest are a property of the
*practice*: the layer prefix that files the output inside the office's own
numbering, the living-room suffix, the Archicad wait a big job needs, which
studies this person ever runs. Those are the same on every project and every
day, and retyping them is both tedious and a way to get one of them subtly
wrong.

So they are written, on request, to one small JSON file beside the user's own
settings -- ``%APPDATA%`` then ``Loriini``, ``settings.json`` -- and read back
when the window opens. On request rather than on every change, because a
saved default nobody chose to save is worse than no saved default at all: the
next run would quietly use a setting typed once for one odd project.

Nothing here ever raises. A settings file that has been hand-edited into
nonsense, or sits on a locked profile, or was written by a later version with
fields this one has never heard of, must leave the window opening on its own
defaults -- not refusing to open at all.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path

#: The folder name under %APPDATA%, and the file in it. Named for the program
#: rather than for the product: everything the tool writes *into a project* is
#: "Sun Study", and this is the application's own state.
FOLDER = "Loriini"
FILE = "settings.json"


def folder() -> Path:
    """Where this user's settings live.

    ``%APPDATA%`` because that is where Windows keeps per-user application
    settings and it follows a roaming profile between machines. The home
    directory is the fallback for a developer on anything else, so the module
    can be exercised off Windows.
    """
    roaming = os.environ.get("APPDATA")
    return Path(roaming) / FOLDER if roaming else Path.home() / f".{FOLDER.casefold()}"


def path() -> Path:
    """The settings file itself, whether or not it exists yet."""
    return folder() / FILE


def load() -> dict[str, str | bool]:
    """What was saved, or nothing at all.

    Every failure answers with an empty mapping, which the window reads as
    "no saved settings" and starts from its own defaults. Values that are
    neither a field's text nor a tick are dropped one by one rather than
    condemning the whole file, so a single bad line costs one setting.
    """
    try:
        read = json.loads(path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # OSError is the file that is not there or will not open;
        # ValueError covers both the JSON that will not parse and the
        # ``UnicodeDecodeError`` from a file that is not text at all, which
        # is a ValueError and would otherwise sail through a narrower guard
        # and stop the window opening.
        return {}
    if not isinstance(read, dict):
        return {}
    return {str(name): value for name, value in read.items() if isinstance(value, str | bool)}


def save(values: Mapping[str, str | bool]) -> Path:
    """Write these as the defaults, and say where they went.

    The path is returned so the window can print it: a preference somebody
    cannot find is a preference they cannot delete, and "it remembers the
    wrong thing now" is otherwise unanswerable.
    """
    where = path()
    where.parent.mkdir(parents=True, exist_ok=True)
    where.write_text(json.dumps(dict(values), indent=2, sort_keys=True), encoding="utf-8")
    return where


def forget() -> bool:
    """Delete the saved settings. True if the file is now gone.

    A locked profile answers False rather than raising, like everything else
    here: by the time this is called the page has already been put back to its
    defaults, and a file that will not delete is worth reporting but not worth
    stopping for. Whether there was anything to delete is a separate question,
    asked of ``path`` by the caller that wants to say so.
    """
    try:
        path().unlink(missing_ok=True)
    except OSError:
        return False
    return True
