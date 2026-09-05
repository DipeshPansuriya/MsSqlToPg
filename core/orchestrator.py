"""Conversion dispatch: run the engine, then validate its output.

Single engine by design. The hosted ChartDB LLM engine was removed on
2026-07-31 because it silently truncated large queries (31 of 60 CTEs returned
on a 15 KB input, HTTP 200, no error) and sent query text to a third party.
SQLGlot is local, deterministic, has no output-size ceiling, and its output is
now checked against the real PostgreSQL grammar.

The app makes NO network calls. Keep it that way.

Knows nothing about Tkinter -- the UI supplies a callback and does its own
thread marshalling.
"""

from __future__ import annotations

import threading
from typing import Callable

from core import validator
from core.models import ConversionResult, Severity
from engines import sqlglot_engine

ResultCallback = Callable[[ConversionResult], None]


def convert(sql: str) -> ConversionResult:
    """Convert T-SQL to PostgreSQL and validate the result. Never raises."""
    try:
        result = sqlglot_engine.convert(sql)
    except Exception as exc:  # an engine must never take down the app
        return ConversionResult().fail(
            "ENGINE-CRASH", f"{type(exc).__name__}: {exc}")

    if result.ok and result.sql.strip():
        # Checked against the REAL PostgreSQL grammar (libpg_query), not a
        # heuristic. An INVALID verdict makes the result unusable.
        result.validation = validator.validate(result.sql)
        for message in result.validation.errors:
            severity = (Severity.ERROR if not result.validation.valid
                        else Severity.INFO)
            result.add("PG-SYNTAX", severity, message)
    return result


def convert_async(sql: str, on_result: ResultCallback,
                  on_done: Callable[[], None] | None = None) -> None:
    """Convert on a worker thread.

    `on_result` is invoked from that thread. Callers touching UI widgets must
    marshal back to the UI thread themselves.
    """
    def _worker() -> None:
        try:
            on_result(convert(sql))
        finally:
            if on_done is not None:
                on_done()

    threading.Thread(target=_worker, daemon=True).start()
