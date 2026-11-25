"""Runtime-resolvability checks -- the layer between the linter and the validator.

WHY THIS MODULE EXISTS
----------------------
core/validator.py checks GRAMMAR using the real PostgreSQL parser. That is an
allowlist and it catches unknown unknowns -- but it only answers "is this
syntactically PostgreSQL?", never "will this resolve at execution time?".

    OPENJSON(@x)   parses cleanly: it is a legal function-call SHAPE.
                   PostgreSQL then fails with
                       42883: function openjson(text) does not exist

Two converted constants shipped exactly like that and only surfaced when a user
opened the screen. Grammar-valid is NOT runnable.

So every constant needs THREE checks, not two:

    1. linter        -- blocklist of known-bad patterns          (core/linter.py)
    2. validator     -- PostgreSQL grammar allowlist             (core/validator.py)
    3. resolvability -- functions/constructs that parse but      (this module)
                        cannot resolve, plus constructs that
                        must never be auto-converted

Codes are R0xx so they never collide with L0xx (linter) or A0xx (autofix).
"""

from __future__ import annotations

import re

from core.linter import mask_literals
from core.models import Severity, Warning

# --------------------------------------------------------------------------
# R001 -- functions PostgreSQL does not have
# --------------------------------------------------------------------------
# These all PARSE (legal call shape) and die at execution with 42883.
# Deliberately EXCLUDES functions PostgreSQL really does have, which earlier
# sweeps wrongly "fixed": LTRIM, RTRIM, BTRIM, UPPER, LOWER, SUBSTRING,
# REPLACE, COALESCE, NULLIF, CAST, ABS, ROUND, TRIM.
MISSING_FUNCTIONS: dict[str, str] = {
    "OPENJSON": "json_array_elements_text(...) / json_to_recordset(...)",
    "STRING_SPLIT": "STRING_TO_TABLE(x, ',')",
    # v2.14.0: what is left when a conversion only stripped `dbo.` from the
    # T-SQL table function dbo.Split -- 18 Master constants (PC12 repairs it)
    "SPLIT": "STRING_TO_TABLE(x, ',') -- see A022 / PC12",
    "ISNULL": "COALESCE(a, b)",
    "GETDATE": "NOW()",
    "GETUTCDATE": "NOW() AT TIME ZONE 'UTC'",
    "DATEADD": "x + INTERVAL '...'",
    "DATEDIFF": "EXTRACT(EPOCH FROM (a - b)) / <unit>",
    "DATEPART": "EXTRACT(<field> FROM x)",
    "DATENAME": "TO_CHAR(x, 'FMDay' / 'FMMonth')",
    "CHARINDEX": "STRPOS(haystack, needle)",
    "LEN": "LENGTH(x)",
    "STUFF": "STRING_AGG(...) -- see R004",
    "IIF": "CASE WHEN ... THEN ... ELSE ... END",
    "FORMAT": "TO_CHAR(x, '<mask>')",
    "REPLICATE": "REPEAT(x, n)",
    "SPACE": "REPEAT(' ', n)",
    "NEWID": "gen_random_uuid()",
    "TRY_CAST": "CAST with a validity guard (~ '^-?[0-9]+$')",
    "TRY_CONVERT": "CAST with a validity guard",
    "SCOPE_IDENTITY": "INSERT ... RETURNING <id>",
    "DATEFROMPARTS": "MAKE_DATE(y, m, d)",
    "EOMONTH": "(DATE_TRUNC('month', x) + INTERVAL '1 month - 1 day')::date",
    # v2.13.0 -- names sqlglot FABRICATES (they exist nowhere) and T-SQL
    # built-ins it passes through unchanged. Every one parses, every one is 42883.
    "NUMBER_TO_STR": "TO_CHAR(x, 'FM999,999,999,990.00') -- see A026",
    "DATE_FROM_PARTS": "MAKE_DATE(CAST(y AS INT), ...) -- see A032",
    "CURRENT_TIMESTAMP_L_T_Z": "CURRENT_TIMESTAMP",
    "ISJSON": "x IS JSON (PostgreSQL 16+)",
    "ISNUMERIC": "x ~ '^\\s*[-+]?([0-9]+(\\.[0-9]*)?|\\.[0-9]+)\\s*$'",
    "ISDATE": "a regex guard, or a typed parameter -- PostgreSQL has no ISDATE",
    "SWITCHOFFSET": "x AT TIME ZONE '<IANA zone>'",
    "TODATETIMEOFFSET": "x AT TIME ZONE '<IANA zone>'",
    "SYSDATETIMEOFFSET": "CURRENT_TIMESTAMP",
    "QUOTENAME": "QUOTE_IDENT(x) -- or build the identifier in C#",
    "STR": "LPAD(CAST(ROUND(CAST(x AS NUMERIC), d) AS TEXT), len)",
    "PATINDEX": "a regex: POSITION / SUBSTRING(x FROM 'pattern')",
    "DATALENGTH": "OCTET_LENGTH(CAST(x AS TEXT))",
    "SOUNDEX": "soundex(x) needs the fuzzystrmatch extension",
    "DIFFERENCE": "difference(a, b) needs the fuzzystrmatch extension",
    "CONTAINS": "full-text search: to_tsvector(x) @@ plainto_tsquery(...)",
    "FREETEXT": "full-text search: to_tsvector(x) @@ plainto_tsquery(...)",
    "OBJECT_ID": "to_regclass('name') -- or pg_temp checks for temp tables",
    "DB_NAME": "current_database()",
    "SUSER_SNAME": "current_user",
    "SUSER_NAME": "current_user",
    "USER_NAME": "current_user",
    "HOST_NAME": "inet_client_addr() -- or pass it from C#",
    "APP_NAME": "current_setting('application_name')",
    "ERROR_MESSAGE": "nothing outside PL/pgSQL -- see TRY/CATCH (R002)",
    "ERROR_NUMBER": "nothing outside PL/pgSQL -- see TRY/CATCH (R002)",
    "ERROR_LINE": "nothing outside PL/pgSQL -- see TRY/CATCH (R002)",
    "CHECKSUM": "md5(...) / hashtext(...) -- values will differ",
    "BINARY_CHECKSUM": "md5(...) -- values will differ",
    "HASHBYTES": "digest(x, 'sha256') (pgcrypto) or md5(x)",
    "FORMATMESSAGE": "format('%s ...', ...)",
    "TRY_PARSE": "a regex guard + CAST",
    "PARSE": "CAST / TO_DATE with an explicit mask",
    "SESSION_CONTEXT": "current_setting('app.<key>', true)",
    "YEAR": "CAST(EXTRACT(YEAR FROM x) AS INT) -- see A032",
    "MONTH": "CAST(EXTRACT(MONTH FROM x) AS INT) -- see A032",
    "DAY": "CAST(EXTRACT(DAY FROM x) AS INT) -- see A032",
    "CHOOSE": "CASE i WHEN 1 THEN a WHEN 2 THEN b END -- see A038",
    "STRING_ESCAPE": "to_json(x)::text (json) -- by hand",
    "VAR": "VAR_SAMP(x)",
    "VARP": "VAR_POP(x)",
    "STDEVP": "STDDEV_POP(x)",
}
_FUNC_RES = [
    (name, repl, re.compile(r"(?i)\b" + name + r"\s*\("))
    for name, repl in MISSING_FUNCTIONS.items()
]

