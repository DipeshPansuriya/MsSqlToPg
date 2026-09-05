"""Linter rule tests.

Every rule gets a positive case, a negative case, and -- where relevant -- a
string-literal false-positive case, since masking is the subtlest part of the design.
"""

from core import linter
from core.models import Severity


def codes(warnings):
    return [w.code for w in warnings]


def run(rule, original="", converted=""):
    return rule(original, converted, linter.mask_literals(converted))


# --------------------------------------------------------------------------
# mask_literals
# --------------------------------------------------------------------------

def test_mask_replaces_literal_contents():
    assert linter.mask_literals("SELECT 'ActiveUsers'") == "SELECT '<STR>'"


def test_mask_handles_escaped_quotes():
    assert linter.mask_literals("SELECT 'it''s here'") == "SELECT '<STR>'"


def test_mask_preserves_dollar_quoted_blocks():
    sql = "DO $$ DECLARE x INT; BEGIN SELECT 'a'; END $$;"
    masked = linter.mask_literals(sql)
    # The plpgsql body is code, not a literal -- it must survive intact.
    assert "DECLARE x INT" in masked


# --------------------------------------------------------------------------
# L001 string concatenation
# --------------------------------------------------------------------------

def test_l001_flags_plus_with_string():
    # One warning per offending expression, not one per operator: regex matches
    # are non-overlapping, so `+ '<STR>'` consumes the literal and the trailing
    # `+` does not match again. Flagging the line once is the right UX.
    warnings = run(linter.l001_string_concat, converted="SELECT a + ' - ' + b FROM t")
    assert codes(warnings) == ["L001"]
    assert warnings[0].severity is Severity.ERROR


def test_l001_flags_literal_on_the_left():
    assert codes(run(linter.l001_string_concat,
                     converted="SELECT 'prefix: ' + name FROM t")) == ["L001"]


def test_l001_known_limitation_column_to_column_concat():
    """DOCUMENTED GAP: `a + b` where both are text columns is undetectable.

    Neither regex nor sqlglot's AST carries column type information, so there is
    no way to distinguish text concatenation from numeric addition here. L009 and
    manual review are the backstop. Do not "fix" this by flagging every `+`.
    """
    assert run(linter.l001_string_concat, converted="SELECT first + last FROM t") == []


def test_l001_ignores_numeric_addition():
    assert run(linter.l001_string_concat, converted="SELECT qty + 1 FROM t") == []


def test_l001_ignores_pipe_concatenation():
    assert run(linter.l001_string_concat, converted="SELECT a || ' - ' || b") == []


# --------------------------------------------------------------------------
# L002 VARCHAR(MAX)
# --------------------------------------------------------------------------

def test_l002_flags_varchar_max():
    assert codes(run(linter.l002_varchar_max, converted="DECLARE x VARCHAR(MAX)")) == ["L002"]


def test_l002_flags_nvarchar_max():
    assert codes(run(linter.l002_varchar_max, converted="x NVARCHAR( MAX )")) == ["L002"]


def test_l002_ignores_sized_varchar():
    assert run(linter.l002_varchar_max, converted="x VARCHAR(50)") == []


def test_l002_ignores_the_word_max_in_a_literal():
    assert run(linter.l002_varchar_max, converted="SELECT 'VARCHAR(MAX)' AS note") == []


# --------------------------------------------------------------------------
# L003 BIT
# --------------------------------------------------------------------------

def test_l003_flags_bare_bit():
    assert codes(run(linter.l003_bit_type, converted="IsActive BIT DEFAULT 1")) == ["L003"]


def test_l003_ignores_bit_varying():
    assert run(linter.l003_bit_type, converted="flags BIT VARYING(8)") == []


def test_l003_ignores_bit_inside_identifier():
    assert run(linter.l003_bit_type, converted="SELECT rabbit, bitmask FROM t") == []


# --------------------------------------------------------------------------
# L004 surviving variables
# --------------------------------------------------------------------------

def test_l004_flags_declared_local_variable():
    assert codes(run(linter.l004_surviving_variables,
                     original="DECLARE @Status INT;",
                     converted="SELECT * FROM t WHERE id = @Status")) == ["L004"]


