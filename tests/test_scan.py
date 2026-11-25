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
# Source encoding
#
# Visual Studio still saves some files in the Windows ANSI code page. A strict
# UTF-8 read failed the WHOLE file on the first non-ASCII byte -- in practice an
# em-dash inside a comment (0x97), nowhere near the SQL -- so its queries were
# never converted and the row just said UnicodeDecodeError.
# --------------------------------------------------------------------------

# 0x97 is an em-dash in cp1252 and an invalid UTF-8 start byte.
CP1252_SQL = (
    'namespace App;\n'
    'public static class Q\n'
    '{\n'
    '    /// <summary>Civil number — maps to LoginName.</summary>\n'
    '    public static readonly string Get = @"\n'
    '        SELECT TOP 1 a FROM t WITH(NOLOCK) WHERE ISNULL(b, 0) = 0";\n'
    '}\n'
)


def test_reads_cp1252_source_that_is_not_valid_utf8(tmp_path):
    path = tmp_path / "Legacy.cs"
    path.write_bytes(CP1252_SQL.encode("cp1252"))
    text, encoding = scan.read_source(path)
    assert encoding == "cp1252"
    assert "—" in text


def test_cp1252_file_is_converted_not_failed(tmp_path):
    root = tmp_path / "p"
    root.mkdir()
    (root / "Legacy.cs").write_bytes(CP1252_SQL.encode("cp1252"))

    report = scan.run(root)

    assert report.failed == 0, [f.error for f in report.files]
    assert report.rewritten == 1


def test_cp1252_file_keeps_its_encoding_and_its_em_dash(tmp_path):
    """Rewriting must move the SQL literal and nothing else -- re-encoding the
    whole file to UTF-8 would touch bytes the scanner never promised to touch."""
    root = tmp_path / "p"
    root.mkdir()
    path = root / "Legacy.cs"
    path.write_bytes(CP1252_SQL.encode("cp1252"))

    scan.run(root)

    raw = path.read_bytes()
    assert b"\x97" in raw, "em-dash must survive in the original code page"
    text = raw.decode("cp1252")
    assert "NOLOCK" not in text.upper()


def test_utf8_bom_is_preserved(tmp_path):
    root = tmp_path / "p"
    root.mkdir()
    path = root / "Bom.cs"
    path.write_bytes(b"\xef\xbb\xbf" + GOOD.encode("utf-8"))

    scan.run(root)

    assert path.read_bytes().startswith(b"\xef\xbb\xbf")


def test_report_flags_a_legacy_encoding(tmp_path):
    root = tmp_path / "p"
    root.mkdir()
    (root / "Legacy.cs").write_bytes(CP1252_SQL.encode("cp1252"))

    text = scan.render_report(scan.run(root))

    assert "cp1252" in text


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

def test_interpolated_string_with_safe_holes_is_rewritten(tmp_path):
    """{expr} holes are masked, the SQL around them converts, holes restored,
    and the `$` prefix survives the round-trip."""
    root = tmp_path / "p"
    root.mkdir()
    path = root / "I.cs"
    path.write_text(INTERPOLATED, encoding="utf-8")

    report = scan.run(root)
    text = path.read_text(encoding="utf-8")
    assert report.rewritten == 1
    assert "{table}" in text, "the hole must survive conversion"
    assert '$@"' in text, "the interpolation prefix must survive"
    finding = report.files[0].findings[0]
    assert finding.action == scan.REWRITTEN


def test_interpolated_string_with_unsafe_holes_is_skipped(tmp_path):
    """Escaped braces are literal braces -- refusal case, never rewritten."""
    unsafe = ('namespace App;\npublic static class Q\n{\n'
              '    public static readonly string Get = '
              '$@"SELECT \'{{X}}\' FROM t";\n}\n')
    root = tmp_path / "p"
    root.mkdir()
    path = root / "U.cs"
    path.write_text(unsafe, encoding="utf-8")

    report = scan.run(root)
    assert path.read_text(encoding="utf-8") == unsafe
    assert report.rewritten == 0
    finding = report.files[0].findings[0]
    assert "hand" in finding.reason


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


# --------------------------------------------------------------------------
# Line endings -- the scanner must not touch bytes outside the SQL literals
# --------------------------------------------------------------------------

CRLF_SOURCE = (
    'namespace Q\r\n'
    '{\r\n'
    '    public static class C\r\n'
    '    {\r\n'
    '        public static readonly string A = @"\r\n'
    '            SELECT TOP 5 Name FROM Users WHERE ISNULL(IsDeleted, 0) = 0";\r\n'
    '    }\r\n'
    '}\r\n'
)


def test_crlf_file_gains_no_stray_carriage_returns(tmp_path):
    """write_text() with the default newline=None translates every "\n" to
    os.linesep -- and read_source() decodes bytes without translation, so the
    text already held "\r\n". The result was "\r\r\n", one extra CR per run,
    compounding on every rescan until the file was full of ^M^M^M."""
    root = tmp_path / "p"
    root.mkdir()
    path = root / "Q.cs"
    path.write_bytes(CRLF_SOURCE.encode("utf-8"))

    scan.run(root)
    raw = path.read_bytes()
    assert b"\r\r" not in raw, "stray CR introduced"
    assert raw.count(b"\r\n") == raw.count(b"\n"), "mixed line endings"


def test_rescanning_is_byte_stable(tmp_path):
    """The compounding was only visible on the SECOND run -- one pass looked
    fine. Converting an already-converted file must be a no-op."""
    root = tmp_path / "p"
    root.mkdir()
    path = root / "Q.cs"
    path.write_bytes(CRLF_SOURCE.encode("utf-8"))

    scan.run(root)
    first = path.read_bytes()
    scan.run(root)
    assert path.read_bytes() == first


def test_lf_file_stays_lf(tmp_path):
    root = tmp_path / "p"
    root.mkdir()
    path = root / "Q.cs"
    path.write_bytes(CRLF_SOURCE.replace("\r\n", "\n").encode("utf-8"))

    scan.run(root)
    assert b"\r" not in path.read_bytes()


def test_dominant_newline_detection():
    assert scan.dominant_newline("a\r\nb\r\n") == "\r\n"
    assert scan.dominant_newline("a\nb\n") == "\n"
    assert scan.dominant_newline("") == "\n"
    # One stray LF in an otherwise CRLF file is the anomaly, not the rule.
    assert scan.dominant_newline("a\r\nb\r\nc\n") == "\r\n"


# v2.16.0: a literal written without a final `;` keeps that ending, because C# may
# extend it elsewhere (`InsertAudit + " RETURNING Id;"`) and `...; RETURNING Id;` is
# a syntax error.
EXTENDED_ELSEWHERE = '''namespace App;
public static class Q
{
    public static readonly string Insert = @"
        INSERT INTO t (a) SELECT b FROM u WITH(NOLOCK) WHERE ISNULL(c, 0) = 0
        ";
    public static readonly string Terminated = @"SELECT a FROM t WITH(NOLOCK);";
}
'''


def test_final_terminator_follows_the_original(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "Q.cs").write_text(EXTENDED_ELSEWHERE, encoding="utf-8")
    scan.run(root)
    after = (root / "Q.cs").read_text(encoding="utf-8")
    insert = after[after.index("Insert ="):after.index("Terminated =")]
    assert "COALESCE(c, 0) = 0" in insert and ";" not in insert.split('@"', 1)[1].rsplit('"', 1)[0]
    terminated = after[after.index("Terminated ="):]
    assert terminated.split('@"', 1)[1].rsplit('"', 1)[0].rstrip().endswith(";")
