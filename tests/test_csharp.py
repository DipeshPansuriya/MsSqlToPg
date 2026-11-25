"""C# SQL-literal extraction tests.

This module drives IN-PLACE source rewriting, so the cost of a false positive is
corrupted customer code. The tests below are mostly about what must NOT be
matched.
"""

import pytest

from core import csharp as _csharp_for_leading_semicolon


def test_leading_semicolon_cte_is_recognised_as_sql():
    """`;WITH cte AS (...)` is the standard T-SQL defensive CTE idiom. It used
    to fail the is-this-SQL shape test, so the literal was never offered to the
    converter and never reported -- a silent miss."""
    src = '''class Q { public static readonly string A = @"
        ;WITH TagWithGroupCTE AS (
            SELECT Code FROM Ref_Tag
        )
        SELECT Code FROM TagWithGroupCTE"; }'''
    found = _csharp_for_leading_semicolon.find_sql_literals(src)
    assert len(found) == 1, found
    assert "TagWithGroupCTE" in found[0].sql


def test_leading_semicolon_alone_is_not_sql():
    src = 'class Q { public static readonly string A = @";  just text, no keywords"; }'
    assert _csharp_for_leading_semicolon.find_sql_literals(src) == []

from core import csharp


def sqls(text):
    return [lit.sql for lit in csharp.find_sql_literals(text)]


# --------------------------------------------------------------------------
# Recognising SQL
# --------------------------------------------------------------------------

def test_finds_verbatim_string():
    text = 'var q = @"SELECT a FROM t";'
    found = csharp.find_sql_literals(text)
    assert len(found) == 1
    assert found[0].kind == csharp.VERBATIM
    assert found[0].sql == "SELECT a FROM t"


def test_verbatim_doubled_quotes_are_decoded():
    text = 'var q = @"SELECT ""Col"" FROM t";'
    assert sqls(text) == ['SELECT "Col" FROM t']


def test_finds_regular_string():
    text = 'var q = "SELECT a FROM t";'
    found = csharp.find_sql_literals(text)
    assert found and found[0].kind == csharp.REGULAR


def test_finds_raw_string():
    text = 'var q = """\nSELECT a FROM t\n""";'
    found = csharp.find_sql_literals(text)
    assert found and found[0].kind == csharp.RAW
    assert "SELECT a FROM t" in found[0].sql


def test_reports_line_numbers():
    text = 'class C\n{\n    var q = @"SELECT a FROM t";\n}'
    assert csharp.find_sql_literals(text)[0].line == 3


# --------------------------------------------------------------------------
# What must NOT be matched -- these protect customer source
# --------------------------------------------------------------------------

def test_plain_message_is_not_sql():
    assert sqls('var m = "just a message, definitely not SQL";') == []


def test_select_without_structure_is_not_sql():
    """'SELECT a name' is prose, not a query."""
    assert sqls('var m = "Please SELECT a name";') == []


def test_sql_inside_a_line_comment_is_ignored():
    assert sqls('// var q = @"SELECT a FROM t";\nint x = 1;') == []


def test_sql_inside_a_block_comment_is_ignored():
    assert sqls('/* @"SELECT a FROM t" */ int x = 1;') == []


def test_quote_inside_char_literal_does_not_desync_the_scanner():
    text = "char c = '\"'; var q = @\"SELECT a FROM t\";"
    assert sqls(text) == ["SELECT a FROM t"]


def test_escaped_quote_in_regular_string_is_handled():
    text = 'var m = "he said \\"hi\\""; var q = @"SELECT a FROM t";'
    assert sqls(text) == ["SELECT a FROM t"]


def test_regular_string_cannot_span_lines():
    assert sqls('var m = "SELECT a\nFROM t";') == []


# --------------------------------------------------------------------------
# Interpolated strings -- detected, never rewritten
# --------------------------------------------------------------------------

def test_interpolated_string_is_flagged():
    found = csharp.find_sql_literals('var q = $@"SELECT a FROM {table}";')
    assert found and found[0].interpolated


def test_dollar_at_order_both_ways():
    for prefix in ("$@", "@$"):
        found = csharp.find_sql_literals(f'var q = {prefix}"SELECT a FROM t";')
        assert found and found[0].interpolated, prefix


def test_plain_verbatim_is_not_interpolated():
    found = csharp.find_sql_literals('var q = @"SELECT a FROM t";')
    assert not found[0].interpolated


# --------------------------------------------------------------------------
# Encoding back into C#
# --------------------------------------------------------------------------

