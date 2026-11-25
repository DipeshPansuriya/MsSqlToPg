"""Check-PG mode (v2.13.0): find and repair defects in ALREADY-CONVERTED SQL.

The C# scan must never run over converted code: PostgreSQL re-parsed as T-SQL
shifts results. But most of the defects fixed by hand in Aug-Sep 2026 sit in
code that was converted BEFORE the converter learned about them -- e.g. the 26
Export files calling fnGetLocalTime(CAST(col AS TIMESTAMP), ...). This mode
reads each SQL literal as PostgreSQL, never as T-SQL, and:

  REPAIRS (text-level, each one re-validated -- a repair that makes a VALID
  query INVALID is reverted):
    PC01 fnGetLocalTime(CAST(x AS TIMESTAMP), ...) / (NOW() AT TIME ZONE 'UTC')
         -> keep the timestamptz overload (A025)
    PC02 NUMBER_TO_STR(x, '<.NET mask>') -> TO_CHAR(x, '<PG mask>') (A026)
    PC03 welded alias `XON X.col` -> `X ON X.col` (A029)
    PC04 COALESCE(bool, 0/1) and bool = 0/1 -> FALSE/TRUE (A007/A024) -- only
         where the literal is provably PostgreSQL, so a SQL Server twin in a
         dual-provider file is never touched
    PC05 fabricated names: DATE_FROM_PARTS -> MAKE_DATE,
         CURRENT_TIMESTAMP_L_T_Z() -> CURRENT_TIMESTAMP, UTINYINT -> SMALLINT,
         BYTEA(MAX) -> BYTEA, EXTRACT(DAYOFWEEK/DAYOFYEAR ...) -> DOW+1 / DOY
    PC06 YEAR(x)/MONTH(x)/DAY(x) -> CAST(EXTRACT(... FROM x) AS INT)
    PC07 unguarded DROP of a temp table -> DROP TABLE IF EXISTS pg_temp.x, and
         a DROP guard ahead of every CREATE TEMP TABLE (A040)
    PC08 RETURNING INSERTED.x -> RETURNING x; SET NOCOUNT removed;
         'TMDay'/'TMMonth' -> 'FMDay'/'FMMonth'; 'DDTHH' mask -> 'DD"T"HH';
         GROUP BY ... WITH ROLLUP -> GROUP BY ROLLUP (...)
    PC09 (v2.14.0) missing ';' between the statements of a multi-result batch,
         placed where PostgreSQL's parser stops -- kept only if it then parses
    PC10 (v2.14.0) T-SQL [name] -> name / "reserved" (never array syntax)
    PC11 (v2.14.0) '%@x%' -> '%' || @x || '%' (the processor never binds it)
    PC15 (v2.17.0) `(SELECT COUNT(*) FROM t) AS TotalRecords` -> COUNT(*) OVER()
         when the statement reads all of t (A059)
    PC16 (v2.17.0) nested SELECT kept on its '(' line in a TotalRecords
         statement, so the filterString pager wraps the whole statement (A060)
  REPORTS: grammar validity, missing functions (R001), T-SQL residue (R007),
  and the v2.13.0 lint (L022-L026, R008).

Literals that still carry T-SQL are NOT touched here -- they are listed as
"still T-SQL, use the Scan tab".
"""

from __future__ import annotations

import re
import shutil
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from core import autofix, csharp, interpolation, markers, pgfixes, resolvability, scan, totalrecords, validator
from core.models import Severity, Warning

FIXED = "FIXED"
REPORTED = "REPORTED"
CLEAN = "CLEAN"
TSQL = "T-SQL"
FAILED = "FAILED"

_PG_EVIDENCE = re.compile(
    r"::|\bLIMIT\b|\bILIKE\b|\bTRUE\b|\bFALSE\b|\bNOW\s*\(\)|\|\||\bpg_temp\b|\bRETURNING\b|"
    r"\bINTERVAL\s*'|\bLATERAL\b|\bSTRING_TO_TABLE\b|\bjsonb?_\w+\(|\bCREATE\s+TEMP", re.I)


# --------------------------------------------------------------------------
# repairs -- each (sql) -> (sql, count)
# --------------------------------------------------------------------------

