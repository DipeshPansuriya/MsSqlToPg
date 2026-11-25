"""v2.16.0: A010 drop policy, A019 join form, A054 schema-typed implicit conversions,
A035 time operands, L031 name collisions, typed-literal marker, schema_from_ddl.

Every A054 case comes from the 2026-10-09 evaluation of the reference corpus
(the converted query failed on PostgreSQL 17 while the hand conversion ran).
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from core import markers, orchestrator, schema_hints, typefix, validator
from tools import schema_from_ddl

TYPES = """\
# table.column<TAB>type
system_lookup.listid\tinteger
system_lookup.identifier\tcitext
system_lookup.parentid\tinteger
system_lookup.isactive\tboolean
fileconfiguration.filesizeuom\tcitext
fileconfiguration.maxfilesize\tcitext
ref_fxrate.costrate\tnumeric(18,4)
ref_region.regionid\tinteger
ref_region.regioncode\tcitext
client_lookup.siteid\tcitext
ref_machine.lastmodified\ttimestamp without time zone
ref_machine.created\ttimestamp without time zone
order_details.billdt\tcitext
ref_country.countryid\tinteger
ref_class.sourcecountry\tcitext
ref_uom.isactive\tboolean
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


# --------------------------------------------------------------------------
# A010 -- placeholder prefixes dropped by default (one PostgreSQL database)
# --------------------------------------------------------------------------

@pytest.mark.parametrize("src, expected", [
    ("SELECT RegionCode FROM [@AppDbName].[dbo].[Ref_Region] WITH(NOLOCK)", "FROM Ref_Region"),
    ("SELECT * FROM @AppDbName.dbo.Orders", "FROM Orders"),
    ("SELECT * FROM [@DBName].[Orders]", "FROM Orders"),
    ("SELECT * FROM [@AppDbName].[reporting].[Sales]", "FROM reporting.Sales"),
])
def test_a010_drops_placeholder_by_default(src, expected):
    r = convert(src)
    assert expected in flat(r.sql)
    assert "@AppDbName" not in r.sql and "@DBName" not in r.sql
    assert "A010" in codes(r)
    assert "L015" not in codes(r)


def test_a010_schema_mode_keeps_quoted_schema(monkeypatch):
    monkeypatch.setenv(schema_hints.PLACEHOLDER_ENV_VAR, "schema")
    r = convert("SELECT a.x FROM [@AppDbName].dbo.A a JOIN [@DBName].[dbo].[B] b ON a.id = b.id")
    assert '"@AppDbName".A' in r.sql and '"@DBName".B' in r.sql


def test_cli_flag_selects_schema_mode():
    import app
    args = app.build_parser().parse_args(["--keep-db-placeholders", "x.sql"])
    assert args.keep_db_placeholders


# --------------------------------------------------------------------------
# A019 -- UPDATE alias SET ... FROM t alias INNER JOIN ...
# --------------------------------------------------------------------------

def test_a019_update_alias_with_inner_join_moves_on_to_where():
    r = convert("UPDATE MA SET DepotType = SLV.Description FROM #RefDepot MA INNER JOIN System_Lookup SLV "
                "ON SLV.ListId = MA.DepotTypeId WHERE MA.TenantProductId = @TenantProductId")
    out = flat(r.sql)
    assert out.startswith("UPDATE RefDepot AS MA SET DepotType = SLV.Description FROM System_Lookup AS SLV WHERE")
    assert "SLV.ListId = MA.DepotTypeId" in out and "MA.TenantProductId = @TenantProductId" in out
    assert "JOIN" not in out
    assert "A019" in codes(r)


def test_a019_two_joins_become_a_comma_list():
    r = convert("UPDATE MA SET CityCode = MC.CityCode FROM #RefDepot MA JOIN Ref_City MC ON MC.CityId = MA.CityId "
                "JOIN Ref_State S ON S.StateId = MC.StateId")
    out = flat(r.sql)
    assert "FROM Ref_City AS MC, Ref_State AS S WHERE MC.CityId = MA.CityId AND S.StateId = MC.StateId" in out


