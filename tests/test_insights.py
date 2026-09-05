"""Query Insights tests.

The headline requirement: a "changed" claim must be VERIFIED. Asserting
"BIT -> BOOLEAN" while CAST(x AS BIT) is still in the output is worse than
saying nothing, because the Warnings panel simultaneously reports the opposite.
"""

from core import insights
from core.models import Severity, Warning

DO_BLOCK = """CREATE TEMP TABLE "UserTarget" ("ID" INT);
DO $$
DECLARE
    tableName VARCHAR(50) := 'ActiveUsers';
BEGIN
    EXECUTE 'INSERT INTO "UserTarget" SELECT 1';
END $$;
SELECT * FROM "UserTarget";"""


# --------------------------------------------------------------------------
# split_statements
# --------------------------------------------------------------------------

def test_splits_simple_statements():
    assert insights.split_statements("SELECT 1; SELECT 2;") == ["SELECT 1", "SELECT 2"]


def test_does_not_split_inside_dollar_block():
    parts = insights.split_statements(DO_BLOCK)
    assert len(parts) == 3
    assert parts[1].startswith("DO $$") and "END $$" in parts[1]


def test_does_not_split_inside_string_literal():
    parts = insights.split_statements("SELECT 'a;b' AS x; SELECT 2;")
    assert len(parts) == 2 and "'a;b'" in parts[0]


def test_handles_escaped_quote():
    assert len(insights.split_statements("SELECT 'it''s; fine'; SELECT 2;")) == 2


def test_does_not_split_inside_line_comment():
    assert len(insights.split_statements("SELECT 1; -- a; b\nSELECT 2;")) == 2


def test_trailing_statement_without_semicolon():
    assert insights.split_statements("SELECT 1") == ["SELECT 1"]


# --------------------------------------------------------------------------
# describe
# --------------------------------------------------------------------------

def test_describes_temporary_table():
    assert "temporary" in insights.describe(
        'CREATE TEMPORARY TABLE "T" ("ID" INT)').lower()


def test_describes_select_star_and_columns():
    assert "all columns" in insights.describe("SELECT * FROM t")
    assert "2 columns" in insights.describe("SELECT a, b FROM t")


def test_describes_joins_and_ctes():
    text = insights.describe(
        "WITH c AS (SELECT 1) SELECT a.x FROM a JOIN b ON a.id=b.id")
    assert "joined with" in text and "CTE" in text


def test_describes_union():
    assert "UNION" in insights.describe("SELECT a FROM t UNION SELECT a FROM u")


def test_describes_dml():
    assert insights.describe("INSERT INTO t (a) VALUES (1)").startswith("Insert")
    assert insights.describe("UPDATE t SET a=1").startswith("Update")
    assert insights.describe("DELETE FROM t WHERE a=1").startswith("Delete")


def test_describes_do_block():
    text = insights.describe("DO $$ DECLARE x INT; BEGIN EXECUTE 'x'; END $$")
    assert "PL/pgSQL" in text and "dynamic SQL" in text


# --------------------------------------------------------------------------
# changes -- VERIFIED claims only
# --------------------------------------------------------------------------

def test_claims_top_to_limit_when_top_is_gone():
    notes = insights.changes("SELECT TOP 10 * FROM t", "SELECT * FROM t LIMIT 10")
    assert any("LIMIT" in n for n in notes)


def test_does_not_claim_bit_conversion_when_bit_survived():
    """THE BUG THIS MODULE EXISTS FOR.

    The old annotator matched BOOLEAN/TRUE/FALSE in the output -- including the
    string literals 'true'/'false' -- and claimed BIT had been converted while
    CAST(x AS BIT) was still sitting in the SQL.
    """
    original = "SELECT CAST(x AS BIT) AS f FROM t"
    converted = ("SELECT CAST((CASE WHEN y='true' THEN 'true' ELSE 'false' END) "
                 "AS BIT) AS f FROM t")
    assert not any("BOOLEAN" in n for n in insights.changes(original, converted))


def test_reports_the_survivor_instead():
    original = "SELECT CAST(x AS BIT) FROM t"
    converted = "SELECT CAST(x AS BIT) FROM t"
    assert any("STILL PRESENT" in n for n in insights.unconverted(original, converted))


def test_claims_bit_conversion_when_bit_really_is_gone():
    notes = insights.changes("SELECT CAST(x AS BIT) FROM t",
                             "SELECT CAST(x AS BOOLEAN) FROM t")
    assert any("BOOLEAN" in n for n in notes)