_CATALOG_RES = [
    (r"(?i)\bsys\.time_zone_info\b", "sys.time_zone_info",
     "pg_timezone_names -- NOT a drop-in: it is IANA-keyed while the product "
     "stores Windows ids, so translate the id first"),
    (r"(?i)\bsp_executesql\b", "sp_executesql",
     "build the statement in C# with bound parameters, or rewrite set-based"),
    (r"(?i)\bdbo\.", "dbo. prefix", "drop it -- PostgreSQL resolves via search_path"),
    # v2.13.0: T-SQL types sqlglot maps to names PostgreSQL does not have
    (r"(?i)(?:\bAS|::)\s*(?:UTINYINT|VARIANT|IMAGE|SMALLMONEY)\b|\bBYTEA\s*\(",
     "UTINYINT / VARIANT / IMAGE / SMALLMONEY / BYTEA(n) type",
     "SMALLINT / TEXT / BYTEA / DECIMAL(10,4) / BYTEA -- see A033"),
    # a pre-pass placeholder that failed to expand must never ship
    (r"(?i)\bMSSQLTOPG_\w+", "internal MSSQLTOPG_ placeholder",
     "report this conversion -- a v2.13.0 pre-pass placeholder was not expanded"),
]
_CATALOG_RES = [(re.compile(p), n, r) for p, n, r in _CATALOG_RES]

