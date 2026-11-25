@echo off
REM Build MsSqlToPg.exe
REM
REM   BOTH --collect-all FLAGS ARE LOAD-BEARING. Keep them on EVERY PyInstaller
REM   invocation below, including the smoke-test twin:
REM
REM     sqlglot  imports its dialects dynamically via importlib, so PyInstaller's
REM              static analysis never sees them. Without the flag the exe builds
REM              and launches fine, then fails at conversion time with
REM              "No module named 'sqlglot.dialects.postgres'".
REM
REM     pglast   ships a compiled libpg_query extension (the real PostgreSQL
REM              parser). Without the flag, syntax validation silently degrades
REM              to "NOT CHECKED" in the packaged build only.
REM
REM   --windowed gives the exe NO CONSOLE, so sys.stdin/stdout/stderr are all
REM   None at runtime. app.stdin_is_piped() guards for this; see
REM   tests/test_app_launcher.py before changing the launcher.

setlocal
cd /d "%~dp0"

set COLLECT=--collect-all sqlglot --collect-all pglast

echo === running tests ===
python -m pytest -q
if errorlevel 1 (
    echo TESTS FAILED - build aborted.
    exit /b 1
)

echo.
echo === building GUI executable ===
python -m PyInstaller --onefile --windowed --name MsSqlToPg %COLLECT% ^
    --noconfirm app.py
if errorlevel 1 exit /b 1

echo.
echo === smoke-testing the packaged binary ===
REM The windowed exe cannot report its own errors, so build a console twin with
REM IDENTICAL flags and run real conversions through it.
python -m PyInstaller --onefile --console --name MsSqlToPgCheck %COLLECT% ^
    --noconfirm app.py >nul 2>&1
if errorlevel 1 exit /b 1

dist\MsSqlToPgCheck.exe tests\fixtures\merge.sql | findstr /C:"MERGE" >nul
if errorlevel 1 (
    echo SMOKE TEST FAILED - the packaged binary cannot convert SQL.
    exit /b 1
)

REM Prove A023 shipped: FOR XML PATH must come out as STRING_AGG. sqlglot DROPS
REM the FOR XML option, so a stale bundle would silently emit a multi-row subquery
REM where a scalar was meant -- valid grammar, wrong results.
dist\MsSqlToPgCheck.exe tests\fixtures\for_xml_path.sql | findstr /C:"STRING_AGG" >nul
if errorlevel 1 (
    echo SMOKE TEST FAILED - A023 FOR XML PATH rewrite missing from the bundle.
    exit /b 1
)

REM Prove R001 shipped: QUOTENAME PARSES (legal call shape) and dies at runtime
REM with 42883. A bundle without the resolvability pass would report VALID and
REM ship a query that cannot execute. (Until v2.13.0 this used OPENJSON, which
REM A028 now converts -- see the next check.)
dist\MsSqlToPgCheck.exe tests\fixtures\r001_missing_function.sql | findstr /C:"R001" >nul
if errorlevel 1 (
    echo SMOKE TEST FAILED - R001 resolvability pass missing from the bundle.
    exit /b 1
)

REM Prove A028 shipped: OPENJSON must come out as jsonb set functions.
dist\MsSqlToPgCheck.exe tests\fixtures\openjson.sql | findstr /C:"jsonb_array_elements_text" >nul
if errorlevel 1 (
    echo SMOKE TEST FAILED - A028 OPENJSON rewrite missing from the bundle.
    exit /b 1
)

REM Prove the SOURCE-side pass shipped. sqlglot returns an EMPTY string for
REM T-SQL control flow, so an output-only check would report nothing here and
REM R002/R003 would never fire on the constants that most need them.
dist\MsSqlToPgCheck.exe tests\fixtures\control_flow.sql | findstr /C:"R002" >nul
if errorlevel 1 (
    echo SMOKE TEST FAILED - R002 source-side resolvability missing from the bundle.
    exit /b 1
)

