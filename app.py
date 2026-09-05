"""MS-SQL to PostgreSQL Converter -- entry point.

GUI:    python app.py
File:   python app.py query.sql [--no-insights]
Stdin:  type query.sql | python app.py --cli
Scan:   python app.py --scan "E:/Project/MyApp" [--dry-run]

Fully offline. No network calls: conversion is sqlglot, validation is pglast
(the real PostgreSQL parser). Nothing leaves the machine.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core import applog, insights, orchestrator, scan    # noqa: E402
from core.version import banner                          # noqa: E402
from core.models import ConversionResult, Severity       # noqa: E402

_ICON = {Severity.ERROR: "[ERROR]", Severity.WARNING: "[WARN ]",
         Severity.INFO: "[info ]"}


def _safe_stdout() -> None:
    """Windows consoles default to cp1252 and blow up on non-ASCII output."""
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


def _print_result(result: ConversionResult, original: str,
                  show_insights: bool) -> None:
    # The SQL is printed clean and paste-ready. Analysis goes BELOW it, never
    # inside it as comments.
    if result.sql:
        print(result.sql)
    else:
        print("(no SQL produced)")

    if show_insights and result.sql:
        text = insights.render_text(
            insights.analyze(original, result.sql, result.warnings,
                             result.validation))
        if text:
            print("\n-- Query Insights " + "-" * 51)
            print(text)

    if result.warnings:
        print("\n-- warnings " + "-" * 57)
        for warning in result.sorted_warnings:
            line = f" (line {warning.line})" if warning.line else ""
            print(f"{_ICON[warning.severity]} {warning.code}: "
                  f"{warning.message}{line}")

    print(f"\n>> {'USABLE' if result.is_usable else 'NEEDS REVIEW'}"
          f"   ({result.duration_ms:,} ms)")


def run_cli(args: argparse.Namespace) -> int:
    if args.file:
        sql = Path(args.file).read_text(encoding="utf-8", errors="replace")
    else:
        sql = sys.stdin.read()

    if not sql.strip():
        print("No input SQL provided.", file=sys.stderr)
        return 2

    result = orchestrator.convert(sql)
    applog.log_conversion(sql, result, args.file or "(stdin)")
    _print_result(result, sql, not args.no_insights)
    return 0 if result.is_usable else 1


def run_scan(args: argparse.Namespace) -> int:
    root = Path(args.scan)
    if not root.is_dir():
        print(f"Not a directory: {root}", file=sys.stderr)
        return 2

    files = scan.find_cs_files(root, not args.no_recursive)
    if not files:
        print(f"No .cs files found in {root}", file=sys.stderr)
        return 2

    mode = ("DRY RUN -- nothing will be written" if args.dry_run
            else "REWRITING FILES IN PLACE (.bak backups will be created)")
    print(f"Scanning {len(files)} .cs file(s) under {root}")
    print(f"Mode: {mode}\n")

    def progress(item: scan.FileReport, index: int, total: int) -> None:
        if not item.findings and item.status != scan.FAILED:
            return
        try:
            shown = item.path.relative_to(root)
        except ValueError:
            shown = item.path
        detail = f" -- {item.error}" if item.error else ""
        print(f"[{index:>4}/{total}] {item.status:<10} {shown}"
              f"  ({len(item.findings)} query){detail}")
        for finding in item.findings:
            if finding.action != scan.REWRITTEN:
                print(f"           line {finding.line}: {finding.action} "
                      f"{finding.reason}")

    report = scan.run(root, recursive=not args.no_recursive,
                      dry_run=args.dry_run, on_progress=progress)
    path = scan.write_report(report)

    print("\n" + "=" * 60)
    print(f"Files scanned      : {report.scanned}")
    print(f"Files with SQL     : {report.with_sql}")
    print(f"Queries found      : {report.queries}")
    print(f"Queries rewritten  : {report.rewritten}")
    print(f"Needing attention  : {report.needs_attention}")
    print(f"Files failed       : {report.failed}")
    print(f"Duration           : {report.duration_ms / 1000:.1f}s")
    if path:
        print(f"Report             : {path}")
    applog.log_message(
        f"Scan {root}: {report.queries} queries, {report.rewritten} rewritten, "
        f"{report.needs_attention} need attention, {report.failed} failed"
        f"{' (dry run)' if args.dry_run else ''}")
    return 0 if report.failed == 0 else 1


def stdin_is_piped() -> bool:
    """True when input is being piped in rather than typed.

    PyInstaller's --windowed build has NO CONSOLE: sys.stdin, sys.stdout and
    sys.stderr are all None. A bare `sys.stdin.isatty()` therefore raises
    AttributeError before the window can open -- a crash that only ever appears
    in the packaged build, never under `python app.py`.
    """
    try:
        return sys.stdin is not None and not sys.stdin.isatty()
    except (AttributeError, ValueError, OSError):
        return False


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Convert MS-SQL (T-SQL) to PostgreSQL.")
    parser.add_argument("file", nargs="?",
                        help="a .sql file; omit to read stdin, "
                             "or omit entirely to launch the GUI")
    parser.add_argument("--no-insights", action="store_true",
                        help="do not print the Query Insights section")
    parser.add_argument("--cli", action="store_true",
                        help="force CLI mode when reading stdin")
    parser.add_argument("--scan", metavar="DIR",
                        help="scan a C# project and rewrite MS-SQL literals "
                             "as PostgreSQL, in place (.bak backups made)")
    parser.add_argument("--dry-run", action="store_true",
                        help="with --scan: report what would change, write nothing")
    parser.add_argument("--no-recursive", action="store_true",
                        help="with --scan: do not descend into subfolders")
    parser.add_argument("--version", action="store_true",
                        help="print version and log location, then exit")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    _safe_stdout()
    folder = applog.configure()

    if args.version:
        print(banner())
        if folder:
            print(f"Logs: {folder}")
        return 0

    if args.scan:
        return run_scan(args)

    if args.file or args.cli or stdin_is_piped():
        return run_cli(args)

    from ui.main_window import launch
    launch()
    return 0


if __name__ == "__main__":
    sys.exit(main())
