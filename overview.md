# Overview — MS-SQL → PostgreSQL Converter

## What
Windows desktop utility, **v2.0.0**. Two modes:

1. **Convert a query** — paste T-SQL, get PostgreSQL validated against the real
   PostgreSQL grammar, plus Query Insights (what it does / what changed / what
   needs attention).
2. **Scan C# project** — point at a folder; it finds MS-SQL string literals in
   `.cs` files and **rewrites them in place** with `.bak` backups.

## Stack
- Python 3.14 + Tkinter/ttk
- `sqlglot==30.14.0` (pinned — dialect behaviour shifts between releases)
- `pglast` (embeds libpg_query, PostgreSQL's own C parser)
- PyInstaller `--onefile --windowed` → ~17 MB `MsSqlToPg.exe`

**Fully offline.** No network calls — enforced by a test that monkeypatches
`socket.socket` to fail.

## Code location
`E:\Project\MsSqlToPg\` — all source code lives here.

## Version & logs — READ THE LOG BEFORE ASKING DIPESH ANYTHING

Version appears in the window title, toolbar, `--version`, and every log entry.

**Log location:** `E:\Project\MsSqlToPg\dist\logs\` (falls back to
`%LOCALAPPDATA%\MsSqlToPg\logs\` if the exe folder is read-only).

- `MsSqlToPg.log` — one block per conversion: version, source, sizes, duration,
  PG syntax verdict, and **every warning with its line number**. Rotates at
  5 MB, keeps 3.
- `runs\<stamp>-input.sql` / `-output.sql` — full SQL for the last 20 runs.

**When Dipesh reports a problem, read these first.** They contain the exact
input, exact output and every finding — no need to ask him to re-paste anything.

## Three tiers of correction

| Tier | Module | Behaviour |
|---|---|---|
| **A001–A009** auto-fix | `core/autofix.py` | Unambiguous defects corrected on the AST. **Every one disclosed as a warning — nothing silent.** |
| **L001–L014** lint | `core/linter.py` | Reported, never auto-fixed: needs human judgement |
| **validator** | `core/validator.py` | Real PostgreSQL grammar (pglast). INVALID ⇒ result not usable |

Auto-fixes: A001 `TIMESTAMP`→datetime, A002 Windows timezone→IANA, A003
DD-MM-YYYY→ISO, A004 `VARCHAR(MAX)`→`TEXT`, A005 `CLUSTERED INDEX`→`INDEX`,
A006 `GETUTCDATE()`→`NOW() AT TIME ZONE 'UTC'`, A007 BIT column vs integer
comparison→boolean, A008 `BIT` type/`CAST(0 AS BIT)`→`BOOLEAN`/`FALSE`,
A009 `db.dbo.Table`→`Table`.

Each has a **refusal case** it deliberately leaves alone — see architecture.md.

## C# project scan
`core/csharp.py` uses a real tokenizer (not regex) so SQL inside comments or
char literals is never touched. `core/scan.py` orchestrates: `.bak` backups,
rewrite only when the conversion is usable, interpolated strings reported never
rewritten, `bin`/`obj`/generated files skipped, per-file isolation.

CLI: `MsSqlToPg.exe --scan "E:/Project/App" [--dry-run]`

## Design doc
`_documents/2026-07-30-mssql-to-postgres-converter-design.md`
(NOTE: written before ChartDB and batch mode were removed — architecture.md and
this file are current; the design doc is historical.)
