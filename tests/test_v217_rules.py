"""v2.17.0: A059 / A060 / L032 and their converted-code twins PC15 / PC16.

A statement that returns a TotalRecords / TotalCount column is rewritten at run
time by ACS's filterString pager (QueryParameterProcessor.GetFilteredRecordsAsync):
it cuts the batch at every line-leading SELECT and wraps the piece that mentions
TotalRecords. Every case comes from EDINotification (2026-10-21), where three
list screens returned 500 on PostgreSQL because the converted count subquery put
its SELECT on its own line and the pager wrapped the subquery.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from core import orchestrator, pgcheck, totalrecords, validator

FIXTURES = Path(__file__).parent / "fixtures"

COMMUNICATION_TYPE = """\
SELECT EDI_CommunicationType_Id, Code, Description, IsActive,
       CASE WHEN ISNULL(IsActive, 'false') = 'true' THEN 'YES' ELSE 'NO' END AS IsActiveDesc
INTO #CommunicationType
FROM EDI_CommunicationType WITH(NOLOCK)
WHERE ISNULL(IsDeleted, 'false') = 'false'
  AND OrgProdId = @OrgProdId
ORDER BY 1 DESC;

SELECT *, (SELECT COUNT(*) AS TotalRecords FROM #CommunicationType) AS TotalRecords
FROM #CommunicationType
ORDER BY EDI_CommunicationType_Id DESC;

DROP TABLE #CommunicationType;
"""

# What the Aug 2026 conversion left in EDICommunicationTypeQueries.cs.
CONVERTED_COMMUNICATION_TYPE = """
            SELECT
              EDI_CommunicationType_Id,
              Code,
              Description,
              IsActive,
              CASE WHEN COALESCE(IsActive, FALSE) = TRUE THEN 'YES' ELSE 'NO' END AS IsActiveDesc
            INTO TEMPORARY CommunicationType
            FROM EDI_CommunicationType
            WHERE
              COALESCE(IsDeleted, FALSE) = FALSE AND OrgProdId = 3223
            ORDER BY
              1 DESC NULLS LAST;

            SELECT
              *,
              (
                SELECT
                  COUNT(*) AS TotalRecords
                FROM CommunicationType
              ) AS TotalRecords
            FROM CommunicationType
            ORDER BY
              EDI_CommunicationType_Id DESC NULLS LAST;

            DROP TABLE CommunicationType;
            """

# The SQL the API actually sent to PostgreSQL for it (42601 near "FROM").
REPORTED_WRAP = """
SELECT EDI_CommunicationType_Id, Code, Description, IsActive,
CASE WHEN COALESCE(IsActive, FALSE) = TRUE THEN 'YES' ELSE 'NO' END AS IsActiveDesc
INTO TEMPORARY CommunicationType FROM EDI_CommunicationType
WHERE COALESCE(IsDeleted, FALSE) = FALSE AND OrgProdId = 3223 ORDER BY 1 DESC NULLS LAST;
SELECT *, ( SELECT * FROM (SELECT COUNT(*) AS TotalRecords FROM CommunicationType ) AS TotalRecords
FROM CommunicationType ) FilteredRecords ORDER BY EDI_CommunicationType_Id DESC NULLS LAST
OFFSET @skipRecords ROWS FETCH NEXT @TopRecords ROWS ONLY;
DROP TABLE CommunicationType;
"""


def flat(sql: str) -> str:
    return re.sub(r"\s+", " ", sql).strip()


def codes(result) -> list[str]:
    return [w.code for w in result.warnings]


def convert(sql: str):
    r = orchestrator.convert(sql)
    assert r.validation is not None and r.validation.valid, r.sql
    return r


def pager_ok(sql: str) -> bool:
    """The converted SQL as the pager would run it with a grid filter applied."""
    return validator.validate(totalrecords.infra_wrap(sql, "Code LIKE @p0")).valid


def nested_select_lines(sql: str) -> list[str]:
    return [line for line in sql.splitlines()[1:] if re.match(r"\s*SELECT\b", line, re.I)]


# --------------------------------------------------------------------------
# the pager port is anchored to what production ran
# --------------------------------------------------------------------------

def test_infra_wrap_reproduces_the_reported_query():
    assert flat(totalrecords.infra_wrap(CONVERTED_COMMUNICATION_TYPE, "")) == flat(REPORTED_WRAP)
    assert not validator.validate(REPORTED_WRAP).valid


def test_infra_wrap_falls_back_to_the_last_select():
    sql = "SELECT Id FROM a;\nSELECT Code FROM b ORDER BY Code;"
    wrapped = flat(totalrecords.infra_wrap(sql, "Code = @p0"))
    assert wrapped.startswith("SELECT Id FROM a; SELECT * FROM (SELECT Code FROM b WHERE Code = @p0 )")
    assert "FilteredRecords ORDER BY Code OFFSET @skipRecords ROWS" in wrapped


# --------------------------------------------------------------------------
# A059 -- the count subquery over the statement's own table becomes a window
# --------------------------------------------------------------------------

def test_a059_count_subquery_becomes_window_count():
    r = convert(COMMUNICATION_TYPE)
    assert "COUNT(*) OVER() AS TotalRecords" in r.sql
    assert "A059" in codes(r) and "L032" not in codes(r)
    assert pager_ok(r.sql)


def test_a059_multi_statement_batch_fixture():
    r = convert((FIXTURES / "v217_smoke.sql").read_text(encoding="utf-8"))
    assert "COUNT(*) OVER() AS TotalRecords\nFROM DataExtract" in r.sql
    assert "A059" in codes(r)
    assert pager_ok(r.sql)


def test_a059_count_one_and_total_count():
    r = convert("SELECT *, (SELECT COUNT(1) FROM #t) AS TotalCount FROM #t ORDER BY Id;")
    assert "COUNT(*) OVER() AS TotalCount" in r.sql
    assert pager_ok(r.sql)


@pytest.mark.parametrize("sql", [
    # a WHERE on the subquery counts a different set
    "SELECT *, (SELECT COUNT(*) FROM #t WHERE IsActive = 1) AS TotalRecords FROM #t ORDER BY Id;",
    # another table
    "SELECT *, (SELECT COUNT(*) FROM #a) AS TotalRecords FROM #b ORDER BY Id;",
    # an aggregate outer query returns one row
    "SELECT MAX(Id) AS MaxId, (SELECT COUNT(*) FROM #t) AS TotalRecords FROM #t;",
    # a join multiplies the rows
    "SELECT a.*, (SELECT COUNT(*) FROM #t) AS TotalRecords FROM #t a JOIN Other o ON o.Id = a.Id ORDER BY a.Id;",
    # DISTINCT collapses them
    "SELECT DISTINCT Code, (SELECT COUNT(*) FROM #t) AS TotalRecords FROM #t;",
])
def test_a059_refuses_when_the_window_would_count_other_rows(sql):
    r = convert(sql)
    assert "A059" not in codes(r)
    assert "OVER()" not in r.sql
    # A060 still keeps the statement in one piece for the pager
    assert "A060" in codes(r) and not nested_select_lines(r.sql)
    assert pager_ok(r.sql) and "L032" not in codes(r)


def test_a059_refuses_an_outer_where_and_l032_reports_the_pager_break():
    # the pager appends its own WHERE after this one -- no rewrite can fix that
    r = convert("SELECT *, (SELECT COUNT(*) FROM #t) AS TotalRecords FROM #t WHERE IsActive = 1 ORDER BY Id;")
    assert "A059" not in codes(r)
    assert "L032" in codes(r)


def test_a059_a060_ignore_other_aliases():
    r = convert("SELECT *, (SELECT COUNT(*) FROM #t) AS Cnt FROM #t ORDER BY Id;")
    assert not {"A059", "A060", "L032"} & set(codes(r))
    assert nested_select_lines(r.sql)          # untouched: nothing to wrap here


def test_a059_ignores_string_literals_and_comments():
    for sql in ["SELECT '(SELECT COUNT(*) FROM t) AS TotalRecords' AS Note FROM t;",
                "-- (SELECT COUNT(*) FROM t) AS TotalRecords\nSELECT Id FROM t;"]:
        r = convert(sql)
        assert not {"A059", "A060"} & set(codes(r)), r.sql
        assert "OVER()" not in r.sql


def test_window_count_shape_is_left_alone():
    r = convert("SELECT *, COUNT(*) OVER() AS TotalRecords FROM #t ORDER BY Id;")
    assert not {"A059", "A060", "L032"} & set(codes(r))
    assert pager_ok(r.sql)


# --------------------------------------------------------------------------
# A060 -- nested SELECTs stay on the line of their '('
# --------------------------------------------------------------------------

def test_a060_derived_table_in_a_total_records_statement():
    r = convert("SELECT x.Id, COUNT(*) OVER() AS TotalRecords "
                "FROM (SELECT Id FROM Orders WHERE OrgId = @OrgId) x ORDER BY x.Id;")
    assert "A060" in codes(r) and "(SELECT" in r.sql
    assert not nested_select_lines(r.sql)
    assert pager_ok(r.sql)


def test_a060_never_glues_across_a_comment():
    sql = ("SELECT *, ( -- all rows\n    SELECT COUNT(*) FROM t WHERE x = 1\n  ) AS TotalRecords\n"
           "FROM t\nORDER BY Id;")
    fixed, glued = totalrecords.glue_nested_selects(sql)
    assert glued == 0 and fixed == sql
    assert [w.code for w in totalrecords.lint(sql)] == ["L032"]


def test_a060_whitespace_only():
    sql = "SELECT a,\n  (\n    SELECT MAX(b) FROM t2 WHERE t2.k = t.k\n  ) AS TotalRecords\nFROM t;"
    fixed, glued = totalrecords.glue_nested_selects(sql)
    assert glued == 1
    assert re.sub(r"\s", "", fixed) == re.sub(r"\s", "", sql)
    assert "(SELECT MAX(b)" in fixed


# --------------------------------------------------------------------------
# PC15 / PC16 -- the same repairs on already-converted code (--check-pg)
# --------------------------------------------------------------------------

def test_pc15_repairs_the_converted_edi_query():
    result = pgcheck.check_sql(CONVERTED_COMMUNICATION_TYPE)
    assert ("PC15", 1) in result.applied
    assert "COUNT(*) OVER() AS TotalRecords" in result.sql
    assert result.valid_after
    assert pager_ok(result.sql)
    assert "L032" not in [w.code for w in result.findings]


def test_pc15_pc16_leave_repaired_code_alone():
    fixed = pgcheck.check_sql(CONVERTED_COMMUNICATION_TYPE).sql
    assert not pgcheck.check_sql(fixed).applied


def test_pc16_glues_when_pc15_refuses():
    sql = ("SELECT\n  *,\n  (\n    SELECT\n      COUNT(*)\n    FROM t\n    WHERE x = 1\n  ) AS TotalRecords\n"
           "FROM t\nORDER BY\n  Id;")
    result = pgcheck.check_sql(sql)
    assert [code for code, _ in result.applied] == ["PC16"]
    assert pager_ok(result.sql)
