# MS-SQL → PostgreSQL Converter

Desktop utility that converts T-SQL to PostgreSQL, validates the result against
the **real PostgreSQL grammar**, and reports what changed and what still needs
attention.

**Fully offline.** No network calls, no API keys, no query text ever leaves the
machine. Safe to hand to anyone.

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

### Safety rules

Because it edits your source, it is deliberately conservative:

| Rule | Behaviour |
|---|---|
| Backup | `.bak` written before a file is first modified; never overwritten on later runs, so it always holds the original |
| Only clean conversions | A query is rewritten **only** if PostgreSQL syntax is VALID with zero ERROR findings. Anything doubtful is left untouched and reported |
| Interpolated strings | `$"...{expr}..."` is reported, **never** rewritten — the holes are dynamic SQL |
| Comments | A real C# tokenizer, not a regex, so SQL inside `//` or `/* */` is never touched |
| Build output | `bin`, `obj`, `.vs`, `packages`, `*.Designer.cs`, `*.g.cs` skipped |
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
is not SQL.

Converted SQL is re-indented to match the surrounding code, and
`scan-report.md` is written to the project root.

## Version and logs

The version appears in the window title, the toolbar, and every log entry:

```bat
MsSqlToPg.exe --version
MS-SQL to PostgreSQL Converter v1.0.0  (L001-L014 + PostgreSQL grammar validation)
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
| **linter** (L001–L014) | Blocklist of known defects | Semantic traps: `BIT`, `TRY_CAST`, `text + text`, dropped `EXEC`, `TIMESTAMP`→`BYTEA`, Windows timezones, cross-database refs, DD-MM-YYYY dates | Anything with no rule |
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
core/linter.py          rules L001-L010
core/validator.py       PostgreSQL grammar check (pglast)
core/insights.py        Query Insights (syntax / what / changed / attention)
engines/sqlglot_engine.py
ui/main_window.py       Tkinter window
ui/sql_text.py          SQL pane + highlighting
```

Layering rule: the UI never transpiles or validates; the engine never touches
widgets; the linter and validator are pure text-in / findings-out.