def test_a019_left_join_matches_the_target_row_on_ctid():
    r = orchestrator.convert("UPDATE MA SET MA.x = 1 FROM #RefDepot MA LEFT JOIN Ref_City MC ON MC.CityId = MA.CityId WHERE MA.Id > 0")
    sql = flat(r.sql)
    assert sql.startswith("UPDATE RefDepot AS MA_upd SET x = 1 FROM RefDepot AS MA LEFT JOIN Ref_City AS MC")
    assert "WHERE MA_upd.ctid = MA.ctid AND ( MA.Id > 0 )" in sql
    assert "A019" in codes(r) and "L018" not in codes(r)


def test_a019_unaliased_target_repeated_in_from():
    r = orchestrator.convert("UPDATE #tempCity SET StateCode = S.StateCode FROM #tempCity INNER JOIN Ref_State S ON S.StateId = #tempCity.StateId")
    assert flat(r.sql) == "UPDATE tempCity SET StateCode = S.StateCode FROM Ref_State AS S WHERE S.StateId = tempCity.StateId;"


def test_a055_set_target_qualifier_dropped():
    r = orchestrator.convert("UPDATE c SET c.StateCode = S.StateCode, c.StateName = S.StateName FROM #tempCity c INNER JOIN Ref_State S ON S.StateId = c.StateId")
    assert "SET StateCode = S.StateCode, StateName = S.StateName" in flat(r.sql)
    assert "A055" in codes(r)


def test_a056_recursive_cte_and_date_arithmetic(typed):
    r = convert(";WITH DateCTE AS (SELECT CAST(@d AS DATE) AS DateValue UNION ALL "
                "SELECT DATEADD(DAY, 1, DateValue) FROM DateCTE WHERE DateValue < @e) SELECT * FROM DateCTE")
    sql = flat(r.sql)
    assert sql.startswith("WITH RECURSIVE DateCTE AS (")
    # DATEADD on a DATE returned a DATE; date + interval is a timestamp in PostgreSQL
    assert "SELECT CAST(DateValue + INTERVAL '1 DAY' AS DATE) FROM DateCTE" in sql
    assert "A056" in codes(r)


def test_a051_string_alias_is_a_plain_name():
    r = orchestrator.convert("SELECT OG.OrgName AS 'CreatedBy', 'x' AS 'Is Full Day' INTO #Z FROM Organization OG; "
                             "SELECT CreatedBy FROM #Z")
    sql = flat(r.sql)
    assert "OG.OrgName AS CreatedBy," in sql and "'x' AS \"Is Full Day\"" in sql


# --------------------------------------------------------------------------
# A054 -- implicit conversions from column_types.txt
# --------------------------------------------------------------------------

def test_a054_integer_column_against_text_column(typed):
    r = convert("SELECT FS.ListId FROM FileConfiguration DC LEFT JOIN System_Lookup FS ON FS.ListId = DC.FileSizeUOM")
    assert "FS.ListId = CAST(NULLIF(TRIM(DC.FileSizeUOM), '') AS INTEGER)" in flat(r.sql)
    assert "A054" in codes(r)


def test_a054_text_column_against_number_literal(typed):
    r = convert("SELECT 1 FROM Client_Lookup CLK WHERE CLK.SiteId = 0")
    assert "CLK.SiteId = '0'" in flat(r.sql)


def test_a054_number_in_split_list(typed):
    r = convert("SELECT ListId FROM System_Lookup SL WHERE SL.ListId IN (SELECT value FROM STRING_SPLIT(@Ids, ','))")
    assert re.search(r"CAST\(NULLIF\(TRIM\(value\), ''\) AS INTEGER\)", r.sql), r.sql


def test_a054_like_on_a_number(typed):
    r = convert("SELECT 1 FROM Ref_FxRate E WHERE E.CostRate LIKE '%' + @s + '%'")
    assert "CAST(E.CostRate AS TEXT) LIKE" in flat(r.sql)


def test_a054_coalesce_number_and_date_with_empty_string(typed):
    r = convert("SELECT COALESCE(SLV.ParentId, '') AS p, COALESCE(V.LastModified, '') AS d "
                "FROM System_Lookup SLV, Ref_Machine V")
    out = flat(r.sql)
    assert "COALESCE(SLV.ParentId, 0)" in out
    assert "COALESCE(V.LastModified, '1900-01-01')" in out


def test_a054_date_compared_with_empty_string(typed):
    r = convert("SELECT CASE WHEN (VM.LastModified IS NULL OR VM.LastModified = '') THEN VM.Created END FROM Ref_Machine VM")
    assert "VM.LastModified = '1900-01-01'" in flat(r.sql)


