"""Regression tests from a REAL production dashboard query.

A 58 KB T-SQL dashboard converted with no reported errors, then failed on its
very first statement with:

    SQL Error [42846]: ERROR: cannot cast type date to bytea

Every defect below produces PostgreSQL that the parser ACCEPTS. Only execution
against real types exposed them. That is precisely why rules L011-L014 exist:
they close the gap between "syntactically valid" and "actually runs".

Fixtures are distilled from the real query, not invented.
"""

import pathlib

import pytest

from core import linter, orchestrator
from tools import fix_tsql_for_postgres as fixer

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def codes(result) -> set[str]:
    return {w.code for w in result.warnings}


# --------------------------------------------------------------------------
# L011 -- the one that actually broke production
# --------------------------------------------------------------------------

def test_tsql_timestamp_is_auto_corrected_to_a_datetime():
    """T-SQL TIMESTAMP is ROWVERSION (8-byte binary), NOT a datetime, so a
    faithful translation is BYTEA -- which then fails with 'cannot cast type
    date to bytea'.

    Nobody writing CAST(d AS timestamp) means a rowversion. The engine retypes
    it and DISCLOSES the assumption rather than emitting unusable SQL.
    """
    source = fixture("dashboard_timestamp_trap.sql")
    result = orchestrator.convert(source)
    assert "BYTEA" not in result.sql.upper()
    assert "TIMESTAMP" in result.sql.upper()
    assert "A001" in codes(result), "the assumption must be disclosed"
    assert "L011" not in codes(result)


def test_genuine_rowversion_is_left_alone():
    """SAFETY RULE: if the source says ROWVERSION, the author knows the
    distinction -- do not silently retype their binary column to a datetime."""
    source = ("CREATE TABLE #t (v rowversion, w timestamp); "
              "SELECT CAST(x AS timestamp) FROM #t;")
    result = orchestrator.convert(source)
    assert "BYTEA" in result.sql.upper(), "rowversion must stay binary"
    assert "A001" not in codes(result)
    assert "L011" in codes(result), "and L011 still reports it"


def test_a_column_named_timestamp_is_not_retyped():
    """The retype runs on the AST, so only TYPE positions are touched."""
    result = orchestrator.convert("SELECT timestamp FROM events")
    assert "BYTEA" not in result.sql.upper()
    assert "timestamp" in result.sql.lower()


def test_l011_silent_without_the_bytea_and_timestamp_pair():
    """A genuine binary column must NOT be flagged."""
    assert linter.l011_tsql_timestamp_became_bytea(
        "CREATE TABLE t (payload VARBINARY(MAX))",
        "CREATE TABLE t (payload BYTEA)",
        "CREATE TABLE t (payload BYTEA)") == []


def test_datetime2_source_converts_cleanly():
    """The FIX: DATETIME2 in the source produces TIMESTAMP, no L011."""
    source = fixture("dashboard_timestamp_trap.sql").replace(
        "timestamp", "datetime2")
    result = orchestrator.convert(source)
    assert "BYTEA" not in result.sql.upper()
    assert "L011" not in codes(result)


# --------------------------------------------------------------------------
# L012 -- Windows timezone names
# --------------------------------------------------------------------------

def test_windows_timezone_is_auto_corrected():
    """PostgreSQL raises 'time zone "India Standard Time" not recognized'."""
    result = orchestrator.convert(fixture("dashboard_charge_summary.sql"))
    assert "Asia/Kolkata" in result.sql
    assert "India Standard Time" not in result.sql
    assert "A002" in codes(result)
    assert "L012" not in codes(result)


def test_unknown_timezone_is_left_alone_and_reported():
    """Guessing a timezone would silently shift every timestamp -- refuse."""
    result = orchestrator.convert(
        "SELECT fnGetLocalTime(d, 'Narnia Standard Time') FROM t")
    assert "Narnia Standard Time" in result.sql
    assert "L012" in codes(result)


