# Conventions — MS-SQL → PostgreSQL Converter

## Code
- Python 3.14, standard library first. Two third-party deps: `sqlglot`, `pglast`
- `sqlglot` is **pinned** (30.14.0) in `requirements.txt` — do not float it
- Dataclasses for all cross-layer data (`ConversionResult`, `Warning`,
  `Validation`, `SqlLiteral`, `Finding`, `FileReport`, `ScanReport`)
- Type hints on every public function
- No widget access outside `ui/`
- **No dead code.** `vulture --min-confidence 60` and `pyflakes` must both come
  back empty. Checked at the end of every session.

## Comments
Comments explain **why**, never what. A comment that restates the code is
deleted. The bar: would a competent developer be puzzled without it?

Every auto-fix and linter rule carries a docstring explaining the PostgreSQL
behaviour that makes it necessary — and, where it declines to act, why guessing
would be worse than reporting.

## TDD
Tests first. Every new rule ships with:
- a positive case
- a negative case (must NOT fire)
- a string-literal false-positive case

For `core/scan.py` specifically, **most tests assert what must NOT happen** — it
rewrites customer source, so false positives cost more than misses.

No test makes a live network call. `test_no_network_call_is_made` monkeypatches
`socket.socket` to fail: offline is a tested invariant, not a claim.

## Naming
- Auto-fixes: `A001`–`Annn`, one function each, registered in `ALL_FIXES`
- Linter rules: `L001`–`Lnnn`, one function each, registered in `ALL_RULES`
- Fixtures named after the construct under test, e.g. `dashboard_bit_flags.sql`
- Regression fixtures are **distilled from real production queries**, not invented

## Layout
```
E:\Project\MsSqlToPg\
├── app.py                 GUI / CLI / --scan entry point
├── build.bat              the ONLY supported build path
├── core/     models, orchestrator, autofix, linter, validator,
│             insights, csharp, scan, applog, version
├── engines/  sqlglot_engine.py
├── ui/       main_window.py (Notebook: ConvertTab + ScanTab), sql_text.py
└── tests/    + fixtures/ (+ fixtures/csharp/)
```

## Build
Always `build.bat` — never a bare `pyinstaller` call. It builds the GUI exe and
a console twin from ONE shared flag variable, then smoke-tests the packaged
binary and fails the build if validation is not active.

Kill any running `MsSqlToPg.exe` first or the link step fails on a locked file.

## Version discipline
`core/version.py` is the single source. Bump it whenever behaviour changes —
it is stamped into the window title, `--version`, and every log entry, so a bug
report identifies its own build.

## Logs before questions
When Dipesh reports a problem, read `dist\logs\` first. `MsSqlToPg.log` has every
warning with line numbers; `runs\<stamp>-input.sql` / `-output.sql` hold the full
SQL for the last 20 runs. Do not ask him to re-paste anything.

## Artifacts
All Claude-generated docs, reports and scripts go under
`E:\AI Agents\AI-Agent-Team\_project_mssql_postgrsql\`.
Source code goes to `E:\Project\MsSqlToPg\` (explicitly authorised by Dipesh).

## Shell gotcha (cost real time this project)
Bash heredocs mangle Python containing backticks, `\n`, or `$`. Backticks get
executed by the shell; `\\n` collapses. **Use the Write/Edit tools for any file
content with escapes or markdown backticks** — not `python - <<'EOF'`.
