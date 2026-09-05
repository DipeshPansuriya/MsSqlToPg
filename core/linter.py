"""Post-conversion defect detection.

Every rule compares the ORIGINAL T-SQL against the CONVERTED PostgreSQL, so we can
detect disappearance (L007 dropped EXEC, L009 mass statement loss) and not merely
bad output.

Rules L001-L009 work on text. L010 works on the parsed AST and therefore runs for
SQLGlot output only -- it catches input SQLGlot silently failed to understand,
which no regex over the output string could find.
"""

from __future__ import annotations

import re
from typing import Callable

from core.models import ConversionResult, Severity, Warning

# --------------------------------------------------------------------------
# Literal masking
# --------------------------------------------------------------------------
# Replace the CONTENTS of string literals but keep them recognisable as strings.
# Not full removal: L001 must still see that an operand of `+` is a string, while
# L008 must not fire on the word GETDATE appearing inside a quoted string.
_LITERAL_RE = re.compile(r"'(?:[^']|'')*'")
_MASK = "'<STR>'"

# PostgreSQL dollar-quoted blocks ($$ ... $$) are code, not string literals --
# masking them would hide real defects, so they are left intact.
_DOLLAR_BLOCK_RE = re.compile(r"\$\$.*?\$\$", re.DOTALL)


def mask_literals(sql: str) -> str:
    """Blank out string-literal contents, preserving quote markers."""
    placeholders: list[str] = []

    def _stash(match: re.Match[str]) -> str:
        placeholders.append(match.group(0))
        return f"\x00{len(placeholders) - 1}\x00"

    protected = _DOLLAR_BLOCK_RE.sub(_stash, sql)
    masked = _LITERAL_RE.sub(_MASK, protected)

    def _restore(match: re.Match[str]) -> str:
        return placeholders[int(match.group(1))]

    return re.sub(r"\x00(\d+)\x00", _restore, masked)


def _line_of(haystack: str, index: int) -> int:
    return haystack.count("\n", 0, index) + 1


# --------------------------------------------------------------------------
# Rules
# --------------------------------------------------------------------------
# Each rule takes (original_tsql, converted_sql, masked_converted) and returns
# a list of Warning. Adding a rule = adding a function + a test, nothing else.

RuleFn = Callable[[str, str, str], list[Warning]]


def l001_string_concat(original: str, converted: str, masked: str) -> list[Warning]:
    """`+` used with a string operand. PostgreSQL requires `||` for text."""
    out: list[Warning] = []
    for m in re.finditer(r"'<STR>'\s*\+|\+\s*'<STR>'", masked):
        out.append(Warning(
            "L001", Severity.ERROR,
            "String concatenation uses '+'. PostgreSQL requires '||' for text.",
            _line_of(masked, m.start()),
        ))
    return out


def l002_varchar_max(original: str, converted: str, masked: str) -> list[Warning]:
    """VARCHAR(MAX) / NVARCHAR(MAX) survived. PostgreSQL has no MAX length."""
    out: list[Warning] = []
    for m in re.finditer(r"\b N? VARCHAR \s* \(\s* MAX \s*\)", masked,
                         re.IGNORECASE | re.VERBOSE):
        out.append(Warning(
            "L002", Severity.ERROR,
            "VARCHAR(MAX) is not valid in PostgreSQL. Use TEXT.",
            _line_of(masked, m.start()),
        ))
    return out


def l003_bit_type(original: str, converted: str, masked: str) -> list[Warning]:
    """Bare BIT column type. In PostgreSQL BIT is a bit-string, not a boolean."""
    out: list[Warning] = []
    for m in re.finditer(r"(?<![\w.])BIT\b(?!\s*(?:VARYING|\())", masked,
                         re.IGNORECASE):
        out.append(Warning(
            "L003", Severity.WARNING,
            "BIT in PostgreSQL is a bit-string type, not a boolean. "
            "T-SQL BIT should map to BOOLEAN.",
            _line_of(masked, m.start()),
        ))
    return out


# `DECLARE @x INT` and a procedure's `@p INT` parameter list are LOCAL VARIABLES:
# they must become a DO $$ block or a function parameter.
# An @name that is NEITHER is a DAPPER PARAMETER placeholder -- and Npgsql accepts
# `@name` natively, so it must be preserved EXACTLY AS-IS. Same syntax, opposite
# correct outcomes; the source query is the only way to tell them apart.
_DECLARED_VAR_RE = re.compile(r"\bDECLARE\s+(@\w+)", re.IGNORECASE)
_PROC_SIGNATURE_RE = re.compile(
    r"\bCREATE\s+(?:OR\s+ALTER\s+)?PROC(?:EDURE)?\s+[\w.\[\]]+(.*?)\bAS\b",
    re.IGNORECASE | re.DOTALL)