def test_l012_finds_the_name_inside_a_string_literal():
    """The defect lives INSIDE a quoted string, so this rule must run unmasked."""
    warnings = linter.l012_windows_timezone(
        "", "SELECT fn(d, 'Pacific Standard Time')", "SELECT fn(d, '<STR>')")
    assert [w.code for w in warnings] == ["L012"]


def test_l012_accepts_iana_names():
    assert linter.l012_windows_timezone(
        "", "SELECT fn(d, 'Asia/Kolkata')", "SELECT fn(d, '<STR>')") == []


# --------------------------------------------------------------------------
# L013 -- cross-database references
# --------------------------------------------------------------------------

def test_cross_database_reference_is_auto_corrected():
    """PostgreSQL cannot query across databases. With everything in ONE
    database, the db.dbo. qualifier is simply dropped."""
    result = orchestrator.convert(fixture("dashboard_cross_database.sql"))
    assert "qa_northwind_final" not in result.sql
    assert "A009" in codes(result)
    assert "L013" not in codes(result)


def test_l013_reports_each_database_once():
    sql = ("SELECT * FROM qa_northwind_final.dbo.A "
           "JOIN qa_northwind_final.dbo.B ON 1=1 "
           "JOIN other_db.dbo.C ON 1=1")
    warnings = linter.l013_cross_database_reference("", sql, sql)
    assert len(warnings) == 2, "one per database, not per reference"


def test_l013_ignores_plain_schema_qualification():
    assert linter.l013_cross_database_reference(
        "", "SELECT * FROM public.Users", "SELECT * FROM public.Users") == []


# --------------------------------------------------------------------------
# L014 -- ambiguous date literals
# --------------------------------------------------------------------------

def test_ddmmyyyy_date_literal_is_auto_corrected():
    result = orchestrator.convert(fixture("dashboard_timestamp_trap.sql"))
    assert "2026-07-31" in result.sql
    assert "31-07-2026" not in result.sql
    assert "A003" in codes(result)


def test_ambiguous_date_is_left_alone_and_reported():
    """'05-07-2026' could be 5 July or 7 May. Guessing corrupts data silently
    with no error, so it is reported rather than rewritten."""
    result = orchestrator.convert("SELECT CAST('05-07-2026' AS date)")
    assert "05-07-2026" in result.sql
    assert "L014" in codes(result)
    assert "A003" not in codes(result)


def test_l014_distinguishes_impossible_from_silently_wrong():
    """31-07 cannot be a month -- it errors. 05-07 silently becomes 7 May."""
    impossible = linter.l014_ambiguous_date_literal(
        "", "SELECT '31-07-2026'", "SELECT '<STR>'")
    assert "no month 31" in impossible[0].message

    silent = linter.l014_ambiguous_date_literal(
        "", "SELECT '05-07-2026'", "SELECT '<STR>'")
    assert "SILENTLY" in silent[0].message


def test_l014_accepts_iso_dates():
    assert linter.l014_ambiguous_date_literal(
        "", "SELECT '2026-07-31'", "SELECT '<STR>'") == []


# --------------------------------------------------------------------------
# Already-covered defects, confirmed against the real query
# --------------------------------------------------------------------------

def test_nvarchar_max_is_auto_corrected():
    result = orchestrator.convert(fixture("dashboard_charge_summary.sql"))
    assert "VARCHAR(MAX)" not in result.sql.upper()
    assert "TEXT" in result.sql.upper()
    assert "A004" in codes(result)
    assert "L002" not in codes(result)


def test_getutcdate_is_auto_corrected():
    """PostgreSQL raises 'function getutcdate() does not exist'."""
    result = orchestrator.convert(fixture("dashboard_charge_summary.sql"))
    assert "GETUTCDATE" not in result.sql.upper()
    # NOW() is emitted as its PostgreSQL synonym CURRENT_TIMESTAMP.
    assert "CURRENT_TIMESTAMP AT TIME ZONE 'UTC'" in result.sql.upper()
    assert "A006" in codes(result)
    assert "L008" not in codes(result)


