"""Liquibase changelog conversion tests.

This feature APPENDS TO CHANGELOGS THAT DRIVE PRODUCTION DEPLOYMENTS. A twin
that does not run is worse than no twin: Liquibase stops the whole update and
the release fails. So every safety rule in core/liquibase.py's docstring has a
test here, and the central one is `final_check` -- nothing is ever emitted
unless the assembled text parses as real PostgreSQL.
"""

from __future__ import annotations

import pytest

from core import liquibase

HEADER = "--liquibase formatted sql\n\n"

ADD_COLUMN = HEADER + """--changeset app-team:2026-06-10-BinLocation-mssql dbms:mssql
--comment: Add BinLocation column to ParentOrder
IF NOT EXISTS (
    SELECT 1 FROM sys.columns
    WHERE Name = 'BinLocation' AND Object_ID = OBJECT_ID('ParentOrder')
)
BEGIN
ALTER TABLE ParentOrder
ADD BinLocation NVARCHAR(100) NULL;
END

--rollback ALTER TABLE [ParentOrder] DROP COLUMN [BinLocation];
"""

ALREADY_TWINNED = ADD_COLUMN + """
--changeset app-team:2026-06-10-BinLocation-postgresql dbms:postgresql
ALTER TABLE ParentOrder ADD COLUMN IF NOT EXISTS BinLocation VARCHAR(100) NULL;
"""

DUPLICATE_IDS = HEADER + """--changeset db-team:2026-04-28-T100 dbms:mssql
ALTER TABLE PasswordReset ADD TokenHash NVARCHAR(64) NULL;

--changeset db-team:2026-04-28-T100 dbms:postgresql
ALTER TABLE PasswordReset ADD COLUMN IF NOT EXISTS TokenHash VARCHAR(64);
"""

# A real defect: a dbms:postgresql changeset that is a verbatim copy
# of its MSSQL twin and dies on deploy.
TSQL_IN_PG_CHANGESET = HEADER + """--changeset app-team:2026-03-12-guid-pg dbms:postgresql
IF NOT EXISTS (SELECT * FROM sys.columns WHERE object_id = OBJECT_ID(N'dbo.JobDetails') AND name = 'RowGuid')
BEGIN
    ALTER TABLE dbo.JobDetails ADD RowGuid UNIQUEIDENTIFIER NULL
END
"""

UNSUPPORTED_GUARD = HEADER + """--changeset t:proc-mssql dbms:mssql
IF NOT EXISTS (SELECT 1 FROM sys.objects WHERE name = 'x')
BEGIN
    DECLARE @n INT = 1;
    WHILE @n < 10 BEGIN SET @n = @n + 1; END
END
"""


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------

def test_parses_changeset_identity_and_attributes():
    changesets = liquibase.parse(ADD_COLUMN)
    assert len(changesets) == 1
    changeset = changesets[0]
    assert changeset.author == "app-team"
    assert changeset.id == "2026-06-10-BinLocation-mssql"
    assert changeset.dbms == "mssql"
    assert changeset.comment == "Add BinLocation column to ParentOrder"


def test_rollback_and_comment_are_not_part_of_the_body():
    changeset = liquibase.parse(ADD_COLUMN)[0]
    assert "--rollback" not in changeset.body
    assert "--comment" not in changeset.body
    assert len(changeset.rollbacks) == 1
    assert "DROP COLUMN" in changeset.rollbacks[0]


def test_split_statements_attribute_is_read():
    text = HEADER + "--changeset a:b dbms:mssql splitStatements:false\nSELECT 1;\n"
    assert liquibase.parse(text)[0].split_statements_off is True


def test_is_changelog_requires_the_magic_first_line():
    assert liquibase.is_changelog(ADD_COLUMN)
    assert not liquibase.is_changelog("SELECT 1;")


def test_find_changelog_files_detects_by_content_not_folder(tmp_path):
    (tmp_path / "Changelogs").mkdir()
    (tmp_path / "Changelogs" / "a.sql").write_text(ADD_COLUMN, encoding="utf-8")
    (tmp_path / "plain.sql").write_text("SELECT 1;", encoding="utf-8")
    found = liquibase.find_changelog_files(tmp_path)
    assert [p.name for p in found] == ["a.sql"]


