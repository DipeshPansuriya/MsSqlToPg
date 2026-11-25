"""Tests for core/resolvability.py -- the runtime-resolvability layer.

The bug these exist to prevent: a constant that PASSES the grammar validator and
still dies at execution. OPENJSON shipped twice that way.
"""

from core import resolvability
from core.models import Severity


def codes(sql, **kw):
    return [w.code for w in resolvability.check(sql, **kw)]


def messages(sql, **kw):
    return " | ".join(w.message for w in resolvability.check(sql, **kw))


# ---------------------------------------------------------------- R001
def test_openjson_is_caught_even_though_it_parses():
    """The regression that motivated the module."""
    import pglast
    sql = "SELECT value FROM OPENJSON('[]')"
    pglast.parse_sql(sql)                      # grammar says fine
    assert "R001" in codes(sql)                # resolvability says no
    assert "42883" in messages(sql)


def test_each_missing_function_is_reported():
    for fn in ("STRING_SPLIT", "GETDATE", "DATEADD", "CHARINDEX", "IIF",
               "NEWID", "SCOPE_IDENTITY", "EOMONTH", "REPLICATE"):
        sql = f"SELECT {fn}(a, b) FROM t"
        assert "R001" in codes(sql), fn


def test_functions_postgresql_actually_has_are_not_flagged():
    """These were wrongly 'fixed' by earlier sweeps -- must stay silent."""
    sql = ("SELECT LTRIM(RTRIM(a)), BTRIM(b), UPPER(c), LOWER(d), "
           "SUBSTRING(e FROM 1 FOR 2), REPLACE(f,'x','y'), COALESCE(g,0), "
           "NULLIF(h,0), ABS(i), ROUND(j,2), TRIM(k) FROM t")
    assert "R001" not in codes(sql)


def test_sys_catalog_and_dbo_prefix():
    assert "R001" in codes("SELECT current_utc_offset FROM sys.time_zone_info")
    assert "R001" in codes("SELECT * FROM dbo.Split('a,b', ',')")


def test_function_name_inside_a_comment_is_ignored():
    assert "R001" not in codes("SELECT 1 -- was GETDATE() before conversion")
    assert "R001" not in codes("SELECT 1 /* DATEADD(day,1,x) */ FROM t")


def test_function_name_inside_a_string_literal_is_ignored():
    assert "R001" not in codes("SELECT 'ISNULL(a,b)' AS note FROM t")


# ---------------------------------------------------------------- R002
def test_control_flow_is_reported():
    for sql in ("IF EXISTS (SELECT 1 FROM t) SELECT 1",
                "IF (@x = 1)\nSELECT 1",
                "SET @total = 5",
                "WHILE (@i < 10) SELECT 1",
                "DECLARE @x INT",
                "MERGE INTO t USING s ON t.id = s.id",
                "RAISERROR('x', 16, 1)"):
        assert "R002" in codes(sql), sql


def test_control_flow_drives_the_rewrite_message():
    assert "REWRITE" in messages("IF EXISTS (SELECT 1 FROM t) SELECT 1")


# ---------------------------------------------------------------- R003
def test_three_arg_convert_names_the_actual_date_format():
    msg = messages("SELECT CONVERT(date, x, 105) FROM t")
    assert "R003" in codes("SELECT CONVERT(date, x, 105) FROM t")
    assert "dd-mm-yyyy" in msg
    assert "WRONG" in msg


def test_two_arg_convert_is_not_flagged_by_r003():
    """Two-arg CONVERT is safe to automate; only the 3-arg form is not."""
    out = [w for w in resolvability.check("SELECT CONVERT(date, x) FROM t")
           if w.code == "R003"]
    assert out == []


def test_convert_bit_is_flagged():
    assert "R003" in codes("SELECT CONVERT(bit, x) FROM t")


def test_select_top_warns_about_placement():
    ws = [w for w in resolvability.check("SELECT TOP 1 a FROM t") if w.code == "R003"]
    assert ws and ws[0].severity is Severity.WARNING
    assert "DIFFERENT row" in ws[0].message


# ---------------------------------------------------------------- R004
def test_stuff_over_subquery_is_the_for_xml_remnant():
    sql = "SELECT (STUFF((SELECT ', ' || c FROM x), 1, 1, '')) FROM t"
    assert "R004" in codes(sql)
    assert "21000" in messages(sql)


# ---------------------------------------------------------------- R005
def test_cast_inside_case_branch_is_not_guarded():
    sql = "SELECT json_array_elements_text(CASE WHEN x LIKE '[%' THEN x::json ELSE '[]'::json END)"
    assert "R005" in codes(sql)


# ---------------------------------------------------------------- R006
def test_temp_table_without_drop_guard():
    assert "R006" in codes("CREATE TEMP TABLE OrderGuids (a int)")


def test_temp_table_with_drop_guard_is_clean():
    sql = ("DROP TABLE IF EXISTS pg_temp.OrderGuids;\n"
           "CREATE TEMP TABLE OrderGuids (a int)")
    assert "R006" not in codes(sql)


# ---------------------------------------------------------------- fragments
def test_fragment_names_are_recognised():
    assert resolvability.is_probable_fragment("GetOrderList_Preamble")
    assert resolvability.is_probable_fragment("GetOutboundGetAllV3_Template")
    assert resolvability.is_probable_fragment("ColMap_TotalGrossWgtUnit")
    assert not resolvability.is_probable_fragment("GetAllBookingInbound")


# ---------------------------------------------------------------- misc
def test_empty_input_is_safe():
    assert resolvability.check("") == []
    assert resolvability.check("   ") == []


def test_summary_counts_only_r_codes():
    ws = resolvability.check("SELECT OPENJSON(x), CONVERT(bit, y) FROM t")
    s = resolvability.summary(ws)
    assert s.get("R001") == 1
    assert s.get("R003") == 1


# ---------------------------------------------------------------- source-side
# Regression: sqlglot cannot represent T-SQL control flow. Given
# "IF EXISTS (...) SELECT ..." it returns an EMPTY string with ok=True, so a
# check hooked only onto the OUTPUT saw nothing and R002/R003 never fired on the
# constants that need them most. These assert the source-side pass.

def test_control_flow_is_reported_even_when_the_engine_returns_nothing():
    from core.orchestrator import convert
    r = convert("IF EXISTS (SELECT 1 FROM t) SELECT 1")
    assert r.sql.strip() == "", "engine unexpectedly produced output"
    assert "R002" in [w.code for w in r.warnings]


def test_three_arg_convert_reported_from_source_when_engine_gives_up():
    from core.orchestrator import convert
    r = convert("IF EXISTS (SELECT 1 FROM t) SELECT CONVERT(date, x, 105) FROM t")
    codes = [w.code for w in r.warnings]
    assert "R002" in codes and "R003" in codes


def test_check_source_excludes_r001_by_design():
    """The input is T-SQL, so ISNULL/GETDATE are expected there -- not defects."""
    ws = resolvability.check_source("SELECT ISNULL(a, 0), GETDATE() FROM t")
    assert [w.code for w in ws if w.code == "R001"] == []


def test_check_source_is_safe_on_empty():
    assert resolvability.check_source("") == []


def test_clean_query_produces_no_resolvability_noise():
    from core.orchestrator import convert
    r = convert("SELECT a FROM t WHERE b = 1")
    assert [w.code for w in r.warnings if w.code.startswith("R")] == []
