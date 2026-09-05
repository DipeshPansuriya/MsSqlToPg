"""Dapper parameter handling.

The codebase this tool serves uses Dapper, so queries carry `@ParamName`
placeholders. Npgsql accepts `@name` natively, so a Dapper parameter must survive
conversion BYTE-FOR-BYTE.

SQLGlot rewrites every `@Name` to `$Name`, which is invalid PostgreSQL and breaks
Dapper at runtime. restore_dapper_parameters() undoes that -- but only for names
that were never DECLAREd, since a genuine local variable does need converting.
"""

from core import linter
from engines import sqlglot_engine


def codes(warnings):
    return [w.code for w in warnings]


# --------------------------------------------------------------------------
# declared_variables -- the disambiguator
# --------------------------------------------------------------------------

def test_declare_makes_a_local_variable():
    assert linter.declared_variables("DECLARE @Status INT;") == {"@status"}


def test_procedure_parameters_are_locals():
    sql = "CREATE PROCEDURE dbo.usp_X @Status INT, @Name NVARCHAR(50) AS BEGIN SELECT 1; END"
    assert linter.declared_variables(sql) == {"@status", "@name"}


def test_undeclared_parameter_is_not_a_local():
    assert linter.declared_variables(
        "SELECT * FROM Users WHERE Status = @Status") == set()


# --------------------------------------------------------------------------
# Round-trip through the engine
# --------------------------------------------------------------------------

def test_dapper_parameters_survive_verbatim():
    sql = "SELECT * FROM Users WHERE Status = @Status AND OrgId = @OrgId"
    result = sqlglot_engine.convert(sql)
    assert "@Status" in result.sql
    assert "@OrgId" in result.sql
    assert "$Status" not in result.sql
    assert "$OrgId" not in result.sql


def test_top_parameter_survives_into_limit():
    sql = "SELECT TOP (@PageSize) * FROM Orders WHERE CustomerId = @CustomerId"
    result = sqlglot_engine.convert(sql)
    assert "LIMIT @PageSize" in result.sql
    assert "@CustomerId" in result.sql
    assert "$" not in result.sql


def test_dapper_query_produces_no_parameter_warnings():
    """The whole point: a clean Dapper SELECT must not be flagged."""
    sql = ("SELECT TOP (@PageSize) o.OrderID, ISNULL(o.Freight, 0) AS F "
           "FROM dbo.Orders o WITH (NOLOCK) "
           "WHERE o.CustomerId = @CustomerId AND o.OrderDate >= @FromDate "
           "ORDER BY o.OrderDate DESC")
    result = sqlglot_engine.convert(sql)
    assert "L004" not in codes(result.warnings)
    assert not result.has_errors, [str(w) for w in result.warnings]
    assert result.is_usable


def test_insert_and_update_parameters_survive():
    for sql in ("UPDATE Users SET Name = @Name WHERE Id = @Id",
                "INSERT INTO Users (Name, Email) VALUES (@Name, @Email)"):
        result = sqlglot_engine.convert(sql)
        assert "$" not in result.sql, sql
        assert "L004" not in codes(result.warnings), sql


# --------------------------------------------------------------------------
# Genuine locals must STILL be reported
# --------------------------------------------------------------------------

def test_declared_local_is_still_flagged():
    sql = ("DECLARE @TableName NVARCHAR(50) = 'ActiveUsers'; "
           "SELECT * FROM Users WHERE Name = @TableName;")
    result = sqlglot_engine.convert(sql)
    assert "L004" in codes(result.warnings)


def test_mixed_local_and_dapper_parameter():
    """A DECLAREd local must be flagged while the Dapper parameter is preserved."""
    sql = ("DECLARE @Cutoff INT = 5; "
           "SELECT * FROM Orders WHERE Qty > @Cutoff AND CustomerId = @CustomerId;")
    result = sqlglot_engine.convert(sql)
    assert "@CustomerId" in result.sql          # Dapper param preserved
    l004 = [w for w in result.warnings if w.code == "L004"]
    assert l004, "the DECLAREd local must still be reported"
    assert all("Cutoff" in w.message for w in l004), \
        "only the local should be flagged, never the Dapper parameter"


def test_system_variables_are_flagged():
    warnings = linter.l004_surviving_variables(
        "SELECT @@IDENTITY", "SELECT @@IDENTITY",
        linter.mask_literals("SELECT @@IDENTITY"))
    assert codes(warnings) == ["L004"]


# --------------------------------------------------------------------------
# restore_dapper_parameters in isolation
# --------------------------------------------------------------------------

def test_restore_leaves_dollar_quoted_blocks_alone():
    converted = "DO $$ BEGIN PERFORM 1; END $$;"
    assert sqlglot_engine.restore_dapper_parameters("", converted) == converted


def test_restore_leaves_positional_parameters_alone():
    converted = "SELECT * FROM t WHERE a = $1 AND b = $2"
    assert sqlglot_engine.restore_dapper_parameters("", converted) == converted


def test_restore_keeps_declared_locals_broken_so_l004_sees_them():
    out = sqlglot_engine.restore_dapper_parameters(
        "DECLARE @x INT;", "SELECT $x")
    assert out == "SELECT $x"