def test_build_output_is_excluded(tmp_path):
    for folder in ("bin", "obj", "node_modules"):
        (tmp_path / folder).mkdir()
        (tmp_path / folder / "gen.sql").write_text(ADD_COLUMN, encoding="utf-8")
    assert liquibase.find_changelog_files(tmp_path) == []


# --------------------------------------------------------------------------
# Defect detection (report-only)
# --------------------------------------------------------------------------

def test_duplicate_identity_is_detected():
    changesets = liquibase.parse(DUPLICATE_IDS)
    assert liquibase.duplicate_identities(changesets) == ["db-team:2026-04-28-T100"]


def test_duplicate_identity_is_reported_as_a_finding():
    _text, findings, _added = liquibase.process_text(DUPLICATE_IDS)
    assert any(f.code == "LB007" for f in findings)


def test_changeset_header_without_an_author_is_reported():
    """Real defect found in one repository: `--changeset <id> dbms:mssql` with
    no `author:` prefix. Liquibase cannot parse the file at all."""
    text = HEADER + "--changeset 2026-06-11-Add-Priority-sql dbms:mssql\nSELECT 1;\n"
    assert liquibase.malformed_headers(text)
    _out, findings, _added = liquibase.process_text(text)
    assert any(f.code == "LB008" for f in findings)


def test_changeset_header_missing_the_colon_is_reported():
    """Real defect found in one repository: `app-team2026-02-18-...` -- the
    author ran into the id because the colon was dropped."""
    text = HEADER + "--changeset team2026-02-18-TaxCode-mssql dbms:mssql\nSELECT 1;\n"
    assert any(code == "LB008" for _line, _h in liquibase.malformed_headers(text)
               for code in ["LB008"])


def test_wellformed_headers_are_not_flagged():
    assert liquibase.malformed_headers(ADD_COLUMN) == []


def test_existing_pg_changeset_containing_tsql_is_reported():
    _text, findings, added = liquibase.process_text(TSQL_IN_PG_CHANGESET)
    assert added == 0
    assert any(f.code == "LB009" for f in findings)


# --------------------------------------------------------------------------
# Guard unwrapping
# --------------------------------------------------------------------------

def test_column_guard_becomes_add_column_if_not_exists():
    body, _r, codes, reason = liquibase.convert_sql(
        liquibase.parse(ADD_COLUMN)[0].body)
    assert reason == ""
    assert "LB005" in codes
    assert "ADD COLUMN IF NOT EXISTS" in body.upper()
    assert "SYS.COLUMNS" not in body.upper()
    assert "BEGIN" not in body.upper()


def test_index_guard_becomes_create_index_if_not_exists_and_drops_go():
    sql = ("IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_A' "
           "AND object_id = OBJECT_ID('OrderBatch'))\nBEGIN\n"
           "CREATE NONCLUSTERED INDEX IX_A ON OrderBatch (IsDeleted, Created);\n"
           "END\nGO\n")
    body, _r, codes, reason = liquibase.convert_sql(sql)
    assert reason == ""
    assert {"LB004", "LB005"} <= set(codes)
    assert "CREATE INDEX IF NOT EXISTS" in body.upper()
    assert "NONCLUSTERED" not in body.upper()


def test_unrecognised_guard_shape_is_refused_not_guessed():
    changeset = liquibase.parse(UNSUPPORTED_GUARD)[0]
    _body, _r, _codes, reason = liquibase.convert_sql(changeset.body)
    assert reason != ""


def test_boolean_comparison_in_a_partial_index_is_fixed():
    """The real Import 2026-04-16 defect: `WHERE IsDeleted = 0` against a
    BOOLEAN column raises `operator does not exist: boolean = integer`."""
    sql = ("CREATE NONCLUSTERED INDEX IX_B ON ChildOrder (ParentId) "
           "INCLUDE (ChildId) WHERE IsDeleted = 0;")
    body, _r, _codes, reason = liquibase.convert_sql(sql)
    assert reason == ""
    assert "= FALSE" in body.upper()
    assert "= 0" not in body


# --------------------------------------------------------------------------
# The output guarantee
# --------------------------------------------------------------------------

def test_final_check_rejects_invalid_postgresql():
    assert liquibase.final_check("ALTER TABLE t ADD COLUMN c TEXT;") == ""
    assert liquibase.final_check("IF EXISTS (SELECT 1) BEGIN SELECT 1 END") != ""


