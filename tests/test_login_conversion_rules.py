"""Rules from a real conversion run (2026-08-12).

Spec codes were renumbered to avoid collisions with 2026-08-11 fixes:
spec-A011 -> A015 (TRY_CAST), spec-A012 -> A014 (boolean ''),
spec-A013 -> A016 (UPDATE/SET glue). See CONVERSION_RULES.md.
"""

from core import orchestrator


def convert(sql: str):
    return orchestrator.convert(sql)


def codes(result):
    return {w.code for w in result.warnings}


# ------------------------------------------------------------- A007 expansion

def test_defaultsite_allowlisted_as_boolean():
    result = convert(
        "SELECT 1 FROM UserSite UB WHERE ISNULL(UB.DefaultSite, 0) = 1")
    assert "A007" in codes(result)
    low = result.sql.upper()
    assert "TRUE" in low and "= 1" not in low


def test_boolean_string_compare_true_false():
    result = convert(
        "SELECT 1 FROM Cfg WHERE ISNULL(IsFlowSent, 'false') = 'false'")
    assert "A007" in codes(result)
    assert "'false'" not in result.sql.lower()
    assert "FALSE" in result.sql.upper()


def test_is2fa_matches_boolean_convention():
    result = convert("UPDATE Users SET Is2FA = '' WHERE UserId = @UserId")
    assert "A014" in codes(result)
    assert "''" not in result.sql
    assert "FALSE" in result.sql.upper()


# ------------------------------------------------------------------ A014/A015

def test_a014_insert_empty_string_boolean_positional():
    # Table name must NOT shift the VALUES index: Is2FA is column 1 of 2.
    result = convert(
        "INSERT INTO Users (Is2FA, Name) VALUES ('', 'x')")
    assert "A014" in codes(result)
    assert "FALSE" in result.sql.upper()
    assert "'x'" in result.sql  # the non-boolean value is untouched


def test_a014_refuses_non_boolean_column():
    result = convert("INSERT INTO Users (Remark, Name) VALUES ('', 'x')")
    assert "A014" not in codes(result)


def test_a015_try_cast_becomes_cast():
    result = convert("SELECT TRY_CAST(Amount AS INT) FROM Payments")
    assert "A015" in codes(result)
    assert "TRY_CAST" not in result.sql.upper()
    assert "CAST" in result.sql.upper()


# ------------------------------------------------------------------------ A016

def test_a016_update_set_glued_to_one_line():
    result = convert("UPDATE Users\nSET Name = 'x'\nWHERE UserId = 1")
    assert result.is_usable
    for line in result.sql.splitlines():
        if line.strip().upper().startswith("SET"):
            raise AssertionError(f"line-leading SET survived: {line!r}")


# ------------------------------------------------------------------ L016/L017

def test_l016_insert_returning_split_is_error():
    result = convert(
        "INSERT INTO Log (Msg) VALUES ('x'); RETURNING Logid")
    assert any(w.code == "L016" and w.severity.name == "ERROR"
               for w in result.warnings)


def test_l017_quoted_identifiers_info():
    result = convert('SELECT SCMH."key" FROM SystemConfig SCMH')
    assert any(w.code == "L017" for w in result.warnings)


# ------------------------------------------------------------------ whitespace

def test_no_extra_interior_whitespace_added():
    result = convert(
        "SELECT A FROM T WITH(NOLOCK) WHERE B = 1")
    assert result.is_usable
    for line in result.sql.splitlines():
        body = line.lstrip()
        assert "  " not in body, f"double space introduced: {line!r}"
        assert line == line.rstrip(), f"trailing whitespace: {line!r}"


def test_whitespace_inside_literals_untouched():
    result = convert("SELECT 'a  b   c' AS x")
    assert "'a  b   c'" in result.sql


# -------------------------------------------------- navigation-shaped regression

def test_navigation_shape_union_isnull_nolock():
    sql = (
        "SELECT MenuId, MenuName FROM Menu M WITH(NOLOCK) "
        "WHERE ISNULL(M.IsDeleted, 0) = 0 AND M.RoleId = @RoleId "
        "UNION "
        "SELECT MenuId, MenuName FROM AdminMenu AM WITH(NOLOCK) "
        "WHERE ISNULL(AM.IsDeleted, 0) = 0 AND AM.RoleId = @RoleId")
    result = convert(sql)
    assert result.is_usable
    up = result.sql.upper()
    assert "@ROLEID" in up.replace(" ", "")
    assert "ISNULL" not in up
    assert "NOLOCK" not in up
