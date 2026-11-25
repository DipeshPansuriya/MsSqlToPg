"""Conversion dispatch: run the engine, then validate its output.

Single engine by design. The hosted ChartDB LLM engine was removed on
2026-07-31 because it silently truncated large queries (31 of 60 CTEs returned
on a 15 KB input, HTTP 200, no error) and sent query text to a third party.
SQLGlot is local, deterministic, has no output-size ceiling, and its output is
now checked against the real PostgreSQL grammar.

The app makes NO network calls. Keep it that way.

Knows nothing about Tkinter -- the UI supplies a callback and does its own
thread marshalling.
"""

from __future__ import annotations

import threading
from typing import Callable

from core import autofix, pgfixes, resolvability, validator, typefix, schema_hints, totalrecords
from core.models import ConversionResult, Severity
from engines import sqlglot_engine

ResultCallback = Callable[[ConversionResult], None]


# A013: T-SQL table locking hints. SQLGlot parses WITH (NOLOCK) after a bare
# table but NOT after `Table Alias` inside UPDATE ... FROM joins -- one such
# hint makes the whole batch unparseable. The hints are locking directives with
# no PostgreSQL meaning and are dropped in every conversion anyway, so they are
# stripped BEFORE parsing and the removal is disclosed. Keyword list is closed:
# these words are near-impossible inside a legitimate string literal.
_TABLE_HINT_RE = __import__("re").compile(
    r"\bWITH\s*\(\s*(?:NOLOCK|READUNCOMMITTED|UPDLOCK|HOLDLOCK|ROWLOCK|"
    r"READPAST|TABLOCK|TABLOCKX|XLOCK|PAGLOCK|NOWAIT|READCOMMITTED|"
    r"READCOMMITTEDLOCK|REPEATABLEREAD|SERIALIZABLE|FORCESEEK|FORCESCAN|NOEXPAND|"
    r"INDEX\s*\(\s*[\w\s,\[\]]+\)|INDEX\s*=\s*\[?\w+\]?)"
    r"(?:\s*,\s*(?:NOLOCK|READUNCOMMITTED|UPDLOCK|HOLDLOCK|ROWLOCK|"
    r"READPAST|TABLOCK|TABLOCKX|XLOCK|PAGLOCK|NOWAIT|READCOMMITTED|"
    r"READCOMMITTEDLOCK|REPEATABLEREAD|SERIALIZABLE|FORCESEEK|FORCESCAN|NOEXPAND|"
    r"INDEX\s*\(\s*[\w\s,\[\]]+\)|INDEX\s*=\s*\[?\w+\]?))*\s*\)",
    __import__("re").IGNORECASE)


# A016 -- every shape the ACS splitter (PostgreSQLUtility.SplitStatements) can
# tear. It cuts at ANY line whose first token is one of
# SELECT/INSERT/UPDATE/DELETE/DROP/CREATE/ALTER/IF/SET/DECLARE and appends
# "\n ;" to the piece before it, so a statement whose CONTINUATION begins such
# a line is split in half -> 42601. A piece already ending in '(', 'union' or
# ';' is safe, which is why only these four shapes need gluing.
_SPLIT_GLUE_RES = [
    # UPDATE t / SET c = 1
    __import__("re").compile(
        r"(?im)^([ \t]*UPDATE\b[^\n;]*?)[ \t]*\n[ \t]*(SET\b)"),
    # INSERT INTO t (cols) / SELECT ...
    __import__("re").compile(
        r"(?im)^([ \t]*INSERT\s+INTO\s+[\w.\"]+\s*\([^()\n]*\))[ \t]*\n[ \t]*(SELECT\b)"),
    # CREATE [TEMP] TABLE x AS / SELECT ...
    __import__("re").compile(
        r"(?im)^([ \t]*CREATE\s+(?:TEMP\s+|TEMPORARY\s+|UNLOGGED\s+)?TABLE\s+"
        r"[\w.\"]+\s+AS)[ \t]*\n[ \t]*(SELECT\b)"),
    # a CTE's closing ')' / SELECT -- tearing here orphans the whole WITH
    # clause. Guarded on the ')' line carrying no ';', which is what tells a
    # CTE close apart from the end of the previous statement.
    __import__("re").compile(
        r"(?m)^([ \t]*\))[ \t]*\n[ \t]*(SELECT\b)"),
]

