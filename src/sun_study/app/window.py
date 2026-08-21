"""The window itself.

Shaped by one rule: a colleague should have to decide only what is genuinely a
decision. Everything that is a property of the *project* -- which layer carries
the apartments, which masters exist, which Layout Book subsets the practice
files this kind of drawing under -- is read from the open Archicad and offered
as a list, because the project already knows and a person mistyping it is the
commonest way a run measures the wrong thing.

Everything that is a real decision, and everything the reference project needed
that another will not, sits behind Advanced, closed.
"""

from __future__ import annotations

import queue
import tkinter as tk
from dataclasses import dataclass, field
from tkinter import scrolledtext, ttk

from sun_study.app import probe
from sun_study.app.runner import Run
from sun_study.disclaimer import STATUS

PAD = 8

#: Layer name fragments that mark the envelope a facade study measures. A
#: guess, offered rather than applied: the picker is filled with them and the
#: list stays editable, because the next project names its slabs differently.
SKIN_WORDS = ("Wall.External", "Floor.", "Balustrade", "Screens")


@dataclass
class Job:
    """One command to run, and what to call it while it runs."""

    label: str
    args: list[str] = field(default_factory=list)


class Window:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("Sun Study")
        self.root.minsize(760, 640)

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
        ttk.Label(frame, text="Archicad").grid(row=row, column=0, sticky="w")
        picker = ttk.Frame(frame)
        picker.grid(row=row, column=1, sticky="ew", pady=2)
        picker.columnconfigure(0, weight=1)
        self.instance = ttk.Combobox(picker, state="readonly", values=[])
        self.instance.grid(row=0, column=0, sticky="ew")
        self.instance.bind("<<ComboboxSelected>>", lambda _event: self.refresh())
        ttk.Button(picker, text="Refresh", command=self.refresh, width=9).grid(
            row=0, column=1, padx=(6, 0)
        )
        row += 1

        self.status = ttk.Label(frame, text="", foreground="#666")
        self.status.grid(row=row, column=1, sticky="w", pady=(0, PAD))
        row += 1

        ttk.Label(frame, text="Study").grid(row=row, column=0, sticky="nw")
        studies = ttk.Frame(frame)
        studies.grid(row=row, column=1, sticky="w")
        self.do_facade = tk.BooleanVar(value=True)
        self.do_floors = tk.BooleanVar(value=True)
        self.do_plans = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            studies,
            text="Facade skin in 3D",
            variable=self.do_facade,
            command=self._sync,
        ).grid(row=0, column=0, sticky="w")
        self.floors_box = ttk.Checkbutton(
            studies,
            text="including floors, balcony decks and soffits",
            variable=self.do_floors,
        )
        self.floors_box.grid(row=1, column=0, sticky="w", padx=(18, 0))
        ttk.Checkbutton(
            studies, text="Apartment plans and sheets", variable=self.do_plans
        ).grid(row=2, column=0, sticky="w")
        row += 1

        ttk.Separator(frame).grid(row=row, column=0, columnspan=2, sticky="ew", pady=PAD)
        row += 1

        self.apartments = self._combo(frame, row, "Apartment zones")
        row += 1
        self.master = self._combo(frame, row, "Sheet master")
        row += 1
        self.exclude = self._entry(frame, row, "Ignore above (m)", "100")
        row += 1
        self.year = self._entry(frame, row, "Year", "2024")
        row += 1

        # Advanced, closed. Everything in it has a defensible default and is
        # the kind of thing one project needs and the next does not.
        self.advanced_open = tk.BooleanVar(value=False)
        self.advanced_button = ttk.Button(
            frame, text="▸  Advanced", command=self._toggle_advanced, width=16
        )
        self.advanced_button.grid(row=row, column=0, columnspan=2, sticky="w", pady=(PAD, 2))
        row += 1

        self.advanced = ttk.Frame(frame)
        self.advanced.grid(row=row, column=0, columnspan=2, sticky="ew")
        self.advanced.columnconfigure(1, weight=1)
        self.advanced.grid_remove()
        self.combination = self._combo(self.advanced, 0, "Export combination")
        self.subject = self._entry(self.advanced, 1, "Facade layers", "")
        self.require = self._entry(self.advanced, 2, "Also export", "")
        self.hide = self._entry(self.advanced, 3, "Keep off drawings", "")
        self.instants = self._entry(self.advanced, 4, "Plan times", "09:00, 12:00, 15:00")
        self.shadow_subset = self._combo(self.advanced, 5, "Times filed in")
        self.adg_subset = self._combo(self.advanced, 6, "Diagrams filed in")
        self.grid_m = self._entry(self.advanced, 7, "Skin cell (m)", "0.5")
        row += 1

        buttons = ttk.Frame(frame)
        buttons.grid(row=row, column=0, columnspan=2, sticky="ew", pady=(PAD, 4))
        buttons.columnconfigure(0, weight=1)
        self.go = ttk.Button(buttons, text="Run study", command=self._start)
        self.go.grid(row=0, column=0, sticky="ew")
        self.cancel = ttk.Button(buttons, text="Stop", command=self._stop, state="disabled")
        self.cancel.grid(row=0, column=1, padx=(6, 0))
        row += 1

        self.progress = ttk.Progressbar(frame, mode="determinate", maximum=100)
        self.progress.grid(row=row, column=0, columnspan=2, sticky="ew")
        row += 1

        frame.rowconfigure(row, weight=1)
        self.log = scrolledtext.ScrolledText(
            frame, height=14, wrap="word", state="disabled", font=("Consolas", 9)
        )
        self.log.grid(row=row, column=0, columnspan=2, sticky="nsew", pady=(6, 0))
        row += 1

        ttk.Label(frame, text=STATUS, foreground="#a33", wraplength=700).grid(
            row=row, column=0, columnspan=2, sticky="w", pady=(6, 0)
        )
        self._sync()

    def _combo(self, parent: ttk.Frame, row: int, label: str) -> ttk.Combobox:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=2)
        box = ttk.Combobox(parent, values=[])
        box.grid(row=row, column=1, sticky="ew", pady=2)
        return box

    def _entry(self, parent: ttk.Frame, row: int, label: str, initial: str) -> ttk.Entry:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=2)
        box = ttk.Entry(parent)
        box.insert(0, initial)
        box.grid(row=row, column=1, sticky="ew", pady=2)
        return box

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
        if not found.reachable:
            self.status.config(text="; ".join(found.problems) or "could not read the project")
            return
        self.status.config(
            text=(
                f"{len(found.layers)} layers · {len(found.zone_layers)} carry zones · "
                f"{len(found.masters)} masters · {len(found.subsets)} subsets"
            )
        )
        self._fill(self.apartments, found.zone_layers, ("Zone.Unit", "Zone."))
        # "VERTICAL - No Scale" before "COVER/NO SCALE": both say no scale
        # and one of them is a cover sheet. A diagram shrunk to fit the page
        # is at no stated scale, which is why a no-scale title block is right.
        self._fill(
            self.master,
            found.masters,
            ("VERTICAL - No Scale", "VERTICAL NO SCALE", "No Scale", "A1"),
        )
        self._fill(self.combination, found.combinations, ("IFC ARCH", "IFC"))
        self._fill(self.shadow_subset, found.subsets, ("SHADOW",))
        self._fill(self.adg_subset, found.subsets, ("ADG",))
        if not self.subject.get():
            skin = [
                name for name in found.layers if any(word in name for word in SKIN_WORDS)
            ]
            self.subject.insert(0, ", ".join(skin))

    def _fill(
        self, box: ttk.Combobox, values: tuple[str, ...], prefer: tuple[str, ...]
    ) -> None:
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

    def jobs(self) -> list[Job]:
        """The command lines the ticked boxes mean. Public, and pure, because
        this is the part worth testing without a window on screen."""
        port = str(self.ports[max(self.instance.current(), 0)]) if self.ports else ""
        common = ["--port", port]
        made: list[Job] = []

        if self.do_facade.get():
            args = ["massing", "--timezone", "Australia/Sydney", *common]
            for name in self._listed(self.subject):
                args += ["--subject-layer", name]
            # Without an IfcSpace there is nothing to fit the skin onto the
            # building against, and no IFC combination shows the zone layers.
            # The zone-named ones only: plenty of annotation layers carry a
            # Zone too, and forcing those into the export adds nothing to
            # measure and a good deal to export.
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
