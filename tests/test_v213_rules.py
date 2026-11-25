"""v2.13.0 -- every rule added from the Aug/Sep 2026 hand-conversion lessons.

Each test pins a defect that v2.12.0 shipped as grammar-VALID with no ERROR:
it parsed, and then failed at runtime or returned different data.
"""

from __future__ import annotations

import pytest

from core import orchestrator, pgfixes, schema_hints, validator
from core.models import Severity


def conv(sql: str):
    return orchestrator.convert(sql)


def codes(result) -> set[str]:
    return {w.code for w in result.warnings}


def errors(result) -> set[str]:
    return {w.code for w in result.warnings if w.severity is Severity.ERROR}


def one_line(sql: str) -> str:
    return " ".join(sql.split())


def assert_usable(result):
    assert result.is_usable, [str(w) for w in result.warnings]
    assert result.validation is None or result.validation.valid


# ---------------------------------------------------------------- A025
def test_fntolocaltime_cast_to_datetime_is_dropped():
    r = conv("SELECT fnToLocalTime(CAST(a.ReceivedTime AS DATETIME), @TimeZone) AS t FROM a")
    assert "A025" in codes(r)
    assert "CAST(a.ReceivedTime AS TIMESTAMP)" not in r.sql
    assert "FNTOLOCALTIME(a.ReceivedTime, @TimeZone)" in one_line(r.sql).upper().replace(
        "A.RECEIVEDTIME", "a.ReceivedTime").replace("@TIMEZONE", "@TimeZone")


def test_fntolocaltime_getutcdate_becomes_now_not_naive_utc():
    r = conv("SELECT dbo.fnToLocalTime(GETUTCDATE(), @TimeZone) AS t")
    assert "AT TIME ZONE 'UTC'" not in r.sql
    assert "CURRENT_TIMESTAMP" in r.sql


def test_getutcdate_outside_fntolocaltime_is_unchanged():
    r = conv("SELECT GETUTCDATE() AS t")
    assert "AT TIME ZONE 'UTC'" in r.sql


# ---------------------------------------------------------------- A026
@pytest.mark.parametrize("mask,expected", [
    ("N2", "FM999,999,999,999,990.00"),
    ("N0", "FM999,999,999,999,990"),
    ("0.00", "FM999999999999990.00"),
    ("#,##0.00", "FM999,999,999,999,990.00"),
    ("D5", "FM999999999900000"),
])
def test_format_numeric_masks(mask, expected):
    r = conv(f"SELECT FORMAT(n, '{mask}') AS x FROM t")
    assert "NUMBER_TO_STR" not in r.sql
    assert f"TO_CHAR(n, '{expected}')" in r.sql
    assert_usable(r)


@pytest.mark.parametrize("mask,expected", [
    ("dd/MM/yyyy", "DD/MM/YYYY"),
    ("hh:mm tt", "HH12:MI AM"),
    ("yyyy-MM-ddTHH:mm:ss", 'YYYY-MM-DD"T"HH24:MI:SS'),
    ("dd-MMM-yyyy HH:mm", "DD-Mon-YYYY HH24:MI"),
])
def test_format_date_masks(mask, expected):
    r = conv(f"SELECT FORMAT(d, '{mask}') AS x FROM t")
    assert f"TO_CHAR(d, '{expected}')" in r.sql
    assert_usable(r)


def test_format_parameter_mask_is_kept_and_warned():
    r = conv("SELECT FORMAT(d, @mask) AS x FROM t")
    assert "TO_CHAR(d, @mask)" in r.sql
    assert "%B" not in r.sql            # sqlglot's fabricated mask is gone
    assert "L024" in codes(r)


def test_format_currency_is_refused():
    r = conv("SELECT FORMAT(n, 'C') AS x FROM t")
    assert not r.is_usable
    assert "A026" in errors(r) or "R001" in errors(r)


def test_format_single_letter_d_is_refused_not_treated_as_number():
    assert pgfixes._numeric_mask("d") is None


# ---------------------------------------------------------------- A027 / A040
def test_if_object_id_temp_guard_becomes_pg_temp_drop():
    r = conv("IF OBJECT_ID('tempdb..#t') IS NOT NULL DROP TABLE #t")
    assert one_line(r.sql) == "DROP TABLE IF EXISTS pg_temp.t;"
    assert_usable(r)


