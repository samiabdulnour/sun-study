"""Transport for Archicad's JSON API and the Tapir add-on.

Everything in this package is written against the protocol as it actually is,
read out of the Tapir source and its published command definitions, not from
memory. ``docs/archicad.md`` records what was verified, against which version,
and where each fact came from.

**None of this package is covered by CI**, because there is no Archicad to run
against. That is the whole reason ``core`` is kept pure: the parts that can be
tested are tested exhaustively, and the part that cannot is kept thin, boring
and free of any analysis logic. The tests here exercise the request and
response handling through a fake transport; the real thing needs a human at a
workstation, following the checklist in ``docs/archicad.md``.

The protocol
------------
Archicad 24+ listens on ``http://127.0.0.1:19723`` and takes::

    {"command": "API.<Name>", "parameters": {...}}

Tapir's commands are reached through one official command::

    {"command": "API.ExecuteAddOnCommand",
     "parameters": {"addOnCommandId": {"commandNamespace": "TapirCommand",
                                       "commandName": "<Name>"},
                    "addOnCommandParameters": {...}}}

**Errors arrive at two levels, and both must be checked.** The outer response
carries ``succeeded`` and may carry ``error``; the inner Tapir response, under
``result.addOnCommandResponse``, may carry its own ``error`` while the outer
call reports success. Tapir's own reference client prints both and returns
``None``, so a caller that does not inspect the return value sees a silent
failure. This module raises instead.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Protocol

__all__ = [
    "PORT_RANGE",
    "ArchicadConnection",
    "ArchicadError",
    "ArchicadNotRunningError",
    "CommandFailedError",
    "HttpTransport",
    "Instance",
    "TapirUnavailableError",
    "Transport",
    "find_instances",
    "where_archicad_actually_is",
]

DEFAULT_HOST = "http://127.0.0.1"
DEFAULT_PORT = 19723
TAPIR_NAMESPACE = "TapirCommand"

#: Every port Archicad can put its JSON API on. Each running instance claims
#: one, in order, so the second Archicad open on a machine is on 19724 and a
#: tool hard-wired to 19723 talks to the wrong project -- or, once the first
#: instance is closed, to nothing at all. Range taken from Tapir's own client
#: (``sandbox/python-package/src/tapir_py/core.py``: ``range(19723, 19743)``).
PORT_RANGE = range(DEFAULT_PORT, 19743)

#: Tapir add-on version this package was written against, from the repository
#: at the time (``archicad-addon/Sources/AddOnVersion.hpp``).
VERIFIED_AGAINST_TAPIR_VERSION = "1.5.7"

#: The newest "since" version among the commands this package uses.
#: ``GetElementsByIFCIds`` arrived in 1.5.1 and is the binding constraint.
MINIMUM_TAPIR_VERSION = (1, 5, 1)


class ArchicadError(Exception):
    """Base for every failure talking to Archicad."""


class ArchicadNotRunningError(ArchicadError):
    """Nothing is listening on the JSON API port."""


class TapirUnavailableError(ArchicadError):
    """Archicad answered but the Tapir add-on is missing or too old."""


class CommandFailedError(ArchicadError):
    """Archicad accepted the request and reported a failure."""


class Transport(Protocol):
    """Anything that can carry one JSON request and return the response.

    Exists so the request and response handling can be tested without an
    Archicad, which is the only part of this package a machine can check.
    """

    def send(self, payload: dict[str, Any]) -> dict[str, Any]: ...


@dataclass(frozen=True)
class HttpTransport:
    """The real transport: an HTTP POST to Archicad's JSON API port."""

    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    timeout_seconds: float = 300.0
    """Generous on purpose. An IFC export of a large model is not quick, and a
    timeout mid-export leaves a half-written file that looks like a real one."""

    @property
    def url(self) -> str:
        return f"{self.host}:{self.port}"

    def send(self, payload: dict[str, Any]) -> dict[str, Any]:
        request = urllib.request.Request(
            self.url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                body = response.read()
        except urllib.error.URLError as exc:
            # Deliberately does not scan for other instances here. Building an
            # error message must not do network I/O: this path is hit on every
            # failed call, and a twenty-port scan per failure turns one dead
            # connection into a very slow one. The CLI scans once, when it has
            # decided to tell a human. See `where_archicad_actually_is`.
            raise ArchicadNotRunningError(
                f"Could not reach Archicad's JSON API at {self.url} ({exc.reason}). "
                f"Check that Archicad is running with a project open, and that the "
                f"JSON interface is enabled in Options > Work Environment > "
                f"Model Compare and JSON Interface."
            ) from exc
        except TimeoutError as exc:
            raise ArchicadError(
                f"Archicad did not answer within {self.timeout_seconds:g}s. A large "
                f"IFC export can legitimately take longer; raise the timeout rather "
                f"than assuming the export failed."
            ) from exc

        try:
            decoded: dict[str, Any] = json.loads(body)
        except json.JSONDecodeError as exc:
            raise ArchicadError(
                f"Archicad returned something that is not JSON: {body[:200]!r}"
            ) from exc
        return decoded


@dataclass(frozen=True)
class Instance:
    """One running Archicad, found by scanning the port range."""

    port: int
    project: str = ""
    """The open project's name, blank if it could not be read."""

    def describe(self) -> str:
        return f"port {self.port}" + (f" -- {self.project}" if self.project else "")


def find_instances(
    host: str = DEFAULT_HOST,
    ports: Iterable[int] = PORT_RANGE,
    *,
    timeout_seconds: float = 0.4,
) -> tuple[Instance, ...]:
    """Every Archicad answering on the JSON API, with the project each has open.

    Archicad gives each running instance its own port in order, so a second
    project opened alongside the first is on 19724 and the default port either
    reaches the wrong project or, once the first is closed, nothing at all.
    That failure reads as "Archicad is not running" and sends people to check
    a setting that was never off, so the answer is to go and look.

    Deliberately impatient. This runs on a path that has already failed, and
    twenty ports at the normal timeout would be a very long wait to be told
    something simple.
    """
    found: list[Instance] = []
    for port in ports:
        probe = HttpTransport(host=host, port=port, timeout_seconds=timeout_seconds)
        try:
            alive = probe.send({"command": "API.IsAlive", "parameters": {}})
        except ArchicadError:
            continue
        if not alive.get("succeeded"):
            continue
        found.append(Instance(port=port, project=_project_name(probe)))
    return tuple(found)


def _project_name(probe: HttpTransport) -> str:
    """The open project's name, or blank.

    Best effort: an instance with no project open still answers ``IsAlive``,
    and knowing a port is live is most of the value even without a name.
    """
    try:
        response = probe.send(
            {
                "command": "API.ExecuteAddOnCommand",
                "parameters": {
                    "addOnCommandId": {
                        "commandNamespace": TAPIR_NAMESPACE,
                        "commandName": "GetProjectInfo",
                    },
                    "addOnCommandParameters": {},
                },
            }
        )
    except ArchicadError:
        return ""
    inner = (response.get("result") or {}).get("addOnCommandResponse") or {}
    if not isinstance(inner, dict):
        return ""
    name = inner.get("projectName") or inner.get("projectPath") or ""
    return str(name) if isinstance(name, str) else ""


def where_archicad_actually_is(
    tried: int, host: str = DEFAULT_HOST, ports: Iterable[int] = PORT_RANGE
) -> str:
    """Where Archicad *is* listening, if anywhere, or ``""`` if that adds nothing.

    Called once by the CLI after a connection has already failed, never from
    the transport. The default message sends people to the Work Environment
    setting, which is the wrong place whenever the real cause is a second
    Archicad holding the port -- so this goes and looks before letting that
    advice stand.
    """
    others = [instance for instance in find_instances(host, ports) if instance.port != tried]
    if not others:
        return ""
    listed = "; ".join(instance.describe() for instance in others)
    return (
        f"Archicad IS answering, on another port: {listed}. Each running instance "
        f"gets its own port, so a second project opened alongside the first does not "
        f"take {PORT_RANGE.start}. Re-run with --port {others[0].port}, or close the "
        f"other instances and reopen this project."
    )


class ArchicadConnection:
    """A connection to Archicad, with Tapir's commands reachable through it."""

    def __init__(self, transport: Transport | None = None) -> None:
        self._transport = transport if transport is not None else HttpTransport()
        self._tapir_version: str | None = None

    # -- raw protocol ----------------------------------------------------
    def run_official(self, command: str, parameters: dict[str, Any] | None = None) -> Any:
        """Run one of Archicad's own ``API.*`` commands."""
        response = self._transport.send({"command": command, "parameters": parameters or {}})

        if not response.get("succeeded", False):
            error = response.get("error") or {}
            raise CommandFailedError(
                f"{command} failed: {error.get('message', 'no message')} "
                f"(code {error.get('code', 'none')})"
            )
        return response.get("result")

    def run_tapir(self, command: str, parameters: dict[str, Any] | None = None) -> Any:
        """Run one of Tapir's commands.

        Checks both error levels. The inner one is the dangerous one: the
        official call reports success while the add-on response carries a
        failure, so a client that only looks at ``succeeded`` reads an error
        object as though it were data.
        """
        result = self.run_official(
            "API.ExecuteAddOnCommand",
            {
                "addOnCommandId": {
                    "commandNamespace": TAPIR_NAMESPACE,
                    "commandName": command,
                },
                "addOnCommandParameters": parameters or {},
            },
        )

        if not isinstance(result, dict) or "addOnCommandResponse" not in result:
            raise TapirUnavailableError(
                f"Archicad ran API.ExecuteAddOnCommand for {command!r} but returned no "
                f"add-on response. The Tapir add-on is probably not installed. Install "
                f"the AC26 build from "
                f"https://github.com/ENZYME-APD/tapir-archicad-automation/releases"
            )

        inner = result["addOnCommandResponse"]
        if isinstance(inner, dict) and "error" in inner:
            error = inner["error"] or {}
            raise CommandFailedError(
                f"Tapir command {command} failed: {error.get('message', 'no message')} "
                f"(code {error.get('code', 'none')})"
            )
        return inner

    # -- handshake -------------------------------------------------------
    @property
    def tapir_version(self) -> str:
        if self._tapir_version is None:
            response = self.run_tapir("GetAddOnVersion")
            self._tapir_version = str(response.get("version", ""))
        return self._tapir_version

    def require_tapir(self) -> str:
        """Confirm Tapir is present and new enough, or say exactly what is wrong.

        Called before anything else so a version problem surfaces as a version
        problem, rather than as an unexplained failure on whichever command
        happens to be the first one that needs a newer add-on.
        """
        return self.require_tapir_at_least(
            MINIMUM_TAPIR_VERSION,
            "GetElementsByIFCIds, which maps IFC GlobalIds onto Archicad elements,",
        )

    def require_tapir_at_least(self, minimum: tuple[int, ...], because: str) -> str:
        """Confirm the add-on is new enough for one particular feature.

        Separate from ``require_tapir`` so an optional capability can need a
        newer build without locking everyone else out. Drawing needs 1.5.7 for
        ``CreateHatches``; reading and writing numbers need only 1.5.1, and
        someone who wants the numbers should not be blocked by a picture they
        did not ask for.

        ``because`` names the command that sets the floor, so the message says
        what the update actually buys.
        """
        version = self.tapir_version
        try:
            parts = tuple(int(p) for p in version.split(".")[:3])
        except ValueError as exc:
            raise TapirUnavailableError(
                f"Could not read the Tapir add-on version from {version!r}."
            ) from exc

        if parts < minimum:
            needed = ".".join(str(p) for p in minimum)
            raise TapirUnavailableError(
                f"Tapir {version} is installed but this needs at least {needed}. "
                f"{because} arrived in {needed}. Update the add-on from "
                f"https://github.com/ENZYME-APD/tapir-archicad-automation/releases"
            )
        return version

    def describe(self) -> str:
        """One line for the console banner, so the human can see what it found."""
        version = self.require_tapir()
        note = (
            ""
            if version == VERIFIED_AGAINST_TAPIR_VERSION
            else f" (this tool was written against {VERIFIED_AGAINST_TAPIR_VERSION})"
        )
        return f"Archicad JSON API reachable, Tapir add-on {version}{note}"
