"""v2.14.0 -- bracket identifiers (A051/PC10), missing statement separators in
already-converted SQL (PC09), and parameters buried inside string literals
(A052/PC11/L030).

All three were found on 2026-10-07 converting a real API: every case below
parsed as VALID PostgreSQL and then failed at runtime (or silently returned
the wrong rows), so the validator alone never caught them.
"""

from __future__ import annotations

from pathlib import Path

from core import orchestrator, pgcheck, validator
from core.models import Severity


def convert(sql):
    return orchestrator.convert(sql)


def flat(sql):
    return " ".join(sql.split())


def codes(result):
    return {w.code for w in result.warnings}


def errors(result):
    return [w.code for w in result.warnings if w.severity is Severity.ERROR]


# ==========================================================================
# A051 -- [bracket] identifiers in T-SQL input (Convert / Scan)
# ==========================================================================

def test_bracketed_table_and_columns_come_out_unquoted():
    """`[ER].[ItemClassCode]` used to become `"ER"."ItemClassCode"`: the alias ER is
    declared unquoted (folds to er), so "ER" is a missing FROM entry, and the
    column is itemclasscode -- valid grammar, runtime failure."""
    r = convert("SELECT [ER].[ItemClassCode] FROM [dbo].[Ref_Class] ER "
                "WHERE ISNULL([ER].[IsDeleted],0)=0")
    out = flat(r.sql)
    assert "ER.ItemClassCode" in out
    assert "FROM Ref_Class AS ER" in out
    assert '"' not in out
    assert "A051" in codes(r)
    assert r.validation and r.validation.valid


def test_placeholder_prefix_keeps_a010_and_table_is_unquoted(monkeypatch):
    monkeypatch.setenv("MSSQLTOPG_DB_PLACEHOLDER", "schema")   # pre-2.16 mode
    r = convert("SELECT a FROM [@AppDbName].[dbo].[Ref_Tax] T")
    out = flat(r.sql)
    assert '"@AppDbName".Ref_Tax' in out
    assert '"Ref_Tax"' not in out


def test_reserved_words_become_lowercase_quoted():
    """The schema generator created reserved-word columns as "order"."""
    out = flat(convert("SELECT m0.[Order], m0.[Group], [User] FROM Ref_Seq m0").sql)
    assert 'm0."order"' in out and 'm0."group"' in out and '"user"' in out


def test_names_that_need_quoting_keep_their_text():
    out = flat(convert("SELECT MZ.AreaName AS [Zone Name] FROM Ref_Area MZ").sql)
    assert 'AS "Zone Name"' in out


def test_bracketed_alias_with_spaces_still_parses():
    """sqlglot's T-SQL reader refuses `AS "Is Full Day"`, so names that must
    stay quoted are left in brackets for it (it emits the quoted form)."""
    r = convert("SELECT e.LOC_IsActive_BT AS [Is Active], e.LOC_IsFullDay_BT AS [Is Full Day] "
                "FROM CAL_HolidayList e")
    assert r.validation and r.validation.valid, errors(r)
    assert 'AS "Is Full Day"' in flat(r.sql)


def test_glued_bracket_alias_gets_its_space():
    out = flat(convert("SELECT MTP.SiteArea[SiteAreaId], COUNT(*)[Total] "
                       "FROM Ref_Site_Printing MTP GROUP BY MTP.SiteArea").sql)
    assert "MTP.SiteArea AS SiteAreaId" in out
    assert "AS Total" in out


def test_bracketed_alias_and_bare_reference_now_agree():
    """`AS [Status]` became "Status" while `TMP.Status` folds to status ->
    column tmp.status does not exist."""
    r = convert("SELECT TMP.Status FROM (SELECT 'A' AS [Status]) TMP ORDER BY [Status]")
    assert '"Status"' not in r.sql
    assert r.validation and r.validation.valid


def test_brackets_inside_string_literal_are_untouched():
    out = convert("SELECT '[Order]' AS x, a FROM t").sql
    assert "'[Order]'" in out


def test_like_character_class_is_left_to_a036():
    out = flat(convert("SELECT * FROM T WHERE Code LIKE '[A-Z]%'").sql)
    assert "SIMILAR TO '[A-Z]%'" in out


def test_brackets_in_comments_are_untouched():
    sql = "SELECT a FROM t -- see [Order] column\n"
    r = convert(sql)
    assert "A051" not in codes(r)


def test_no_brackets_no_a051():
    assert "A051" not in codes(convert("SELECT a FROM t"))


# ==========================================================================
# A018 -- terminator before a TRAILING comment on a code line
# ==========================================================================

