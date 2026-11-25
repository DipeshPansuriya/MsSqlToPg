"""A019 / L018: T-SQL UPDATE/DELETE targeting a FROM alias.

PostgreSQL resolves the UPDATE target BEFORE the FROM clause, so
`UPDATE S ... FROM templst S` passes grammar validation and fails at runtime
with 'relation "s" does not exist' -- valid syntax, wrong meaning. A019 fixes
the unambiguous single-table form; L018 blocks the join form from being
rewritten into source.
"""

from core import orchestrator
from core.models import Severity


def errors(result):
    return [w.code for w in result.warnings if w.severity is Severity.ERROR]


def codes(result):
    return {w.code for w in result.warnings}


# --------------------------------------------------------------------------
# A019 -- the unambiguous single-table form is fixed
# --------------------------------------------------------------------------

def test_update_alias_single_table_is_retargeted():
    result = orchestrator.convert(
        "UPDATE S SET flag = 'x' FROM #templst S WHERE S.a = 1")
    assert errors(result) == []
    sql = " ".join(result.sql.split())
    assert sql.startswith("UPDATE templst AS S SET")
    assert "FROM" not in sql.upper().replace("FROM TEMPLST", "")
    assert "A019" in codes(result)


def test_delete_alias_is_retargeted():
    result = orchestrator.convert("DELETE S FROM #templst S WHERE S.a = 1")
    assert errors(result) == []
    assert " ".join(result.sql.split()).startswith("DELETE FROM templst AS S")


def test_plain_update_is_untouched():
    result = orchestrator.convert("UPDATE t SET a = 1 WHERE b = 2")
    assert "A019" not in codes(result)
    assert errors(result) == []


def test_update_with_unrelated_from_is_untouched():
    """`UPDATE t SET ... FROM other o` -- the target is a real table, not the
    FROM alias. Retargeting would change meaning; leave it alone."""
    result = orchestrator.convert(
        "UPDATE t SET a = o.v FROM other o WHERE o.id = t.id")
    assert "A019" not in codes(result)


# --------------------------------------------------------------------------
# L018 -- the join form is refused loudly, not guessed at
# --------------------------------------------------------------------------

def test_update_alias_with_join_is_flagged_as_error():
    result = orchestrator.convert(
        "UPDATE S SET flag = 'x' "
        "FROM other O INNER JOIN #templst S ON O.id = S.id")
    assert "L018" in errors(result)


def test_l018_message_names_the_alias():
    result = orchestrator.convert(
        "UPDATE S SET flag = 'x' "
        "FROM other O INNER JOIN #templst S ON O.id = S.id")
    message = next(w.message for w in result.warnings if w.code == "L018")
    assert "'S'" in message


# --------------------------------------------------------------------------
# A007 SET-clause guard (found while converting DocNoList)
# --------------------------------------------------------------------------

def test_string_flag_assignment_is_not_booleanised():
    """`SET IsRefundExpire = 'false'` writes to a VARCHAR('true'/'false')
    column -- sqlglot parses SET items as EQ nodes, and rewriting the string
    to a boolean breaks the UPDATE at runtime."""
    result = orchestrator.convert(
        "UPDATE #t SET IsRefundExpire = 'false' WHERE a = 1")
    assert "= 'false'" in result.sql


def test_integer_flag_assignment_is_still_booleanised():
    """`SET IsDeleted = 1` on a BIT column must still become TRUE."""
    result = orchestrator.convert("UPDATE t SET IsDeleted = 1 WHERE a = 1")
    assert "= TRUE" in result.sql


def test_string_flag_comparison_is_still_booleanised():
    """Outside a SET clause the 'true'/'false' comparison handling stays."""
    result = orchestrator.convert("SELECT a FROM t WHERE IsDeleted = 'true'")
    assert "= TRUE" in result.sql


# --------------------------------------------------------------------------
# A020 -- boolean columns used as values (the 42804 class)
# --------------------------------------------------------------------------

def test_cast_of_boolean_coalesce_becomes_case_producing_1_0():
    """`CAST(COALESCE(IsActive, 0) AS VARCHAR(10))` fails at runtime with
    42804 AND would render 'true'/'false' where T-SQL rendered '1'/'0'."""
    result = orchestrator.convert(
        "SELECT CAST(COALESCE(SLV.IsActive, 0) AS VARCHAR(10)) AS IsActive "
        "FROM System_ListOfValue SLV")
    sql = " ".join(result.sql.split())
    assert "CASE WHEN COALESCE(SLV.IsActive, FALSE) THEN '1' ELSE '0' END" in sql
    assert "A020" in codes(result)