def test_final_check_rejects_tsql_that_the_grammar_accepts():
    """PostgreSQL's grammar takes any unknown word as a type name, so
    NVARCHAR(MAX) PARSES and only fails at execution. Grammar validation alone
    cannot promise "runs without error" -- the residual gate must catch it."""
    sql = "ALTER TABLE t ADD c NVARCHAR(MAX) NULL;"
    assert liquibase.final_check(sql) != ""
    assert "NVARCHAR" in liquibase.final_check(sql).upper()


def test_boolean_compared_to_integer_is_caught():
    """`WHERE IsDeleted = 0` parses but raises
    `operator does not exist: boolean = integer` on a BOOLEAN column."""
    leftovers = liquibase.residual_tsql(
        "CREATE INDEX ix ON t (a) WHERE IsDeleted = 0;")
    assert any("boolean column compared" in item for item in leftovers)


def test_boolean_check_ignores_ordinary_integer_columns():
    assert liquibase.residual_tsql("SELECT * FROM t WHERE TenantProductId = 0;") == []


def test_plpgsql_if_exists_inside_a_do_block_is_not_flagged():
    """`IF EXISTS (...) THEN` is ordinary PL/pgSQL. Flagging it would reject
    every correctly converted DO $$ ... $$ changeset."""
    sql = ("DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns "
           "WHERE table_name = 't') THEN ALTER TABLE t ALTER COLUMN c TYPE "
           "numeric(18,4); END IF; END $$;")
    assert liquibase.residual_tsql(sql) == []
    assert liquibase.final_check(sql) == ""


def test_dbo_qualifier_is_refused():
    assert liquibase.residual_tsql("ALTER TABLE dbo.t ADD COLUMN c TEXT;")


def test_make_idempotent_output_still_parses():
    """make_idempotent edits text AFTER the engine validated it, so its result
    must be re-checked -- this is the gap that would otherwise ship broken DDL."""
    converted = "ALTER TABLE t ADD COLUMN c TEXT;"
    patched = liquibase.make_idempotent(converted, "column")
    assert "IF NOT EXISTS" in patched
    assert liquibase.final_check(patched) == ""


def test_every_emitted_statement_is_semicolon_terminated():
    body, _r, _c, reason = liquibase.convert_sql(
        liquibase.parse(ADD_COLUMN)[0].body)
    assert reason == ""
    assert body.rstrip().endswith(";")


def test_twin_is_only_emitted_when_conversion_is_usable(monkeypatch):
    """If the final syntax gate fails, no changeset may be written at all."""
    monkeypatch.setattr(liquibase, "final_check",
                        lambda _sql: "simulated invalid syntax")
    text, findings, added = liquibase.process_text(ADD_COLUMN)
    assert added == 0
    assert text == ADD_COLUMN
    assert any(f.action == liquibase.SKIPPED for f in findings)


# --------------------------------------------------------------------------
# Twin generation
# --------------------------------------------------------------------------

def test_twin_gets_a_distinct_changeset_id():
    """LB001 -- the defect that broke the Community release. Liquibase keys on
    id+author+file, so a shared id collapses both engines into one row."""
    text, _findings, added = liquibase.process_text(ADD_COLUMN)
    assert added == 1
    changesets = liquibase.parse(text)
    assert len(changesets) == 2
    assert len({c.identity for c in changesets}) == 2
    assert liquibase.duplicate_identities(changesets) == []


def test_twin_is_gated_on_postgresql():
    text, _findings, _added = liquibase.process_text(ADD_COLUMN)
    twin = liquibase.parse(text)[1]
    assert twin.dbms == "postgresql"


def test_original_changeset_is_never_modified():
    text, _findings, _added = liquibase.process_text(ADD_COLUMN)
    assert ADD_COLUMN.rstrip("\n") in text


def test_rollback_is_converted_too():
    text, _findings, _added = liquibase.process_text(ADD_COLUMN)
    twin = liquibase.parse(text)[1]
    assert twin.rollbacks
    assert "[" not in twin.rollbacks[0]


def test_dollar_quoted_body_gets_split_statements_false():
    changeset = liquibase.Changeset(author="a", id="b", attrs={"dbms": "mssql"})
    twin = liquibase.TwinResult(sql="DO $$ BEGIN NULL; END $$;",
                                needs_split_off=True, ok=True)
    rendered = liquibase.render_twin(changeset, twin)
    assert "splitStatements:false" in rendered