def test_a054_case_number_and_empty_string_needs_no_schema():
    r = convert("SELECT CASE WHEN a = 1 THEN 1 ELSE '' END AS OrderVal FROM t")
    assert "THEN 1 ELSE 0 END" in flat(r.sql)


def test_a054_case_number_and_text_branch(typed):
    r = convert("SELECT CASE WHEN @m = 'X' THEN 0 ELSE CAST(ParentId AS VARCHAR(10)) END FROM System_Lookup")
    assert "THEN '0' ELSE" in flat(r.sql)


def test_a054_case_boolean_branch_gets_true_false(typed):
    r = convert("SELECT 1 FROM Ref_UOM MUOM WHERE COALESCE(MUOM.IsActive, 0) = "
                "CASE WHEN @IsAll = 1 THEN COALESCE(MUOM.IsActive, 0) ELSE 1 END")
    assert "ELSE TRUE END" in flat(r.sql)


def test_a054_text_column_in_number_subquery(typed):
    r = convert("SELECT 1 FROM Ref_Class MH WHERE MH.SourceCountry IN (SELECT CountryId FROM Ref_Country)")
    assert "CAST(NULLIF(TRIM(MH.SourceCountry), '') AS INTEGER) IN" in flat(r.sql)


def test_a054_text_into_fntolocaltime_and_to_char(typed):
    r = convert("SELECT FORMAT(dbo.fnToLocalTime(SD.BillDt, @TimeZone), 'dd/MM/yyyy') FROM Order_Details SD")
    out = flat(r.sql)
    assert "FNTOLOCALTIME(CAST(SD.BillDt AS TIMESTAMPTZ), @TimeZone)" in out      # timestamptz overload (A025)


def test_a054_without_column_types_changes_nothing_schema_dependent():
    r = convert("SELECT FS.ListId FROM FileConfiguration DC LEFT JOIN System_Lookup FS ON FS.ListId = DC.FileSizeUOM")
    assert "CAST(NULLIF" not in r.sql
    assert "A054" not in codes(r)


def test_a054_unknown_table_is_left_alone(typed):
    r = convert("SELECT 1 FROM Unknown_Table U WHERE U.SiteId = 0")
    assert "U.SiteId = 0" in flat(r.sql)


def test_a054_never_breaks_unparseable_output():
    sql, n = typefix.apply("DO $$ BEGIN PERFORM 1; END $$;")
    assert n == 0 and sql == "DO $$ BEGIN PERFORM 1; END $$;"


# --------------------------------------------------------------------------
# A035 -- DATEDIFF with TIME operands / a time-only literal
# --------------------------------------------------------------------------

def test_a035_time_only_literal_and_time_cast():
    r = convert("SELECT DATEDIFF(MINUTE, '00:00', CAST(S.SlotDuration AS TIME)) FROM Slot S")
    out = flat(r.sql)
    assert "CAST('1900-01-01 00:00' AS TIMESTAMP)" in out
    assert "CAST('1900-01-01' AS DATE) + CAST(S.SlotDuration AS TIME)" in out
    assert "CAST('00:00' AS TIMESTAMP)" not in out


# --------------------------------------------------------------------------
# L031 / markers
# --------------------------------------------------------------------------

def test_l031_parameter_starting_with_an_infra_static_key():
    r = orchestrator.convert("SELECT * FROM t WHERE a IN (SELECT id FROM u WHERE b = @UserIdList) AND c = @UserId")
    hits = [w for w in r.warnings if w.code == "L031"]
    assert hits and hits[0].message.startswith("Parameter name(s) @UserIdList start with")


def test_l031_exact_keys_are_fine():
    r = orchestrator.convert("SELECT * FROM t WHERE c = @UserId AND d = @Tenant_Admin_Id")
    assert "L031" not in codes(r)


def test_typed_date_literal_is_not_string_concat():
    assert markers.find("SELECT DATE '1900-01-01' + CAST(x AS TIME) FROM t") == ([], [])
    assert "string + concat" in markers.find("SELECT 'a' + b FROM t")[1]


# --------------------------------------------------------------------------
# schema files
# --------------------------------------------------------------------------

DDL = """
CREATE TABLE ref_vendor (
    vendorid integer GENERATED BY DEFAULT AS IDENTITY,
    code citext NOT NULL,
    isactive boolean NOT NULL DEFAULT TRUE,
    remarks boolean,
    rate numeric(10,2),
    CONSTRAINT "PK_ref_vendor" PRIMARY KEY (vendorid)
);
CREATE TABLE public.other (id int, remarks text, "Active" boolean);
"""