def test_l004_does_not_flag_dapper_parameter():
    """An @name that was never DECLAREd is a Dapper placeholder. Npgsql accepts
    `@name` natively, so it is correct as-is -- flagging it would make every
    Dapper query in the codebase look broken. See tests/test_dapper_parameters.py."""
    assert run(linter.l004_surviving_variables,
               original="SELECT * FROM t WHERE id = @Status",
               converted="SELECT * FROM t WHERE id = @Status") == []


def test_l004_flags_sqlglot_dollar_rendering():
    assert codes(run(linter.l004_surviving_variables,
                     converted="DECLARE $TableName VARCHAR(50)")) == ["L004"]


def test_l004_allows_dollar_quoting_and_positional_params():
    assert run(linter.l004_surviving_variables,
               converted="DO $$ BEGIN PERFORM 1; END $$; SELECT $1;") == []


def test_l004_ignores_email_in_literal():
    assert run(linter.l004_surviving_variables,
               converted="SELECT 'user@example.com' AS email") == []


# --------------------------------------------------------------------------
# L005 temp-table markers
# --------------------------------------------------------------------------

def test_l005_flags_surviving_hash_name_in_literal():
    sql = "EXECUTE 'INSERT INTO #UserTarget SELECT 1'"
    assert codes(run(linter.l005_temp_table_marker, converted=sql)) == ["L005"]


def test_l005_clean_when_absent():
    assert run(linter.l005_temp_table_marker, converted='INSERT INTO "UserTarget" SELECT 1') == []


# --------------------------------------------------------------------------
# L006 TRY_CAST
# --------------------------------------------------------------------------

def test_l006_flags_lost_try_semantics():
    warnings = run(linter.l006_try_cast_lost,
                   original="SELECT TRY_CAST(c AS INT)",
                   converted="SELECT CAST(c AS INT)")
    assert codes(warnings) == ["L006"]


def test_l006_silent_when_try_preserved():
    assert run(linter.l006_try_cast_lost,
               original="SELECT TRY_CAST(c AS INT)",
               converted="SELECT TRY_CAST(c AS INT)") == []


def test_l006_silent_when_input_had_no_try():
    assert run(linter.l006_try_cast_lost,
               original="SELECT CAST(c AS INT)",
               converted="SELECT CAST(c AS INT)") == []


# --------------------------------------------------------------------------
# L007 dropped EXEC
# --------------------------------------------------------------------------

def test_l007_flags_dropped_exec():
    warnings = run(linter.l007_exec_dropped,
                   original="EXEC(@DynamicSQL);",
                   converted="SELECT 1;")
    assert codes(warnings) == ["L007"]
    assert warnings[0].severity is Severity.ERROR


def test_l007_silent_when_execute_present():
    assert run(linter.l007_exec_dropped,
               original="EXEC(@sql);",
               converted="EXECUTE stmt;") == []


# --------------------------------------------------------------------------
# L008 unconverted builtins
# --------------------------------------------------------------------------

def test_l008_flags_getdate_and_isnull():
    warnings = run(linter.l008_unconverted_builtins,
                   converted="SELECT ISNULL(a,0), GETDATE()")
    assert set(codes(warnings)) == {"L008"}
    assert len(warnings) == 2


def test_l008_flags_len_but_not_length():
    assert len(run(linter.l008_unconverted_builtins, converted="SELECT LEN(a)")) == 1
    assert run(linter.l008_unconverted_builtins, converted="SELECT LENGTH(a)") == []


def test_l008_flags_surviving_nolock():
    assert codes(run(linter.l008_unconverted_builtins,
                     converted="SELECT * FROM t WITH (NOLOCK)")) == ["L008"]


def test_l008_ignores_builtin_name_inside_literal():
    assert run(linter.l008_unconverted_builtins,
               converted="SELECT 'GETDATE() is a T-SQL function' AS note") == []


def test_l008_clean_on_converted_output():
    assert run(linter.l008_unconverted_builtins,
               converted="SELECT COALESCE(a,0), CURRENT_TIMESTAMP") == []


# --------------------------------------------------------------------------
# L009 mass loss
# --------------------------------------------------------------------------

def test_l009_flags_collapsed_output():
    original = "SELECT " + ", ".join(f"col{i}" for i in range(80)) + " FROM t"
    assert codes(run(linter.l009_mass_loss, original=original,
                     converted="SELECT 1")) == ["L009"]


