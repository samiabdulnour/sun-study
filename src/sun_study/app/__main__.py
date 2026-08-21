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
    if os.environ.get("SUN_STUDY_RUN_CLI") == "1" and getattr(sys, "frozen", False):
        from sun_study.cli import app

        app()
        return

    from sun_study.app.window import launch

    launch()


if __name__ == "__main__":
    main()
