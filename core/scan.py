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

import shutil
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from core import csharp, orchestrator
from core.models import ConversionResult, Severity

MAX_WORKERS = 8
MAX_FILE_BYTES = 5 * 1024 * 1024      # skip anything implausible for source
BACKUP_SUFFIX = ".bak"

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
        text = path.read_text(encoding="utf-8-sig", errors="strict")
    except (OSError, UnicodeDecodeError) as exc:
        report.status = FAILED
        report.error = f"{type(exc).__name__}: {exc}"
        return report

    try:
        literals = csharp.find_sql_literals(text)
    except Exception as exc:
        report.status = FAILED
        report.error = f"scan failed: {type(exc).__name__}: {exc}"
        return report

    if not literals:
        return report

    replacements: list[tuple[csharp.SqlLiteral, str]] = []
    for literal in literals:
        if literal.interpolated:
            report.findings.append(Finding(
                literal.line, literal.kind, SKIPPED,
                "interpolated string -- {expr} holes are dynamic SQL, "
                "convert this one by hand"))
            continue

        try:
            result = orchestrator.convert(literal.sql)
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

        indent = csharp.line_indent(text, literal.start) + "    "
        body = csharp.reindent(result.sql, indent)
        replacements.append((literal, csharp.encode(literal.kind, body)))
        report.findings.append(Finding(
            literal.line, literal.kind, REWRITTEN, "", result))

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
        path.write_text(csharp.replace_literals(text, replacements),
                        encoding="utf-8")
        report.backup = backup
        report.status = REWRITTEN
    except OSError as exc:
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
