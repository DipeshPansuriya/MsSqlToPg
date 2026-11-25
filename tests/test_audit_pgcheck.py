"""v2.13.0 -- audit mode (report remaining T-SQL) and check-PG mode (repair
already-converted PostgreSQL without re-converting it)."""

from __future__ import annotations

from pathlib import Path

from core import audit, csharp, markers, pgcheck


def _write(tmp_path: Path, name: str, body: str) -> Path:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8", newline="")
    return path


# ---------------------------------------------------------------- markers
def test_markers_skip_comments_and_strings():
    hard, soft = markers.find("SELECT a FROM t -- #master is a temp table, ISNULL(\n"
                              "WHERE b = 'SELECT TOP 1'")
    assert hard == [] and soft == []


def test_markers_n_prefix_is_token_aware():
    # regression: a string ENDING in " N" matched the N'' marker (15 false
    # refusals in one query file on the first real-repo dry run)
    assert markers.find("SELECT CASE WHEN a THEN 'Y / N' END AS b FROM t") == ([], [])
    assert "N'unicode literal" in markers.find("SELECT a FROM t WHERE b = N'x'")[1]


def test_markers_ignore_postgres_if_exists():
    hard, _ = markers.find("DROP TABLE IF EXISTS pg_temp.x; ALTER TABLE t ADD COLUMN IF NOT EXISTS c INT")
    assert hard == []


def test_markers_find_control_flow_and_cursor():
    hard, _ = markers.find("IF EXISTS (SELECT 1 FROM t) BEGIN DECLARE c CURSOR FOR SELECT a FROM t END")
    assert "IF/BEGIN control flow" in hard and "CURSOR" in hard


# ---------------------------------------------------------------- csharp
def test_batch_starting_with_declare_is_now_a_statement():
    assert csharp.looks_like_sql("DECLARE @x INT = 1\nSELECT a FROM t WHERE b = @x")
    assert csharp.looks_like_sql("SET NOCOUNT ON; SELECT a FROM t")


def test_prose_starting_with_if_is_not_sql():
    assert not csharp.looks_like_sql("If you select from the list, press Save.")


def test_fragment_markers_include_shared_set():
    assert "CURSOR" in csharp.tsql_markers("DECLARE c CURSOR FOR SELECT a FROM t WHERE x = 1")


# ---------------------------------------------------------------- audit
SOURCE = '''
public static class Q
{
    public const string Live = @"SELECT TOP 1 a FROM t WITH(NOLOCK) WHERE ISNULL(b,0) = 0";
    public const string Dead = @"IF EXISTS (SELECT 1 FROM t) BEGIN SELECT a INTO #x FROM t END";
    public const string Done = @"SELECT a FROM t WHERE b = @b LIMIT 1";
    public static string Build(int id)
    {
        string sql = $@"SELECT a FROM t WHERE id = {id}";
        sql += @" AND c IN (SELECT c FROM #PDTemp)";
        return sql + Q.Live;
    }
}
'''


def test_audit_counts_live_dead_and_tails(tmp_path):
    _write(tmp_path, "Q.cs", SOURCE)
    report = audit.run(tmp_path)
    by_name = {r.name: r for r in report.rows}
    assert by_name["Live"].status == "MECHANICAL" and by_name["Live"].live
    assert by_name["Dead"].status == "REWRITE" and not by_name["Dead"].live
    assert by_name["Done"].status == "PG"
    # the `sql +=` tail holding #PDTemp is grouped with its interpolated head
    assert by_name["sql"].status == "REWRITE" and by_name["sql"].interpolated
    path = audit.write_report(report)
    assert path and path.exists() and (tmp_path / "tsql-audit.csv").exists()
    text = path.read_text(encoding="utf-8")
    assert "still T-SQL -- live" in text


# ---------------------------------------------------------------- check-pg
def test_check_sql_repairs_known_defects():
    sql = ("SELECT fnToLocalTime(CAST(j.ReceivedTime AS TIMESTAMP), @TimeZone) AS t, "
           "NUMBER_TO_STR(j.Amount, 'N2') AS amt, CURRENT_TIMESTAMP_L_T_Z() AS n, "
           "YEAR(j.d) AS y FROM Job j INNER JOIN Term TON T.id = j.tid LIMIT 5")
    result = pgcheck.check_sql(sql)
    out = result.sql
    assert "fnToLocalTime(j.ReceivedTime, @TimeZone)" in out
    assert "TO_CHAR(j.Amount, 'FM999,999,999,999,990.00')" in out
    assert "CURRENT_TIMESTAMP_L_T_Z" not in out
    assert "CAST(EXTRACT(YEAR FROM j.d) AS INT)" in out
    assert "T ON T.id" in out
    assert result.valid_after


def test_check_sql_naive_utc_now():
    out = pgcheck.check_sql(
        "SELECT fnToLocalTime((NOW() AT TIME ZONE 'UTC'), @tz) AS t LIMIT 1").sql
    assert "fnToLocalTime(NOW(), @tz)" in out


