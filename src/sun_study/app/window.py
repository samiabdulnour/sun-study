"""The window itself.

Shaped by one rule: a colleague should have to decide only what is genuinely a
decision. Everything that is a property of the *project* -- which layer carries
the apartments, which masters exist, which Layout Book subsets the practice
files this kind of drawing under -- is read from the open Archicad and offered
as a list, because the project already knows and a person mistyping it is the
commonest way a run measures the wrong thing.

Everything that is a real decision, and everything the reference project needed
that another will not, sits behind Advanced, closed.

Every field says what it is
---------------------------
A line under each control, and a longer one on hover. Not decoration: most of
these settings fail *quietly*. A facade study with the slab layers missing does
not report an error, it reports the least-lit half of the building; an export
without the zone layers runs for minutes and then cannot place the skin. The
hint says what the setting is; the tooltip says what happens when it is wrong,
because that is the part nobody can infer from a label.
"""

from __future__ import annotations

import queue
import tkinter as tk
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from tkinter import scrolledtext, ttk

from sun_study import AUTHOR, PRODUCT, __version__
from sun_study.app import probe
from sun_study.app.runner import Run
from sun_study.archicad import naming
from sun_study.archicad.connection import DEFAULT_TIMEOUT_SECONDS
from sun_study.disclaimer import STATUS

PAD = 8
HINT = "#5a5a5a"

#: Layer name fragments that mark the envelope a facade study measures. A
#: guess, offered rather than applied: the picker is filled with them and the
#: list stays editable, because the next project names its slabs differently.
SKIN_WORDS = ("Wall.External", "Floor.", "Balustrade", "Screens")


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


