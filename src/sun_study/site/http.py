"""Resilient HTTP for the free public services the site analysis reads.

Every upstream here -- NSW Spatial Services, ePlanning, SIX Maps, Valhalla and
the public Overpass mirrors -- sheds load without warning (503, 504) or
rate-limits (429). So everything goes through one door: retry on transient
status codes and network errors, exponential backoff with jitter, and a
ceiling so a run cannot stall forever.

Standard library only. A workstation without administrator rights cannot add
a package, and the packaged executable carries whatever it was built with;
``urllib`` is in both already.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

__all__ = [
    "OVERPASS_MIRRORS",
    "FetchError",
    "fetch",
    "get_json",
    "overpass",
    "overpass_cache_dir",
]

#: Status codes worth a second try. Anything else is an answer.
TRANSIENT = frozenset({408, 425, 429, 500, 502, 503, 504, 522, 524})

USER_AGENT = "Loriini site analysis (+https://github.com/samiabdulnour)"

Log = Callable[[str], None]


class FetchError(Exception):
    """A request that failed for good: every attempt, or a definite refusal."""


def fetch(
    url: str,
    *,
    data: bytes | None = None,
    headers: Mapping[str, str] | None = None,
    attempts: int = 5,
    timeout: float = 60.0,
    base_delay: float = 1.5,
    label: str | None = None,
) -> bytes:
    """GET (or POST, with ``data``) and return the body, retrying the transient.

    ``label`` names the service in the error rather than the whole URL, which
    for an ArcGIS query is several hundred characters of envelope.
    """
    last: Exception | None = None
    for attempt in range(attempts):
        if attempt:
            delay = min(base_delay * 2 ** (attempt - 1), 30.0) * (0.7 + random.random() * 0.6)
            time.sleep(delay)
        request = urllib.request.Request(url, data=data, headers={"User-Agent": USER_AGENT})
        for name, value in (headers or {}).items():
            request.add_header(name, value)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return bytes(response.read())
        except urllib.error.HTTPError as error:
            if error.code not in TRANSIENT:
                raise FetchError(f"{label or url} -> HTTP {error.code}") from error
            last = error
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            last = error
    raise FetchError(f"{label or url} -> {last}") from last


def get_json(
    url: str,
    params: Mapping[str, str] | None = None,
    *,
    attempts: int = 5,
    timeout: float = 60.0,
    headers: Mapping[str, str] | None = None,
    label: str | None = None,
) -> Any:
    """GET a JSON document. An ArcGIS ``error`` object in the body is raised
    too: the service answers 200 to a bad query and puts the refusal inside."""
    full = f"{url}?{urllib.parse.urlencode(params)}" if params else url
    body = fetch(full, attempts=attempts, timeout=timeout, headers=headers, label=label or url)
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise FetchError(f"{label or url} -> not JSON ({body[:60]!r})") from error
    if isinstance(data, dict) and data.get("error"):
        raise FetchError(f"{label or url} -> {json.dumps(data['error'])[:300]}")
    return data


# -- Overpass ----------------------------------------------------------------

OVERPASS_MIRRORS: tuple[str, ...] = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://lz4.overpass-api.de/api/interpreter",
    "https://z.overpass-api.de/api/interpreter",
    "https://overpass.osm.jp/api/interpreter",
)


def overpass_cache_dir() -> Path:
    """Where Overpass answers are kept between runs.

    Under the local application data folder rather than beside the project:
    the public Overpass network is unavailable often enough that a month-old
    answer for the same query is far better than none, and it is not part of
    any one project's output.
    """
    local = os.environ.get("LOCALAPPDATA")
    root = Path(local) if local else Path.home() / ".loriini"
    return root / "Loriini" / "cache" / "osm"


def _endpoints() -> list[str]:
    """A local Overpass first, when ``OVERPASS_LOCAL`` names one."""
    local = (os.environ.get("OVERPASS_LOCAL") or "").strip()
    return [local, *OVERPASS_MIRRORS] if local else list(OVERPASS_MIRRORS)


def overpass(
    query: str,
    *,
    rounds: int = 2,
    timeout: float = 40.0,
    cache_dir: Path | None = None,
    max_age_s: float = 30 * 24 * 3600.0,
    log: Log | None = None,
) -> dict[str, Any]:
    """Run an Overpass QL query, trying every mirror per round.

    A cached answer younger than ``max_age_s`` is returned without a request.
    When every mirror fails, a stale cached answer is returned with a note;
    only with nothing cached at all does this raise.
    """
    cache_dir = cache_dir if cache_dir is not None else overpass_cache_dir()
    digest = hashlib.sha1(query.encode("utf-8")).hexdigest()[:16]
    cache_file = cache_dir / f"{digest}.json"
    try:
        if time.time() - cache_file.stat().st_mtime < max_age_s:
            return dict(json.loads(cache_file.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError):
        pass

    last: Exception | None = None
    body = urllib.parse.urlencode({"data": query}).encode("utf-8")
    for round_number in range(rounds):
        if round_number:
            wait = min(8.0 * round_number, 16.0)
            if log:
                log(f"  OSM busy, waiting {wait:.0f}s before retry {round_number + 1}/{rounds}")
            time.sleep(wait)
        for mirror in _endpoints():
            try:
                text = fetch(mirror, data=body, attempts=1, timeout=timeout, label=mirror).decode(
                    "utf-8", errors="replace"
                )
            except FetchError as error:
                last = error
                continue
            if not text.lstrip().startswith("{"):
                last = FetchError(f"{mirror} -> non-JSON response")
                continue
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError as error:
                last = error
                continue
            try:
                cache_dir.mkdir(parents=True, exist_ok=True)
                cache_file.write_text(text, encoding="utf-8")
            except OSError:
                pass
            return dict(parsed)

    try:
        stale = json.loads(cache_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raise FetchError(f"Overpass unavailable on every mirror: {last}") from last
    if log:
        log("  OSM unavailable -- using cached data from an earlier run")
    return dict(stale)
