"""SQLGlot engine -- local AST transpiler.

Fast (~100 ms on a 15 KB query), offline, deterministic, and with no output-size
ceiling. Excellent on SELECT/DML/DDL.

THE CRITICAL BEHAVIOUR: SQLGlot's default ErrorLevel is WARN. It writes to Python
`logging` and returns PARTIAL OUTPUT WITH NO EXCEPTION. In a GUI those log lines
are invisible, so the user receives confidently-wrong SQL.

Mitigation is a two-pass invocation:
  Pass 1 (detection) -- unsupported_level=RAISE, catch UnsupportedError.
  Pass 2 (output)    -- default level, with a log handler attached.

Rule enforced by the UI: never render SQLGlot output without its warnings.
"""

from __future__ import annotations

import logging
import re
import time

import sqlglot
from sqlglot.errors import ErrorLevel, ParseError, UnsupportedError

from core import autofix, linter
from core.models import ConversionResult, Severity

READ_DIALECT = "tsql"
WRITE_DIALECT = "postgres"


def restore_dapper_parameters(original: str, converted: str) -> str:
    """Undo SQLGlot's `@Name` -> `$Name` rewrite for Dapper parameters.

    SQLGlot renders every T-SQL `@Name` as `$Name`. That is invalid PostgreSQL
    (which uses `$1`/`$2` positionally, never `$Name`) AND it breaks Dapper.

    Npgsql accepts `@Name` placeholders natively, so a Dapper parameter needs NO
    conversion at all -- it must survive byte-for-byte.

    Only names that were genuinely DECLAREd (or are procedure parameters) are left
    as `$Name`, so L004 still reports them: those are real local variables that
    need a DO $$ block, not placeholders.
    """
    declared = linter.declared_variables(original)

    def _replace(match: "re.Match[str]") -> str:
        name = match.group(1)
        if f"@{name}".lower() in declared:
            return match.group(0)   # genuine local -- leave broken so L004 flags it
        return f"@{name}"           # Dapper parameter -- restore verbatim

    # (?<!\$) keeps $$ dollar-quoted blocks intact.
    return re.sub(r"(?<!\$)\$([A-Za-z_]\w*)", _replace, converted)


class _WarningCollector(logging.Handler):
    """Captures anything sqlglot logs during a single transpile call."""

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


def convert(sql: str) -> ConversionResult:
    """Transpile T-SQL to PostgreSQL. Never raises."""
    started = time.perf_counter()
    result = ConversionResult()

    if not sql.strip():
        return result.fail("SG-EMPTY", "No input SQL provided.")

    # ---- Parse once, keep the AST for L010 -------------------------------
    try:
        expressions = sqlglot.parse(sql, read=READ_DIALECT)
    except ParseError as exc:
        # Genuinely unbalanced syntax. Pass 2 would fail identically, so stop.
        result.duration_ms = int((time.perf_counter() - started) * 1000)
        return result.fail("SG-PARSE", f"Could not parse the input SQL: {exc}")
    except Exception as exc:  # defensive: engines never raise
        result.duration_ms = int((time.perf_counter() - started) * 1000)
        return result.fail("SG-PARSE", f"Unexpected parse failure: {exc}")

    # ---- Pass 1: detection -----------------------------------------------
    # UnsupportedError is a GENERATION failure -- partial output does exist, so
    # we record what was lost and continue to pass 2 to retrieve it.
    unsupported: list[str] = []
    try:
        sqlglot.transpile(sql, read=READ_DIALECT, write=WRITE_DIALECT,
                          unsupported_level=ErrorLevel.RAISE)
    except UnsupportedError as exc:
        unsupported = [line.strip() for line in str(exc).split("\n") if line.strip()]
    except Exception:
        # Anything else surfaces properly in pass 2; do not mask it here.
        pass

    # ---- Pass 2: output ---------------------------------------------------
    # Correct unambiguous defects on the AST before generating PostgreSQL.
    # Every fix is disclosed below -- nothing is changed silently.
    applied = autofix.apply_all(expressions, sql)

    collector = _WarningCollector()
    sqlglot_logger = logging.getLogger("sqlglot")
    sqlglot_logger.addHandler(collector)
    try:
        # Generate from the (possibly retyped) AST rather than re-transpiling
        # the text, so the retype above actually reaches the output.
        statements = [t.sql(dialect=WRITE_DIALECT, pretty=True)
                      for t in expressions if t is not None]
        result.sql = ";\n\n".join(s.rstrip(";") for s in statements if s.strip())
        if result.sql:
            result.sql += ";"
        result.sql = restore_dapper_parameters(sql, result.sql)
    except Exception as exc:
        result.duration_ms = int((time.perf_counter() - started) * 1000)
        return result.fail("SG-GENERATE", f"Could not generate PostgreSQL: {exc}")
    finally:
        sqlglot_logger.removeHandler(collector)

    # ---- Warnings ---------------------------------------------------------
    for fix in applied:
        result.add(fix.code, Severity.WARNING, f"AUTO-CORRECTED: {fix.note}")

    for message in unsupported:
        result.add("SG-UNSUPPORTED", Severity.ERROR,
                   f"{message} -- this construct was DROPPED from the output.")

    for message in collector.messages:
        # Deduplicate against the pass-1 findings.
        if not any(message.strip() in u for u in unsupported):
            result.add("SG-LOG", Severity.WARNING, message)

    linter.apply(result, sql, expressions=expressions)
    result.duration_ms = int((time.perf_counter() - started) * 1000)
    return result
