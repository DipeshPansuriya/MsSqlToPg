# MsSqlToPg — Tool Update Spec (from Login conversion chat)

**Implemented 2026-08-12 in v2.2.0.** Canonical rule book: `CONVERSION_RULES.md`.

> ⚠ **Renumbering:** spec codes A011/A012/A013 were already taken by fixes
> shipped 2026-08-11 (A011 `+`→`||` concat, A012 STRING_SPLIT, A013 hint-strip).
> Final codes: spec-A011 (TRY_CAST) → **A015** · spec-A012 (boolean `''`) →
> **A014** · spec-A013 (UPDATE/SET glue) → **A016**.

## Implementation status

| Item | Status |
|------|--------|
| A007 expand ('true'/'false' compares) + allowlist + `Is2FA` regex | ✅ done (`core/autofix.py`) |
| spec-A011 TRY_CAST → CAST | ✅ done as **A015** (disclosed; L006 keeps the NULL-vs-raise caution) |
| spec-A012 empty-string boolean (+ `schema.expressions` positional read) | ✅ done as **A014** (INSERT values + UPDATE SET) |
| spec-A013 UPDATE/SET same line | ✅ done as **A016** post-pass in `core/orchestrator.py` |
| L016 `INSERT …; RETURNING` ERROR | ✅ done — linter rule + orchestrator fallback (SQLGlot fails the parse on a dangling RETURNING, so the source is checked even when conversion fails) |
| L017 `"quoted"` identifiers INFO | ✅ done |
| F009 / F010 / M004 / M005 in `tools/fix_tsql_for_postgres.py` | ✅ done |
| **New (Vasu 2026-08-12): no extra whitespace during conversion** | ✅ done — `_tidy_whitespace` in orchestrator: interior space runs collapse to one, trailing spaces dropped; indentation and string-literal content untouched |
| `CONVERSION_RULES.md` | ✅ created, canonical |
| `core/version.py` → 2.2.0, RULES = A001–A016 / L001–L017 | ✅ done |
| `README.md` link to rule book | ✅ done |
| `conventions.md` note (rule book updated with every new rule) | ✅ done |
| `open-issues.md` DefaultBranch item | ✅ closed |
| `tests/test_login_conversion_rules.py` | ✅ 13 tests, full suite **290 passed** |
| Rebuild `dist\MsSqlToPg.exe` via `build.bat` | ✅ after green tests |

## Out of scope for the tool (app-side, still open)

- Remove trailing `;` from `InsertLoginLog` base SQL in Community C# (caller appends ` RETURNING Logid;`) — L016 now flags it.
- Set `IsPostgresQuery=true` in `UserDataRepository` for native-PG queries (R-CHAT-03).
- Paste `GetUserDataQuery_PostgreSQL_Fixed.sql` into `GetUserDataQueris.cs` (product code).

## Change log

| Date | Note |
|------|------|
| 2026-08-12 | Initial tool-update spec from Login PG conversion chat. |
| 2026-08-12 | All items implemented in v2.2.0; codes renumbered (see above); whitespace rule added per Vasu. |
