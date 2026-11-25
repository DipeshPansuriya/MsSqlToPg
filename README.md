# MS-SQL → PostgreSQL Converter

[![GitHub release](https://img.shields.io/github/release/DipeshPansuriya/MsSqlToPg.svg?label=latest)](https://github.com/DipeshPansuriya/MsSqlToPg/releases/latest)
[![GitHub tag](https://img.shields.io/github/tag/DipeshPansuriya/MsSqlToPg.svg)](https://github.com/DipeshPansuriya/MsSqlToPg/tags)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.8+](https://img.shields.io/badge/python-3.8+-blue.svg)](https://www.python.org/)
[![Platform: Windows](https://img.shields.io/badge/platform-windows-lightgrey)](https://github.com/DipeshPansuriya/MsSqlToPg/releases)

Desktop utility that converts T-SQL to PostgreSQL, validates the result against
the **real PostgreSQL grammar**, and reports what changed and what still needs
attention.

**Fully offline.** No network calls, no API keys, no query text ever leaves the
machine. Safe to hand to anyone.

## Latest release — v2.18.1

Download the self-contained Windows executable from the
[releases page](https://github.com/DipeshPansuriya/MsSqlToPg/releases):

- **`MsSqlToPg.exe`** — ~17 MB, one file, no Python or prerequisites needed
- `boolean_columns.txt` / `column_types.txt` — optional schema hint files shipped beside the exe
- `boolean_columns_query.sql` / `column_types_query.sql` — read-only queries to regenerate those files

See the [changelog](#changelog) below for what changed in each version.

## Quick start

```bat
pip install -r requirements.txt
python app.py                 :: GUI
```

Or just run `dist\MsSqlToPg.exe` — self-contained, ~17 MB, no prerequisites.

## Scan a C# project

Finds MS-SQL string literals in `.cs` files and rewrites them as PostgreSQL,
in place.

```bat
MsSqlToPg.exe --scan "E:/Project/Login_Command" --dry-run   :: report only
MsSqlToPg.exe --scan "E:/Project/Login_Command"             :: rewrite in place
```

Or the **Scan C# project** tab in the GUI.

## Audit what is left, and check converted code (v2.13.0)

```bat
MsSqlToPg.exe --audit "D:/TFS/KLSPL.Community.Import.API"                :: report only
MsSqlToPg.exe --check-pg "D:/TFS/KLSPL.Community.Import.API" --dry-run   :: report only
MsSqlToPg.exe --check-pg "D:/TFS/KLSPL.Community.Import.API"             :: repair in place
```

Or the **Audit / Check converted PG** tab.

- **Audit** lists every SQL constant that still holds T-SQL, live or dead
  (a name referenced nowhere but its own declaration), split into
  *whole-constant rewrite* (IF/DECLARE/#temp/cursor/dynamic SQL...) and
  *mechanical*. It reads interpolated strings, local-variable SQL and `+=`
  tails too, and strips SQL comments before matching. Writes
  `tsql-audit.md` + `tsql-audit.csv`. Never changes a file.
- **Check converted PG** reads already-converted SQL *as PostgreSQL* — never
  re-scan converted code with `--scan` — and repairs the defects found by hand
  in Aug–Sep 2026 (PC01–PC08: `fnGetLocalTime(CAST(... AS TIMESTAMP))`,
  `NUMBER_TO_STR`, welded `ON`, booleans vs 0/1, fabricated names,
  `YEAR()/MONTH()/DAY()`, temp-table guards, `DDTHH` masks, `WITH ROLLUP`)
  and, since v2.14.0, PC09–PC11: the missing `;` between the statements of a
  multi-result query, T-SQL `[name]` brackets, and `'%@x%'` parameters the
  query processor never binds. Constants that open with a `-- PostgreSQL …`
  comment header are now checked too.
  Every repair is re-validated; a literal that still has T-SQL is never
  touched. `.bak` backups, `pg-check-report.md`.

## v2.14.0 — what changed for converted queries (2026-10-07)

- **`[brackets]`** (A051 in Convert/Scan, PC10 in Check-PG): `[Master_Tax]`,
  `[ER].[IsDeleted]` used to become `"Master_Tax"`, `"ER"."IsDeleted"` — valid
  grammar, but PostgreSQL matches quoted names case-sensitively against a
  lower-case schema, so they failed at runtime. They now come out bare;
  reserved words become `"order"`, names with spaces keep their quotes.
- **`;` between statements**: Check-PG adds it (PC09); Convert no longer loses
  it behind a trailing `--` comment (A018 fix).
- **`'%@search%'`** becomes `'%' || @search || '%'` (A052/PC11); other
  parameters inside text are reported (L030).
- **v2.15.0: `SPLIT(x, ',')`** (left when `dbo.` was stripped from `dbo.Split`, or
  from `CROSS APPLY dbo.Split`) does not exist in PostgreSQL: R001 now reports it,
  Convert rewrites it (A053) and Check-PG repairs it (PC12) to a `STRING_TO_TABLE`
  derived table with dbo.Split's `items` column.

## v2.18.0 — parameters and control-flow batches (2026-11-05)

Measured on the Community Stock module: develop T-SQL converted with this tool,
then executed on PostgreSQL 17 with every parameter typed the way Npgsql binds
the C# value. v2.17.0 ran 53 of 78 outputs; v2.18.0 runs 75.

- **A061** — a Dapper parameter compared with a typed column is cast to that
  type: `Owner_OrgId = CAST(NULLIF(TRIM(CAST('@OrgCode' AS TEXT)), '') AS BIGINT)`,
  `Org_Code = CAST(@AgentOrgId AS TEXT)`. SQL Server converted whatever C# sent;
  PostgreSQL raises `integer = text`. Needs `column_types.txt`.
- **Scan / Check-PG** now pick up literals that open with `IF (...)`,
  `if('@x' = ...)`, `BEGIN <statement> ... END`, or a `--` comment in front of
  T-SQL. They used to be reported as fragments and never converted.

## v2.18.1 — quoted '0'/'1' against a boolean (2026-11-17)

- **A007** — `ISNULL(sdty.InHouse,'0') = '0'` now converts to
  `COALESCE(sdty.InHouse, FALSE) = FALSE` instead of `... = '0'`. A quoted
  `'0'`/`'1'` compared with a boolean column (by schema list, `Is...` name or
  `CAST(.. AS BIT)` proof) becomes FALSE/TRUE, like `0`/`1` and `'true'`/`'false'`
  already did. A string assigned in `SET` is left alone.

## Boolean columns (`boolean_columns.txt`)

Many flag columns follow no naming convention (`GATEINSTATUS`,
`HasAirlineAccepted`), and `COALESCE(flag, 0)` / `flag = 1` against a BOOLEAN
column parses and then fails at runtime. Run `boolean_columns_query.sql`
(read-only) against the PostgreSQL database and save its output as
`boolean_columns.txt` next to the exe. The converter (A024) and the
converted-PG check (PC04) then use the schema's own answer. Check-PG repairs
booleans **only** from this list; a name that merely looks boolean is reported.

## Column types (`column_types.txt`, v2.16.0)

SQL Server converts silently where PostgreSQL refuses: `int_col = text_col`,
`LIKE` on a number, `COALESCE(int_col, '')`, `STRING_AGG(int_col, ',')` all
parse and then fail at runtime. With `column_types.txt` next to the exe (one
`table.column<TAB>type` line per column) A054 makes each conversion explicit the
way SQL Server did it, and a `^Is...` column the schema declares as something
other than boolean is no longer rewritten as a flag. Generate the file with
`column_types_query.sql` (read-only, against the PostgreSQL database), or
without database access from a `pg_dump --schema-only` / EF create script:

```
python -m tools.schema_from_ddl schema.sql --out column_types.txt --merge-boolean boolean_columns.txt
```

The batch's own temp tables, table variables and CTEs are typed from their
definitions in the query, so they need no file. `MSSQLTOPG_COLUMN_TYPES` and
`MSSQLTOPG_BOOLEAN_COLUMNS` point at other copies of the two files.

## Database placeholders (A010, v2.16.0)

`[@MasterdbName].[dbo].[T]` / `@DBName.T` lose the placeholder by default: the
target is ONE PostgreSQL database and the infra's text substitution would turn
`"@MasterdbName".T` into a schema that does not exist. For a platform that maps
each SQL Server database to a PostgreSQL schema, run with
`--keep-db-placeholders` (or `MSSQLTOPG_DB_PLACEHOLDER=schema`).

### Safety rules

Because it edits your source, it is deliberately conservative:

| Rule | Behaviour |
|---|---|
| Backup | `.bak` written before a file is first modified; never overwritten on later runs, so it always holds the original |
| Only clean conversions | A query is rewritten **only** if PostgreSQL syntax is VALID with zero ERROR findings. Anything doubtful is left untouched and reported |
| Interpolated strings | `$"...{expr}..."` is reported, **never** rewritten — the holes are dynamic SQL |
| Composed queries (v2.16.0) | A literal that is one piece of a query C# assembles (`StringBuilder.Append` chain, `+`, `+=`) is reported, never converted alone — the converted piece would end in `;` while the pieces appended after it stay T-SQL |
| Final `;` (v2.16.0) | A literal written without a final `;` is rewritten without one, so C# that extends it elsewhere (`Insert + " RETURNING Id;"`) still produces one statement |
| Comments | A real C# tokenizer, not a regex, so SQL inside `//` or `/* */` is never touched |
| Build output | `bin`, `obj`, `.vs`, `packages`, `*.Designer.cs`, `*.g.cs` skipped |
| Encoding | UTF-8 with or without a BOM; anything that is not valid UTF-8 falls back to `cp1252` and is written back in the **same** encoding, so only the SQL bytes move. A NUL byte means binary, not source — that file FAILs |
| Line endings | Written back with `newline=""` (no translation) and converted SQL re-terminated to the file's dominant ending, so a CRLF file stays CRLF and a rescan is byte-stable. Without this the write added one CR per run (`\r\r\n`, then `\r\r\r\n`, …) |
| Isolation | One unreadable or exploding file becomes a FAILED row; the run completes |

The GUI asks for confirmation before a real run and recommends a clean git
branch. Dry run never prompts and never writes.

### String forms

| C# form | Handling |
|---|---|
| `@"..."` verbatim | rewritten (doubled quotes un-escaped and re-escaped) |
| triple-quoted raw (C# 11) | rewritten (fence widened if the SQL contains one) |
| `"..."` regular | rewritten |
| `$@"...{x}..."` interpolated | reported only |

A string counts as SQL only with a leading DML/DDL keyword **and** a structural
keyword (`FROM`, `INTO`, `JOIN`, `SET`, `VALUES`, …). `"Please SELECT a name"`
is not SQL. A leading `;` is skipped first, so the T-SQL defensive CTE idiom
`;WITH cte AS (…)` is recognised — it used to fail the shape test, which meant
the literal was never converted **and never reported**.

**SQL fragments** — a literal that is *not* a whole statement but still carries
T-SQL, such as a scalar subquery `(SELECT TOP 1 … WITH(NOLOCK) …)`, a `CASE`
expression, a column mapping, or a batch beginning with `DECLARE`/`BEGIN` —
are **reported but never rewritten**. C# splices these into a larger query at
runtime, so the T-SQL in them reaches the database just the same; but a
fragment has no `FROM` clause, which makes its table aliases and its target
context unknowable, and rewriting one is guesswork. Reporting them turns a
silent miss into a row you can act on.

Converted SQL is re-indented to match the surrounding code, and
`scan-report.md` is written to the project root.

## Version and logs

The version appears in the window title, the toolbar, and every log entry:

```bat
MsSqlToPg.exe --version
MS-SQL to PostgreSQL Converter v2.13.0  (L001-L029 lint, A001-A050 auto-fix, R001-R008 resolvability, ...)
Logs: E:\Project\MsSqlToPg\dist\logs
```

Every conversion is logged next to the exe (or `%LOCALAPPDATA%\MsSqlToPg\logs`
if that folder is read-only). **Open log folder** in the toolbar jumps there.

| File | Contents |
|---|---|
| `MsSqlToPg.log` | One block per conversion: version, source, sizes, duration, syntax verdict, every warning with its line number. Rotates at 5 MB, keeps 3. |
| `runs\<stamp>-input.sql` | The exact SQL that was converted |
| `runs\<stamp>-output.sql` | The exact SQL produced |

Only the last 20 runs are kept, so the folder cannot grow without bound. Inputs
over 500,000 chars are not dumped.

The point is diagnosis after the fact: a problem can be investigated from the
log alone, without re-running or re-pasting anything.

```
2026-07-31 14:18:01  v1.0.0  NEEDS REVIEW
  source   : dashboard.sql
  size     : 579 in -> 517 out
  duration : 26 ms
  syntax   : VALID
  findings : 6 error(s), 0 warning(s)
  warnings :
    [ERROR  ] L011: BYTEA came from T-SQL TIMESTAMP ... (line 4)
    [ERROR  ] L012: 'India Standard Time' is a Windows timezone name ... (line 18)
    [ERROR  ] L014: Date literal '31-07-2026' is DD-MM-YYYY ... (line 22)
  sql saved: <logs>/runs/20260731-141801-921-*.sql
uns60731-141801-921-*.sql
```

Logging is best-effort by design — if no location is writable, conversion still
works. That is covered by a test.

## CLI

```bat
python app.py --version                :: version + log location
python app.py query.sql                :: convert a file
python app.py query.sql --no-insights  :: SQL only, no analysis
type query.sql | python app.py --cli   :: from stdin
```

Exit code `0` when the result is usable, `1` when it needs review.

## How assurance works

Three independent layers, each catching what the others cannot:

| Layer | What it is | Catches | Blind to |
|---|---|---|---|
| **sqlglot** | AST transpiler | — | — |
| **linter** (L001–L015) | Blocklist of known defects | Semantic traps: `BIT`, `TRY_CAST`, `text + text`, dropped `EXEC`, `TIMESTAMP`→`BYTEA`, Windows timezones, cross-database refs, DD-MM-YYYY dates, surviving 3-part references | Anything with no rule |
| **pglast validator** | PostgreSQL's own C parser (libpg_query) | **Every** syntax error, including ones nobody anticipated | Valid syntax with wrong meaning |

The linter is a blocklist; the validator is an allowlist defined by PostgreSQL
itself. Only the second catches unknown unknowns.

**Neither catches** valid-syntax-wrong-semantics — `CAST(x AS BIT)` parses fine,
it just means a bit-string instead of a boolean. That needs `PREPARE` against a
live database, or a result-set comparison. The linter covers the common cases
(L003 flags exactly that one); beyond them, human review is the backstop.

An `INVALID` syntax verdict marks the result **not usable**.

## Query Insights

Analysis is shown **beside** the SQL, never inside it — the converted SQL stays
clean and paste-ready.

```
PostgreSQL syntax: VALID

What this query does:
  - Select 6 columns from "Rights_FieldLabels", joined with 1 other table(s)

What changed from T-SQL:
  - ISNULL() -> COALESCE()
  - WITH (NOLOCK) removed -- PostgreSQL readers never block writers

Needs attention:
  ! BIT is STILL PRESENT in the output -- not converted
  ! L003: BIT in PostgreSQL is a bit-string type, not a boolean. (line 6)

Query facts:
  - Statements: 1   - Tables: Master_Localization, Rights_FieldLabels
  - Joins: 1        - Parameters: @OrgProdId, @RoleId
```

### Every claim is verified

A rule may only assert `X -> Y` when the T-SQL form is actually **absent** from
the output. Finding `Y` present is not evidence that `X` is gone.

This exists because an earlier version got it wrong: it claimed `BIT -> BOOLEAN`
while `CAST(x AS BIT)` was still in the SQL, because it matched the string
literals `'true'`/`'false'`. The Warnings panel said the opposite in the same
run. Claims are now matched against literal-masked SQL, and a conversion that
did *not* happen is reported under **Needs attention** instead.

## Dapper parameters

**Npgsql accepts `@name` natively, so a Dapper parameter needs no conversion and
must survive byte-for-byte.**

sqlglot rewrites every `@Name` to `$Name` — invalid PostgreSQL (it uses `$1`/`$2`
positionally, never `$Name`) and it breaks Dapper at runtime.
`restore_dapper_parameters()` undoes that.

```sql
-- in                                    -- out
SELECT TOP (@PageSize) *                 SELECT *
FROM Orders                              FROM Orders
WHERE CustomerId = @CustomerId           WHERE CustomerId = @CustomerId
                                         LIMIT @PageSize
```

`@Name` is ambiguous in T-SQL — either a Dapper placeholder (preserve) or a local
from `DECLARE @Name` (must become a `DO $$ … $$` block). The disambiguator is the
source query: a name never `DECLARE`d and not a procedure parameter is a
placeholder. L004 fires on declared locals and surviving `$Name`, **never on a
plain Dapper parameter**.

## Database-name placeholders

A third kind of `@Name`: a database name the **application substitutes into the
query text** before executing (`FROM [@MasterdbName].[dbo].[Master_Airport]`).
Dropping it would silently repoint the query at the current database, so A010
keeps it as a **quoted schema qualifier**:

```sql
-- in                                          -- out
FROM [@MasterdbName].[dbo].[Master_Airport]    FROM "@MasterdbName".Master_Airport
```

`"@MasterdbName"` is a legal quoted identifier, so grammar validation passes.
The value your application substitutes must now be the PostgreSQL **schema**
holding those tables (exact case — it lands inside double quotes), whether
native or a `postgres_fdw` foreign schema. A placeholder combined with a real
schema (`[@X].[reporting].[T]`) has no two-level mapping — refused and flagged
as L015.

## Warning codes

| Prefix | Source |
|---|---|
| `SG-*` | sqlglot (parse/generate failures, dropped constructs) |
| `PG-SYNTAX` | PostgreSQL grammar validation |
| `L0xx` | Linter |

| Code | Detects |
|---|---|
| L001 | `+` used for string concatenation (PostgreSQL needs `\|\|`) |
| L002 | `VARCHAR(MAX)` survived |
| L003 | Bare `BIT` (PostgreSQL `BIT` is a bit-string, not boolean) |
| L004 | Surviving **declared** `@var` locals, `@@system` vars, or invalid `$var` |
| L005 | `#temp` names surviving, including inside string literals |
| L006 | `TRY_CAST` collapsed to `CAST` (raises instead of returning NULL) |
| L007 | Input had `EXEC` but output has no execution statement |
| L008 | Unconverted T-SQL builtin (`GETDATE`, `ISNULL`, `LEN`, `NOLOCK`, …) |
| L009 | Output under 40% of input size — probable mass statement loss |
| L010 | Input parsed as a value expression or passthrough, not a statement |
| L011 | `BYTEA` from T-SQL `TIMESTAMP` (which is ROWVERSION, not a datetime) |
| L012 | Windows timezone name (`'India Standard Time'`) — PG needs IANA |
| L013 | `db.dbo.Table` cross-database reference — PG cannot query across databases |
| L014 | `DD-MM-YYYY` date literal — PG's default DateStyle is MDY |
| L015 | A database qualifier survived (placeholder + real schema, e.g. `[@X].[reporting].[T]`) — PG accepts 3-part names at parse time but rejects them at execution, so only the AST check catches it |
| L019 | `FOR JSON PATH/AUTO` present — **reported, never guessed**. Dotted aliases build a nested document and `WITHOUT_ARRAY_WRAPPER` collapses the array, so the PostgreSQL form is a hand-built `json_build_object` tree; wrap it in `json_strip_nulls()` to keep FOR JSON's drop-null-keys behaviour, and use `json_` not `jsonb_` (jsonb sorts keys, and field order is part of the payload contract) |
| L020 | an `@name` inside a `--` comment — `ReplaceQueryParameters` is a raw text substitution that strips the quotes from the `'@Param'` form even inside comments. Write the name without the `@` |
| L018 | `UPDATE` targets a FROM alias (`UPDATE S … FROM a X JOIN b S`) — valid PG grammar, fails at runtime with `relation "s" does not exist`; the single-table form is auto-fixed (A019), the join form is reported |

### Known limitation

`a + b` where both operands are text *columns* cannot be detected — neither regex
nor sqlglot's AST carries column type information. Deliberate: flagging every `+`
would drown the real findings.

## Tests

```bat
pip install -r requirements-dev.txt
python -m pytest -q
```

`tests/test_sqlglot_engine.py` is the regression guard against sqlglot upgrades —
**run it before bumping the pinned version**, since dialect behaviour shifts
between releases.

## Build

```bat
build.bat
```

Runs the tests, builds `dist\MsSqlToPg.exe`, then smoke-tests the packaged binary
through a console twin. Two flags are load-bearing and the script enforces both:

- `--collect-all sqlglot` — dialects load dynamically, PyInstaller can't see them
- `--collect-all pglast` — compiled libpg_query extension

Without the second, validation silently degrades to `NOT CHECKED` **in the
packaged build only**. The smoke test fails the build unless the binary actually
prints `PostgreSQL syntax: VALID`.

## Layout

```
app.py                  entry point (GUI + CLI)
core/models.py          ConversionResult, Warning, Severity
core/orchestrator.py    convert + validate, threading
core/linter.py          rules L001-L020
core/pgfixes.py         v2.13.0 rules: text pre-pass, AST fixes A024-A050, lint L021-L029
core/markers.py         the shared T-SQL marker set (R007, fragments, audit)
core/schema_hints.py    boolean_columns.txt / column_types.txt loaders (A024, A054), A010 policy
core/typefix.py         A054 implicit conversions; the batch-aware column type Resolver
core/procedural.py      A058 IF / ELSE flattening, statement segmentation for A031
core/totalrecords.py    A059/A060/L032 (PC15/PC16): TotalRecords statements vs ACS's filterString pager
tools/schema_from_ddl.py  column_types.txt / boolean_columns.txt from a DDL script
core/audit.py           --audit: remaining T-SQL inventory
core/pgcheck.py         --check-pg: repair already-converted PostgreSQL (PC01-PC16)
core/resolvability.py   R001-R006 runtime-resolvability
core/validator.py       PostgreSQL grammar check (pglast)
core/insights.py        Query Insights (syntax / what / changed / attention)
engines/sqlglot_engine.py
ui/main_window.py       Tkinter window
ui/sql_text.py          SQL pane + highlighting
```

Layering rule: the UI never transpiles or validates; the engine never touches
widgets; the linter and validator are pure text-in / findings-out.

## Rule book

Every auto-fix (A001-A061), lint rule (L001-L032), resolvability rule (R001-R008), converted-PG repair (PC01-PC16) and hand rule (R-CHAT-*) is documented in [CONVERSION_RULES.md](CONVERSION_RULES.md). Update it in the same change as any new rule.

## Changelog

### v2.18.1 — 2026-11-17
- **A007** — a quoted `'0'`/`'1'` compared with a boolean column (by schema list, `Is...` name or `CAST(.. AS BIT)` proof) now converts to `FALSE`/`TRUE` instead of `... = '0'`. A string assigned in `SET` is left alone.

### v2.18.0 — 2026-11-05
- **A061** — a Dapper parameter compared with a typed column is cast to that type (`Owner_OrgId = CAST(... AS BIGINT)`).
- Scan / Check-PG now pick up literals that open with `IF (...)`, `if('@x' = ...)`, `BEGIN ... END`, or a `--` comment in front of T-SQL.

### v2.14.0 — 2026-10-07
- **[brackets]** (PC10): `[Master_Tax]` now comes out bare instead of `"Master_Tax"` (quoted names are case-sensitive in PostgreSQL).
- **`;` between statements** (PC09): Check-PG adds the missing terminator.
- **`'%@search%'`** (PC11) becomes `'%' || @search || '%'`.
- **PC12**: `SPLIT(x, ',')` rewritten to a `STRING_TO_TABLE` derived table.

### v2.13.0 — Audit / Check converted PG
- **Audit** lists every SQL constant that still holds T-SQL.
- **Check-PG** repairs already-converted PostgreSQL (PC01–PC08): `fnGetLocalTime`, `NUMBER_TO_STR`, welded `ON`, booleans vs 0/1, fabricated names, `YEAR()/MONTH()/DAY()`, temp-table guards, `DDTHH` masks, `WITH ROLLUP`.

### Earlier
- **v2.16.0** — column types (`column_types.txt`), database placeholders (`[@MasterdbName]`), IF/ELSE flattening (A058).
- **v2.17.0** — TotalRecords window count (A059).
- **v2.0.0** — Initial release: query conversion, C# project scan, three-tier correction (auto-fix / lint / validate), Query Insights.

Full rule details live in [CONVERSION_RULES.md](CONVERSION_RULES.md).

## License

[MIT](LICENSE) — Copyright (c) 2026 Dipesh Pansuriya.

