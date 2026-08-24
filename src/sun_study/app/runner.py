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
import shutil
import subprocess
import sys
import tempfile
import threading
from collections.abc import Callable, Sequence
from pathlib import Path

from sun_study import STOP_FILE_VAR

#: Set on the child so a frozen build knows to be the command line rather than
#: open a second window. A packaged app is one executable: ``sys.executable``
#: is the app itself, not a Python that can be asked for ``-m sun_study.cli``.
CLI_MARKER = "SUN_STUDY_RUN_CLI"

#: Windows only, and only when frozen: keep a console from flashing up behind
#: the window for every run. 0 elsewhere, where the flag does not exist.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

#: How long a stopped run is given to put the layers back before it is killed
#: outright. Generous: what it is finishing is a handful of Archicad calls
#: over HTTP, and the alternative to waiting is the project left as the export
#: left it.
GRACE_SECONDS = 20.0


def command_prefix() -> list[str]:
    """How to invoke the command line, packaged or not."""
    if getattr(sys, "frozen", False):
        return [sys.executable]
    return [sys.executable, "-u", "-m", "sun_study.cli"]


def child_environment(stop_file: Path | None = None) -> dict[str, str]:
    """The child's environment: unbuffered, uncoloured, and self-aware.

    Unbuffered because a progress line held in a pipe buffer for four minutes
    is worse than no progress line. Uncoloured because the window renders text,
    not ANSI escapes, and Typer colours its warnings by default.

    ``stop_file`` is where the run is told to watch for its stop request. See
    ``Run.stop``.
    """
    env = dict(os.environ)
    if stop_file is not None:
        env[STOP_FILE_VAR] = str(stop_file)
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
        #: Written to ask the run to stop, and deliberately not created up
        #: front: the run watches for this file appearing, so one lying about
        #: would stop it before it had begun.
        self._scratch = Path(tempfile.mkdtemp(prefix="sun-study-run-"))
        self._stop_file = self._scratch / "stop"

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        self._thread = threading.Thread(target=self._work, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Ask the run to stop, and give it time to put the project back.

        Asked, not killed. The study holds the project's layer state open and
        restores it in a ``finally``; ``terminate()`` on Windows is
        ``TerminateProcess``, which runs no ``finally`` at all, so the button
        that exists to get somebody out of a wrong run was leaving somebody's
        project showing the export's layers.

        The asking is a file, because this window has no console to send a
        control event from and a control event is the only way to interrupt a
        Windows process politely -- see ``cli.listen_for_stop``. Terminate and
        then kill are still here, in that order, for a run too wedged to
        notice: a study that will not come back is worse than one stopped
        roughly.

        The waiting happens on a thread of its own. This is called from the UI
        thread, and a window frozen for twenty seconds reads as a crash.
        """
        self._stopped = True
        process = self._process
        if process is None or process.poll() is not None:
            return
        try:
            self._stop_file.write_text("stop", encoding="utf-8")
        except OSError:  # pragma: no cover - a scratch directory that vanished
            process.terminate()
            return
        threading.Thread(target=self._insist, args=(process,), daemon=True).start()

    def _insist(self, process: subprocess.Popen[str]) -> None:
        """Escalate on a stopped run that did not stop. Not the first move."""
        try:
            process.wait(timeout=GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=GRACE_SECONDS)
            except subprocess.TimeoutExpired:  # pragma: no cover - not coming back
                process.kill()

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
                env=child_environment(self._stop_file),
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
        shutil.rmtree(self._scratch, ignore_errors=True)
        if self._stopped:
            self._on_line("stopped.")
        self._on_done(code)