def test_terminator_goes_before_a_comment_at_the_end_of_a_code_line():
    """Searchalliance regression: `... LIKE '%' + '@x' + '%' --GROUP BY y`
    got its `;` appended AFTER the comment, where it vanished; the next SELECT
    ran into this one and the whole batch was refused (SG-PARSE)."""
    from core import autofix
    fixed, n = autofix.infer_statement_terminators(
        "SELECT a FROM t WHERE b LIKE '%' + '@x' + '%' --GROUP BY a\n"
        "SELECT c FROM u")
    assert n == 1
    assert fixed.splitlines()[0] == "SELECT a FROM t WHERE b LIKE '%' + '@x' + '%'; --GROUP BY a"


def test_comment_after_a_semicolon_stays_with_its_statement():
    """sqlglot returns `; -- note` as a statement of its own, which came out as
    an empty `/* note */;` statement between the real ones."""
    r = convert("SELECT a FROM t; -- note\nSELECT b FROM u;")
    assert ";\n\n/* note */" not in r.sql            # not a statement of its own
    assert "FROM t\n/* note */;" in r.sql
    assert r.sql.count(";") == 2
    assert r.validation and r.validation.valid


def test_comment_only_input_is_left_alone():
    r = convert("-- just a note")
    assert r.ok and "*/;" not in r.sql


def test_batch_with_trailing_comment_converts():
    r = convert("SELECT * INTO #T FROM (SELECT a FROM x) AS TC\n"
                "SELECT a FROM #T WHERE a LIKE '%' + 'q' + '%' --GROUP BY a\n"
                "SELECT b FROM #T\n"
                "DROP TABLE #T")
    assert r.ok and r.validation and r.validation.valid, errors(r)


# ==========================================================================
# A023 -- STUFF length written as LEN(',')
# ==========================================================================

def test_for_xml_stuff_with_len_length_argument_is_rewritten():
    """Getproducttypesearch: STUFF((...FOR XML PATH('')),1,LEN(','),'')
    was refused (the comma inside LEN(',') broke the argument match), so
    sqlglot dropped FOR XML and left STUFF over a multi-row subquery."""
    r = convert("SELECT ISNULL((select STUFF((select ', ' + MS.TagCode from Ref_Tag MS "
                "WHERE MS.Id = P.Id ORDER BY MS.Priority DESC for xml path('')),1,LEN(','),'')),'All') AS TagCode "
                "FROM Ref_ItemType P")
    out = flat(r.sql)
    assert "STRING_AGG(MS.TagCode, ', ' ORDER BY MS.Priority DESC" in out
    assert "STUFF" not in out.upper()
    assert r.validation and r.validation.valid


def test_for_xml_subquery_ending_in_a_comment_keeps_its_close():
    """The rewrite appends ')' after the subquery; a trailing `--` comment
    swallowed it (WorkLocation: ... = 0 -- AND HL.ClassCodeId = MH.ClassCodeId)."""
    r = convert("SELECT ISNULL((select STUFF((select ', ' + OP.Loc from OP\n"
                "WHERE ISNULL(OP.IsDeleted,0)=0 -- AND OP.Code = M.Code\n"
                "for xml path('')),1,LEN(','),'')),'ALL') as [Loc] FROM M")
    assert r.validation and r.validation.valid, errors(r)
    assert "STRING_AGG(OP.Loc" in flat(r.sql)


# ==========================================================================
# A052 / L030 -- parameters buried inside a string literal
# ==========================================================================

def test_like_wildcard_param_literal_is_bound():
    """A parameter pre-processor only rewrites an EXACT '@Name' to a bound
    parameter; '%@searchText%' stays a literal and matches the text
    '@searchText' -- the search silently returns nothing."""
    r = convert("SELECT Code FROM T WHERE Code LIKE '%@searchText%'")
    assert "LIKE '%' || @searchText || '%'" in flat(r.sql)
    assert "A052" in codes(r)


def test_exact_quoted_param_is_left_alone():
    r = convert("SELECT a FROM T WHERE TenantProductId = '@TenantProductId'")
    assert "'@TenantProductId'" in r.sql
    assert "A052" not in codes(r) and "L030" not in codes(r)


def test_email_and_static_replacements_are_not_params():
    r = convert("SELECT a FROM T WHERE Mail LIKE '%@gmail.com%' AND Owner LIKE '%@UserId%'")
    out = flat(r.sql)
    assert "'%@gmail.com%'" in out and "'%@UserId%'" in out
    assert "A052" not in codes(r)


def test_other_unbound_param_text_is_reported():
    r = convert("SELECT a FROM T WHERE Code IN ('@First,@Second')")
    assert "L030" in codes(r)


# ==========================================================================
# PC09 -- missing ';' between statements in ALREADY-CONVERTED PostgreSQL
# ==========================================================================