def declared_variables(original: str) -> set[str]:
    """Lower-cased @names that are genuine T-SQL locals, not Dapper parameters."""
    names = {m.group(1).lower() for m in _DECLARED_VAR_RE.finditer(original)}
    signature = _PROC_SIGNATURE_RE.search(original)
    if signature:
        names |= {v.lower() for v in re.findall(r"@\w+", signature.group(1))}
    return names


def l004_surviving_variables(original: str, converted: str, masked: str) -> list[Warning]:
    """T-SQL locals, or SQLGlot's invalid $var rendering, survived into output.

    Dapper parameters are deliberately NOT flagged -- `@Name` is valid Npgsql.
    """
    declared = declared_variables(original)
    out: list[Warning] = []

    for m in re.finditer(r"@@?[A-Za-z_]\w*", masked):
        token = m.group(0)
        if token.startswith("@@"):
            out.append(Warning(
                "L004", Severity.ERROR,
                f"T-SQL system variable '{token}' has no PostgreSQL equivalent.",
                _line_of(masked, m.start()),
            ))
        elif token.lower() in declared:
            out.append(Warning(
                "L004", Severity.ERROR,
                f"T-SQL local variable '{token}' survived. PostgreSQL needs a "
                "DECLARE block inside DO $$ ... $$ or a function parameter.",
                _line_of(masked, m.start()),
            ))
        # else: a Dapper/Npgsql parameter placeholder -- correct as-is, no warning.

    # SQLGlot renders @Name as $Name, which is invalid in PostgreSQL AND breaks
    # Dapper. $$ (dollar-quoting) and $1 (positional) are legitimate -- excluded.
    for m in re.finditer(r"(?<!\$)\$[A-Za-z_]\w*", masked):
        out.append(Warning(
            "L004", Severity.ERROR,
            f"'{m.group(0)}' is not a valid PostgreSQL parameter and will break "
            f"Dapper. Use '@{m.group(0)[1:]}' for a Dapper parameter, or a "
            f"DO $$ ... $$ block for a local variable.",
            _line_of(masked, m.start()),
        ))
    return out


def l005_temp_table_marker(original: str, converted: str, masked: str) -> list[Warning]:
    """`#temp` names surviving. Runs on UNMASKED text -- the point is to find
    them inside string literals, e.g. in dynamic SQL that was not rewritten."""
    out: list[Warning] = []
    for m in re.finditer(r"#{1,2}[A-Za-z_]\w*", converted):
        out.append(Warning(
            "L005", Severity.WARNING,
            f"T-SQL temp-table name '{m.group(0)}' survived. PostgreSQL "
            "temporary tables have no '#' prefix.",
            _line_of(converted, m.start()),
        ))
    return out


def l006_try_cast_lost(original: str, converted: str, masked: str) -> list[Warning]:
    """TRY_CAST/TRY_CONVERT collapsed to CAST -- raises instead of returning NULL."""
    had_try = re.search(r"\bTRY_(CAST|CONVERT|PARSE)\b", original, re.IGNORECASE)
    still_has = re.search(r"\bTRY_(CAST|CONVERT|PARSE)\b", masked, re.IGNORECASE)
    if had_try and not still_has:
        return [Warning(
            "L006", Severity.WARNING,
            "TRY_CAST/TRY_CONVERT was converted to a plain CAST. It will now "
            "raise on bad input instead of returning NULL.",
        )]
    return []


def l007_exec_dropped(original: str, converted: str, masked: str) -> list[Warning]:
    """Input executed dynamic SQL but the output has no execution statement."""
    had_exec = re.search(r"\bEXEC(UTE)?\s*[\(@]", original, re.IGNORECASE)
    still_has = re.search(r"\bEXECUTE\b", masked, re.IGNORECASE)
    if had_exec and not still_has:
        return [Warning(
            "L007", Severity.ERROR,
            "Input contained EXEC/EXECUTE but the output has no execution "
            "statement -- dynamic SQL was dropped.",
        )]
    return []


_TSQL_BUILTINS = [
    "GETDATE", "GETUTCDATE", "ISNULL", "CHARINDEX", "NEWID", "IIF",
    "SCOPE_IDENTITY", "DATEPART", "SYSDATETIME", "STUFF", "PATINDEX",
]