def test_bit_flags_are_auto_corrected():
    """CAST(0 AS BIT) in T-SQL is boolean FALSE; in PostgreSQL it is the
    bit-string B'0' and comparing it to a boolean column fails."""
    result = orchestrator.convert(fixture("dashboard_bit_flags.sql"))
    assert "AS BIT" not in result.sql.upper()
    assert "FALSE" in result.sql.upper()
    assert "A008" in codes(result)
    assert "L003" not in codes(result)


def test_clustered_index_is_auto_corrected():
    """PostgreSQL has no CLUSTERED index -- it is a hard syntax error."""
    result = orchestrator.convert(fixture("dashboard_cross_database.sql"))
    assert "CLUSTERED" not in result.sql.upper()
    assert "CREATE INDEX" in result.sql.upper()
    assert "A005" in codes(result)
    assert result.validation is not None and result.validation.valid,         "removing CLUSTERED should make the whole script parse"


# --------------------------------------------------------------------------
# The fix script closes all of it
# --------------------------------------------------------------------------

@pytest.mark.parametrize("name", [
    "dashboard_timestamp_trap.sql",
    "dashboard_charge_summary.sql",
    "dashboard_cross_database.sql",
    "dashboard_bit_flags.sql",
])
def test_fix_script_removes_the_auto_fixable_defects(name):
    """End-to-end: fix the SOURCE T-SQL, then convert, and the whole class of
    auto-fixable defects is gone."""
    fixed, _ = fixer.apply_fixes(fixture(name))
    result = orchestrator.convert(fixed)

    auto_fixable = {"L002", "L003", "L008", "L011", "L012", "L014"}
    assert not (codes(result) & auto_fixable), \
        f"{name}: {sorted(codes(result) & auto_fixable)} survived the fixer"


def test_fix_script_leaves_cross_database_for_a_human():
    """L013 needs a schema decision only the developer can make -- it must NOT
    be silently rewritten."""
    fixed, _ = fixer.apply_fixes(fixture("dashboard_cross_database.sql"))
    assert "qa_northwind_final.dbo" in fixed
    assert fixer.find_manual(fixed), "it must at least be REPORTED"


def test_bit_literals_become_boolean_literals_not_casts():
    """PostgreSQL CANNOT cast integer to boolean.

    An earlier version of the fixer emitted CAST(0 AS BOOLEAN), which parses
    fine and then fails at runtime with 'cannot cast type integer to boolean'.
    The only correct replacement is the literal FALSE/TRUE.
    """
    fixed, _ = fixer.apply_fixes(fixture("dashboard_bit_flags.sql"))
    assert "CAST(0 AS BOOLEAN)" not in fixed.upper()
    assert "CAST(1 AS BOOLEAN)" not in fixed.upper()
    assert "FALSE" in fixed.upper()

    converted = orchestrator.convert(fixed).sql.upper()
    assert "AS BOOLEAN" not in converted, \
        "no integer->boolean cast may survive into the output"


# --------------------------------------------------------------------------
# A007 / A008 -- the BIT-to-BOOLEAN family
# --------------------------------------------------------------------------

def test_bit_column_compared_to_integer_is_auto_corrected():
    """PostgreSQL: 'COALESCE types boolean and integer cannot be matched'.

    SQL Server silently coerces BIT <-> integer; PostgreSQL does not.
    """
    result = orchestrator.convert(
        "SELECT * FROM t WHERE ISNULL(IsDeleted, 0) = 0")
    assert "COALESCE(IsDeleted, FALSE) = FALSE" in result.sql
    assert "A007" in codes(result)


def test_non_boolean_column_is_left_alone():
    """A column with no boolean evidence must NOT be rewritten -- doing so
    would break a genuine integer comparison."""
    result = orchestrator.convert(
        "SELECT * FROM t WHERE ISNULL(Quantity, 0) = 0")
    assert "COALESCE(Quantity, 0) = 0" in result.sql
    assert "A007" not in codes(result)


