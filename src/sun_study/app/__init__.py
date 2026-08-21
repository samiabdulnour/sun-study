"""A window for the sun study, for people who do not use a terminal.

The tool is a command line with forty options, most of which are a property of
the *project* rather than a decision anybody makes twice: which layer carries
the apartment zones, which master the sheets go on, how high the hotlinked
masters are parked. A colleague should not have to know them, and should not
have to be told them either -- the project already knows, so the window reads
them out of the open Archicad and offers them.

What is deliberately *not* here is any of the study. The window collects
settings and runs the same command line anybody else would run, in a
subprocess, and shows what it prints. Every number stays in one tested place,
and the window cannot quietly disagree with the CLI about what a run means.
"""