REM Prove the v2.13.0 pre-pass and AST rules shipped. Each of these produced
REM grammar-VALID but runtime-broken SQL in v2.12.0 (fabricated NUMBER_TO_STR,
REM elapsed-time DATEDIFF, dropped temp-table guard), so a stale bundle would
REM pass every other smoke test here.
dist\MsSqlToPgCheck.exe tests\fixtures\v213_smoke.sql | findstr /C:"FM999,999,999,999,990.00" >nul
if errorlevel 1 (
    echo SMOKE TEST FAILED - A026 FORMAT rewrite missing from the bundle.
    exit /b 1
)
dist\MsSqlToPgCheck.exe tests\fixtures\v213_smoke.sql | findstr /C:"DROP TABLE IF EXISTS pg_temp.tmp" >nul
if errorlevel 1 (
    echo SMOKE TEST FAILED - A027/A040 temp-table rules missing from the bundle.
    exit /b 1
)
dist\MsSqlToPgCheck.exe tests\fixtures\v213_smoke.sql | findstr /C:"AS DATE) - CAST(" >nul
if errorlevel 1 (
    echo SMOKE TEST FAILED - A035 DATEDIFF boundary rewrite missing from the bundle.
    exit /b 1
)

REM Prove the v2.14.0 rules shipped (from the Master API conversion). Each one
REM was grammar-VALID and broken at runtime, so no earlier check would notice
REM a stale bundle: A051 [brackets] -> bare names (quoted ones are case-
REM sensitive), A052 '%@x%' bound, and the A018 fix -- a trailing `--` comment
REM used to swallow the inferred `;`, so this batch did not parse at all.
dist\MsSqlToPgCheck.exe tests\fixtures\v214_smoke.sql | findstr /C:"MTP.TerminalZone AS TerminalZoneId" >nul
if errorlevel 1 (
    echo SMOKE TEST FAILED - A051 bracket identifiers missing from the bundle.
    exit /b 1
)
dist\MsSqlToPgCheck.exe tests\fixtures\v214_smoke.sql | findstr /C:"|| @SearchString ||" >nul
if errorlevel 1 (
    echo SMOKE TEST FAILED - A052 parameter-literal rewrite missing from the bundle.
    exit /b 1
)
dist\MsSqlToPgCheck.exe tests\fixtures\v214_smoke.sql | findstr /C:"PostgreSQL syntax: VALID" >nul
if errorlevel 1 (
    echo SMOKE TEST FAILED - A018 terminator before a trailing comment missing from the bundle.
    exit /b 1
)

REM Prove the v2.16.0 rules shipped (measured by executing the Master corpus on
REM PostgreSQL): A058 IF/ELSE -> guarded UNION ALL (sqlglot returned EMPTY output
REM for the IF), A055 unqualified SET target, A010 placeholder dropped.
dist\MsSqlToPgCheck.exe tests\fixtures\v216_smoke.sql | findstr /C:"UNION ALL" >nul
if errorlevel 1 (
    echo SMOKE TEST FAILED - A058 IF/ELSE flattening missing from the bundle.
    exit /b 1
)
dist\MsSqlToPgCheck.exe tests\fixtures\v216_smoke.sql | findstr /C:"SET StateCode = S.StateCode" >nul
if errorlevel 1 (
    echo SMOKE TEST FAILED - A055 SET-target qualifier rule missing from the bundle.
    exit /b 1
)
dist\MsSqlToPgCheck.exe tests\fixtures\v216_smoke.sql | findstr /C:"FROM Master_Airline AS MC" >nul
if errorlevel 1 (
    echo SMOKE TEST FAILED - A010 still keeps the database placeholder.
    exit /b 1
)

REM Prove the v2.17.0 rule shipped: A059 turns the TotalRecords count subquery
REM that ACS's filterString pager wrapped (500 on three EDINotification lists)
REM into a window count.
dist\MsSqlToPgCheck.exe tests\fixtures\v217_smoke.sql | findstr /L /C:"OVER() AS TotalRecords" >nul
if errorlevel 1 (
    echo SMOKE TEST FAILED - A059 TotalRecords window count missing from the bundle.
    exit /b 1
)

REM Prove the bundled pglast actually works. "NOT CHECKED" means the compiled
REM extension did not make it into the bundle.
dist\MsSqlToPgCheck.exe tests\fixtures\merge.sql | findstr /C:"PostgreSQL syntax: VALID" >nul
if errorlevel 1 (
    echo SMOKE TEST FAILED - pglast is missing from the bundle ^(syntax NOT CHECKED^).
    exit /b 1
)

del /q dist\MsSqlToPgCheck.exe >nul 2>&1

REM A024: the boolean column list is read from next to the exe. Ship the
REM current list (and the query that regenerates it) beside the build.
copy /y boolean_columns.txt dist\ >nul
copy /y boolean_columns_query.sql dist\ >nul
REM A054 (v2.16.0): the column types, and the query that regenerates them.
copy /y column_types.txt dist\ >nul
copy /y column_types_query.sql dist\ >nul

echo.
echo BUILD OK  ^-^>  dist\MsSqlToPg.exe
endlocal
