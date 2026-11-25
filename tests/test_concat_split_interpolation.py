"""A011 string concat, A012 STRING_SPLIT, and interpolated-string masking.

Fixtures are distilled from real KLSPL queries (DropdownQueryMap.cs,
BulkUpdateHelper.cs, WarehouseLoadingQueries.cs).
"""

from core import interpolation, orchestrator


def convert(sql: str):
    return orchestrator.convert(sql)


# ---------------------------------------------------------------- A011 concat

def test_a011_like_param_pattern():
    result = convert(
        "SELECT Code FROM Master_City "
        "WHERE (@SearchedVal = '' OR CityCode LIKE '%' + @SearchedVal + '%')")
    assert "||" in result.sql
    assert "+" not in result.sql
    assert any(w.code == "A011" for w in result.warnings)


def test_a011_column_literal_column():
    result = convert(
        "SELECT SHCCode + ' - ' + SHCDescription AS Name FROM Master_SHC")
    assert "|| ' - ' ||" in result.sql
    assert any(w.code == "A011" for w in result.warnings)


def test_a011_refuses_numeric_addition():
    result = convert("SELECT Qty + 1 FROM Stock_Item")
    assert "||" not in result.sql
    assert not any(w.code == "A011" for w in result.warnings)


def test_a011_refuses_typeless_chain():
    # col + col: no literal proves the type -- must stay for L001/human review
    result = convert("SELECT ColA + ColB FROM T")
    assert "||" not in result.sql
    assert not any(w.code == "A011" for w in result.warnings)


# ------------------------------------------------------ A017 N'...' literals

def test_a017_unwraps_national_literal():
    """N'x' is not harmless in PostgreSQL: it parses as the type-qualified
    literal `N 'x'` and fails at RUNTIME with 'type "n" does not exist', so
    grammar validation alone never catches it."""
    result = convert("SELECT N'x' AS d FROM t")
    assert "N'" not in result.sql
    assert "'x'" in result.sql
    assert any(w.code == "A017" for w in result.warnings)


def test_a017_unblocks_a011_for_national_chain():
    """The real DropdownQueryMap shape: sqlglot parses N'...' as exp.National,
    not exp.Literal, so A011 saw no string literal and refused the chain --
    leaving `text + text`, which PostgreSQL rejects."""
    result = convert(
        "SELECT (C.IATATwoLetterCode + N' - ' + B.OrgName) AS Description FROM t")
    assert "||" in result.sql
    assert "+" not in result.sql
    assert "N'" not in result.sql
    assert any(w.code == "A017" for w in result.warnings)
    assert any(w.code == "A011" for w in result.warnings)


def test_a017_national_inside_like_chain():
    result = convert(
        "SELECT a FROM t WHERE (a + N' - ' + b) LIKE '%' + @S + '%'")
    assert "+" not in result.sql
    assert result.is_usable


# ---------------------------------------------------------- A012 STRING_SPLIT

def test_a012_bare_string_split():
    result = convert("SELECT value FROM STRING_SPLIT(@Ids, ',')")
    low = result.sql.lower()
    assert "string_to_table" in low
    assert "string_split" not in low
    assert "value" in low
    assert any(w.code == "A012" for w in result.warnings)


def test_a012_aliased_string_split_gets_value_column():
    result = convert(
        "SELECT s.value FROM STRING_SPLIT(@Ids, ',') AS s")
    low = result.sql.lower()
    assert "string_to_table" in low
    assert "(value)" in low.replace(" ", "")
    assert any(w.code == "A012" for w in result.warnings)


def test_a012_refuses_three_argument_form():
    result = convert("SELECT * FROM STRING_SPLIT(@Ids, ',', 1)")
    assert "string_to_table" not in result.sql.lower()
    assert not any(w.code == "A012" for w in result.warnings)


# ------------------------------------------------------- interpolation masking

def test_mask_and_restore_roundtrip():
    sql = "SELECT * FROM {tableName} WHERE Id = @Id AND Org = '{orgCode}'"
    masked, holes = interpolation.mask_holes(sql)
    assert holes == ["{tableName}", "{orgCode}"]
    assert "{" not in masked
    assert interpolation.restore_holes(masked, holes) == sql


def test_mask_refuses_escaped_braces():
    assert interpolation.mask_holes("SELECT '{{literal}}' FROM T") is None


def test_mask_refuses_quote_inside_hole():
    assert interpolation.mask_holes(
        'SELECT {(x ? "a" : "b")} FROM T') is None


def test_mask_refuses_unbalanced():
    assert interpolation.mask_holes("SELECT {oops FROM T") is None


def test_zero_hole_string_is_maskable():
    masked, holes = interpolation.mask_holes("SELECT 1")
    assert masked == "SELECT 1"
    assert holes == []
    assert interpolation.restore_holes(masked, holes) == "SELECT 1"


def test_restore_refuses_dropped_sentinel():
    masked, holes = interpolation.mask_holes("SELECT {a} FROM T")
    assert interpolation.restore_holes("SELECT 1 FROM T", holes) is None


def test_interpolated_conversion_end_to_end():
    # The real-world shape: T-SQL around a C# hole must convert, hole intact.
    sql = ("SELECT TOP 5 Name FROM {tableName} "
           "WHERE ISNULL(IsDeleted, 0) = 0 AND Code LIKE '%' + @Search + '%'")
    masked, holes = interpolation.mask_holes(sql)
    result = convert(masked)
    assert result.is_usable
    restored = interpolation.restore_holes(result.sql, holes)
    assert restored is not None
    assert "{tableName}" in restored
    assert "||" in restored
    assert "TOP" not in restored.upper().replace("TO_", "")


def test_a013_nolock_in_update_from_join_parses():
    sql = ("UPDATE ORG SET ORG.A = B.A FROM #t ORG "
           "INNER JOIN X B WITH(NOLOCK) ON ORG.Id = B.Id")
    result = convert(sql)
    assert not any(w.code == 'SG-PARSE' for w in result.warnings)
    assert 'NOLOCK' not in result.sql.upper()
    assert any(w.code == 'A013' for w in result.warnings)



# --------------------------------------------------------------------------
# A011 extended text evidence (ConsigneeData regression)
# --------------------------------------------------------------------------

def test_chain_inside_function_argument_is_its_own_chain():
    """The old find_ancestor test skipped a chain nested inside a function
    argument of a larger chain -- both were left as `+` and refused."""
    result = convert(
        "SELECT sc.name + isnull(' (' + sc.code + ')', '') AS d FROM t sc")
    assert "||" in result.sql
    assert "+" not in result.sql.split("FROM")[0]


def test_isnull_with_text_argument_proves_the_chain():
    result = convert("SELECT a + ISNULL(b, '') FROM t")
    assert "a || COALESCE(b, '')" in " ".join(result.sql.split())


def test_cast_to_char_type_proves_the_chain():
    result = convert("SELECT a + CAST(n AS NVARCHAR(10)) FROM t")
    assert "||" in result.sql


def test_untyped_chain_is_still_refused():
    result = convert("SELECT a + b FROM t")
    assert "a + b" in " ".join(result.sql.split())


def test_numeric_literal_chain_is_still_refused():
    result = convert("SELECT a + 1 + 'x' FROM t")
    assert "||" not in result.sql
