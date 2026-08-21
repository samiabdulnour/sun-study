"""Run the command line from the window, and stream what it says.

In a subprocess, on purpose. The study takes minutes and prints as it goes, so
the window has to stay answerable and show progress rather than freeze on a
call. A thread would do that too; a process buys three things a thread does
not. It can be stopped -- a colleague who picked the wrong project should not
have to kill the window to get out of a six-minute run. Nothing the study does
to interpreter state can reach the window. And the window runs the *same*
command anybody else would type, so there is one tested path and no second
implementation of what a run means.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
from collections.abc import Callable, Sequence
from pathlib import Path

#: Set on the child so a frozen build knows to be the command line rather than
#: open a second window. A packaged app is one executable: ``sys.executable``
#: is the app itself, not a Python that can be asked for ``-m sun_study.cli``.
CLI_MARKER = "SUN_STUDY_RUN_CLI"

#: Windows only, and only when frozen: keep a console from flashing up behind
#: the window for every run. 0 elsewhere, where the flag does not exist.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def command_prefix() -> list[str]:
    """How to invoke the command line, packaged or not."""
    if getattr(sys, "frozen", False):
        return [sys.executable]
    return [sys.executable, "-u", "-m", "sun_study.cli"]


def child_environment() -> dict[str, str]:
    """The child's environment: unbuffered, uncoloured, and self-aware.

    Unbuffered because a progress line held in a pipe buffer for four minutes
    is worse than no progress line. Uncoloured because the window renders text,
    not ANSI escapes, and Typer colours its warnings by default.
    """
    env = dict(os.environ)
    env[CLI_MARKER] = "1"
    env["PYTHONUNBUFFERED"] = "1"
    env["NO_COLOR"] = "1"
    env["TERM"] = "dumb"
    # Rich decides its width from the terminal it cannot see, and wraps at 80
    # with a box drawn round it. Give it something wide and plain instead.
    env["COLUMNS"] = "160"
    return env


class Run:
    """One study, running. Stoppable, and readable while it runs."""

    def __init__(
        self,
        args: Sequence[str],
        *,
        on_line: Callable[[str], None],
        on_done: Callable[[int], None],
        cwd: Path | None = None,
    ) -> None:
        self.args = list(args)
        self._on_line = on_line
        self._on_done = on_done
        self._cwd = cwd
        self._process: subprocess.Popen[str] | None = None
        self._thread: threading.Thread | None = None
        self._stopped = False

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        self._thread = threading.Thread(target=self._work, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Ask the run to stop. Terminate, not kill: the study restores the
        project's layer state in a ``finally``, and killing it outright is
        exactly how that gets skipped."""
        self._stopped = True
        process = self._process
        if process is not None and process.poll() is None:
            process.terminate()

    def _work(self) -> None:
        argv = [*command_prefix(), *self.args]
        try:
            self._process = subprocess.Popen(
                argv,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                cwd=str(self._cwd) if self._cwd else None,
                env=child_environment(),
                creationflags=_NO_WINDOW if getattr(sys, "frozen", False) else 0,
            )
        except OSError as error:
            self._on_line(f"could not start the study: {error}")
            self._on_done(-1)
            return

        assert self._process.stdout is not None
        for line in self._process.stdout:
            self._on_line(line.rstrip("\n"))
        code = self._process.wait()
        if self._stopped:
            self._on_line("stopped.")
        self._on_done(code)
