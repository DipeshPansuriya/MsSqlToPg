"""Logging tests.

The log exists so a problem can be diagnosed WITHOUT asking the user to
re-paste SQL. That means two hard requirements:

  1. Every warning, with its line number, must reach the log.
  2. The full input and output SQL must be recoverable for recent runs.

And one safety requirement: logging must NEVER break a conversion.
"""

import logging

import pytest

from core import applog, orchestrator
from core.models import ConversionResult, Severity
from core.version import VERSION, banner, title


@pytest.fixture
def log_dir(tmp_path, monkeypatch):
    """Point logging at a temp dir and reset module state between tests."""
    monkeypatch.setattr(applog, "_logger", None)
    monkeypatch.setattr(applog, "_log_dir", None)
    monkeypatch.setattr(applog, "_candidate_dirs", lambda: [tmp_path / "logs"])
    folder = applog.configure()
    yield folder
    for handler in logging.getLogger("mssqltopg").handlers[:]:
        handler.close()
        logging.getLogger("mssqltopg").removeHandler(handler)


def read_log(folder) -> str:
    return (folder / applog.LOG_NAME).read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# Version
# --------------------------------------------------------------------------

def test_version_is_in_the_title():
    assert VERSION in title()
    assert "PostgreSQL" in title()


def test_banner_names_the_rule_set():
    """A log excerpt must identify which rules were in force."""
    assert VERSION in banner()
    assert "L001-L014" in banner()


# --------------------------------------------------------------------------
# Log contents
# --------------------------------------------------------------------------

def test_startup_stamps_the_version(log_dir):
    assert VERSION in read_log(log_dir)


def test_conversion_is_logged_with_verdict_and_timing(log_dir):
    source = "SELECT TOP 1 a FROM t"
    result = orchestrator.convert(source)
    applog.log_conversion(source, result, "unit-test.sql")

    text = read_log(log_dir)
    assert "unit-test.sql" in text
    assert "USABLE" in text
    assert "ms" in text
    assert "VALID" in text


def test_every_warning_reaches_the_log_with_its_line(log_dir):
    """Warning codes AND line numbers -- without them the log cannot diagnose."""
    source = "SELECT a + ' - ' + b FROM t WHERE c = @@IDENTITY"
    result = orchestrator.convert(source)
    applog.log_conversion(source, result, "defects.sql")

    text = read_log(log_dir)
    assert "L001" in text
    assert "NEEDS REVIEW" in text
    assert "(line" in text


def test_auto_corrections_are_logged(log_dir):
    """The TIMESTAMP assumption must be visible in the log, not silent."""
    source = "CREATE TABLE #t (FromDateUtc timestamp);"
    result = orchestrator.convert(source)
    applog.log_conversion(source, result, "timestamp-trap.sql")
    assert "A001" in read_log(log_dir)


def test_full_sql_is_recoverable_for_the_run(log_dir):
    """The point of the run dumps: reconstruct the exact input and output."""
    source = "SELECT TOP 3 marker_column FROM some_table"
    result = orchestrator.convert(source)
    applog.log_conversion(source, result, "dump-test.sql")

    dumps = sorted((log_dir / "runs").glob("*-input.sql"))
    assert dumps, "no input dump written"
    assert "marker_column" in dumps[-1].read_text(encoding="utf-8")

    outputs = sorted((log_dir / "runs").glob("*-output.sql"))
    assert "LIMIT" in outputs[-1].read_text(encoding="utf-8").upper()


def test_run_dumps_are_pruned(log_dir, monkeypatch):
    """The folder must not grow without bound."""
    monkeypatch.setattr(applog, "KEEP_RUNS", 3)
    for i in range(6):
        result = ConversionResult(sql=f"SELECT {i};")
        applog.log_conversion(f"SELECT {i}", result, f"run{i}.sql")
    inputs = list((log_dir / "runs").glob("*-input.sql"))
    assert len(inputs) <= 3


def test_oversized_input_is_not_dumped(log_dir, monkeypatch):
    monkeypatch.setattr(applog, "MAX_DUMP_CHARS", 100)
    huge = "SELECT 1; " * 200
    applog.log_conversion(huge, ConversionResult(sql="x"), "big")
    assert not list((log_dir / "runs").glob("*-input.sql"))


def test_free_form_messages_are_logged(log_dir):
    applog.log_message("Loaded E:\\queries\\dashboard.sql (58,603 chars)")
    assert "dashboard.sql" in read_log(log_dir)


# --------------------------------------------------------------------------
# Logging must never break a conversion
# --------------------------------------------------------------------------

def test_log_conversion_never_raises(monkeypatch):
    """Even with no writable location at all."""
    monkeypatch.setattr(applog, "_logger", None)
    monkeypatch.setattr(applog, "_log_dir", None)
    monkeypatch.setattr(applog, "_candidate_dirs", lambda: [])
    applog.log_conversion("SELECT 1", ConversionResult(), "x")
    applog.log_message("still fine")


def test_configure_returns_none_when_nothing_is_writable(monkeypatch):
    monkeypatch.setattr(applog, "_logger", None)
    monkeypatch.setattr(applog, "_log_dir", None)
    monkeypatch.setattr(applog, "_candidate_dirs", lambda: [])
    assert applog.configure() is None


def test_dump_failure_does_not_break_logging(log_dir, monkeypatch):
    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(applog, "_dump_run", boom)
    result = orchestrator.convert("SELECT 1")
    applog.log_conversion("SELECT 1", result, "x.sql")   # must not raise