def test_runonchange_is_carried_over_to_the_twin():
    changeset = liquibase.Changeset(
        author="a", id="b", attrs={"dbms": "mssql", "runOnChange": "true"})
    rendered = liquibase.render_twin(changeset, liquibase.TwinResult(sql="SELECT 1;", ok=True))
    assert "runOnChange:true" in rendered


# --------------------------------------------------------------------------
# Idempotency -- unlike the C# scanner, this may be re-run
# --------------------------------------------------------------------------

def test_existing_twin_is_not_duplicated():
    text, findings, added = liquibase.process_text(ALREADY_TWINNED)
    assert added == 0
    assert text == ALREADY_TWINNED
    assert any(f.code == "LB006" for f in findings)


def test_running_twice_is_a_no_op():
    once, _f, _a = liquibase.process_text(ADD_COLUMN)
    twice, _f2, added = liquibase.process_text(once)
    assert added == 0
    assert twice == once


# --------------------------------------------------------------------------
# File handling
# --------------------------------------------------------------------------

@pytest.fixture
def project(tmp_path):
    folder = tmp_path / "App_DB" / "Changelogs" / "sql"
    folder.mkdir(parents=True)
    (folder / "add-column.sql").write_text(ADD_COLUMN, encoding="utf-8")
    return tmp_path


def test_dry_run_writes_nothing(project):
    before = (project / "App_DB" / "Changelogs" / "sql" / "add-column.sql").read_text(encoding="utf-8")
    report = liquibase.run(project, dry_run=True)
    after = (project / "App_DB" / "Changelogs" / "sql" / "add-column.sql").read_text(encoding="utf-8")
    assert report.converted == 1
    assert before == after
    assert not list(project.rglob("*.bak"))


def test_real_run_writes_a_backup_and_updates_the_file(project):
    report = liquibase.run(project, dry_run=False)
    assert report.converted == 1
    path = project / "App_DB" / "Changelogs" / "sql" / "add-column.sql"
    assert (path.parent / "add-column.sql.bak").exists()
    assert len(liquibase.parse(path.read_text(encoding="utf-8"))) == 2


def test_crlf_line_endings_are_preserved(tmp_path):
    path = tmp_path / "crlf.sql"
    path.write_bytes(ADD_COLUMN.replace("\n", "\r\n").encode("utf-8"))
    liquibase.run(tmp_path, dry_run=False)
    raw = path.read_bytes()
    assert b"\r\n" in raw
    assert b"\n" not in raw.replace(b"\r\n", b"")


def test_a_broken_file_does_not_stop_the_run(project, monkeypatch):
    folder = project / "App_DB" / "Changelogs" / "sql"
    (folder / "bad.sql").write_text(ADD_COLUMN, encoding="utf-8")
    original = liquibase.process_text

    def explode(text, repair=False):
        if "bad-marker" in text:
            raise RuntimeError("boom")
        return original(text, repair=repair)

    (folder / "bad.sql").write_text(ADD_COLUMN + "-- bad-marker\n", encoding="utf-8")
    monkeypatch.setattr(liquibase, "process_text", explode)
    report = liquibase.run(project, dry_run=True)
    assert report.failed == 1
    assert report.scanned == 2


# --------------------------------------------------------------------------
# Repair mode -- rewriting broken PostgreSQL changesets
# --------------------------------------------------------------------------

def test_repair_off_by_default_leaves_the_file_alone():
    text, _findings, added = liquibase.process_text(TSQL_IN_PG_CHANGESET)
    assert added == 0
    assert text == TSQL_IN_PG_CHANGESET


def test_repair_rewrites_a_tsql_body_into_postgresql():
    text, findings, added = liquibase.process_text(
        TSQL_IN_PG_CHANGESET, repair=True)
    assert added == 1
    assert any(f.action == liquibase.REPAIRED for f in findings)
    fixed = liquibase.parse(text)[0]
    assert fixed.dbms == "postgresql"
    assert "SYS.COLUMNS" not in fixed.body.upper()
    assert "UNIQUEIDENTIFIER" not in fixed.body.upper()
    assert liquibase.final_check(fixed.body) == ""