def test_check_sql_temp_table_guards():
    out = pgcheck.check_sql("CREATE TEMP TABLE x AS SELECT 1 AS a; SELECT * FROM x; DROP TABLE x;").sql
    assert out.startswith("DROP TABLE IF EXISTS pg_temp.x;")
    assert out.rstrip().endswith("DROP TABLE IF EXISTS pg_temp.x;")


def test_check_sql_booleans_need_the_schema_list(tmp_path, monkeypatch):
    from core import schema_hints
    sql = "SELECT a FROM t WHERE COALESCE(t.IsActive, 0) = 1 AND t.IsDeleted = 0 LIMIT 5"
    listing = tmp_path / "b.txt"
    listing.write_text("isdeleted\n", encoding="utf-8")
    monkeypatch.setenv(schema_hints.ENV_VAR, str(listing))
    schema_hints.reset()
    try:
        result = pgcheck.check_sql(sql)
        assert "t.IsDeleted = FALSE" in result.sql           # listed -> repaired
        assert "COALESCE(t.IsActive, 0) = 1" in result.sql   # naming only -> reported
        assert any(w.code == "L028" for w in result.findings)
    finally:
        schema_hints.reset()


def test_check_sql_boolean_coalesce_compare_is_converted_too():
    # regression from the first real-repo dry run (StatusUpdate_Query):
    # COALESCE(EJM.IsCancelJob,0)=0 became COALESCE(.., FALSE)=0 -- still 42883
    out = pgcheck.check_sql(
        "SELECT a FROM t WHERE COALESCE(EJM.defaultsite,0)=0 LIMIT 1").sql
    assert "COALESCE(EJM.defaultsite, FALSE)=FALSE" in out
    out = pgcheck.check_sql(
        "SELECT a FROM t WHERE COALESCE(EJM.defaultsite, FALSE) = 1 LIMIT 1").sql
    assert "COALESCE(EJM.defaultsite, FALSE) = TRUE" in out


def test_correct_boolean_coalesce_is_not_a_suspect():
    assert pgcheck.boolean_suspects("SELECT a FROM t WHERE COALESCE(t.IsActive, FALSE) = TRUE") == []
    assert pgcheck.boolean_suspects("SELECT a FROM t WHERE COALESCE(t.IsActive, 0) = 1") == ["IsActive"]


def test_check_sql_dotnet_millisecond_token_in_mask():
    out = pgcheck.check_sql("SELECT TO_CHAR(a, 'YYYY-MM-DDTHH24:MI:SS.fffZ') AS s FROM t LIMIT 1").sql
    assert "'YYYY-MM-DD\"T\"HH24:MI:SS.MSZ'" in out


def test_check_sql_does_not_touch_parameters():
    from core import schema_hints
    schema_hints.reset()
    out = pgcheck.check_sql("SELECT a FROM t WHERE @IsOutbound = 0 AND defaultsite = 1 LIMIT 1").sql
    assert "@IsOutbound = 0" in out and "defaultsite = TRUE" in out


def test_check_sql_without_pg_evidence_leaves_booleans():
    out = pgcheck.check_sql("SELECT a FROM t WHERE defaultsite = 1").sql
    assert "defaultsite = 1" in out


def test_check_sql_mask_t_before_hh_only_inside_to_char():
    out = pgcheck.check_sql("SELECT TO_CHAR(a, 'YYYY-MM-DDTHH24:MI') AS s, DTHH FROM t LIMIT 1").sql
    assert "'YYYY-MM-DD\"T\"HH24:MI'" in out and ", DTHH FROM" in out


def test_check_run_rewrites_file_with_backup(tmp_path):
    body = ('public class Q {\r\n'
            '    public const string A = @"SELECT fnToLocalTime(CAST(x.D AS TIMESTAMP), @Tz) AS t\r\n'
            '        FROM x LIMIT 1";\r\n'
            '    public const string B = @"SELECT TOP 1 a FROM t WITH(NOLOCK)";\r\n'
            '}\r\n')
    path = _write(tmp_path, "Q.cs", body)
    dry = pgcheck.run(tmp_path, dry_run=True)
    assert dry.total("fixed") == 1 and dry.total("tsql") == 1
    assert path.read_bytes().decode("utf-8") == body           # dry run wrote nothing
    real = pgcheck.run(tmp_path, dry_run=False)
    assert real.total("fixed") == 1
    new = path.read_bytes().decode("utf-8")
    assert "fnToLocalTime(x.D, @Tz)" in new
    assert "SELECT TOP 1 a FROM t WITH(NOLOCK)" in new          # T-SQL never touched
    assert "\r\r\n" not in new and "\r\n        FROM x LIMIT 1" in new
    assert (tmp_path / "Q.cs.bak").read_bytes().decode("utf-8") == body
    again = pgcheck.run(tmp_path, dry_run=True)
    assert again.total("fixed") == 0                           # idempotent
    assert pgcheck.write_report(real).exists()
