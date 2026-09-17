"""How the window looks: the palette, the type, and the ttk styles.

Why this file exists
--------------------

The window used Windows' own ``vista`` theme, which draws its widgets through
the operating system. That is the right default for a tool nobody has designed,
and the wrong one here: the icons brought a palette with them -- blue is the
interface, ochre is the sun, grey is what is built -- and a native theme cannot
be told about any of it. A selected tab stays Windows blue, a Run button stays
Windows grey, and the drawing beside the label is the only thing in the window
that knows what the tool is about.

So the theme is ``clam``, which is the one ttk theme drawn by Tk rather than by
the OS and therefore the only one that can be restyled. Everything below is the
same palette the icons are drawn from and the same measurements the design
boards use.

What it is careful about
------------------------

**Contrast.** Every text colour here clears 4.5:1 on the surface it sits on.
The hint grey is the one that wants watching: the old ``#5a5a5a`` was fine on
white and this keeps that standard rather than drifting paler for looks.

**Failing soft.** A machine without ``clam``, or without Segoe UI, must still
open the window. Every step is guarded and the worst case is the plain ttk
default, which is ugly and entirely usable -- the same rule ``preferences`` and
``icons`` follow.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk

# -- the palette, the same one the icons are drawn from --------------------

#: The window's own surround: the strip at the top, the run bar at the bottom.
#: Warm rather than Windows grey, which is what stops the window reading as a
#: dialog from 2009.
CHROME = "#EFEEE9"

#: Where the settings are. White, because the icons are drawn on white and a
#: sheet of paper on grey is not a sheet of paper.
SURFACE = "#FFFFFF"

#: A tab nobody has selected, and the trough of the progress bar.
MUTED = "#E3E1DA"

INK = "#1F1E1A"
BODY = "#3C3B36"
#: The line under a control. 5.3:1 on white, which is the point of it.
HINT = "#6B6960"
LINE = "#D8D5CB"

BLUE = "#1E63C8"
BLUE_TINT = "#E8F0FC"
BLUE_EDGE = "#9DBDEB"
BLUE_DEEP = "#14417F"

SUN = "#D98A1E"
RED = "#C0392B"

#: The gap everything is spaced by, and the one the old layout already used.
PAD = 8

#: Type. Segoe UI because this is a Windows tool and it is the face the rest of
#: the operating system is set in; the log stays monospaced because it prints
#: layer names and numbers that have to line up.
FACE = "Segoe UI"
MONO = "Consolas"


def _has_face(name: str) -> bool:
    """Whether this machine actually has that typeface."""
    try:
        return name in tkfont.families()
    except tk.TclError:
        return False


def apply(root: tk.Misc) -> ttk.Style:
    """Dress the window. Returns the style, already configured.

    Idempotent, and safe to call more than once: ttk styles are settings on
    the interpreter rather than objects, so the second call overwrites the
    first with the same values.
    """
    style = ttk.Style(root)

    # clam is the themeable one. Without it every `configure` below is
    # accepted and then ignored by the platform engine, which is worse than
    # not trying: the window would be half restyled.
    try:
        style.theme_use("clam")
    except tk.TclError:  # pragma: no cover - a Tk built without clam
        return style

    body = (FACE, 9) if _has_face(FACE) else ("TkDefaultFont", 9)
    strong = (FACE, 9, "bold") if _has_face(FACE) else ("TkDefaultFont", 9, "bold")
    heading = (FACE, 10, "bold") if _has_face(FACE) else ("TkDefaultFont", 10, "bold")

    try:
        root.option_add("*Font", body)
    except tk.TclError:
        pass

    # -- the surfaces -----------------------------------------------------
    #
    # White is the default, not the exception. Nearly everything in this
    # window is a setting, settings sit on the content surface, and there are
    # two hundred labels down there against a dozen in the chrome -- so the
    # default goes to the many and the chrome asks for itself by name.
    style.configure(
        ".",
        background=SURFACE,
        foreground=BODY,
        font=body,
        bordercolor=LINE,
        lightcolor=SURFACE,
        darkcolor=SURFACE,
        focuscolor=BLUE,
    )
    style.configure("TFrame", background=SURFACE)
    style.configure("TLabel", background=SURFACE, foreground=BODY)
    style.configure("TSeparator", background=LINE)

    #: The line under a control, on the content surface.
    style.configure("Hint.TLabel", background=SURFACE, foreground=HINT)
    #: A study's own name, above the fields it needs.
    style.configure("Heading.TLabel", background=SURFACE, foreground=INK, font=heading)
    style.configure("Strong.TLabel", background=SURFACE, foreground=INK, font=strong)

    #: The window's surround: the project strip at the top, the run bar at the
    #: bottom, the summary down the left.
    style.configure("Chrome.TFrame", background=CHROME)
    style.configure("Chrome.TLabel", background=CHROME, foreground=BODY)
    style.configure("ChromeHint.TLabel", background=CHROME, foreground=HINT)
    style.configure("ChromeStrong.TLabel", background=CHROME, foreground=INK, font=strong)
    style.configure(
        "Chip.TLabel",
        background=SURFACE,
        foreground=BODY,
        bordercolor=LINE,
        relief="solid",
        borderwidth=1,
        padding=(6, 2),
    )

    # -- the tab strip ----------------------------------------------------
    style.configure("TNotebook", background=CHROME, borderwidth=0, tabmargins=(PAD, 6, PAD, 0))
    style.configure(
        "TNotebook.Tab",
        background=MUTED,
        foreground=BODY,
        bordercolor=LINE,
        padding=(12, 7),
        font=body,
    )
    # The selected tab is the content's own surface, so the two read as one
    # sheet rather than as a strip above a box.
    style.map(
        "TNotebook.Tab",
        background=[("selected", SURFACE), ("active", "#EDF3FC")],
        foreground=[("selected", INK)],
        font=[("selected", strong)],
    )

    # -- fields -----------------------------------------------------------
    for kind in ("TEntry", "TCombobox", "TSpinbox"):
        style.configure(
            kind,
            fieldbackground=SURFACE,
            background=SURFACE,
            foreground=INK,
            bordercolor=LINE,
            lightcolor=SURFACE,
            darkcolor=SURFACE,
            insertcolor=INK,
            arrowcolor=BODY,
            padding=(5, 3),
        )
        style.map(
            kind,
            bordercolor=[("focus", BLUE), ("hover", BLUE_EDGE)],
            fieldbackground=[("disabled", MUTED), ("readonly", SURFACE)],
            foreground=[("disabled", HINT)],
        )

    # clam calls these `indicatorbackground` and `indicatorforeground`, not
    # `indicatorcolor` -- which every ttk guide says and which this file said
    # first. ttk accepts an option it does not have and silently ignores it,
    # so the wrong name is not an error, it is a tick that stays white with a
    # black mark on it. Checked with `Style.element_options` rather than
    # guessed at a second time.
    #
    # Unticked, the mark is white on white and so invisible; ticked, the box
    # fills blue and the mark is knocked out of it -- which is rule six of the
    # icon set, applied to the one control the set does not draw.
    style.configure(
        "TCheckbutton",
        background=SURFACE,
        foreground=INK,
        indicatorbackground=SURFACE,
        indicatorforeground=SURFACE,
        indicatormargin=(1, 1, 6, 1),
        bordercolor=LINE,
        padding=(0, 3),
    )
    style.map(
        "TCheckbutton",
        indicatorbackground=[("selected", BLUE), ("disabled", MUTED), ("active", BLUE_TINT)],
        indicatorforeground=[("selected", SURFACE)],
        bordercolor=[("selected", BLUE_DEEP), ("active", BLUE_EDGE)],
        foreground=[("disabled", HINT)],
        background=[("active", SURFACE)],
    )

    # -- buttons ----------------------------------------------------------
    style.configure(
        "TButton",
        background="#F4F3EF",
        foreground=INK,
        bordercolor=LINE,
        lightcolor="#F4F3EF",
        darkcolor="#F4F3EF",
        padding=(12, 5),
        font=body,
    )
    style.map(
        "TButton",
        background=[("pressed", MUTED), ("active", "#FBFAF7")],
        bordercolor=[("active", BLUE_EDGE)],
        foreground=[("disabled", HINT)],
    )

    #: Run. The one emphasised control in the window, tinted rather than
    #: filled: a solid blue button next to three grey ones reads as a warning.
    style.configure(
        "Run.TButton",
        background=BLUE_TINT,
        foreground=BLUE_DEEP,
        bordercolor=BLUE,
        lightcolor=BLUE_TINT,
        darkcolor=BLUE_TINT,
        padding=(14, 6),
        font=strong,
    )
    style.map(
        "Run.TButton",
        background=[("pressed", BLUE_EDGE), ("active", "#DCE8FA"), ("disabled", MUTED)],
        foreground=[("disabled", HINT)],
        bordercolor=[("disabled", LINE)],
    )

    #: A tile in a choice row: icon only, no text, and square.
    style.configure(
        "Tile.TButton",
        background=SURFACE,
        bordercolor=LINE,
        lightcolor=SURFACE,
        darkcolor=SURFACE,
        padding=2,
        relief="solid",
    )
    style.map(
        "Tile.TButton",
        background=[("pressed", BLUE_EDGE), ("active", BLUE_TINT)],
        bordercolor=[("active", BLUE_EDGE)],
    )
    #: The chosen tile. Filled blue, and the drawing inside it is the white
    #: one -- the same move the printer driver makes.
    #: The chosen tile. Tinted and edged in blue rather than filled with it:
    #: these tiles keep their drawing, and a white-on-blue sun is not a sun.
    #: The icon-only tiles on the design boards invert; these carry a picture
    #: that has to stay readable, so they take the named-card treatment.
    style.configure(
        "TileOn.TButton",
        background=BLUE_TINT,
        bordercolor=BLUE,
        lightcolor=BLUE_TINT,
        darkcolor=BLUE_TINT,
        padding=2,
        relief="solid",
    )
    style.map(
        "TileOn.TButton",
        background=[("active", "#DCE8FA"), ("pressed", BLUE_EDGE)],
        bordercolor=[("active", BLUE)],
    )

    # -- the rest ---------------------------------------------------------
    _own_tick(style)

    style.configure(
        "Horizontal.TProgressbar",
        background=BLUE,
        troughcolor=SURFACE,
        bordercolor=LINE,
        lightcolor=BLUE,
        darkcolor=BLUE,
        thickness=10,
    )
    style.configure(
        "Vertical.TScrollbar",
        background=MUTED,
        troughcolor=SURFACE,
        bordercolor=SURFACE,
        arrowcolor=BODY,
        lightcolor=MUTED,
        darkcolor=MUTED,
    )
    style.map("Vertical.TScrollbar", background=[("active", "#CFCCC2")])

    return style


def _own_tick(style: ttk.Style) -> None:
    """Replace the checkbutton indicator with a drawn one.

    Tk 9 draws clam's selected indicator as a cross. A cross means no, and the
    control it sits on means yes -- which is not a polish complaint: the study
    ticks are the six most important controls in the window and half the point
    of them is being readable at a glance from the tab strip.

    So the indicator becomes two images from the icon set, which also makes it
    the same blue-and-knocked-out-white as everything else that is chosen.
    Only the element is swapped; clam's own layout around it is kept, so the
    padding, the focus ring and the label are untouched.

    Leaves the theme alone on any failure. An ugly tick is worth a great deal
    less than a window that opens.
    """
    # Imported here rather than at the top: `icons` needs a Tk root to make a
    # PhotoImage, and this is the only thing in the module that needs it.
    from sun_study.app import icons

    off = icons.named("tick_off", 16)
    on = icons.named("tick_on", 16)
    if off is None or on is None:
        return

    element = "Loriini.Checkbutton.indicator"
    try:
        style.element_create(element, "image", off, ("selected", on), sticky="", border=0)
    except tk.TclError:
        # Already created -- `apply` has been called twice on this
        # interpreter, which is allowed and means the element is there.
        pass
    try:
        style.layout(
            "TCheckbutton",
            [
                (
                    "Checkbutton.padding",
                    {
                        "sticky": "nswe",
                        "children": [
                            (element, {"side": "left", "sticky": ""}),
                            (
                                "Checkbutton.focus",
                                {
                                    "side": "left",
                                    "sticky": "w",
                                    "children": [("Checkbutton.label", {"sticky": "nswe"})],
                                },
                            ),
                        ],
                    },
                )
            ],
        )
    except tk.TclError:  # pragma: no cover - a Tk that will not take a layout
        return
    # The drawn box carries its own margin, so clam's is no longer wanted.
    style.configure("TCheckbutton", indicatormargin=(0, 0, 6, 0))


def log_font() -> tuple[str, int]:
    """What the log is set in."""
    return (MONO, 9) if _has_face(MONO) else ("TkFixedFont", 9)
