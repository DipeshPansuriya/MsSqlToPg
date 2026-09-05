"""Project-scan tests.

This feature REWRITES CUSTOMER SOURCE FILES. Every safety rule in
core/scan.py's docstring has a test here, because the cost of getting it wrong
is corrupted code in someone's repository.
"""

import pytest

from core import orchestrator, scan

GOOD = '''namespace App;
public static class Q
{
    public static readonly string Get = @"
        SELECT TOP 1 a FROM t WITH(NOLOCK) WHERE ISNULL(b, 0) = 0";
}
'''

INTERPOLATED = '''namespace App;
public static class Q
{
    public static readonly string Get = $@"SELECT a FROM {table} WHERE b = 1";
}
'''

NOT_SQL = '''namespace App;
public class Dto { public int UserId { get; set; } }
'''


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "proj"
    (root / "Queries").mkdir(parents=True)
    (root / "Queries" / "Good.cs").write_text(GOOD, encoding="utf-8")
    (root / "Queries" / "Dto.cs").write_text(NOT_SQL, encoding="utf-8")
    return root


# --------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------

def test_finds_cs_files_recursively(project):
    assert {p.name for p in scan.find_cs_files(project)} == {"Good.cs", "Dto.cs"}


def test_build_output_is_excluded(project):
    for folder in ("bin", "obj", ".vs", "packages"):
        (project / folder).mkdir()
        (project / folder / "Gen.cs").write_text(GOOD, encoding="utf-8")
    assert not any(p.name == "Gen.cs" for p in scan.find_cs_files(project))


def test_generated_files_are_excluded(project):
    (project / "Form.Designer.cs").write_text(GOOD, encoding="utf-8")
    (project / "Model.g.cs").write_text(GOOD, encoding="utf-8")
    names = {p.name for p in scan.find_cs_files(project)}
    assert "Form.Designer.cs" not in names and "Model.g.cs" not in names


# --------------------------------------------------------------------------
# Dry run must not touch anything
# --------------------------------------------------------------------------

def test_dry_run_writes_nothing(project):
    before = (project / "Queries" / "Good.cs").read_text(encoding="utf-8")
    report = scan.run(project, dry_run=True)
    assert report.rewritten == 1
    assert (project / "Queries" / "Good.cs").read_text(encoding="utf-8") == before
    assert not list(project.rglob("*.bak"))


# --------------------------------------------------------------------------
# Real run
# --------------------------------------------------------------------------

def test_rewrites_and_backs_up(project):
    original = (project / "Queries" / "Good.cs").read_text(encoding="utf-8")
    report = scan.run(project)

    after = (project / "Queries" / "Good.cs").read_text(encoding="utf-8")
    assert after != original
    assert "LIMIT 1" in after and "COALESCE" in after
    assert "WITH(NOLOCK)" not in after

    backup = project / "Queries" / "Good.cs.bak"
    assert backup.exists()
    assert backup.read_text(encoding="utf-8") == original
    assert report.rewritten == 1


def test_file_without_sql_is_untouched(project):
    before = (project / "Queries" / "Dto.cs").read_text(encoding="utf-8")
    scan.run(project)
    assert (project / "Queries" / "Dto.cs").read_text(encoding="utf-8") == before
    assert not (project / "Queries" / "Dto.cs.bak").exists()


def test_backup_is_not_overwritten_on_a_second_run(project):
    scan.run(project)
    first = (project / "Queries" / "Good.cs.bak").read_text(encoding="utf-8")
    scan.run(project)
    assert (project / "Queries" / "Good.cs.bak").read_text(encoding="utf-8") == first, \
        "the .bak must always hold the ORIGINAL, not the previous conversion"


def test_rewritten_file_is_still_parseable_csharp(project):
    from core import csharp
    scan.run(project)
    text = (project / "Queries" / "Good.cs").read_text(encoding="utf-8")
    assert text.count("{") == text.count("}")
    assert csharp.find_sql_literals(text), "the SQL literal must survive"


# --------------------------------------------------------------------------
# What must NOT be rewritten
# --------------------------------------------------------------------------

def test_interpolated_string_is_reported_not_rewritten(tmp_path):
    root = tmp_path / "p"
    root.mkdir()
    path = root / "I.cs"
    path.write_text(INTERPOLATED, encoding="utf-8")

    report = scan.run(root)
    assert path.read_text(encoding="utf-8") == INTERPOLATED
    assert report.rewritten == 0
    assert report.needs_attention == 1
    finding = report.files[0].findings[0]
    assert "interpolated" in finding.reason


def test_unusable_conversion_is_left_alone(tmp_path, monkeypatch):
    """A broken query must NEVER be planted in customer source."""
    from core.models import ConversionResult, Severity

    def bad(_sql):
        result = ConversionResult(sql="THIS IS BROKEN")
        result.add("L999", Severity.ERROR, "deliberately unusable")
        return result

    monkeypatch.setattr(orchestrator, "convert", bad)
    root = tmp_path / "p"
    root.mkdir()
    path = root / "G.cs"
    path.write_text(GOOD, encoding="utf-8")

    report = scan.run(root)
    assert path.read_text(encoding="utf-8") == GOOD
    assert "THIS IS BROKEN" not in path.read_text(encoding="utf-8")
    assert report.rewritten == 0 and report.needs_attention == 1


# --------------------------------------------------------------------------
# Robustness -- "our application must not crash"
# --------------------------------------------------------------------------

def test_one_bad_file_does_not_stop_the_run(project, monkeypatch):
    calls = {"n": 0}
    real = orchestrator.convert

    def flaky(sql):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        return real(sql)

    monkeypatch.setattr(orchestrator, "convert", flaky)
    report = scan.run(project)
    assert report.scanned == 2, "the run must complete"


def test_unreadable_file_is_reported_not_raised(tmp_path):
    root = tmp_path / "p"
    root.mkdir()
    (root / "Binary.cs").write_bytes(b"\xff\xfe\x00\x01 not utf-8 \xff")
    report = scan.run(root)
    assert report.failed == 1
    assert report.files[0].status == scan.FAILED


def test_scales_to_many_files(tmp_path):
    """100+ files, as in a real project."""
    root = tmp_path / "big"
    root.mkdir()
    for i in range(120):
        (root / f"Q{i}.cs").write_text(GOOD, encoding="utf-8")
    report = scan.run(root, dry_run=True)
    assert report.scanned == 120
    assert report.rewritten == 120
    assert report.failed == 0


def test_should_stop_halts_early(project):
    report = scan.run(project, should_stop=lambda: True)
    assert report.scanned == 0


def test_empty_folder_is_fine(tmp_path):
    report = scan.run(tmp_path)
    assert report.scanned == 0 and report.failed == 0


# --------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------

def test_report_names_files_lines_and_actions(project):
    report = scan.run(project, dry_run=True)
    text = scan.render_report(report)
    assert "Good.cs" in text
    assert "REWRITTEN" in text
    assert "DRY RUN" in text


def test_report_is_written_to_disk(project):
    report = scan.run(project, dry_run=True)
    path = scan.write_report(report)
    assert path is not None and path.exists()