def _replace_calls(sql: str, name_re: str, build) -> tuple[str, int]:
    """Replace each call `name(args)` with build(args_list) (None = keep)."""
    count = 0
    pos = 0
    while True:
        masked = pgfixes.code_mask(sql)
        m = re.compile(name_re, re.I).search(masked, pos)
        if not m:
            break
        close = pgfixes.match_paren(masked, m.end())
        if close < 0:
            break
        args = pgfixes.split_top_level(sql[m.end():close], masked[m.end():close])
        new = build(m, args)
        if new is None:
            pos = m.end()
            continue
        sql = sql[:m.start()] + new + sql[close + 1:]
        pos = m.start() + len(new)
        count += 1
    return sql, count


_NAIVE_CAST_RE = re.compile(
    r"(?is)^\s*(?:CAST\s*\(\s*(.+?)\s+AS\s+TIMESTAMP(?:\s+WITHOUT\s+TIME\s+ZONE)?\s*\)"
    r"|(.+?)\s*::\s*TIMESTAMP(?:\s+WITHOUT\s+TIME\s+ZONE)?)\s*$")
_UTC_NOW_RE = re.compile(r"(?is)^\s*\(?\s*(NOW\s*\(\s*\)|CURRENT_TIMESTAMP)\s+AT\s+TIME\s+ZONE\s+'UTC'\s*\)?\s*$")
_SAFE_INNER_RE = re.compile(r"(?is)^\s*([\w.\"]+|NOW\s*\(\s*\)|CURRENT_TIMESTAMP|COALESCE\s*\(.*\)|MAX\s*\(.*\)|MIN\s*\(.*\))\s*$")


def repair_fngetlocaltime(sql: str) -> tuple[str, int]:
    def build(m, args):
        if not args:
            return None
        first = args[0]
        c = _NAIVE_CAST_RE.match(first)
        if c:
            inner = c.group(1) or c.group(2)
            if _SAFE_INNER_RE.match(inner):
                return f"{m.group(0)[:-1]}({inner.strip()}{', ' if len(args) > 1 else ''}{', '.join(args[1:])})"
        if _UTC_NOW_RE.match(first):
            return f"{m.group(0)[:-1]}(NOW(){', ' if len(args) > 1 else ''}{', '.join(args[1:])})"
        return None
    return _replace_calls(sql, r"\bfngetlocaltime\s*\(", build)


def repair_number_to_str(sql: str) -> tuple[str, int]:
    def build(m, args):
        if len(args) != 2:
            return None
        lit = re.fullmatch(r"(?s)'((?:[^']|'')*)'", args[1])
        if not lit:
            return None
        pg = pgfixes._numeric_mask(lit.group(1).replace("''", "'"))
        return None if pg is None else f"TO_CHAR({args[0]}, {pgfixes._sql_string(pg)})"
    return _replace_calls(sql, r"\bNUMBER_TO_STR\s*\(", build)


def repair_welded(sql: str) -> tuple[str, int]:
    ctx = pgfixes.Context()
    out = pgfixes.fix_welded_on(sql, ctx)
    return out, sum(a.count for a in ctx.applied)


def _is_bool(name: str) -> bool:
    """Stricter than autofix, because this repairs code that may already have
    been tested: a live `IsX = 0` that works means IsX is still an INTEGER.
    Only the schema's own list (boolean_columns.txt) or the confirmed
    allowlist is trusted here; a name that merely looks boolean is reported
    (L028), never rewritten."""
    from core import schema_hints
    low = name.lower()
    return low in schema_hints.boolean_columns() or low in autofix._KNOWN_BOOLEAN_COLUMNS


def boolean_suspects(sql: str) -> list[str]:
    """Is*-named columns compared to 0/1 that the schema list does not cover."""
    masked = pgfixes.code_mask(sql)
    names = set()
    for m in _BOOL_CMP_RE.finditer(masked):
        if re.match(r"^Is[A-Z0-9]", m.group(2)) and not _is_bool(m.group(2)):
            names.add(m.group(2))
    for m in _BOOL_COALESCE_RE.finditer(masked):
        has_int = m.group(3).strip("'") in ("0", "1") or m.group(5) is not None
        if has_int and re.match(r"^Is[A-Z0-9]", m.group(2)) and not _is_bool(m.group(2)):
            names.add(m.group(2))
    return sorted(names)