def test_string_literals_never_trigger_a_claim():
    """A query selecting the word 'GETDATE()' is not a GETDATE conversion."""
    notes = insights.changes("SELECT 'GETDATE()' AS note FROM t",
                             "SELECT 'GETDATE()' AS note FROM t")
    assert not any("CURRENT_TIMESTAMP" in n for n in notes)


def test_no_claim_when_feature_absent_from_source():
    notes = insights.changes("SELECT * FROM t", "SELECT * FROM t LIMIT 10")
    assert not any("TOP" in n for n in notes)


def test_nolock_removal_claimed_only_when_removed():
    assert any("NOLOCK" in n for n in
               insights.changes("SELECT * FROM t WITH (NOLOCK)", "SELECT * FROM t"))
    assert not any("NOLOCK" in n for n in
                   insights.changes("SELECT * FROM t WITH (NOLOCK)",
                                    "SELECT * FROM t WITH (NOLOCK)"))


def test_isnull_claim_requires_isnull_gone():
    assert any("COALESCE" in n for n in
               insights.changes("SELECT ISNULL(a,0)", "SELECT COALESCE(a,0)"))
    # Mixed output: one converted, one left behind -> no success claim.
    assert not any("COALESCE" in n for n in
                   insights.changes("SELECT ISNULL(a,0), ISNULL(b,0)",
                                    "SELECT COALESCE(a,0), ISNULL(b,0)"))


# --------------------------------------------------------------------------
# stats
# --------------------------------------------------------------------------

def test_stats_report_tables_joins_and_parameters():
    data = insights.stats(
        "SELECT * FROM a JOIN b ON a.id=b.id WHERE x=@Id",
        "SELECT * FROM a JOIN b ON a.id=b.id WHERE x=@Id")
    assert data["Joins"] == 1
    assert "a" in data["Tables"] and "b" in data["Tables"]
    assert data["Parameters"] == "@Id"


def test_stats_counts_statements():
    assert insights.stats("", "SELECT 1; SELECT 2;")["Statements"] == 2


# --------------------------------------------------------------------------
# analyze / render
# --------------------------------------------------------------------------

def test_analyze_merges_warnings_into_attention():
    warnings = [Warning("L003", Severity.WARNING, "BIT is not boolean", 1)]
    data = insights.analyze("SELECT TOP 1 * FROM t", "SELECT * FROM t LIMIT 1",
                            warnings)
    assert any("L003" in a for a in data.attention)


def test_analyze_skips_info_warnings():
    warnings = [Warning("API-RETRY", Severity.INFO, "retried once")]
    data = insights.analyze("SELECT 1", "SELECT 1", warnings)
    assert not any("API-RETRY" in a for a in data.attention)


def test_analyze_of_empty_sql_is_empty():
    assert insights.analyze("SELECT 1", "").is_empty


def test_render_text_has_all_sections():
    warnings = [Warning("L003", Severity.WARNING, "BIT is not boolean", 1)]
    text = insights.render_text(
        insights.analyze("SELECT TOP 1 * FROM t WITH (NOLOCK)",
                         "SELECT * FROM t LIMIT 1", warnings))
    assert "What this query does" in text
    assert "What changed from T-SQL" in text
    assert "Needs attention" in text
    assert "Query facts" in text


def test_render_text_is_ascii():
    """Printed to a cp1252 Windows console -- non-ASCII would raise."""
    text = insights.render_text(
        insights.analyze("DECLARE @x INT; EXEC(@x); SELECT TOP 1 * FROM t",
                         DO_BLOCK))
    assert all(ord(c) < 128 for c in text)


# --------------------------------------------------------------------------
# Regressions from the real dashboard query's insights output
# --------------------------------------------------------------------------

def test_create_table_name_is_not_empty():
    """CREATE TABLE wraps the table in a Schema node carrying the columns, so
    tree.this.name was empty and every table showed as Create a table ""."""
    assert '"tmpDateConversions"' in insights.describe(
        "CREATE TEMPORARY TABLE tmpDateConversions (a INT, b DATE)")
    assert '"users"' in insights.describe("CREATE TABLE users (id INT)")


def test_union_branch_count_is_not_off_by_one():
    """find_all(Union) includes the node itself -- a 2-branch UNION was
    reported as 3."""
    assert "2 result sets" in insights.describe(
        "SELECT a FROM t UNION ALL SELECT a FROM u")
    assert "3 result sets" in insights.describe(
        "SELECT a FROM t UNION ALL SELECT a FROM u UNION ALL SELECT a FROM v")