def test_if_exists_tempdb_sysobjects_guard():
    r = conv("IF EXISTS (SELECT 1 FROM tempdb.dbo.sysobjects WHERE ID = "
             "OBJECT_ID(N'tempdb..#Rates')) DROP TABLE #Rates\n"
             "SELECT a INTO #Rates FROM r\nSELECT * FROM #Rates")
    sql = one_line(r.sql)
    assert "DROP TABLE IF EXISTS pg_temp.Rates" in sql
    assert "CREATE TEMPORARY TABLE Rates AS SELECT" in sql
    assert_usable(r)


def test_select_into_temp_gets_create_as_and_guard():
    r = conv("SELECT a INTO #tmp FROM t WHERE x = 1; SELECT * FROM #tmp")
    sql = one_line(r.sql)
    assert sql.startswith("DROP TABLE IF EXISTS pg_temp.tmp;")
    assert "CREATE TEMPORARY TABLE tmp AS SELECT" in sql
    assert "R006" not in codes(r)
    assert_usable(r)


def test_drop_temp_table_is_qualified_with_pg_temp():
    r = conv("CREATE TABLE #t (a INT); DROP TABLE #t")
    assert one_line(r.sql).endswith("DROP TABLE IF EXISTS pg_temp.t;")


def test_guard_is_not_duplicated_when_already_present():
    r = conv("DROP TABLE IF EXISTS #t; CREATE TABLE #t (a INT)")
    assert one_line(r.sql).count("DROP TABLE IF EXISTS pg_temp.t") == 1


# ---------------------------------------------------------------- A028
def test_openjson_with_clause_normalises_object_or_array():
    r = conv("SELECT j.Id FROM OPENJSON(@j) WITH (Id INT '$.id', Name NVARCHAR(50) '$.name') j")
    sql = one_line(r.sql)
    assert "OPENJSON" not in sql
    assert "jsonb_array_elements(" in sql
    assert "jsonb_build_array(" in sql          # single object -> one row
    assert "IS NOT JSON THEN '[]'::jsonb" in sql
    assert "jsonb_extract_path_text(e.value, 'id')" in sql
    assert_usable(r)


def test_openjson_path_and_bracketed_camelcase_key():
    r = conv("SELECT j.Id FROM a CROSS APPLY OPENJSON(a.Payload, '$.items') "
             "WITH (Id INT '$.id', [parentNumber] VARCHAR(20)) j")
    sql = one_line(r.sql)
    assert "#> '{items}'" in sql
    # the JSON key keeps its case; since v2.14.0 (A051) the bracketed column
    # name itself is unbracketed like every other [name] -- AS parentNumber
    assert "'parentNumber'" in sql and "AS parentNumber" in sql
    assert "LATERAL" in sql
    assert_usable(r)


def test_openjson_without_with_returns_key_value():
    r = conv("SELECT value FROM OPENJSON(@ids)")
    sql = one_line(r.sql)
    assert "jsonb_array_elements_text" in sql and "jsonb_each_text" in sql
    assert_usable(r)


def test_openjson_complex_source_is_refused():
    r = conv("SELECT value FROM OPENJSON((SELECT x FROM y))")
    assert not r.is_usable
    assert "A028" in errors(r) or "R001" in errors(r)


# ---------------------------------------------------------------- A029
def test_welded_alias_is_split_when_qualifier_matches():
    r = conv("SELECT a FROM t1 X INNER JOIN t2 YON Y.id = X.id")
    assert "A029" in codes(r)
    assert_usable(r)


def test_welded_nolock_on():
    r = conv("SELECT a FROM t1 X WITH(NOLOCK)INNER JOIN t2 Y WITH(NOLOCK)ON Y.id = X.id")
    assert_usable(r)


def test_ordinary_alias_ending_in_on_is_not_split():
    ctx = pgfixes.Context()
    sql = "SELECT a FROM Person PERSON JOIN t ON PERSON.id = t.id"
    assert pgfixes.fix_welded_on(sql, ctx) == sql


# ---------------------------------------------------------------- A031
def test_single_assignment_declares_are_inlined():
    r = conv("DECLARE @OrgId INT = @OrgIdParam\nDECLARE @From DATE\n"
             "SET @From = DATEADD(day, -7, GETDATE())\n"
             "SELECT a FROM t WHERE OrgId = @OrgId AND d >= @From")
    sql = one_line(r.sql)
    assert "DECLARE" not in sql.upper()
    assert "@OrgIdParam" in sql
    assert "R002" not in codes(r)
    assert_usable(r)