_BOOL_CMP_RE = re.compile(r"(?<![@\w.:])((?:\w+\.)?(\w+))(\s*)(=|<>|!=)(\s*)([01])(?![\w.])")
_BOOL_COALESCE_RE = re.compile(
    r"(?i)\bCOALESCE\s*\(\s*((?:\w+\.)?(\w+))\s*,\s*('?[01]'?|TRUE|FALSE)\s*\)"
    r"(?:(\s*(?:=|<>|!=)\s*)([01])(?![\w.]))?")


def _bool_word(token: str) -> str:
    return "TRUE" if token.strip("'").upper() in ("1", "TRUE") else "FALSE"


def repair_booleans(sql: str) -> tuple[str, int]:
    masked = pgfixes.code_mask(sql)
    if not _PG_EVIDENCE.search(masked):
        return sql, 0
    edits = []
    for m in _BOOL_COALESCE_RE.finditer(masked):
        if not _is_bool(m.group(2)):
            continue
        default_is_digit = m.group(3).strip("'") in ("0", "1")
        if not default_is_digit and m.group(5) is None:
            continue              # already COALESCE(x, FALSE) with no 0/1 compare
        # the comparison AFTER the COALESCE must change too: COALESCE(x, FALSE) = 0
        # is still boolean = integer (42883)
        tail = f"{m.group(4)}{_bool_word(m.group(5))}" if m.group(5) is not None else ""
        edits.append((m.start(), m.end(),
                      f"COALESCE({sql[m.start(1):m.end(1)]}, {_bool_word(m.group(3))}){tail}"))
    for m in _BOOL_CMP_RE.finditer(masked):
        if any(s <= m.start() < e for s, e, _ in edits):
            continue
        if _is_bool(m.group(2)):
            word = "TRUE" if m.group(6) == "1" else "FALSE"
            edits.append((m.start(6), m.end(6), word))
    return pgfixes._apply_edits(sql, edits), len(edits)


_SIMPLE_SUBS = [
    (re.compile(r"(?i)\bDATE_FROM_PARTS\s*\("), "MAKE_DATE("),
    (re.compile(r"(?i)\bCURRENT_TIMESTAMP_L_T_Z\s*\(\s*\)"), "CURRENT_TIMESTAMP"),
    (re.compile(r"(?i)\bUTINYINT\b"), "SMALLINT"),
    (re.compile(r"(?i)\bBYTEA\s*\(\s*(?:MAX|\d+)\s*\)"), "BYTEA"),
    (re.compile(r"(?i)\bEXTRACT\s*\(\s*DAYOFYEAR\s+FROM\b"), "EXTRACT(DOY FROM"),
    (re.compile(r"(?i)\bRETURNING\s+INSERTED\."), "RETURNING "),
    (re.compile(r"(?i)(?<![\w@])SET\s+NOCOUNT\s*=?\s*(?:ON|OFF)\s*;?"), ""),
]


def repair_simple(sql: str) -> tuple[str, int]:
    masked = pgfixes.code_mask(sql)
    edits = []
    for rx, new in _SIMPLE_SUBS:
        for m in rx.finditer(masked):
            edits.append((m.start(), m.end(), new))
    # EXTRACT(DAYOFWEEK FROM x) -> (CAST(EXTRACT(DOW FROM x) AS INT) + 1)
    sql2 = pgfixes._apply_edits(sql, edits)
    sql3, n = _replace_calls(
        sql2, r"\bEXTRACT\s*\(",
        lambda m, a: (f"(CAST(EXTRACT(DOW FROM {re.sub(r'(?is)^\s*DAYOFWEEK\s+FROM\s+', '', a[0])}) AS INT) + 1)"
                      if len(a) == 1 and re.match(r"(?is)^\s*DAYOFWEEK\s+FROM\s+", a[0]) else None))
    # string-literal masks: TMDay/TMMonth padding, unquoted T before HH
    sql4, k = re.subn(r"'TM(Day|Month)'", r"'FM\1'", sql3)
    j = 0
    mask_edits = []
    for mm in pgfixes._TOCHAR_MASK_RE.finditer(sql4):
        mask = mm.group(1)
        fixed = re.sub(r"(?<=DD|MM|YY)T(?=HH)", '"T"', mask)
        fixed = re.sub(r"(?<![A-Za-z])fff(?!f)", "MS", fixed)   # .NET milliseconds
        if fixed != mask:
            mask_edits.append((mm.start(1), mm.end(1), fixed))
            j += 1
    sql5 = pgfixes._apply_edits(sql4, mask_edits)
    return sql5, len(edits) + n + k + j


