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

REM Prove the bundled pglast actually works. "NOT CHECKED" means the compiled
REM extension did not make it into the bundle.
dist\MsSqlToPgCheck.exe tests\fixtures\merge.sql | findstr /C:"PostgreSQL syntax: VALID" >nul
if errorlevel 1 (
    echo SMOKE TEST FAILED - pglast is missing from the bundle ^(syntax NOT CHECKED^).
    exit /b 1
)

del /q dist\MsSqlToPgCheck.exe >nul 2>&1

echo.
echo BUILD OK  ^-^>  dist\MsSqlToPg.exe
endlocal