def l008_unconverted_builtins(original: str, converted: str, masked: str) -> list[Warning]:
    """A T-SQL builtin with no PostgreSQL equivalent survived unconverted."""
    out: list[Warning] = []
    for fn in _TSQL_BUILTINS:
        for m in re.finditer(rf"\b{fn}\s*\(", masked, re.IGNORECASE):
            out.append(Warning(
                "L008", Severity.WARNING,
                f"T-SQL function {fn}() survived unconverted.",
                _line_of(masked, m.start()),
            ))
    # LEN() -> LENGTH() in PostgreSQL; matched separately to avoid hitting LENGTH.
    for m in re.finditer(r"\bLEN\s*\(", masked, re.IGNORECASE):
        out.append(Warning(
            "L008", Severity.WARNING,
            "T-SQL LEN() survived. PostgreSQL uses LENGTH().",
            _line_of(masked, m.start()),
        ))
    # NOLOCK has no PostgreSQL equivalent and should have been removed.
    for m in re.finditer(r"\bNOLOCK\b", masked, re.IGNORECASE):
        out.append(Warning(
            "L008", Severity.WARNING,
            "WITH (NOLOCK) survived. PostgreSQL has no equivalent hint.",
            _line_of(masked, m.start()),
        ))
    return out


# Below this input size the length ratio is too noisy to be meaningful --
# `SELECT TOP 1 *` -> `SELECT * ... LIMIT 1` legitimately changes length a lot.
L009_MIN_INPUT_CHARS = 200
L009_MIN_RATIO = 0.40


def l009_mass_loss(original: str, converted: str, masked: str) -> list[Warning]:
    """Output drastically shorter than input -- statements probably vanished.

    The safety net: even when no specific rule matches, a collapse in size means
    content was lost.
    """
    src = original.strip()
    if len(src) < L009_MIN_INPUT_CHARS:
        return []
    if not converted.strip():
        return [Warning("L009", Severity.ERROR,
                        "Output is empty but input was not.")]
    ratio = len(converted.strip()) / len(src)
    if ratio < L009_MIN_RATIO:
        return [Warning(
            "L009", Severity.ERROR,
            f"Output is {ratio:.0%} the size of the input "
            f"(threshold {L009_MIN_RATIO:.0%}) -- statements were likely dropped.",
        )]
    return []


# --------------------------------------------------------------------------
# L011-L014 -- found on a real 58 KB dashboard query that converted "cleanly"
# and then failed on its first statement. Every one of these produces output
# the PostgreSQL parser ACCEPTS; only execution reveals them.
# --------------------------------------------------------------------------

def l011_tsql_timestamp_became_bytea(original: str, converted: str,
                                     masked: str) -> list[Warning]:
    """T-SQL TIMESTAMP is ROWVERSION -- an 8-byte BINARY, not a datetime.

    It converts, correctly, to BYTEA. The author almost always meant DATETIME2.
    Symptom at runtime: "cannot cast type date to bytea".
    """
    if not re.search(r"\bBYTEA\b", masked, re.IGNORECASE):
        return []
    if not re.search(r"\b(?:AS\s+)?TIMESTAMP\b", original, re.IGNORECASE):
        return []
    out: list[Warning] = []
    for m in re.finditer(r"\bBYTEA\b", masked, re.IGNORECASE):
        out.append(Warning(
            "L011", Severity.ERROR,
            "BYTEA came from T-SQL TIMESTAMP, which is ROWVERSION (binary), "
            "not a datetime. If a date/time was intended, change the SOURCE "
            "to DATETIME2 and reconvert.",
            _line_of(masked, m.start()),
        ))
    return out


# Windows timezone identifiers. PostgreSQL only knows IANA names
# ('Asia/Kolkata'), so these fail at runtime with "time zone not recognized".
_WINDOWS_TZ_RE = re.compile(
    r"'([A-Za-z][\w.+\- ]*(?:Standard|Daylight) Time)'")


def l012_windows_timezone(original: str, converted: str,
                          masked: str) -> list[Warning]:
    """Runs on UNMASKED text -- the defect lives INSIDE a string literal."""
    out: list[Warning] = []
    for m in re.finditer(_WINDOWS_TZ_RE, converted):
        out.append(Warning(
            "L012", Severity.ERROR,
            f"'{m.group(1)}' is a Windows timezone name. PostgreSQL uses IANA "
            f"names (e.g. 'Asia/Kolkata') and will fail at runtime.",
            _line_of(converted, m.start()),
        ))
    return out


def l013_cross_database_reference(original: str, converted: str,
                                  masked: str) -> list[Warning]:
    """`db.dbo.Table` -- PostgreSQL cannot query across databases."""
    out: list[Warning] = []
    seen: set[str] = set()
    for m in re.finditer(r'\b(\w+)\.(?:dbo|"dbo"|\[dbo\])\.', masked,
                         re.IGNORECASE):
        if m.group(1).lower() in seen:
            continue
        seen.add(m.group(1).lower())
        out.append(Warning(
            "L013", Severity.ERROR,
            f"'{m.group(0)}...' is a cross-database reference. PostgreSQL "
            f"cannot query across databases -- use schema.table within one "
            f"database, or a foreign-data wrapper.",
            _line_of(masked, m.start()),
        ))
    return out


