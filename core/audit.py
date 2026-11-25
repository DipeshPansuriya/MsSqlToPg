"""Audit mode (v2.13.0): what T-SQL is LEFT in a C# project? Report only.

This is the campaign scanner (scripts/pg-conversion/tsqlscan.py) built into the
tool, so "is this repo done?" has one answer instead of three:

  * every C# string literal is read -- verbatim, regular, raw, AND
    interpolated ($@"..." / @$"...") and local-variable SQL, which the old
    name-based scans missed;
  * literals are grouped by their owner (`Name = "..." + "..."`, `x +=`), so a
    half-converted constant whose TAIL still says `FROM #PDTemp` is caught;
  * SQL comments are stripped before matching (converted files carry comments
    like "#master is a session temp table");
  * each constant gets HARD markers (needs a whole-constant rewrite) and SOFT
    markers (mechanical), from the shared set in core/markers;
  * liveness: a constant whose name appears only once in the project (its own
    declaration) is reported DEAD -- no point converting it.

Nothing is written except the report files.
"""

from __future__ import annotations

import collections
import csv
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from core import csharp, markers, scan

_SQLISH = re.compile(
    r"\b(SELECT\b[\s\S]*\bFROM\b|INSERT\s+INTO|UPDATE\s+[\w\"\[.#@]+\s+SET|DELETE\s+FROM|"
    r"DECLARE\s+@|EXEC\s|MERGE\s+INTO|;?\s*WITH\s+\w+\s+AS\s*\()", re.I)
_NAME_RE = re.compile(r"(?:\b(?:const\s+)?string\s+|\b)([A-Za-z_]\w*)\s*(?:=>|=|\+=)\s*$")
_CALL_RE = re.compile(r"([A-Za-z_]\w*)\s*[\(,]\s*$")


def owner_name(text: str, pos: int) -> str:
    back = text[max(0, pos - 300):pos].rstrip()
    back = re.sub(r"(\"\s*\+\s*)$", "", back)
    m = _NAME_RE.search(back)
    if m:
        return m.group(1)
    m = _CALL_RE.search(back)
    return (m.group(1) + "(...)") if m else "<inline>"


@dataclass
class AuditRow:
    file: str
    line: int
    name: str
    size: int
    interpolated: bool
    hard: list[str]
    soft: list[str]
    refs: int = 0
    live: bool = True

    @property
    def status(self) -> str:
        if self.hard:
            return "REWRITE"
        if self.soft:
            return "MECHANICAL"
        return "PG"


@dataclass
class AuditReport:
    root: Path | None = None
    files_scanned: int = 0
    rows: list[AuditRow] = field(default_factory=list)
    duration_ms: int = 0

    @property
    def tsql_rows(self) -> list[AuditRow]:
        return [r for r in self.rows if r.status != "PG"]

    @property
    def live_tsql(self) -> list[AuditRow]:
        return [r for r in self.tsql_rows if r.live]

    def marker_counts(self) -> dict[str, int]:
        counts: collections.Counter = collections.Counter()
        for r in self.live_tsql:
            counts.update(r.hard + r.soft)
        return dict(counts.most_common())


def _literals(text: str):
    """(start, sql, interpolated) for every string literal, sharing the scan's
    tokenizer so the two can never disagree about what a literal is."""
    return csharp._walk_literals(text, lambda s: len(s) >= 12)


def audit_file(path: Path, root: Path) -> list[AuditRow]:
    try:
        text, _enc = scan.read_source(path)
    except (OSError, UnicodeDecodeError, LookupError):
        return []
    groups: "collections.OrderedDict[tuple, dict]" = collections.OrderedDict()
    for lit in _literals(text):
        name = owner_name(text, lit.start)
        key = (name, lit.line) if name.startswith("<") or name.endswith("(...)") else (name, None)
        g = groups.setdefault(key, {"line": lit.line, "parts": [], "interp": False})
        g["parts"].append(lit.sql)
        g["interp"] = g["interp"] or lit.interpolated
    rows = []
    try:
        shown = str(path.relative_to(root)).replace("\\", "/")
    except ValueError:
        shown = str(path)
    for (name, _), g in groups.items():
        sql = "\n".join(g["parts"])
        if not _SQLISH.search(sql) and not csharp.looks_like_sql_fragment(sql):
            continue
        hard, soft = markers.find(sql)
        rows.append(AuditRow(shown, g["line"], name, len(sql), g["interp"], hard, soft))
    return rows


