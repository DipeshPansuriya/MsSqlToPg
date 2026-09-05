# Sessions — MS-SQL → PostgreSQL Converter

## 2026-08-10 — v2.0.0: C# project scan; BIT family + cross-DB auto-fixes

**256 tests passing.** `dist\MsSqlToPg.exe` = 17 MB. Vulture + pyflakes clean.

### Started from a real runtime error
Dipesh ran the converted dashboard query and got:

```
ERROR: COALESCE types boolean and integer cannot be matched
```

Cause found in the log (`runs\...-input.sql`): `COALESCE(EG.IsDeleted, 0) = 0`.
His PostgreSQL schema has `IsDeleted` as **boolean** (migrated from `BIT`), but
the T-SQL compares it to integer `0`. SQL Server coerces silently; PostgreSQL
refuses. The validator could not see it — it is valid syntax, wrong semantics.

### Three auto-fixes added (A007–A009)

| | Fix | On his query |
|---|---|---|
| A007 | `COALESCE(flag, 0) = 0` → `= FALSE` | 93 predicates (`IsActive`, `IsDeleted`) |
| A008 | `CAST(0 AS BIT)` → `FALSE`; `BIT` type → `BOOLEAN` | 25 casts |
| A009 | `qa_northwind_final.dbo.T` → `T`; `dbo.fn()` → `fn()` | all, after "we have single db now" |

A009 needed two code paths: tables are `exp.Table`, but `dbo.fnGetLocalTime(...)`
parses as `Dot(Identifier, Anonymous)`. The table fix missed it — it would have
failed with *"schema dbo does not exist"* right after everything else passed.

**Result: his 57 KB query now converts with ZERO errors, syntax VALID.**

### Then: "we write sql query in our project under c# class file"
He sent `GetUserConfigQueries.cs` and asked whether the tool could scan a folder
of C# files, find MS-SQL, and convert it — 2 files or 100+, without crashing.

Asked one question (output mode); he chose **rewrite in place**.

- `core/csharp.py` — a real **tokenizer**, not a regex. Tracks comments, char
  literals and every C# string form. A regex would match inside
  `// var q = @"SELECT..."` and corrupt real code. For in-place rewriting that
  is the minimum bar.
- `core/scan.py` — thread pool, per-file isolation, `.bak` backups, dry run,
  progress callback, stop flag, `scan-report.md`.
- `ui/main_window.py` — `App` became a Notebook: `ConvertTab` + `ScanTab`.
- `app.py` — `--scan DIR [--dry-run] [--no-recursive]`.

Verified on his files: `WITH(NOLOCK)` removed, `ISNULL`→`COALESCE`,
`CAST(...AS BIT)`→`BOOLEAN`, Dapper `@OrgProdId`/`@RoleId` preserved, indentation
matched to his existing style, `GetUserConfigQuery.cs` (DTO) correctly skipped.
120-file scale test passes.

### Two failures worth remembering
1. A bash heredoc silently failed to insert `line_indent`, so the scan reported
   `FAILED: module has no attribute 'line_indent'` — **and the run completed**.
   That was the isolation design proving itself on a real bug, not a test.
2. A later heredoc containing markdown backticks got **executed by the shell**
   and mangled `_memory/overview.md`. Rewritten with the Write tool.
   Convention added: never use heredocs for content with escapes or backticks.

### Dead code
Earlier in the session, `vulture` + `pyflakes` cleanup removed `ConversionResult.engine`/
`.summary`/`.raw`, `ENGINE_NAME`, `worst_severity`, `Validation.statement_count`,
a dead `_candidate_dirs` branch and a shadowed import. Mid-cleanup I deleted the
`pglast.parse_sql()` call inside `validate()` — which would have made **every**
query report VALID. Eleven tests failed instantly and named it.

### Open
`DefaultBranch` in `GetUserConfigQueries.cs` is a flag column A007 does NOT
recognise (no `Is` prefix, never used with `CAST(0 AS BIT)`). If it is boolean in
PostgreSQL, that query still fails. Need Dipesh's flag-column names.

---

## 2026-07-31 (final) — PG parser validation; ChartDB and batch REMOVED

**159 tests passing.** `dist\MsSqlToPg.exe` = 16.7 MB (was 22 MB).