def repair_year_month_day(sql: str) -> tuple[str, int]:
    def build(m, args):
        if len(args) != 1:
            return None
        part = re.match(r"(?i)\b(YEAR|MONTH|DAY)", m.group(0)).group(1).upper()
        return f"CAST(EXTRACT({part} FROM {args[0]}) AS INT)"
    return _replace_calls(sql, r"(?<![\w.])(?:YEAR|MONTH|DAY)\s*\(", build)


def repair_temp_tables(sql: str) -> tuple[str, int]:
    masked = pgfixes.code_mask(sql)
    temps = {m.group(1).strip('"').lower() for m in re.finditer(
        r"(?i)\bCREATE\s+(?:TEMP|TEMPORARY)\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(?:pg_temp\.)?(\"?\w+\"?)", masked)}
    edits = []
    for m in re.finditer(r"(?i)\bDROP\s+TABLE\s+(?!IF\s+EXISTS)(?:pg_temp\.)?(\"?\w+\"?)", masked):
        if m.group(1).strip('"').lower() in temps:
            edits.append((m.start(), m.end(), f"DROP TABLE IF EXISTS pg_temp.{m.group(1)}"))
    for m in re.finditer(r"(?i)\bDROP\s+TABLE\s+IF\s+EXISTS\s+(\"?\w+\"?)(?!\.)", masked):
        if m.group(1).strip('"').lower() in temps and m.group(1).lower() != "pg_temp":
            edits.append((m.start(1), m.start(1), "pg_temp."))
    sql = pgfixes._apply_edits(sql, edits)
    sql, guards = pgfixes.add_temp_drop_guards(sql)
    return sql, len(edits) + guards


def repair_rollup(sql: str) -> tuple[str, int]:
    ctx = pgfixes.Context()
    out = pgfixes.fix_generated_text(sql, ctx)
    return out, sum(a.count for a in ctx.applied)


def repair_brackets(sql: str) -> tuple[str, int]:
    """PC10 -- T-SQL [name] left in converted code (pgfixes, pg_mode)."""
    return pgfixes.rewrite_bracket_identifiers(sql, pg_mode=True)


def repair_param_literals(sql: str) -> tuple[str, int]:
    """PC11 -- '%@x%' is never bound by QueryParameterProcessor (A052)."""
    return pgfixes.rewrite_param_literals(sql, "||")


def repair_split_calls(sql: str) -> tuple[str, int]:
    """PC12 -- bare SPLIT(x, sep): no such function in PostgreSQL (pgfixes)."""
    return pgfixes.rewrite_split_calls(sql)


# PC13 (v2.16.0): an earlier conversion kept the runtime placeholder as a quoted
# schema ("@MasterdbName".T, A010's pre-2.16 output). With the default "drop"
# policy -- one PostgreSQL database -- the prefix goes, like A010 now does.
_QUOTED_PLACEHOLDER_RE = re.compile(r'"@[A-Za-z_]\w*"\s*\.\s*')


def repair_placeholder_schemas(sql: str) -> tuple[str, int]:
    """PC13 -- "@MasterdbName".T -> T when MSSQLTOPG_DB_PLACEHOLDER is drop (default)."""
    from core import schema_hints
    if schema_hints.db_placeholder_policy() != "drop":
        return sql, 0
    out, count, pos = [], 0, 0
    for m in _QUOTED_PLACEHOLDER_RE.finditer(sql):
        if _inside_string(sql, m.start()):
            continue
        out.append(sql[pos:m.start()])
        pos = m.end()
        count += 1
    out.append(sql[pos:])
    return ("".join(out), count) if count else (sql, 0)


