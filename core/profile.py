"""Project profile: the names that belong to YOUR application, not to the tool.

A few rules need to know how the target application behaves: which parameter
names it replaces as plain text, which flag columns are boolean, and what its
"to local time" SQL function is called. Those names differ from project to
project, so they are read from an optional JSON file instead of living in code.

Lookup order (first file found wins):
    1. the path in the MSSQLTOPG_PROFILE environment variable
    2. project_profile.json next to the running executable
    3. project_profile.json next to app.py (source checkout)

The file is optional. Without it every list is empty and the rules that depend
on those names simply do not fire. Copy project_profile.example.json to start.
`#`-free, strict JSON; every key is optional:

    literal_safe_parameters     names the application replaces as text even inside
                                a '...' literal: never rewritten, never reported
                                (case-insensitive, without the @)
    prefix_replaced_parameters  names replaced as raw text with no word boundary,
                                so a longer parameter starting with one breaks
                                (L031; case-sensitive, with the @)
    boolean_columns             flag columns that are boolean but are not named
                                Is... (case-insensitive)
    local_time_functions        user-defined functions that take a timestamptz and
                                a time-zone name (A025, PC01, F007, M002)
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

FILE_NAME = "project_profile.json"
ENV_VAR = "MSSQLTOPG_PROFILE"


@dataclass(frozen=True)
class Profile:
    literal_safe_parameters: frozenset[str] = frozenset()
    prefix_replaced_parameters: tuple[str, ...] = ()
    boolean_columns: frozenset[str] = frozenset()
    local_time_functions: tuple[str, ...] = ()
    source: str | None = None       # the file it came from
    problem: str | None = None      # why a file that exists could not be used


_cache: tuple[str | None, Profile] | None = None


def _candidates() -> list[Path]:
    paths: list[Path] = []
    env = os.environ.get(ENV_VAR)
    if env:
        paths.append(Path(env))
    if getattr(sys, "frozen", False):
        paths.append(Path(sys.executable).resolve().parent / FILE_NAME)
    paths.append(Path(__file__).resolve().parent.parent / FILE_NAME)
    return paths


def _names(data: dict, key: str) -> list[str]:
    value = data.get(key, [])
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ValueError(f'"{key}" must be a list of strings')
    return [v.strip() for v in value if v.strip()]


def parse(text: str, source: str | None = None) -> Profile:
    """Read profile JSON. Raises ValueError when it is not usable."""
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("the top level must be a JSON object")
    return Profile(
        literal_safe_parameters=frozenset(
            n.lstrip("@").lower() for n in _names(data, "literal_safe_parameters")),
        prefix_replaced_parameters=tuple(
            "@" + n.lstrip("@") for n in _names(data, "prefix_replaced_parameters")),
        boolean_columns=frozenset(n.lower() for n in _names(data, "boolean_columns")),
        local_time_functions=tuple(_names(data, "local_time_functions")),
        source=source,
    )


def current() -> Profile:
    """The active profile (empty when no file is present).

    Read once and kept: the rules ask per column and per literal, so looking for
    the file on every call is measurably slow. Changing the environment variable
    reloads it; after editing the file, call reset() or restart the app.
    """
    global _cache
    env = os.environ.get(ENV_VAR)
    if _cache is not None and _cache[0] == env:
        return _cache[1]
    loaded = Profile()
    for path in _candidates():
        try:
            if not path.is_file():
                continue
            loaded = parse(path.read_text(encoding="utf-8-sig"), str(path))
        except (OSError, ValueError) as exc:
            # a broken file must not stop conversions, but it must be visible
            loaded = Profile(problem=f"{path}: {exc}")
        break
    _cache = (env, loaded)
    return loaded


def is_local_time_function(name: str) -> bool:
    low = name.lower()
    return any(low == n.lower() for n in current().local_time_functions)


def describe() -> str:
    """One line for --version and the log."""
    active = current()
    if active.problem:
        return f"project profile IGNORED -- {active.problem}"
    return f"project profile: {active.source or 'none (generic defaults)'}"


def reset() -> None:
    """Forget the cached file (tests, or after the file is edited)."""
    global _cache
    _cache = None