def test_schema_from_ddl_parses_tables_and_types():
    tables = schema_from_ddl.parse_ddl(DDL)
    assert tables["ref_vendor"]["code"] == "citext"
    assert tables["ref_vendor"]["rate"] == "numeric(10,2)"
    assert "pk_ref_vendor" not in tables["ref_vendor"]
    assert tables["other"]["active"] == "boolean"


def test_schema_from_ddl_boolean_names_skip_ambiguous():
    names = schema_from_ddl.boolean_names(schema_from_ddl.parse_ddl(DDL))
    assert names == ["active", "isactive"]          # remarks is text elsewhere


def test_schema_from_ddl_writes_both_files(tmp_path):
    ddl = tmp_path / "s.sql"
    ddl.write_text(DDL, encoding="utf-8")
    keep = tmp_path / "keep.txt"
    keep.write_text("entrystatus\nremarks\n", encoding="utf-8")
    assert schema_from_ddl.main([str(ddl), "--out", str(tmp_path), "--merge-boolean", str(keep)]) == 0
    assert schema_hints.parse((tmp_path / "boolean_columns.txt").read_text(encoding="utf-8")) == \
        frozenset({"active", "isactive", "entrystatus"})
    types = schema_hints.parse_types((tmp_path / "column_types.txt").read_text(encoding="utf-8"))
    assert types["ref_vendor"]["rate"] == "numeric" and types["ref_vendor"]["code"] == "text"


def test_type_family():
    assert schema_hints.type_family("character varying(50)") == "text"
    assert schema_hints.type_family("timestamp with time zone") == "timestamp"
    assert schema_hints.type_family("bigint") == "integer"
    assert schema_hints.type_family("geometry") == "other"


# --------------------------------------------------------------------------
# PC13 / PC14 -- Check-PG on already-converted code
# --------------------------------------------------------------------------

def test_pc13_drops_quoted_placeholder_schema():
    from core import pgcheck
    out, n = pgcheck.repair_placeholder_schemas(
        """SELECT a FROM "@AppDbName".Ref_Tax AS T JOIN "@DBName".X ON 1 = 1 WHERE note = '"@DBName".keep'""")
    assert n == 2
    assert out == """SELECT a FROM Ref_Tax AS T JOIN X ON 1 = 1 WHERE note = '"@DBName".keep'"""


def test_pc13_respects_schema_mode(monkeypatch):
    from core import pgcheck
    monkeypatch.setenv(schema_hints.PLACEHOLDER_ENV_VAR, "schema")
    assert pgcheck.repair_placeholder_schemas('SELECT a FROM "@AppDbName".T')[1] == 0


def test_pc14_applies_a054_to_converted_code(typed):
    from core import pgcheck
    out, n = pgcheck.repair_implicit_conversions(
        "SELECT 1 FROM Client_Lookup AS CLK WHERE CLK.SiteId = 0")
    assert n == 1 and "CLK.SiteId = '0'" in out


# --------------------------------------------------------------------------
# v2.16.0 round 2 -- batch-declared types, booleans, DISTINCT/ORDER BY, text +
# --------------------------------------------------------------------------

def test_a007_integer_flag_from_a_temp_table_is_not_boolean():
    r = convert("SELECT CASE WHEN COUNT(1) > 0 THEN 1 ELSE 0 END AS IsRemoteConfig INTO #RemoteFlag FROM T; "
                "SELECT 1 FROM #RemoteFlag f WHERE f.IsRemoteConfig = 1")
    assert "WHERE f.IsRemoteConfig = 1" in flat(r.sql)


def test_a007_varchar_flag_declared_in_temp_table_keeps_its_text():
    r = convert("CREATE TABLE #templst (DocNo VARCHAR(50), IsOfferExpire VARCHAR(10)); "
                "SELECT DocNo FROM #templst WHERE IsOfferExpire = 'false'")
    assert "WHERE IsOfferExpire = 'false'" in flat(r.sql)


def test_a007_boolean_compared_with_a_bit_case():
    r = convert("SELECT 1 FROM Ref_Door WHERE ISNULL(IsActive, 0) = (CASE WHEN @s = 'Active' THEN 1 ELSE 0 END)")
    assert "COALESCE(IsActive, FALSE) = ( CASE WHEN @s = 'Active' THEN TRUE ELSE FALSE END )" in flat(r.sql)