### Scope cut — the app is now one thing
Dipesh: "we use PG Parser API one we have to remove" -> remove ChartDB engine.
Then: "Remove Batch process also".

Deleted: `engines/chartdb_engine.py`, `core/batch.py`, their tests, the engine
selector, the Batch tab, `requests`. **The app now makes NO network calls** —
conversion is sqlglot (local), validation is pglast (local). Safe to hand to
anyone; the data-governance concern about sharing the exe is gone.

`tests/test_orchestrator.py::test_no_network_call_is_made` monkeypatches
`socket.socket` to fail — offline is now a tested invariant, not a claim.

### Added: PostgreSQL grammar validation (`core/validator.py`)
Answering "how do we know the converted query is perfect?" — you cannot prove it
statically, but you can climb a ladder. We took level 2 of 4:

1. Pattern lint (had) — blocklist of anticipated defects
2. **Real PG parser (added)** — `pglast` wraps libpg_query, PostgreSQL's own C
   parser. An ALLOWLIST defined by PostgreSQL itself; catches unknown unknowns.
3. `PREPARE` on a live PG — would catch `ISNULL()`, `text + text`, bad columns
4. Result-set diff — the only true proof; the only thing that catches the BIT class

Verified catches that the linter alone could miss: `$Name`, `VARCHAR(MAX)`,
surviving `TOP`, surviving `NOLOCK`, `DECLARE @x`, malformed input.
Verified blind spots (documented in tests): `CAST(x AS BIT)`, `a + ' - ' + b`,
`ISNULL()` — all parse cleanly, all need level 3/4.

An INVALID verdict sets `is_usable = False`.

### Bug caught during the build
The GUI PyInstaller command was missing `--collect-all pglast` — only the
smoke-test twin had it. That would have shipped an exe where validation silently
degraded to "NOT CHECKED". `build.bat` now uses one shared flag variable and
**fails the build** unless the packaged binary prints `PostgreSQL syntax: VALID`.
Lesson: for a validator, the dangerous failure is degrading to "not checked"
while still looking like it ran — gate on the POSITIVE verdict.

## 2026-07-31 (earlier) — Dapper fix, comments, batch mode, SQLGlot default

**160 tests passing, 0 skipped.** `dist\MsSqlToPg.exe` rebuilt (22 MB).

### 1. Dapper parameters — CORRECTNESS BUG, now fixed
Dipesh: "we using Dapper parameter, so in sql we pass @variable name."

SQLGlot rewrote every `@Param` -> `$Param`. Invalid PostgreSQL (`$1`/`$2` are
positional; `$Name` is nothing) AND breaks Dapper at runtime. ChartDB already
handled it correctly. Fixed by `restore_dapper_parameters()`; L004 rewritten to
fire only on DECLAREd locals, `@@system` vars, and surviving `$Name`. See
architecture.md for the disambiguation rule.

### 2. Explanatory comments (`core/annotator.py`)
Purpose line per statement (from the AST) + why-it-changed notes (17 rules
comparing original vs converted). On by default; `--no-comments` / toolbar
checkbox to disable. Generated locally, so SQLGlot output gets them too.

Output is forced ASCII — `->` not `→`. The Windows console is cp1252 and
crashed with UnicodeEncodeError on the arrow during development.

### 3. Batch mode (`core/batch.py`) — pulled forward from Phase 2
Folder in, folder out, structure preserved, `report.md` written. GUI gained a
Batch tab with a live results table and Stop button.

Concurrency is engine-dependent and deliberate: SQLGlot runs 8 files in
parallel; ChartDB runs STRICTLY SEQUENTIALLY with a 1 s pause, because parallel
calls to a free unofficial endpoint would earn 429s for the entire batch.

### 4. Default engine changed Both -> SQLGlot
Dipesh's workload is large queries with no stored procedures, where SQLGlot is
the reliable engine and ChartDB truncates.

### Bugs found while building
- `tree.args["with"]` was renamed `with_` in sqlglot 30.x, so CTE counting
  silently returned 0. Switched to `find_all(exp.CTE)` — node classes are far
  more stable across versions than arg names.
- UI tests were flaky: creating a fresh `tk.Tk()` per test exhausts Tk resources
  and raises TclError, which the skip guard swallowed — so tests silently
  stopped running while still reporting green. Fixed with a module-scoped root.
  Verified stable across 3 consecutive runs.