# --------------------------------------------------------------------------
# R002 -- procedural control flow
# --------------------------------------------------------------------------
# PostgreSQL has NONE of this outside a function body. Omitting these from a
# scan makes a full rewrite look like a mechanical substitution: in one sweep
# it mis-sized 92 constants down to 23.
_CONTROL_FLOW = [
    (re.compile(r"(?im)^\s*(ELSE\s+)?IF\s*(\(|EXISTS|NOT\s+EXISTS|@)"), "IF / ELSE block"),
    (re.compile(r"(?im)^\s*BEGIN\s*$"), "BEGIN ... END block"),
    (re.compile(r"(?im)^\s*SET\s+@\w+\s*="), "SET @var ="),
    (re.compile(r"(?i)\bWHILE\s*\(|\bWHILE\s+@"), "WHILE loop"),
    (re.compile(r"(?i)\bMERGE\s+INTO\b"), "MERGE"),
    (re.compile(r"(?i)\bOUTPUT\s+INSERTED\."), "OUTPUT INSERTED"),
    (re.compile(r"(?i)\bRAISERROR\b|\bTHROW\s+\d"), "RAISERROR / THROW"),
    (re.compile(r"(?i)\bBEGIN\s+TRY\b"), "BEGIN TRY / CATCH"),
    (re.compile(r"(?i)\b(UN)?PIVOT\s*\("), "PIVOT / UNPIVOT"),
    (re.compile(r"(?i)\bDECLARE\s+@"), "DECLARE @"),
    # v2.13.0 -- the rest of the campaign scanner's hand-rewrite set
    (re.compile(r"(?i)\bCURSOR\s+(LOCAL\s+|FAST_FORWARD\s+|STATIC\s+|READ_ONLY\s+)*FOR\b"
                r"|\bFETCH\s+NEXT\s+FROM\b|@@FETCH_STATUS"), "CURSOR"),
    (re.compile(r"(?i)\bsp_executesql\b|\bEXEC(?:UTE)?\s*\(\s*@|\bEXEC(?:UTE)?\s+@\w+\s*$"),
     "dynamic SQL (EXEC(@sql) / sp_executesql)"),
    (re.compile(r"(?i)\bOPENXML\b|\bsp_xml_\w+|\.nodes\s*\("), "XML shredding (OPENXML / .nodes())"),
    (re.compile(r"(?i)@@(ROWCOUNT|ERROR|TRANCOUNT)\b"), "@@ROWCOUNT / @@ERROR / @@TRANCOUNT"),
    (re.compile(r"(?i)\bBEGIN\s+TRAN(SACTION)?\b|\bCOMMIT\s+TRAN(SACTION)?\b|\bROLLBACK\s+TRAN(SACTION)?\b"),
     "explicit transaction (BEGIN/COMMIT/ROLLBACK TRAN)"),
    (re.compile(r"(?i)\bGOTO\s+\w+|\bWAITFOR\s+(DELAY|TIME)\b"), "GOTO / WAITFOR"),
]