def test_a054_like_on_a_boolean_matches_bit_text(typed):
    r = convert("SELECT * INTO #TempCT FROM (SELECT CT.UomId, CT.IsActive FROM Ref_UOM CT) AS TC; "
                "SELECT IsActive FROM #TempCT WHERE IsActive LIKE '%' + @s + '%' GROUP BY IsActive")
    assert "WHERE CASE IsActive WHEN TRUE THEN '1' WHEN FALSE THEN '0' END LIKE" in flat(r.sql)


def test_a054_union_of_boolean_and_integer_is_integer():
    r = convert("SELECT ISNULL(l.IsActive, 0) AS IsParent, l.x FROM Ref_UOM l UNION SELECT 0 AS IsParent, 1")
    assert "CAST(COALESCE(l.IsActive, FALSE) AS INTEGER) AS IsParent" in flat(r.sql)


def test_a054_text_plus_text_is_concatenation():
    r = convert("CREATE TABLE #FD (ProviderCode VARCHAR(3), BatchNumber VARCHAR(5)); "
                "SELECT FD.ProviderCode + FD.BatchNumber AS Batch FROM #FD FD")
    assert "FD.ProviderCode || FD.BatchNumber AS Batch" in flat(r.sql)


def test_a054_coalesce_text_with_number_default(typed):
    r = convert("SELECT ISNULL(H.SourceCountry, 0) AS c FROM Ref_Class H "
                "LEFT JOIN Ref_Country Cnt ON Cnt.CountryId = ISNULL(H.SourceCountry, 0)")
    sql = flat(r.sql)
    assert "COALESCE(H.SourceCountry, '0') AS c" in sql
    assert "Cnt.CountryId = COALESCE(CAST(NULLIF(TRIM(H.SourceCountry), '') AS INTEGER), 0)" in sql


def test_a054_convert_varchar_of_text_is_the_text():
    r = convert("SELECT FORMAT(CT.ValidFrom, 'dd MMM yyyy') AS VALIDFrom INTO #T FROM Ref_Machine CT; "
                "SELECT CONVERT(VARCHAR, VALIDFrom, 120) AS VALIDFrom FROM #T")
    assert "SELECT CAST(VALIDFrom AS VARCHAR) AS VALIDFrom FROM T" in flat(r.sql)


def test_a054_param_literal_is_not_text():
    r = convert("SELECT TOP (CASE WHEN ISNULL('@TopRecords', '') = '' THEN 10000 ELSE '@TopRecords' END) Code FROM Ref_Tax")
    assert "THEN 10000 ELSE '@TopRecords' END" in flat(r.sql)


def test_a054_insert_select_converts_to_the_declared_column():
    r = convert("CREATE TABLE #x (AllowedSize INT); CREATE TABLE #d (MaxFileSize VARCHAR(10)); "
                "INSERT INTO #x (AllowedSize) SELECT DC.MaxFileSize FROM #d DC")
    assert "SELECT CAST(NULLIF(TRIM(DC.MaxFileSize), '') AS INTEGER) FROM d AS DC" in flat(r.sql)


def test_a057_distinct_ordered_by_an_unselected_column():
    r = convert("SELECT DISTINCT CAST(SLV.ListId AS NVARCHAR(MAX)) AS Value, SLV.Description AS Name "
                "FROM System_Lookup SLV ORDER BY SLV.ListId DESC")
    sql = flat(r.sql)
    assert "SELECT CAST(SLV.ListId AS TEXT) AS Value, SLV.Description AS Name FROM System_Lookup AS SLV "            "GROUP BY 1, 2 ORDER BY MAX(SLV.ListId) DESC" in sql
    assert "A057" in codes(r)


def test_a057_selected_order_key_is_left_alone():
    r = convert("SELECT DISTINCT a.Name AS Name FROM T a ORDER BY a.Name")
    assert "SELECT DISTINCT a.Name AS Name" in flat(r.sql) and "A057" not in codes(r)


def test_a012_cross_apply_split_keeps_the_value_column():
    r = convert("SELECT T.Id, LTRIM(RTRIM(value)) AS s FROM T CROSS APPLY STRING_SPLIT(T.List, ',') AS SplitValues")
    assert "STRING_TO_TABLE(T.List, ',') AS SplitValues(value)" in flat(r.sql)