def _inside_string(sql: str, index: int) -> bool:
    """True when `index` falls inside a '...' string literal."""
    inside, i = False, 0
    while i < index:
        if sql[i] == "'":
            if inside and i + 1 < len(sql) and sql[i + 1] == "'":
                i += 2
                continue
            inside = not inside
        i += 1
    return inside


def repair_implicit_conversions(sql: str) -> tuple[str, int]:
    """PC14 -- A054 on converted code: SQL Server's silent conversions made explicit from column_types.txt."""
    from core import typefix
    return typefix.apply(sql)


_STATEMENT_KEYWORDS = frozenset(
    {"SELECT", "UPDATE", "INSERT", "DELETE", "WITH", "CREATE", "DROP", "TRUNCATE", "ALTER", "MERGE"})
_NEAR_INDEX_RE = re.compile(r'at or near "([^"]*)", at index (\d+)')


def _syntax_error_at(sql: str) -> tuple[str, int] | None:
    """(token, offset) of the first PostgreSQL syntax error; None when valid
    (or when no offset is reported). Same Dapper masking as the validator."""
    if not validator.AVAILABLE:
        return None
    try:
        validator.pglast.parse_sql(validator._mask_dapper_parameters(sql))
        return None
    except validator.ParseError as exc:
        m = _NEAR_INDEX_RE.search(str(exc))
        return (m.group(1), int(m.group(2))) if m else ("", -1)
    except Exception:
        return ("", -1)


def repair_statement_separators(sql: str) -> tuple[str, int]:
    """PC09 -- statements of a multi-result batch with no ';' between them.

    SQL Server accepts `SELECT ... SELECT ...` as one batch; PostgreSQL stops
    at the second SELECT (42601). Conversions that kept the batch text without
    re-parsing it (by hand, or text-level fixes) carried the gap over -- 24
    such constants in the Master API alone. PostgreSQL's own parser points at
    each boundary: the error is AT a statement keyword, and the `;` goes after
    the last code character before it (never into a trailing comment). Every
    other parse error is left alone, and the result is kept only if the
    whole literal then parses."""
    if _valid(sql) is not False:
        return sql, 0
    current, inserted = sql, 0
    for _ in range(64):
        error = _syntax_error_at(current)
        if error is None:
            break
        token, offset = error
        if offset < 0 or token.upper() not in _STATEMENT_KEYWORDS:
            return sql, 0
        masked = pgfixes.code_mask(current)
        k = offset - 1
        while k >= 0 and masked[k].isspace():
            k -= 1
        if k < 0 or masked[k] in ";(,":
            return sql, 0
        current = current[:k + 1] + ";" + current[k + 1:]
        inserted += 1
    if not inserted or _valid(current) is not True:
        return sql, 0
    return current, inserted


REPAIRS = [
    ("PC10", "T-SQL [bracket] identifiers unbracketed", repair_brackets),
    ("PC11", "'%@param%' literal bound as '%' || @param || '%'", repair_param_literals),
    ("PC12", "SPLIT(x, sep) -> STRING_TO_TABLE derived table", repair_split_calls),
    ("PC13", '"@Placeholder".T -> T (one PostgreSQL database)', repair_placeholder_schemas),
    ("PC01", "fnGetLocalTime kept on its timestamptz overload", repair_fngetlocaltime),
    ("PC02", "NUMBER_TO_STR -> TO_CHAR", repair_number_to_str),
    ("PC03", "welded alias ON split", repair_welded),
    ("PC04", "boolean compared/defaulted with 0/1 -> TRUE/FALSE", repair_booleans),
    ("PC05", "fabricated function/type names", repair_simple),
    ("PC06", "YEAR/MONTH/DAY -> EXTRACT", repair_year_month_day),
    ("PC07", "temp-table DROP guards", repair_temp_tables),
    ("PC08", "DATENAME padding / ROLLUP", repair_rollup),
    ("PC14", "implicit conversions made explicit (column_types.txt)", repair_implicit_conversions),
    ("PC15", "TotalRecords count subquery -> COUNT(*) OVER()", totalrecords.window_counts),
    ("PC16", "nested SELECT kept on its '(' line in a TotalRecords statement", totalrecords.glue_nested_selects),
    # last: the other repairs can change where (and whether) PostgreSQL stops
    ("PC09", "missing ';' between statements", repair_statement_separators),
]


