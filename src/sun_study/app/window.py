"""The window itself.

Shaped by one rule: a colleague should have to decide only what is genuinely a
decision. Everything that is a property of the *project* -- which layer carries
the apartments, which masters exist, which Layout Book subsets the practice
files this kind of drawing under -- is read from the open Archicad and offered
as a list, because the project already knows and a person mistyping it is the
commonest way a run measures the wrong thing.

Grouped by what comes out of it
-------------------------------
A tab per *output* -- General, Facade skin, Solar diagrams, and two more that
are not built yet and say so. That is how the work is asked for: a job wants
the facade skin, or it wants the solar diagrams, and the person setting one up
should be able to read the whole of that study and none of the rest.

It replaces a single column of thirty settings with an Advanced panel under
them, which was the wrong cut. "Advanced" is not a property of a setting: the
facade layers are not harder than the year, they are simply the facade
study's, and a section that hides half of its own inputs somewhere else is not
a section. What is genuinely shared -- the year, the title block, the layer
state the export starts from, the numbering the results file themselves under
-- is what General now is, and it is shared rather than advanced.

The cost of tabs is that a section nobody opens is a section nobody knows the
state of, and this window's whole subject is the wrong answer nobody noticed.
So it is paid for twice, in ``Window._sync``: a tab whose study will run says
so on its own label, and a line above Run names every study queued whichever
tab happens to be showing.

Every field says what it is
---------------------------
A line under each control, and a longer one on hover. Not decoration: most of
these settings fail *quietly*. A facade study with the slab layers missing does
not report an error, it reports the least-lit half of the building; an export
without the zone layers runs for minutes and then cannot place the skin. The
hint says what the setting is; the tooltip says what happens when it is wrong,
because that is the part nobody can infer from a label.

Which makes sections no screen can hold either
----------------------------------------------
Sections shorten the page but do not fix it: Solar diagrams alone is two
studies and a dozen questions, each answered with a line of its own, which is
more than a laptop shows -- and a window cannot be dragged taller than the
screen it is on, so anything past the bottom edge is not awkward to reach but
unreachable. So every tab is its own ``Scroller``. Run, the progress bar and
the log sit below the notebook and outside it, so the button stays findable
and the log stays readable while a study runs, whichever section is showing.

What is worth remembering between runs
--------------------------------------
Half of these fields are a property of the *project* and are read out of
Archicad every time. The rest are a property of the *practice* -- the layer
prefix that files the output inside the office's own numbering, the
living-room suffix, the wait a big export needs, which studies this person ever
runs, which section they work in -- and those are saved, on request, by
``preferences``. The project always wins afterwards: the lists are refilled
from the open Archicad, so a saved name it does not have is replaced by one it
does.
"""

from __future__ import annotations

import math
import queue
import sys
import tkinter as tk
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from tkinter import filedialog, scrolledtext, ttk

from sun_study import AUTHOR, PRODUCT, __version__
from sun_study.app import preferences, probe
from sun_study.app.runner import Run
from sun_study.archicad import naming
from sun_study.archicad.connection import DEFAULT_TIMEOUT_SECONDS
from sun_study.cli import DEFAULT_SHADOW_DATES, DEFAULT_SHADOW_HOURS
from sun_study.disclaimer import STATUS

PAD = 8
HINT = "#5a5a5a"

#: Layer name fragments that mark the envelope a facade study measures. A
#: guess, offered rather than applied: the picker is filled with them and the
#: list stays editable, because the next project names its slabs differently.
SKIN_WORDS = ("Wall.External", "Floor.", "Balustrade", "Screens")

#: What "Ignore above" says before a project has been read. A placeholder, and
#: named so the test for "nobody has touched this" cannot drift from the value
#: the field is built with.
DEFAULT_EXCLUDE_ABOVE_M = "100"

#: Headroom over the topmost storey. A roof, a lift overrun and a parapet are
#: all real and all within a storey or two of the top slab; hotlinked masters
#: are parked far higher -- 157 to 281 m on the reference project. 15 m clears
#: the first and is nowhere near the second, and is the same figure
#: ``ingest.scene`` uses to decide when overhead geometry is suspicious.
STOREY_HEADROOM_M = 15.0

#: What each study is called, in the one place all three callers read it
#: from: the line above Run that says what is queued, the ``──`` rule the log
#: prints when that study starts, and the tab that marks itself. A study
#: named one thing before it runs and another while it runs is a study
#: somebody cannot follow.
FACADE_JOB = "facade skin"
PLANS_JOB = "apartment plans and sheets"
COMMUNAL_JOB = "communal open space"
SHADOW_JOB = "shadow diagram"
SUN_EYE_JOB = "sun eye views"
SITE_JOB = "site and context analysis"


class Tooltip:
    """The longer explanation, on hover.

    Hand-rolled because Tk has none, and short enough to be worth it. The
    delay matters: without it, dragging the mouse across the form flashes six
    of these, which reads as a fault rather than as help.
    """

    def __init__(self, widget: tk.Widget, text: str, delay_ms: int = 450) -> None:
        self.widget = widget
        self.text = text
        self.delay_ms = delay_ms
        self._after: str | None = None
        self._window: tk.Toplevel | None = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    @property
    def visible(self) -> bool:
        """Whether the hover text is on screen."""
        return self._window is not None

    def _schedule(self, _event: object = None) -> None:
        self._cancel()
        self._after = self.widget.after(self.delay_ms, self._show)

    def _cancel(self) -> None:
        if self._after is not None:
            self.widget.after_cancel(self._after)
            self._after = None

    def _show(self) -> None:
        if self._window is not None:
            return
        x = self.widget.winfo_rootx() + 14
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 4
        self._window = tk.Toplevel(self.widget)
        self._window.wm_overrideredirect(True)
        self._window.wm_geometry(f"+{x}+{y}")
        tk.Label(
            self._window,
            text=self.text,
            justify="left",
            wraplength=430,
            background="#ffffe0",
            relief="solid",
            borderwidth=1,
            padx=7,
            pady=5,
        ).pack()

    def _hide(self, _event: object = None) -> None:
        self._cancel()
        if self._window is not None:
            self._window.destroy()
            self._window = None