def test_a051_string_alias_glued_to_the_next_word():
    r = convert("Select RegionName as 'Name'from Ref_Region A")
    assert flat(r.sql) == "SELECT RegionName AS Name FROM Ref_Region AS A;"


# --------------------------------------------------------------------------
# A058 -- IF / ELSE flattened into guarded statements
# --------------------------------------------------------------------------

def test_a058_if_else_becomes_guarded_union_all():
    r = convert("Declare @SearchVal nvarchar(25) SET @SearchVal = '@SearchString' "
                "if( @SearchVal = '') Begin select TOP 10 MC.Name, 1 AS IsActive from Ref_Vendor MC "
                "WHERE ISNULL(MC.IsDeleted,0)=0 ORDER BY MC.Name END "
                "ELSE BEGIN select MC.Name, NULL AS IsActive from Ref_Vendor MC WHERE MC.Name LIKE '%' + @SearchVal + '%' END")
    sql = flat(r.sql)
    guard = "CASE WHEN ( ( CAST(( '@SearchString' ) AS VARCHAR(25)) = '' ) ) THEN 1 ELSE 0 END"
    assert sql.startswith(f"( SELECT MC.Name, 1 AS IsActive FROM Ref_Vendor AS MC WHERE ( {guard} = 1 ) AND (")
    # the bare NULL takes the other branch's type (A054)
    assert "ORDER BY MC.Name NULLS FIRST LIMIT 10 ) UNION ALL SELECT MC.Name, CAST(NULL AS INTEGER) AS IsActive" in sql
    assert f"WHERE ( {guard} = 0 ) AND ( MC.Name LIKE" in sql
    assert "A058" in codes(r) and "A031" in codes(r)


def test_a058_star_from_a_temp_table_pairs_with_its_columns():
    r = convert("SELECT Id, Name INTO #t FROM Ref_Vendor; IF @x = 1 BEGIN SELECT * FROM #t a END "
                "ELSE BEGIN SELECT Id, Name FROM #t WHERE Id > 5 END; DROP TABLE #t")
    sql = flat(r.sql)
    assert "SELECT * FROM t AS a WHERE CASE WHEN ( @x = 1 ) THEN 1 ELSE 0 END = 1 UNION ALL " \
           "SELECT Id, Name FROM t WHERE ( CASE WHEN ( @x = 1 ) THEN 1 ELSE 0 END = 0 ) AND ( Id > 5 );" in sql


def test_a058_aggregate_without_group_by_is_wrapped():
    r = convert("IF @Mode = 'A' BEGIN SELECT COUNT(*) AS n FROM Ref_Vendor END ELSE BEGIN SELECT COUNT(*) AS n FROM Ref_Depot END")
    sql = flat(r.sql)
    assert sql.startswith("SELECT * FROM ( SELECT COUNT(*) AS n FROM Ref_Vendor ) AS _if1 WHERE")
    assert "UNION ALL SELECT * FROM ( SELECT COUNT(*) AS n FROM Ref_Depot ) AS _if2" in sql


def test_a058_guards_dml_in_a_branch():
    r = convert("IF @x = 1 UPDATE Ref_Depot SET Name = 'a' WHERE Id = 3 ELSE DELETE FROM Ref_Depot WHERE Id = 4")
    sql = flat(r.sql)
    assert "UPDATE Ref_Depot SET Name = 'a' WHERE ( CASE WHEN ( @x = 1 ) THEN 1 ELSE 0 END = 1 ) AND ( Id = 3 );" in sql
    assert "DELETE FROM Ref_Depot WHERE ( CASE WHEN ( @x = 1 ) THEN 1 ELSE 0 END = 0 ) AND ( Id = 4 );" in sql


def test_a058_refuses_a_conditional_assignment():
    r = orchestrator.convert("DECLARE @y INT IF @x = 1 SET @y = 1 ELSE SET @y = 2 SELECT @y")
    assert "A058" not in codes(r)


def test_a058_refuses_branches_with_different_columns():
    r = orchestrator.convert("IF @x = 1 SELECT Id FROM Ref_Depot ELSE SELECT Name FROM Ref_Depot")
    assert "A058" not in codes(r)


def test_a054_string_agg_of_a_number_is_text():
    r = convert("CREATE TABLE #m (BranchId INT); SELECT STRING_AGG(m.BranchId, ',') AS b FROM #m m")
    assert "STRING_AGG(CAST(m.BranchId AS TEXT), ',') AS b" in flat(r.sql)


