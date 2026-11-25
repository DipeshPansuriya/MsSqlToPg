# Open Issues — MS-SQL → PostgreSQL Converter

## Active

- **Case-insensitive search (decision needed).** SQL Server compared `LIKE` / `=` case-insensitively; the
  converter keeps `LIKE` (case-sensitive in PostgreSQL). L025 covers lookup columns only.
- **NULL parameters are bound as TEXT** by some data-access layers when no DbType is
  given, so `int_col = @x` fails only when the value is null. Not visible from the SQL alone; needs the C#
  caller. Converted SQL should `CAST(@x AS INT)` such parameters. **v2.18.0:** A061 does that wherever the
  parameter is compared with a column of known type; assignments (`SET col = @x`, `VALUES (@x)`) are not cast yet.
- **Left from the 2026-11-05 run (v2.18.0):** UNION `''` vs an INT column (SQL Server read '' as 0);
  `ISNULL(varchar, bigint)` takes the FIRST argument's type in SQL Server, `COALESCE` the highest -- after
  transpiling, the output cannot tell which the source used; a `SELECT *` CTAS temp table is not typed when an
  `UPDATE … FROM` joins it; `INSERT INTO #t SELECT t.*` of a BIT into an INT temp column. IF shapes A058 refuses:
  sequential IFs without ELSE, IF without BEGIN/END, nested IF, `SELECT … INTO #t` inside a branch.
- **What v2.16.0 still cannot convert (2026-10-09, reference corpus):** 37 batches build dynamic SQL (EXEC /
  sp_executesql — needs the C# caller rewritten); 29 use FOR XML / FOR JSON / OPENJSON shapes (L019);
  IF batches that assign variables inside a branch or return a different number of result sets per branch
  (A058 refuses them); `DATEADD(unit, n, '@TimeParam')` on a TIME-valued text parameter (only the caller knows
  it is a TIME). When `column_types.txt` is built from ORM models, regenerate it from a live database
  (`column_types_query.sql`) when one is reachable: columns the models do not map are invisible to A054.

- ~~A007 non-`Is*` flag miss~~ **CLOSED 2026-08-12 (v2.2.0):** A007 has a
  known-boolean allowlist (now the profile's `boolean_columns`), matches `Is2FA`-style names (digit after `Is`), and
  rewrites `'true'/'false'` string compares. Extend the allowlist as more
  non-`Is*` flag columns surface — it remains the one fix that depends on the
  target schema.

- **Awaiting the first real `--scan` run.** The C# project scan shipped 2026-08-10
  and is verified on small distilled fixtures, not yet
  on a whole project. Start with `--dry-run`.

## Risks to watch

- **SQLGlot upgrades can silently change output.** Pinned to 30.14.0.
  `tests/test_sqlglot_engine.py` is the guard — run before bumping.
- **PyInstaller flags are load-bearing.** `--collect-all sqlglot`,
  `--collect-all pglast`, and the `stdin_is_piped()` guard each nearly shipped a
  broken exe. Always build via `build.bat`.
- **In-place rewriting is destructive by nature.** `.bak` files and the
  "only rewrite clean conversions" rule are the safety net, but the real
  protection is running it on a clean git branch. The GUI says so; the CLI
  does not enforce it.
- **`dist\logs\` is deleted by a careless `rm -rf dist/logs`.** Happened this
  session — it destroyed the saved copy of Dipesh's original 57 KB query. Not
  critical (he has the source) but the logs are the diagnostic record; do not
  clear them casually.

## Known limitations (by design — do not "fix" blindly)

- L001 cannot detect `a + b` where both operands are text *columns* — no type
  information available. Flagging every `+` would drown real findings.
- The validator cannot catch valid-syntax/wrong-semantics: `CAST(x AS BIT)`,
  `text + text`, `ISNULL()`. That is what the auto-fix tier is for.
- A003 refuses ambiguous dates (`05-07-2026`) — guessing corrupts data with no
  error. L014 reports them instead.
- Interpolated C# strings are never rewritten. Dynamic SQL cannot be converted
  safely around `{expr}` holes.
- Syntax highlighting skipped above 60,000 characters.
- No conversion history / persistence.

## Deferred

- **Level 3 validation** — `PREPARE` against a live PostgreSQL. Would catch
  `ISNULL()`, `text + text`, bad column names, and the A007 boolean question
  definitively. Requires a connection string; not offline.
- **Level 4** — result-set diff between SQL Server and PostgreSQL. The only true
  proof of equivalence.
- Conversion history / SQLite persistence.

## Decided, no longer open

- A010 placeholder policy → **drop** `[@AppDbName].[dbo].` by default (one PostgreSQL database; the
  quoted-schema output failed on 1,580 of 1,756 reference queries when executed). `--keep-db-placeholders` /
  `MSSQLTOPG_DB_PLACEHOLDER=schema` keeps the old form for a schema-per-database target (2026-10-09)

- Engine → sqlglot only; ChartDB removed (2026-07-31)
- Batch folder conversion of `.sql` files → removed (2026-07-31); replaced by the
  C# project scan, which is what Dipesh actually needed (2026-08-10)
- App makes **no network calls** — tested invariant (2026-07-31)
- Toolkit → Tkinter, not PySide (2026-07-30)
- Source location → `E:\Project\MsSqlToPg\` (2026-07-30)
- L010 discriminator → `isinstance(exp.Condition)`, not a name allowlist (2026-07-31)
- Auto-fix tier added, every fix disclosed as a warning (2026-07-31)
- Scan output mode → **rewrite in place with `.bak`**, chosen by Dipesh over
  side-by-side copies or report-only (2026-08-10)
