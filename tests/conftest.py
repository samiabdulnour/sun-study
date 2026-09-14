"""What every test in this tree needs settled before it runs.

Only one thing so far, and it is here rather than in the CLI tests because it
is not their business: the help text they read is rendered by Rich, and Rich
decides on colour and width from the environment it finds.

CI sets ``FORCE_COLOR`` so pytest's own output stays readable in the run log.
That variable also reaches the CLI under test, and a coloured ``--timeout`` is
not the string ``--timeout`` -- it is an escape sequence with the option name
cut in half, inside a table wrapped to whatever width was guessed. Eleven
tests that pass on a developer's machine therefore failed on the runner and
only on the runner, which is the worst kind of red: it says nothing about the
code and it cannot be reproduced by running the suite.

Asserting on the characters means asking for the characters.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def plain_wide_output(monkeypatch: pytest.MonkeyPatch) -> None:
    """Help text as what it says, not as one terminal's rendering of it."""
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("TERM", "dumb")
    # Wide enough that no option name is truncated into an ellipsis.
    monkeypatch.setenv("COLUMNS", "200")
