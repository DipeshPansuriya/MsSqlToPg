"""v2.18.0: A061 Dapper parameters cast to the compared column's type; scan/check-pg see IF (...) and
BEGIN ... END batches and T-SQL behind a leading comment.

Every case comes from the 2026-11-05 module run (T-SQL, executed on PostgreSQL 17 with the
parameters typed the way Npgsql binds the C# values): the converted query parsed, then failed with 42883
`integer = text` / `character varying = integer`, or the scan never offered the batch to the converter.
"""
from __future__ import annotations

import re

import pytest

from core import csharp, orchestrator, scan, schema_hints, typefix

TYPES = """\
itemdetail.owner_customerid\tinteger
itemdetail.customer_code\tcharacter varying(20)
itemdetail.itemnumber\tbigint
itemdetail.jobholdtime\ttimestamp without time zone
itemdetail.itemlocationid\tinteger
itemdetail.isdeleted\tboolean
itemdetail.remarks\ttext
items.createdon\ttimestamp without time zone
items.weight\tnumeric(18,2)
"""


def flat(sql: str) -> str:
    return re.sub(r"\s+", " ", sql).strip()


def codes(result) -> list[str]:
    return [w.code for w in result.warnings]


@pytest.fixture
def typed(tmp_path, monkeypatch):
    path = tmp_path / "column_types.txt"
    path.write_text(TYPES, encoding="utf-8")
    monkeypatch.setenv(schema_hints.TYPES_ENV_VAR, str(path))
    schema_hints.reset()
    return path


def convert(sql: str):
    r = orchestrator.convert(sql)
    assert r.validation is not None and r.validation.valid, r.sql
    return r


NUM = "CAST(NULLIF(TRIM(CAST({p} AS TEXT)), '') AS {t})"


# --------------------------------------------------------------------------
# A061 -- positive
# --------------------------------------------------------------------------

def test_int_column_vs_quoted_param_is_cast(typed):
    # Verifydocrangequery5: Owner_CustomerId INT, C# passes `string Customer_Code`
    r = convert("SELECT ItemNumber FROM ItemDetail WITH(NOLOCK) WHERE Owner_CustomerId = '@CustomerCode'")
    assert "Owner_CustomerId = " + NUM.format(p="'@CustomerCode'", t="BIGINT") in flat(r.sql)
    assert "A061" in codes(r)


def test_varchar_column_vs_bare_param_is_cast_to_text(typed):
    # Getresellerdocumentdetails: ItemDetail.Customer_Code varchar(20), C# passes `int ResellerId`
    r = convert("SELECT sd.ItemNumber FROM ItemDetail sd WHERE sd.Customer_Code = @ResellerId")
    assert "sd.Customer_Code = CAST(@ResellerId AS TEXT)" in flat(r.sql)


def test_param_on_the_left_is_cast(typed):
    r = convert("SELECT sd.ItemNumber FROM ItemDetail sd WHERE @ResellerId = sd.Customer_Code AND @OrgId <> sd.Owner_CustomerId")
    out = flat(r.sql)
    assert "CAST(@ResellerId AS TEXT) = sd.Customer_Code" in out
    assert NUM.format(p="@OrgId", t="BIGINT") + " <> sd.Owner_CustomerId" in out


def test_bigint_and_numeric_columns(typed):
    r = convert("SELECT ItemNumber FROM ItemDetail sd JOIN Items s ON 1 = 1 "
                "WHERE sd.ItemNumber >= @maxexist AND s.Weight > @MinWeight")
    out = flat(r.sql)
    assert "sd.ItemNumber >= " + NUM.format(p="@maxexist", t="BIGINT") in out
    assert "s.Weight > " + NUM.format(p="@MinWeight", t="NUMERIC") in out


def test_between_params_take_the_cast_expressions_type(typed):
    # GetItemAllotmentListQuery: CAST(Createdon AS DATE) BETWEEN @FromDate AND @ToDate, C# strings
    r = convert("SELECT ItemId FROM Items WHERE CAST(Createdon AS DATE) BETWEEN @FromDate AND @ToDate")
    out = flat(r.sql)
    assert ("BETWEEN " + NUM.format(p="@FromDate", t="DATE") + " AND " + NUM.format(p="@ToDate", t="DATE")) in out


def test_timestamp_plus_interval_side(typed):
    # Getallocateditemsforoutbound: '@CurrentCallDate' is DateTime.UtcNow.ToString(...)
    r = convert("SELECT ItemNumber FROM ItemDetail sd WHERE '@CurrentCallDate' >= DATEADD(MINUTE, 1, sd.JobHoldTime)")
    assert NUM.format(p="'@CurrentCallDate'", t="TIMESTAMP") + " >= " in flat(r.sql)


