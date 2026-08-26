"""Entry point. The window, unless the parent asked for the command line.

One executable does both. A packaged build has no Python to invoke, so
``runner`` re-launches *this* program with ``SUN_STUDY_RUN_CLI`` set and the
command's arguments after it, and the check below is what makes that land in
the command line rather than opening a second window.
"""

from __future__ import annotations

import os
import sys


def main() -> None:
    # The term this build runs under, checked at the only door the packaged
    # executable has -- both halves below come through here. It is silent
    # while the build is inside its term; see ``sun_study.licence``.
    from sun_study import licence

    if os.environ.get("SUN_STUDY_RUN_CLI") == "1" and getattr(sys, "frozen", False):
        from sun_study.cli import app

        # The child of a window run. ``cli.main`` is not what starts it, so
        # the check is made here instead, and reported the way a child is
        # heard: on stderr, which the window is already streaming.
        try:
            licence.check()
        except licence.LicenceExpiredError as expired:
            print(str(expired), file=sys.stderr)
            raise SystemExit(4) from expired

        app()
        return

    try:
        licence.check()
    except licence.LicenceExpiredError as expired:
        _say_and_stop(str(expired))
        return

    from sun_study.app.window import launch

    launch()


def _say_and_stop(message: str) -> None:
    """Put the message where somebody who double-clicked will see it.

    A gui-script has no console, so a print goes nowhere at all -- the
    executable would appear to do nothing, which is exactly the failure this
    is meant not to be. A message box needs no window behind it: the root is
    made, hidden, used and destroyed.
    """
    import tkinter as tk
    from tkinter import messagebox

    from sun_study import PRODUCT

    root = tk.Tk()
    root.withdraw()
    try:
        messagebox.showerror(PRODUCT, message)
    finally:
        root.destroy()


if __name__ == "__main__":
    main()