def test_reassigned_variable_is_refused():
    r = conv("DECLARE @x INT = 1; SET @x = @x + 1; SELECT @x")
    assert not r.is_usable


def test_multi_variable_declare_is_refused():
    r = conv("DECLARE @a INT = 1, @b INT = 2; SELECT @a + @b")
    assert not r.is_usable


def test_subquery_init_with_dml_in_batch_is_refused():
    r = conv("DECLARE @n INT = (SELECT COUNT(*) FROM t)\nINSERT INTO t (a) VALUES (1)\n"
             "SELECT @n")
    assert "A031" not in codes(r)


# ---------------------------------------------------------------- A032
def test_datefromparts_and_year_month_day():
    r = conv("SELECT DATEFROMPARTS(YEAR(x), MONTH(x), 1) AS d, DAY(x) AS dd FROM t")
    sql = one_line(r.sql)
    assert "DATE_FROM_PARTS" not in sql and "YEAR(" not in sql.replace("EXTRACT(YEAR", "")
    assert "MAKE_DATE(CAST(EXTRACT(YEAR FROM x) AS INT)" in sql
    assert_usable(r)


def test_datetimefromparts_six_args():
    r = conv("SELECT DATETIMEFROMPARTS(2026,1,2,3,4,5,0) AS d")
    assert "MAKE_TIMESTAMP(2026, 1, 2, 3, 4, 5)" in r.sql
    assert_usable(r)


def test_sysdatetimeoffset():
    r = conv("SELECT SYSDATETIMEOFFSET() AS d")
    assert "L_T_Z" not in r.sql and "CURRENT_TIMESTAMP" in r.sql


# ---------------------------------------------------------------- A033
def test_types_sqlglot_mapped_wrongly():
    r = conv("SELECT CAST(a AS FLOAT) AS a, CAST(b AS MONEY) AS b, CAST(c AS VARBINARY(MAX)) AS c, "
             "CAST(d AS SQL_VARIANT) AS d, CAST(e AS IMAGE) AS e, CAST(f AS TINYINT) AS f FROM t")
    sql = r.sql
    assert "DOUBLE PRECISION" in sql
    for bad in ("UTINYINT", "VARIANT", "IMAGE", "BYTEA(", "REAL"):
        assert bad not in sql, bad
    assert_usable(r)


def test_real_stays_real():
    assert "REAL" in conv("SELECT CAST(a AS REAL) AS a FROM t").sql


# ---------------------------------------------------------------- A034
def test_round_with_truncate_flag():
    r = conv("SELECT ROUND(a, 2, 1) AS d FROM t")
    assert "TRUNC(" in r.sql
    assert_usable(r)


# ---------------------------------------------------------------- A035
def test_datediff_day_counts_boundaries():
    r = conv("SELECT DATEDIFF(day, a, b) AS d FROM t")
    assert "CAST(b AS DATE) - CAST(a AS DATE)" in one_line(r.sql)
    assert "86400" not in r.sql


@pytest.mark.parametrize("unit,fragment", [
    ("month", "* 12"), ("year", "EXTRACT(YEAR FROM b)"),
    ("hour", "DATE_TRUNC('HOUR'"), ("mi", "DATE_TRUNC('MINUTE'"),
])
def test_datediff_other_units(unit, fragment):
    r = conv(f"SELECT DATEDIFF({unit}, a, b) AS d FROM t")
    assert fragment in one_line(r.sql)
    assert_usable(r)


def test_datediff_with_string_literal_operand_is_cast():
    r = conv("SELECT DATEDIFF(day, '2026-01-01', b) AS d FROM t")
    assert "CAST('2026-01-01' AS TIMESTAMP)" in r.sql
    assert_usable(r)


# ---------------------------------------------------------------- A036
def test_like_bracket_class_becomes_similar_to():
    r = conv("SELECT a FROM t WHERE b LIKE '[A-Z]%'")
    assert "SIMILAR TO '[A-Z]%'" in r.sql


def test_not_like_bracket_keeps_the_negation():
    r = conv("SELECT a FROM t WHERE b NOT LIKE '%[0-9]%'")
    assert "NOT b SIMILAR TO" in one_line(r.sql)