def test_l009_ignores_short_input():
    # Below the 200-char floor the ratio is too noisy to be meaningful.
    assert run(linter.l009_mass_loss, original="SELECT TOP 1 * FROM t",
               converted="SELECT * FROM t LIMIT 1") == []


def test_l009_silent_on_comparable_length():
    original = "SELECT " + ", ".join(f"col{i}" for i in range(80)) + " FROM t"
    assert run(linter.l009_mass_loss, original=original, converted=original) == []


def test_l009_flags_empty_output():
    original = "SELECT " + ", ".join(f"col{i}" for i in range(80)) + " FROM t"
    assert codes(run(linter.l009_mass_loss, original=original, converted="")) == ["L009"]


# --------------------------------------------------------------------------
# L010 -- AST based
# --------------------------------------------------------------------------

def test_l010_flags_expression_parsed_as_non_statement():
    import sqlglot
    # 'SELEKT * FRM' parses as a MULTIPLICATION expression, not SQL, and raises
    # nothing at any error_level. Only the AST reveals it.
    trees = sqlglot.parse("SELEKT * FRM", read="tsql")
    warnings = linter.l010_unparsed_ast(trees)
    assert codes(warnings) == ["L010"]
    assert warnings[0].severity is Severity.ERROR


def test_l010_flags_command_passthrough():
    import sqlglot
    trees = sqlglot.parse("CREATE TABL x (", read="tsql")
    assert codes(linter.l010_unparsed_ast(trees)) == ["L010"]


def test_l010_silent_on_valid_sql():
    import sqlglot
    trees = sqlglot.parse("SELECT TOP 1 * FROM t", read="tsql")
    assert linter.l010_unparsed_ast(trees) == []


# --------------------------------------------------------------------------
# lint() aggregation
# --------------------------------------------------------------------------

def test_lint_runs_every_rule():
    warnings = linter.lint(
        original="DECLARE @v INT; SELECT TRY_CAST(a AS INT) + 'x' FROM t WITH (NOLOCK)",
        converted="SELECT CAST(a AS INT) + 'x' FROM t WITH (NOLOCK) WHERE b = @v",
    )
    found = set(codes(warnings))
    assert {"L001", "L004", "L006", "L008"} <= found


def test_lint_clean_output_has_no_warnings():
    assert linter.lint(
        original="SELECT TOP 1 name FROM users",
        converted='SELECT name FROM users LIMIT 1',
    ) == []


# --------------------------------------------------------------------------
# L010 -- must not fire on legitimate T-SQL statement nodes
# --------------------------------------------------------------------------

import pytest


@pytest.mark.parametrize("sql", [
    "DECLARE @x INT;",
    "SET @x = 1;",
    "EXEC(@s);",
    "IF 1=1 SELECT 1;",
    "MERGE INTO t USING s ON t.a=s.a WHEN MATCHED THEN UPDATE SET t.b=s.b",
    "INSERT INTO t VALUES (1)",
    "CREATE TABLE t (a INT)",
])
def test_l010_does_not_fire_on_valid_statements(sql):
    """DECLARE/SET/EXEC/IF are real T-SQL statements, not typos. Flagging them
    as 'not SQL' would bury the genuine L004/L007 findings in noise."""
    import sqlglot
    assert linter.l010_unparsed_ast(sqlglot.parse(sql, read="tsql")) == []


# --------------------------------------------------------------------------
# dedupe
# --------------------------------------------------------------------------

def test_dedupe_collapses_repeats_of_the_same_defect():
    # `'a' + @v + 'b'` matches the L001 pattern on both sides of one expression.
    warnings = linter.lint(original="x", converted="SELECT 'a' + v + 'b' FROM t")
    l001 = [w for w in warnings if w.code == "L001"]
    assert len(l001) == 1


def test_dedupe_keeps_distinct_lines():
    warnings = linter.dedupe([
        linter.Warning("L004", Severity.ERROR, "same", 1),
        linter.Warning("L004", Severity.ERROR, "same", 2),
        linter.Warning("L004", Severity.ERROR, "same", 1),
    ])
    assert len(warnings) == 2