# MERGE runs on PostgreSQL 15+ (ACS is 17.2) -- a check, not a rewrite.
_WARNING_ONLY = {"MERGE"}
_MERGE_NOTE = ("MERGE runs on PostgreSQL 15+, so this is not a rewrite -- but "
               "check it: `WHEN NOT MATCHED BY SOURCE` needs 17, `OUTPUT $action` "
               "has no equivalent, and the target alias must not be repeated in SET.")
# Converted by v2.13.0 before the source check runs, so not reported from source:
_SOURCE_SKIP = {"OUTPUT INSERTED", "PIVOT / UNPIVOT"}

# --------------------------------------------------------------------------
# R003 -- must NEVER be auto-converted
# --------------------------------------------------------------------------
# Style codes ARE the date format. A blind swap yields silently wrong dates,
# which is worse than an error because nothing fails.
CONVERT_STYLES: dict[str, str] = {
    "100": "mon dd yyyy hh:miAM", "101": "mm/dd/yyyy", "102": "yyyy.mm.dd",
    "103": "dd/mm/yyyy", "104": "dd.mm.yyyy", "105": "dd-mm-yyyy",
    "106": "dd mon yyyy", "108": "hh:mi:ss", "110": "mm-dd-yyyy",
    "111": "yyyy/mm/dd", "112": "yyyymmdd", "120": "yyyy-mm-dd hh:mi:ss",
    "121": "yyyy-mm-dd hh:mi:ss.mmm", "126": "ISO 8601", "23": "yyyy-mm-dd",
}
_CONVERT3_RE = re.compile(
    r"(?i)\bCONVERT\s*\(\s*[A-Za-z_]+\s*(?:\([^)]*\))?\s*,[^,()]*(?:\([^)]*\))?[^,()]*,\s*(\d+)\s*\)")
_CONVERT_BIT_RE = re.compile(r"(?i)\bCONVERT\s*\(\s*bit\s*,")
_TOP_RE = re.compile(r"(?i)\bSELECT\s+(DISTINCT\s+)?TOP\s*\(?\s*[\w@]+\s*\)?")

# --------------------------------------------------------------------------
# R004..R006 -- traps that cost real debugging time
# --------------------------------------------------------------------------
_STUFF_SUBQUERY_RE = re.compile(r"(?i)\bSTUFF\s*\(\s*\(\s*SELECT\b")
_CASE_CAST_RE = re.compile(r"(?i)\bCASE\b(?:(?!\bEND\b).){0,400}?::\s*json\b", re.DOTALL)
_CREATE_TEMP_RE = re.compile(r"(?i)\bCREATE\s+(?:TEMP|TEMPORARY)\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?([\w.\"]+)")
_DROP_GUARD_RE = re.compile(r"(?i)\bDROP\s+TABLE\s+IF\s+EXISTS\s+(?:pg_temp\.)?([\w.\"]+)")

# --------------------------------------------------------------------------
# False positives -- these look broken and are NOT
# --------------------------------------------------------------------------
_FRAGMENT_SUFFIXES = ("_preamble", "_template", "_body", "_cte", "_fragment")
_FRAGMENT_PREFIXES = ("colmap_", "datefilter_")
_IN_PARAM_RE = re.compile(r"(?i)\bIN\s+@\w+")


def is_probable_fragment(name: str | None) -> bool:
    """True for constants that are concatenated into a larger statement.

    These are not meant to parse alone; reporting them wastes review time.
    """
    if not name:
        return False
    low = name.lower()
    return low.endswith(_FRAGMENT_SUFFIXES) or low.startswith(_FRAGMENT_PREFIXES)


def _line_of(text: str, index: int) -> int:
    return text.count("\n", 0, index) + 1