def test_plain_like_is_untouched():
    assert "LIKE 'abc%'" in conv("SELECT a FROM t WHERE b LIKE 'abc%'").sql


# ---------------------------------------------------------------- A037
def test_len_ignores_trailing_spaces():
    assert "LENGTH(RTRIM(CAST(a AS TEXT)))" in conv("SELECT LEN(a) AS n FROM t").sql


# ---------------------------------------------------------------- A038
def test_isjson_isnumeric_datalength_choose_var():
    r = conv("SELECT CHOOSE(2, 'a', 'b') AS c, VAR(x) AS v FROM t "
             "WHERE ISJSON(a) = 1 AND ISNUMERIC(b) = 1 AND DATALENGTH(c) > 0")
    sql = one_line(r.sql)
    assert "IS JSON" in sql and "ISJSON" not in sql
    assert "ISNUMERIC" not in sql and "~ '^" in sql
    assert "OCTET_LENGTH" in sql and "VAR_SAMP" in sql
    assert "CASE 2 WHEN 1 THEN 'a' WHEN 2 THEN 'b' END" in sql
    assert_usable(r)


def test_isjson_equals_zero_is_negated():
    assert "NOT CAST(a AS TEXT) IS JSON" in one_line(conv("SELECT a FROM t WHERE ISJSON(a) = 0").sql)


# ---------------------------------------------------------------- A039
def test_output_inserted_becomes_returning():
    r = conv("INSERT INTO t (a) OUTPUT INSERTED.Id VALUES (1)")
    assert one_line(r.sql).endswith("RETURNING Id;")
    assert_usable(r)


def test_delete_output_deleted_parses_and_returns():
    r = conv("DELETE FROM t OUTPUT DELETED.Id WHERE a = 1")
    assert one_line(r.sql) == "DELETE FROM t WHERE a = 1 RETURNING Id;"
    assert_usable(r)


def test_update_output_inserted():
    r = conv("UPDATE t SET a = 1 OUTPUT INSERTED.a WHERE b = 2")
    assert "RETURNING a" in r.sql and "INSERTED" not in r.sql


# ---------------------------------------------------------------- A041
def test_union_order_by_qualified_column():
    r = conv("SELECT a.x FROM a UNION SELECT b.x FROM b ORDER BY a.x")
    assert "ORDER BY x" in one_line(r.sql)
    assert "ORDER BY a.x" not in one_line(r.sql)


def test_union_order_by_uses_output_alias():
    r = conv("SELECT a.x AS Code FROM a UNION ALL SELECT b.y FROM b ORDER BY a.x")
    assert "ORDER BY Code" in one_line(r.sql)


# ---------------------------------------------------------------- A042
def test_pivot_becomes_conditional_aggregation():
    r = conv("SELECT * FROM (SELECT a, b, c FROM t) s PIVOT (SUM(c) FOR b IN ([X],[Y])) p")
    sql = one_line(r.sql)
    assert "SUM(CASE WHEN b = 'X' THEN c END) AS \"X\"" in sql
    assert "GROUP BY a" in sql
    assert "SG-UNSUPPORTED" not in codes(r)
    assert_usable(r)


# ---------------------------------------------------------------- A043
def test_session_options_and_query_hints_are_removed():
    r = conv("SET NOCOUNT ON; SET TRANSACTION ISOLATION LEVEL READ UNCOMMITTED; "
             "SELECT a FROM t WHERE b = 1 OPTION (RECOMPILE, MAXDOP 1)")
    assert one_line(r.sql) == "SELECT a FROM t WHERE b = 1;"
    assert_usable(r)


def test_set_dateformat_is_an_error():
    assert "L029" in errors(conv("SET DATEFORMAT dmy; SELECT 1"))


# ---------------------------------------------------------------- A044
def test_scope_identity_after_insert():
    r = conv("INSERT INTO t (a) VALUES (1); SELECT SCOPE_IDENTITY()")
    assert "LASTVAL()" in r.sql
    assert_usable(r)


def test_at_at_identity_after_insert():
    assert "LASTVAL()" in conv("INSERT INTO t (a) VALUES (1); SELECT @@IDENTITY").sql


