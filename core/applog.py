"""Conversion logging.

Purpose: make every conversion reconstructable AFTER the fact, so a problem can
be diagnosed from the log alone rather than by asking the user to re-paste SQL.

Two tiers:

  logs/MsSqlToPg.log      one compact block per conversion -- version, sizes,
                          timing, syntax verdict, and EVERY warning with its
                          line number. Rotates at 5 MB, keeps 3 files.

  logs/runs/<stamp>-*.sql the full input and output SQL for that run. This is
                          what makes after-the-fact diagnosis actually possible
                          -- warning codes alone rarely explain a failure.
                          Only the most recent KEEP_RUNS runs are retained, so
                          the folder cannot grow without bound.

Logging must NEVER break a conversion: every write is best-effort.
"""

from __future__ import annotations

import datetime as _dt
import logging
import logging.handlers
import os
import sys
from pathlib import Path

from core.models import ConversionResult, Severity
from core.version import VERSION, banner

LOG_NAME = "MsSqlToPg.log"
MAX_BYTES = 5 * 1024 * 1024
BACKUP_COUNT = 3
KEEP_RUNS = 20            # per-run SQL dumps retained
MAX_DUMP_CHARS = 500_000  # refuse to dump absurdly large inputs

_logger: logging.Logger | None = None
_log_dir: Path | None = None


def _candidate_dirs() -> list[Path]:
    """Beside the executable first -- that is where a user looks. Fall back to
    LOCALAPPDATA when the install location is read-only (Program Files)."""
    exe_dir = Path(sys.executable).parent if getattr(sys, "frozen", False) \
        else Path(__file__).resolve().parent.parent
    local = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "MsSqlToPg"
    return [exe_dir / "logs", local / "logs"]


def log_dir() -> Path | None:
    return _log_dir


def configure() -> Path | None:
    """Set up logging. Returns the log directory, or None if none is writable."""
    global _logger, _log_dir
    if _logger is not None:
        return _log_dir

    for candidate in _candidate_dirs():
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            probe = candidate / ".write-test"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
        except OSError:
            continue

        logger = logging.getLogger("mssqltopg")
        logger.setLevel(logging.INFO)
        logger.propagate = False
        handler = logging.handlers.RotatingFileHandler(
            candidate / LOG_NAME, maxBytes=MAX_BYTES,
            backupCount=BACKUP_COUNT, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)

        _logger, _log_dir = logger, candidate
        logger.info(f"\n{'=' * 78}\nSTARTED {_stamp()}  {banner()}\n{'=' * 78}")
        return candidate

    return None


def _stamp() -> str:
    return _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _file_stamp() -> str:
    return _dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:-3]


def log_conversion(source_sql: str, result: ConversionResult,
                   source_name: str = "(pasted)") -> None:
    """Record one conversion. Best-effort: never raises."""
    try:
        if _logger is None and configure() is None:
            return
        assert _logger is not None

        syntax = result.validation.verdict if result.validation else "NOT CHECKED"
        verdict = "USABLE" if result.is_usable else "NEEDS REVIEW"
        errors = sum(1 for w in result.warnings if w.severity is Severity.ERROR)
        warns = sum(1 for w in result.warnings if w.severity is Severity.WARNING)

        lines = [
            "",
            "-" * 78,
            f"{_stamp()}  v{VERSION}  {verdict}",
            f"  source   : {source_name}",
            f"  size     : {len(source_sql):,} in -> {len(result.sql):,} out",
            f"  duration : {result.duration_ms:,} ms",
            f"  syntax   : {syntax}",
            f"  findings : {errors} error(s), {warns} warning(s)",
        ]
        if result.warnings:
            lines.append("  warnings :")
            for w in result.sorted_warnings:
                where = f" (line {w.line})" if w.line else ""
                lines.append(f"    [{w.severity.value.upper():7}] {w.code}: "
                             f"{w.message}{where}")

        dump = _dump_run(source_sql, result)
        if dump:
            lines.append(f"  sql saved: {dump}")

        _logger.info("\n".join(lines))
    except Exception:
        pass  # logging must never break a conversion


def _dump_run(source_sql: str, result: ConversionResult) -> str | None:
    """Write the full input/output SQL for this run, then prune old runs."""
    if _log_dir is None:
        return None
    if len(source_sql) > MAX_DUMP_CHARS:
        return None
    try:
        runs = _log_dir / "runs"
        runs.mkdir(parents=True, exist_ok=True)
        stamp = _file_stamp()
        (runs / f"{stamp}-input.sql").write_text(source_sql, encoding="utf-8")
        (runs / f"{stamp}-output.sql").write_text(result.sql, encoding="utf-8")
        _prune(runs)
        return str(runs / f"{stamp}-*.sql")
    except OSError:
        return None


def _prune(runs: Path) -> None:
    """Keep only the most recent KEEP_RUNS pairs."""
    try:
        stamps = sorted({p.name.rsplit("-", 1)[0] for p in runs.glob("*.sql")})
        for old in stamps[:-KEEP_RUNS]:
            for path in runs.glob(f"{old}-*.sql"):
                path.unlink(missing_ok=True)
    except OSError:
        pass


def log_message(text: str) -> None:
    """Free-form note (startup, file loads, errors). Best-effort."""
    try:
        if _logger is None and configure() is None:
            return
        assert _logger is not None
        _logger.info(f"{_stamp()}  {text}")
    except Exception:
        pass