def check(sql: str, *, name: str | None = None) -> list[Warning]:
    """Return resolvability warnings for one converted SQL constant."""
    out: list[Warning] = []
    if not sql or not sql.strip():
        return out

    masked = mask_literals(sql)
    # strip -- and /* */ comments so a converted construct mentioned in a note
    # does not raise a false alarm
    code = re.sub(r"--[^\n]*", "", masked)
    code = re.sub(r"/\*.*?\*/", " ", code, flags=re.DOTALL)

    # R001 -- functions that do not exist
    for fname, repl, rx in _FUNC_RES:
        m = rx.search(code)
        # `... AS split(items)` is a derived table's column list (what A022
        # emits), not a call to a split() function
        while m and fname == "SPLIT" and re.search(r"(?i)\bAS\s+$", code[:m.start()]):
            m = rx.search(code, m.end())
        if m:
            out.append(Warning(
                "R001", Severity.ERROR,
                f"{fname}() does not exist in PostgreSQL. This PARSES -- it is a "
                f"legal call shape -- and fails at execution with 42883. "
                f"Use {repl}.",
                _line_of(code, m.start())))

    for rx, label, repl in _CATALOG_RES:
        m = rx.search(code)
        if m:
            out.append(Warning(
                "R001", Severity.ERROR,
                f"{label} has no PostgreSQL equivalent. Use {repl}.",
                _line_of(code, m.start())))

    # R002 -- procedural control flow
    for rx, label in _CONTROL_FLOW:
        m = rx.search(code)
        if m and label in _WARNING_ONLY:
            out.append(Warning("R002", Severity.WARNING, _MERGE_NOTE,
                               _line_of(code, m.start())))
        elif m:
            out.append(Warning(
                "R002", Severity.ERROR,
                f"{label} is procedural T-SQL. PostgreSQL has no control flow "
                f"outside a function body, so this is a REWRITE (fold into "
                f"CASE / a guarded WHERE / a CTE), not a substitution.",
                _line_of(code, m.start())))

    # R003 -- never auto-convert
    for m in _CONVERT3_RE.finditer(code):
        style = m.group(1)
        fmt = CONVERT_STYLES.get(style, "see the SQL Server style table")
        out.append(Warning(
            "R003", Severity.ERROR,
            f"CONVERT(..., {style}) -- the style code IS the date format "
            f"({fmt}). Never convert this mechanically; a blind swap yields "
            f"silently WRONG dates. Use TO_DATE/TO_CHAR with an explicit mask.",
            _line_of(code, m.start())))

    m = _CONVERT_BIT_RE.search(code)
    if m:
        out.append(Warning(
            "R003", Severity.ERROR,
            "CONVERT(bit, ...) -- bit and boolean do not coerce alike. "
            "Convert by hand.", _line_of(code, m.start())))

    m = _TOP_RE.search(code)
    if m:
        out.append(Warning(
            "R003", Severity.WARNING,
            "SELECT TOP -> LIMIT moves the clause to the END of the statement. "
            "Inside a subquery, or around an ORDER BY, wrong placement silently "
            "returns a DIFFERENT row. Place it by hand and re-check.",
            _line_of(code, m.start())))

    # R004 -- STUFF wrapping a subquery: the FOR XML was stripped, the wrapper left
    m = _STUFF_SUBQUERY_RE.search(code)
    if m:
        out.append(Warning(
            "R004", Severity.ERROR,
            "STUFF((SELECT ...)) -- the FOR XML PATH was removed but its STUFF "
            "wrapper was left behind, so the subquery now returns MANY rows: "
            "21000 'more than one row returned by a subquery'. Rewrite as "
            "STRING_AGG(col, ', ' ORDER BY ...); STUFF is then unnecessary.",
            _line_of(code, m.start())))

    # R005 -- a cast inside a CASE branch is not protected
    m = _CASE_CAST_RE.search(code)
    if m:
        out.append(Warning(
            "R005", Severity.WARNING,
            "A ::json cast inside a CASE branch is NOT guarded -- PostgreSQL "
            "constant-folds the unused branch when the value is a literal and "
            "still raises 22P02. Use "
            "COALESCE(NULLIF(BTRIM(COALESCE(x,'')),''),'[]')::json instead.",
            _line_of(code, m.start())))

    # R006 -- temp table created without a DROP guard ahead of it
    created = {t.lower().lstrip("pg_temp.") for t in _CREATE_TEMP_RE.findall(code)}
    dropped = {t.lower().lstrip("pg_temp.") for t in _DROP_GUARD_RE.findall(code)}
    for t in sorted(created - dropped):
        m = _CREATE_TEMP_RE.search(code)
        out.append(Warning(
            "R006", Severity.WARNING,
            f"CREATE TEMP TABLE {t} has no 'DROP TABLE IF EXISTS pg_temp.{t}' "
            f"ahead of it. On a POOLED connection a mid-batch error leaves the "
            f"table alive and every later call fails with 'relation already "
            f"exists'. Always lead with the drop.",
            _line_of(code, m.start()) if m else None))

    return out