# L016 fallback: a dangling `; RETURNING` also makes SQLGlot fail the whole
# parse, and the linter only runs on successful conversions -- the defect
# would otherwise hide behind SG-PARSE. (On successful conversions the linter
# raises L016 itself; the two paths are mutually exclusive.)
_RETURNING_SPLIT_RE = __import__("re").compile(
    r";\s*RETURNING\b", __import__("re").IGNORECASE)


def _tidy_whitespace(sql: str) -> str:
    """Collapse runs of spaces/tabs OUTSIDE single-quoted literals to one
    space and strip trailing whitespace -- without touching indentation at
    the start of a line or anything inside a string literal."""
    out: list[str] = []
    i, n = 0, len(sql)
    in_string = False
    at_line_start = True
    while i < n:
        ch = sql[i]
        if in_string:
            out.append(ch)
            if ch == "'":
                if i + 1 < n and sql[i + 1] == "'":
                    out.append("'")
                    i += 2
                    continue
                in_string = False
            i += 1
            continue
        if ch == "'":
            in_string = True
            out.append(ch)
            i += 1
            continue
        if ch == "\n":
            # drop any spaces/tabs we just emitted before the newline
            while out and out[-1] in " \t":
                out.pop()
            out.append(ch)
            at_line_start = True
            i += 1
            continue
        if ch in " \t":
            j = i
            while j < n and sql[j] in " \t":
                j += 1
            if at_line_start:
                out.append(sql[i:j])      # indentation is preserved verbatim
            else:
                out.append(" ")           # interior runs collapse to one space
            i = j
            continue
        out.append(ch)
        at_line_start = False
        i += 1
    return "".join(out)


