"""Scan a C# project for MS-SQL and rewrite it as PostgreSQL, in place.

SAFETY RULES -- this edits source files, so it is deliberately conservative:

  1. A `.bak` copy is written before a file is modified for the first time.
  2. A query is rewritten ONLY when its conversion is usable -- PostgreSQL
     syntax VALID and zero ERROR findings. Anything doubtful is left exactly as
     it was and reported, so a broken query is never planted in your source.
  3. Interpolated strings ($"...{expr}...") are never rewritten -- the holes are
     dynamic SQL.
  4. A file is written only if something actually changed.
  5. One bad file cannot stop the run: it becomes a FAILED row.

Use dry_run=True to see exactly what would happen without touching anything.
"""

from __future__ import annotations

import codecs
import shutil
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from core import csharp, interpolation, orchestrator
from core.models import ConversionResult, Severity

MAX_WORKERS = 8
MAX_FILE_BYTES = 5 * 1024 * 1024      # skip anything implausible for source
BACKUP_SUFFIX = ".bak"

# Last-resort decode for source Visual Studio saved in the Windows ANSI code
# page. cp1252 gives every byte 0x00-0xFF a meaning, so it never raises.
LEGACY_ENCODING = "cp1252"


def read_source(path: Path) -> tuple[str, str]:
    """Return (text, encoding to write it back with).

    Almost all source is UTF-8, with or without a BOM. A file saved as Windows
    ANSI decodes as neither, and a strict UTF-8 read raises UnicodeDecodeError
    -- which used to fail the entire file before a single query was looked at,
    even when the offending byte was an em-dash sitting in a comment
    (`0x97` in cp1252) with no bearing on the SQL.

    The encoding travels back with the text so a rewrite can restore the file
    exactly as it was found. Normalising a cp1252 file to UTF-8 would change
    bytes outside the SQL literals, which is the one thing this scanner
    promises never to do.
    """
    raw = path.read_bytes()

    # cp1252 gives almost every byte a meaning, so without this guard a binary
    # file that happens to be named *.cs would "decode" into mojibake and be
    # reported CLEAN instead of FAILED. No C# source contains a NUL.
    if b"\x00" in raw:
        raise UnicodeDecodeError(
            LEGACY_ENCODING, raw, 0, 1,
            "binary content (NUL byte) -- not C# source")

    if raw.startswith(codecs.BOM_UTF8):
        return raw.decode("utf-8-sig"), "utf-8-sig"
    try:
        return raw.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        return raw.decode(LEGACY_ENCODING), LEGACY_ENCODING

def dominant_newline(text: str) -> str:
    """The line ending the file actually uses.

    read_source() decodes bytes without translation, so `text` carries the
    real terminators. Converted SQL always arrives with "\\n"; inserting that
    into a CRLF file unchanged leaves the rewritten literals as the only
    LF-only lines, which shows up as a whole-file diff in git and a mixed
    file in the editor. Ties go to CRLF -- a lone stray LF in a CRLF file is
    the anomaly, not the convention.
    """
    crlf = text.count("\r\n")
    lf = text.count("\n") - crlf
    return "\r\n" if crlf >= lf and crlf else "\n"


REWRITTEN = "REWRITTEN"
SKIPPED = "SKIPPED"
FAILED = "FAILED"
CLEAN = "CLEAN"                       # SQL found, already fine, nothing to do


@dataclass
class Finding:
    line: int
    kind: str
    action: str
    reason: str = ""
    result: ConversionResult | None = None


@dataclass
class FileReport:
    path: Path
    findings: list[Finding] = field(default_factory=list)
    status: str = CLEAN
    error: str = ""
    backup: Path | None = None
    encoding: str = "utf-8"           # what the file was read as, and rewritten as

    @property
    def rewritten(self) -> int:
        return sum(1 for f in self.findings if f.action == REWRITTEN)

    @property
    def needs_attention(self) -> int:
        return sum(1 for f in self.findings if f.action == SKIPPED)


@dataclass
class ScanReport:
    root: Path | None = None
    files: list[FileReport] = field(default_factory=list)
    duration_ms: int = 0
    dry_run: bool = False

    @property
    def scanned(self) -> int:
        return len(self.files)

    @property
    def with_sql(self) -> int:
        return sum(1 for f in self.files if f.findings)

    @property
    def queries(self) -> int:
        return sum(len(f.findings) for f in self.files)

    @property
    def rewritten(self) -> int:
        return sum(f.rewritten for f in self.files)

    @property
    def needs_attention(self) -> int:
        return sum(f.needs_attention for f in self.files)

    @property
    def failed(self) -> int:
        return sum(1 for f in self.files if f.status == FAILED)