# --------------------------------------------------------------------------
# A031 v2.16.0 -- dead DECLARE defaults, SELECT @v = ..., statement ends
# --------------------------------------------------------------------------

def test_a031_default_overwritten_before_use():
    r = convert("DECLARE @VarSearch NVARCHAR(MAX) = '' DECLARE @VarTop INT = 0 SET @VarSearch = '@SearchValue' "
                "SET @VarTop = '@TopRecords' SELECT TOP (@VarTop) Code FROM Ref_Tax WHERE Code LIKE '%' + @VarSearch + '%'")
    assert flat(r.sql) == ("SELECT Code FROM Ref_Tax WHERE Code LIKE '%' || CAST(( '@SearchValue' ) AS TEXT) || '%' "
                           "LIMIT CAST(( '@TopRecords' ) AS INT);")


def test_a031_select_assignment_keeps_its_from_on_the_next_line():
    r = convert("DECLARE @TotalBay INT\nSELECT @TotalBay = COUNT(BayConfigDetailsId)\nFROM #tempBay\n"
                "UPDATE SCH_SlotDuration SET TotalNoOfBay = @TotalBay WHERE Id = @Id")
    assert flat(r.sql) == ("UPDATE SCH_SlotDuration SET TotalNoOfBay = CAST(( ( SELECT COUNT(BayConfigDetailsId) "
                           "FROM tempBay LIMIT 1 ) ) AS INT) WHERE Id = @Id;")


def test_a031_select_assignment_refused_when_the_table_changes_later():
    r = orchestrator.convert("DECLARE @n INT SELECT @n = COUNT(*) FROM #t INSERT INTO #t (Id) VALUES (1) SELECT @n AS n")
    assert "A031" not in codes(r)


def test_a007_cast_as_bit_compared_with_an_integer():
    r = convert("DECLARE @IsScheduled BIT\nSELECT @IsScheduled = D.FlagType\nFROM SystemConfig_Details AS D\n"
                "SELECT Id FROM Ref_Vendor WHERE ISNULL(@IsScheduled, 0) = 1")
    assert "AS BOOLEAN), FALSE ) = TRUE" in flat(r.sql)


def test_a054_number_against_an_inlined_text_variable():
    r = convert("CREATE TABLE #m (Id INT); DECLARE @Sel NVARCHAR(MAX) = '' SET @Sel = '@SelectedValue' "
                "SELECT CASE WHEN m.Id = @Sel THEN 0 ELSE 1 END AS o FROM #m m")
    assert "m.Id = CAST(NULLIF(TRIM(CAST(( '@SelectedValue' ) AS TEXT)), '') AS INTEGER)" in flat(r.sql)


def test_a054_number_against_empty_string_is_zero():
    r = convert("DECLARE @Top INT = 0 SET @Top = '@TopRecords' "
                "SELECT TOP (CASE WHEN ISNULL(@Top, 0) = '' THEN 10 ELSE @Top END) Code FROM Ref_Tax")
    assert "WHEN COALESCE(CAST(( '@TopRecords' ) AS INT), 0) = 0 THEN 10" in flat(r.sql)


# --------------------------------------------------------------------------
# scan v2.16.0 -- a piece of a query assembled in C# is never converted alone
# --------------------------------------------------------------------------

def test_scan_composed_query_pieces_are_not_rewritten():
    from core import csharp
    src = (
        'class A {\n'
        '  string One() { StringBuilder query = new StringBuilder(); query.Append(@"SELECT a FROM t WHERE x = 1"); '
        'return query.ToString(); }\n'
        '  string Two(bool f) { StringBuilder query = new StringBuilder(); query.Append(@"SELECT a FROM t WHERE x = 1");\n'
        '     if (f) { query.Append(@" AND y = 2"); } return query.ToString(); }\n'
        '  string q3 = "SELECT a FROM t WHERE x = 1" + extra;\n'
        '  void M() { var sql = "SELECT c FROM t WHERE 1 = 1"; sql += " AND d = 1"; }\n'
        '}')
    verdicts = {lit.line: csharp.composed_query(src, lit) for lit in csharp.find_sql_literals(src)}
    assert verdicts[2] is None
    assert "Append" in verdicts[3]
    assert "+" in verdicts[5]
    assert "+=" in verdicts[6]
