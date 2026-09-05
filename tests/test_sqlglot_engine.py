"""SQLGlot engine tests.

These double as the regression guard against sqlglot upgrades: the pinned version
is 30.14.0, and the assertions below encode the behaviour verified on 2026-07-30.
Run this suite BEFORE bumping the pin.
"""

import pathlib

import pytest

from core.models import Severity
from engines import sqlglot_engine

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def codes(result):
    return {w.code for w in result.warnings}


# --------------------------------------------------------------------------
# The good case
# --------------------------------------------------------------------------

def test_complex_select_converts_cleanly():
    result = sqlglot_engine.convert(fixture("complex_select_cte_window.sql"))
    assert result.ok
    sql = result.sql.upper()
    assert "LIMIT 100" in sql          # TOP 100      -> LIMIT
    assert "COALESCE" in sql           # ISNULL       -> COALESCE
    assert "TO_CHAR" in sql            # CONVERT(120) -> TO_CHAR
    assert "POSITION" in sql           # CHARINDEX    -> POSITION
    assert "NOLOCK" not in sql         # hint correctly removed
    assert not result.has_errors


def test_cross_apply_becomes_lateral():
    result = sqlglot_engine.convert(fixture("cross_apply_string_agg.sql"))
    assert result.ok
    assert "LATERAL" in result.sql.upper()


def test_identity_ddl_converts():
    result = sqlglot_engine.convert(fixture("ddl_identity.sql"))
    assert result.ok
    assert "GENERATED" in result.sql.upper()


def test_merge_is_preserved():
    result = sqlglot_engine.convert(fixture("merge.sql"))
    assert result.ok
    assert "MERGE" in result.sql.upper()


# --------------------------------------------------------------------------
# The dangerous cases -- output exists but is lossy
# --------------------------------------------------------------------------

def test_stored_procedure_produces_output_but_flags_errors():
    """The headline failure: sqlglot silently reduces a procedure to a stub.

    Output must still be shown, but it must carry ERROR warnings.
    """
    result = sqlglot_engine.convert(fixture("stored_procedure.sql"))
    assert result.has_errors, "silent data loss must never reach the user"
    assert "SG-UNSUPPORTED" in codes(result)


def test_dynamic_sql_exec_is_flagged():
    result = sqlglot_engine.convert(fixture("dynamic_sql_exec.sql"))
    assert result.has_errors
    # Either the generator reported the drop, or the linter caught the aftermath.
    assert codes(result) & {"SG-UNSUPPORTED", "L004", "L007"}


def test_malformed_input_is_caught_by_l010():
    """sqlglot itself does NOT reject this -- 'SELEKT * FRM' parses as a
    multiplication expression. L010 is the only thing standing between the user
    and silent nonsense."""
    result = sqlglot_engine.convert(fixture("malformed.sql"))
    assert "L010" in codes(result)
    assert result.has_errors


# --------------------------------------------------------------------------
# Contract
# --------------------------------------------------------------------------

def test_empty_input_fails_cleanly():
    result = sqlglot_engine.convert("   \n  ")
    assert not result.ok
    assert result.sql == ""


def test_unbalanced_syntax_reports_parse_error():
    result = sqlglot_engine.convert("SELECT * FROM t WHERE )")
    assert not result.ok
    assert "SG-PARSE" in codes(result)


def test_engine_never_raises():
    for bad in ["", "???", "\x00", "SELECT", "'unterminated"]:
        result = sqlglot_engine.convert(bad)


def test_duration_is_recorded():
    result = sqlglot_engine.convert("SELECT 1")
    assert result.duration_ms >= 0


def test_large_query_is_not_truncated():
    """No output ceiling: the whole point of preferring sqlglot on big queries."""
    ctes = ", ".join(
        f"cte{i} AS (SELECT TOP 10 a, ISNULL(b,0) AS b FROM t WHERE x > {i})"
        for i in range(60)
    )
    sql = f"WITH {ctes} SELECT * FROM cte0;"
    result = sqlglot_engine.convert(sql)
    assert result.ok
    assert result.sql.count("cte") >= 60