ProgressFn = Callable[[FileReport, int, int], None]


def find_cs_files(root: Path, recursive: bool = True) -> list[Path]:
    """Every .cs file worth scanning. Build output is excluded -- rewriting
    generated code would be pointless and confusing."""
    pattern = "**/*.cs" if recursive else "*.cs"
    skip = {"bin", "obj", ".git", ".vs", "node_modules", "packages", "TestResults"}
    out = []
    for path in root.glob(pattern):
        if not path.is_file():
            continue
        if skip & set(p.name for p in path.parents):
            continue
        if path.name.endswith((".Designer.cs", ".g.cs", ".g.i.cs", ".AssemblyInfo.cs")):
            continue
        out.append(path)
    return sorted(out)


def _process(path: Path, dry_run: bool) -> FileReport:
    report = FileReport(path=path)
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            report.status = SKIPPED
            report.error = "file too large to be source"
            return report
        text, report.encoding = read_source(path)
    except (OSError, UnicodeDecodeError, LookupError) as exc:
        report.status = FAILED
        report.error = f"{type(exc).__name__}: {exc}"
        return report

    try:
        literals = csharp.find_sql_literals(text)
    except Exception as exc:
        report.status = FAILED
        report.error = f"scan failed: {type(exc).__name__}: {exc}"
        return report

    # SQL FRAGMENTS: literals that carry T-SQL but are not whole statements
    # (scalar subqueries, CASE expressions, WHERE predicates, column mappings)
    # which C# splices into a larger query at runtime. They cannot be rewritten
    # -- a fragment has no FROM clause, so its aliases and target context are
    # unknowable -- but they MUST be reported: the T-SQL in them still reaches
    # the database, and before this they were neither converted nor mentioned.
    try:
        for fragment in csharp.find_sql_fragments(text):
            report.findings.append(Finding(
                fragment.line, fragment.kind, SKIPPED,
                "T-SQL FRAGMENT (not a full statement -- convert by hand): "
                + ", ".join(csharp.tsql_markers(fragment.sql))))
    except Exception as exc:      # a reporting extra must never fail a scan
        report.findings.append(Finding(
            0, "", SKIPPED, f"fragment scan skipped: {type(exc).__name__}"))

    if not literals:
        return report

    newline = dominant_newline(text)
    replacements: list[tuple[csharp.SqlLiteral, str]] = []
    for literal in literals:
        holes: list[str] | None = None
        sql_to_convert = literal.sql
        # v2.16.0: a piece of a query C# assembles (StringBuilder.Append, +, +=) is never
        # converted alone -- it would end in `;` while T-SQL pieces are appended after it
        piece = csharp.composed_query(text, literal)
        if piece:
            report.findings.append(Finding(
                literal.line, literal.kind, SKIPPED,
                f"piece of a query assembled in C# ({piece}) -- convert the whole query by "
                f"hand; converting one piece breaks it at runtime"))
            continue
        if literal.interpolated:
            masked = interpolation.mask_holes(literal.sql)
            if masked is None:
                report.findings.append(Finding(
                    literal.line, literal.kind, SKIPPED,
                    "interpolated string with unsafe {expr} holes (escaped "
                    "braces, nested quotes/braces) -- convert this one by hand"))
                continue
            sql_to_convert, holes = masked

        try:
            result = orchestrator.convert(sql_to_convert)
        except Exception as exc:
            report.findings.append(Finding(
                literal.line, literal.kind, FAILED,
                f"{type(exc).__name__}: {exc}"))
            continue

        if not result.is_usable:
            report.findings.append(Finding(
                literal.line, literal.kind, SKIPPED,
                "conversion needs review -- left unchanged", result))
            continue

        converted_sql = result.sql
        if holes is not None:
            restored = interpolation.restore_holes(converted_sql, holes)
            if restored is None:
                report.findings.append(Finding(
                    literal.line, literal.kind, SKIPPED,
                    "conversion moved or dropped a {expr} hole -- left "
                    "unchanged, convert this one by hand"))
                continue
            converted_sql = restored

        # v2.16.0: keep the literal's own ending. A constant written without a final `;` may be
        # extended by C# elsewhere (`InsertAudit + " RETURNING Id;"`); the engine's added `;`
        # would turn that into `...; RETURNING Id;` -- a syntax error at runtime.
        if not literal.sql.rstrip().endswith(";"):
            stripped = converted_sql.rstrip()
            if stripped.endswith(";"):
                converted_sql = stripped[:-1].rstrip()

        indent = csharp.line_indent(text, literal.start) + "    "
        body = csharp.reindent(converted_sql, indent)
        # The engine always emits "\n". A CRLF file must stay CRLF THROUGHOUT,
        # or the rewritten literals become the only LF-only lines in the file.
        if newline != "\n":
            body = body.replace("\n", newline)
        encoded = csharp.encode(literal.kind, body)
        if literal.interpolated:
            encoded = "$" + encoded
        replacements.append((literal, encoded))
        note = f"{len(holes)} {{expr}} hole(s) preserved" if holes else ""
        report.findings.append(Finding(
            literal.line, literal.kind, REWRITTEN, note, result))

    if not replacements:
        report.status = CLEAN if report.findings else CLEAN
        return report

    if dry_run:
        report.status = REWRITTEN
        return report

    try:
        backup = path.with_suffix(path.suffix + BACKUP_SUFFIX)
        if not backup.exists():
            shutil.copy2(path, backup)
        # Same encoding it was read with, so the only bytes that move are the
        # SQL literals themselves.
        #
        # newline="" is LOAD-BEARING. read_source() decodes bytes directly, so
        # `text` still holds the file's real "\r\n"; the default newline=None
        # would translate every "\n" to os.linesep and turn each of those into
        # "\r\r\n" -- one extra CR per run, compounding on every rescan.
        with path.open("w", encoding=report.encoding, newline="") as handle:
            handle.write(csharp.replace_literals(text, replacements))
        report.backup = backup
        report.status = REWRITTEN
    except (OSError, UnicodeEncodeError) as exc:
        report.status = FAILED
        report.error = f"could not write file: {exc}"
    return report


