# Open Issues — MS-SQL → PostgreSQL Converter

## Active

- **A007 boolean detection is a heuristic and can be wrong.** It treats a column
  as boolean when the source uses it with `CAST(0/1 AS BIT)`, or when it is named
  `^Is[A-Z]`. On Dipesh's dashboard query that caught `IsActive` and `IsDeleted`
  correctly — but **`DefaultBranch` was missed** (`COALESCE(UB.DefaultBranch, 0) = 1`
  in `GetUserConfigQueries.cs`). If it is boolean in PostgreSQL, that query fails.
  **Next step:** ask Dipesh for his flag-column names and add them to A007's
  known-boolean list. This is the one auto-fix that depends on the target schema.

- **Awaiting the first real `--scan` run.** The C# project scan shipped 2026-08-10
  and is verified on distilled fixtures of Dipesh's `Login_Command` files, not yet
  on the whole project. Start with `--dry-run`.

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