def matching(names: Sequence[str], query: str) -> list[str]:
    """Layers matching a search box, in the order the project lists them.

    Every word has to appear, in any order and anywhere in the name, so
    "floor str" finds ``01 | Floor.Structural`` without anybody having to
    remember whether the group number or the dot comes first. Case is ignored
    because layer naming is nobody's memory test.
    """
    words = query.casefold().split()
    if not words:
        return list(names)
    return [name for name in names if all(word in name.casefold() for word in words)]


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

        canvas = tk.Canvas(outer, highlightthickness=0)
        canvas.grid(row=2, column=0, sticky="nsew", pady=6)
        bar = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        bar.grid(row=2, column=1, sticky="ns", pady=6)
        canvas.configure(yscrollcommand=bar.set)
        self._list = ttk.Frame(canvas)
        self._window_id = canvas.create_window((0, 0), window=self._list, anchor="nw")
        self._canvas = canvas
        self._list.bind("<Configure>", lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(self._window_id, width=e.width))
        canvas.bind_all("<MouseWheel>", self._wheel)

        self.count = ttk.Label(outer, text="", foreground=HINT)
        self.count.grid(row=3, column=0, sticky="w")

        buttons = ttk.Frame(outer)
        buttons.grid(row=4, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        buttons.columnconfigure(0, weight=1)
        ttk.Button(buttons, text="Clear all", command=self._clear).grid(row=0, column=0, sticky="w")
        ttk.Button(buttons, text="Cancel", command=self.destroy).grid(row=0, column=1, padx=6)
        ttk.Button(buttons, text="Use these", command=self._accept).grid(row=0, column=2)

        self._repaint()
        self.query.focus_set()
        self.bind("<Escape>", lambda _e: self.destroy())
        self.grab_set()

    def _wheel(self, event: tk.Event[tk.Misc]) -> None:
        self._canvas.yview_scroll(-1 * (event.delta // 120), "units")

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

    def _accept(self) -> None:
        # Back in the project's own order, not tick order: a list a person can
        # scan against the layer palette is worth more than one recording the
        # sequence somebody happened to click in.
        self.result = [name for name in self._available if name in self._ticked]
        self.destroy()


@dataclass
class Job:
    """One command to run, and what to call it while it runs."""

    label: str
    args: list[str] = field(default_factory=list)


class Window:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("Sun Study")
        self.root.minsize(820, 700)

        #: Lines from the worker thread. Tkinter is not thread-safe, so nothing
        #: touches a widget from the runner's thread: lines go through here and
        #: the UI thread drains them on a timer.
        self.incoming: queue.Queue[tuple[str, object]] = queue.Queue()

        self.options = probe.ProjectOptions()
        self.ports: list[int] = []
        self.run: Run | None = None
        self.queued: list[Job] = []

        self._build()
        self.refresh()
        self.root.after(80, self._drain)

    # -- layout ------------------------------------------------------------
    def _build(self) -> None:
        frame = ttk.Frame(self.root, padding=PAD)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(1, weight=1)
        row = 0

        # Which Archicad. Listed, never assumed: each instance gets its own
        # port, so the default is right only for whichever started first, and
        # two projects open is the ordinary case in an office.
        label = ttk.Label(frame, text="Archicad")
        label.grid(row=row, column=0, sticky="w")
        picker = ttk.Frame(frame)
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
            "below.",
        )
        row += 1
        self._hint(
            frame,
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

        self.status = ttk.Label(frame, text="", foreground=HINT, wraplength=620, justify="left")
        self.status.grid(row=row, column=1, sticky="w", pady=(0, PAD))
        row += 1

        ttk.Label(frame, text="Study").grid(row=row, column=0, sticky="nw")
        studies = ttk.Frame(frame)
        studies.grid(row=row, column=1, sticky="w")
        self.do_facade = tk.BooleanVar(value=True)
        self.do_floors = tk.BooleanVar(value=True)
        self.do_plans = tk.BooleanVar(value=False)

        facade_box = ttk.Checkbutton(
            studies, text="Facade skin in 3D", variable=self.do_facade, command=self._sync
        )
        facade_box.grid(row=0, column=0, sticky="w")
        Tooltip(
            facade_box,
            "Paints the outside of the building with one colour per band of "
            "direct sun hours on 21 June, as real 3D elements on the tool's own "
            "layer. Switch that layer off to hide the result; nothing else in "
            "the model is touched. This measures surface area, not apartments, "
            "so it answers a massing question rather than an ADG one.",
        )
        self.floors_box = ttk.Checkbutton(
            studies,
            text="including floors, balcony decks and soffits",
            variable=self.do_floors,
        )
        self.floors_box.grid(row=1, column=0, sticky="w", padx=(18, 0))
        Tooltip(
            self.floors_box,
            "Horizontal surfaces take far more sun than any wall. Leaving them "
            "out is not a smaller study — it is a study of the least-lit half "
            "of the building, and nothing in the result says so. Needs the slab "
            "layers named under Advanced → Facade layers.",
        )
        plans_box = ttk.Checkbutton(
            studies, text="Apartment plans and sheets", variable=self.do_plans
        )
        plans_box.grid(row=2, column=0, sticky="w")
        Tooltip(
            plans_box,
            "The ADG assessment proper: hours of direct sun per apartment, the "
            "sun patch drawn on each floor plan, and a sheet per time of day. "
            "Needs Zones and windows in the model, so it is off by default — "
            "the facade study works on a massing that has neither.",
        )
        row += 1
        self._hint(frame, row, "What to run. Both together run one after the other.")
        row += 1

        ttk.Separator(frame).grid(row=row, column=0, columnspan=2, sticky="ew", pady=PAD)
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
        self.exclude, row = self._entry(
            frame,
            row,
            "Ignore above (m)",
            "100",
            "Drops anything sitting entirely above this height.",
            "Hotlinked unit-type masters are parked high above the real "
            "building — 157 to 281 m on this project — on the same layers as "
            "the building itself, so height is the only thing that separates "
            "them. Left in, they join the area being measured and quietly "
            "change every percentage. Clear the box to keep everything.",
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

        # Advanced, closed. Everything in it has a defensible default and is
        # the kind of thing one project needs and the next does not.
        self.advanced_open = tk.BooleanVar(value=False)
        self.advanced_button = ttk.Button(
            frame, text="▸  Advanced", command=self._toggle_advanced, width=16
        )
        self.advanced_button.grid(row=row, column=0, columnspan=2, sticky="w", pady=(PAD, 2))
        Tooltip(
            self.advanced_button,
            "Settings with a sensible default that one project needs and the "
            "next does not. Worth opening the first time a new project is set "
            "up, and worth leaving alone after that.",
        )
        row += 1

        self.advanced = ttk.Frame(frame)
        self.advanced.grid(row=row, column=0, columnspan=2, sticky="ew")
        self.advanced.columnconfigure(1, weight=1)
        self.advanced.grid_remove()
        inner = 0
        self.combination, inner = self._combo(
            self.advanced,
            inner,
            "Export combination",
            "The office layer combination the IFC export starts from.",
            "The translator exports what is shown, so the layer state is an "
            "input to every number below. The run sets it from this "
            "combination, forces on what the study needs over the top, and "
            "puts every layer back afterwards — so the answer does not depend "
            "on what happened to be on screen.",
        )
        self.subject, inner = self._picker(
            self.advanced,
            inner,
            "Facade layers",
            "The layers that are the building being measured. Comma separated.",
            "Everything else in the model still casts shade but is not counted "
            "in the area. Without this, the facade area on a developed model "
            "includes every internal partition and balustrade. The slab layers "
            "belong here too, or there are no floors to colour.",
        )
        self.require, inner = self._picker(
            self.advanced,
            inner,
            "Also export",
            "Layers forced into the export whatever the combination says.",
            "The zone layers must be in the export or the 3D skin cannot be "
            "placed on the building — the study needs a Zone to line the IFC "
            "up against, and neither of this project's IFC combinations shows "
            "them. Left empty, the zone layers are added automatically.",
        )
        self.hide, inner = self._picker(
            self.advanced,
            inner,
            "Keep off drawings",
            "Layers switched off on the study drawings and in the export.",
            "Grids and dimension layers usually: they are the practice's own "
            "annotation and clutter a sun study without adding to it. What "
            "counts as clutter is a decision about the drawing, so it is named "
            "here rather than guessed from layer names.",
        )
        self.livable, inner = self._entry(
            self.advanced,
            inner,
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
        self.instants, inner = self._entry(
            self.advanced,
            inner,
            "Plan times",
            "09:00, 12:00, 15:00",
            "Times of day to draw a sun patch for. Comma separated.",
            "One floor-plan sheet per time. Nine, twelve and three are the "
            "conventional set. Each one adds a set of views and a layout, so a "
            "long list makes a long run.",
        )
        self.shadow_subset, inner = self._combo(
            self.advanced,
            inner,
            "Times filed in",
            "Layout Book subset the clock-time sheets go into.",
            "So the sheets sit with the practice's own drawings of that kind "
            "instead of at the root of the book. The subset has to exist "
            "already — the run will not create one, because the Layout Book is "
            "the office's structure to organise.",
        )
        self.adg_subset, inner = self._combo(
            self.advanced,
            inner,
            "Diagrams filed in",
            "Subset for the sheets that are not a time of day.",
            "The banded plan and the two-hour plan. Same rule: the subset must "
            "exist, and a missing one is reported rather than invented, with "
            "the sheets left at the root of the book.",
        )
        self.prefix, inner = self._entry(
            self.advanced,
            inner,
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
        self.wait_min, inner = self._entry(
            self.advanced,
            inner,
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
        self.grid_m, inner = self._entry(
            self.advanced,
            inner,
            "Skin cell (m)",
            "0.5",
            "Cell size of the 3D facade skin.",
            "Finer looks better and makes many more elements — half the cell "
            "size is roughly four times the count, and this project already "
            "makes over five thousand at 0.5 m. A face narrower than one cell "
            "is not drawn at all, so a coarse setting loses thin columns.",
        )
        row += 1

        buttons = ttk.Frame(frame)
        buttons.grid(row=row, column=0, columnspan=2, sticky="ew", pady=(PAD, 4))
        buttons.columnconfigure(0, weight=1)
        self.go = ttk.Button(buttons, text="Run study", command=self._start)
        self.go.grid(row=0, column=0, sticky="ew")
        Tooltip(
            self.go,
            "Runs the study in the project chosen above. Minutes rather than "
            "seconds: the export alone takes a couple. Nothing is saved — look "
            "at the result in Archicad and save it yourself if you want to "
            "keep it.",
        )
        self.cancel = ttk.Button(buttons, text="Stop", command=self._stop, state="disabled")
        self.cancel.grid(row=0, column=1, padx=(6, 0))
        Tooltip(
            self.cancel,
            "Asks the run to stop and lets it put the project's layer state "
            "back on the way out. It can take a few seconds to come to a halt.",
        )
        row += 1

        self.progress = ttk.Progressbar(frame, mode="determinate", maximum=100)
        self.progress.grid(row=row, column=0, columnspan=2, sticky="ew")
        row += 1

        frame.rowconfigure(row, weight=1)
        self.log = scrolledtext.ScrolledText(
            frame, height=11, wrap="word", state="disabled", font=("Consolas", 9)
        )
        self.log.grid(row=row, column=0, columnspan=2, sticky="nsew", pady=(6, 0))
        row += 1

        ttk.Label(frame, text=STATUS, foreground="#a33", wraplength=760).grid(
            row=row, column=0, columnspan=2, sticky="w", pady=(6, 0)
        )
        row += 1
        ttk.Label(
            frame,
            text=f"{PRODUCT} {__version__}  ·  created by {AUTHOR}",
            foreground=HINT,
        ).grid(row=row, column=0, columnspan=2, sticky="w", pady=(2, 0))
        self._sync()

    def _hint(self, parent: ttk.Frame, row: int, text: str) -> ttk.Label:
        """The line under a control. Returned so it can be rewritten: the
        useful thing to say under a zone field is what the project turned out
        to hold, which is not known until it has been read."""
        made = ttk.Label(parent, text=text, foreground=HINT, wraplength=560)
        made.grid(row=row, column=1, sticky="w", pady=(0, 4))
        return made

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

    def _toggle_advanced(self) -> None:
        opening = not self.advanced_open.get()
        self.advanced_open.set(opening)
        self.advanced_button.config(text="▾  Advanced" if opening else "▸  Advanced")
        if opening:
            self.advanced.grid()
        else:
            self.advanced.grid_remove()

    def _sync(self) -> None:
        self.floors_box.config(state="normal" if self.do_facade.get() else "disabled")

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
        if not self.subject.get():
            skin = [name for name in found.layers if any(w in name for w in SKIN_WORDS)]
            self.subject.insert(0, ", ".join(skin))

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
            fits = [kind for kind in kinds if getattr(kind, wanted)]
            if not self._listed(names_box):
                names_box.delete(0, "end")
                names_box.insert(0, ", ".join(kind.label for kind in fits))
            taken = f" — taken as {what}: {_some(k.label for k in fits)}" if fits else ""
            line.config(text=f"carries {_some(kind.described() for kind in kinds)}{taken}")

    def _zone_defaults(self) -> list[str]:
        """Zone layers worth forcing into the export, narrowest first.

        A project's zone layers are the ones named for zones. Everything else
        that happens to carry a Zone -- annotation, area calculations -- is
        somebody's schedule, not the building.
        """
        named = [name for name in self.options.zone_layers if "Zone." in name]
        return named or list(self.options.zone_layers)

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
            made.append(Job("facade skin", args))

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
            made.append(Job("apartment plans and sheets", args))

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


def launch() -> None:
    root = tk.Tk()
    try:
        ttk.Style().theme_use("vista")
    except tk.TclError:
        pass
    Window(root)
    root.mainloop()