@dataclass
class CheckResult:
    sql: str
    applied: list[tuple[str, int]] = field(default_factory=list)
    findings: list[Warning] = field(default_factory=list)
    valid_before: bool | None = None
    valid_after: bool | None = None

    @property
    def changed(self) -> bool:
        return bool(self.applied)


def _valid(sql: str) -> bool | None:
    try:
        v = validator.validate(sql)
        return getattr(v, "valid", None)
    except Exception:
        return None


def check_sql(sql: str) -> CheckResult:
    """Repair + report one PostgreSQL statement. Never raises."""
    result = CheckResult(sql=sql)
    result.valid_before = _valid(sql)
    current = sql
    for code, _label, repair in REPAIRS:
        try:
            new, count = repair(current)
        except Exception:
            continue
        if not count or new == current:
            continue
        # a repair that turns a VALID statement INVALID is reverted
        if result.valid_before and _valid(new) is False:
            continue
        current = new
        result.applied.append((code, count))
    result.sql = current
    result.valid_after = _valid(current) if result.applied else result.valid_before
    if result.valid_after is False:
        result.findings.append(Warning("PG-SYNTAX", Severity.ERROR,
                                       "not valid PostgreSQL grammar"))
    result.findings += resolvability.check(current)
    result.findings += pgfixes.residue(current)
    result.findings += pgfixes.lint("", current)
    suspects = boolean_suspects(current)
    if suspects:
        result.findings.append(Warning(
            "L028", Severity.WARNING,
            f"Is*-named column(s) {', '.join(suspects[:6])} compared/defaulted with 0/1. "
            f"If the column is BOOLEAN this raises 42883/42804 at runtime; if it is "
            f"still INTEGER it is correct. Add confirmed booleans to "
            f"boolean_columns.txt and re-run to repair them."))
    # R006 is repaired above; drop its now-stale warning
    result.findings = [w for w in result.findings if not (w.code == "R006" and result.applied)]
    return result


# --------------------------------------------------------------------------
# project run
# --------------------------------------------------------------------------

@dataclass
class Finding:
    line: int
    action: str
    detail: str = ""
    result: CheckResult | None = None


@dataclass
class FileReport:
    path: Path
    findings: list[Finding] = field(default_factory=list)
    status: str = CLEAN
    error: str = ""
    backup: Path | None = None
    encoding: str = "utf-8"

    @property
    def fixed(self) -> int:
        return sum(1 for f in self.findings if f.action == FIXED)

    @property
    def reported(self) -> int:
        return sum(1 for f in self.findings if f.action == REPORTED)

    @property
    def tsql(self) -> int:
        return sum(1 for f in self.findings if f.action == TSQL)


@dataclass
class CheckReport:
    root: Path | None = None
    files: list[FileReport] = field(default_factory=list)
    duration_ms: int = 0
    dry_run: bool = False

    def total(self, attr: str) -> int:
        return sum(getattr(f, attr) for f in self.files)