def test_column_proven_boolean_by_cast_as_bit():
    """Evidence beats naming: a column used with CAST(0 AS BIT) is boolean
    even without an Is-prefix."""
    from core import autofix
    proven = autofix._boolean_columns(
        "WHERE ISNULL(EGIN.ThroughParking, CAST(0 AS BIT)) = CAST(0 AS BIT)")
    assert "throughparking" in proven


def test_cast_bit_literals_become_boolean_literals():
    """T-SQL BIT is the boolean type; PostgreSQL BIT is a bit-string."""
    result = orchestrator.convert(
        "SELECT * FROM t WHERE flag = CAST(1 AS BIT) AND other = CAST(0 AS BIT)")
    assert "CAST(1 AS BIT)" not in result.sql.upper()
    assert "TRUE" in result.sql.upper() and "FALSE" in result.sql.upper()
    assert "A008" in codes(result)


def test_bit_column_type_becomes_boolean():
    result = orchestrator.convert("CREATE TABLE t (IsActive BIT)")
    assert "BOOLEAN" in result.sql.upper()
    assert "BIT" not in result.sql.upper().replace("BOOLEAN", "")
    assert "A008" in codes(result)
    assert "L003" not in codes(result)


def test_cast_expression_as_bit_becomes_boolean_cast():
    result = orchestrator.convert("SELECT CAST(col AS BIT) FROM t")
    assert "CAST(col AS BOOLEAN)" in result.sql
    assert "A008" in codes(result)


def test_the_whole_dashboard_query_now_validates():
    """End-to-end on the real 57 KB query: every auto-fixable defect gone,
    only the schema decision (L013) left for a human."""
    source = fixture("dashboard_bit_flags.sql")
    result = orchestrator.convert(source)
    assert result.validation is not None and result.validation.valid
    assert "BIT" not in result.sql.upper().replace("BOOLEAN", "")


# --------------------------------------------------------------------------
# A009 -- database.dbo.Table qualifiers (single-database target)
# --------------------------------------------------------------------------

def test_cross_database_qualifier_is_stripped():
    result = orchestrator.convert(
        "SELECT * FROM qa_northwind_final.dbo.Inv_Invoice")
    assert "qa_northwind_final" not in result.sql
    assert "dbo" not in result.sql
    assert "Inv_Invoice" in result.sql
    assert "A009" in codes(result)
    assert "L013" not in codes(result)


def test_bracketed_dbo_qualifier_is_stripped():
    result = orchestrator.convert(
        "SELECT * FROM qa_northwind_final.[dbo].[Master_VehicleType]")
    assert "qa_northwind_final" not in result.sql and "dbo" not in result.sql


def test_dbo_qualified_function_call_is_stripped():
    """dbo.fn(...) parses as a Dot, not a Table -- PostgreSQL would fail with
    'schema dbo does not exist'."""
    result = orchestrator.convert(
        "SELECT dbo.fnGetLocalTime(d, 'Asia/Kolkata') FROM t")
    assert "dbo." not in result.sql
    assert "fnGetLocalTime" in result.sql or "FNGETLOCALTIME" in result.sql.upper()


def test_a_real_schema_is_preserved():
    """SAFETY: only SQL Server's default `dbo` is dropped. Dropping a genuine
    schema would silently repoint the query at a different table."""
    result = orchestrator.convert("SELECT * FROM reporting.Sales")
    assert "reporting" in result.sql


def test_database_dropped_but_real_schema_kept():
    result = orchestrator.convert("SELECT * FROM otherdb.reporting.Sales")
    assert "otherdb" not in result.sql
    assert "reporting" in result.sql


def test_unqualified_tables_are_untouched():
    result = orchestrator.convert("SELECT * FROM Shipment_Master")
    assert "A009" not in codes(result)
    assert "Shipment_Master" in result.sql
