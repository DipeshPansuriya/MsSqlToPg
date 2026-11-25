"""A023 FOR XML PATH -> STRING_AGG, A016 splitter glue, L019/L020 findings.

Every case here is modelled on a query hand-converted on 2026-08-21 that
the tool could not do on its own.
"""
from core.autofix import (
    find_for_json,
    find_params_in_comments,
    rewrite_for_xml_path,
)
from core.orchestrator import convert


# -- A023 ------------------------------------------------------------------

def test_for_xml_idiom_becomes_string_agg():
    sql = ("SELECT STUFF((SELECT ', ' + s.Code FROM s WHERE s.Id = t.Id "
           "FOR XML PATH('')), 1, 2, '') AS L FROM t;")
    out, count = rewrite_for_xml_path(sql)
    assert count == 1
    assert "STRING_AGG(s.Code, ', ')" in out
    assert "FOR XML" not in out.upper()
    assert "STUFF" not in out.upper()


def test_for_xml_distinct_and_order_by_are_carried_into_the_aggregate():
    # a DISTINCT tag list column, ordered inside the aggregate.
    sql = ("SELECT COALESCE(STUFF((select distinct ', ' + CONVERT(TEXT,TAG.TagId) "
           "FROM OrderTagDetail TAG WHERE TAG.OrderLineId = D.Id ORDER BY TAG.TagId "
           "for xml path('')),1,1,''),'ALL') AS TagList FROM D;")
    out, count = rewrite_for_xml_path(sql)
    assert count == 1
    assert "STRING_AGG(DISTINCT CONVERT(TEXT,TAG.TagId), ', ' ORDER BY TAG.TagId)" in out
    # the COALESCE(..., 'ALL') fallback must survive untouched
    assert "'ALL'" in out


def test_plain_stuff_is_left_alone():
    """STUFF without a FOR XML tail is a genuine string edit, not the idiom."""
    sql = "SELECT STUFF('abcdef', 2, 3, 'XYZ') AS Plain;"
    out, count = rewrite_for_xml_path(sql)
    assert count == 0
    assert out == sql


def test_paren_matching_survives_parens_inside_string_literals():
    sql = ("SELECT STUFF((SELECT ', ' + REPLACE(s.Code, ')(', '') FROM s "
           "FOR XML PATH('')), 1, 2, '') AS L FROM t;")
    out, count = rewrite_for_xml_path(sql)
    assert count == 1
    assert "STRING_AGG(REPLACE(s.Code, ')(', ''), ', ')" in out


def test_two_idioms_in_one_query_both_rewrite():
    sql = ("SELECT STUFF((SELECT ', ' + a.C FROM a FOR XML PATH('')),1,2,'') AS A, "
           "STUFF((SELECT ', ' + b.C FROM b FOR XML PATH('')),1,2,'') AS B;")
    out, count = rewrite_for_xml_path(sql)
    assert count == 2
    assert out.upper().count("STRING_AGG") == 2


def test_for_xml_query_now_converts_end_to_end():
    """Before A023 sqlglot reported SG-UNSUPPORTED and dropped the option."""
    sql = ("SELECT STUFF((SELECT ', ' + s.Code FROM s WHERE s.Id = t.Id "
           "FOR XML PATH('')), 1, 2, '') AS L FROM t;")
    result = convert(sql)
    assert result.ok
    assert "STRING_AGG" in result.sql.upper()
    assert not any("Unsupported query option" in str(getattr(w, "message", w))
                   for w in result.warnings)


# -- L019 ------------------------------------------------------------------

def test_for_json_is_detected_and_not_guessed():
    assert find_for_json("SELECT a AS [R.H.A] FROM t FOR JSON PATH, WITHOUT_ARRAY_WRAPPER;") == 1
    assert find_for_json("SELECT a FROM t;") == 0


# -- L020 ------------------------------------------------------------------

def test_parameter_named_inside_a_comment_is_reported():
    sql = "-- AppDbName prefix dropped, was @AppDbName\nSELECT 1;"
    assert find_params_in_comments(sql) == ["@AppDbName"]


def test_parameter_outside_a_comment_is_not_reported():
    assert find_params_in_comments("SELECT * FROM t WHERE id = @Id;") == []


# -- A016 ------------------------------------------------------------------

def _no_line_starts_a_split_keyword_mid_statement(sql: str) -> bool:
    """A keyword-based splitter cuts at a line-leading keyword and appends ' ;'."""
    import re
    return not re.search(
        r"(?im)^[ \t]*(?:UPDATE\b[^\n;]*|INSERT\s+INTO\s+\w+\s*\([^()]*\)|"
        r"CREATE\s+(?:TEMP\s+|TEMPORARY\s+)?TABLE\s+\w+\s+AS)[ \t]*\r?\n"
        r"[ \t]*(?:SELECT|SET)\b", sql)


def test_update_set_stays_on_one_line():
    result = convert("UPDATE t\nSET c = 1 FROM s WHERE s.id = t.id;")
    assert result.ok
    assert _no_line_starts_a_split_keyword_mid_statement(result.sql)


def test_insert_into_columns_and_its_select_stay_on_one_line():
    result = convert("INSERT INTO x (a, b)\nSELECT a, b FROM t;")
    assert result.ok
    assert _no_line_starts_a_split_keyword_mid_statement(result.sql)


def test_cte_close_paren_is_not_split_from_its_select():
    """`WITH c AS (...)` newline `SELECT` -- the splitter cuts there too and
    orphans the CTE. Every CTE-based hand conversion depends on this."""
    result = convert("WITH c AS (SELECT 1 AS n) SELECT n FROM c;")
    assert result.ok
    import re
    assert not re.search(r"(?m)^\s*\)\s*\r?\n\s*SELECT\b", result.sql)
