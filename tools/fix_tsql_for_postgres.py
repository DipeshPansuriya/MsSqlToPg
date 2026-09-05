"""Pre-convert fixes for T-SQL that is heading to PostgreSQL.

Run this on the SOURCE T-SQL *before* converting. Every fix here addresses a
defect that either survives conversion silently or produces valid-looking
PostgreSQL that fails at runtime.

    python tools/fix_tsql_for_postgres.py dashboard.sql -o dashboard_fixed.sql

Use --dry-run to see the counts without writing anything.

WHY EACH FIX EXISTS -- all eight were found on a real 58 KB dashboard query
that converted "cleanly" and then failed on the first statement.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Fix:
    code: str
    why: str
    pattern: str
    replacement: str
    flags: int = re.IGNORECASE
    manual: bool = False        # cannot be auto-fixed; report only


# Applied in order. Order matters: NVARCHAR(MAX) must be handled before the
# generic NVARCHAR rule would otherwise see it.
FIXES: list[Fix] = [
    Fix(
        "F001-timestamp",
        "T-SQL TIMESTAMP is ROWVERSION (8-byte BINARY), NOT a datetime. It "
        "converts to BYTEA and fails with 'cannot cast type date to bytea'. "
        "DATETIME2 is what was meant.",
        r"\bAS\s+timestamp\b", "AS datetime2",
    ),
    Fix(
        "F001-timestamp-col",
        "Same, in column declarations.",
        r"(\b\w+\s+)timestamp(\s*[,)\n])", r"\1datetime2\2",
    ),
    Fix(
        "F002-nvarchar-max",
        "NVARCHAR(MAX)/VARCHAR(MAX) has no PostgreSQL equivalent -- use TEXT.",
        r"\b(?:N)?VARCHAR\s*\(\s*MAX\s*\)", "TEXT",
    ),
    Fix(
        "F003-clustered",
        "PostgreSQL has no CLUSTERED index. Plain CREATE INDEX; add "
        "'CLUSTER tbl USING ix' afterwards only if you need physical ordering.",
        r"\bCREATE\s+CLUSTERED\s+INDEX\b", "CREATE INDEX",
    ),
    Fix(
        "F004-getutcdate",
        "GETUTCDATE() does not exist in PostgreSQL.",
        r"\bGETUTCDATE\s*\(\s*\)", "(NOW() AT TIME ZONE 'UTC')",
    ),
    Fix(
        "F005-timezone",
        "PostgreSQL uses IANA timezone names, not Windows ones. "
        "'India Standard Time' is unknown to PG; 'Asia/Kolkata' is correct.",
        r"'India Standard Time'", "'Asia/Kolkata'",
    ),
    Fix(
        "F006-bit-literal",
        "T-SQL BIT is a boolean; PostgreSQL BIT is a bit-string. "
        "The replacement is the LITERAL FALSE, not CAST(0 AS BOOLEAN) -- "
        "PostgreSQL cannot cast integer to boolean and CAST(0 AS BOOLEAN) "
        "parses fine then fails at runtime.",
        r"\bCAST\s*\(\s*0\s+AS\s+BIT\s*\)", "FALSE",
    ),
    Fix(
        "F006-bit-literal-1",
        "Same for CAST(1 AS BIT) -> the literal TRUE.",
        r"\bCAST\s*\(\s*1\s+AS\s+BIT\s*\)", "TRUE",
    ),
    Fix(
        "F007-dbo-prefix",
        "PostgreSQL has no 'dbo' schema. Function calls must drop the prefix "
        "(and the function itself has to be ported).",
        r"\bdbo\.(fnGetLocalTime)\b", r"\1",
    ),
    Fix(
        "F008-date-literal",
        "DD-MM-YYYY fails under PostgreSQL's default DateStyle -- it reads 31 "
        "as the month. ISO format is unambiguous.",
        r"'(\d{2})-(\d{2})-(\d{4})(\s+[\d:]+)?'", r"'\3-\2-\1\4'",
    ),
]

# Reported but never auto-rewritten: the correct target depends on how the
# database was actually laid out, and guessing would silently break the query.
MANUAL: list[Fix] = [
    Fix(
        "M001-cross-database",
        "PostgreSQL cannot query across databases. 'qa_northwind_final.dbo.Table' "
        "must become 'schema.Table' inside ONE database -- decide the schema "
        "and rewrite, or create a foreign-data wrapper.",
        r"\b\w+\.(?:dbo|\[dbo\])\.\[?\w+\]?", "", manual=True,
    ),
    Fix(
        "M002-udf",
        "fnGetLocalTime is a user-defined function -- it must be ported to "
        "PL/pgSQL and exist in the target database before this query runs.",
        r"\bfnGetLocalTime\b", "", manual=True,
    ),
    Fix(
        "M003-bit-column",
        "Columns still declared BIT need reviewing: BIT in PostgreSQL is a "
        "bit-string type, not a boolean.",
        r"(?<![\w.])BIT\b(?!\s*(?:VARYING|\())", "", manual=True,
    ),
]


def apply_fixes(sql: str) -> tuple[str, list[tuple[str, int, str]]]:
    """Return (fixed_sql, [(code, count, why)])."""
    report: list[tuple[str, int, str]] = []
    for fix in FIXES:
        fixed, count = re.subn(fix.pattern, fix.replacement, sql, flags=fix.flags)
        if count:
            report.append((fix.code, count, fix.why))
            sql = fixed
    return sql, report


def find_manual(sql: str) -> list[tuple[str, int, str]]:
    found: list[tuple[str, int, str]] = []
    for fix in MANUAL:
        count = len(re.findall(fix.pattern, sql, flags=fix.flags))
        if count:
            found.append((fix.code, count, fix.why))
    return found


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Apply pre-conversion fixes to T-SQL bound for PostgreSQL.")
    parser.add_argument("file", help="the .sql file to fix")
    parser.add_argument("-o", "--out", help="output file (default: <name>_fixed.sql)")
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would change, write nothing")
    args = parser.parse_args()

    source = Path(args.file)
    sql = source.read_text(encoding="utf-8", errors="replace")

    fixed, report = apply_fixes(sql)
    manual = find_manual(fixed)

    print(f"Source: {source}  ({len(sql):,} chars)\n")
    if report:
        print("AUTO-FIXED")
        for code, count, why in report:
            print(f"  [{code}] x{count}")
            print(f"      {why}")
        print()
    else:
        print("No auto-fixable issues found.\n")

    if manual:
        print("NEEDS MANUAL WORK -- not rewritten, the right answer depends on "
              "your schema")
        for code, count, why in manual:
            print(f"  [{code}] x{count}")
            print(f"      {why}")
        print()

    if args.dry_run:
        print("(dry run -- nothing written)")
        return 0

    destination = Path(args.out) if args.out else \
        source.with_name(source.stem + "_fixed" + source.suffix)
    destination.write_text(fixed, encoding="utf-8")
    print(f"Written: {destination}  ({len(fixed):,} chars)")
    print("\nNext: run this through MsSqlToPg.exe, then address the manual items.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