@pytest.mark.parametrize("kind", [csharp.VERBATIM, csharp.REGULAR, csharp.RAW])
def test_encode_decode_round_trip(kind):
    sql = 'SELECT "Col" FROM t WHERE a = \'x\''
    literal = csharp.encode(kind, sql)
    found = csharp.find_sql_literals(f"var q = {literal};")
    assert found, f"{kind} literal did not re-parse"
    assert found[0].sql.strip() == sql


def test_raw_fence_widens_when_sql_contains_triple_quote():
    sql = 'SELECT """ FROM t'
    out = csharp.encode(csharp.RAW, sql)
    assert out.startswith('""""')


# --------------------------------------------------------------------------
# Rewriting
# --------------------------------------------------------------------------

def test_replace_literals_preserves_surrounding_code():
    text = 'A(); var q = @"SELECT a FROM t"; B();'
    literal = csharp.find_sql_literals(text)[0]
    out = csharp.replace_literals(text, [(literal, '@"SELECT a FROM t LIMIT 1"')])
    assert out.startswith("A(); var q = ")
    assert out.endswith("; B();")
    assert "LIMIT 1" in out


def test_multiple_replacements_do_not_corrupt_offsets():
    text = 'var a = @"SELECT x FROM t"; var b = @"SELECT y FROM u";'
    literals = csharp.find_sql_literals(text)
    assert len(literals) == 2
    out = csharp.replace_literals(
        text, [(literals[0], '@"ONE"'), (literals[1], '@"TWO"')])
    assert '@"ONE"' in out and '@"TWO"' in out
    assert "SELECT" not in out


def test_reindent_matches_surrounding_style():
    text = '    var q = @"SELECT 1";'
    indent = csharp.line_indent(text, text.index("@")) + "    "
    assert indent == "        "
    out = csharp.reindent("SELECT\n  a\nFROM t", indent)
    assert out.startswith("\n        SELECT")
    assert "\n          a" in out


# --------------------------------------------------------------------------
# SQL fragments -- reported, never rewritten
# --------------------------------------------------------------------------
# A fragment is a literal C# splices into a larger query: a scalar subquery, a
# CASE expression, a column mapping. It fails the whole-statement shape test,
# so before this it was neither converted NOR reported -- yet the T-SQL inside
# it still reached the database. JobQueries.cs had 6 of them.

def test_colmap_scalar_subquery_is_a_fragment():
    sql = ("(SELECT TOP 1 OrgName FROM Organization WITH(NOLOCK) "
           "WHERE OrgId = Job2.HandlerId AND ISNULL(IsDeleted,0) = 0)")
    assert csharp.looks_like_sql_fragment(sql)
    assert "WITH (NOLOCK)" in csharp.tsql_markers(sql)
    assert "TOP n" in csharp.tsql_markers(sql)


def test_case_expression_fragment_is_detected():
    assert csharp.looks_like_sql_fragment(
        "CASE WHEN ISNULL(Job.IsGroupJob,0)=1 THEN 'Consol' ELSE 'Direct' END")


def test_bare_predicate_fragment_is_detected():
    """No clause keyword at all -- qualifies via the comparison operator."""
    assert csharp.looks_like_sql_fragment("ISNULL(a,0)=0 AND b=1")


def test_clean_ansi_fragment_is_not_reported():
    """Nothing to convert -- a finding here would be pure noise."""
    assert not csharp.looks_like_sql_fragment(
        "Job.ShippedDate >= @InputDateFrom AND Job.ShippedDate < @InputDateTo")
    assert not csharp.looks_like_sql_fragment(
        "CONCAT(Job.Prefix, '-', Job.ParentNumberId)")


def test_full_statement_is_not_a_fragment():
    """The two scans must not both claim the same literal."""
    sql = "SELECT a FROM t WHERE ISNULL(x,0)=0"
    assert csharp.looks_like_sql(sql)
    assert not csharp.looks_like_sql_fragment(sql)


def test_prose_is_never_a_fragment():
    """Regression: '[Enter]' matched the bracket marker and 'and' matched the
    old hint, so an ordinary message was reported as T-SQL."""
    assert not csharp.looks_like_sql_fragment("Press [Enter] and we will send it")
    assert not csharp.looks_like_sql_fragment("just a message, not SQL")
    assert not csharp.looks_like_sql_fragment("1=1")
    assert not csharp.looks_like_sql_fragment("")


def test_find_sql_fragments_skips_comments():
    src = '// var x = "(SELECT TOP 1 a FROM t WITH(NOLOCK))";\nint y = 1;'
    assert csharp.find_sql_fragments(src) == []


def test_find_sql_fragments_reports_line_numbers():
    src = ('class C\n{\n    const string M = '
           '"(SELECT TOP 1 a FROM t WITH(NOLOCK))";\n}')
    found = csharp.find_sql_fragments(src)
    assert len(found) == 1 and found[0].line == 3