def test_repair_preserves_changeset_identity():
    """The changeset is being FIXED, not replaced. Renaming it would make
    Liquibase treat it as new work and re-run it."""
    before = liquibase.parse(TSQL_IN_PG_CHANGESET)[0]
    text, _findings, _added = liquibase.process_text(
        TSQL_IN_PG_CHANGESET, repair=True)
    after = liquibase.parse(text)[0]
    assert (after.author, after.id) == (before.author, before.id)


def test_repair_adds_split_statements_to_a_dollar_quoted_changeset():
    text = HEADER + (
        "--changeset t:decimals-postgresql dbms:postgresql\n"
        "DO $$\nBEGIN\n  ALTER TABLE t ALTER COLUMN c TYPE numeric(18,4);\n"
        "END $$;\n")
    out, findings, added = liquibase.process_text(text, repair=True)
    assert added == 1
    assert "splitStatements:false" in out
    assert any(f.action == liquibase.REPAIRED for f in findings)
    assert liquibase.parse(out)[0].split_statements_off is True


def test_repair_never_touches_a_valid_changeset():
    """Rewriting a changeset that already ran would change its checksum --
    exactly the failure this whole exercise started from."""
    text = HEADER + ("--changeset t:ok-postgresql dbms:postgresql\n"
                     "ALTER TABLE t ADD COLUMN IF NOT EXISTS c TEXT;\n")
    out, _findings, added = liquibase.process_text(text, repair=True)
    assert added == 0
    assert out == text


def test_repair_refuses_when_conversion_is_not_clean(monkeypatch):
    monkeypatch.setattr(liquibase, "final_check",
                        lambda _sql: "simulated invalid syntax")
    text, findings, added = liquibase.process_text(
        TSQL_IN_PG_CHANGESET, repair=True)
    assert added == 0
    assert text == TSQL_IN_PG_CHANGESET
    assert any(f.action == liquibase.SKIPPED and f.code == "LB009"
               for f in findings)


def test_repair_and_twin_generation_coexist_in_one_file():
    """Both edit kinds apply to the same file without corrupting offsets."""
    text = ADD_COLUMN + "\n" + TSQL_IN_PG_CHANGESET.split("\n", 2)[2]
    out, _findings, added = liquibase.process_text(text, repair=True)
    assert added == 2
    parsed = liquibase.parse(out)
    assert len(parsed) == 3
    assert liquibase.duplicate_identities(parsed) == []


def test_repaired_file_is_stable_on_a_second_pass():
    once, _f, _a = liquibase.process_text(TSQL_IN_PG_CHANGESET, repair=True)
    twice, _f2, added = liquibase.process_text(once, repair=True)
    assert added == 0
    assert twice == once


def test_report_renders_without_a_root():
    assert "Liquibase changelog conversion" in liquibase.render_report(
        liquibase.ChangelogReport())


# --------------------------------------------------------------------------
# Comment-only fragments
# --------------------------------------------------------------------------

BANNERED = HEADER + """--changeset app-team:2026-08-11-idx-mssql dbms:mssql
------------------------------------------------------------
-- OrderBatch: soft-delete filters
------------------------------------------------------------
IF NOT EXISTS (
    SELECT 1 FROM sys.indexes WHERE name = 'IX_A' AND object_id = OBJECT_ID('OrderBatch')
)
BEGIN
    CREATE NONCLUSTERED INDEX IX_A ON OrderBatch (IsDeleted, Created);
END
GO
"""


def test_comment_banners_do_not_fail_the_conversion():
    """A `-- section` banner between statements is not SQL. Handing it to the
    engine returns empty output, which would otherwise read as a failure and
    lose the whole changeset."""
    assert liquibase.is_comment_only("-- just a note\n---------\n")
    assert not liquibase.is_comment_only("-- note\nSELECT 1;")
    _text, findings, added = liquibase.process_text(BANNERED)
    assert added == 1
    assert any(f.action == liquibase.CONVERTED for f in findings)


def test_comment_only_changeset_produces_no_twin():
    text = HEADER + "--changeset a:b dbms:mssql\n-- nothing but a note\n"
    _out, _findings, added = liquibase.process_text(text)
    assert added == 0


def test_generated_index_twin_is_idempotent_and_valid():
    text, _findings, _added = liquibase.process_text(BANNERED)
    twin = liquibase.parse(text)[1]
    assert "CREATE INDEX IF NOT EXISTS" in twin.body.upper()
    assert "NONCLUSTERED" not in twin.body.upper()
    assert liquibase.final_check(twin.body) == ""