def test_timestamp_plus_interval_times_n_side(typed):
    # Getallocateditemsforoutbound: DATEADD(MINUTE, @HoldTime, col) -> col + INTERVAL '1 MINUTE' * @HoldTime
    r = convert("SELECT ItemNumber FROM ItemDetail sd WHERE @CurrentCallDate >= DATEADD(MINUTE, @HoldTime, sd.JobHoldTime)")
    assert NUM.format(p="@CurrentCallDate", t="TIMESTAMP") + " >= " in flat(r.sql)


def test_param_against_empty_string_is_cast_to_text():
    # GetTenantItemsList_*: (@X = '' OR col = @X) -- an int @X made '' raise 22P02; no schema needed
    r = convert("SELECT a FROM t WHERE (@LocId = '' OR @Other <> '')")
    out = flat(r.sql)
    assert "CAST(@LocId AS TEXT) = ''" in out and "CAST(@Other AS TEXT) <> ''" in out


def test_param_defaulted_to_a_text_literal_is_cast_to_text():
    # GetItemAllotmentListQueryForRenewal: ISNULL('@ItemLocationId','') = '' with a C# int -> 22P02
    r = convert("SELECT a FROM t WHERE ISNULL('@ItemLocationId', '') = '' AND NULLIF(@EndDate, '') IS NULL")
    out = flat(r.sql)
    assert "COALESCE(CAST('@ItemLocationId' AS TEXT), '') = ''" in out
    assert "NULLIF(CAST(@EndDate AS TEXT), '')" in out


def test_csv_list_inside_string_to_table_is_left_alone():
    # A022's empty-list guard: the CSV parameter is always a C# string
    r = convert("SELECT CAST(items AS INT) FROM dbo.Split(@Ids, ',')")
    assert "STRING_TO_TABLE(NULLIF(@Ids, ''), ',')" in flat(r.sql)


def test_param_defaulted_to_a_number_is_cast_to_a_number():
    # AdvancedSearch: ISNULL(@OrgId, 0) <> 0 with C# `string? Customer_Id`; a null int? arrives as
    # TEXT from the data layer too -- COALESCE(text, 0) raises 42804 either way
    r = convert("SELECT a FROM t WHERE ISNULL(@OrgId, 0) <> 0 AND ISNULL(@Rate, 0.5) > 1")
    out = flat(r.sql)
    assert "COALESCE(" + NUM.format(p="@OrgId", t="BIGINT") + ", 0) <> 0" in out
    assert "COALESCE(" + NUM.format(p="@Rate", t="NUMERIC") + ", 0.5) > 1" in out


def test_param_against_number_literal_is_left_alone():
    # `@Flag = 1`: a C# int or bool flag (L021 covers the bool), the cast would only add noise
    r = convert("SELECT a FROM t WHERE @LocId = 0 AND 1 = @IsAdmin")
    assert "CAST(" not in r.sql


def test_coalesce_of_a_typed_column(typed):
    r = convert("SELECT a FROM ItemDetail WHERE ISNULL(ItemLocationId, 0) = @LocationId")
    assert "COALESCE(ItemLocationId, 0) = " + NUM.format(p="@LocationId", t="BIGINT") in flat(r.sql)


# --------------------------------------------------------------------------
# A061 -- must NOT fire
# --------------------------------------------------------------------------

def test_no_schema_no_cast():
    r = convert("SELECT ItemNumber FROM ItemDetail WHERE Owner_CustomerId = '@CustomerCode' AND Customer_Code = @ResellerId")
    assert "CAST(" not in r.sql
    assert "A061" not in codes(r)


def test_static_text_params_are_left_alone(typed):
    # The application splices these in as text before Dapper runs
    r = convert("SELECT ItemNumber FROM ItemDetail WHERE Owner_CustomerId = @Tenant_Admin_Id AND Owner_CustomerId = @UserId")
    assert "CAST(" not in r.sql


def test_param_against_non_empty_string_literal_is_left_alone(typed):
    r = convert("SELECT a FROM t WHERE '@BusinessType' = 'Agent' AND @Kind <> 'GSA'")
    assert "CAST(" not in r.sql


def test_boolean_column_vs_param_is_left_alone(typed):
    r = convert("SELECT a FROM ItemDetail WHERE IsDeleted = @IsDeleted")
    assert "CAST(@IsDeleted" not in r.sql