# ---------------------------------------------------------------- A045
def test_table_variable_becomes_temp_table():
    r = conv("DECLARE @t TABLE (a INT); INSERT INTO @t SELECT 1; SELECT * FROM @t")
    sql = one_line(r.sql)
    assert "CREATE TEMPORARY TABLE tv_t" in sql
    assert "FROM tv_t" in sql and "@t" not in sql
    assert_usable(r)


# ---------------------------------------------------------------- A046
@pytest.mark.parametrize("style,mask", [
    (103, "DD/MM/YYYY"), (105, "DD-MM-YYYY"), (120, "YYYY-MM-DD HH24:MI:SS"),
    (121, "YYYY-MM-DD HH24:MI:SS.MS"), (114, "HH24:MI:SS:MS"),
    (126, 'YYYY-MM-DD"T"HH24:MI:SS.MS'), (106, "DD Mon YYYY"), (23, "YYYY-MM-DD"),
])
def test_convert_style_to_char(style, mask):
    r = conv(f"SELECT CONVERT(VARCHAR(30), a, {style}) AS d FROM t")
    assert f"TO_CHAR(a, '{mask}')" in r.sql
    for bad in ("%p", "None", ".US", "DDTHH"):
        assert bad not in r.sql
    assert_usable(r)


def test_convert_parse_with_parameter():
    r = conv("SELECT CONVERT(DATETIME, @d, 105) AS a, CONVERT(DATE, @d, 23) AS b")
    sql = one_line(r.sql)
    assert "TO_TIMESTAMP(@d, 'DD-MM-YYYY')" in sql and "CAST(@d AS DATE)" in sql
    assert_usable(r)


def test_convert_date_of_column_with_style_is_left_for_a_human():
    r = conv("SELECT CONVERT(DATE, x.CreatedDate, 103) AS a FROM x")
    assert "R003" in errors(r)


def test_convert_unknown_style_is_refused():
    assert "R003" in errors(conv("SELECT CONVERT(VARCHAR(30), a, 131) AS a FROM t"))


# ---------------------------------------------------------------- A047
def test_start_of_month_idiom():
    r = conv("SELECT DATEADD(month, DATEDIFF(month, 0, GETDATE()), 0) AS d")
    assert "DATE_TRUNC('month', CURRENT_TIMESTAMP)" in r.sql
    assert "0 +" not in r.sql
    assert_usable(r)


# ---------------------------------------------------------------- A048
def test_datepart_weekday_is_sunday_one():
    r = conv("SELECT DATEPART(dw, x) AS a, DATEPART(dayofyear, x) AS b, DATEPART(ms, x) AS c FROM t")
    sql = one_line(r.sql)
    assert "CAST(EXTRACT(DOW FROM x) AS INT) + 1" in sql
    assert "EXTRACT(DOY FROM x)" in sql and "% 1000" in sql
    assert "DAYOFWEEK" not in sql
    assert_usable(r)


# ---------------------------------------------------------------- A049
def test_getdate_minus_integer_is_days():
    r = conv("SELECT GETDATE() - 1 AS d")
    assert "INTERVAL '1' DAY" in r.sql or "INTERVAL '1 DAY'" in r.sql
    assert_usable(r)


# ---------------------------------------------------------------- A050
def test_datename_is_not_blank_padded():
    r = conv("SELECT DATENAME(weekday, x) AS a, DATENAME(month, x) AS b FROM t")
    assert "'FMDay'" in r.sql and "'FMMonth'" in r.sql and "'TM" not in r.sql


def test_with_rollup():
    r = conv("SELECT a, b, COUNT(*) AS n FROM t GROUP BY a, b WITH ROLLUP")
    assert "GROUP BY ROLLUP (a," in one_line(r.sql)
    assert_usable(r)


# ---------------------------------------------------------------- A024
def test_boolean_list_drives_coalesce_fix(tmp_path, monkeypatch):
    listing = tmp_path / "bools.txt"
    listing.write_text("# comment\nentrystatus\n", encoding="utf-8")
    monkeypatch.setenv(schema_hints.ENV_VAR, str(listing))
    schema_hints.reset()
    try:
        r = conv("SELECT a FROM t WHERE COALESCE(EM.ENTRYSTATUS, 0) = 1")
        assert "COALESCE(EM.ENTRYSTATUS, FALSE) = TRUE" in one_line(r.sql)
    finally:
        schema_hints.reset()