def _process(path: Path, dry_run: bool) -> FileReport:
    report = FileReport(path=path)
    try:
        text, report.encoding = scan.read_source(path)
    except (OSError, UnicodeDecodeError, LookupError) as exc:
        report.status, report.error = FAILED, f"{type(exc).__name__}: {exc}"
        return report
    literals = csharp.find_sql_literals(text, allow_leading_comments=True)
    if not literals:
        return report
    newline = scan.dominant_newline(text)
    replacements = []
    for lit in literals:
        hard, soft = markers.find(lit.sql)
        # brackets alone do not make a literal T-SQL any more: PC10 repairs
        # them in place (whatever it cannot place safely is still reported).
        tsql_left = hard or [s for s in soft if s not in ("string + concat", "[bracket] identifiers")]
        if tsql_left:
            report.findings.append(Finding(lit.line, TSQL,
                                           "still T-SQL (" + ", ".join(tsql_left[:4])
                                           + ") -- convert it with the Scan tab"))
            continue
        sql, holes = lit.sql, None
        if lit.interpolated:
            masked = interpolation.mask_holes(lit.sql)
            if masked is None:
                continue
            sql, holes = masked
        result = check_sql(sql)
        serious = [w for w in result.findings if w.severity is not Severity.INFO]
        if result.changed:
            new_sql = result.sql
            if holes is not None:
                new_sql = interpolation.restore_holes(new_sql, holes)
                if new_sql is None:
                    report.findings.append(Finding(lit.line, REPORTED,
                                                   "repair moved an {expr} hole -- not applied", result))
                    continue
            # keep the literal's own layout: repairs are in-place text edits
            if newline != "\n":
                new_sql = new_sql.replace("\r\n", "\n").replace("\n", newline)
            encoded = csharp.encode(lit.kind, new_sql)
            if lit.interpolated:
                encoded = "$" + encoded
            replacements.append((lit, encoded))
            report.findings.append(Finding(
                lit.line, FIXED, ", ".join(f"{c} x{n}" for c, n in result.applied), result))
        elif serious:
            report.findings.append(Finding(lit.line, REPORTED,
                                           "; ".join(sorted({w.code for w in serious})), result))
    if not replacements:
        report.status = REPORTED if report.reported or report.tsql else CLEAN
        return report
    report.status = FIXED
    if dry_run:
        return report
    try:
        backup = path.with_suffix(path.suffix + scan.BACKUP_SUFFIX)
        if not backup.exists():
            shutil.copy2(path, backup)
        with path.open("w", encoding=report.encoding, newline="") as handle:
            handle.write(csharp.replace_literals(text, replacements))
        report.backup = backup
    except (OSError, UnicodeEncodeError) as exc:
        report.status, report.error = FAILED, f"could not write file: {exc}"
    return report


def run(root: str | Path, *, recursive: bool = True, dry_run: bool = True,
        on_progress: Callable[[FileReport, int, int], None] | None = None,
        should_stop: Callable[[], bool] | None = None) -> CheckReport:
    started = time.perf_counter()
    root = Path(root)
    report = CheckReport(root=root, dry_run=dry_run)
    files = scan.find_cs_files(root, recursive)
    with ThreadPoolExecutor(max_workers=scan.MAX_WORKERS) as pool:
        futures = []
        for path in files:
            if should_stop and should_stop():
                break
            futures.append(pool.submit(_process, path, dry_run))
        for index, future in enumerate(futures, start=1):
            try:
                item = future.result()
            except Exception as exc:
                item = FileReport(path=Path("<unknown>"), status=FAILED,
                                  error=f"{type(exc).__name__}: {exc}")
            report.files.append(item)
            if on_progress:
                on_progress(item, index, len(files))
    report.duration_ms = int((time.perf_counter() - started) * 1000)
    return report


def render_report(report: CheckReport) -> str:
    mode = "DRY RUN -- nothing was written" if report.dry_run else "files repaired in place (.bak kept)"
    lines = [
        "# Converted-PostgreSQL check",
        "",
        f"- Root: `{report.root}`",
        f"- Mode: **{mode}**",
        f"- Duration: {report.duration_ms / 1000:.1f}s",
        "",
        "| | Count |",
        "|---|---|",
        f"| .cs files scanned | {len(report.files)} |",
        f"| statements repaired | {report.total('fixed')} |",
        f"| statements with findings (not auto-fixable) | {report.total('reported')} |",
        f"| statements still T-SQL (use the Scan tab) | {report.total('tsql')} |",
        f"| files failed | {sum(1 for f in report.files if f.status == FAILED)} |",
        "",
    ]
    for item in report.files:
        if not item.findings and item.status != FAILED:
            continue
        try:
            shown = item.path.relative_to(report.root)
        except ValueError:
            shown = item.path
        lines.append(f"### `{shown}` -- {item.status}")
        if item.error:
            lines.append(f"- {item.error}")
        for f in item.findings:
            lines.append(f"- line {f.line}: **{f.action}** {f.detail}")
            if f.result is not None:
                for w in f.result.findings:
                    if w.severity is not Severity.INFO:
                        lines.append(f"    - {w.code}: {w.message}")
        lines.append("")
    return "\n".join(lines) + "\n"


def write_report(report: CheckReport, filename: str = "pg-check-report.md") -> Path | None:
    if report.root is None:
        return None
    try:
        path = report.root / filename
        path.write_text(render_report(report), encoding="utf-8")
        return path
    except OSError:
        return None