### Added tests
`test_dapper_parameters.py` (14), `test_annotator.py` (24), `test_batch.py` (18),
`test_ui_smoke.py` (13 — builds the real widget tree in a hidden root; the first
UI coverage in the project).

## 2026-07-31 — Phase 1 built and shipped

**Delivered:** working desktop utility at `E:\Project\MsSqlToPg\`, packaged as
`dist\MsSqlToPg.exe` (22 MB, self-contained). 90 tests passing.

**Scope change mid-session:** Dipesh clarified "We have large Query more than 200
characters, we not have any SP." That inverted the engine recommendation — see
architecture.md. Probed a 15 KB / 60-CTE query:
- SQLGlot: 98 ms, complete, all 60 CTEs
- ChartDB: 29 s, **silently truncated** — 31 of 60 CTEs, cut mid-statement,
  HTTP 200, no `<<<END_SQL>>>`, no summary

That made truncation detection a hard ERROR (`API-TRUNCATED`) rather than a warning.

**Built:** `core/` (models, linter L001–L010, orchestrator), `engines/`
(sqlglot, chartdb), `ui/` (Tkinter window + SQL pane), `app.py` (GUI + CLI),
`tests/` (90 tests, 7 fixtures), `build.bat`, `README.md`.

**Bugs found and fixed during build:**
1. **L010 false positives** — the original allowlist of statement-type names
   flagged `DECLARE`/`SET` as "not SQL". Replaced with `isinstance(tree,
   exp.Condition)`, which is sqlglot's base class for value-producing nodes.
   No allowlist to maintain.
2. **Duplicate L001 warnings** — same defect reported twice per expression.
   Added `linter.dedupe()`.
3. **PyInstaller `--windowed` crash** — `sys.stdin` is `None` with no console,
   so `sys.stdin.isatty()` raised `AttributeError` before the window opened.
   Invisible to `python app.py` and to every test. Fixed + regression test in
   `tests/test_app_launcher.py`.
4. **Missing sqlglot dialects in the exe** — `No module named
   'sqlglot.dialects.postgres'`. sqlglot imports dialects dynamically, so
   PyInstaller misses them. Needs `--collect-all sqlglot`. Now enforced by
   `build.bat`, which also smoke-tests the packaged binary.

**Verified:** window handle + title confirmed via Win32, screenshot captured via
`PrintWindow`. Live both-engine run on the sample query: SQLGlot → NEEDS REVIEW
(8 warnings), ChartDB → USABLE (correct `DO $$` block), `pick_best` chose ChartDB.

**Next steps:** Dipesh to trial on real production queries. Phase 2 batch tab
still deferred.

## 2026-07-30 — Design session

**Brief:** Dipesh asked for a desktop utility to convert MS-SQL queries to
PostgreSQL, initially via the ChartDB API, then extended to also use SQLGlot
("we have complex query are there").

**Work done:**
- Probed the ChartDB endpoint live — confirmed plain-text response with
  `<<<SQL>>>` markers, no auth, ~3–8 s latency.
- Probed `sqlglot` 30.14.0 against seven escalating T-SQL cases. Found it silently
  destroys procedural T-SQL: a stored procedure collapsed to
  `CREATE PROCEDURE  AS BEGIN SET NOCOUNT = ON` with no exception raised.
  Also confirmed unfixed `+` string concat, `TRY_CAST` → `CAST`, bare `BIT`,
  and `EXEC` statement dropped.
- Verified the mitigation: `unsupported_level=ErrorLevel.RAISE` does surface the loss.
- Wrote design doc to `_documents/2026-07-30-mssql-to-postgres-converter-design.md`.

**Decisions:**
- Python + Tkinter (Dipesh's choice)
- Two engines, **Both** side-by-side as default
- Linter (9 rules) included in Phase 1
- Retry + show-raw on ChartDB failure
- Source code at `E:\Project\MsSqlToPg\`
- Batch mode deferred to Phase 2, but core layered so it's additive

**Next steps:**
- Write implementation plan (writing-plans skill)
- Scaffold `E:\Project\MsSqlToPg\`, TDD from the linter and engine tests outward
