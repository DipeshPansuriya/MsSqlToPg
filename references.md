# References — MS-SQL → PostgreSQL Converter

## Libraries
- SQLGlot docs — https://sqlglot.com/sqlglot.html
- SQLGlot repo — https://github.com/tobymao/sqlglot
- SQLGlot T-SQL dialect — `sqlglot/dialects/tsql.py`
- SQLGlot PostgreSQL dialect — `sqlglot/dialects/postgres.py`
- Error levels — `sqlglot.errors.ErrorLevel` (`IGNORE` / `WARN` / `RAISE` / `IMMEDIATE`)
- pglast — https://github.com/lelit/pglast (Python wrapper over libpg_query)
- libpg_query — https://github.com/pganalyze/libpg_query (PostgreSQL's own C parser)

## PostgreSQL semantics behind the rules
- String concatenation uses `||`, not `+`
- `BIT` is a fixed-length **bit-string** type; T-SQL `BIT` is boolean → `BOOLEAN`
- Comparing BOOLEAN with INTEGER fails: *"COALESCE types boolean and integer
  cannot be matched"* — the error that drove A007
- T-SQL `TIMESTAMP` is `ROWVERSION` (8-byte binary) → converts to `BYTEA` →
  *"cannot cast type date to bytea"* — the error that drove A001
- No `VARCHAR(MAX)` — use `TEXT`
- No `TRY_CAST`; nearest equivalent needs an exception-handling function
- Only IANA timezone names; a Windows name raises *"time zone ... not recognized"*
- Default DateStyle is MDY, so `'31-07-2026'` errors and `'05-07-2026'` silently
  becomes 7 May
- No cross-database queries; no `dbo` schema
- Indexes are never `CLUSTERED`; procedural code lives in `DO $$ ... $$` blocks

## C# string literal forms (for the project scan)
- Verbatim `@"..."` — `""` is an escaped quote
- Regular `"..."` — `\"` escaped; cannot span lines
- Raw `"""..."""` (C# 11) — no escaping; fence widens if content contains it
- Interpolated `$"..."` / `$@"..."` / `@$"..."` — `{expr}` holes, never rewritten
- Dapper parameters `@UserId` survive conversion untouched (they are not T-SQL
  variables to sqlglot — verified)

## Real-world inputs used as regression fixtures
- A 57 KB production dashboard query — source of L011–L014 and A001–A009
- `GetUserConfigQueries.cs` / `GetUserConfigQuery.cs` from `Login_Command` —
  source of the C# scan fixtures (`tests/fixtures/csharp/`)

## Internal
- Design doc — `_documents/2026-07-30-mssql-to-postgres-converter-design.md`
  (historical: predates removal of ChartDB and batch mode)
- Source code — `E:\Project\MsSqlToPg\`
- Logs — `E:\Project\MsSqlToPg\dist\logs\`
- Related but separate — `_project_databasemigration` (SQL Server → SQL Server
  *data* migration; no overlap with this query converter)