# DD-MM-YYYY. PostgreSQL's default DateStyle is MDY, so '31-07-2026' errors
# outright and '05-07-2026' silently becomes 7 May instead of 5 July.
_DMY_RE = re.compile(r"'(\d{2})-(\d{2})-(\d{4})")


def l014_ambiguous_date_literal(original: str, converted: str,
                                masked: str) -> list[Warning]:
    """Runs on UNMASKED text -- the defect lives INSIDE a string literal."""
    out: list[Warning] = []
    for m in re.finditer(_DMY_RE, converted):
        first = int(m.group(1))
        detail = ("will error: there is no month "
                  f"{first}" if first > 12 else
                  "will SILENTLY parse as month-day and return wrong data")
        out.append(Warning(
            "L014", Severity.ERROR,
            f"Date literal '{m.group(1)}-{m.group(2)}-{m.group(3)}' is "
            f"DD-MM-YYYY. PostgreSQL's default DateStyle is MDY, so this "
            f"{detail}. Use ISO 'YYYY-MM-DD'.",
            _line_of(converted, m.start()),
        ))
    return out


ALL_RULES: list[RuleFn] = [
    l001_string_concat,
    l002_varchar_max,
    l003_bit_type,
    l004_surviving_variables,
    l005_temp_table_marker,
    l006_try_cast_lost,
    l007_exec_dropped,
    l008_unconverted_builtins,
    l009_mass_loss,
    l011_tsql_timestamp_became_bytea,
    l012_windows_timezone,
    l013_cross_database_reference,
    l014_ambiguous_date_literal,
]


def dedupe(warnings: list[Warning]) -> list[Warning]:
    """Collapse identical (code, message, line) warnings, preserving order.

    A single defect can match a rule's regex more than once -- e.g.
    `'a' + @v + 'b'` matches the L001 pattern on both sides of the same
    expression. Reporting it twice is noise, not information.
    """
    seen: set[tuple] = set()
    out: list[Warning] = []
    for warning in warnings:
        key = (warning.code, warning.message, warning.line)
        if key not in seen:
            seen.add(key)
            out.append(warning)
    return out


def lint(original: str, converted: str) -> list[Warning]:
    """Run every text rule. Returns [] when the output looks clean."""
    if not converted.strip():
        return l009_mass_loss(original, converted, "")
    masked = mask_literals(converted)
    out: list[Warning] = []
    for rule in ALL_RULES:
        out.extend(rule(original, converted, masked))
    return dedupe(out)


# --------------------------------------------------------------------------
# L010 -- AST-based, SQLGlot only
# --------------------------------------------------------------------------
# SQLGlot does not reject input it cannot understand. It either reinterprets it
# (`SELEKT * FRM` parses as a MULTIPLICATION expression, `SELEKT` x `FRM`) or wraps
# it in a passthrough `exp.Command` node and echoes it verbatim. Neither raises at
# any error_level. Only the AST reveals this.

def l010_unparsed_ast(expressions: list) -> list[Warning]:
    """Flag passthrough or non-statement top-level AST nodes.

    Discriminator: `exp.Condition` is sqlglot's base for VALUE-producing nodes
    (Mul, Column, Binary, Literal). A top-level node that is a Condition means the
    input was read as an expression rather than a statement -- i.e. it is not SQL.
    Every legitimate statement node (Select, Declare, Set, Create, Execute, Merge,
    IfBlock, Insert) is NOT a Condition, so this needs no maintained allowlist.
    """
    try:
        from sqlglot import expressions as exp
    except ImportError:  # pragma: no cover - sqlglot is a hard dependency
        return []

    out: list[Warning] = []
    for tree in expressions:
        if tree is None:
            continue
        if isinstance(tree, exp.Command):
            snippet = tree.sql()[:60]
            out.append(Warning(
                "L010", Severity.ERROR,
                f"SQLGlot could not parse this statement and passed it through "
                f"unchanged: {snippet!r}",
            ))
        elif isinstance(tree, exp.Condition):
            out.append(Warning(
                "L010", Severity.ERROR,
                f"Input was read as a value expression ({type(tree).__name__}), "
                f"not a SQL statement -- so nothing was actually converted. "
                f"Check for a typo in the source query.",
            ))
    return out


def apply(result: ConversionResult, original: str,
          expressions: list | None = None) -> ConversionResult:
    """Attach all applicable lint warnings to a result, in place."""
    if not result.ok:
        return result
    result.warnings.extend(lint(original, result.sql))
    if expressions is not None:
        result.warnings.extend(l010_unparsed_ast(expressions))
    result.warnings[:] = dedupe(result.warnings)
    return result
