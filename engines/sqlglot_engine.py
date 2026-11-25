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
from sqlglot import expressions as exp
from sqlglot.errors import ErrorLevel, ParseError, UnsupportedError

from core import autofix, linter, pgfixes
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


def _unparsed(trees) -> bool:
    """True when the parse 'succeeded' only by falling back to a passthrough
    node -- a Command echo or a bare condition is not a parsed statement."""
    return any(isinstance(t, (exp.Command, exp.Condition))
               and not isinstance(t, exp.Query)
               for t in trees if t is not None)


def _reparse_with_terminators(sql: str):
    """A018: retry an unparseable batch with inferred `;` terminators.

    Returns (fixed_sql, expressions, note) or None. The retry is accepted only
    when it parses into REAL statements -- no ParseError, no Command fallback --
    so a wrong inference can never make things worse than the refusal it
    started from.
    """
    fixed, count = autofix.infer_statement_terminators(sql)
    if not count:
        return None
    try:
        retry = sqlglot.parse(fixed, read=READ_DIALECT)
    except Exception:
        return None
    if not retry or _unparsed(retry):
        return None
    return fixed, retry, autofix.terminator_note(count)


_COMMENT_ONLY_RE = re.compile(r"\s*(?:/\*.*?\*/\s*)+", re.DOTALL)


def _attach_comment_only(statements: list[str]) -> list[str]:
    """sqlglot returns a comment that follows a `;` (`SELECT a FROM t; -- note`)
    as a statement of its own, which came out as an empty `/* note */;`
    statement between the real ones. Keep the comment, on the statement before
    it (or the one after, at the very start); comment-only input is untouched."""
    if not any(s.strip() and not _COMMENT_ONLY_RE.fullmatch(s) for s in statements):
        return statements
    merged: list[str] = []
    leading: list[str] = []
    for s in statements:
        if not s.strip():
            continue
        if _COMMENT_ONLY_RE.fullmatch(s):
            if merged:
                merged[-1] = merged[-1].rstrip().rstrip(";") + "\n" + s.strip()
            else:
                leading.append(s.strip())
            continue
        if leading:
            s = "\n".join(leading) + "\n" + s
            leading = []
        merged.append(s)
    return merged


def convert(sql: str, ctx=None) -> ConversionResult:
    """Transpile T-SQL to PostgreSQL. Never raises.

    `ctx` is the core.pgfixes.Context from the text pre-pass (placeholders,
    FLOAT/REAL evidence); None when called directly."""
    started = time.perf_counter()
    result = ConversionResult()

    if not sql.strip():
        return result.fail("SG-EMPTY", "No input SQL provided.")

    # ---- Parse once, keep the AST for L010 -------------------------------
    pre_applied: list[autofix.Applied] = []
    try:
        expressions = sqlglot.parse(sql, read=READ_DIALECT)
    except ParseError as exc:
        rescue = _reparse_with_terminators(sql)
        if rescue is None:
            # Genuinely unbalanced syntax. Pass 2 would fail identically.
            result.duration_ms = int((time.perf_counter() - started) * 1000)
            return result.fail(
                "SG-PARSE", f"Could not parse the input SQL: {exc}")
        sql, expressions, note = rescue
        pre_applied.append(note)
    except Exception as exc:  # defensive: engines never raise
        result.duration_ms = int((time.perf_counter() - started) * 1000)
        return result.fail("SG-PARSE", f"Unexpected parse failure: {exc}")
    else:
        if _unparsed(expressions):
            # Parsed only via Command/passthrough fallback -- often a batch
            # with missing `;` terminators. Take the retry only if it is
            # strictly better (everything parses as a real statement).
            rescue = _reparse_with_terminators(sql)
            if rescue is not None:
                sql, expressions, note = rescue
                pre_applied.append(note)

    # ---- Auto-fixes --------------------------------------------------------
    # Correct unambiguous defects on the AST before generating PostgreSQL.
    # Every fix is disclosed below -- nothing is changed silently. The v2.13.0
    # rules (core/pgfixes) bracket the original set: A025 must see GETUTCDATE()
    # before A006 rewrites it, and A040 replaces top-level statements in the
    # list itself.
    applied = pgfixes.apply_ast_pre(expressions, sql)
    applied += autofix.apply_all(expressions, sql)
    applied += pgfixes.apply_ast(expressions, sql, ctx)
    try:
        ast_notes = pgfixes.fix_scalar_subquery_order(
            [t for t in expressions if t is not None], sql)
    except Exception:
        ast_notes = []

    # ---- Pass 1: detection -----------------------------------------------
    # UnsupportedError is a GENERATION failure -- partial output does exist, so
    # we record what was lost and continue to pass 2 to retrieve it. Run over
    # the FIXED trees: a construct an auto-fix already rewrote (PIVOT, A042)
    # must not be reported as dropped.
    unsupported: list[str] = []
    for tree in expressions:
        if tree is None:
            continue
        try:
            tree.sql(dialect=WRITE_DIALECT, unsupported_level=ErrorLevel.RAISE)
        except UnsupportedError as exc:
            unsupported += [line.strip() for line in str(exc).split("\n") if line.strip()]
        except Exception:
            # Anything else surfaces properly in pass 2; do not mask it here.
            pass

    # ---- Pass 2: output ---------------------------------------------------

    collector = _WarningCollector()
    sqlglot_logger = logging.getLogger("sqlglot")
    sqlglot_logger.addHandler(collector)
    try:
        # Generate from the (possibly retyped) AST rather than re-transpiling
        # the text, so the retype above actually reaches the output.
        statements = [t.sql(dialect=WRITE_DIALECT, pretty=True)
                      for t in expressions if t is not None]
        statements = _attach_comment_only(statements)
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
    for fix in (*pre_applied, *applied):
        result.add(fix.code, Severity.WARNING, f"AUTO-CORRECTED: {fix.note}")

    for message in unsupported:
        result.add("SG-UNSUPPORTED", Severity.ERROR,
                   f"{message} -- this construct was DROPPED from the output.")

    for message in collector.messages:
        # Deduplicate against the pass-1 findings.
        if not any(message.strip() in u for u in unsupported):
            result.add("SG-LOG", Severity.WARNING, message)

    linter.apply(result, sql, expressions=expressions)
    result.warnings.extend(ast_notes)
    result.duration_ms = int((time.perf_counter() - started) * 1000)
    return result
