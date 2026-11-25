"""A018 statement-terminator inference tests.

T-SQL tolerates whole batches with no `;` between statements; sqlglot cannot
segment them, so those queries were refused wholesale. A018 infers the missing
terminators -- but it edits TEXT before parsing, so the cost of a wrong guess
is a corrupted statement. Most tests below are therefore about what must NOT
be split.
"""

from core import autofix, orchestrator
from core.models import Severity


def infer(sql):
    return autofix.infer_statement_terminators(sql)


def errors(result):
    return [w.code for w in result.warnings if w.severity is Severity.ERROR]


# --------------------------------------------------------------------------
# What must be terminated
# --------------------------------------------------------------------------

def test_unterminated_statements_get_semicolons():
    fixed, n = infer("SELECT a FROM t\nDROP TABLE t")
    assert n == 1
    assert "t;" in fixed.splitlines()[0].replace(" ", "")


def test_create_insert_select_drop_batch_converts_end_to_end():
    """The PayerNameData/GSTPayerNameData shape from DropdownQueryMap."""
    batch = (
        "CREATE TABLE #Temp (PayerName VARCHAR(MAX))\n"
        "INSERT INTO #Temp\n"
        "SELECT OrgName FROM Organization\n"
        "SELECT T.PayerName FROM #Temp AS T\n"
        "DROP TABLE #Temp"
    )
    result = orchestrator.convert(batch)
    assert errors(result) == [], errors(result)
    assert result.validation and result.validation.valid
    assert "A018" in {w.code for w in result.warnings}


def test_terminator_lands_on_code_not_on_a_trailing_comment():
    """InvoiceNoList regression: the last line before UPDATE was a comment-only
    `--OFFSET ...` line, and a `;` appended there vanishes into the comment."""
    batch = (
        "SELECT a FROM t\n"
        "--OFFSET 0 ROWS FETCH NEXT @Top ROWS ONLY\n"
        "DROP TABLE t"
    )
    fixed, n = infer(batch)
    assert n == 1
    lines = fixed.splitlines()
    assert lines[0].rstrip().endswith(";")
    assert not lines[1].rstrip().endswith(";")


# --------------------------------------------------------------------------
# What must NOT be split
# --------------------------------------------------------------------------

def test_insert_select_feed_is_not_split():
    """DocNoList regression: `INSERT INTO #t` + newline + `SELECT` is ONE
    statement; a `;` between them makes the INSERT invalid."""
    _, n = infer("INSERT INTO #t\nSELECT a FROM src")
    assert n == 0


def test_insert_with_column_list_feed_is_not_split():
    _, n = infer("INSERT INTO #t (a, b)\nSELECT a, b FROM src")
    assert n == 0


def test_cte_body_is_not_split():
    _, n = infer("WITH c AS (\n    SELECT a FROM t\n)\nSELECT a FROM c")
    assert n == 0


def test_derived_table_select_is_not_split():
    _, n = infer("SELECT x FROM (\nSELECT a AS x FROM t\n) AS d")
    assert n == 0


def test_union_branches_are_not_split():
    _, n = infer("SELECT a FROM t\nUNION ALL\nSELECT b FROM u")
    assert n == 0


def test_already_terminated_statements_are_untouched():
    sql = "SELECT a FROM t;\nDROP TABLE t;"
    fixed, n = infer(sql)
    assert n == 0
    assert fixed == sql


def test_keywords_inside_strings_and_comments_do_not_trigger():
    sql = (
        "SELECT a FROM t WHERE x = 'not a\nSELECT statement'\n"
        "-- SELECT commented out\n"
        "ORDER BY a"
    )
    _, n = infer(sql)
    assert n == 0


# --------------------------------------------------------------------------
# Engine integration: acceptance requires a real parse
# --------------------------------------------------------------------------

def test_if_else_batches_stay_refused():
    """Terminators cannot fix control flow; the retry must be rejected and the
    original refusal preserved -- never a half-converted output."""
    batch = (
        "IF @Kind = 1\n"
        "BEGIN\n"
        "SELECT a FROM t\n"
        "END\n"
        "ELSE\n"
        "BEGIN\n"
        "SELECT b FROM u\n"
        "END"
    )
    result = orchestrator.convert(batch)
    assert errors(result), "IF/ELSE must still be refused"


def test_valid_input_never_goes_through_inference():
    result = orchestrator.convert("SELECT a FROM t WHERE b = @P")
    assert "A018" not in {w.code for w in result.warnings}