MULTI = ("SELECT * FROM Ref_Tax WHERE TaxId = @TaxId AND COALESCE(IsDeleted, FALSE) = FALSE\n"
         "\n"
         "\tSELECT * FROM Ref_TaxDetails WHERE ParentNo = @TaxId\n"
         "\n"
         "\tselect master.* from Ref_Tax tax inner join Ref_Country master on master.CountryId = tax.CountryCode\n")


def test_check_sql_inserts_missing_statement_separators():
    result = pgcheck.check_sql(MULTI)
    assert result.valid_before is False
    assert result.valid_after is True
    assert ("PC09", 2) in result.applied
    lines = result.sql.splitlines()
    assert lines[0].endswith("= FALSE;")
    assert lines[2].endswith("@TaxId;")


def test_separator_goes_before_a_trailing_comment():
    sql = ("SELECT a FROM t WHERE b LIKE '%' || '@x' || '%' --GROUP BY a\n"
           "SELECT c FROM u")
    result = pgcheck.check_sql(sql)
    assert result.valid_after is True
    assert "|| '%'; --GROUP BY a" in result.sql


def test_update_after_update_is_separated():
    sql = ("UPDATE t SET a = FALSE WHERE id = @Id\n"
           "UPDATE t SET a = TRUE WHERE id = @Other")
    result = pgcheck.check_sql(sql)
    assert result.valid_after is True and "@Id;" in result.sql


def test_valid_cte_and_insert_select_are_not_split():
    for sql in ("WITH c AS (\n SELECT 1 AS a\n)\nSELECT a FROM c",
                "INSERT INTO t (a)\nSELECT a FROM u",
                "SELECT a FROM t\nUNION ALL\nSELECT a FROM u"):
        result = pgcheck.check_sql(sql)
        assert "PC09" not in dict(result.applied), sql
        assert result.sql == sql


def test_other_syntax_errors_are_not_patched_with_semicolons():
    sql = "SELECT a FROM t WHERE\nSELECT b FROM u"
    result = pgcheck.check_sql(sql)
    assert "PC09" not in dict(result.applied)


def test_statement_keyword_inside_string_is_not_a_boundary():
    sql = "SELECT 'x\nSELECT y' AS a FROM t"
    result = pgcheck.check_sql(sql)
    assert result.sql == sql and "PC09" not in dict(result.applied)


# ==========================================================================
# PC10 -- [bracket] identifiers left in converted PostgreSQL
# ==========================================================================

def test_check_sql_unbrackets_tsql_identifiers():
    sql = ("SELECT MTP.SiteArea[SiteAreaId], [ER].[ItemClassCode], m0.[Order]\n"
           "FROM Ref_Class ER LIMIT 1")
    result = pgcheck.check_sql(sql)
    assert "PC10" in dict(result.applied)
    assert "MTP.SiteArea SiteAreaId" in result.sql
    assert "ER.ItemClassCode" in result.sql and 'm0."order"' in result.sql
    assert result.valid_after is True


def test_postgres_array_syntax_is_never_touched():
    for sql in ("SELECT ARRAY[1, 2] AS a",
                "SELECT x::int[] FROM t",
                "SELECT (string_to_array(a, ','))[1] FROM t",
                "SELECT arr[i] FROM t",
                "SELECT '[Order]' AS a FROM t"):
        result = pgcheck.check_sql(sql)
        assert "PC10" not in dict(result.applied), sql
        assert result.sql == sql


# ==========================================================================
# PC11 -- '%@x%' in converted PostgreSQL
# ==========================================================================

def test_check_sql_binds_wildcard_param_literal():
    sql = "SELECT a FROM t WHERE COALESCE(a, '') LIKE '%@searchText%' LIMIT 1"
    result = pgcheck.check_sql(sql)
    assert "PC11" in dict(result.applied)
    assert "LIKE '%' || @searchText || '%'" in result.sql


def test_check_sql_leaves_exact_param_literal():
    sql = "SELECT a FROM t WHERE b = '@TenantProductId' LIMIT 1"
    result = pgcheck.check_sql(sql)
    assert result.sql == sql


# ==========================================================================
# PC12 / R001 -- bare SPLIT() left by conversions that only stripped dbo.
# ==========================================================================

def test_check_sql_replaces_split_with_string_to_table():
    """18 reference constants call SPLIT(): no such function in the PostgreSQL
    schema, so each fails with 42883 at runtime -- grammar-valid all along."""
    sql = ("SELECT a FROM t WHERE id IN (\n  SELECT CAST(items AS INT)\n  FROM SPLIT(@Ids, ',')\n) LIMIT 1")
    result = pgcheck.check_sql(sql)
    assert "PC12" in dict(result.applied)
    assert "STRING_TO_TABLE(CAST(@Ids AS TEXT), ',')" in result.sql
    assert "AS items" in result.sql and ") AS split" in result.sql
    assert "SPLIT(" not in result.sql.upper().replace("STRING_TO_TABLE", "")
    assert result.valid_after is True