class Scroller(ttk.Frame):
    """A pane whose contents may be taller than the screen.

    The window asks about thirty questions and answers each one with a line of
    its own underneath, which is deliberate -- most of these settings fail
    silently and that line is what stops it -- and which makes a form no
    laptop can show at once. Without somewhere to scroll, the settings below
    the fold are not merely awkward to reach, they are unreachable: Tk clips
    them, and a window cannot be dragged taller than the screen it is on.

    A canvas, because Tk has no scrolling frame. The frame goes inside it as a
    single canvas item, kept as wide as the canvas so the fields still
    stretch, and the item's own size is the scroll region -- so adding a row,
    or opening Advanced, re-measures without anything having to be told.
    """

    def __init__(self, parent: tk.Misc) -> None:
        super().__init__(parent)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        self.canvas = tk.Canvas(self, highlightthickness=0, borderwidth=0)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.bar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.bar.grid(row=0, column=1, sticky="ns")
        self.canvas.configure(yscrollcommand=self.bar.set)
        self.content = ttk.Frame(self.canvas)
        self._item = self.canvas.create_window((0, 0), window=self.content, anchor="nw")
        self.content.bind("<Configure>", self._measured)
        self.canvas.bind("<Configure>", self._widened)

    def _measured(self, _event: object = None) -> None:
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _widened(self, event: tk.Event[tk.Misc]) -> None:
        self.canvas.itemconfigure(self._item, width=event.width)

    @property
    def scrollable(self) -> bool:
        """Whether anything is out of sight."""
        first, last = self.canvas.yview()
        return first > 0.0 or last < 1.0

    def spin(self, delta: int) -> None:
        """One turn of the wheel. Ignored when everything already fits.

        The guard is not politeness: a canvas will happily scroll past its own
        scroll region, so a wheel over a form that fits slides the whole form
        off the top of the window and leaves an empty pane behind.

        A mouse sends 120 to the notch and a precision touchpad sends whatever
        the finger did, which is often less. The direction is taken from the
        sign and the distance from the size, so a small gesture is worth one
        line either way -- dividing first would round a gentle push down to
        nothing while a gentle pull up still moved, and a page that scrolls
        one way is worse than one that does not scroll at all.
        """
        if delta == 0 or not self.scrollable:
            return
        lines = max(1, abs(delta) // 120)
        self.canvas.yview_scroll(-lines if delta > 0 else lines, "units")


def wheel_reaches_the_pointer(root: tk.Misc) -> None:
    """Give the wheel to whatever is under the mouse, and to nothing else.

    Two Tk defaults are wrong for a form this tall, and both are undone here
    rather than in six places.

    On Windows the wheel is delivered to the widget with keyboard *focus*, not
    to the one the pointer is over. Bound the ordinary way, the page would
    scroll only while the right thing happened to be focused, which reads as
    broken. So it is bound once across the application and routed by where the
    pointer actually is.

    And a ttk combobox answers the wheel by changing its own value. On a page
    that scrolls, that is not a nuisance but a fault: a colleague rolls past
    "Sheet master" on the way down and the sheets are built on a different
    title block, with nothing on screen to say so. The class binding goes, and
    a combobox then scrolls the form under it like everything else.
    """
    root.unbind_class("TCombobox", "<MouseWheel>")

    def spin(event: tk.Event[tk.Misc]) -> None:
        try:
            under = root.winfo_containing(event.x_root, event.y_root)
        except KeyError:
            # Tk made that window itself and never told Python about it --
            # a combobox's own dropdown is one, and the layer lists are long
            # enough to be scrolled. The list scrolls on its own class
            # binding; all this has to do is not raise a traceback over it.
            return
        while under is not None:
            if isinstance(under, Scroller):
                under.spin(event.delta)
                return
            under = getattr(under, "master", None)

    root.bind_all("<MouseWheel>", spin)


def pickable(names: Sequence[str]) -> list[str]:
    """Layers worth offering, without the palette's own dividers.

    A practice organises its layer list with separator layers --
    ``06 ------------------------------ ZONES`` -- which are real layers and
    hold nothing. They matter because they sort to the top: a colleague
    searching "zone" is offered the divider first and it is the likeliest
    thing to tick, which measures nothing.

    Detected by the run of dashes rather than by position, so a project that
    does not use them loses nothing.
    """
    return [name for name in names if "-----" not in name and name.strip()]


def _some(entries: Iterable[str], most: int = 5) -> str:
    """A readable list of what a layer holds, and how much was left out.

    One project's zone layer carries thirteen distinct names. Printed in full
    they wrap to three lines under a field and stop being read, which loses
    the first five as well as the last eight -- and the whole point of the
    line is that somebody glances at it. The full list, with sizes, is one
    click away in the chooser.
    """
    listed = list(entries)
    shown = ", ".join(listed[:most])
    return shown if len(listed) <= most else f"{shown} and {len(listed) - most} more"


def _letters_and_digits(text: str) -> str:
    """A name reduced to what somebody actually types when searching for it.

    Layer names carry punctuation nobody reproduces from memory -- ``01 |
    Core``, ``05 | Dims/Notes.DA``, ``Wall.External`` -- and the spacing round
    it is the practice's, not the searcher's. Somebody looking for the core
    types ``1| core``, or ``01|core``, and gets nothing, because neither
    ``1|`` nor ``01|core`` appears anywhere in ``01 | Core`` as written.

    Dropping every non-alphanumeric character from both sides makes all three
    the same question. It only ever widens what matches -- punctuation removed
    from the query cannot make a name stop matching -- so nothing that used to
    be findable becomes unfindable.
    """
    return "".join(character for character in text.casefold() if character.isalnum())


def matching(names: Sequence[str], query: str) -> list[str]:
    """Layers matching a search box, in the order the project lists them.

    Every word has to appear, in any order and anywhere in the name, so
    "floor str" finds ``01 | Floor.Structural`` without anybody having to
    remember whether the group number or the dot comes first.

    Punctuation and case are both ignored, on both sides. Layer naming is
    nobody's memory test, and the group number, the bar and the dot are the
    part of a name a person is least likely to type the way it was written --
    ``1| core``, ``01|core`` and ``core`` all have to find ``01 | Core`` or
    the picker is slower than scrolling.
    """
    words = [_letters_and_digits(word) for word in query.split()]
    wanted = [word for word in words if word]
    if not wanted:
        return list(names)
    return [name for name in names if all(word in _letters_and_digits(name) for word in wanted)]


#: Where the layer chooser was when it was last closed, as a Tk geometry
#: string. Module level because it belongs to none of the fields that open
#: one: a colleague setting a project up opens the chooser six or seven times
#: in a row -- facade layers, then also-export, then the neighbours -- and a
#: dialog that jumps back to the middle of the screen every time is one they
#: have to drag off the form again every time.
#:
#: Not saved to disk. Where a window sat is a fact about this afternoon and
#: this screen, and restoring last week's position onto a laptop that no
#: longer has the second monitor would put the chooser somewhere unreachable.
_chooser_geometry: str | None = None


class LayerChooser(tk.Toplevel):
    """Tick the names, rather than typing them. Layers, or a layer's zones.

    A project has a hundred and fifty layers whose names carry a group number,
    a dot and a space -- ``05 | Dims/Notes.DA`` -- and a study that silently
    measures nothing is what a typo in one buys. So the names come from the
    project and the only input is a tick.

    Modal, and it answers with ``None`` when cancelled rather than with the
    list it started from, so a caller can tell "unchanged" from "emptied".
    """

    def __init__(
        self,
        parent: tk.Misc,
        *,
        title: str,
        hint: str,
        available: Sequence[str],
        chosen: Sequence[str],
        describe: Mapping[str, str] | None = None,
    ) -> None:
        """``describe`` is what a row *reads* as, where the name alone does not
        say enough -- a zone name means little without its size, and ``BY``
        beside ``12 zones, 11 m2`` is a balcony to anybody. The value ticked is
        still the name, so what the caller gets back is what the study takes.
        """
        super().__init__(parent)
        self.title(title)
        self.transient(parent.winfo_toplevel())
        self.minsize(520, 460)
        if _chooser_geometry:
            # Size and position both, so a chooser somebody widened to read
            # long layer names opens wide the next time as well.
            self.geometry(_chooser_geometry)
        self.result: list[str] | None = None

        self._available = list(available)
        self._describe = dict(describe or {})
        #: Ticked, kept as a set across filtering: narrowing the list must not
        #: quietly untick what has scrolled out of sight.
        self._ticked: set[str] = {name for name in chosen if name in set(available)}
        self._boxes: dict[str, tk.BooleanVar] = {}

        outer = ttk.Frame(self, padding=PAD)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(2, weight=1)

        ttk.Label(outer, text=hint, foreground=HINT, wraplength=470).grid(
            row=0, column=0, sticky="w", pady=(0, 6)
        )

        search = ttk.Frame(outer)
        search.grid(row=1, column=0, sticky="ew")
        search.columnconfigure(1, weight=1)
        ttk.Label(search, text="Find").grid(row=0, column=0, padx=(0, 6))
        self.query = ttk.Entry(search)
        self.query.grid(row=0, column=1, sticky="ew")
        self.query.bind("<KeyRelease>", lambda _event: self._repaint())

        # The same pane the main form scrolls in, so the wheel behaves the
        # same way over both. It also replaces a ``bind_all`` this dialog
        # used to leave behind it: the binding outlived the window, and every
        # turn of the wheel afterwards went to a canvas that no longer existed.
        scroller = Scroller(outer)
        scroller.grid(row=2, column=0, columnspan=2, sticky="nsew", pady=6)
        self._list = scroller.content

        self.count = ttk.Label(outer, text="", foreground=HINT)
        self.count.grid(row=3, column=0, sticky="w")

        buttons = ttk.Frame(outer)
        buttons.grid(row=4, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        buttons.columnconfigure(0, weight=1)
        ttk.Button(buttons, text="Clear all", command=self._clear).grid(row=0, column=0, sticky="w")
        ttk.Button(buttons, text="Cancel", command=self._close).grid(row=0, column=1, padx=6)
        ttk.Button(buttons, text="Use these", command=self._accept).grid(row=0, column=2)

        self._repaint()
        self.query.focus_set()
        self.bind("<Escape>", lambda _e: self._close())
        # The window manager's own X, which goes nowhere near the buttons.
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.grab_set()

    def _repaint(self) -> None:
        for child in self._list.winfo_children():
            child.destroy()
        self._boxes.clear()
        for index, name in enumerate(matching(self._available, self.query.get())):
            state = tk.BooleanVar(value=name in self._ticked)
            self._boxes[name] = state
            ttk.Checkbutton(
                self._list,
                text=self._describe.get(name, name),
                variable=state,
                command=lambda n=name: self._toggle(n),  # type: ignore[misc]
            ).grid(row=index, column=0, sticky="w")
        self.count.config(text=f"{len(self._ticked)} of {len(self._available)} chosen")

    def _toggle(self, name: str) -> None:
        if self._boxes[name].get():
            self._ticked.add(name)
        else:
            self._ticked.discard(name)
        self.count.config(text=f"{len(self._ticked)} of {len(self._available)} chosen")

    def _clear(self) -> None:
        self._ticked.clear()
        self._repaint()

    def _close(self) -> None:
        """Remember where this was, then go.

        Every route out comes through here -- Cancel, Escape, the window
        manager's X and Use these -- because a dialog that remembers its place
        only when dismissed one particular way is worse than one that never
        does: it looks broken rather than absent.
        """
        global _chooser_geometry
        try:
            # ``wm_geometry`` and not ``winfo_geometry``: the first is what the
            # window manager holds and tracks through a drag, the second is
            # what the widget has been realised at -- and a Toplevel that was
            # never mapped answers that one with 1x1+0+0, which would remember
            # a corner nobody put it in.
            _chooser_geometry = self.wm_geometry()
        except tk.TclError:  # pragma: no cover - already gone
            pass
        self.destroy()

    def _accept(self) -> None:
        # Back in the project's own order, not tick order: a list a person can
        # scan against the layer palette is worth more than one recording the
        # sequence somebody happened to click in.
        self.result = [name for name in self._available if name in self._ticked]
        self._close()


@dataclass
class Job:
    """One command to run, and what to call it while it runs."""

    label: str
    args: list[str] = field(default_factory=list)


class Window:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("Solar Analysis")
        # Never taller than the screen. A window opened past the bottom edge
        # cannot be dragged back by its title bar, so everything below the
        # fold -- Run included -- is out of reach, which is the fault this
        # replaces. The minimum height only has to keep the log and the
        # buttons: the settings above them scroll.
        self.root.minsize(820, 460)
        self.root.geometry(self._on_the_screen(880, 940))

        #: Lines from the worker thread. Tkinter is not thread-safe, so nothing
        #: touches a widget from the runner's thread: lines go through here and
        #: the UI thread drains them on a timer.
        self.incoming: queue.Queue[tuple[str, object]] = queue.Queue()

        self.options = probe.ProjectOptions()
        self.ports: list[int] = []
        self.run: Run | None = None
        self.queued: list[Job] = []

        self._build()
        #: What the window opens with when nothing has been saved. Taken
        #: before any saved settings land on top of it, so Forget has
        #: something to go back to that is not "whatever was on screen".
        self.factory = self.settings()
        self._restore()
        self.refresh()
        self.root.after(80, self._drain)

    def _on_the_screen(self, want_wide: int, want_tall: int) -> str:
        """A size that fits the display this window opened on.

        The margins are for the task bar and the title bar, neither of which
        Tk reports: ``winfo_screenheight`` is the whole panel, borders and
        all.
        """
        wide = min(want_wide, self.root.winfo_screenwidth() - 40)
        tall = min(want_tall, self.root.winfo_screenheight() - 120)
        return f"{max(wide, 820)}x{max(tall, 460)}"

    # -- layout ------------------------------------------------------------
    #: The sections, in the order a colleague meets them. One tab per
    #: *output*, because that is how this work is asked for -- "the facade
    #: skin and the solar diagrams for Tuesday" -- rather than per kind of
    #: setting. General is first and is not an output: it is what every
    #: output is drawn on, and its settings are the ones a project is set up
    #: with once.
    #:
    #: The last two are not built. They are here because the shape of the
    #: tool is worth showing, and because a setting that arrives later then
    #: has a decided place to land instead of being wedged into whichever
    #: section is nearest. Each says plainly that it does nothing yet, and
    #: neither carries a tick, so there is nothing to switch on and wait for.
    GENERAL = "General"
    FACADE = "Facade skin"
    DIAGRAMS = "Solar diagrams"
    SHADOWS = "Shadow diagram"
    EYE = "Sun eye view"
    SITE = "Site analysis"

    def _build(self) -> None:
        wheel_reaches_the_pointer(self.root)

        # The bottom of the window is built first and packed to the bottom,
        # so it stays put while the settings above it change tab and scroll.
        # Run has to be findable without hunting through sections, and a log
        # that scrolls off the top during a run is a log nobody reads --
        # which is most of what this window has to say while it works.
        base = ttk.Frame(self.root, padding=(PAD, 0, PAD, PAD))
        base.pack(side="bottom", fill="x")
        base.columnconfigure(0, weight=1)

        self._project_picker()
        self._sections()
        self._controls(base)
        self._sync()

    def _project_picker(self) -> None:
        """Which Archicad, above the tabs and belonging to none of them.

        Every list in every section is read out of the chosen project and
        every study runs against it, so it is not a setting of one output. It
        also has to stay visible while a section is being filled in: a
        colleague naming zones for the diagrams should not have to leave the
        page to see which project they are naming them in.
        """
        top = ttk.Frame(self.root, padding=(PAD, PAD, PAD, 0))
        top.pack(side="top", fill="x")
        top.columnconfigure(1, weight=1)
        row = 0

        # Listed, never assumed: each instance gets its own port, so the
        # default is right only for whichever started first, and two projects
        # open is the ordinary case in an office.
        label = ttk.Label(top, text="Archicad")
        label.grid(row=row, column=0, sticky="w")
        picker = ttk.Frame(top)
        picker.grid(row=row, column=1, sticky="ew", pady=2)
        picker.columnconfigure(0, weight=1)
        self.instance = ttk.Combobox(picker, state="readonly", values=[])
        self.instance.grid(row=0, column=0, sticky="ew")
        self.instance.bind("<<ComboboxSelected>>", lambda _event: self.refresh())
        rescan = ttk.Button(picker, text="Refresh", command=self.refresh, width=9)
        rescan.grid(row=0, column=1, padx=(6, 0))
        Tooltip(
            rescan,
            "Looks again for running Archicads and re-reads the chosen "
            "project. Press it after opening a project, or after adding a "
            "layer or a Layout Book subset that should appear in the lists "
            "in the sections below.",
        )
        row += 1
        self._hint(
            top,
            row,
            "The open project to measure. Check the name if you have two open.",
        )
        row += 1
        for target in (label, self.instance):
            Tooltip(
                target,
                "Archicad gives every open instance its own port, so there is no "
                "single right one to guess at. The study reads and draws in "
                "whichever project is chosen here — pick the wrong one and it "
                "measures the wrong building and says nothing. Press Refresh "
                "after opening or closing a project.",
            )

        self.status = ttk.Label(top, text="", foreground=HINT, wraplength=620, justify="left")
        self.status.grid(row=row, column=1, sticky="w", pady=(0, 4))

    def _sections(self) -> None:
        """The tabs, each holding everything one output needs.

        Grouped by output rather than by kind, because that is the question a
        colleague arrives with. Thirty settings in one column made a page
        nobody could hold in their head, and the split that mattered was
        never "simple and advanced" -- it was "the four things the facade
        skin needs" against "the nine things the diagrams need". A person
        running one study can now read the whole of it and none of the rest.

        What tabs cost is that a section nobody opens is a section nobody
        knows the state of. Paid for twice, in ``_sync``: a tab whose study
        will run says so on its own label, and the line above Run names every
        study queued whichever tab is showing.
        """
        self.tabs = ttk.Notebook(self.root)
        self.tabs.pack(side="top", fill="both", expand=True, padx=PAD, pady=(4, 0))

        #: Every tab's title in tab order, and every tab's pane. The
        #: notebook's own labels grow a tick when the study in them will run,
        #: so they are no longer safe to read a title back out of, and the
        #: saved "which section was open" is keyed on this list instead.
        self.titles: list[str] = []
        self.panes: list[Scroller] = []

        self._general(self._section(self.GENERAL))
        self._facade(self._section(self.FACADE))
        self._diagrams(self._section(self.DIAGRAMS))
        self._shadows(self._section(self.SHADOWS))
        self._sun_eyes(self._section(self.EYE))
        self._site(self._section(self.SITE))

    def _section(self, title: str) -> ttk.Frame:
        """One tab, and the frame its settings are built into.

        Each tab scrolls on its own rather than the notebook sitting inside
        one scroller, because the sections are not the same length: Solar
        diagrams asks about two studies and is three times General. A single
        scroller would size itself to the longest and leave every short tab
        with a bar that moves nothing, while a laptop that cannot show the
        longest tab still has to be able to reach the bottom of it.
        """
        pane = Scroller(self.tabs)
        self.tabs.add(pane, text=title)
        self.titles.append(title)
        self.panes.append(pane)
        frame = ttk.Frame(pane.content, padding=PAD)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(1, weight=1)
        return frame

    def _general(self, frame: ttk.Frame) -> None:
        """What every output is drawn on, and what none of them chooses.

        Set once when a project is set up and left alone afterwards, which is
        what used to make most of these "advanced". They are not advanced,
        they are shared: the year, the title block, the layer state the
        export starts from and the numbering the results file themselves
        under are the same for the facade skin and for every diagram, and a
        copy of each in three sections is three chances to disagree.
        """
        row = 0
        self.master, row = self._combo(
            frame,
            row,
            "Sheet master",
            "Title block the layouts are built on.",
            "A no-scale master is the right one: the drawings are shrunk to fit "
            "the page, so they are no longer at any stated scale and a title "
            "block claiming 1:200 would be wrong. An existing layout keeps the "
            "master it was made on — nothing in the add-on can change it — so "
            "delete old study sheets before changing this.",
        )
        self.year, row = self._entry(
            frame,
            row,
            "Year",
            "2024",
            "Which year's midwinter date to assess.",
            "The assessment runs on 21 June, the shortest day, which is the "
            "worst case the ADG asks about. The year only shifts the date and "
            "the sun positions slightly; it is here so a study can be repeated "
            "against the same day as an earlier report.",
        )
        self.exclude, row = self._entry(
            frame,
            row,
            "Ignore above (m)",
            DEFAULT_EXCLUDE_ABOVE_M,
            "Drops anything sitting entirely above this height.",
            "Hotlinked unit-type masters are parked high above the real "
            "building — 157 to 281 m on this project — on the same layers as "
            "the building itself, so height is the only thing that separates "
            "them. Left in, they join the area being measured and quietly "
            "change every percentage. Filled in from the project when it is "
            "read: the topmost storey plus 15 m, which clears a roof and a "
            "lift overrun and is far below anything parked. Type over it and "
            "the typed figure is kept. Clear the box to keep everything.",
        )

        ttk.Separator(frame).grid(row=row, column=0, columnspan=2, sticky="ew", pady=PAD)
        row += 1

        self.combination, row = self._combo(
            frame,
            row,
            "Export combination",
            "The office layer combination the IFC export starts from.",
            "The translator exports what is shown, so the layer state is an "
            "input to every number this tool produces. The run sets it from "
            "this combination, forces on what the study needs over the top, "
            "and puts every layer back afterwards — so the answer does not "
            "depend on what happened to be on screen.",
        )
        self.require, row = self._picker(
            frame,
            row,
            "Also export",
            "Layers forced into the export whatever the combination says.",
            "Shared by the facade skin and the communal study, which is why "
            "it is here rather than in either. The zone layers must be in the "
            "export or neither drawing can be placed on the building — both "
            "need a Zone to line the IFC up against, and neither of this "
            "project's IFC combinations shows them. Left empty, the zone "
            "layers are added automatically.",
        )
        self.context, row = self._picker(
            frame,
            row,
            "Neighbouring buildings",
            "Layers that shade the site without being part of it.",
            "They have to be in the export or the study measures a building "
            "with nothing around it, which can only overstate its sunlight. "
            "Named by layer and not by element name because survey context "
            "usually arrives nameless — on one project all 232 neighbours are "
            "unnamed slabs. Listing them here both forces them into the export "
            "and keeps them out of the facade area, which is about the scheme "
            "and not the neighbourhood.",
        )
        self.hide, row = self._picker(
            frame,
            row,
            "Keep off drawings",
            "Layers switched off on the study drawings and in the export.",
            "Grids and dimension layers usually: they are the practice's own "
            "annotation and clutter a sun study without adding to it. What "
            "counts as clutter is a decision about the drawing, so it is named "
            "here rather than guessed from layer names.",
        )

        ttk.Separator(frame).grid(row=row, column=0, columnspan=2, sticky="ew", pady=PAD)
        row += 1

        self.prefix, row = self._entry(
            frame,
            row,
            "Layer prefix",
            naming.DEFAULT_PREFIX,
            "Leads the name of every layer, view and sheet the study creates.",
            "So the output files itself inside the office's own numbering: on "
            "a project whose layer groups run 00 to 13, '14 |' gives "
            "'14 | Sun Study.Results' and it sorts where a reader expects. It "
            "is also how a rerun finds its own sheets to replace, so changing "
            "it leaves the last run's behind to be deleted by hand, and it "
            "cannot be emptied — an empty prefix matches every layout in the "
            "project.",
        )
        self.wait_min, row = self._entry(
            frame,
            row,
            "Archicad wait (min)",
            "30",
            "How long to let Archicad think about one command before giving up.",
            "Only the IFC export comes anywhere near it, and on a big project "
            "that export is minutes rather than seconds -- 455 MB on one "
            "mixed-use job. Too short and the run stops partway with 'Archicad "
            "did not answer', having already done the slow part, and leaves "
            "the project holding the study's layer state. Raising it costs "
            "nothing on a run that works; it only decides how long a genuinely "
            "stuck Archicad is waited on.",
        )

    def _facade(self, frame: ttk.Frame) -> None:
        """The 3D skin: what gets painted, and how finely."""
        row = 0
        self.do_facade = tk.BooleanVar(value=True)
        self.do_floors = tk.BooleanVar(value=True)

        facade_box = ttk.Checkbutton(
            frame, text="Facade skin in 3D", variable=self.do_facade, command=self._sync
        )
        facade_box.grid(row=row, column=0, columnspan=2, sticky="w")
        Tooltip(
            facade_box,
            "Paints the outside of the building with one colour per band of "
            "direct sun hours on 21 June, as real 3D elements on the tool's own "
            "layer. Switch that layer off to hide the result; nothing else in "
            "the model is touched. This measures surface area, not apartments, "
            "so it answers a massing question rather than an ADG one.",
        )
        row += 1
        self.floors_box = ttk.Checkbutton(
            frame,
            text="including floors, balcony decks and soffits",
            variable=self.do_floors,
        )
        self.floors_box.grid(row=row, column=0, columnspan=2, sticky="w", padx=(18, 0))
        Tooltip(
            self.floors_box,
            "Horizontal surfaces take far more sun than any wall. Leaving them "
            "out is not a smaller study — it is a study of the least-lit half "
            "of the building, and nothing in the result says so. Needs the slab "
            "layers named in Facade layers below.",
        )
        row += 1
        self._caption(
            frame,
            row,
            "Every surface of the building itself, banded by hours of direct sun. "
            "A massing question rather than an ADG one.",
        )
        row += 1

        ttk.Separator(frame).grid(row=row, column=0, columnspan=2, sticky="ew", pady=PAD)
        row += 1

        self.subject, row = self._picker(
            frame,
            row,
            "Facade layers",
            "The layers that are the building being measured. Comma separated.",
            "Everything else in the model still casts shade but is not counted "
            "in the area. Without this, the facade area on a developed model "
            "includes every internal partition and balustrade. The slab layers "
            "belong here too, or there are no floors to colour.",
        )
        self.grid_m, row = self._entry(
            frame,
            row,
            "Skin cell (m)",
            "0.5",
            "Cell size of the 3D facade skin.",
            "Finer looks better and makes many more elements — half the cell "
            "size is roughly four times the count, and this project already "
            "makes over five thousand at 0.5 m. A face narrower than one cell "
            "is not drawn at all, so a coarse setting loses thin columns.",
        )

    def _diagrams(self, frame: ttk.Frame) -> None:
        """The drawn studies: apartments, and communal open space.

        Two studies in one section because they are one deliverable. Both
        band a plan by hours of direct sun, both are read at the same
        drawing, and a job that asks for solar diagrams means both — so where
        the sheets are filed is asked once, at the top, for the pair.
        """
        row = 0
        self.adg_subset, row = self._combo(
            frame,
            row,
            "Diagrams filed in",
            "Layout Book subset both studies' banded sheets go into.",
            "So the sheets sit with the practice's own drawings of that kind "
            "instead of at the root of the book. The subset has to exist "
            "already — the run will not create one, because the Layout Book is "
            "the office's structure to organise — and a missing one is "
            "reported rather than invented, with the sheets left at the root.",
        )

        ttk.Separator(frame).grid(row=row, column=0, columnspan=2, sticky="ew", pady=PAD)
        row += 1

        self.do_plans = tk.BooleanVar(value=False)
        plans_box = ttk.Checkbutton(
            frame, text="Apartment plans and sheets", variable=self.do_plans, command=self._sync
        )
        plans_box.grid(row=row, column=0, columnspan=2, sticky="w")
        Tooltip(
            plans_box,
            "The ADG assessment proper: hours of direct sun per apartment, the "
            "sun patch drawn on each floor plan, and a sheet per time of day. "
            "Needs Zones and windows in the model, so it is off by default — "
            "the facade study works on a massing that has neither.",
        )
        row += 1
        self._caption(frame, row, "Hours of sun per apartment, assessed against the ADG.")
        row += 1

        self.apartments, self.apartment_names, self.apartment_hint, row = self._zone_row(
            frame,
            row,
            "Apartment zones",
            "The layer, then which Zones on it are the dwellings.",
            "Only layers that actually carry Zones are listed, found by asking "
            "the Zones rather than by reading layer names — on this project "
            "three layers are named for zones and hold none, while an "
            "annotation layer holds 37. The names matter as much: one layer "
            "carries 15 apartments, 20 balconies and the storage cupboards, "
            "and a balcony assessed as an apartment is a flat with no living "
            "room, which fails silently and drags the percentage down.",
        )
        self.balconies, self.balcony_names, self.balcony_hint, row = self._zone_row(
            frame,
            row,
            "Balcony zones",
            "The Zones that are private open space. Usually the same layer.",
            "Half of the ADG test. Each apartment is judged on its living room "
            "and on its private open space, and the better of the two governs "
            "— so with no balconies named, every apartment is assessed on its "
            "living room alone and the result is worse than the building is. "
            "Left empty, no Zone is treated as open space at all.",
        )
        self.livable, row = self._entry(
            frame,
            row,
            "Living-room glazing",
            "",
            "Suffix marking the windows and doors of a living room, e.g. _L.",
            "The ADG counts sun into living rooms, not into bedrooms, and a "
            "Zone drawn per apartment cannot say which room is which. Where "
            "the office marks its living-room glazing with a suffix on the "
            "opening ID, that is the better answer and it is used instead of "
            "the room names. Left empty, rooms named 'Living Room' are looked "
            "for — and a project that names none is assessed on every opening, "
            "which reads as a pass it has not earned.",
        )
        self.instants, row = self._entry(
            frame,
            row,
            "Plan times",
            "09:00, 12:00, 15:00",
            "Times of day to draw a sun patch for. Comma separated.",
            "One floor-plan sheet per time. Nine, twelve and three are the "
            "conventional set. Each one adds a set of views and a layout, so a "
            "long list makes a long run.",
        )
        self.shadow_subset, row = self._combo(
            frame,
            row,
            "Times filed in",
            "Layout Book subset the clock-time sheets go into.",
            "A sheet that is a time of day belongs with the practice's own, "
            "which is usually a different subset from the banded plans. Same "
            "rule as those: it has to exist already. This files the sun-patch "
            "sheets this study draws; the Shadow diagram study files its own "
            "under its own setting, because the two are different drawings "
            "and a practice does not always keep them together.",
        )

        ttk.Separator(frame).grid(row=row, column=0, columnspan=2, sticky="ew", pady=PAD)
        row += 1

        self.do_communal = tk.BooleanVar(value=False)
        self.do_hourly = tk.BooleanVar(value=True)
        communal_box = ttk.Checkbutton(
            frame, text="Communal open space", variable=self.do_communal, command=self._sync
        )
        communal_box.grid(row=row, column=0, columnspan=2, sticky="w")
        Tooltip(
            communal_box,
            "Hours of direct sun over an outdoor area that belongs to no "
            "dwelling — a playground, a courtyard, a communal terrace — "
            "banded and drawn on the plan with a legend. Needs only a Zone "
            "drawn round the area: no apartment, no windows, no marked "
            "glazing. It reports area and share and offers no verdict, "
            "because the ruleset carries ADG 4A-1 and that is about "
            "apartments.",
        )
        row += 1
        self.hourly_box = ttk.Checkbutton(
            frame,
            text="including one plan per hour",
            variable=self.do_hourly,
        )
        self.hourly_box.grid(row=row, column=0, columnspan=2, sticky="w", padx=(18, 0))
        Tooltip(
            self.hourly_box,
            "A plan for every whole hour in the window -- 08:00, 09:00 and so "
            "on -- showing what is in sun at that moment, each on its own "
            "sheet. The banded plan says how much sun a place gets across the "
            "day and never says when, which is the question somebody standing "
            "in a courtyard at nine in the morning is asking. Eight more "
            "sheets from an eight to three window.",
        )
        row += 1
        self._caption(frame, row, "Hours of sun over a courtyard, a terrace or a playground.")
        row += 1

        self.communal, self.communal_names, self.communal_hint, row = self._zone_row(
            frame,
            row,
            "Communal zones",
            "The Zones covering communal open space.",
            "Nothing is guessed here. A communal area is not separable from a "
            "dwelling by size -- a courtyard and a big flat are the same "
            "number of square metres -- so this is left for you to name, and "
            "the chooser lists every Zone on the layer with its area. The "
            "Zone outline is the extent measured, and the plane assessed is "
            "one metre above the Zone floor.",
        )
        self.communal_window, row = self._entry(
            frame,
            row,
            "Communal window",
            "08:00-15:00",
            "The hours assessed for the communal study, and the threshold below.",
            "The ADG measures 09:00 to 15:00, and that is what the apartment "
            "study uses whatever is typed here. A council DCP may ask for a "
            "wider window for communal open space -- 08:00 to 15:00 is "
            "common -- so this moves the communal study alone. Every drawing "
            "and the areas sheet carry the window they were made with, and "
            "the run says in yellow when it is not the ruleset's.",
        )
        self.communal_hours, row = self._entry(
            frame,
            row,
            "Communal threshold",
            "2",
            "Hours. Draws a second plan: what clears this, and what does not.",
            "Seven bands say how the hours spread across the space. What a "
            "DCP asks is how much of it clears the minimum, and a banded plan "
            "makes a reader do that sum by eye. This draws that split as two "
            "areas on a sheet of its own. Leave it empty for the bands alone.",
        )
        self.communal_height, row = self._entry(
            frame,
            row,
            "Communal height",
            "0",
            "Metres above the ground to assess. 0 is at ground level.",
            "The height a person's sunlight is measured at. 0 means on the "
            "ground itself, which is taken as 50 mm clear of it: the site mesh "
            "shades, and a point exactly on the ground starts every ray inside "
            "the ground and reads as permanent shade. It matters more than it "
            "sounds — on the reference project, moving from one metre to "
            "ground level took the two-hour figure from 45% to 33%, because "
            "the fall and the built form shade the surface far more than they "
            "shade a point a metre above it.",
        )
        self.communal_grid, row = self._entry(
            frame,
            row,
            "Communal grid",
            "0.5",
            "Sample spacing in metres over the communal area.",
            "Half a metre reads a courtyard properly. This is separate from "
            "the ground grid on purpose: the ground is the whole site, and "
            "gridding all of it this finely costs four times the samples for "
            "an answer nobody asked of it.",
        )
        self.communal_csv, row = self._entry(
            frame,
            row,
            "Communal areas CSV",
            "",
            "Where to write the figures as a spreadsheet. Blank writes none.",
            "Every band, the threshold and every hour, with area and share, "
            "under a header of the settings that produced them. Written before "
            "anything is drawn, so the numbers survive whatever Archicad does "
            "next — and a figure that opens in a spreadsheet is a figure "
            "somebody can check.",
        )

    def _shadows(self, frame: ttk.Frame) -> None:
        """The shadow diagram: what already stands, and what the proposal adds.

        Every row on this page names a *view*, never a layer, and that is the
        whole design. A saved view is where the practice has already said what
        each legend row contains -- and it says it with more than layers: the
        office pins geometry in renovation filters, so two views can show the
        same layers and different buildings. A layer combination cannot
        reproduce that, and on AC26 the API cannot even switch to a view to
        look (``ChangeWindow`` by navigator item wants 27). Publishing the
        views as IFCs hands the question to Archicad, which resolves layers,
        renovation filter and pins the way the drawing does.

        So there is deliberately no layer route here, though the command line
        keeps one for projects that need no views. Offering both would offer a
        way to be quietly wrong on exactly this office's models, and a shadow
        sheet built from the wrong massing still looks like a shadow sheet.
        """
        row = 0
        self.do_shadows = tk.BooleanVar(value=False)
        box = ttk.Checkbutton(
            frame, text="Shadow diagram", variable=self.do_shadows, command=self._sync
        )
        box.grid(row=row, column=0, columnspan=2, sticky="w")
        Tooltip(
            box,
            "The shadow the site casts across itself and its neighbours at "
            "each hour, drawn as fills on the plan. Off by default: it needs "
            "views published first, which is a step in Archicad rather than "
            "here.",
        )
        row += 1
        self._caption(
            frame,
            row,
            "Shadows cast by each massing, hour by hour, drawn from published views.",
        )
        row += 1

        self.view_folder, row = self._folder_row(
            frame,
            row,
            "Published views",
            "The folder your Publisher Set wrote its IFCs into.",
            "Publish a set of per-view IFCs from Archicad first, one view per "
            "legend row. Each file is that view exactly as it draws — layers, "
            "renovation filter, pinned geometry and all — which is why this "
            "asks for views and not for layer combinations. The names below "
            "are read from this folder, so point it here before choosing "
            "them.",
        )
        self.baseline_views, row = self._view_row(
            frame,
            row,
            "Always there",
            "Views whose buildings will be on the site whatever is approved.",
            "The existing neighbours, the future context, whatever already "
            "stands on the site. These accumulate: each is charged only for "
            "ground the earlier ones had not already darkened, so their areas "
            "add up and their fills abut instead of overlapping. Order is the "
            "order drawn, back to front.",
        )
        self.scenario_views, row = self._view_row(
            frame,
            row,
            "Being tested",
            "Views of the massings this sheet is comparing.",
            "The TOD envelope, the SEARs envelope, the proposal. Each is cast "
            "against every baseline and against no other scenario — so two "
            "scenarios overlap on the sheet, which is the comparison being "
            "drawn. Putting one of these under Always there instead tests it "
            "against itself and reads as a much smaller shadow.",
        )
        self.terrain_views, row = self._view_row(
            frame,
            row,
            "Ground it lands on",
            "The view holding the terrain. It receives shadow, never casts it.",
            "Shadows land on the ground and on whatever is standing, so the "
            "terrain has to be in the run — but as a receiver. Among the "
            "occluders it puts every sample below the hill in permanent shade "
            "and prints a solid grey sheet. Left empty, shadows fall on a "
            "flat plane at the datum instead.",
        )
        self.terrain_floor, row = self._entry(
            frame,
            row,
            "Ignore ground below",
            "",
            "Metres. Blank keeps all of it.",
            "A survey often runs past the neighbourhood into ground tens of "
            "metres lower, and a shadow reaching that falls the whole way and "
            "runs on for hundreds of metres. Right arithmetically, and not "
            "what the sheet is asking.",
        )
        self.shadow_dates, row = self._entry(
            frame,
            row,
            "Days",
            DEFAULT_SHADOW_DATES,
            "MM-DD, comma separated.",
            "21 June is the one that decides things — the shortest day, when "
            "shadows are longest. The equinoxes and midsummer are drawn "
            "alongside it by convention. Each day adds a full set of sheets.",
        )
        self.shadow_hours, row = self._entry(
            frame,
            row,
            "Hours",
            DEFAULT_SHADOW_HOURS,
            "Whole hours, comma separated.",
            "One sheet per hour per day. Nine to three is the assessed "
            "window; the long shadows at either end are correct and surprise "
            "people — on 21 June the sun is about 19 degrees up and a shadow "
            "runs nearly three times the height that casts it.",
        )
        self.shadow_favourite, row = self._entry(
            frame,
            row,
            "Fill Favorite",
            "",
            "Name of a Fill Favorite to draw with. Blank uses plain fills.",
            "The way to get the practice's own fill with no contour around "
            "it: Archicad's CreateHatches has a contour pen and no switch to "
            "turn the contour off, so a contour-less fill has to come from a "
            "Favorite made by hand in the project.",
        )
        self.shadow_storey, row = self._entry(
            frame,
            row,
            "Draw on storey",
            "0",
            "Storey index the fills are placed on.",
            "Where the fills land, not what casts them. A site drawing is "
            "usually the ground storey — which is not always storey 0: a "
            "project with a survey datum below it numbers the ground 6, and "
            "fills drawn on 0 land in a plan nobody opens and read as nothing "
            "drawn at all.",
        )
        self.shadow_study_subset, row = self._combo(
            frame,
            row,
            "Diagrams filed in",
            "Layout Book subset the shadow sheets go into.",
            "Same rule as the other two: the subset has to exist already, "
            "because the Layout Book is the office's structure to organise. A "
            "missing one is reported rather than invented, with the sheets "
            "left at the root of the book.",
        )

    def _sun_eyes(self, frame: ttk.Frame) -> None:
        """The sun eye views: the model seen from the sun, one view per hour.

        Everything it can see is in sun and everything hidden is in shade,
        which answers "why is this balcony dark at ten" in one picture. The
        settings here are the ones read off the practice's own diagrams; the
        date and the hours come from the ruleset's assessment window and are
        not offered, because a sun eye set at the wrong date is a plausible
        picture of the wrong day.

        Needs the Loriini add-on beside Tapir, and a floor plan tab in front
        when it runs: the projection can only be written with the window and
        the current database agreeing, and the run says so if they do not.
        """
        row = 0
        self.do_sun_eyes = tk.BooleanVar(value=False)
        box = ttk.Checkbutton(
            frame, text="Sun eye views", variable=self.do_sun_eyes, command=self._sync
        )
        box.grid(row=row, column=0, columnspan=2, sticky="w")
        Tooltip(
            box,
            "The model seen from the sun's own position at each hour of the "
            "assessment window, as saved views, 3D Documents and sheets. Off "
            "by default: it needs the Loriini add-on installed beside Tapir, "
            "and a floor plan tab in front in Archicad when it runs.",
        )
        row += 1
        self._caption(
            frame,
            row,
            "Seven views along the sun, 9am to 3pm on 21 June, each exact to the "
            "sun's own bearing; the documents on two sheets.",
        )
        row += 1

        self.eye_pen_set, row = self._entry(
            frame,
            row,
            "Pen set",
            "",
            "Name of the pen set the views use. Blank keeps each view's own.",
            "The practice's drawing pens, so the sheet prints the way the "
            "other DA drawings do. Read off the office's own sun views on the "
            "reference project as '00 FA Pens'.",
        )
        self.eye_override, row = self._entry(
            frame,
            row,
            "Graphic override",
            "Sun Eye Views",
            "The override combination that paints the glazing.",
            "This is the yellow on the diagram: an override rule, not drawn "
            "geometry. It is a different combination from the shadow "
            "diagrams', and a view given the wrong one shows plain glass.",
        )
        self.eye_base_combination, row = self._entry(
            frame,
            row,
            "Layers, from",
            "04 | Shadow Diagrams",
            "The layer combination the views start from. Zones are then hidden.",
            "Every layer that carries a zone is hidden on top of this, "
            "measured from the zones themselves: zones are bodies in 3D and "
            "sit inside the glazing the diagram is meant to show through.",
        )
        self.eye_scale, row = self._entry(
            frame,
            row,
            "Scale",
            "500",
            "Denominator of the documents' scale on the sheet.",
            "1:500 shows the street around the building, which is what the "
            "practice's sheets do. The views of the 3D window itself are at "
            "1:1 whatever this says; a document is the drawing.",
        )
        self.eye_per_sheet, row = self._entry(
            frame,
            row,
            "Per sheet",
            "4",
            "Documents on each layout. Four is a morning sheet and an afternoon sheet.",
            "Seven hours in fours is 9am to noon on one sheet and 1pm to 3pm "
            "on the next, in the same grid with one cell empty. Every sheet "
            "is laid out as a full one so the two match.",
        )
        self.eye_title_block, row = self._entry(
            frame,
            row,
            "Title block width",
            "100",
            "Millimetres kept clear down the right of the sheet.",
            "Archicad reports a layout's margins and nothing about its "
            "master, so the drawings would otherwise tile under the title "
            "block. About 100 mm on the DA B1 VERTICAL masters.",
        )
        self.eye_hours, row = self._entry(
            frame,
            row,
            "Hours",
            "",
            "Whole hours, comma separated. Blank is the ruleset's window, 9 to 15.",
            "Only for a sheet that needs fewer. The assessment window is the "
            "ruleset's and a set drawn at other hours is not the study.",
        )

    def _site(self, frame: ttk.Frame) -> None:
        """Site and context analysis from an address, drawn into worksheets.

        The one section that reads nothing off the project: its input is a
        street address, and everything else comes from NSW open data. What it
        offers is which of the three sheets to make and where the site lands
        in the project frame; the scale of the context sheet is the only
        number, because the site sheet chooses its own to fit the site.
        """
        row = 0
        self.do_site = tk.BooleanVar(value=False)
        box = ttk.Checkbutton(
            frame, text="Site analysis", variable=self.do_site, command=self._sync
        )
        box.grid(row=row, column=0, columnspan=2, sticky="w")
        Tooltip(
            box,
            "Fetches the neighbourhood from NSW open data for the address below "
            "and draws it into worksheets of the open project: the context "
            "analysis, the site analysis and the development summary, each with "
            "a view at its sheet's scale. Needs the Loriini add-on beside Tapir, "
            "and an internet connection. NSW addresses only.",
        )
        row += 1
        self._caption(
            frame,
            row,
            "Type the address, tick the sheets, press Run. A few minutes: the "
            "public data services are slow and are asked for everything at once.",
        )
        row += 1
        self.site_address, row = self._entry(
            frame,
            row,
            "Address",
            "",
            'Street address of the site, e.g. "26-30 Campsie St, Campsie NSW 2194".',
            "Looked up in the NSW address register, so the spelling can be "
            "loose but the suburb has to be right: a number that exists in "
            "another suburb is refused rather than drawn. A unit number is "
            "taken as its building; a ranged number as every lot in the range.",
        )

        self.do_site_context = tk.BooleanVar(value=True)
        self.do_site_site = tk.BooleanVar(value=True)
        self.do_site_summary = tk.BooleanVar(value=True)
        self.do_site_aerial = tk.BooleanVar(value=False)
        for variable, label, detail in (
            (
                self.do_site_context,
                "Context analysis",
                "Land use wash by LEP zone, schools and hospitals coloured by what "
                "they are, heritage items, rail, bus stops and routes, stations, "
                "5 and 10 minute walking catchments, place and street names, "
                "and the site -- the DA 003 sheet, at the scale below.",
            ),
            (
                self.do_site_site,
                "Site analysis",
                "The boundary with its dimensions and corner levels, the fall, "
                "contours, neighbours with storeys and zone, trees, poles, "
                "hydrants, driveways, kerbside parking, one-way streets, noise "
                "sources, sun path and prevailing winds -- the DA 004 sheet, "
                "at whichever of 1:200 to 1:750 fits the site.",
            ),
            (
                self.do_site_summary,
                "Development summary",
                "The planning-controls table: zone, height of building, FSR and "
                "the gross floor area it allows, minimum lot size, heritage, and "
                "the ADG figures worked out for this site area. Council DCP rows "
                "are ruled up empty; they are not open data.",
            ),
            (
                self.do_site_aerial,
                "Save the aerial photo",
                "Also writes the NSW SIX orthophoto of each sheet's extent to the "
                "run folder as JPEG tiles with world files on the MGA grid. Not "
                "drawn: Archicad's API cannot place a picture, so it is there to "
                "place by hand with File > External Content.",
            ),
        ):
            tick = ttk.Checkbutton(frame, text=label, variable=variable, command=self._sync)
            tick.grid(row=row, column=0, columnspan=2, sticky="w", padx=(18, 0))
            Tooltip(tick, detail)
            row += 1
        self.do_site_model = tk.BooleanVar(value=False)
        self.do_site_location = tk.BooleanVar(value=True)
        for variable, label, detail in (
            (
                self.do_site_model,
                "Model the terrain and the neighbours in 3D",
                "One Mesh from the 1 m contours and one slab per neighbouring "
                "building footprint, as tall as its storeys say -- or as the "
                "LEP height control allows, and said to be assumed -- on the "
                "LORIINI layer, for the shadow and sun eye studies to cast "
                "against. The site's own buildings are left out.",
            ),
            (
                self.do_site_location,
                "Set the project location from the address",
                "Puts Options > Project Location at the site's centre, with the "
                "survey point on the MGA2020 grid, so the sun studies and the "
                "IFC exports know where the project is. Only when the site is "
                "placed at the project origin, and only if the location is still "
                "a city preset; north is kept as it is.",
            ),
        ):
            tick = ttk.Checkbutton(frame, text=label, variable=variable, command=self._sync)
            tick.grid(row=row, column=0, columnspan=2, sticky="w", padx=(18, 0))
            Tooltip(tick, detail)
            row += 1
        self.site_ticks = [
            self.do_site_context,
            self.do_site_site,
            self.do_site_summary,
            self.do_site_aerial,
            self.do_site_model,
            self.do_site_location,
        ]

        self.site_scale, row = self._entry(
            frame,
            row,
            "Context scale",
            "3000",
            "Denominator of the context sheet's scale. 1:3000 on the office's A1 sheets.",
            "Sets how much neighbourhood is fetched: the A1 map field at this "
            "scale, centred on the site. 1:2000 for a tight urban site, 1:4000 "
            "where the station is a long way off. The site sheet ignores this "
            "and picks the scale its boundary fits at.",
        )
        self.site_anchor, row = self._combo(
            frame,
            row,
            "Site lands at",
            "Where the site goes in the project's frame.",
            "'Project location' trusts Options > Project Preferences > Project "
            "Location: the site is placed where the project's own georeferencing "
            "says it is, turned to the project's north, so it lines up with a "
            "model that is already there. 'Project origin' puts the site's centre "
            "at (0, 0) -- for a project that has nothing in it yet, or whose "
            "location is still the Sydney preset.",
        )
        self.site_anchor.config(values=("Project location", "Project origin"), state="readonly")
        self.site_anchor.set("Project location")
        self.site_out, row = self._folder_row(
            frame,
            row,
            "Run folder",
            "Where what was fetched is saved. Blank is Documents\\Loriini\\site-analysis.",
            "Every run writes the data it fetched as JSON here, and the aerial "
            "if asked for. A saved run can be redrawn without the internet: "
            "sun-study site-analysis --from <this folder>.",
        )

    def _folder_row(
        self, parent: ttk.Frame, row: int, label: str, hint: str, detail: str
    ) -> tuple[ttk.Entry, int]:
        """A path, typeable, with a Browse beside it.

        Same shape as ``_picker`` and for the same reason: the entry is the
        honest record of what gets passed and survives being saved, while the
        button is what makes a path on a network drive bearable to enter.
        """
        name = ttk.Label(parent, text=label)
        name.grid(row=row, column=0, sticky="w", pady=(2, 0))
        holder = ttk.Frame(parent)
        holder.grid(row=row, column=1, sticky="ew", pady=(2, 0))
        holder.columnconfigure(0, weight=1)
        box = ttk.Entry(holder)
        box.grid(row=0, column=0, sticky="ew")
        button = ttk.Button(
            holder, text="Browse ...", width=11, command=lambda: self._browse(box, label)
        )
        button.grid(row=0, column=1, padx=(6, 0))
        self._hint(parent, row + 1, hint)
        for target in (name, box, button):
            Tooltip(target, detail)
        return box, row + 2

    def _browse(self, box: ttk.Entry, label: str) -> None:
        """Pick a folder. Leaves the entry alone if the dialog was cancelled."""
        chosen = filedialog.askdirectory(parent=self.root, title=label, mustexist=True)
        if not chosen:
            return
        box.delete(0, "end")
        box.insert(0, chosen)

    def _view_row(
        self, parent: ttk.Frame, row: int, label: str, hint: str, detail: str
    ) -> tuple[ttk.Entry, int]:
        """A list of published views, ticked from the folder rather than typed.

        The names come from the IFCs on disk, not from Archicad: the folder is
        the only place that knows which views were actually published, and a
        view listed in the navigator but missing from the set is exactly the
        mistake worth catching before a run rather than after.
        """
        name = ttk.Label(parent, text=label)
        name.grid(row=row, column=0, sticky="w", pady=(2, 0))
        holder = ttk.Frame(parent)
        holder.grid(row=row, column=1, sticky="ew", pady=(2, 0))
        holder.columnconfigure(0, weight=1)
        box = ttk.Entry(holder)
        box.grid(row=0, column=0, sticky="ew")
        button = ttk.Button(
            holder,
            text="Choose ...",
            width=11,
            command=lambda: self._choose(box, label, hint, available=self._published_views()),
        )
        button.grid(row=0, column=1, padx=(6, 0))
        self._hint(parent, row + 1, hint)
        for target in (name, box, button):
            Tooltip(target, detail)
        return box, row + 2

    def _published_views(self) -> list[str]:
        """The view names the Publisher Set wrote, read off the folder.

        Publisher decorates a filename with whatever the set's naming rule
        says, so the stem is what the run matches on loosely later; offering
        the stems here means the two agree by construction rather than by the
        person typing the same decoration twice.
        """
        folder = Path(self.view_folder.get().strip())
        if not self.view_folder.get().strip() or not folder.is_dir():
            self._write(
                "Point Published views at the folder your Publisher Set wrote "
                "into, then choose from it."
            )
            return []
        return sorted(path.stem for path in folder.glob("*.ifc"))

    def _not_yet(self, frame: ttk.Frame, what: str) -> None:
        """A section for an output this version does not make.

        Said in full rather than greyed out, because a tab that cannot be
        opened cannot explain itself and reads as something broken. There is
        nothing to tick here on purpose: an empty page is honest, and a
        switch that does nothing is not.
        """
        ttk.Label(frame, text=what, foreground=HINT, wraplength=560, justify="left").grid(
            row=0, column=0, columnspan=2, sticky="w"
        )

    def _controls(self, base: ttk.Frame) -> None:
        """Run, the progress bar and the log, under every section alike."""
        #: What pressing Run will actually do. With the ticks spread across
        #: sections there is otherwise nowhere on screen that answers it: a
        #: colleague reading the Facade skin tab can see that study is on and
        #: nothing whatever about the other two.
        self.queued_line = ttk.Label(base, text="", foreground=HINT, wraplength=760)
        self.queued_line.grid(row=0, column=0, sticky="w", pady=(PAD, 0))

        buttons = ttk.Frame(base)
        buttons.grid(row=1, column=0, sticky="ew", pady=(4, 4))
        buttons.columnconfigure(0, weight=1)
        self.go = ttk.Button(buttons, text="Run study", command=self._start)
        self.go.grid(row=0, column=0, sticky="ew")
        Tooltip(
            self.go,
            "Runs every ticked study in the project chosen above, one after "
            "the other. Minutes rather than seconds: the export alone takes a "
            "couple. Nothing is saved — look at the result in Archicad and "
            "save it yourself if you want to keep it.",
        )
        self.cancel = ttk.Button(buttons, text="Stop", command=self._stop, state="disabled")
        self.cancel.grid(row=0, column=1, padx=(6, 0))
        Tooltip(
            self.cancel,
            "Asks the run to stop and lets it put the project's layer state "
            "back on the way out. It can take a few seconds to come to a halt.",
        )
        keep = ttk.Button(buttons, text="Save as default", command=self._remember, width=16)
        keep.grid(row=0, column=2, padx=(6, 0))
        Tooltip(
            keep,
            "Remembers every section -- the ticks, the fields and which "
            "section was open -- and fills the window in with it next time. "
            "Meant for what belongs to the practice rather than to one job: "
            "the layer prefix, the living-room suffix, the Archicad wait, "
            "which studies you run. Anything the open project disagrees with "
            "is overruled by the project, so a saved layer name cannot make a "
            "run measure a layer that is not there.",
        )
        drop = ttk.Button(buttons, text="Forget", command=self._forget, width=9)
        drop.grid(row=0, column=3, padx=(6, 0))
        Tooltip(
            drop,
            "Throws the saved settings away and puts every section back to "
            "what it opens with on a machine that has never been set up. The "
            "project is then read again, so the lists fill from the open "
            "Archicad as they did the first time.",
        )

        self.progress = ttk.Progressbar(base, mode="determinate", maximum=100)
        self.progress.grid(row=2, column=0, sticky="ew")

        self.log = scrolledtext.ScrolledText(
            base, height=11, wrap="word", state="disabled", font=("Consolas", 9)
        )
        self.log.grid(row=3, column=0, sticky="nsew", pady=(6, 0))

        ttk.Label(base, text=STATUS, foreground="#a33", wraplength=760).grid(
            row=4, column=0, sticky="w", pady=(6, 0)
        )
        ttk.Label(
            base,
            text=f"{PRODUCT} {__version__}  ·  created by {AUTHOR}",
            foreground=HINT,
        ).grid(row=5, column=0, sticky="w", pady=(2, 0))

    def _hint(self, parent: ttk.Frame, row: int, text: str) -> ttk.Label:
        """The line under a control. Returned so it can be rewritten: the
        useful thing to say under a zone field is what the project turned out
        to hold, which is not known until it has been read."""
        made = ttk.Label(parent, text=text, foreground=HINT, wraplength=560)
        made.grid(row=row, column=1, sticky="w", pady=(0, 4))
        return made

    def _caption(self, parent: ttk.Frame, row: int, text: str) -> None:
        """What a whole study is, under the tick that turns it on.

        Flush with the left margin and across both columns, unlike ``_hint``,
        which starts where the fields do. A study's tick has no label beside
        it -- it *is* its own label -- so a line indented into the field
        column reads as belonging to the last thing above it, which on both
        of these is the dependent tick. Aligned with the tick instead, it
        plainly captions the block.
        """
        ttk.Label(parent, text=text, foreground=HINT, wraplength=620).grid(
            row=row, column=0, columnspan=2, sticky="w", padx=(18, 0), pady=(2, 4)
        )

    def _combo(
        self, parent: ttk.Frame, row: int, label: str, hint: str, detail: str
    ) -> tuple[ttk.Combobox, int]:
        name = ttk.Label(parent, text=label)
        name.grid(row=row, column=0, sticky="w", pady=(2, 0))
        box = ttk.Combobox(parent, values=[])
        box.grid(row=row, column=1, sticky="ew", pady=(2, 0))
        self._hint(parent, row + 1, hint)
        for target in (name, box):
            Tooltip(target, detail)
        return box, row + 2

    def _entry(
        self, parent: ttk.Frame, row: int, label: str, initial: str, hint: str, detail: str
    ) -> tuple[ttk.Entry, int]:
        name = ttk.Label(parent, text=label)
        name.grid(row=row, column=0, sticky="w", pady=(2, 0))
        box = ttk.Entry(parent)
        box.insert(0, initial)
        box.grid(row=row, column=1, sticky="ew", pady=(2, 0))
        self._hint(parent, row + 1, hint)
        for target in (name, box):
            Tooltip(target, detail)
        return box, row + 2

    def _zone_row(
        self, parent: ttk.Frame, row: int, label: str, hint: str, detail: str
    ) -> tuple[ttk.Combobox, ttk.Entry, ttk.Label, int]:
        """A layer and, beside it, which of the Zones on it are meant.

        One row for the two halves of one question, because they are not
        separable: a layer says where to look and the names say what is there,
        and a project that keeps its apartments, its balconies and its storage
        on one layer -- which is the ordinary case, not an odd one -- is
        measured wrongly by either half alone.

        The names are ticked from the project like the layers are, and the
        line underneath says what was found, so a guess can be checked without
        opening the Zone settings in Archicad.
        """
        name = ttk.Label(parent, text=label)
        name.grid(row=row, column=0, sticky="w", pady=(2, 0))
        holder = ttk.Frame(parent)
        holder.grid(row=row, column=1, sticky="ew", pady=(2, 0))
        holder.columnconfigure(0, weight=2)
        holder.columnconfigure(1, weight=3)

        layer = ttk.Combobox(holder, values=[])
        layer.grid(row=0, column=0, sticky="ew")
        names = ttk.Entry(holder)
        names.grid(row=0, column=1, sticky="ew", padx=(6, 0))
        button = ttk.Button(
            holder,
            text="Choose ...",
            width=11,
            command=lambda: self._choose(
                names,
                f"{label}: which names",
                "Zones on the chosen layer. The size beside each name is what "
                "says what it is: a dwelling is tens of square metres, a "
                "balcony is a few, a storage cupboard is less.",
                [kind.label for kind in self._kinds_on(layer.get())],
                {kind.label: kind.described() for kind in self._kinds_on(layer.get())},
            ),
        )
        button.grid(row=0, column=2, padx=(6, 0))
        line = self._hint(parent, row + 1, hint)
        layer.bind("<<ComboboxSelected>>", lambda _event: self._offer_zone_names())
        for target in (name, layer, names, button):
            Tooltip(target, detail)
        return layer, names, line, row + 2

    def _picker(
        self, parent: ttk.Frame, row: int, label: str, hint: str, detail: str
    ) -> tuple[ttk.Entry, int]:
        """A layer list: ticked from the project, still typeable.

        The entry stays because it is the honest record of what will be
        passed, and because somebody setting up a new project may want to
        paste a list. The button is what makes it usable: a hundred and fifty
        layer names carrying a group number, a dot and a space are not
        something to retype, and a typo here measures nothing and says so
        several minutes later.
        """
        name = ttk.Label(parent, text=label)
        name.grid(row=row, column=0, sticky="w", pady=(2, 0))
        holder = ttk.Frame(parent)
        holder.grid(row=row, column=1, sticky="ew", pady=(2, 0))
        holder.columnconfigure(0, weight=1)
        box = ttk.Entry(holder)
        box.grid(row=0, column=0, sticky="ew")
        button = ttk.Button(
            holder,
            text="Choose ...",
            width=11,
            command=lambda: self._choose(box, label, hint),
        )
        button.grid(row=0, column=1, padx=(6, 0))
        self._hint(parent, row + 1, hint)
        for target in (name, box, button):
            Tooltip(target, detail)
        return box, row + 2

    def _choose(
        self,
        box: ttk.Entry,
        label: str,
        hint: str,
        available: Sequence[str] | None = None,
        describe: Mapping[str, str] | None = None,
    ) -> None:
        """Tick names into an entry. Leaves it alone if nothing was chosen.

        ``available`` defaults to the project's layers, which is what most of
        these are; a zone row passes the names its layer carries instead.
        """
        offered = list(available) if available is not None else pickable(self.options.layers)
        if not offered:
            self._write(f"Nothing to choose from for {label}. Read a project first.")
            return
        dialog = LayerChooser(
            self.root,
            title=label,
            hint=hint,
            available=offered,
            chosen=self._listed(box),
            describe=describe,
        )
        self.root.wait_window(dialog)
        if dialog.result is None:
            return
        box.delete(0, "end")
        box.insert(0, ", ".join(dialog.result))

    # -- which studies are on, said in every place it matters ----------------
    def _studies(self) -> list[tuple[str, tk.BooleanVar, str]]:
        """Every study this window runs: its name, its tick, and its section.

        One list because two things have to agree about it and would
        otherwise be written out separately -- the tab that marks itself when
        the study in it will run, and the line above Run that says what is
        queued. The names are the ones ``jobs`` labels the run with, so a
        colleague reads the same words before pressing Run and in the log
        afterwards.
        """
        return [
            (FACADE_JOB, self.do_facade, self.FACADE),
            (PLANS_JOB, self.do_plans, self.DIAGRAMS),
            (COMMUNAL_JOB, self.do_communal, self.DIAGRAMS),
            (SHADOW_JOB, self.do_shadows, self.SHADOWS),
            (SUN_EYE_JOB, self.do_sun_eyes, self.EYE),
            (SITE_JOB, self.do_site, self.SITE),
        ]

    def _open_section(self) -> str:
        """Which tab is showing, by title.

        By title and not by number so that inserting Shadow diagram ahead of
        Sun eye view, or dropping a section, cannot reopen somebody on a
        different page than the one they left.
        """
        try:
            # Tk's own wrappers, and none of ttk's Notebook is annotated --
            # hence the three ignores in this file and nowhere else.
            return self.titles[self.tabs.index("current")]  # type: ignore[no-untyped-call]
        except (tk.TclError, IndexError):  # pragma: no cover - no tab yet
            return ""

    def _show_section(self, title: str) -> None:
        """Open that tab, if there is still one by that name."""
        if title in self.titles:
            self.tabs.select(self.titles.index(title))  # type: ignore[no-untyped-call]

    def _sync(self) -> None:
        """Keep the window telling the truth about what it will do.

        Three things that are one fact seen from three places, which is why
        they move together.

        A dependent tick is only a question while the study above it is on.

        A tab whose study will run says so on its own label. That is what
        tabs cost: a section nobody opens is a section nobody knows the state
        of, and a colleague who has never opened Solar diagrams should still
        be able to see from the outside that two studies are waiting in it.

        And the line above Run names every study queued. With the ticks
        spread over three sections there is otherwise nowhere on screen that
        answers "what happens if I press this" -- which is the one question a
        run of several minutes had better not get wrong.
        """
        self.floors_box.config(state="normal" if self.do_facade.get() else "disabled")
        self.hourly_box.config(state="normal" if self.do_communal.get() else "disabled")

        running = {where for _name, tick, where in self._studies() if tick.get()}
        for index, title in enumerate(self.titles):
            self.tabs.tab(  # type: ignore[no-untyped-call]
                index, text=f"{title} ✓" if title in running else title
            )

        queued = [name for name, tick, _where in self._studies() if tick.get()]
        self.queued_line.config(
            text=(
                "Will run: " + ", then ".join(queued)
                if queued
                else "Nothing ticked. Choose a study in one of the sections above."
            )
        )

    # -- settings that outlive the window ------------------------------------
    def _fields(self) -> dict[str, ttk.Entry]:
        """Every typed or chosen setting, under a name that survives a rename.

        Written out here rather than gathered off the widget tree, because the
        name is what a saved file is keyed on: a field moved into Advanced, or
        relabelled, or re-ordered, must not cost somebody the setting they
        saved. A ``Combobox`` is an ``Entry``, so both kinds are read and
        written the same way and belong in one mapping.

        The Archicad picker is deliberately not here. Which instance answered,
        on which port, is a fact about this afternoon rather than a preference,
        and restoring last week's port would point the study at whatever holds
        it today.
        """
        return {
            "apartment_layer": self.apartments,
            "apartment_names": self.apartment_names,
            "balcony_layer": self.balconies,
            "balcony_names": self.balcony_names,
            "communal_layer": self.communal,
            "communal_names": self.communal_names,
            "communal_window": self.communal_window,
            "communal_hours": self.communal_hours,
            "communal_height": self.communal_height,
            "communal_grid": self.communal_grid,
            "communal_csv": self.communal_csv,
            "master_layout": self.master,
            "exclude_above": self.exclude,
            "year": self.year,
            "layer_combination": self.combination,
            "subject_layers": self.subject,
            "require_layers": self.require,
            "context_layers": self.context,
            "hide_layers": self.hide,
            "livable_suffix": self.livable,
            "plan_instants": self.instants,
            "shadow_subset": self.shadow_subset,
            "shadow_view_folder": self.view_folder,
            "shadow_baseline_views": self.baseline_views,
            "shadow_scenario_views": self.scenario_views,
            "shadow_terrain_views": self.terrain_views,
            "shadow_terrain_floor": self.terrain_floor,
            "shadow_dates": self.shadow_dates,
            "shadow_hours": self.shadow_hours,
            "shadow_favourite": self.shadow_favourite,
            "shadow_storey": self.shadow_storey,
            "shadow_study_subset": self.shadow_study_subset,
            "eye_pen_set": self.eye_pen_set,
            "eye_override": self.eye_override,
            "eye_base_combination": self.eye_base_combination,
            "eye_scale": self.eye_scale,
            "eye_per_sheet": self.eye_per_sheet,
            "eye_title_block": self.eye_title_block,
            "eye_hours": self.eye_hours,
            "site_address": self.site_address,
            "site_scale": self.site_scale,
            "site_anchor": self.site_anchor,
            "site_out": self.site_out,
            "adg_subset": self.adg_subset,
            "layer_prefix": self.prefix,
            "archicad_wait_minutes": self.wait_min,
            "skin_grid": self.grid_m,
        }

    def _ticks(self) -> dict[str, tk.BooleanVar]:
        """The boxes, by the same rule."""
        return {
            "study_facade": self.do_facade,
            "study_floors": self.do_floors,
            "study_plans": self.do_plans,
            "study_communal": self.do_communal,
            "study_shadows": self.do_shadows,
            "study_sun_eyes": self.do_sun_eyes,
            "study_hourly": self.do_hourly,
            "study_site": self.do_site,
            "site_context": self.do_site_context,
            "site_site": self.do_site_site,
            "site_summary": self.do_site_summary,
            "site_aerial": self.do_site_aerial,
            "site_model": self.do_site_model,
            "site_location": self.do_site_location,
        }

    #: The section that was showing, saved under its own name. It is neither
    #: a field nor a tick, and it is worth keeping for the same reason the
    #: open Advanced panel used to be: somebody who only ever runs the
    #: diagrams should open on the diagrams, not on a page of layer settings
    #: they set up once in March.
    OPEN_SECTION = "open_section"

    def settings(self) -> dict[str, str | bool]:
        """Everything on this page, as it would be saved. Public and pure, so
        the round trip can be tested without a file or a screen."""
        saved: dict[str, str | bool] = {name: box.get() for name, box in self._fields().items()}
        saved.update({name: state.get() for name, state in self._ticks().items()})
        saved[self.OPEN_SECTION] = self._open_section()
        return saved

    def apply(self, saved: Mapping[str, str | bool]) -> None:
        """Put saved settings back on the page, one at a time.

        Each is taken only if it is the kind of thing that field holds, and
        anything unrecognised is ignored: a file written by a later version,
        or edited by hand, should cost the settings it got wrong and no more.
        Nothing here is validated further than that, because every one of
        these is checked against the open project a moment later -- the lists
        are refilled from Archicad and a name the project does not have is
        replaced by one it does.
        """
        for name, box in self._fields().items():
            value = saved.get(name)
            if isinstance(value, str):
                box.delete(0, "end")
                box.insert(0, value)
        for name, state in self._ticks().items():
            ticked = saved.get(name)
            if isinstance(ticked, bool):
                state.set(ticked)
        opened = saved.get(self.OPEN_SECTION)
        if isinstance(opened, str):
            # A title this version does not have -- a file saved before the
            # sections existed, or after one was renamed -- leaves the
            # notebook where it is, which is General. Losing which tab was
            # open is the cheapest thing in the file to lose.
            self._show_section(opened)
        self._sync()

    def _restore(self) -> None:
        """Open with what was saved, if anything was."""
        saved = preferences.load()
        if not saved:
            return
        self.apply(saved)
        self._write(f"Opened with the settings saved in {preferences.path()}.")

    def _remember(self) -> None:
        """Save the page as the defaults, and say where it went.

        The path is printed because a preference nobody can find is one nobody
        can delete, and "it remembers the wrong thing now" is otherwise an
        unanswerable complaint. A profile that will not be written to is
        reported in the log rather than raised: it is a failure to save a
        convenience, not a failure to run a study.
        """
        try:
            where = preferences.save(self.settings())
        except OSError as refused:
            self._write(f"Could not save the settings: {refused}")
            return
        self._write(f"Saved. These settings are what the window will open with: {where}")

    def _forget(self) -> None:
        """Back to how the window opens on a machine nobody has set up."""
        where = preferences.path()
        had = where.is_file()
        gone = preferences.forget()
        self.apply(self.factory)
        if self.options.reachable:
            # Emptied fields are filled from the project again, exactly as
            # they were the first time it was read -- otherwise Forget leaves
            # a page of blanks rather than a page of defaults.
            self._offer()
        if not had:
            self._write("There were no saved settings. The page is back to its defaults.")
        elif gone:
            self._write("Saved settings deleted. The page is back to its defaults.")
        else:
            self._write(f"The page is back to its defaults, but {where} could not be deleted.")

    # -- reading the project -------------------------------------------------
    def refresh(self) -> None:
        found = probe.running()
        self.ports = [instance.port for instance in found]
        self.instance.config(
            values=[f"{instance.project}  ·  port {instance.port}" for instance in found]
        )
        if not found:
            self.status.config(text="no Archicad answering. Open a project, then Refresh.")
            self.options = probe.ProjectOptions()
            return
        if self.instance.current() < 0:
            self.instance.current(0)

        self.status.config(text="reading the project ...")
        self.root.update_idletasks()
        self.options = probe.options(self.ports[max(self.instance.current(), 0)])
        self._offer()

    def _offer(self) -> None:
        found = self.options
        # Nothing here works without the add-on: 116 of the tool's 124
        # Archicad calls are Tapir commands. So it is said plainly, and Run is
        # switched off rather than left to fail several steps later.
        self.go.config(state="disabled" if found.tapir_missing else "normal")
        if found.tapir_missing:
            self.status.config(
                text=(
                    "Archicad is running but the Tapir add-on is not installed, and "
                    "nothing here works without it. Install the Archicad 26 build "
                    "from github.com/ENZYME-APD/tapir-archicad-automation/releases, "
                    "restart Archicad, then press Refresh."
                ),
                foreground="#a33",
            )
            return
        if not found.reachable:
            self.status.config(
                text="; ".join(found.problems) or "could not read the project",
                foreground="#a33",
            )
            return
        self.status.config(
            foreground=HINT,
            text=(
                f"Tapir {found.tapir} · {len(found.layers)} layers · "
                f"{len(found.zone_layers)} carry zones · {len(found.masters)} masters · "
                f"{len(found.subsets)} subsets"
            ),
        )
        self._only_what_this_project_has()
        self._fill(self.apartments, found.zone_layers, ("Zone.Unit", "Zone."))
        # The balconies are usually on the apartments' own layer -- one
        # project keeps 15 units, 20 balconies and the storage on
        # "06 | Zone.Units" -- so that is the first candidate, and a layer
        # carrying open-space-sized zones is the next.
        self._fill(
            self.balconies,
            found.zone_layers,
            (self.apartments.get(), *(kind.layer for kind in found.zone_kinds if kind.open_space)),
        )
        self._fill(self.communal, found.zone_layers, ("Zone.", "Calc."))
        self._offer_zone_names()
        # "VERTICAL - No Scale" before "COVER/NO SCALE": both say no scale and
        # one of them is a cover sheet.
        self._fill(
            self.master,
            found.masters,
            ("VERTICAL - No Scale", "VERTICAL NO SCALE", "No Scale", "A1"),
        )
        self._fill(self.combination, found.combinations, ("IFC ARCH", "IFC"))
        self._fill(self.shadow_subset, found.subsets, ("SHADOW",))
        self._fill(self.adg_subset, found.subsets, ("ADG",))
        self._fill(self.shadow_study_subset, found.subsets, ("SHADOW",))
        if not self.subject.get():
            skin = [name for name in found.layers if any(w in name for w in SKIN_WORDS)]
            self.subject.insert(0, ", ".join(skin))
        self._offer_height_cut()

    def _offer_height_cut(self) -> None:
        """Put this building's own height in "Ignore above", not a placeholder.

        100 m is a guess that is wrong twice over: on a townhouse it cuts
        nothing and lets a parked hotlink master through, and on a tower it
        cuts the top ten storeys off the thing being measured. The project
        knows its own storeys, so the cut is offered from them -- the topmost
        storey plus enough headroom for a roof and a lift overrun.

        Only over the placeholder. A figure somebody typed, or one restored
        from saved settings, is a decision and outranks anything read here;
        the tooltip says so. An unreadable storey list leaves the placeholder
        alone rather than clearing the field, because an empty box means
        "measure everything, however high" and that is the failure this
        setting exists to prevent.

        And only when the answer is *lower* than the placeholder. A project
        can park its hotlink masters on real storeys, and then the storey list
        describes the parked geometry rather than the building: the reference
        project defines 134 storeys running to 422.5 m, at a regular 3.2 m
        spacing the whole way, with no gap to tell the tower from the masters
        above it. A cut at 437 m excludes nothing, which makes it worse than
        the placeholder rather than better -- so it is refused, and the reason
        is printed rather than swallowed.
        """
        top = self.options.top_storey_m
        if top is None or self.exclude.get().strip() != DEFAULT_EXCLUDE_ABOVE_M:
            return
        cut = math.ceil(top + STOREY_HEADROOM_M)
        if cut >= float(DEFAULT_EXCLUDE_ABOVE_M):
            self._write(
                f"Left 'Ignore above' at {DEFAULT_EXCLUDE_ABOVE_M} m. This project's "
                f"highest storey is at {top:g} m, which would put the cut at {cut} m "
                f"-- above everything, so it would exclude nothing. That usually means "
                f"the hotlink masters are parked on storeys of their own. Set it by "
                f"hand to just above the real building."
            )
            return
        self.exclude.delete(0, "end")
        self.exclude.insert(0, str(cut))
        self._write(
            f"Ignore above set to {cut} m from the project: its highest storey is at "
            f"{top:g} m, plus {STOREY_HEADROOM_M:g} m for a roof and a lift overrun."
        )

    @staticmethod
    def _as_the_project_spells_them(
        wanted: Iterable[str], offered: Iterable[str]
    ) -> tuple[list[str], list[str]]:
        """Split names into the ones this project has and the ones it has not.

        Spacing and case are ignored going in, because a name pasted out of a
        report or typed by hand is nobody's memory test; what comes back is
        the project's own spelling, because that is what the command line has
        to be handed.
        """
        by_shape = {" ".join(name.split()).casefold(): name for name in offered}
        kept: list[str] = []
        lost: list[str] = []
        for name in wanted:
            found = by_shape.get(" ".join(name.split()).casefold())
            (lost if found is None else kept).append(name if found is None else found)
        return kept, lost

    def _only_what_this_project_has(self) -> None:
        """Drop layer names the open project does not carry, and correct the rest.

        Saved settings are why this is here. A picker is filled from the
        project only when it is *empty*, so a facade list saved on one job and
        opened on another would sit there unchallenged and produce a study of
        nothing -- no error, no warning, the exact silent wrong answer this
        window exists to prevent. Dropping the names leaves the field empty,
        and an empty field is filled from the project like a first run.

        It also catches a typed name, which is the same fault arriving by a
        different door, and rewrites a name whose spacing does not match.

        Said out loud, never silently: a setting that vanished without a word
        is worse than one that was wrong.
        """
        lost: list[str] = []
        for box in (self.subject, self.require, self.context, self.hide):
            kept, dropped = self._as_the_project_spells_them(self._listed(box), self.options.layers)
            if not dropped and kept == self._listed(box):
                continue
            lost += dropped
            box.delete(0, "end")
            box.insert(0, ", ".join(kept))
        if lost:
            self._write(f"Not layers in this project, so left out: {_some(lost)}")

    def _fill(self, box: ttk.Combobox, values: tuple[str, ...], prefer: tuple[str, ...]) -> None:
        """Offer these, and pick the likeliest -- without overriding a choice.

        Preferences are ordered fragments rather than exact names, because the
        thing being guessed at is an office's own naming.
        """
        box.config(values=list(values))
        if not values or box.get() in values:
            return
        for want in prefer:
            match = next((v for v in values if want.casefold() in v.casefold()), None)
            if match is not None:
                box.set(match)
                return
        box.set(values[0])

    # -- running ---------------------------------------------------------------
    def _kinds_on(self, layer: str) -> list[probe.ZoneKind]:
        """What the zones on one layer are called, biggest first."""
        wanted = " ".join(layer.split()).casefold()
        return [
            kind
            for kind in self.options.zone_kinds
            if " ".join(kind.layer.split()).casefold() == wanted
        ]

    def _offer_zone_names(self) -> None:
        """Fill in which zones are dwellings and which are open space.

        Guessed from floor area, and *shown* rather than applied quietly: the
        line under each field says what was found and what was taken, and the
        chooser lists every name with its size. A guess nobody can see is the
        thing this window exists to avoid.

        Area because nothing else in the model separates them. The names are
        an office's own codes -- ``G08``, ``BY``, ``SC`` -- and mean nothing
        outside it, while an apartment is tens of square metres and a balcony
        is a few, on every project there has ever been. The cuts are the ADG's
        own figures, in ``probe``.
        """
        for layer_box, names_box, line, wanted, what in (
            (self.apartments, self.apartment_names, self.apartment_hint, "dwelling", "dwellings"),
            (self.balconies, self.balcony_names, self.balcony_hint, "open_space", "open space"),
        ):
            kinds = self._kinds_on(layer_box.get())
            if not kinds:
                line.config(text=f"No Zones read on that layer, so nothing is taken as {what}.")
                continue
            self._only_on_this_layer(names_box, layer_box.get(), kinds)
            fits = [kind for kind in kinds if getattr(kind, wanted)]
            if not self._listed(names_box):
                names_box.delete(0, "end")
                names_box.insert(0, ", ".join(kind.label for kind in fits))
            taken = f" — taken as {what}: {_some(k.label for k in fits)}" if fits else ""
            line.config(text=f"carries {_some(kind.described() for kind in kinds)}{taken}")

        # Communal space gets no guess. Area cannot tell a courtyard from a
        # large flat, so the line reports what is on the layer and stops.
        kinds = self._kinds_on(self.communal.get())
        if kinds:
            self._only_on_this_layer(self.communal_names, self.communal.get(), kinds)
        self.communal_hint.config(
            text=(
                f"carries {_some(kind.described() for kind in kinds)} — name the "
                f"ones that are communal"
                if kinds
                else "No Zones read on that layer."
            )
        )

    def _only_on_this_layer(
        self, names_box: ttk.Entry, layer: str, kinds: Sequence[probe.ZoneKind]
    ) -> None:
        """Keep only the zone names that layer actually carries.

        The same fault as a stale layer, one level down, and it arrives by two
        doors: a name saved on another project, and a layer changed under
        names that were guessed for the one before it. Either way the study is
        told to measure zones that are not there, which is not an error --
        it is an assessment of nothing, or of half a building.

        Only when the layer gave up its zones at all. A layer that read as
        empty is a failure to read, not a statement that the names are wrong,
        and it must not cost somebody a list they chose.
        """
        wanted = self._listed(names_box)
        kept, dropped = self._as_the_project_spells_them(wanted, (kind.label for kind in kinds))
        if not dropped and kept == wanted:
            return
        names_box.delete(0, "end")
        names_box.insert(0, ", ".join(kept))
        if dropped:
            self._write(f"Not Zones on {layer}, so left out: {_some(dropped)}")

    def _zone_defaults(self) -> list[str]:
        """Zone layers worth forcing into the export, narrowest first.

        A project's zone layers are the ones named for zones. Everything else
        that happens to carry a Zone -- annotation, area calculations -- is
        somebody's schedule, not the building.
        """
        named = [name for name in self.options.zone_layers if "Zone." in name]
        return named or list(self.options.zone_layers)

    def _window(self) -> tuple[str, str]:
        """The communal study's assessment window, as start and end.

        One field because it is one thought -- "eight to three" -- and two
        boxes invite a start with no end. Anything unparseable is passed as
        nothing rather than guessed at: the run then uses the ruleset's own
        window and says so, which is the safe way to be wrong.
        """
        typed = self.communal_window.get().strip()
        halves = [half.strip() for half in typed.replace("to", "-").split("-")]
        if len(halves) != 2 or not all(halves):
            return "", ""
        return halves[0], halves[1]

    def _listed(self, entry: ttk.Entry) -> list[str]:
        return [part.strip() for part in entry.get().split(",") if part.strip()]

    def _wait_seconds(self) -> str:
        """The Archicad wait, in the seconds the command line wants.

        Minutes on screen because that is the unit the number is thought about
        in -- an export takes minutes -- and seconds on the command line
        because that is what ``--timeout`` takes. Anything unreadable falls
        back to the command line's own default rather than refusing to run: a
        mistyped wait should not stop a study, and the default is right for
        every project but the largest.
        """
        try:
            minutes = float(self.wait_min.get().strip())
        except ValueError:
            return f"{DEFAULT_TIMEOUT_SECONDS:g}"
        if minutes <= 0:
            return f"{DEFAULT_TIMEOUT_SECONDS:g}"
        return f"{minutes * 60.0:g}"

    def jobs(self) -> list[Job]:
        """The command lines the ticked boxes mean. Public, and pure, because
        this is the part worth testing without a window on screen."""
        port = str(self.ports[max(self.instance.current(), 0)]) if self.ports else ""
        common = ["--port", port, "--timeout", self._wait_seconds()]
        if self.prefix.get().strip():
            common += ["--layer-prefix", self.prefix.get().strip()]
        made: list[Job] = []

        if self.do_facade.get():
            args = ["massing", "--timezone", "Australia/Sydney", *common]
            for name in self._listed(self.subject):
                args += ["--subject-layer", name]
            # Without an IfcSpace there is nothing to fit the skin onto the
            # building against, and no IFC combination shows the zone layers.
            for name in self._listed(self.require) or self._zone_defaults():
                args += ["--require-layer", name]
            for name in self._listed(self.hide):
                args += ["--hide-layer", name]
            if self.combination.get():
                args += ["--layer-combination", self.combination.get()]
            if self.exclude.get().strip():
                args += ["--exclude-above", self.exclude.get().strip()]
            args += [
                "--model-bands",
                "--model-flat" if self.do_floors.get() else "--no-model-flat",
                "--model-grid",
                self.grid_m.get().strip() or "0.5",
            ]
            if self.master.get():
                args += ["--master-layout", self.master.get()]
            args += ["--year", self.year.get().strip() or "2024"]
            made.append(Job(FACADE_JOB, args))

        if self.do_plans.get():
            args = [
                "archicad-run",
                "--timezone",
                "Australia/Sydney",
                *common,
                "--draw",
                "--sheet",
            ]
            if self.apartments.get():
                args += ["--apartment-zone-layer", self.apartments.get()]
            # Without these the layer alone decides, and a layer that mixes
            # dwellings with balconies and storage -- the ordinary case --
            # assesses all three as apartments. Two thirds of them have no
            # living room, so the building fails on zones nobody lives in.
            for name in self._listed(self.apartment_names):
                args += ["--apartment-zone-name", name]
            # The balcony layer is only named when something narrows it. On its
            # own, with the apartments' own layer chosen, it would take every
            # apartment for open space and leave the assessment with no
            # apartments at all.
            balcony_names = self._listed(self.balcony_names)
            if self.balconies.get() and (
                balcony_names or self.balconies.get() != self.apartments.get()
            ):
                args += ["--open-space-zone-layer", self.balconies.get()]
                for name in balcony_names:
                    args += ["--open-space-zone-name", name]
            if self.livable.get().strip():
                args += ["--livable-suffix", self.livable.get().strip()]
            if self.combination.get():
                args += ["--layer-combination", self.combination.get()]
            for name in self._listed(self.hide):
                args += ["--hide-layer", name]
            for stamp in self._listed(self.instants):
                args += ["--plan-instant", stamp]
            if self.exclude.get().strip():
                args += ["--exclude-above", self.exclude.get().strip()]
            if self.master.get():
                args += ["--master-layout", self.master.get()]
            if self.shadow_subset.get():
                args += ["--layout-subset", self.shadow_subset.get()]
            if self.adg_subset.get():
                args += ["--adg-subset", self.adg_subset.get()]
            args += ["--year", self.year.get().strip() or "2024"]
            made.append(Job(PLANS_JOB, args))

        if self.do_communal.get():
            args = ["massing", "--timezone", "Australia/Sydney", *common]
            if self.communal.get():
                args += ["--zone-layer", self.communal.get()]
            for name in self._listed(self.communal_names):
                args += ["--zone-name", name]
            # The drawing is fitted onto the project from Zones the export
            # carries, and the communal layer alone gives one. A second zone
            # layer is what makes the plan placeable; those Zones are not
            # measured.
            for name in self._listed(self.require) or self._zone_defaults():
                args += ["--require-layer", name]
            if self.combination.get():
                args += ["--layer-combination", self.combination.get()]
            for name in self._listed(self.hide):
                args += ["--hide-layer", name]
            if self.exclude.get().strip():
                args += ["--exclude-above", self.exclude.get().strip()]
            if self.master.get():
                args += ["--master-layout", self.master.get()]
            if self.adg_subset.get():
                args += ["--zone-subset", self.adg_subset.get()]
            for name in self._listed(self.context):
                # Both: into the export, because a neighbour that is not
                # exported shades nothing, and out of the facade denominator.
                args += ["--require-layer", name, "--context-layer", name]
            if self.communal_height.get().strip():
                args += ["--zone-height", self.communal_height.get().strip()]
            if self.communal_grid.get().strip():
                args += ["--zone-grid", self.communal_grid.get().strip()]
            if self.communal_csv.get().strip():
                args += ["--zone-csv", self.communal_csv.get().strip()]
            start, end = self._window()
            if start:
                args += ["--window-start", start]
            if end:
                args += ["--window-end", end]
            if self.communal_hours.get().strip():
                args += ["--zone-hours", self.communal_hours.get().strip()]
            # The areas always. The plans are what a reader looks at and the
            # areas are what they quote, and a study that draws the one
            # without writing the other invites a figure read off a colour.
            args += ["--zone-sheet", "--zone-stats"]
            if self.do_hourly.get():
                args += ["--zone-hourly"]
            args += ["--year", self.year.get().strip() or "2024"]
            made.append(Job(COMMUNAL_JOB, args))

        if self.do_shadows.get():
            args = ["shadows", "--timezone", "Australia/Sydney", *common]
            # Views, never layers: the folder is a set of per-view IFCs and
            # Archicad has already resolved each one's layers, renovation
            # filter and pins. Passing the name bare makes it both the legend
            # row and what finds the geometry, which is what the view name
            # already is.
            if self.view_folder.get().strip():
                args += ["--shadow-from-views", self.view_folder.get().strip()]
            for name in self._listed(self.baseline_views):
                args += ["--shadow-source", name]
            for name in self._listed(self.scenario_views):
                args += ["--shadow-scenario", name]
            for name in self._listed(self.terrain_views):
                args += ["--shadow-terrain", name]
            if self.terrain_floor.get().strip():
                args += ["--shadow-terrain-floor", self.terrain_floor.get().strip()]
            if self.shadow_dates.get().strip():
                args += ["--shadow-date", self.shadow_dates.get().strip()]
            if self.shadow_hours.get().strip():
                args += ["--shadow-hour", self.shadow_hours.get().strip()]
            if self.shadow_favourite.get().strip():
                args += ["--shadow-favourite", self.shadow_favourite.get().strip()]
            if self.shadow_storey.get().strip():
                args += ["--shadow-storey", self.shadow_storey.get().strip()]
            if self.exclude.get().strip():
                args += ["--exclude-above", self.exclude.get().strip()]
            if self.master.get():
                args += ["--master-layout", self.master.get()]
            if self.shadow_study_subset.get():
                args += ["--shadow-subset", self.shadow_study_subset.get()]
            args += ["--year", self.year.get().strip() or "2024"]
            made.append(Job(SHADOW_JOB, args))

        if self.do_sun_eyes.get():
            args = ["sun-eyes", "--timezone", "Australia/Sydney", *common]
            # Only what is filled in is sent, so the command's own defaults
            # -- read off the practice's diagrams -- hold for a blank field,
            # and a blank pen set keeps each view's own rather than naming
            # one the project does not have.
            for flag, field in (
                ("--pen-set", self.eye_pen_set),
                ("--override", self.eye_override),
                ("--base-combination", self.eye_base_combination),
                ("--scale", self.eye_scale),
                ("--per-sheet", self.eye_per_sheet),
                ("--title-block-mm", self.eye_title_block),
                ("--hour", self.eye_hours),
            ):
                if field.get().strip():
                    args += [flag, field.get().strip()]
            if self.master.get():
                args += ["--master-layout", self.master.get()]
            args += ["--year", self.year.get().strip() or "2024"]
            made.append(Job(SUN_EYE_JOB, args))

        if self.do_site.get() and self.site_address.get().strip():
            args = ["site-analysis", self.site_address.get().strip(), *common]
            args += ["--context" if self.do_site_context.get() else "--no-context"]
            args += ["--site" if self.do_site_site.get() else "--no-site"]
            args += ["--summary" if self.do_site_summary.get() else "--no-summary"]
            if self.do_site_aerial.get():
                args += ["--aerial"]
            if self.do_site_model.get():
                args += ["--model"]
            if not self.do_site_location.get():
                args += ["--keep-location"]
            if self.site_scale.get().strip():
                args += ["--scale", self.site_scale.get().strip()]
            # The combobox says it in words; the command takes a keyword.
            args += [
                "--anchor",
                "site" if "origin" in self.site_anchor.get().lower() else "location",
            ]
            if self.site_out.get().strip():
                args += ["--out", self.site_out.get().strip()]
            made.append(Job(SITE_JOB, args))

        return made

    def _start(self) -> None:
        if self.run is not None and self.run.running:
            return
        if not self.ports:
            self._write("No Archicad to run against. Open a project, then Refresh.")
            return
        if self.options.tapir_missing:
            self._write("The Tapir add-on is not installed in this Archicad.")
            return
        if self.do_site.get() and not self.site_address.get().strip():
            self._write("Site analysis is ticked but has no address. Type one in its section.")
            return
        queued = self.jobs()
        if not queued:
            self._write("Nothing selected. Tick a study first.")
            return

        self.log.config(state="normal")
        self.log.delete("1.0", "end")
        self.log.config(state="disabled")
        self.queued = queued
        self.go.config(state="disabled")
        self.cancel.config(state="normal")
        self.progress.config(mode="indeterminate")
        self.progress.start(12)
        self._next()

    def _next(self) -> None:
        if not self.queued:
            self._write("")
            self._write("Done.")
            self._finished()
            return
        job = self.queued.pop(0)
        self._write(f"── {job.label} ──")
        self._write("sun-study " + " ".join(job.args))
        self._write("")
        self.run = Run(
            job.args,
            on_line=lambda line: self.incoming.put(("line", line)),
            on_done=lambda code: self.incoming.put(("done", code)),
        )
        self.run.start()

    def _stop(self) -> None:
        self.queued.clear()
        if self.run is not None:
            self.run.stop()

    def _finished(self) -> None:
        self.progress.stop()
        self.progress.config(mode="determinate", value=0)
        self.go.config(state="normal")
        self.cancel.config(state="disabled")

    def _drain(self) -> None:
        """Move the worker's lines into the widget, on the UI thread."""
        try:
            while True:
                kind, payload = self.incoming.get_nowait()
                if kind == "line":
                    self._write(str(payload))
                    continue
                code = int(payload)  # type: ignore[call-overload]
                if code == 0:
                    self._next()
                else:
                    self._write(f"[the study stopped, exit code {code}]")
                    self.queued.clear()
                    self._finished()
        except queue.Empty:
            pass
        self.root.after(80, self._drain)

    def _write(self, line: str) -> None:
        self.log.config(state="normal")
        self.log.insert("end", line + "\n")
        self.log.see("end")
        self.log.config(state="disabled")


def icon_path() -> Path | None:
    """Where the application icon is, packaged or not.

    A ``--onefile`` build unpacks itself to a temp directory on every start
    and points ``sys._MEIPASS`` at it, so the packaged path is neither beside
    the .exe nor beside this file. From a source checkout it is where it was
    written, at the top of the project.

    ``None`` rather than an exception when it is missing: an icon is how the
    window looks, not whether the study runs, and a checkout that has not been
    given one should still open.
    """
    packaged = getattr(sys, "_MEIPASS", None)
    root = Path(packaged) if packaged else Path(__file__).resolve().parents[3]
    found = root / "assets" / "loriini.ico"
    return found if found.is_file() else None


def wear_the_icon(window: tk.Tk) -> None:
    """Put the application icon on the window and everything it opens.

    ``--icon`` at build time carves the icon into the .exe as a Windows
    resource, which is what Explorer and the task bar read -- and Tk never
    looks at it. Without this the executable has the right icon in the folder
    and the window it opens wears the Tk feather, which is the sort of half a
    job somebody notices immediately.

    ``default=`` rather than a plain call, so the message boxes and dialogs
    raised later inherit it instead of each needing to be found and dressed.
    """
    found = icon_path()
    if found is None:
        return
    try:
        window.iconbitmap(default=str(found))  # type: ignore[no-untyped-call]
    except tk.TclError:
        # .ico is a Windows format and ``default=`` is a Windows option. This
        # is a Windows tool, but a developer on anything else should get a
        # plain window rather than a crash on the way to one.
        pass


def launch() -> None:
    root = tk.Tk()
    try:
        ttk.Style().theme_use("vista")
    except tk.TclError:
        pass
    wear_the_icon(root)
    Window(root)
    root.mainloop()