def test_convert_of_bare_boolean_keeps_null_as_null():
    result = orchestrator.convert(
        "SELECT CONVERT(NVARCHAR(10), SLV.IsActive) AS IsActive FROM t SLV")
    sql = " ".join(result.sql.split())
    assert "THEN '1'" in sql and "THEN '0'" in sql
    assert "ELSE" not in sql          # NULL must stay NULL, like CONVERT


def test_boolean_coalesce_default_is_fixed_in_any_context():
    result = orchestrator.convert("SELECT COALESCE(IsDeleted, 0) AS d FROM t")
    assert "COALESCE(IsDeleted, FALSE)" in result.sql


def test_isnull_with_string_zero_default_is_fixed():
    result = orchestrator.convert(
        "SELECT CAST(ISNULL(IsActive, '0') AS VARCHAR(10)) AS a FROM t")
    assert "THEN '1' ELSE '0'" in " ".join(result.sql.split())


def test_non_boolean_casts_are_untouched():
    result = orchestrator.convert(
        "SELECT CAST(COALESCE(Amount, 0) AS VARCHAR(10)) AS a, "
        "CAST(COALESCE(Name, '') AS VARCHAR(10)) AS b FROM t")
    sql = " ".join(result.sql.split())
    assert "CAST(COALESCE(Amount, 0) AS VARCHAR(10))" in sql
    assert "CAST(COALESCE(Name, '') AS VARCHAR(10))" in sql
    assert "A020" not in codes(result)


def test_a020_is_idempotent_on_its_own_output():
    once = orchestrator.convert(
        "SELECT CAST(COALESCE(IsActive, 0) AS VARCHAR(10)) AS a FROM t").sql
    twice = orchestrator.convert(once).sql
    assert " ".join(once.split()) == " ".join(twice.split())


# --------------------------------------------------------------------------
# A021 -- numeric CAST of a parameter tolerates ''
# --------------------------------------------------------------------------

def test_try_cast_of_param_becomes_nullif_form():
    result = orchestrator.convert(
        "SELECT * FROM t WHERE (@SelectedVal = '' "
        "OR Id = TRY_CAST(@SelectedVal AS INT))")
    assert "CAST(NULLIF(CAST(@SelectedVal AS TEXT), '') AS INT)" in \
        " ".join(result.sql.split())
    assert "A021" in codes(result)


def test_plain_cast_of_param_is_also_protected():
    result = orchestrator.convert(
        "SELECT * FROM t WHERE A.ActivityId = CAST(@FilterId AS INT)")
    assert "NULLIF" in result.sql


def test_cast_of_column_is_untouched():
    result = orchestrator.convert("SELECT CAST(items AS INT) FROM x")
    assert "NULLIF" not in result.sql
    assert "A021" not in codes(result)


def test_a021_is_idempotent():
    once = orchestrator.convert(
        "SELECT * FROM t WHERE Id = CAST(@V AS INT)").sql
    twice = orchestrator.convert(once).sql
    assert " ".join(once.split()) == " ".join(twice.split())


# --------------------------------------------------------------------------
# A022 -- ACS dbo.Split table function
# --------------------------------------------------------------------------

def test_dbo_split_becomes_string_to_table_with_items_alias():
    result = orchestrator.convert(
        "SELECT CAST(items AS INT) FROM dbo.Split(@PayerIDs, ',')")
    assert "STRING_TO_TABLE(NULLIF(@PayerIDs, ''), ',') AS split(items)" in \
        " ".join(result.sql.split())
    assert "A022" in codes(result)


def test_empty_csv_guard_is_added_to_existing_string_to_table():
    """string_to_table('') returns one '' row; strict-function NULL returns
    zero rows -- matching dbo.Split('') and keeping CAST(items AS INT) safe."""
    result = orchestrator.convert(
        "SELECT CAST(items AS INT) "
        "FROM STRING_TO_TABLE(@Ids, ',') AS split(items)")
    assert "NULLIF(@Ids, '')" in result.sql


def test_bare_split_survivor_is_also_rewritten():
    """Earlier conversions stripped `dbo.` and left `SPLIT(...)` behind."""
    result = orchestrator.convert(
        "SELECT CAST(items AS INT) FROM SPLIT(@Ids, ',')")
    assert "STRING_TO_TABLE" in result.sql


def test_split_existing_alias_is_preserved():
    result = orchestrator.convert(
        "SELECT CAST(s.items AS INT) FROM dbo.Split(@Ids, ',') s")
    assert "AS s(items)" in " ".join(result.sql.split())