def run(root: str | Path, *, recursive: bool = True, dry_run: bool = False,
        on_progress: ProgressFn | None = None,
        should_stop: Callable[[], bool] | None = None) -> ScanReport:
    started = time.perf_counter()
    root = Path(root)
    report = ScanReport(root=root, dry_run=dry_run)

    files = find_cs_files(root, recursive)
    if not files:
        return report

    total = len(files)
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = []
        for path in files:
            if should_stop and should_stop():
                break
            futures.append(pool.submit(_process, path, dry_run))
        for index, future in enumerate(futures, start=1):
            try:
                item = future.result()
            except Exception as exc:      # a file must never kill the run
                item = FileReport(path=Path("<unknown>"), status=FAILED,
                                  error=f"{type(exc).__name__}: {exc}")
            report.files.append(item)
            if on_progress:
                on_progress(item, index, total)

    report.duration_ms = int((time.perf_counter() - started) * 1000)
    return report


# --------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------

def render_report(report: ScanReport) -> str:
    mode = "DRY RUN -- nothing was written" if report.dry_run else "files rewritten in place"
    lines = [
        "# C# project scan",
        "",
        f"- Root: `{report.root}`",
        f"- Mode: **{mode}**",
        f"- Duration: {report.duration_ms / 1000:.1f}s",
        "",
        "| | Count |",
        "|---|---|",
        f"| .cs files scanned | {report.scanned} |",
        f"| files containing SQL | {report.with_sql} |",
        f"| queries found | {report.queries} |",
        f"| queries rewritten | {report.rewritten} |",
        f"| queries needing attention | {report.needs_attention} |",
        f"| files failed | {report.failed} |",
        "",
    ]

    interesting = [f for f in report.files if f.findings or f.status == FAILED]
    if not interesting:
        lines.append("No SQL found.")
        return "\n".join(lines) + "\n"

    lines += ["## Files", ""]
    for item in interesting:
        try:
            shown = item.path.relative_to(report.root)
        except ValueError:
            shown = item.path
        lines.append(f"### `{shown}` -- {item.status}")
        if item.error:
            lines.append(f"- {item.error}")
        if item.encoding == LEGACY_ENCODING:
            lines.append(
                f"- encoding: **{LEGACY_ENCODING}** (not UTF-8) -- read and "
                "rewritten in place as-is. Re-save the file as UTF-8 when "
                "convenient; the offending bytes are usually punctuation such "
                "as an em-dash in a comment.")
        if item.backup:
            lines.append(f"- backup: `{item.backup.name}`")
        for finding in item.findings:
            lines.append(f"- line {finding.line} ({finding.kind}): "
                         f"**{finding.action}** {finding.reason}".rstrip())
            if finding.result is not None:
                for warning in finding.result.sorted_warnings:
                    if warning.severity is Severity.INFO:
                        continue
                    lines.append(f"    - {warning.code}: {warning.message}")
        lines.append("")
    return "\n".join(lines) + "\n"


def write_report(report: ScanReport, filename: str = "scan-report.md") -> Path | None:
    if report.root is None:
        return None
    path = report.root / filename
    try:
        path.write_text(render_report(report), encoding="utf-8")
        return path
    except OSError:
        return None