def check_source(sql: str) -> list[Warning]:
    """Resolvability rules that must run on the ORIGINAL T-SQL, not the output.

    WHY THIS EXISTS
    ---------------
    sqlglot has no node for T-SQL procedural control flow. Given

        IF EXISTS (SELECT 1 FROM t) SELECT CONVERT(date, x, 105) FROM t

    it reports SG-UNSUPPORTED and returns an EMPTY string with ok=True. A check
    hooked only onto the converted output therefore sees nothing at all -- so
    R002 (control flow) and R003 (never-auto-convert) could essentially never
    fire, which is exactly backwards: those constructs are the whole reason the
    engine gave up.

    Only the rules that describe the INPUT are run here. R001 (missing
    functions) is deliberately excluded: the input is T-SQL, so of course it
    contains ISNULL/GETDATE, and reporting those before conversion would be
    noise. R001 belongs on the output, where a survivor is a real defect.
    """
    out: list[Warning] = []
    if not sql or not sql.strip():
        return out

    masked = mask_literals(sql)
    code = re.sub(r"--[^\n]*", "", masked)
    code = re.sub(r"/\*.*?\*/", " ", code, flags=re.DOTALL)

    for rx, label in _CONTROL_FLOW:
        if label in _SOURCE_SKIP:
            continue
        m = rx.search(code)
        if m and label in _WARNING_ONLY:
            out.append(Warning("R002", Severity.WARNING, _MERGE_NOTE,
                               _line_of(code, m.start())))
        elif m:
            out.append(Warning(
                "R002", Severity.ERROR,
                f"{label} in the SOURCE. PostgreSQL has no control flow outside "
                f"a function body, and sqlglot cannot represent it -- so this "
                f"constant is a hand REWRITE (fold into CASE / a guarded WHERE "
                f"/ a CTE). Converting the surrounding text is not enough.",
                _line_of(code, m.start())))

    for m in _CONVERT3_RE.finditer(code):
        style = m.group(1)
        fmt = CONVERT_STYLES.get(style, "see the SQL Server style table")
        out.append(Warning(
            "R003", Severity.ERROR,
            f"CONVERT(..., {style}) in the SOURCE -- the style code IS the date "
            f"format ({fmt}). Convert by hand with TO_DATE/TO_CHAR and an "
            f"explicit mask; a mechanical swap yields silently WRONG dates.",
            _line_of(code, m.start())))

    m = _CONVERT_BIT_RE.search(code)
    if m:
        out.append(Warning(
            "R003", Severity.ERROR,
            "CONVERT(bit, ...) in the SOURCE -- bit and boolean do not coerce "
            "alike. Convert by hand.", _line_of(code, m.start())))

    m = _STUFF_SUBQUERY_RE.search(code)
    if m:
        out.append(Warning(
            "R004", Severity.ERROR,
            "STUFF((SELECT ...)) in the SOURCE. If its FOR XML PATH is dropped "
            "without rewriting the whole idiom, the subquery becomes multi-row "
            "and fails with 21000. Target form is STRING_AGG(col, sep ORDER BY "
            "...), with no STUFF.", _line_of(code, m.start())))

    return out


def summary(warnings: list[Warning]) -> dict[str, int]:
    """Count warnings by R-code, for dashboards and the CLI summary line."""
    counts: dict[str, int] = {}
    for w in warnings:
        if w.code.startswith("R"):
            counts[w.code] = counts.get(w.code, 0) + 1
    return counts