def test_split_keeps_an_existing_alias():
    sql = ("SELECT 1 FROM t WHERE EXISTS (SELECT 1 FROM split(sd.days, ',') reqd "
           "INNER JOIN m ON m.Day = reqd.items) LIMIT 1")
    result = pgcheck.check_sql(sql)
    assert ") reqd INNER JOIN" in result.sql and "AS split" not in result.sql
    assert result.valid_after is True


def test_split_lookalikes_are_untouched():
    for sql in ("SELECT split_part(a, ',', 1) FROM t",
                "SELECT x FROM regexp_split_to_table(a, ',') AS x",
                "SELECT 'FROM SPLIT(a, b)' AS t"):
        result = pgcheck.check_sql(sql)
        assert "PC12" not in dict(result.applied), sql
        assert result.sql == sql


def test_cross_apply_dbo_split_converts_fully():
    """A022 handled FROM dbo.Split but not CROSS APPLY dbo.Split, which came out
    as JOIN LATERAL SPLIT(...) -- 42883 at runtime (A053)."""
    r = convert("SELECT DC.Id, SplitItems.items FROM DocConfig DC "
                "CROSS APPLY dbo.Split(DC.FileType, ',') AS SplitItems")
    out = flat(r.sql)
    assert "STRING_TO_TABLE(CAST(DC.FileType AS TEXT), ',')" in out
    assert "AS SplitItems" in out
    assert "R001" not in codes(r) and "A053" in codes(r)
    assert r.validation and r.validation.valid


def test_a022_output_is_not_reported_as_split():
    r = convert("SELECT a FROM t WHERE a IN (SELECT CAST(items AS INT) FROM dbo.Split(@Ids, ','))")
    assert "R001" not in codes(r), [w.message for w in r.warnings if w.code == "R001"]


def test_r001_reports_split():
    from core import resolvability
    found = resolvability.check("SELECT CAST(items AS INT) FROM SPLIT(@Ids, ',') AS s")
    assert any(w.code == "R001" and "SPLIT" in w.message for w in found)
    assert not any(w.code == "R001" and "SPLIT" in w.message
                   for w in resolvability.check("SELECT split_part(a, ',', 1) FROM t"))


# ==========================================================================
# file level: a literal whose only T-SQL residue is brackets is repaired
# ==========================================================================

def _write(tmp_path: Path, name: str, body: str) -> Path:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8", newline="")
    return path


def test_check_run_repairs_bracket_only_literal_and_separators(tmp_path):
    body = ('public class Q {\r\n'
            '    public static readonly string A = @"SELECT MTP.SitePrintingId, MTP.SiteArea[SiteAreaId]\r\n'
            '        FROM Ref_Site_Printing MTP WHERE COALESCE(MTP.IsDeleted, FALSE) = FALSE\r\n'
            '\r\n'
            '        SELECT MTG.SiteGridId FROM Ref_Site_PrintingGrid MTG";\r\n'
            '}\r\n')
    path = _write(tmp_path, "Q.cs", body)
    real = pgcheck.run(tmp_path, dry_run=False)
    assert real.total("fixed") == 1
    new = path.read_bytes().decode("utf-8")
    assert "MTP.SiteArea SiteAreaId" in new
    assert "= FALSE;\r\n" in new
    assert "\r\r\n" not in new
    sql = new.split('@"', 1)[1].split('";', 1)[0]
    assert validator.validate(sql).valid


def test_check_run_sees_constants_that_open_with_a_comment(tmp_path):
    """Converted constants start with `-- PostgreSQL. Converted ...`; the strict
    SQL shape skipped every one of them, so no repair could ever reach them."""
    body = ('public class Q {\r\n'
            '    public static readonly string A = @"-- PostgreSQL. Converted from T-SQL.\r\n'
            '        SELECT a FROM t WHERE b LIKE \'%@searchText%\' LIMIT 1";\r\n'
            '}\r\n')
    path = _write(tmp_path, "Q.cs", body)
    real = pgcheck.run(tmp_path, dry_run=False)
    assert real.total("fixed") == 1
    assert "LIKE '%' || @searchText || '%'" in path.read_bytes().decode("utf-8")


def test_scan_shape_still_ignores_commented_literals():
    from core import csharp
    text = 'var q = @"-- note\r\nSELECT a FROM t";'
    assert csharp.find_sql_literals(text) == []
    assert len(csharp.find_sql_literals(text, allow_leading_comments=True)) == 1
