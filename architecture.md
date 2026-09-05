# Architecture — MS-SQL → PostgreSQL Converter

## Load-bearing decisions

### 1. Three-layer separation
```
ui/        Tkinter — never transpiles
core/      orchestrator, models, autofix, linter, validator, insights,
           csharp, scan, applog, version
engines/   sqlglot_engine.py — never touches widgets
```
The UI knows nothing about how conversion happens. This is what let the C#
project scan land as an additive tab rather than a rewrite.

### 2. SQLGlot two-pass invocation — MANDATORY
SQLGlot's default `ErrorLevel` is `WARN`: it logs to Python `logging` and returns
**partial output with no exception**. In a GUI those log lines are invisible, so
the user receives confidently-wrong SQL. The single most dangerous failure mode.

- Pass 1 (detection): `unsupported_level=ErrorLevel.RAISE` → catch
  `UnsupportedError`, record one ERROR per unsupported construct.
- Pass 2 (output): default error level, with a `logging.Handler` attached to the
  `sqlglot` logger for the call duration.

**Rule: never render SQLGlot output without its warnings.**

### 3. Engines never raise
A conversion problem returns `ok=False` plus an ERROR warning.

### 4. Threading
Tkinter widgets are not thread-safe. Work runs on worker threads and returns
data only; `root.after(0, ...)` marshals back to the UI thread, which does all
rendering. Applies to both conversion and the project scan.

### 5. Three tiers of correction — the central design idea

| Tier | Module | Rule |
|---|---|---|
| **auto-fix** A001–A009 | `core/autofix.py` | Exactly one defensible answer ⇒ correct it on the AST, **and always disclose it as a warning** |
| **lint** L001–L014 | `core/linter.py` | Needs a judgement call about the target schema ⇒ report, never rewrite |
| **validate** | `core/validator.py` | Real PostgreSQL grammar. INVALID ⇒ `is_usable = False` |

**Every auto-fix has a documented refusal case.** This is what keeps the tier
honest — if a fix has no case it declines, it is guessing:

- A001 rowversion: if the source says `ROWVERSION` anywhere, change nothing
- A002 timezone: unknown Windows name left alone (guessing shifts every timestamp)
- A003 dates: `05-07-2026` is ambiguous — left for L014 (guessing corrupts silently)
- A007 boolean: only columns *proven* boolean (used with `CAST(0 AS BIT)`) or
  named `^Is[A-Z]`; `COALESCE(Quantity, 0) = 0` is untouched
- A009 qualifier: only SQL Server's default `dbo` is dropped; a real schema
  (`reporting.Sales`) is preserved

Auto-fixes are AST rewrites, not text substitution — a column merely NAMED
`timestamp`, or `'VARCHAR(MAX)'` inside a comment, is never touched.

### 6. Linter compares input to output
Rules receive both the original T-SQL and the converted SQL, so they detect
*disappearance* (L007 EXEC dropped, L009 mass statement loss) — not just bad
output. String literals are masked before matching, EXCEPT for rules whose
defect lives inside a literal (L012 timezone, L014 date).

### 7. Validator is an allowlist, linter is a blocklist
The linter only finds defects someone wrote a rule for. `pglast` embeds
libpg_query — PostgreSQL's own C parser — so what it accepts is *defined by
PostgreSQL*. Only the allowlist catches unknown unknowns.

**Its blind spot, documented in tests:** valid syntax with wrong semantics.
`CAST(x AS BIT)`, `a + ' - ' + b`, `ISNULL()` all parse cleanly. This is why
tier 1 (auto-fix) exists — the validator cannot see these.

### 8. C# scan uses a tokenizer, not a regex
`core/csharp.py` walks the source tracking comments, char literals and every
string form. A regex would match inside `// var q = @"SELECT..."` and corrupt
real code. For a feature that **rewrites customer source in place**, the
tokenizer is the minimum bar, not over-engineering.

`core/scan.py` safety rules, all tested:
- `.bak` before first modification, never overwritten (always holds the original)
- Rewrite ONLY when the conversion is usable (VALID + zero ERRORs)
- Interpolated `$"...{expr}..."` reported, never rewritten
- `bin`/`obj`/`.vs`/`packages`/`*.Designer.cs`/`*.g.cs` skipped
- Per-file isolation: one exploding file becomes a FAILED row, the run completes

## Verified SQLGlot defects (v30.14.0)
All silent at default settings:
- `CREATE PROCEDURE dbo.usp_X @P INT AS BEGIN ... END` → name, params, body lost
- `EXEC(@DynamicSQL);` → statement absent from output
- `DECLARE @T NVARCHAR(50)` → `DECLARE $T VARCHAR(MAX)` (invalid PostgreSQL)
- `a + ' - ' + b` → unchanged (PostgreSQL cannot `+` text; needs `||`)
- `TRY_CAST(x AS INT)` → `CAST(x AS INT)` (raises instead of returning NULL)
- `IsActive BIT DEFAULT 1` → unchanged (PG `BIT` is a bit-string, not boolean)
- `SELEKT * FRM` → echoed verbatim, parsed as a **multiplication expression**
- `CREATE TABL x (` → echoed verbatim as an `exp.Command` passthrough

**SQLGlot does not reject input it cannot understand.** It reinterprets it as an
expression or wraps it in `exp.Command`. Detect at the AST level — L010 checks
for `exp.Command` nodes and top-level expression-type nodes. No regex over
output text can catch it.

## Packaging is a distinct execution environment

### 1. No console in `--windowed`
`sys.stdin`, `sys.stdout`, `sys.stderr` are all **None**. `sys.stdin.isatty()`
raises `AttributeError` before the window opens. Guarded by
`app.stdin_is_piped()`; regression test in `tests/test_app_launcher.py`.
Invisible to the test suite AND to `python app.py` — a console exists in both.

### 2. `--collect-all` is load-bearing for BOTH sqlglot and pglast
Both import dynamically, so PyInstaller's static analysis never bundles them.
Without the flags the exe builds and launches fine, then fails at runtime.
Missing `--collect-all pglast` nearly shipped an exe where validation silently
degraded to "NOT CHECKED".

`build.bat` uses ONE shared flag variable for the GUI and console twin, and
**fails the build** unless the packaged binary prints `PostgreSQL syntax: VALID`.

## Verifying the GUI
`Get-Process | Where MainWindowHandle -ne 0` is the real check. A live PID proves
nothing — a crashed build stayed resident for seconds with no window.
`SetForegroundWindow` is blocked by Windows; capture with `PrintWindow` against
the handle.

## Gotchas
- Pin `sqlglot`. An unpinned upgrade can silently change output correctness.
- Tkinter highlighting is skipped above 60,000 chars — `tag_add` is too slow.
- L001 cannot detect `a + b` where both operands are text *columns* — no type
  information exists in regex or AST. L009 and manual review are the backstop.
- A007 depends on the TARGET schema, not just dialect rules, so unlike A001–A006
  it can be wrong. It therefore NAMES the columns it treated as boolean instead
  of printing a count. Disclosure scales with uncertainty.
