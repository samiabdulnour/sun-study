"""The single source of truth for the tool's status disclaimer.

Brief section 9: this is a design-iteration tool, not a certified compliance
instrument. The README, the CLI banner and every exported results file carry
the same words, from here, so the three can never drift apart.

The wording changes only when the reference comparison in ``docs/validation.md``
is complete, and changing it is a deliberate act, not a tidy-up.
"""

from __future__ import annotations

STATUS = "PROTOTYPE -- NOT VALIDATED FOR SUBMISSION"

DISCLAIMER = (
    "This tool is for design-stage iteration only. It has not yet been "
    "validated against a reference implementation on a real project, and its "
    "output must not be used as a compliance figure in a DA submission. A DA "
    "submission rests on a consultant's report. See docs/validation.md."
)