def test_schema_list_parser_tolerates_query_output():
    names = schema_hints.parse("column_name\n| IsActive |\n\nEntryStatus  # note\n")
    assert names == {"isactive", "entrystatus"}


# ---------------------------------------------------------------- lint
def test_l021_boolean_parameter_compared_to_zero():
    assert "L021" in codes(conv("SELECT a FROM t WHERE @IsOutbound = 0"))


def test_l022_empty_string_against_date_column():
    assert "L022" in codes(conv("SELECT a FROM t WHERE CreatedDate = ''"))


def test_l022_does_not_fire_on_reason():
    assert "L022" not in codes(conv("SELECT a FROM t WHERE Reason = ''"))


def test_l023_reserved_alias():
    assert "L023" in errors(conv("SELECT x AS LocalTime FROM t"))


def test_l025_case_sensitive_lookup():
    assert "L025" in codes(conv("SELECT a FROM t WHERE ScreenName = 'NewOrder'"))


def test_l026_plus_literal():
    assert "L026" in codes(conv("SELECT CASE WHEN a = 1 THEN '+' ELSE '-' END AS s FROM t"))


def test_l027_top_one_without_order():
    assert "L027" in codes(conv("SELECT a FROM t WHERE b = (SELECT TOP 1 c FROM u)"))
    assert "L027" not in codes(conv("SELECT a FROM t WHERE b = (SELECT TOP 1 c FROM u ORDER BY c)"))


def test_r008_bad_mask_in_output():
    warnings = pgfixes.lint("", "SELECT TO_CHAR(a, 'YYYY-MM-DDTHH24:MI') FROM t")
    assert any(w.code == "R008" for w in warnings)


# ---------------------------------------------------------------- R007
@pytest.mark.parametrize("sql", [
    "SELECT SWITCHOFFSET(a, '+05:30') AS d FROM t",
    "SELECT QUOTENAME(a) AS d FROM t",
])
def test_r007_or_r001_blocks_tsql_survivors(sql):
    r = conv(sql)
    assert not r.is_usable
    assert {"R001", "R007"} & errors(r)


def test_r007_is_quiet_on_clean_output():
    r = conv("SELECT a, COALESCE(b, 0) AS b FROM t WHERE c = @c")
    assert "R007" not in codes(r)


def test_r007_exec_marker_ignores_executed_columns():
    # regression: `\bEXEC\s*\w` matched ExecutedFlag / ExecutedDate (30 false
    # refusals across one module on the first real-repo dry run)
    r = conv("SELECT MAX(CASE WHEN k = 'x' THEN f END) AS ExecutedFlag, ExecutedDate FROM t")
    assert "R007" not in codes(r)
    assert_usable(r)


def test_set_nocount_equals_form_is_removed():
    # already-converted repo code carried sqlglot's `SET NOCOUNT = OFF;`
    r = conv("UPDATE t SET a = 1 WHERE b = @b; SET NOCOUNT = OFF;")
    assert "NOCOUNT" not in r.sql
    assert_usable(r)


def test_format_with_runtime_substituted_mask_placeholder():
    r = conv("SELECT FORMAT(d, '@TimeFormat') AS x FROM t")
    assert "TO_CHAR(d, '@TimeFormat')" in r.sql
    assert "L024" in codes(r)
    assert_usable(r)


def test_merge_is_a_warning_not_an_error():
    r = conv("MERGE INTO t USING s ON t.id = s.id WHEN MATCHED THEN UPDATE SET t.a = s.a;")
    assert "R002" in codes(r) and "R002" not in errors(r)


# ---------------------------------------------------------------- placeholders
def test_no_placeholder_ever_leaks():
    sqls = ["SELECT FORMAT(a, 'N2') AS x FROM t",
            "SELECT CONVERT(VARCHAR(10), a, 103) AS x FROM t",
            "SELECT value FROM OPENJSON(@x)"]
    for sql in sqls:
        assert "MSSQLTOPG_" not in conv(sql).sql


def test_all_rewrites_pass_real_grammar():
    sql = ("SELECT FORMAT(a, 'N2') AS x, DATEDIFF(day, a, b) AS y, DATEPART(dw, a) AS z "
           "FROM t WHERE b NOT LIKE '[0-9]%' ORDER BY a")
    r = conv(sql)
    assert validator.validate(r.sql).valid