def test_param_vs_param_and_column_vs_column_are_left_alone(typed):
    r = convert("SELECT a FROM ItemDetail WHERE @A = @B AND Owner_CustomerId = ItemLocationId")
    assert "CAST(" not in r.sql


def test_param_text_inside_string_literal_is_not_touched(typed):
    r = convert("SELECT 'Owner_CustomerId = @CustomerCode' AS Msg FROM ItemDetail WHERE Remarks = 'x = @y'")
    assert "'Owner_CustomerId = @CustomerCode'" in r.sql and "CAST(" not in r.sql


def test_apply_is_idempotent(typed):
    once = convert("SELECT a FROM ItemDetail sd WHERE sd.Customer_Code = @ResellerId AND sd.Owner_CustomerId = '@OrgId'").sql
    again, n = typefix.cast_params(once)
    assert n == 0 and again == once


# --------------------------------------------------------------------------
# Scan: IF (...) / BEGIN ... END batches and a leading comment
# --------------------------------------------------------------------------

@pytest.mark.parametrize("sql", [
    "if('@LinkedWith' = 'Community') begin SELECT a FROM t1 WITH(NOLOCK) end else begin SELECT a FROM t2 end",
    "IF (@IsForRenewal = 'Y') BEGIN SELECT a FROM t1 END ELSE BEGIN SELECT a FROM t2 END",
    "IF '@orgType' = 'Agent' BEGIN SELECT a FROM t1 END ELSE BEGIN SELECT a FROM t2 END",
    "BEGIN DECLARE @t TABLE (v INT) INSERT INTO @t SELECT a FROM t1 SELECT v FROM @t END",
    "-- Drop temp tables if they exist\nDROP TABLE IF EXISTS #tmp\nSELECT a INTO #tmp FROM t1 WITH(NOLOCK)\nSELECT a FROM #tmp",
])
def test_control_batches_look_like_sql(sql):
    assert csharp.looks_like_sql(sql)
    assert not csharp.looks_like_sql_fragment(sql)


@pytest.mark.parametrize("text", [
    "If (you) select a value from the list, press OK",
    "If (needed) copy the rows from the source file",
    "Begin the upload from the portal",
    "BEGIN select the rows from the grid and press Save",
    "-- PostgreSQL. Converted 2026-10-01\nSELECT a FROM t WHERE b = @B",
    "IF the item is allocated, select it from the list",
])
def test_prose_and_converted_pg_are_not_control_batches(text):
    assert not csharp.looks_like_sql(text)


def test_scan_rewrites_an_if_else_batch(tmp_path):
    src = ('class Q {\n    public static readonly string Check = @"\n'
           "        if('@LinkedWith' = 'Community')\n        begin\n"
           "        SELECT MCO.TenantID as OrgId FROM Ref_Tenant MCO WITH(NOLOCK)\n        end\n"
           "        else\n        begin\n        select org.OrgId AS OrgId FROM Organization org WITH(NOLOCK)\n"
           '        end\n";\n}\n')
    f = tmp_path / "Q.cs"
    f.write_text(src, encoding="utf-8")
    report = scan.run(tmp_path)
    finding = report.files[0].findings[0]
    assert finding.action == scan.REWRITTEN, finding.reason
    out = f.read_text(encoding="utf-8")
    assert "WITH(NOLOCK)" not in out and "UNION ALL" in out


def test_scan_reports_but_never_rewrites_an_unconvertible_batch(tmp_path):
    # nested IF is not flattened (A058): reported, file untouched
    src = ('class Q {\n    public static readonly string Nested = @"\n'
           "        IF (@R = 'Y') BEGIN IF (@T = 'G') BEGIN SELECT a FROM t1 WITH(NOLOCK) END END\n"
           "        ELSE BEGIN SELECT a FROM t2 END\n"
           '";\n}\n')
    f = tmp_path / "Q.cs"
    f.write_text(src, encoding="utf-8")
    report = scan.run(tmp_path)
    finding = report.files[0].findings[0]
    assert finding.action == scan.SKIPPED and "FRAGMENT" not in finding.reason
    assert f.read_text(encoding="utf-8") == src


def test_check_pg_sees_an_if_batch_as_t_sql():
    src = ('class Q { string A = @"if(\'@T\' = \'A\') begin SELECT a FROM t1 WITH(NOLOCK) end"; }')
    assert len(csharp.find_sql_literals(src, allow_leading_comments=True)) == 1