def convert(sql: str) -> ConversionResult:
    """Convert T-SQL to PostgreSQL and validate the result. Never raises."""
    # Kept verbatim for the source-side resolvability rules at the end: they must
    # see the ORIGINAL T-SQL, because sqlglot returns an empty string for input
    # it cannot represent (notably procedural control flow).
    original_sql = sql
    sql, hint_count = _TABLE_HINT_RE.subn(" ", sql)

    # A023 must run BEFORE the parse: sqlglot has no node for `FOR XML PATH`,
    # reports "Unsupported query option" and DROPS it, which would turn a
    # scalar string-agg subquery into a multi-row subquery.
    comment_params = autofix.find_params_in_comments(sql)
    for_json_count = autofix.find_for_json(sql)
    sql, for_xml_count = autofix.rewrite_for_xml_path(sql)

    # v2.13.0 text pre-pass (core/pgfixes): constructs sqlglot cannot parse
    # (welded aliases, table variables, DELETE ... OUTPUT), drops (IF OBJECT_ID
    # temp guards, SET NOCOUNT) or maps to fabricated forms (CONVERT styles,
    # FORMAT masks, OPENJSON). Placeholders it plants are expanded after
    # generation.
    sql, ctx = pgfixes.pre_parse(sql)
    preprocessed = sql

    try:
        result = sqlglot_engine.convert(sql, ctx)
    except Exception as exc:  # an engine must never take down the app
        return ConversionResult().fail(
            "ENGINE-CRASH", f"{type(exc).__name__}: {exc}")

    for fix in ctx.applied:
        result.add(fix.code, Severity.WARNING, f"AUTO-CORRECTED: {fix.note}")

    if not result.ok and _RETURNING_SPLIT_RE.search(sql):
        result.add(
            "L016", Severity.ERROR,
            "RETURNING is cut off from its INSERT by a ';' -- PostgreSQL "
            "raises 42601. Remove the semicolon so RETURNING stays part of "
            "the INSERT statement (check for a trailing ';' in the base "
            "query when C# appends RETURNING).")

    if for_xml_count:
        result.add(
            "A023", Severity.INFO,
            f"{for_xml_count} `STUFF(( ... FOR XML PATH('')),1,n,'')` "
            f"string-join idiom(s) rewritten to STRING_AGG before parsing. "
            f"sqlglot drops the FOR XML option, which would leave a multi-row "
            f"subquery where a scalar was meant. The original STUFF length "
            f"argument was usually 1 against a two-character separator, so "
            f"T-SQL left a leading space on every value; STRING_AGG does not.")

    if for_json_count:
        result.add(
            "L019", Severity.ERROR,
            f"{for_json_count} `FOR JSON PATH/AUTO` clause(s) -- NOT "
            f"converted. Dotted aliases (x AS [Root.Header.Id]) build a "
            f"NESTED document, JSON_QUERY embeds raw JSON and "
            f"WITHOUT_ARRAY_WRAPPER collapses the array; the PostgreSQL "
            f"equivalent is a hand-built json_build_object tree whose shape "
            f"depends on the alias hierarchy. Convert by hand and wrap the "
            f"document in json_strip_nulls() to keep FOR JSON's "
            f"drop-null-keys behaviour. See CONVERSION_RULES.md.")

    if comment_params:
        names = ", ".join(sorted(set(comment_params)))
        result.add(
            "L020", Severity.WARNING,
            f"Parameter name(s) mentioned inside a `--` comment: {names}. "
            f"ACS's QueryParameterProcessor.ReplaceQueryParameters is a raw "
            f"text substitution and strips the quotes from the '@Param' form "
            f"wherever it appears, comments included. Npgsql skips comments so "
            f"a BOUND name survives, but an UNBOUND one is a live hazard -- "
            f"write the name without the '@'.")

    if hint_count:
        result.add(
            "A013", Severity.INFO,
            f"{hint_count} T-SQL table hint(s) (WITH (NOLOCK) etc.) removed "
            f"before parsing -- locking hints have no PostgreSQL equivalent.")

    if result.ok and result.sql.strip():
        # Expand the pre-pass placeholders and add temp-table DROP guards
        # BEFORE the splitter glue and validation, so both see the final text.
        count_before = len(ctx.applied)
        result.sql = pgfixes.post_generate(result.sql, ctx)
        for fix in ctx.applied[count_before:]:
            result.add(fix.code, Severity.WARNING, f"AUTO-CORRECTED: {fix.note}")

        # A054 (v2.16.0): SQL Server's silent conversions (int = text, LIKE on a
        # number, '' as 0 / 1900-01-01, text into fnGetLocalTime) made explicit
        # from column_types.txt. Surgical text edits; untouched text is kept.
        result.sql, typed = typefix.apply(result.sql)
        if typed:
            result.add(
                "A054", Severity.WARNING,
                f"AUTO-CORRECTED: {typed} implicit conversion(s) SQL Server "
                f"applied silently made explicit from the schema "
                f"({schema_hints.types_source() or 'no column_types.txt'}): "
                f"a text side compared with a number is CAST to that number "
                f"(NULLIF(TRIM(x), '')), a number literal against a text column "
                f"is quoted, LIKE on a number/date compares its text, '' means "
                f"0 / 1900-01-01, text passed to fnGetLocalTime/TO_CHAR is CAST "
                f"to TIMESTAMP. PostgreSQL raises 42883/22P02 on the original.")

        # A061 (v2.18.0): a Dapper parameter compared with a typed column is cast to that type --
        # Npgsql binds the C# type (text / integer), SQL Server converted it silently.
        result.sql, cast = typefix.cast_params(result.sql)
        if cast:
            result.add(
                "A061", Severity.WARNING,
                f"AUTO-CORRECTED: {cast} Dapper parameter(s) cast to the type of the column or "
                f"value they are compared with ({schema_hints.types_source() or 'no column_types.txt'}). "
                f"SQL Server converted `int_col = '@x'` / `varchar_col = @x` whatever type C# "
                f"passed; Npgsql binds the .NET type and PostgreSQL raises 42883 (integer = text). "
                f"Numbers, dates and timestamps go through CAST(NULLIF(TRIM(CAST(@x AS TEXT)), '') "
                f"AS type), so an int, a numeric string, a DateTime or NULL all work and '' matches "
                f"nothing; text columns get CAST(@x AS TEXT), which compares as text (SQL Server "
                f"compared varchar = int numerically: '007' = 7).")

    result.warnings.extend(ctx.warnings)

    if result.ok and result.sql.strip():
        # A016: ACS's runtime splitter (PostgreSQLUtility.SplitStatements) is
        # not statement-aware -- a line-leading SET is treated as a NEW
        # statement and terminated with ';', producing 42601 near ';'. Keep
        # UPDATE and SET on one line so the splitter cannot cut between them.
        glue_count = 0
        for glue_re in _SPLIT_GLUE_RES:
            # Re-run each pattern until it stops matching: gluing one pair can
            # expose another (a CTE chain closes several ')' in sequence).
            while True:
                result.sql, n = glue_re.subn(r"\1 \2", result.sql)
                if not n:
                    break
                glue_count += n
        if glue_count:
            result.add(
                "A016", Severity.INFO,
                f"{glue_count} statement continuation(s) re-joined onto one "
                f"line (UPDATE/SET, INSERT INTO(cols)/SELECT, CREATE TABLE "
                f"AS/SELECT, CTE close/SELECT). The ACS statement splitter "
                f"treats a line-leading keyword as a new statement and "
                f"appends ';' to the piece before it, tearing the statement "
                f"in half and raising 42601.")
        # A059 / A060: the same splitter feeds ACS's filterString pager, which
        # wraps the piece that mentions TotalRecords -- and the pretty printer
        # starts every subquery's SELECT on a new line (core/totalrecords).
        result.sql, windowed = totalrecords.window_counts(result.sql)
        result.sql, glued = totalrecords.glue_nested_selects(result.sql)
        if windowed:
            result.add(
                "A059", Severity.WARNING,
                f"AUTO-CORRECTED: {windowed} `(SELECT COUNT(*) FROM t) AS "
                f"TotalRecords` subquery(ies) over the statement's own table "
                f"rewritten to `COUNT(*) OVER() AS TotalRecords`. ACS's filterString "
                f"pager cuts at every line-leading SELECT and wraps the piece that "
                f"mentions TotalRecords; the subquery's SELECT started its own "
                f"line, so the pager wrapped the subquery and PostgreSQL raised "
                f"42601 near FROM. Same value on every row (the statement reads "
                f"the whole table once). Under a grid filter the window counts the "
                f"FILTERED rows, so the pager total now matches the rows shown.")
        if glued:
            result.add(
                "A060", Severity.INFO,
                f"{glued} nested SELECT(s) in a TotalRecords/TotalCount statement "
                f"kept on the line of their '(' -- ACS's filterString pager cuts "
                f"at a line-leading SELECT and would wrap the subquery instead of "
                f"the statement.")
        # No extra whitespace may be introduced by conversion: collapse
        # multi-space runs outside string literals, drop trailing spaces.
        result.sql = _tidy_whitespace(result.sql)

    if result.ok and result.sql.strip():
        # Checked against the REAL PostgreSQL grammar (libpg_query), not a
        # heuristic. An INVALID verdict makes the result unusable.
        result.validation = validator.validate(result.sql)
        for message in result.validation.errors:
            severity = (Severity.ERROR if not result.validation.valid
                        else Severity.INFO)
            result.add("PG-SYNTAX", severity, message)

        # Grammar is not enough. OPENJSON(x) PARSES -- it is a legal call shape
        # -- and dies at execution with 42883. Two converted constants shipped
        # exactly that way. The resolvability pass is what catches functions
        # that cannot resolve, procedural control flow, and the constructs that
        # must never be auto-converted (3-arg CONVERT style codes, TOP, bit).
        result.warnings.extend(resolvability.check(result.sql))

        # R007: the shared T-SQL marker set (core/markers) over the OUTPUT --
        # the campaign scanner found CURSOR, @@ROWCOUNT, OUTPUT INSERTED,
        # SET NOCOUNT, DATETIME2 ... in constants this tool had passed.
        result.warnings.extend(pgfixes.residue(result.sql))

    # v2.13.0 lint: needs the schema or the C# caller to decide (L021-L027,
    # R008). Runs on the source too, so it reports even when output is empty.
    result.warnings.extend(pgfixes.lint(preprocessed, result.sql if result.ok else ""))

    # Source-side rules run REGARDLESS of whether the engine produced output.
    # sqlglot cannot represent T-SQL control flow: given `IF EXISTS (...) SELECT`
    # it returns an EMPTY string with ok=True, so an output-only check sees
    # nothing and R002/R003 would never fire on the very constants that need
    # them most. See resolvability.check_source(). It reads the PRE-PROCESSED
    # text: constructs the pre-pass already converted (IF OBJECT_ID temp
    # guards, inlined DECLAREs, STUFF/FOR XML, CONVERT styles) are not
    # reported as if they were still there.
    result.warnings.extend(resolvability.check_source(preprocessed))
    result.warnings[:] = list(dict.fromkeys(result.warnings))
    return result


def convert_async(sql: str, on_result: ResultCallback,
                  on_done: Callable[[], None] | None = None) -> None:
    """Convert on a worker thread.

    `on_result` is invoked from that thread. Callers touching UI widgets must
    marshal back to the UI thread themselves.
    """
    def _worker() -> None:
        try:
            on_result(convert(sql))
        finally:
            if on_done is not None:
                on_done()

    threading.Thread(target=_worker, daemon=True).start()