def run(root: str | Path, *, recursive: bool = True,
        on_progress: Callable[[Path, int, int], None] | None = None,
        should_stop: Callable[[], bool] | None = None) -> AuditReport:
    started = time.perf_counter()
    root = Path(root)
    report = AuditReport(root=root)
    files = scan.find_cs_files(root, recursive)
    identifiers: collections.Counter = collections.Counter()
    for index, path in enumerate(files, start=1):
        if should_stop and should_stop():
            break
        report.rows.extend(audit_file(path, root))
        try:
            identifiers.update(re.findall(r"[A-Za-z_]\w*", scan.read_source(path)[0]))
        except Exception:
            pass
        report.files_scanned += 1
        if on_progress:
            on_progress(path, index, len(files))
    for row in report.rows:
        bare = row.name.split("(")[0]
        row.refs = identifiers.get(bare, 0)
        row.live = row.name.startswith("<") or row.name.endswith("(...)") or row.refs >= 2
    report.duration_ms = int((time.perf_counter() - started) * 1000)
    return report


def write_csv(report: AuditReport, path: Path) -> Path:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["file", "line", "constant", "status", "live", "refs", "size",
                         "interpolated", "hard_markers", "soft_markers"])
        for r in report.rows:
            if r.status == "PG":
                continue
            writer.writerow([r.file, r.line, r.name, r.status, "live" if r.live else "DEAD",
                             r.refs, r.size, "yes" if r.interpolated else "",
                             "; ".join(r.hard), "; ".join(r.soft)])
    return path


def render_report(report: AuditReport) -> str:
    tsql = report.tsql_rows
    live = report.live_tsql
    rewrite = [r for r in live if r.status == "REWRITE"]
    mech = [r for r in live if r.status == "MECHANICAL"]
    lines = [
        "# T-SQL audit (report only -- nothing was changed)",
        "",
        f"- Root: `{report.root}`",
        f"- Duration: {report.duration_ms / 1000:.1f}s",
        "",
        "| | Count |",
        "|---|---|",
        f"| .cs files scanned | {report.files_scanned} |",
        f"| SQL constants found | {len(report.rows)} |",
        f"| already PostgreSQL (no T-SQL marker) | {len(report.rows) - len(tsql)} |",
        f"| **still T-SQL -- live** | **{len(live)}** |",
        f"| &nbsp;&nbsp;needs a whole-constant rewrite | {len(rewrite)} |",
        f"| &nbsp;&nbsp;mechanical (Scan tab can convert) | {len(mech)} |",
        f"| still T-SQL -- dead (never referenced) | {len(tsql) - len(live)} |",
        "",
        "## Live T-SQL by marker",
        "",
        "| Marker | Constants |",
        "|---|---|",
    ]
    lines += [f"| {k} | {v} |" for k, v in report.marker_counts().items()]
    lines += ["", "## Live constants", ""]
    for r in sorted(live, key=lambda x: (x.file, x.line)):
        tag = " (interpolated)" if r.interpolated else ""
        lines.append(f"- `{r.file}:{r.line}` **{r.name}** -- {r.status}{tag}: "
                     + ", ".join(r.hard + r.soft))
    lines += ["", "A clean audit means no known T-SQL MARKER is left. It is not proof "
                  "that a query runs: execute it (scripts/pg-conversion/run_real.py) -- "
                  "silent wrong-data defects are only found that way.", ""]
    return "\n".join(lines)


def write_report(report: AuditReport, filename: str = "tsql-audit.md") -> Path | None:
    if report.root is None:
        return None
    try:
        path = report.root / filename
        path.write_text(render_report(report), encoding="utf-8")
        write_csv(report, report.root / "tsql-audit.csv")
        return path
    except OSError:
        return None
