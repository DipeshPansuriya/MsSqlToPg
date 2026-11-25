"""v2.13.0 rules -- the September 2026 hand-conversion lessons, automated.

Every rule here was a defect found by hand AFTER v2.12.0 shipped: a query the
converter reported as clean (grammar VALID, no ERROR) that then failed at
runtime or -- worse -- returned wrong data. They are grouped by WHEN they run:

  pre_parse()      TEXT rewrites before sqlglot sees the SQL -- constructs
                   sqlglot cannot parse, drops, or maps to a fabricated form
  apply_ast_pre()  AST fixes that must run BEFORE core/autofix (A025 must see
                   GETUTCDATE() before A006 rewrites it)
  apply_ast()      AST fixes that run AFTER core/autofix
  post_generate()  TEXT fixes on the generated PostgreSQL (placeholder
                   expansion, temp-table DROP guards)
  lint()           reported, never rewritten -- needs the schema or the C#
                   caller to decide

As everywhere in this tool: every fix is DISCLOSED, and every fix has a
refusal case where it leaves the SQL alone and says why.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import sqlglot
from sqlglot import expressions as exp

from core import autofix
from core.autofix import Applied
from core.models import Severity, Warning


# ==========================================================================
# Shared text helpers
# ==========================================================================

def code_mask(sql: str) -> str:
    """Same-length copy of `sql` where comment text becomes spaces and
    string-literal CONTENT becomes '_' (the quotes stay). Regexes run on the
    mask so nothing inside a comment or a literal is ever matched; edits are
    then applied to the original at the same offsets."""
    out = list(sql)
    i, n = 0, len(sql)
    while i < n:
        c = sql[i]
        if c == "'":
            j = i + 1
            while j < n:
                if sql[j] == "'":
                    if j + 1 < n and sql[j + 1] == "'":
                        out[j] = out[j + 1] = "_"
                        j += 2
                        continue
                    break
                if sql[j] != "\n":
                    out[j] = "_"
                j += 1
            i = j + 1
            continue
        if sql.startswith("--", i):
            j = sql.find("\n", i)
            j = n if j < 0 else j
            for k in range(i, j):
                out[k] = " "
            i = j
            continue
        if sql.startswith("/*", i):
            j = sql.find("*/", i + 2)
            j = n if j < 0 else j + 2
            for k in range(i, j):
                if sql[k] != "\n":
                    out[k] = " "
            i = j
            continue
        i += 1
    return "".join(out)


def match_paren(text: str, i: int) -> int:
    """`i` is just past an opening '('; index of the matching ')', or -1.
    Run it on the MASK so parens inside literals/comments never count."""
    depth = 1
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def split_top_level(text: str, masked: str) -> list[str]:
    """Split on commas at paren depth 0 (masked decides depth)."""
    parts, depth, start = [], 0, 0
    for i, ch in enumerate(masked):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch == "," and depth == 0:
            parts.append(text[start:i])
            start = i + 1
    parts.append(text[start:])
    return [p.strip() for p in parts]


def _apply_edits(sql: str, edits: list[tuple[int, int, str]]) -> str:
    for start, end, new in sorted(edits, key=lambda e: -e[0]):
        sql = sql[:start] + new + sql[end:]
    return sql


def _sql_string(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


# ==========================================================================
# [bracket] identifiers -- shared by A051 (T-SQL input) and PC10 (converted PG)
# ==========================================================================
# T-SQL brackets only DELIMIT a name; SQL Server still matches it
# case-insensitively. sqlglot turns every bracket into a double-quoted
# identifier, and in PostgreSQL a quoted identifier is case-SENSITIVE: the
# schema was created with unquoted (lower-case) names, so "Master_Tax",
# "IsDeleted" or an alias "ER" declared bare all fail at runtime -- relation /
# column does not exist, missing FROM-clause entry -- while the grammar check
# passes. A bare name folds to lower case and matches. Names PostgreSQL will
# not take bare (reserved words) are quoted in LOWER case, which is how the
# schema generator created them ("order"); names with spaces or symbols keep
# their quoted text, since only a quoted identifier can carry them.

try:
    from pglast import keywords as _pg_keywords
    _PG_NEEDS_QUOTES = frozenset(_pg_keywords.RESERVED_KEYWORDS
                                 | _pg_keywords.TYPE_FUNC_NAME_KEYWORDS
                                 | _pg_keywords.COL_NAME_KEYWORDS)
except ImportError:  # pragma: no cover - pglast is a pinned dependency
    _PG_NEEDS_QUOTES = frozenset()

# SQL Server's reserved words: bare, they break the T-SQL parse before
# PostgreSQL ever sees the name ([Key], [Percent], [File], [Plan]).
_TSQL_RESERVED = frozenset("""
add all alter and any as asc authorization backup begin between break browse bulk by
cascade case check checkpoint close clustered coalesce collate column commit compute
constraint contains containstable continue convert create cross current current_date
current_time current_timestamp current_user cursor database dbcc deallocate declare
default delete deny desc disk distinct distributed double drop dump else end errlvl
escape except exec execute exists exit external fetch file fillfactor for foreign
freetext freetexttable from full function goto grant group having holdlock identity
identity_insert identitycol if in index inner insert intersect into is join key kill
left like lineno load merge national nocheck nonclustered not null nullif of off
offsets on open opendatasource openquery openrowset openxml option or order outer over
percent pivot plan precision primary print proc procedure public raiserror read
readtext reconfigure references replication restore restrict return revert revoke
right rollback rowcount rowguidcol rule save schema securityaudit select session_user
set setuser shutdown some statistics system_user table tablesample textsize then to
top tran transaction trigger truncate try_convert tsequal union unique unpivot update
updatetext use user values varying view waitfor when where while with within
writetext""".split())

_BARE_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")
_TEMP_NAME_RE = re.compile(r"^##?[A-Za-z0-9_]+$")
# PC10 only: content that is a T-SQL name and nothing a PostgreSQL array
# subscript would hold (numbers, expressions, slices).
_PG_MODE_CONTENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_ $#]*$")
_GLUED_ALIAS_RE = re.compile(r"^[A-Z][A-Za-z0-9_]+$")


def bracket_name(content: str, *, pg_mode: bool = False) -> str:
    """Spelling of the name inside `[...]` (`]]` is an escaped `]`).

    Plain names come out bare. A name that must stay quoted (reserved word,
    spaces/symbols) is, in T-SQL input, left in BRACKETS -- lower-cased for a
    reserved word -- because sqlglot's T-SQL reader refuses some double-quoted
    aliases (`AS "Is Full Day"`) and emits brackets as the quoted form anyway.
    In pg_mode (no sqlglot pass) the PostgreSQL quotes are written directly."""
    name = content.replace("]]", "]")
    if _TEMP_NAME_RE.match(name):
        return name
    if _BARE_NAME_RE.match(name):
        low = name.lower()
        if low in _PG_NEEDS_QUOTES or low in _TSQL_RESERVED:
            return f'"{low}"' if pg_mode else f"[{low}]"
        return name
    if pg_mode:
        return '"' + name.replace('"', '""') + '"'
    return "[" + name.replace("]", "]]") + "]"


def _bracket_close(sql: str, masked: str, start: int) -> int:
    """Index of the `]` closing the `[` at `start` on the same line, -1 if none.
    `]]` inside the brackets is an escaped bracket, not the close."""
    j = start + 1
    while j < len(sql):
        ch = masked[j]
        if ch == "\n":
            return -1
        if ch == "]":
            if j + 1 < len(sql) and masked[j + 1] == "]":
                j += 2
                continue
            return j
        j += 1
    return -1


def rewrite_bracket_identifiers(sql: str, *, pg_mode: bool = False) -> tuple[str, int]:
    """Replace T-SQL `[name]` delimiters, outside literals and comments.

    `[@Placeholder]` is left to A010 (it must stay a quoted schema). In
    pg_mode (already-converted PostgreSQL, PC10) `[` is also PostgreSQL's
    array syntax, so only unmistakable T-SQL names are touched: nothing after
    ARRAY, `)` or `]`, nothing that is a number or an expression, and a name
    glued straight onto an identifier (`col[Alias]`) only when it is a
    CamelCase alias rather than a subscript like `arr[i]`.
    """
    masked = code_mask(sql)
    edits: list[tuple[int, int, str]] = []
    i = masked.find("[")
    while i >= 0:
        close = _bracket_close(sql, masked, i)
        if close < 0:
            i = masked.find("[", i + 1)
            continue
        content = sql[i + 1:close]
        before = masked[:i].rstrip()
        prev = masked[i - 1] if i else ""
        glued = bool(prev) and (prev.isalnum() or prev in "_)]\"")
        skip = (not content.strip()
                or content.startswith("@")
                or not re.search(r"[A-Za-z]", content)
                or re.search(r"(?i)\bARRAY$", before))
        if not skip and pg_mode:
            skip = (prev in ")]"
                    or not _PG_MODE_CONTENT_RE.match(content)
                    or (glued and not _GLUED_ALIAS_RE.match(content)))
        if not skip:
            name = bracket_name(content, pg_mode=pg_mode)
            # pg_mode: a `[dbo].` qualifier must go -- PostgreSQL has no dbo
            # schema (in T-SQL input A009 strips it from the AST instead).
            if pg_mode and content.lower() == "dbo" and masked[close + 1:close + 2] == ".":
                edits.append((i, close + 2, ""))
            else:
                new = (" " if glued else "") + name
                if new != sql[i:close + 1]:
                    edits.append((i, close + 1, new))
        i = masked.find("[", close + 1)
    if not edits:
        return sql, 0
    return _apply_edits(sql, edits), len(edits)


_STRING_ALIAS_RE = re.compile(r"(?i)\bAS\s+'")


def rewrite_string_aliases(sql: str) -> tuple[str, int]:
    """v2.16.0: T-SQL's string-literal column alias `x AS 'CreatedBy'` is spelled
    like a [bracket] name. sqlglot emitted it as the quoted "CreatedBy", which a
    later bare `SELECT CreatedBy FROM #t` (folded to createdby) cannot find."""
    masked = code_mask(sql)
    edits: list[tuple[int, int, str]] = []
    for m in _STRING_ALIAS_RE.finditer(masked):
        open_ = m.end() - 1
        close = masked.find("'", open_ + 1)
        if close < 0:
            continue
        content = sql[open_ + 1:close].replace("''", "'")
        if not content.strip() or not re.search(r"[A-Za-z]", content) or "]" in content:
            continue
        # the quote was the delimiter in `AS 'Name'from` -- keep the words apart
        glued = close + 1 < len(sql) and (sql[close + 1].isalnum() or sql[close + 1] in "_@#")
        edits.append((open_, close + 1, bracket_name(content) + (" " if glued else "")))
    if not edits:
        return sql, 0
    return _apply_edits(sql, edits), len(edits)


# ==========================================================================
# PC12 -- bare SPLIT(x, sep) left in converted PostgreSQL
# ==========================================================================
# Earlier conversions stripped `dbo.` from the T-SQL table function
# `dbo.Split(x, ',')` and kept the call. PostgreSQL has no split() (A022 maps
# the T-SQL form when converting; this repairs code converted before that).
# dbo.Split returned an `items` column, trimmed, without empty slices, so a
# trailing comma never reached a CAST(items AS INT) -- the replacement keeps
# all three properties and the alias the query already uses.

_SPLIT_CALL_RE = re.compile(r"(?i)\b(FROM|JOIN)(\s+(?:LATERAL\s+)?)(?:dbo\s*\.\s*)?split\s*\(")
_ALIAS_AFTER_RE = re.compile(r"^\s*(?:AS\s+)?([A-Za-z_]\w*)")
_NOT_AN_ALIAS = frozenset("""where inner left right full cross join on group order union except intersect
limit offset and or having window fetch for returning natural using select with lateral""".split())


def rewrite_split_calls(sql: str) -> tuple[str, int]:
    masked = code_mask(sql)
    edits = []
    for m in _SPLIT_CALL_RE.finditer(masked):
        close = match_paren(masked, m.end())
        if close < 0:
            continue
        args = split_top_level(sql[m.end():close], masked[m.end():close])
        if len(args) != 2:
            continue
        alias = _ALIAS_AFTER_RE.match(masked[close + 1:])
        has_alias = bool(alias) and alias.group(1).lower() not in _NOT_AN_ALIAS
        new = (f"{m.group(1)}{m.group(2)}(SELECT BTRIM(s.v) AS items FROM STRING_TO_TABLE("
               f"CAST({args[0]} AS TEXT), {args[1]}) AS s(v) WHERE BTRIM(s.v) <> '')"
               + ("" if has_alias else " AS split"))
        edits.append((m.start(), close + 1, new))
    if not edits:
        return sql, 0
    return _apply_edits(sql, edits), len(edits)


# ==========================================================================
# '%@Param%' -- a parameter written INSIDE a string literal (A052 / PC11 / L030)
# ==========================================================================
# ACS's QueryParameterProcessor binds a quoted parameter only when the literal
# is EXACTLY '@Name' (regex '@(\w+)' between quotes). '%@searchText%' is never
# bound: the filter compares against the text '@searchText' and matches
# nothing -- no error on either database. The LIKE-wildcard forms have one
# obvious meaning and are rewritten to a bound concatenation; anything else
# mixing text and a parameter is reported (L030). Names the processor
# replaces as plain TEXT before binding (@UserId, the @...dbName family) do
# work inside a literal and are left alone, as are e-mail-like texts.

_STATIC_TEXT_PARAMS = frozenset(n.lower() for n in (
    "DBName", "LogdbName", "HangdbName", "TSMdbName", "LCCTdbName", "EDIdbName", "PCSdbName",
    "WFdbName", "EXPdbName", "IMPdbName", "TarifdbName", "PassengerdbName", "MasterdbName",
    "PGdbName", "RIdbName", "PCS1dbName", "PCS1MobiledbName", "CommonMobiledbName",
    "HospitalitydbName", "contentmgtdbName", "UserId", "ScreenId", "ScreenName", "RoleId",
    "Community_Admin_OrgId"))
_WILDCARD_PARAM_RE = re.compile(r"^(%?)@([A-Za-z_][A-Za-z0-9_]*)(%?)$")
_PARAM_IN_TEXT_RE = re.compile(r"(?<![\w@])@([A-Za-z_][A-Za-z0-9_]*)(?![\w.])")
_EXACT_PARAM_RE = re.compile(r"^@[A-Za-z_][A-Za-z0-9_]*$")


def _string_literals(sql: str, masked: str):
    """(start, end, content) of every top-level '...' literal (not in comments)."""
    i, n = 0, len(sql)
    while i < n:
        if masked[i] == "'":
            j = i + 1
            while j < n:
                if sql[j] == "'":
                    if j + 1 < n and sql[j + 1] == "'":
                        j += 2
                        continue
                    break
                j += 1
            prefix_n = i > 0 and sql[i - 1] in "Nn" and (i < 2 or not (sql[i - 2].isalnum() or sql[i - 2] == "_"))
            if not prefix_n:
                yield i, j + 1, sql[i + 1:j]
            i = j + 1
            continue
        i += 1


def rewrite_param_literals(sql: str, concat: str) -> tuple[str, int]:
    """'%@x%' / '@x%' / '%@x' -> '%' <concat> @x <concat> '%'."""
    masked = code_mask(sql)
    edits = []
    for start, end, content in _string_literals(sql, masked):
        m = _WILDCARD_PARAM_RE.match(content)
        if not m or not (m.group(1) or m.group(3)) or m.group(2).lower() in _STATIC_TEXT_PARAMS:
            continue
        parts = (["'%'"] if m.group(1) else []) + ["@" + m.group(2)] + (["'%'"] if m.group(3) else [])
        edits.append((start, end, f" {concat} ".join(parts)))
    if not edits:
        return sql, 0
    return _apply_edits(sql, edits), len(edits)


def unbound_param_texts(sql: str) -> list[str]:
    """L030: literals that mix text and a @param the processor will not bind."""
    found = []
    for _start, _end, content in _string_literals(sql, code_mask(sql)):
        if _EXACT_PARAM_RE.match(content) or _WILDCARD_PARAM_RE.match(content):
            continue
        names = [n for n in _PARAM_IN_TEXT_RE.findall(content)
                 if n.lower() not in _STATIC_TEXT_PARAMS]
        if names:
            found.append("'" + content[:40] + "'")
    return found


# ==========================================================================
# Conversion context -- carries placeholders from pre_parse to post_generate
# ==========================================================================

PLACEHOLDER_PREFIX = "MSSQLTOPG_"


@dataclass
class Context:
    # MSSQLTOPG_FN_n(args...)  -> name(args..., <extra>)
    functions: dict[int, tuple[str, str]] = field(default_factory=dict)
    # MSSQLTOPG_TVF_n()        -> (<subquery text>)
    tables: dict[int, str] = field(default_factory=dict)
    applied: list[Applied] = field(default_factory=list)
    warnings: list[Warning] = field(default_factory=list)
    counter: int = 0

    def next_id(self) -> int:
        self.counter += 1
        return self.counter

    def note(self, code: str, count: int, text: str) -> None:
        if count:
            self.applied.append(Applied(code, count, text))

    def warn(self, code: str, severity: Severity, text: str) -> None:
        self.warnings.append(Warning(code, severity, text))


# ==========================================================================
# PRE-PARSE (text)
# ==========================================================================

# ---- A029 welded alias: `JOIN t XON X.col` -------------------------------
# `X WITH(NOLOCK)ON X.a` is legal T-SQL (the `)` ends the hint). An earlier
# hint-stripper removed `WITH(NOLOCK)` without leaving a space, welding the
# alias onto ON. The alias must equal the qualifier its own ON clause uses,
# which is what makes the split provably right.
_WELDED_RE = re.compile(
    r"(?i)\b(JOIN|FROM)(\s+[\w.\[\]\"#@]+\s+(?:AS\s+)?)(\w+?)(ON)(\s+)(\w+)(\s*\.)")


def fix_welded_on(sql: str, ctx: Context) -> str:
    masked = code_mask(sql)
    edits = []
    for m in _WELDED_RE.finditer(masked):
        alias, qualifier = m.group(3), m.group(6)
        if alias and alias.lower() == qualifier.lower():
            edits.append((m.start(4), m.start(4), " "))
    if edits:
        ctx.note("A029", len(edits),
                 f"{len(edits)} alias(es) welded onto ON (`XON X.col`) split "
                 f"back into `X ON X.col` -- the alias matched the qualifier "
                 f"its own ON clause uses. Left welded, the query does not parse.")
    return _apply_edits(sql, edits)


# ---- A043 session SET options -------------------------------------------
_SET_OPTION_RE = re.compile(
    r"(?i)(?<![\w@])SET\s+(?:NOCOUNT|ANSI_NULLS|QUOTED_IDENTIFIER|ANSI_WARNINGS|"
    r"ANSI_PADDING|ARITHABORT|XACT_ABORT|CONCAT_NULL_YIELDS_NULL|"
    r"NUMERIC_ROUNDABORT|FMTONLY|IMPLICIT_TRANSACTIONS)\s*=?\s*(?:ON|OFF)\b[ \t]*;?"
    r"|(?<![\w@])SET\s+TRANSACTION\s+ISOLATION\s+LEVEL\s+"
    r"(?:READ\s+UNCOMMITTED|READ\s+COMMITTED|SNAPSHOT|REPEATABLE\s+READ|SERIALIZABLE)[ \t]*;?"
    r"|(?<![\w@])SET\s+(?:DEADLOCK_PRIORITY|LOCK_TIMEOUT)\s+[-\w]+[ \t]*;?")
_SET_DATEFORMAT_RE = re.compile(r"(?i)(?<![\w@])SET\s+(DATEFORMAT|LANGUAGE|DATEFIRST)\b")


_QUERY_HINT_WORDS = (r"RECOMPILE|MAXDOP|OPTIMIZE\s+FOR|FAST|HASH|MERGE|LOOP|ORDER\s+GROUP|"
                     r"CONCAT\s+UNION|FORCE\s+ORDER|KEEP\s+PLAN|KEEPFIXED\s+PLAN|MAXRECURSION|"
                     r"USE\s+HINT|QUERYTRACEON|NO_PERFORMANCE_SPOOL|ROBUST\s+PLAN|EXPAND\s+VIEWS|"
                     r"LABEL|MIN_GRANT_PERCENT|MAX_GRANT_PERCENT|DISABLE_OPTIMIZED_NESTED_LOOP|"
                     r"IGNORE_NONCLUSTERED_COLUMNSTORE_INDEX|PARAMETERIZATION")
_OPTION_RE = re.compile(r"(?i)\bOPTION\s*\(\s*(?:" + _QUERY_HINT_WORDS + r")\b")


def fix_session_options(sql: str, ctx: Context) -> str:
    masked = code_mask(sql)
    options = [(m.start(), m.end(), "") for m in _SET_OPTION_RE.finditer(masked)]
    # OPTION (RECOMPILE / MAXDOP n / ...) query hints: planner directives with no
    # PostgreSQL meaning. sqlglot reports them UNSUPPORTED (ERROR), which
    # blocked otherwise-clean conversions.
    hints = []
    for m in _OPTION_RE.finditer(masked):
        open_at = masked.index("(", m.start())
        close = match_paren(masked, open_at + 1)
        if close > 0:
            hints.append((m.start(), close + 1, ""))
    edits = options + hints
    if hints:
        ctx.note("A043", len(hints),
                 f"{len(hints)} OPTION (...) query hint(s) removed (RECOMPILE / MAXDOP / "
                 f"OPTIMIZE FOR / MAXRECURSION ...). Planner directives with no "
                 f"PostgreSQL equivalent; MAXRECURSION has no limit to enforce.")
    if options:
        ctx.note("A043", len(options),
                 f"{len(options)} T-SQL session option(s) removed (SET NOCOUNT / "
                 f"ANSI_* / XACT_ABORT / TRANSACTION ISOLATION LEVEL ...). "
                 f"sqlglot emits `SET NOCOUNT = ON`, which PostgreSQL rejects "
                 f"at runtime ('unrecognized configuration parameter'); none of "
                 f"them changes query results on PostgreSQL.")
    m = _SET_DATEFORMAT_RE.search(masked)
    if m:
        ctx.warn("L029", Severity.ERROR,
                 f"SET {m.group(1).upper()} changes how T-SQL parses date "
                 f"strings / names days. PostgreSQL's equivalent (DateStyle) "
                 f"is SESSION-wide and would leak across the pooled connection. "
                 f"Remove it and pass dates as typed parameters or ISO literals.")
    return _apply_edits(sql, edits)


# ---- A027 temp-table existence guards -----------------------------------
_TEMP_DROP_GUARD_RES = [
    re.compile(
        r"(?is)\bIF\s+OBJECT_ID\s*\(\s*N?'tempdb\.(?:dbo)?\.(#\w+)'\s*"
        r"(?:,\s*N?'U'\s*)?\)\s+IS\s+NOT\s+NULL\s+(?:BEGIN\s+)?"
        r"DROP\s+TABLE\s+(\#\w+)\s*;?\s*(?:END\b\s*;?)?"),
    re.compile(
        r"(?is)\bIF\s+EXISTS\s*\(\s*SELECT\s+[\w*\s,]+?\s+FROM\s+tempdb\.(?:dbo|sys)?\.?"
        r"(?:sysobjects|objects|tables)\s+WHERE\s+\w+\s*=\s*OBJECT_ID\s*\(\s*"
        r"N?'tempdb\.(?:dbo)?\.(#\w+)'\s*\)\s*\)\s+(?:BEGIN\s+)?"
        r"DROP\s+TABLE\s+(\#\w+)\s*;?\s*(?:END\b\s*;?)?"),
    re.compile(
        r"(?is)\bIF\s+OBJECT_ID\s*\(\s*N?'(#\w+)'\s*(?:,\s*N?'U'\s*)?\)\s+IS\s+NOT\s+NULL\s+"
        r"(?:BEGIN\s+)?DROP\s+TABLE\s+(\#\w+)\s*;?\s*(?:END\b\s*;?)?"),
]


def fix_temp_drop_guards(sql: str, ctx: Context) -> str:
    """The guard's table name lives INSIDE a string literal, so this matches the
    original text and then checks the match starts in code, not in a comment."""
    masked = code_mask(sql)
    edits = []
    for rx in _TEMP_DROP_GUARD_RES:
        for m in rx.finditer(sql):
            if masked[m.start()] != sql[m.start()]:
                continue          # inside a comment
            if m.group(1).lower() != m.group(2).lower():
                continue          # guards one table, drops another: refuse
            if any(s <= m.start() < e for s, e, _ in edits):
                continue
            edits.append((m.start(), m.end(), f"DROP TABLE IF EXISTS {m.group(2)};\n"))
    if edits:
        ctx.note("A027", len(edits),
                 f"{len(edits)} `IF OBJECT_ID('tempdb..#t') IS NOT NULL DROP TABLE #t` "
                 f"guard(s) rewritten as `DROP TABLE IF EXISTS pg_temp.t`. sqlglot "
                 f"cannot represent the IF and returned EMPTY output for it.")
    return _apply_edits(sql, edits)


# ---- A045 table variables -> temp tables --------------------------------
_TABLE_VAR_RE = re.compile(r"(?i)\bDECLARE\s+(@\w+)\s+(?:AS\s+)?TABLE\s*\(")


def fix_table_variables(sql: str, ctx: Context) -> str:
    count = 0
    names = []
    while True:
        masked = code_mask(sql)
        m = _TABLE_VAR_RE.search(masked)
        if not m:
            break
        close = match_paren(masked, m.end())
        if close < 0:
            break
        var = m.group(1)
        temp = "#tv_" + var[1:]
        tail = "" if masked[close + 1:].lstrip().startswith(";") else ";"
        body = sql[m.end():close]
        sql = sql[:m.start()] + f"CREATE TABLE {temp} (" + body + ")" + tail + sql[close + 1:]
        masked = code_mask(sql)
        ref = re.compile(r"(?i)" + re.escape(var) + r"\b")
        sql = _apply_edits(sql, [(r.start(), r.end(), temp) for r in ref.finditer(masked)])
        names.append(var)
        count += 1
    if count:
        ctx.note("A045", count,
                 f"{count} table variable(s) ({', '.join(names)}) turned into temp "
                 f"tables (#tv_<name>, then pg_temp). PostgreSQL has no table "
                 f"variables outside PL/pgSQL; `DECLARE @t TABLE` did not parse.")
    return sql


# ---- A031 inline single-assignment DECLAREd scalars ----------------------
_DECLARE_RE = re.compile(
    r"(?i)\bDECLARE\s+(@\w+)\s+(?:AS\s+)?([A-Za-z_]\w*(?:\s*\(\s*(?:\d+|MAX)\s*(?:,\s*\d+\s*)?\))?)")
_CONTROL_RE = re.compile(r"(?i)\b(IF|WHILE|BEGIN|GOTO|RETURN|CURSOR)\b")
_DML_RE = re.compile(r"(?i)\b(INSERT|UPDATE|DELETE|MERGE|TRUNCATE|CREATE|DROP|ALTER|EXEC|EXECUTE)\b")


def _statement_end(masked: str, start: int) -> int:
    """End (exclusive) of the expression/statement beginning at `start`:
    the first `;` at depth 0, or the end of a line at depth 0, or the end."""
    depth = 0
    i, n = start, len(masked)
    while i < n:
        ch = masked[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            if depth == 0:
                return i
            depth -= 1
        elif depth == 0 and ch == ";":
            return i
        elif depth == 0 and ch == "\n":
            rest = masked[i + 1:].lstrip()
            done = masked[start:i].rstrip()
            # a continuation line (operator / closing) keeps the expression open
            if rest[:1] in ("+", "-", "*", "/", ",", ")") or re.match(
                    r"(?i)(AND|OR|THEN|ELSE|WHEN|END)\b", rest) or \
                    done[-1:] in ("+", "-", "*", "/", ",", "(", "=") or \
                    re.search(r"(?i)\b(AND|OR|THEN|ELSE|WHEN|CASE)$", done):
                i += 1
                continue
            return i
        i += 1
    return n


def _semicolon_end(masked: str, start: int) -> int:
    """The first `;` at parenthesis depth 0 after `start` (or the end of the text)."""
    depth = 0
    for i in range(start, len(masked)):
        ch = masked[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch == ";" and depth == 0:
            return i
    return len(masked)


def _top_level_from(masked: str) -> int | None:
    depth = 0
    for m in re.finditer(r"\(|\)|\bFROM\b", masked, re.I):
        if m.group(0) == "(":
            depth += 1
        elif m.group(0) == ")":
            depth -= 1
        elif depth == 0:
            return m.start()
    return None


def _reads_changed_rows(value_mask: str, masked: str, start: int) -> bool:
    """Would an inlined subquery (evaluated where the variable is USED) see different rows than at the
    assignment? -- True when the batch writes, after `start`, to a table the subquery reads."""
    from core import procedural
    read = {procedural._name(m.group(1)) for m in procedural._READ_SOURCE.finditer(value_mask)}
    aliases = {m.group(2).lower(): procedural._name(m.group(1))
               for m in procedural._READ_SOURCE.finditer(masked) if m.group(2)}
    written = set()
    for m in procedural._WRITE_TARGET.finditer(masked, start):
        target = procedural._name(m.group(1))
        written.add(aliases.get(target, target))
    return bool(read & written) or not read


def _has_control_flow(masked: str) -> bool:
    """IF / WHILE / BEGIN ... -- but not the IF of `DROP TABLE IF EXISTS` (A027's output)."""
    return any(not (m.group(1).upper() == "IF"
                    and re.search(r"(?i)\b(TABLE|INDEX|VIEW|SCHEMA)\s+$", masked[:m.start()]))
               for m in _CONTROL_RE.finditer(masked))


def fix_if_blocks(sql: str, ctx: Context) -> str:
    """A058 -- IF / ELSE flattened into guarded statements (core/procedural)."""
    from core import procedural
    masked = code_mask(sql)
    if not re.search(r"(?i)\bIF\b", masked) or not _has_control_flow(masked):
        if re.search(r"(?i)\bDECLARE\s+@|\bSET\s+@|\bSELECT\s+@\w+\s*=", masked):
            # variables in a batch without terminators: mark the statement ends for A031
            sql, ok = procedural.terminate(sql, masked)
            ctx.terminated = ok           # type: ignore[attr-defined]
        return sql
    sql, count, refused = procedural.flatten(sql, masked)
    ctx.terminated = bool(count)          # type: ignore[attr-defined]
    if count:
        ctx.note("A058", count,
                 f"{count} IF / ELSE block(s) flattened: each branch's statements carry the "
                 f"branch condition as a guard (`CASE WHEN (c) THEN 1 ELSE 0 END = 1` / `= 0`, "
                 f"so a NULL condition takes ELSE as in T-SQL) and the result sets the branches "
                 f"return at the same position are joined with UNION ALL -- only the taken "
                 f"branch contributes rows. Temp tables created inside a branch are created "
                 f"(empty) even when it is not taken. VERIFY the branches' columns have the "
                 f"same types.")
    elif refused:
        ctx.refused_if = refused          # type: ignore[attr-defined]
    return sql


def fix_inline_declares(sql: str, ctx: Context) -> str:
    masked = code_mask(sql)
    if _has_control_flow(masked):
        return sql            # procedural batch: a hand rewrite anyway
    inlined: list[str] = []
    skipped: set[str] = set()
    while True:
        masked = code_mask(sql)
        found = None
        for m in _DECLARE_RE.finditer(masked):
            if m.group(1).lower() not in skipped:
                found = m
                break
        if not found:
            break
        var, vtype = found.group(1), sql[found.start(2):found.end(2)]
        after = found.end()
        rest = masked[after:]
        init_text = None
        remove: list[tuple[int, int]] = []
        eq = re.match(r"\s*=", rest)
        init_start = after + (eq.end() if eq else 0)
        decl_end = _statement_end(masked, init_start)
        if re.match(r"\s*,", rest) or len(split_top_level(
                sql[init_start:decl_end], masked[init_start:decl_end])) > 1:
            skipped.add(var.lower())      # DECLARE @a INT = 1, @b ... -- refuse
            continue
        stmt_end = decl_end + (1 if masked[decl_end:decl_end + 1] == ";" else 0)
        sets = list(re.finditer(r"(?i)\bSET\s+" + re.escape(var) + r"\s*=", masked))
        selects = list(re.finditer(r"(?i)\bSELECT\s+" + re.escape(var) + r"\s*=", masked))
        uses = [r.start() for r in re.finditer(r"(?i)" + re.escape(var) + r"\b", masked)
                if r.start() > found.end() and not any(
                    s.start() <= r.start() < s.end() for s in sets + selects)]
        assigned_at = after
        if eq and not sets and not selects:
            init_text = sql[init_start:decl_end].strip()
            remove.append((found.start(), stmt_end))
        elif len(sets) == 1 and not selects and (
                not eq or not any(u < sets[0].start() for u in uses)):
            # v2.16.0: `DECLARE @v T = '' ... SET @v = '@Param'` -- the default is never read
            s = sets[0]
            s_end = (_semicolon_end(masked, s.end()) if getattr(ctx, "terminated", False)
                     else _statement_end(masked, s.end()))
            init_text = sql[s.end():s_end].strip()
            if re.search(r"(?i)" + re.escape(var) + r"\b", masked[s.end():s_end]):
                skipped.add(var.lower())          # SET @v = @v + ... reads the default
                continue
            assigned_at = s.start()
            remove.append((found.start(), stmt_end))
            remove.append((s.start(), s_end + (1 if masked[s_end:s_end + 1] == ";" else 0)))
        elif len(selects) == 1 and not sets and not eq and getattr(ctx, "terminated", False):
            # v2.16.0: `SELECT @v = expr FROM ...` -- the value of a one-row subquery. Only in a
            # batch whose statement ends are known (`;`): a line-based end would cut the FROM off
            s = selects[0]
            s_end = _semicolon_end(masked, s.end())
            body, body_mask = sql[s.end():s_end], masked[s.end():s_end]
            if len(split_top_level(body, body_mask)) > 1 or re.search(r"(?i)\bORDER\s+BY\b", body_mask):
                skipped.add(var.lower())          # SELECT @a = x, @b = y / last-row ORDER BY
                continue
            frm = _top_level_from(body_mask)
            init_text = (f"(SELECT TOP 1 {body[:frm].strip()} {body[frm:].strip()})" if frm is not None
                         else body.strip())
            assigned_at = s.start()
            remove.append((found.start(), stmt_end))
            remove.append((s.start(), s_end + (1 if masked[s_end:s_end + 1] == ";" else 0)))
        else:
            skipped.add(var.lower())
            continue
        # any other assignment to the variable -> refuse
        assign = re.compile(r"(?i)(\bSET\s+|\bSELECT\s+(?:[^;]*?,\s*)?)" + re.escape(var)
                            + r"\s*[-+*/]?=|" + re.escape(var) + r"\s+OUTPUT\b")
        others = [a for a in assign.finditer(masked)
                  if not any(s <= a.start() < e for s, e in remove)]
        if not init_text or others or (
                re.search(r"(?i)\bSELECT\b", code_mask(init_text))
                and _reads_changed_rows(code_mask(init_text), masked, assigned_at)):
            # re-assigned, or a subquery value could read rows the batch changes
            skipped.add(var.lower())
            continue
        replacement = f"CAST(({init_text}) AS {vtype})"
        ref = re.compile(r"(?i)" + re.escape(var) + r"\b")
        edits = [(r.start(), r.end(), replacement) for r in ref.finditer(masked)
                 if not any(s <= r.start() < e for s, e in remove)]
        edits += [(s, e, "") for s, e in remove]
        sql = _apply_edits(sql, edits)
        inlined.append(var)
    if inlined:
        ctx.note("A031", len(inlined),
                 f"{len(inlined)} DECLAREd scalar(s) assigned exactly once "
                 f"({', '.join(inlined)}) inlined as CAST((value) AS type). "
                 f"PostgreSQL has no variables outside PL/pgSQL. Refused when "
                 f"the variable is re-assigned, the batch has control flow, or a "
                 f"subquery value could read rows the batch itself modifies.")
    return sql


# ---- A046 CONVERT(type, expr, style) -- an explicit, verified style table --
# sqlglot's own mapping emits `%p` (100/109), microseconds where T-SQL shows
# milliseconds (113/114/121/126), `DDTHH` -- PostgreSQL reads that `TH` as an
# ORDINAL SUFFIX (126/127) -- and the literal mask 'None' (127/131). All of
# those parse. None of them is right.
CONVERT_STYLES: dict[int, tuple[str, bool]] = {
    # style: (PostgreSQL mask, exact?)  -- exact=False means disclosed approx
    0: ("Mon DD YYYY HH12:MIAM", False), 100: ("Mon DD YYYY HH12:MIAM", False),
    1: ("MM/DD/YY", True), 101: ("MM/DD/YYYY", True),
    2: ("YY.MM.DD", True), 102: ("YYYY.MM.DD", True),
    3: ("DD/MM/YY", True), 103: ("DD/MM/YYYY", True),
    4: ("DD.MM.YY", True), 104: ("DD.MM.YYYY", True),
    5: ("DD-MM-YY", True), 105: ("DD-MM-YYYY", True),
    6: ("DD Mon YY", True), 106: ("DD Mon YYYY", True),
    7: ("Mon DD, YY", True), 107: ("Mon DD, YYYY", True),
    8: ("HH24:MI:SS", True), 108: ("HH24:MI:SS", True), 24: ("HH24:MI:SS", True),
    9: ("Mon DD YYYY HH12:MI:SS:MSAM", False), 109: ("Mon DD YYYY HH12:MI:SS:MSAM", False),
    10: ("MM-DD-YY", True), 110: ("MM-DD-YYYY", True),
    11: ("YY/MM/DD", True), 111: ("YYYY/MM/DD", True),
    12: ("YYMMDD", True), 112: ("YYYYMMDD", True),
    13: ("DD Mon YYYY HH24:MI:SS:MS", True), 113: ("DD Mon YYYY HH24:MI:SS:MS", True),
    14: ("HH24:MI:SS:MS", True), 114: ("HH24:MI:SS:MS", True),
    20: ("YYYY-MM-DD HH24:MI:SS", True), 120: ("YYYY-MM-DD HH24:MI:SS", True),
    21: ("YYYY-MM-DD HH24:MI:SS.MS", True), 121: ("YYYY-MM-DD HH24:MI:SS.MS", True),
    25: ("YYYY-MM-DD HH24:MI:SS.MS", True),
    22: ("MM/DD/YY HH12:MI:SS AM", True),
    23: ("YYYY-MM-DD", True),
    126: ('YYYY-MM-DD"T"HH24:MI:SS.MS', False), 127: ('YYYY-MM-DD"T"HH24:MI:SS.MS', False),
}
# ISO shapes PostgreSQL parses natively -- a plain CAST is exact for them
_ISO_PARSE_STYLES = {20, 120, 21, 121, 23, 25, 126, 127, 12, 112}
_CHAR_TYPE_RE = re.compile(r"(?i)^\s*(N?VARCHAR|N?CHAR|VARCHAR2|TEXT|NTEXT)\s*(\(\s*(\d+|MAX)\s*\))?\s*$")
_DATE_TYPE_RE = re.compile(r"(?i)^\s*(DATE|DATETIME|DATETIME2|SMALLDATETIME|TIME)\s*(\(\s*\d+\s*\))?\s*$")
_CONVERT_CALL_RE = re.compile(r"(?i)(?<![\w.])CONVERT\s*\(")
_SIMPLE_VALUE_RE = re.compile(r"(?s)^\s*(@\w+|N?'(?:[^']|'')*')\s*$")


def fix_convert_styles(sql: str, ctx: Context) -> str:
    exact = approx = parse_cast = 0
    refused: list[str] = []
    ignored = 0
    while True:
        masked = code_mask(sql)
        target = None
        for m in _CONVERT_CALL_RE.finditer(masked):
            close = match_paren(masked, m.end())
            if close < 0:
                continue
            args = split_top_level(sql[m.end():close], masked[m.end():close])
            if len(args) == 3 and re.fullmatch(r"\d+", args[2]):
                target = (m, close, args)
                break
        if not target:
            break
        m, close, (ctype, expr, style_s) = target
        style = int(style_s)
        mapping = CONVERT_STYLES.get(style)
        if _CHAR_TYPE_RE.match(ctype):
            if mapping is None:
                refused.append(style_s)
                new = f"MSSQLTOPG_KEEP_CONVERT({ctype}, {expr}, {style_s})"
            elif re.match(r"(?s)^\s*N?'", expr):
                ignored += 1          # style is ignored for string input
                new = f"CAST({expr} AS {ctype})"
            else:
                fid = ctx.next_id()
                ctx.functions[fid] = ("TO_CHAR", _sql_string(mapping[0]))
                new = f"CAST(MSSQLTOPG_FN_{fid}({expr}) AS {ctype})"
                if mapping[1]:
                    exact += 1
                else:
                    approx += 1
        elif _DATE_TYPE_RE.match(ctype):
            dtype = _DATE_TYPE_RE.match(ctype).group(1).upper()
            if not _SIMPLE_VALUE_RE.match(expr) and not re.search(
                    r"(?i)\bCAST\s*\(.*\bAS\s+N?VARCHAR", expr):
                # A column/expression: T-SQL ignores the style if it is already
                # a date/time, but PARSES with it if it is text. Which one is a
                # schema question -- left for R003 (check_source) to report.
                refused.append(style_s)
                new = f"MSSQLTOPG_KEEP_CONVERT({ctype}, {expr}, {style_s})"
            elif style in _ISO_PARSE_STYLES:
                parse_cast += 1
                new = f"CAST({expr} AS {ctype})"
            elif mapping is None:
                refused.append(style_s)
                new = f"MSSQLTOPG_KEEP_CONVERT({ctype}, {expr}, {style_s})"
            else:
                fid = ctx.next_id()
                fn = "TO_DATE" if dtype == "DATE" else "TO_TIMESTAMP"
                ctx.functions[fid] = (fn, _sql_string(mapping[0]))
                new = f"MSSQLTOPG_FN_{fid}({expr})"
                if fn == "TO_TIMESTAMP":
                    new = f"CAST({new} AS {ctype})"
                if mapping[1]:
                    exact += 1
                else:
                    approx += 1
        else:
            new = f"MSSQLTOPG_KEEP_CONVERT({ctype}, {expr}, {style_s})"
            ignored += 1
        sql = sql[:m.start()] + new + sql[close + 1:]
    sql = re.sub(r"MSSQLTOPG_KEEP_CONVERT\(", "CONVERT(", sql)
    total = exact + approx + parse_cast + ignored
    if total:
        ctx.note("A046", total,
                 f"{total} CONVERT(type, x, style) call(s) converted from an "
                 f"explicit T-SQL style table: {exact} exact TO_CHAR/TO_DATE "
                 f"mask(s), {parse_cast} ISO parse(s) as plain CAST, {ignored} "
                 f"where T-SQL itself ignores the style (string literal -> string)"
                 + (f", {approx} APPROXIMATE (styles 0/100/9/109 space-pad the "
                    f"day and hour, 126/127 drop a zero millisecond part -- "
                    f"PostgreSQL zero-pads / always prints .MS)" if approx else "")
                 + ". sqlglot's own mapping emitted '%p', microseconds for "
                   "milliseconds, 'None', and 'DDTHH' (TH = ordinal suffix). "
                   "Refused shapes stay as CONVERT and R003 reports them.")
    return sql


# ---- A026 FORMAT(x, mask) -------------------------------------------------
_FORMAT_CALL_RE = re.compile(r"(?i)(?<![\w.])FORMAT\s*\(")
_NUM_STANDARD_RE = re.compile(r"^([NnFfDdCcPpGgEeXxRr])(\d{0,2})$")
_NUM_CUSTOM_RE = re.compile(r"^[#0,.]+$")
_DOTNET_DATE_TOKENS = [
    ("yyyy", "YYYY"), ("yyy", "YYYY"), ("yy", "YY"), ("y", "FMYY"),
    ("MMMM", "FMMonth"), ("MMM", "Mon"), ("MM", "MM"), ("M", "FMMM"),
    ("dddd", "FMDay"), ("ddd", "Dy"), ("dd", "DD"), ("d", "FMDD"),
    ("HH", "HH24"), ("H", "FMHH24"), ("hh", "HH12"), ("h", "FMHH12"),
    ("mm", "MI"), ("m", "FMMI"), ("ss", "SS"), ("s", "FMSS"),
    ("fff", "MS"), ("ff", "FF2"), ("f", "FF1"),
    ("tt", "AM"), ("t", "A"),
]


def _numeric_mask(mask: str) -> str | None:
    """.NET numeric format -> PostgreSQL TO_CHAR mask, or None (refuse)."""
    m = _NUM_STANDARD_RE.match(mask)
    if m:
        kind, digits = m.group(1).upper(), m.group(2)
        if kind == "D" and not digits:
            return None       # bare 'd'/'D' is the culture short/long DATE pattern
        if kind == "N":
            d = int(digits) if digits else 2
            return "FM999,999,999,999,990" + ("." + "0" * d if d else "")
        if kind == "F":
            d = int(digits) if digits else 2
            return "FM999999999999990" + ("." + "0" * d if d else "")
        if kind == "D":
            n = int(digits)
            return "FM" + "9" * max(0, 15 - n) + "0" * max(n, 1)
        return None           # C (currency), P (percent), E, G, X, R: culture/format specific
    if _NUM_CUSTOM_RE.match(mask) and ("0" in mask or "#" in mask):
        if mask.count(".") > 1:
            return None
        int_part, _, frac = mask.partition(".")
        grouping = "," in int_part
        zeros = int_part.replace(",", "").count("0")
        width = 15
        digits = ["9"] * (width - zeros) + ["0"] * zeros
        if not zeros:
            digits[-1] = "0" if "0" in int_part else "9"
        if grouping:
            out = ""
            for idx, ch in enumerate(reversed(digits)):
                if idx and idx % 3 == 0:
                    out = "," + out
                out = ch + out
            int_mask = out
        else:
            int_mask = "".join(digits)
        frac_mask = "".join("0" if c == "0" else "9" for c in frac if c in "0#")
        return "FM" + int_mask + ("." + frac_mask if frac_mask else "")
    return None


def _date_mask(mask: str) -> str | None:
    """.NET date/time custom format -> PostgreSQL mask, or None (refuse)."""
    if len(mask) == 1:
        return None           # standard date formats (d, D, g, G ...) are culture-specific
    out, i, n = [], 0, len(mask)
    literal: list[str] = []

    def flush() -> None:
        if literal:
            text = "".join(literal)
            out.append('"' + text.replace('"', '') + '"' if re.search(r"[A-Za-z]", text) else text)
            literal.clear()

    while i < n:
        ch = mask[i]
        if ch == "\\" and i + 1 < n:
            literal.append(mask[i + 1])
            i += 2
            continue
        if ch in "'\"":
            j = mask.find(ch, i + 1)
            if j < 0:
                return None
            literal.append(mask[i + 1:j])
            i = j + 1
            continue
        for token, pg in _DOTNET_DATE_TOKENS:
            if mask.startswith(token, i):
                flush()
                out.append(pg)
                i += len(token)
                break
        else:
            if ch in "zKgF":
                return None   # offsets / era / culture -- refuse
            literal.append(ch)
            i += 1
    flush()
    return "".join(out)


def fix_format_calls(sql: str, ctx: Context) -> str:
    num = date = param = 0
    refused: list[str] = []
    guard = 0
    while guard < 500:
        guard += 1
        masked = code_mask(sql)
        m = _FORMAT_CALL_RE.search(masked)
        if not m:
            break
        close = match_paren(masked, m.end())
        if close < 0:
            break
        args = split_top_level(sql[m.end():close], masked[m.end():close])
        if len(args) not in (2, 3):
            sql = sql[:m.start()] + "MSSQLTOPG_KEEP_FORMAT(" + sql[m.end():]
            continue
        expr, mask_arg = args[0], args[1]
        lit = re.fullmatch(r"(?s)N?'((?:[^']|'')*)'", mask_arg)
        fid = ctx.next_id()
        if lit and re.fullmatch(r"@\w+", lit.group(1)):
            # '@TimeFormat' -- a placeholder QueryParameterProcessor substitutes
            # into the text at runtime: the mask is dynamic, like a parameter.
            param += 1
            ctx.functions[fid] = ("TO_CHAR", "")
            new = f"MSSQLTOPG_FN_{fid}({expr}, {mask_arg})"
        elif lit:
            dotnet = lit.group(1).replace("''", "'")
            pg = _numeric_mask(dotnet)
            if pg is not None:
                num += 1
            else:
                pg = _date_mask(dotnet)
                if pg is not None:
                    date += 1
            if pg is None:
                refused.append(dotnet)
                sql = sql[:m.start()] + "MSSQLTOPG_KEEP_FORMAT(" + sql[m.end():]
                continue
            ctx.functions[fid] = ("TO_CHAR", _sql_string(pg))
            new = f"MSSQLTOPG_FN_{fid}({expr})"
        else:
            param += 1
            ctx.functions[fid] = ("TO_CHAR", "")
            new = f"MSSQLTOPG_FN_{fid}({expr}, {mask_arg})"
        sql = sql[:m.start()] + new + sql[close + 1:]
    sql = re.sub(r"MSSQLTOPG_KEEP_FORMAT\(", "FORMAT(", sql)
    if num or date:
        ctx.note("A026", num + date,
                 f"{num + date} FORMAT(x, '<.NET mask>') call(s) -> TO_CHAR with a "
                 f"translated PostgreSQL mask ({num} numeric, {date} date/time). "
                 f"sqlglot emitted NUMBER_TO_STR -- a function that exists in no "
                 f"PostgreSQL install -- and mistranslated 'tt' and 'THH'. 'N2' "
                 f"-> 'FM999,999,999,999,990.00' keeps the thousands separators.")
    if param:
        ctx.warn("L024", Severity.WARNING,
                 f"{param} FORMAT(x, <non-literal mask>) became TO_CHAR(x, <same "
                 f"argument>). The mask value must now be a POSTGRESQL pattern: a "
                 f".NET mask passed from C# gives silently wrong output ('mm' is "
                 f"MONTH to PostgreSQL, 'HH:mm' -> '09:08'). Translate it in C#. "
                 f"(sqlglot had replaced the argument with a fabricated '%B %-d'.)")
    for dotnet in sorted(set(refused)):
        ctx.warn("A026", Severity.ERROR,
                 f"FORMAT(x, '{dotnet}') left as is -- currency/percent/"
                 f"culture-specific .NET formats have no faithful TO_CHAR mask. "
                 f"Convert by hand.")
    return sql


# ---- A039b DELETE ... OUTPUT DELETED.x -----------------------------------
# sqlglot cannot parse OUTPUT in a DELETE at all (SG-PARSE, whole batch lost).
_DELETE_OUTPUT_RE = re.compile(
    r"(?is)\bOUTPUT\s+((?:DELETED\.(?:\w+|\*)(?:\s+AS\s+\w+)?\s*,\s*)*"
    r"DELETED\.(?:\w+|\*)(?:\s+AS\s+\w+)?)(?=\s)")


def fix_delete_output(sql: str, ctx: Context) -> str:
    count = 0
    while True:
        masked = code_mask(sql)
        m = _DELETE_OUTPUT_RE.search(masked)
        if not m:
            break
        head = masked[:m.start()]
        stmt = list(re.finditer(r"(?i)\b(DELETE|UPDATE|INSERT|SELECT|MERGE)\b", head))
        if not stmt or stmt[-1].group(1).upper() != "DELETE":
            break
        if re.match(r"(?is)\s*INTO\b", masked[m.end():]):
            break             # OUTPUT ... INTO @t -- refuse
        end = _statement_end(masked, m.end())
        if end < len(masked) and masked[end] != ";":
            break
        cols = re.sub(r"(?i)\bDELETED\.", "", sql[m.start(1):m.end(1)])
        sql = (sql[:m.start()] + sql[m.end():end].rstrip()
               + f" RETURNING {cols}" + sql[end:])
        count += 1
    if count:
        ctx.note("A039", count,
                 f"{count} DELETE ... OUTPUT DELETED.x moved to RETURNING x "
                 f"(sqlglot cannot parse OUTPUT in a DELETE).")
    return sql


# ---- A028 OPENJSON -> jsonb set functions --------------------------------
_OPENJSON_RE = re.compile(r"(?i)\bOPENJSON\s*\(")
_JSON_SRC_RE = re.compile(r"^\s*(@\w+|\[?\w+\]?(?:\.\[?\w+\]?)?)\s*$")
_JSON_PATH_RE = re.compile(r"(?i)^\s*N?'\s*(?:lax\s+|strict\s+)?\$((?:\.[\w]+|\.\"[^\"]+\")*)\s*'\s*$")


def _json_path_keys(path: str) -> list[str] | None:
    m = _JSON_PATH_RE.match(path)
    if not m:
        return None
    return [k.strip('"') for k in re.findall(r"\.(\"[^\"]+\"|\w+)", m.group(1))]


def _pg_type(tsql_type: str) -> str | None:
    try:
        out = sqlglot.transpile(f"SELECT CAST(x AS {tsql_type})", read="tsql",
                                write="postgres")[0]
    except Exception:
        return None
    m = re.match(r"(?is)SELECT CAST\(x AS (.+)\)$", out)
    if not m:
        return None
    pg = m.group(1).strip()
    pg = re.sub(r"(?i)^UTINYINT$", "SMALLINT", pg)
    pg = re.sub(r"(?i)^BIT$", "BOOLEAN", pg)
    return pg


def _jsonb_array_source(src: str, keys: list[str]) -> str:
    base = f"CAST({src} AS TEXT)"
    node = f"CAST({base} AS JSONB)"
    if keys:
        node += " #> " + _sql_string("{" + ",".join(keys) + "}")
    return (f"CASE WHEN {base} IS NOT JSON THEN '[]'::jsonb "
            f"WHEN jsonb_typeof({node}) = 'array' THEN {node} "
            f"WHEN jsonb_typeof({node}) = 'object' THEN jsonb_build_array({node}) "
            f"ELSE '[]'::jsonb END")


def fix_openjson(sql: str, ctx: Context) -> str:
    done = 0
    refused = 0
    guard = 0
    while guard < 200:
        guard += 1
        masked = code_mask(sql)
        m = _OPENJSON_RE.search(masked)
        if not m:
            break
        close = match_paren(masked, m.end())
        if close < 0:
            break
        args = split_top_level(sql[m.end():close], masked[m.end():close])
        src = args[0]
        keys: list[str] | None = []
        if len(args) == 2:
            keys = _json_path_keys(args[1])
        end = close + 1
        with_m = re.match(r"(?is)\s*WITH\s*\(", masked[end:])
        defs_text = None
        if with_m:
            wopen = end + with_m.end()
            wclose = match_paren(masked, wopen)
            if wclose < 0:
                keys = None
            else:
                defs_text = (sql[wopen:wclose], masked[wopen:wclose])
                end = wclose + 1
        if not _JSON_SRC_RE.match(src) or keys is None or len(args) > 2:
            refused += 1
            sql = sql[:m.start()] + "MSSQLTOPG_KEEP_OPENJSON(" + sql[m.end():]
            continue
        src_pg = re.sub(r"[\[\]]", "", src.strip())
        array = _jsonb_array_source(src_pg, keys)
        if defs_text is not None:
            cols = []
            ok = True
            for d in split_top_level(*defs_text):
                dm = re.match(
                    r"(?is)^(\[[^\]]+\]|\w+)\s+(.+?)(?:\s+N?'([^']*)')?(\s+AS\s+JSON)?\s*$", d)
                if not dm:
                    ok = False
                    break
                raw_name, ttype, path, as_json = dm.groups()
                quoted = raw_name.startswith("[")
                name = raw_name.strip("[]")
                pkeys = _json_path_keys(f"'{path}'") if path else [name]
                pg_type = _pg_type(ttype)
                if not pkeys or pg_type is None:
                    ok = False
                    break
                key_args = ", ".join(_sql_string(k) for k in pkeys)
                col = f'"{name}"' if quoted else name
                if as_json:
                    value = f"CAST(jsonb_extract_path(e.value, {key_args}) AS TEXT)"
                else:
                    value = f"CAST(jsonb_extract_path_text(e.value, {key_args}) AS {pg_type})"
                cols.append(f"{value} AS {col}")
            if not ok or not cols:
                refused += 1
                sql = sql[:m.start()] + "MSSQLTOPG_KEEP_OPENJSON(" + sql[m.end():]
                continue
            sub = (f"(SELECT {', '.join(cols)} "
                   f"FROM jsonb_array_elements({array}) AS e(value))")
        else:
            base = f"CAST({src_pg} AS TEXT)"
            node = f"CAST({base} AS JSONB)"
            if keys:
                node += " #> " + _sql_string("{" + ",".join(keys) + "}")
            sub = (f"(SELECT CAST(a.o - 1 AS TEXT) AS key, a.v AS value "
                   f"FROM jsonb_array_elements_text(CASE WHEN {base} IS NOT JSON THEN '[]'::jsonb "
                   f"WHEN jsonb_typeof({node}) = 'array' THEN {node} ELSE '[]'::jsonb END) "
                   f"WITH ORDINALITY AS a(v, o) "
                   f"UNION ALL SELECT b.k, b.v "
                   f"FROM jsonb_each_text(CASE WHEN {base} IS NOT JSON THEN '{{}}'::jsonb "
                   f"WHEN jsonb_typeof({node}) = 'object' THEN {node} ELSE '{{}}'::jsonb END) "
                   f"AS b(k, v))")
        tid = ctx.next_id()
        ctx.tables[tid] = sub
        sql = sql[:m.start()] + f"MSSQLTOPG_TVF_{tid}()" + sql[end:]
        done += 1
    sql = re.sub(r"MSSQLTOPG_KEEP_OPENJSON\(", "OPENJSON(", sql)
    if done:
        ctx.note("A028", done,
                 f"{done} OPENJSON call(s) rewritten to jsonb_array_elements / "
                 f"jsonb_each_text. The source is normalised the way OPENJSON "
                 f"behaves: an ARRAY gives one row per element, a single OBJECT "
                 f"gives one row (jsonb_to_recordset alone would raise or silently "
                 f"drop it), non-JSON text gives no rows and no error. JSON keys "
                 f"keep their exact case. Needs PostgreSQL 16+ (IS JSON).")
    if refused:
        ctx.warn("A028", Severity.ERROR,
                 f"{refused} OPENJSON call(s) NOT converted: the source is not a "
                 f"plain @parameter / column, the path has array indexes, or a "
                 f"WITH column could not be mapped. Convert by hand -- see "
                 f"CONVERSION_RULES.md A028 for the target shape.")
    return sql


# ---- FLOAT precision -------------------------------------------------------
_FLOAT_TOKEN_RE = re.compile(r"(?i)\bFLOAT\b")
_REAL_TOKEN_RE = re.compile(r"(?i)\bREAL\b")


def fix_bracket_identifiers(sql: str, ctx: Context) -> str:
    """A051 -- see rewrite_bracket_identifiers. Runs first: every later
    pre-parse regex then sees plain names."""
    sql, count = rewrite_bracket_identifiers(sql)
    sql, aliases = rewrite_string_aliases(sql)
    count += aliases
    ctx.note("A051", count,
             f"{count} [bracket] identifier(s) unbracketed. sqlglot would emit them as "
             f"double-quoted names, which PostgreSQL matches case-SENSITIVELY "
             f"(\"Master_Tax\", \"IsDeleted\", an alias \"ER\" declared bare) -- valid "
             f"grammar, 'does not exist' at runtime. Reserved words are quoted in lower "
             f"case (\"order\"), names with spaces keep their quoted text. Output column "
             f"names now fold to lower case like any bare alias: quote an alias by hand "
             f"if a DataTable is returned to the UI as JSON.")
    return sql


def fix_param_literals(sql: str, ctx: Context) -> str:
    """A052 -- see rewrite_param_literals. Written as T-SQL `+`; the concat
    rule (A011) then emits `||`."""
    sql, count = rewrite_param_literals(sql, "+")
    ctx.note("A052", count,
             f"{count} '%@Param%' LIKE pattern(s) rewritten to '%' || @Param || '%'. "
             f"QueryParameterProcessor binds only an EXACT '@Param' literal, so the "
             f"pattern compared against the text '@Param' and matched nothing.")
    return sql


def pre_parse(sql: str) -> tuple[str, Context]:
    """Every text-level rewrite, in dependency order."""
    ctx = Context()
    for step in (fix_bracket_identifiers, fix_param_literals,
                 fix_welded_on, fix_session_options, fix_temp_drop_guards,
                 fix_table_variables, fix_if_blocks, fix_inline_declares, fix_delete_output,
                 fix_convert_styles, fix_format_calls, fix_openjson):
        try:
            sql = step(sql, ctx)
        except Exception as exc:      # a rewrite must never take down a conversion
            ctx.warn("A0XX", Severity.WARNING,
                     f"{step.__name__} skipped: {type(exc).__name__}: {exc}")
    masked = code_mask(sql)
    ctx.has_float = bool(_FLOAT_TOKEN_RE.search(masked))          # type: ignore[attr-defined]
    ctx.has_real = bool(_REAL_TOKEN_RE.search(masked))            # type: ignore[attr-defined]
    sql = re.sub(r"(?i)@@IDENTITY\b", "SCOPE_IDENTITY()", sql) if "@@" in masked else sql
    return sql, ctx


# ==========================================================================
# AST FIXES
# ==========================================================================

def _is_zero_date(node) -> bool:
    if isinstance(node, exp.TimeStrToTime):
        node = node.this
    if isinstance(node, exp.Literal):
        value = str(node.this).strip()
        return value in ("0", "1900-01-01", "19000101", "1900-01-01 00:00:00")
    return False


def _unit(node) -> str:
    unit = node.args.get("unit")
    return str(unit.this if isinstance(unit, (exp.Var, exp.Literal)) else unit or "").upper()


_UNIT_ALIASES = {
    "YEAR": "YEAR", "YY": "YEAR", "YYYY": "YEAR",
    "QUARTER": "QUARTER", "QQ": "QUARTER", "Q": "QUARTER",
    "MONTH": "MONTH", "MM": "MONTH", "M": "MONTH",
    "WEEK": "WEEK", "WK": "WEEK", "WW": "WEEK",
    "DAY": "DAY", "DD": "DAY", "D": "DAY", "DAYOFYEAR": "DAY", "DY": "DAY", "Y": "DAY",
    "HOUR": "HOUR", "HH": "HOUR",
    "MINUTE": "MINUTE", "MI": "MINUTE", "N": "MINUTE",
    "SECOND": "SECOND", "SS": "SECOND", "S": "SECOND",
}

_DATEDIFF_TEMPLATES = {
    "YEAR": "CAST(EXTRACT(YEAR FROM __B__) - EXTRACT(YEAR FROM __A__) AS INT)",
    "QUARTER": ("CAST((EXTRACT(YEAR FROM __B__) - EXTRACT(YEAR FROM __A__)) * 4 "
                "+ EXTRACT(QUARTER FROM __B__) - EXTRACT(QUARTER FROM __A__) AS INT)"),
    "MONTH": ("CAST((EXTRACT(YEAR FROM __B__) - EXTRACT(YEAR FROM __A__)) * 12 "
              "+ EXTRACT(MONTH FROM __B__) - EXTRACT(MONTH FROM __A__) AS INT)"),
    "WEEK": ("CAST((CAST(DATE_TRUNC('week', __B__ + INTERVAL '1 day') AS DATE) "
             "- CAST(DATE_TRUNC('week', __A__ + INTERVAL '1 day') AS DATE)) / 7 AS INT)"),
    "DAY": "(CAST(__B__ AS DATE) - CAST(__A__ AS DATE))",
    "HOUR": ("CAST(EXTRACT(EPOCH FROM (DATE_TRUNC('hour', __B__) "
             "- DATE_TRUNC('hour', __A__))) / 3600 AS INT)"),
    "MINUTE": ("CAST(EXTRACT(EPOCH FROM (DATE_TRUNC('minute', __B__) "
               "- DATE_TRUNC('minute', __A__))) / 60 AS INT)"),
    "SECOND": ("CAST(EXTRACT(EPOCH FROM (DATE_TRUNC('second', __B__) "
               "- DATE_TRUNC('second', __A__))) AS INT)"),
}


_TIME_ONLY = re.compile(r"^\d{1,2}:\d{2}(:\d{2}(\.\d+)?)?$")


def _as_timestamp(node):
    """Operand for date arithmetic: strip sqlglot's TimeStrToTime wrapper;
    a string literal / parameter / the 0 date gets an explicit CAST (an untyped
    literal or a text-typed Dapper parameter cannot be EXTRACTed from)."""
    if isinstance(node, exp.TimeStrToTime):
        node = node.this
    if isinstance(node, exp.Literal):
        value = node.this
        if not node.is_string and str(value).strip() == "0":
            value = "1900-01-01"
        # v2.16.0: a time-only literal ('00:00') is that time on 1900-01-01, as
        # T-SQL converts it -- CAST('00:00' AS TIMESTAMP) is 22007 in PostgreSQL.
        if _TIME_ONLY.match(str(value).strip()):
            value = f"1900-01-01 {str(value).strip()}"
        return exp.Cast(this=exp.Literal.string(str(value)),
                        to=exp.DataType.build("TIMESTAMP"))
    if isinstance(node, exp.Cast) and node.to.this == exp.DataType.Type.TIME:
        # DATE_TRUNC has no TIME form: T-SQL compared a TIME on 1900-01-01.
        return exp.Add(this=exp.Cast(this=exp.Literal.string("1900-01-01"),
                                     to=exp.DataType.build("DATE")),
                       expression=node.copy())
    if isinstance(node, (exp.Parameter, exp.Placeholder)):
        return exp.Cast(this=node.copy(), to=exp.DataType.build("TIMESTAMP"))
    return node.copy()


def _fill(template: str, **values):
    tree = sqlglot.parse_one(template, read="postgres")
    for col in list(tree.find_all(exp.Column)):
        key = col.name
        if key in values:
            col.replace(values[key].copy())
    return tree


_NAIVE_TYPES = {getattr(exp.DataType.Type, n) for n in
                ("DATETIME", "DATETIME2", "TIMESTAMP", "SMALLDATETIME", "DATETIME64", "TIMESTAMPNTZ")
                if hasattr(exp.DataType.Type, n)}


def fix_fngetlocaltime(expressions: list, original: str) -> Applied | None:
    """A025: keep fnGetLocalTime on its timestamptz overload.

    Only the timestamptz overload maps Windows zone ids ('India Standard
    Time') to IANA. `fnGetLocalTime(CAST(col AS DATETIME), @TimeZone)` becomes
    `CAST(col AS TIMESTAMP)`, which binds the timestamp overload and raises
    'time zone "India Standard Time" not recognized' for every user with a
    zone -- 26 Export files shipped that way. The first argument is a
    timestamptz column (6,913 of them) or a clock function, so the cast is
    dropped, and GETUTCDATE()/SYSUTCDATETIME() become NOW() instead of the
    naive `NOW() AT TIME ZONE 'UTC'`."""
    count = 0
    for tree in expressions:
        for call in list(tree.find_all(exp.Anonymous)):
            if str(call.name).lower() != "fngetlocaltime" or not call.expressions:
                continue
            first = call.expressions[0]
            new = None
            if isinstance(first, exp.Cast) and first.to.this in _NAIVE_TYPES and isinstance(
                    first.this, (exp.Column, exp.Anonymous, exp.CurrentTimestamp,
                                 exp.Coalesce, exp.Max, exp.Min)):
                new = first.this.copy()
            elif isinstance(first, exp.Anonymous) and str(first.name).upper() in (
                    "GETUTCDATE", "SYSUTCDATETIME") and not first.expressions:
                new = exp.CurrentTimestamp()
            if new is not None:
                first.replace(new)
                count += 1
    if not count:
        return None
    return Applied("A025", count,
                   f"{count} fnGetLocalTime first argument(s) kept as timestamptz "
                   f"(CAST(... AS DATETIME) dropped / GETUTCDATE() -> NOW()). Only "
                   f"the timestamptz overload accepts Windows zone ids; the "
                   f"timestamp overload raises 'time zone ... not recognized'.")


def fix_datediff_boundaries(expressions: list, original: str) -> Applied | None:
    """A035: DATEDIFF counts BOUNDARIES crossed, not elapsed time.

    DATEDIFF(day, '23:00 Mon', '01:00 Tue') is 1 in T-SQL; sqlglot's
    EXTRACT(EPOCH ...)/86400 gives 0 -- silently wrong in every "age in
    days" / "overdue" column. Same for month/year/hour/minute."""
    count = 0
    for tree in expressions:
        for node in list(tree.find_all(exp.DateDiff)):
            unit = _UNIT_ALIASES.get(_unit(node))
            if unit is None:
                continue
            template = _DATEDIFF_TEMPLATES[unit]
            new = _fill(template, __B__=_as_timestamp(node.this),
                        __A__=_as_timestamp(node.expression))
            node.replace(new)
            count += 1
    if not count:
        return None
    return Applied("A035", count,
                   f"{count} DATEDIFF call(s) rewritten with T-SQL BOUNDARY "
                   f"semantics (day = date difference, month = calendar months, "
                   f"hour = truncated hours ...). sqlglot's elapsed-seconds form "
                   f"returned 0 for 23:00 -> 01:00 next day where T-SQL returns 1.")


def fix_datepart(expressions: list, original: str) -> Applied | None:
    """A048: DATEPART(unit, x) with T-SQL numbering.

    sqlglot emits EXTRACT(DAYOFWEEK / DAYOFYEAR ...) -- units PostgreSQL does
    not accept -- and passes week/millisecond through with DIFFERENT meaning:
      dw   T-SQL 1=Sunday..7       -> EXTRACT(DOW)+1 (DOW is 0=Sunday)
      wk   T-SQL week 1 = the week holding Jan 1, Sunday start
                                   -> PostgreSQL 'week' is the ISO week
      ms   T-SQL 0-999             -> PostgreSQL milliseconds include seconds*1000
    EXTRACT returns NUMERIC (and fractional seconds); DATEPART returned INT.
    Runs before A035, whose templates build their own EXTRACTs."""
    count = 0
    templates = {
        "DAYOFWEEK": "CAST(EXTRACT(DOW FROM __X__) AS INT) + 1",
        "DOW": "CAST(EXTRACT(DOW FROM __X__) AS INT) + 1",
        "WEEKDAY": "CAST(EXTRACT(DOW FROM __X__) AS INT) + 1",
        "DAYOFYEAR": "CAST(EXTRACT(DOY FROM __X__) AS INT)",
        "DOY": "CAST(EXTRACT(DOY FROM __X__) AS INT)",
        "WEEK": ("CAST((EXTRACT(DOY FROM __X__) + EXTRACT(DOW FROM DATE_TRUNC('year', __X__)) - 1) / 7 AS INT) + 1"),
        "MILLISECOND": "CAST(FLOOR(EXTRACT(MILLISECONDS FROM __X__)) AS INT) % 1000",
        "MICROSECOND": "CAST(FLOOR(EXTRACT(MICROSECONDS FROM __X__)) AS INT) % 1000000",
        "ISO_WEEK": "CAST(EXTRACT(WEEK FROM __X__) AS INT)",
        "ISOWK": "CAST(EXTRACT(WEEK FROM __X__) AS INT)",
    }
    for tree in expressions:
        for node in list(tree.find_all(exp.Extract)):
            unit = str(node.this.this if isinstance(node.this, (exp.Var, exp.Literal)) else node.this).upper()
            if isinstance(node.parent, exp.Cast):
                continue
            template = templates.get(unit)
            if template is None:
                if unit in ("EPOCH",):
                    continue
                template = f"CAST(FLOOR(EXTRACT({unit} FROM __X__)) AS INT)"
            node.replace(_fill(template, __X__=_as_timestamp(node.expression)))
            count += 1
    if not count:
        return None
    return Applied("A048", count,
                   f"{count} DATEPART(...) mapped with T-SQL numbering and INT results "
                   f"(weekday 1=Sunday, T-SQL week-of-year, millisecond 0-999). sqlglot "
                   f"emitted EXTRACT(DAYOFWEEK/DAYOFYEAR) -- rejected by PostgreSQL -- "
                   f"and the ISO week / total milliseconds, silently different.")


def fix_date_integer_arithmetic(expressions: list, original: str) -> Applied | None:
    """A049: GETDATE() - 1 means 'minus one DAY' in T-SQL; timestamp - integer
    is an error in PostgreSQL. Only rewritten where the date side is provably a
    date/time (a clock function or a DATEADD result)."""
    count = 0
    date_like = (exp.CurrentTimestamp, exp.CurrentDate, exp.DateAdd, exp.TimeStrToTime)
    for tree in expressions:
        for op in list(tree.find_all(exp.Add, exp.Sub)):
            left, right = op.this, op.expression
            if isinstance(left, date_like) and isinstance(right, (exp.Literal, exp.Parameter, exp.Neg)) \
                    and not (isinstance(right, exp.Literal) and right.is_string):
                days = right.copy()
                op.set("expression", exp.Mul(this=days, expression=exp.Interval(
                    this=exp.Literal.string("1"), unit=exp.var("DAY"))))
                count += 1
    if not count:
        return None
    return Applied("A049", count,
                   f"{count} date +/- integer expression(s) -> date +/- n * INTERVAL '1 day'. "
                   f"T-SQL treats the integer as days; PostgreSQL rejects "
                   f"timestamp - integer (42883).")


def fix_zero_date_idioms(expressions: list, original: str) -> Applied | None:
    """A047: DATEADD(unit, DATEDIFF(unit, 0, x), 0) -> DATE_TRUNC('unit', x).

    The T-SQL 'start of day / month / year' idiom treats integer 0 as the date
    1900-01-01. sqlglot emitted `0 + INTERVAL ...` -- integer plus interval,
    a runtime error."""
    count = 0
    for tree in expressions:
        for node in list(tree.find_all(exp.DateAdd)):
            this, amount = node.this, node.expression
            diff = None
            if _is_zero_date(this) and isinstance(amount, exp.DateDiff):
                diff = amount
            elif _is_zero_date(amount) and isinstance(this, exp.DateDiff):
                diff = this
            if diff is None or not _is_zero_date(diff.expression):
                continue
            unit = _UNIT_ALIASES.get(_unit(diff))
            if unit is None or unit != _UNIT_ALIASES.get(_unit(node), unit):
                continue
            target = diff.this.this if isinstance(diff.this, exp.TimeStrToTime) else diff.this
            new = exp.Cast(
                this=exp.Anonymous(this="DATE_TRUNC", expressions=[
                    exp.Literal.string(unit.lower()), target.copy()]),
                to=exp.DataType.build("TIMESTAMP"))
            node.replace(new)
            count += 1
        for node in list(tree.find_all(exp.DateAdd)):
            if isinstance(node.this, exp.Literal) and not node.this.is_string \
                    and str(node.this.this) == "0":
                node.set("this", exp.Cast(this=exp.Literal.string("1900-01-01"),
                                          to=exp.DataType.build("TIMESTAMP")))
                count += 1
    if not count:
        return None
    return Applied("A047", count,
                   f"{count} T-SQL zero-date idiom(s) (DATEADD(u, DATEDIFF(u, 0, x), 0) "
                   f"-> DATE_TRUNC('u', x); a bare 0 date -> '1900-01-01'). "
                   f"sqlglot emitted `0 + INTERVAL`, which fails at runtime.")


def fix_date_parts(expressions: list, original: str) -> Applied | None:
    """A032: DATEFROMPARTS -> MAKE_DATE, DATETIMEFROMPARTS -> MAKE_TIMESTAMP,
    SYSDATETIMEOFFSET() -> CURRENT_TIMESTAMP. sqlglot emitted DATE_FROM_PARTS,
    a 7-argument MAKE_TIMESTAMP and CURRENT_TIMESTAMP_L_T_Z() -- none exists."""
    count = 0

    def as_int(node):
        if isinstance(node, exp.Literal) and not node.is_string:
            return node.copy()
        if isinstance(node, (exp.Year, exp.Month, exp.Day)):
            return node.copy()        # becomes CAST(EXTRACT(...) AS INT) below
        return exp.Cast(this=node.copy(), to=exp.DataType.build("INT"))

    for tree in expressions:
        for node in list(tree.find_all(exp.DateFromParts)):
            parts = [node.args.get(k) for k in ("year", "month", "day")]
            if any(p is None for p in parts):
                continue
            node.replace(exp.Anonymous(this="MAKE_DATE", expressions=[as_int(p) for p in parts]))
            count += 1
        for node in list(tree.find_all(exp.TimestampFromParts)):
            keys = ("year", "month", "day", "hour", "min", "sec")
            parts = [node.args.get(k) for k in keys]
            if any(p is None for p in parts):
                continue
            args = [as_int(p) for p in parts[:5]]
            sec = parts[5].copy()
            milli = node.args.get("milli")
            if milli is not None and not (isinstance(milli, exp.Literal) and str(milli.this) == "0"):
                sec = exp.Add(this=sec, expression=exp.Div(this=milli.copy(),
                                                           expression=exp.Literal.number("1000.0")))
            args.append(sec)
            node.replace(exp.Cast(this=exp.Anonymous(this="MAKE_TIMESTAMP", expressions=args),
                                  to=exp.DataType.build("TIMESTAMP")))
            count += 1
        for node in list(tree.find_all(exp.Expression)):
            if type(node).__name__ == "CurrentTimestampLTZ":
                node.replace(exp.CurrentTimestamp())
                count += 1
        # YEAR(x) / MONTH(x) / DAY(x): sqlglot passes them through verbatim and
        # PostgreSQL has none of them (42883). EXTRACT returns NUMERIC on
        # PostgreSQL 14+, T-SQL returned INT -- cast back so arithmetic and
        # Dapper int mapping behave as before.
        for cls, part in ((exp.Year, "YEAR"), (exp.Month, "MONTH"), (exp.Day, "DAY")):
            for node in list(tree.find_all(cls)):
                if not isinstance(node, cls) or node.this is None:
                    continue
                node.replace(exp.Cast(
                    this=exp.Extract(this=exp.var(part), expression=_as_timestamp(node.this)),
                    to=exp.DataType.build("INT")))
                count += 1
    if not count:
        return None
    return Applied("A032", count,
                   f"{count} date-construction call(s) mapped to real PostgreSQL "
                   f"functions (MAKE_DATE / MAKE_TIMESTAMP with INT arguments / "
                   f"CURRENT_TIMESTAMP). sqlglot emitted DATE_FROM_PARTS and "
                   f"CURRENT_TIMESTAMP_L_T_Z(), which parse and then fail with 42883.")


_T = exp.DataType.Type


def make_type_fixer(has_float: bool, has_real: bool):
    def fix_types(expressions: list, original: str) -> Applied | None:
        """A033: T-SQL types sqlglot maps to types PostgreSQL does not have
        (UTINYINT, SMALLMONEY, IMAGE, VARIANT, BYTEA(MAX)) or to a WEAKER one
        (FLOAT -> REAL loses half the precision: T-SQL FLOAT is float(53))."""
        changed: list[str] = []
        for tree in expressions:
            for dt in list(tree.find_all(exp.DataType)):
                t = dt.this
                new = None
                if t in (_T.UTINYINT, _T.TINYINT):
                    new = exp.DataType.build("SMALLINT")
                elif t == _T.SMALLMONEY:
                    new = exp.DataType.build("DECIMAL(10, 4)")
                elif t == _T.MONEY:
                    new = exp.DataType.build("DECIMAL(19, 4)")
                elif t == _T.IMAGE or (t in (_T.VARBINARY, _T.BINARY) and dt.expressions):
                    new = exp.DataType.build("VARBINARY")
                elif t == _T.VARIANT:
                    new = exp.DataType.build("TEXT")
                elif t == _T.FLOAT:
                    params = dt.expressions
                    if params:
                        try:
                            bits = int(str(params[0].this.this if hasattr(params[0].this, "this")
                                           else params[0].this))
                        except (ValueError, AttributeError):
                            bits = 53
                        new = (exp.DataType.build("REAL") if bits <= 24
                               else exp.DataType.build("DOUBLE"))
                    elif has_float and not has_real:
                        new = exp.DataType.build("DOUBLE")
                if new is not None:
                    changed.append(f"{t.name}->{new.sql('postgres')}")
                    dt.replace(new)
        if not changed:
            return None
        return Applied("A033", len(changed),
                       f"{len(changed)} data type(s) corrected: "
                       f"{', '.join(sorted(set(changed)))}. sqlglot emitted UTINYINT / "
                       f"SMALLMONEY / IMAGE / VARIANT / BYTEA(MAX) (no such PostgreSQL "
                       f"types) and REAL for FLOAT (T-SQL FLOAT is double precision).")
    return fix_types


def fix_round_truncate(expressions: list, original: str) -> Applied | None:
    """A034: ROUND(x, n, 1) TRUNCATES in T-SQL; sqlglot silently dropped the
    third argument, turning truncation into rounding."""
    count = 0
    for tree in expressions:
        for node in list(tree.find_all(exp.Round)):
            trunc = node.args.get("truncate")
            if trunc is None:
                continue
            if isinstance(trunc, exp.Literal) and str(trunc.this) == "0":
                node.set("truncate", None)
                continue
            args = [exp.Cast(this=node.this.copy(), to=exp.DataType.build("NUMERIC"))]
            if node.args.get("decimals") is not None:
                args.append(node.args["decimals"].copy())
            node.replace(exp.Anonymous(this="TRUNC", expressions=args))
            count += 1
    if not count:
        return None
    return Applied("A034", count,
                   f"{count} ROUND(x, n, <non-zero>) -> TRUNC(CAST(x AS NUMERIC), n). "
                   f"The third argument means TRUNCATE in T-SQL; sqlglot dropped it "
                   f"and rounded instead -- silently different amounts.")


_LIKE_META = set("|*+?{}()\\")


def fix_like_brackets(expressions: list, original: str) -> Applied | None:
    """A036: LIKE '[A-Z]%' is a character class in T-SQL and a LITERAL '[' in
    PostgreSQL LIKE -- the filter silently matches nothing. SIMILAR TO keeps
    %, _ and [...] with T-SQL meaning; its other regex metacharacters are
    escaped."""
    count = 0
    for tree in expressions:
        for like in list(tree.find_all(exp.Like)):
            if isinstance(like.parent, exp.Escape):
                continue
            pattern = like.expression
            if not (isinstance(pattern, exp.Literal) and pattern.is_string):
                continue
            text = str(pattern.this)
            if not re.search(r"\[[^\]]+\]", text):
                continue
            escaped = "".join("\\" + ch if ch in _LIKE_META else ch for ch in text)
            new = exp.SimilarTo(this=like.this.copy(), expression=exp.Literal.string(escaped))
            # sqlglot keeps `NOT LIKE` as Like(negate=True), not Not(Like):
            # dropping the flag would INVERT the filter.
            if like.args.get("negate"):
                new = exp.Not(this=new)
            like.replace(new)
            count += 1
    if not count:
        return None
    return Applied("A036", count,
                   f"{count} LIKE pattern(s) with a [..] character class -> SIMILAR TO. "
                   f"In PostgreSQL LIKE, '[' is a literal, so the filter matched "
                   f"nothing -- no error.")


def fix_len_trailing(expressions: list, original: str) -> Applied | None:
    """A037: LEN() ignores trailing spaces; LENGTH() counts them."""
    count = 0
    for tree in expressions:
        for node in list(tree.find_all(exp.Length)):
            inner = node.this
            if isinstance(inner, exp.Cast) and inner.to.this == _T.TEXT and not (
                    isinstance(inner.this, exp.Anonymous) and str(inner.this.name).upper() == "RTRIM"):
                node.set("this", exp.Anonymous(this="RTRIM", expressions=[inner.copy()]))
                count += 1
    if not count:
        return None
    return Applied("A037", count,
                   f"{count} LEN(x) -> LENGTH(RTRIM(x)): T-SQL LEN ignores trailing "
                   f"spaces, PostgreSQL LENGTH counts them.")


def fix_misc_functions(expressions: list, original: str) -> Applied | None:
    """A038 ISJSON -> IS JSON; ISNUMERIC -> regex test; DATALENGTH ->
    OCTET_LENGTH; NCHAR(n) -> CHR(n)."""
    changed: list[str] = []
    for tree in expressions:
        for call in list(tree.find_all(exp.Anonymous)):
            name = str(call.name).upper()
            args = call.expressions
            if name == "ISJSON" and len(args) == 1:
                test = exp.Is(this=exp.Cast(this=args[0].copy(), to=exp.DataType.build("TEXT")),
                              expression=exp.var("JSON"))
                parent = call.parent
                lit = None
                if isinstance(parent, (exp.EQ, exp.NEQ)):
                    other = parent.expression if parent.this is call else parent.this
                    if isinstance(other, exp.Literal) and str(other.this) in ("0", "1"):
                        lit = str(other.this)
                if lit is not None:
                    positive = (lit == "1") == isinstance(parent, exp.EQ)
                    parent.replace(test if positive else exp.Not(this=test))
                else:
                    call.replace(exp.Case(ifs=[exp.If(this=test, true=exp.Literal.number(1))],
                                          default=exp.Literal.number(0)))
                changed.append("ISJSON")
            elif name == "ISNUMERIC" and len(args) == 1:
                test = exp.RegexpLike(
                    this=exp.Cast(this=args[0].copy(), to=exp.DataType.build("TEXT")),
                    expression=exp.Literal.string(r"^\s*[-+]?([0-9]+(\.[0-9]*)?|\.[0-9]+)\s*$"))
                call.replace(exp.Case(ifs=[exp.If(this=test, true=exp.Literal.number(1))],
                                      default=exp.Literal.number(0)))
                changed.append("ISNUMERIC")
            elif name == "DATALENGTH" and len(args) == 1:
                call.replace(exp.Anonymous(this="OCTET_LENGTH", expressions=[
                    exp.Cast(this=args[0].copy(), to=exp.DataType.build("TEXT"))]))
                changed.append("DATALENGTH")
            elif name == "NCHAR" and len(args) == 1:
                call.replace(exp.Anonymous(this="CHR", expressions=[args[0].copy()]))
                changed.append("NCHAR")
            elif name == "CHOOSE" and len(args) >= 2:
                call.replace(exp.Case(this=args[0].copy(), ifs=[
                    exp.If(this=exp.Literal.number(i), true=a.copy())
                    for i, a in enumerate(args[1:], start=1)]))
                changed.append("CHOOSE")
            elif name in ("VAR", "VARP", "STDEVP") and len(args) == 1:
                call.replace(exp.Anonymous(this={"VAR": "VAR_SAMP", "VARP": "VAR_POP",
                                                 "STDEVP": "STDDEV_POP"}[name],
                                           expressions=[args[0].copy()]))
                changed.append(name)
    if not changed:
        return None
    return Applied("A038", len(changed),
                   f"{len(changed)} T-SQL-only function(s) mapped: "
                   f"{', '.join(sorted(set(changed)))} (ISJSON -> IS JSON, PostgreSQL "
                   f"16+; ISNUMERIC -> a plain-decimal regex test, STRICTER than "
                   f"T-SQL, which also accepted '$', ',' and 'e' -- so fewer values "
                   f"reach a following CAST; DATALENGTH -> OCTET_LENGTH, NVARCHAR "
                   f"counted 2 bytes per character in T-SQL).")


def fix_output_clause(expressions: list, original: str) -> Applied | None:
    """A039: OUTPUT INSERTED.x -> RETURNING x. sqlglot turned OUTPUT into
    RETURNING but kept the INSERTED./DELETED. qualifier, which PostgreSQL
    rejects ('missing FROM-clause entry for table "inserted"')."""
    count = 0
    for tree in expressions:
        for ret in list(tree.find_all(exp.Returning)):
            if ret.args.get("into"):
                continue
            stmt = ret.parent
            allowed = {"INSERTED"} if isinstance(stmt, (exp.Insert, exp.Update)) else \
                {"DELETED"} if isinstance(stmt, exp.Delete) else set()
            cols = [c for c in ret.find_all(exp.Column) if c.table]
            if not cols or any(c.table.upper() not in allowed for c in cols):
                continue
            for c in cols:
                c.set("table", None)
            count += 1
    if not count:
        return None
    return Applied("A039", count,
                   f"{count} OUTPUT INSERTED./DELETED. clause(s) -> RETURNING with the "
                   f"pseudo-table qualifier removed.")


def fix_temp_tables(expressions: list, original: str) -> Applied | None:
    """A040: `SELECT ... INTO #t` -> `CREATE TEMPORARY TABLE t AS SELECT ...`;
    `DROP TABLE #t` -> `DROP TABLE IF EXISTS pg_temp.t`.

    An unqualified DROP of a converted #temp resolves through search_path and
    would drop a PERMANENT table of the same name. The DROP guard ahead of each
    CREATE is added after generation (post_generate)."""
    count = 0
    for index, tree in enumerate(expressions):
        if tree is None:
            continue
        if isinstance(tree, exp.Select):
            into = tree.args.get("into")
            if into is not None and (into.args.get("temporary") or
                                     (isinstance(into.this, exp.Table)
                                      and into.this.this.args.get("temporary"))):
                table = into.this
                select = tree.copy()
                select.set("into", None)
                expressions[index] = exp.Create(
                    this=exp.to_table(table.name), kind="TABLE", expression=select,
                    properties=exp.Properties(expressions=[exp.TemporaryProperty()]))
                count += 1
                continue
        for drop in list(tree.find_all(exp.Drop)):
            table = drop.this
            if isinstance(table, exp.Table) and table.this is not None and \
                    table.this.args.get("temporary"):
                drop.set("exists", True)
                table.set("db", exp.to_identifier("pg_temp"))
                count += 1
    if not count:
        return None
    return Applied("A040", count,
                   f"{count} #temp statement(s) made PostgreSQL-safe: SELECT INTO #t "
                   f"-> CREATE TEMPORARY TABLE t AS, DROP TABLE #t -> DROP TABLE IF "
                   f"EXISTS pg_temp.t (an unqualified drop could hit a PERMANENT "
                   f"table of the same name).")


def fix_union_order(expressions: list, original: str) -> Applied | None:
    """A041: `... UNION ... ORDER BY a.col` -> ORDER BY <output column>.
    SQL Server resolves the qualified name to the output column; PostgreSQL
    raises 'missing FROM-clause entry for table "a"'."""
    count = 0
    for tree in expressions:
        for union in list(tree.find_all(exp.Union, exp.Except, exp.Intersect)):
            if isinstance(union.parent, (exp.Union, exp.Except, exp.Intersect)):
                continue
            first = union
            while isinstance(first, (exp.Union, exp.Except, exp.Intersect)):
                first = first.this
            last = union
            while isinstance(last, (exp.Union, exp.Except, exp.Intersect)):
                last = last.expression
            orders = [o for o in (union.args.get("order"),
                                  last.args.get("order") if isinstance(last, exp.Select) else None) if o]
            if not orders or not isinstance(first, exp.Select):
                continue
            for order in orders:
                for col in list(order.find_all(exp.Column)):
                    if not col.table:
                        continue
                    target = None
                    for item in first.expressions:
                        inner = item.this if isinstance(item, exp.Alias) else item
                        if isinstance(inner, exp.Column) and inner.name.lower() == col.name.lower() \
                                and (not inner.table or inner.table.lower() == col.table.lower()):
                            target = item.alias_or_name
                            break
                    if target is None and any(i.alias_or_name.lower() == col.name.lower()
                                              for i in first.expressions):
                        target = col.name
                    if target:
                        col.replace(exp.column(target, quoted=col.this.quoted))
                        count += 1
    if not count:
        return None
    return Applied("A041", count,
                   f"{count} qualified ORDER BY column(s) after a UNION/EXCEPT/INTERSECT "
                   f"replaced by the output column name. PostgreSQL rejects "
                   f"`ORDER BY a.col` there ('missing FROM-clause entry').")


def fix_pivot(expressions: list, original: str) -> Applied | None:
    """A042: PIVOT -> conditional aggregation.

    sqlglot DROPS the PIVOT clause (the output is the un-pivoted source --
    valid SQL, different rows). Converted only for the shape whose grouping
    columns are knowable: a derived table with an explicit column list."""
    count = 0
    for tree in expressions:
        for sub in list(tree.find_all(exp.Subquery)):
            pivots = sub.args.get("pivots") or []
            if len(pivots) != 1:
                continue
            pivot = pivots[0]
            if pivot.args.get("unpivot"):
                continue
            select = sub.this
            if not isinstance(select, exp.Select) or any(isinstance(e, exp.Star) for e in select.expressions):
                continue
            aggs = pivot.expressions
            fields = pivot.args.get("fields") or []
            if len(aggs) != 1 or len(fields) != 1 or not isinstance(fields[0], exp.In):
                continue
            agg = aggs[0]
            value_col = agg.this if isinstance(agg.this, exp.Column) else None
            pivot_col = fields[0].this if isinstance(fields[0].this, exp.Column) else None
            if value_col is None or pivot_col is None:
                continue
            outputs = [e.alias_or_name for e in select.expressions]
            group = [n for n in outputs if n.lower() not in (value_col.name.lower(), pivot_col.name.lower())]
            values = []
            for v in fields[0].expressions:
                if isinstance(v, exp.Column):
                    values.append(v.name)
                elif isinstance(v, exp.Literal):
                    values.append(str(v.this))
                else:
                    values = None
                    break
            if not values:
                continue
            src_alias = sub.alias or "pivot_src"
            new_cols = [exp.column(g) for g in group]
            for v in values:
                cond = exp.EQ(this=exp.column(pivot_col.name), expression=exp.Literal.string(v))
                case = exp.Case(ifs=[exp.If(this=cond, true=exp.column(value_col.name))])
                a = agg.copy()
                a.set("this", case)
                new_cols.append(exp.alias_(a, exp.to_identifier(v, quoted=True)))
            inner = exp.Subquery(this=select.copy(), alias=exp.TableAlias(this=exp.to_identifier(src_alias)))
            new_select = exp.Select(expressions=new_cols).from_(inner)
            if group:
                new_select = new_select.group_by(*[exp.column(g) for g in group])
            alias = pivot.args.get("alias")
            alias_name = alias.name if alias is not None else "p"
            sub.replace(exp.Subquery(this=new_select,
                                     alias=exp.TableAlias(this=exp.to_identifier(alias_name))))
            count += 1
    if not count:
        return None
    return Applied("A042", count,
                   f"{count} PIVOT clause(s) rewritten as conditional aggregation "
                   f"(AGG(CASE WHEN col = 'v' THEN value END) AS \"v\", grouped by the "
                   f"remaining source columns). sqlglot dropped PIVOT outright.")


def fix_scope_identity(expressions: list, original: str) -> Applied | None:
    """A044: SCOPE_IDENTITY()/@@IDENTITY after an INSERT -> LASTVAL()."""
    count = 0
    seen_insert = False
    for tree in expressions:
        if tree is None:
            continue
        if isinstance(tree, exp.Insert):
            seen_insert = True
            continue
        if not seen_insert:
            continue
        for call in list(tree.find_all(exp.Anonymous)):
            if str(call.name).upper() == "SCOPE_IDENTITY" and not call.expressions:
                call.replace(exp.Anonymous(this="LASTVAL", expressions=[]))
                count += 1
    if not count:
        return None
    return Applied("A044", count,
                   f"{count} SCOPE_IDENTITY()/@@IDENTITY after an INSERT -> LASTVAL(). "
                   f"Exact for an identity/serial key; a trigger that inserts into "
                   f"another sequence-backed table in between would change it. "
                   f"`INSERT ... RETURNING id` is the tidier form if the key name is known.")


def fix_scalar_subquery_order(expressions: list, original: str) -> list[Warning]:
    """L027 (lint): TOP 1 / LIMIT 1 scalar subquery with no ORDER BY picks an
    ARBITRARY row -- SQL Server and PostgreSQL will not pick the same one."""
    hits = 0
    for tree in expressions:
        for sub in tree.find_all(exp.Subquery):
            sel = sub.this
            if isinstance(sel, exp.Select) and sel.args.get("limit") is not None \
                    and not sel.args.get("order") and isinstance(
                        sub.parent, (exp.EQ, exp.NEQ, exp.Select, exp.Alias, exp.Coalesce)):
                hits += 1
    if not hits:
        return []
    return [Warning("L027", Severity.INFO,
                    f"{hits} TOP 1 / LIMIT 1 scalar subquery(ies) without ORDER BY: "
                    f"the row picked is arbitrary and PostgreSQL will not pick the one "
                    f"SQL Server did. Add an ORDER BY if a specific row is meant.")]


def apply_ast_pre(expressions: list, original: str) -> list[Applied]:
    out = []
    for fix in (fix_fngetlocaltime,):
        r = fix([t for t in expressions if t is not None], original)
        if r:
            out.append(r)
    return out


def apply_ast(expressions: list, original: str, ctx: Context | None = None) -> list[Applied]:
    has_float = getattr(ctx, "has_float", True)
    has_real = getattr(ctx, "has_real", False)
    out = []
    # A040 mutates the list itself (a top-level SELECT INTO becomes a CREATE)
    r = fix_temp_tables(expressions, original)
    if r:
        out.append(r)
    for fix in (fix_datepart, fix_zero_date_idioms, fix_datediff_boundaries,
                fix_date_integer_arithmetic, fix_date_parts,
                make_type_fixer(has_float, has_real), fix_round_truncate,
                fix_like_brackets, fix_len_trailing, fix_misc_functions,
                fix_output_clause, fix_union_order, fix_pivot, fix_scope_identity):
        try:
            r = fix([t for t in expressions if t is not None], original)
        except Exception:          # one fix must never sink the conversion
            r = None
        if r:
            out.append(r)
    return out


# ==========================================================================
# POST-GENERATION (text)
# ==========================================================================

def expand_placeholders(sql: str, ctx: Context) -> str:
    for tid, sub in ctx.tables.items():
        sql = re.sub(r"(?i)MSSQLTOPG_TVF_" + str(tid) + r"\s*\(\s*\)", lambda _m: sub, sql)
    guard = 0
    while guard < 1000:
        guard += 1
        m = re.search(r"(?i)MSSQLTOPG_FN_(\d+)\s*\(", sql)
        if not m:
            break
        close = match_paren(code_mask(sql), m.end())
        if close < 0:
            break
        name, extra = ctx.functions.get(int(m.group(1)), ("TO_CHAR", ""))
        inner = sql[m.end():close]
        new = f"{name}({inner}{', ' + extra if extra else ''})"
        sql = sql[:m.start()] + new + sql[close + 1:]
    return sql


_CREATE_TEMP_RE = re.compile(
    r"(?im)(^|;)(\s*)CREATE\s+(?:TEMP|TEMPORARY)\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(?:pg_temp\.)?(\"?\w+\"?)")


def add_temp_drop_guards(sql: str) -> tuple[str, int]:
    """R006 made a fix: lead every CREATE TEMP TABLE with its DROP guard, so a
    table left alive on a POOLED connection by an earlier failed call cannot
    make every later call fail with 'relation already exists'."""
    masked = code_mask(sql)
    edits = []
    for m in _CREATE_TEMP_RE.finditer(masked):
        name = sql[m.start(3):m.end(3)]
        bare = name.strip('"').lower()
        before = masked[:m.end(2)]
        if re.search(r"(?i)DROP\s+TABLE\s+IF\s+EXISTS\s+(?:pg_temp\.)?\"?" + re.escape(bare) + r"\"?\s*(;|$)",
                     before):
            continue
        if before.count("$$") % 2 == 1:
            continue           # inside a DO/function body
        lead = sql[m.start(2):m.end(2)]
        indent = lead.rsplit("\n", 1)[-1] if "\n" in lead else ""
        edits.append((m.end(2), m.end(2), f"DROP TABLE IF EXISTS pg_temp.{name};\n{indent}"))
    return _apply_edits(sql, edits), len(edits)


_TM_PADDED_RE = re.compile(r"'TM(Day|Month)'")
_ROLLUP_RE = re.compile(r"(?is)\bGROUP\s+BY\s+((?:(?!\bGROUP\s+BY\b).)+?)\s+WITH\s+(ROLLUP|CUBE)\b")


def fix_generated_text(sql: str, ctx: Context) -> str:
    """A050: small generated-text defects.
      * DATENAME -> TO_CHAR(x, 'TMDay'/'TMMonth') pads to 9 characters
        ('May      '); T-SQL returns the bare name. FM suppresses the padding.
      * GROUP BY a, b WITH ROLLUP/CUBE -> GROUP BY ROLLUP/CUBE (a, b)."""
    masked = code_mask(sql)
    n1 = 0
    edits = []
    for m in _TM_PADDED_RE.finditer(sql):
        edits.append((m.start(), m.end(), f"'FM{m.group(1)}'"))
        n1 += 1
    for m in _ROLLUP_RE.finditer(masked):
        cols = sql[m.start(1):m.end(1)]
        edits.append((m.start(), m.end(), f"GROUP BY {m.group(2).upper()} ({cols})"))
        n1 += 1
    if n1:
        ctx.note("A050", n1,
                 f"{n1} generated-text fix(es): DATENAME day/month names no longer "
                 f"blank-padded to 9 characters ('FMDay'), GROUP BY ... WITH "
                 f"ROLLUP/CUBE -> GROUP BY ROLLUP/CUBE (...).")
    return _apply_edits(sql, edits)


def post_generate(sql: str, ctx: Context) -> str:
    sql = expand_placeholders(sql, ctx)
    sql = fix_generated_text(sql, ctx)
    sql, splits = rewrite_split_calls(sql)
    ctx.note("A053", splits,
             f"{splits} SPLIT(x, sep) call(s) left after generation (CROSS APPLY dbo.Split "
             f"becomes JOIN LATERAL SPLIT) rewritten to a STRING_TO_TABLE derived table with "
             f"dbo.Split's `items` column, trimmed, empty slices skipped -- PostgreSQL has no "
             f"split() function (42883).")
    sql, guards = add_temp_drop_guards(sql)
    if guards:
        ctx.note("A040", guards,
                 f"{guards} `DROP TABLE IF EXISTS pg_temp.<t>` guard(s) added ahead of "
                 f"CREATE TEMP TABLE -- on a pooled connection a mid-batch error "
                 f"leaves the table alive and every later call fails with "
                 f"'relation already exists'.")
    return sql


# ==========================================================================
# LINT (reported, never rewritten)
# ==========================================================================

_BOOL_PARAM_RE = re.compile(
    r"(?i)(@(?:Is|Has|Can|Allow|Enable|Show|Include|Exclude|Should)[A-Z0-9_]\w*)\s*(?:=|<>|!=)\s*[01]\b"
    r"|\b[01]\s*(?:=|<>|!=)\s*(@(?:Is|Has|Can|Allow|Enable|Show|Include|Exclude|Should)[A-Z0-9_]\w*)")
_EMPTY_DATE_RE = re.compile(
    r"(?:\b([\w.]*?(?:[Dd]ate|DATE|[Tt]ime|TIME|Dt|DT))\s*(?:=|<>|!=)\s*''"
    r"|(?i:COALESCE)\s*\(\s*([\w.]*?(?:[Dd]ate|DATE|[Tt]ime|TIME|Dt|DT))\s*,\s*''\s*\))")
_RESERVED_ALIAS_RE = re.compile(
    r"(?i)\bAS\s+(LocalTime|LocalTimestamp|Current_Date|Current_Time|Current_Timestamp|"
    r"Current_User|Session_User|Current_Role|Current_Catalog|Current_Schema)\b(?!\s*\()")
_LOOKUP_COMPARE_RE = re.compile(
    r"(?i)\b([\w.]*(?:ScreenName|EventCode|EventName|FlagType|ModuleName|UnitCode|"
    r"ConfigKey|KeyName|StatusCode|MessageType|DocumentType|ProductCode))\s*(?:=|<>|!=|IN\s*\()\s*'[^']*[A-Za-z][^']*'")
_PLUS_LITERAL_RE = re.compile(r"'\+'")
_TOCHAR_MASK_RE = re.compile(r"(?i)\bTO_(?:CHAR|DATE|TIMESTAMP)\s*\((?:[^()']|'(?:[^']|'')*'|\([^()]*\))*?,\s*'((?:[^']|'')*)'\s*\)")


# L031 (v2.16.0): QueryParameterProcessor text-replaces these names in the SQL before
# Dapper sees it (sb.Replace, no word boundary), so any LONGER parameter that starts
# with one of them is cut: @UserIdList -> 7List, @DBNameOld -> KLSPL_DBOld.
_INFRA_STATIC_KEYS = (
    "@CommonMobiledbName", "@Community_Admin_OrgId", "@DBName", "@EDIdbName", "@EXPdbName",
    "@HangdbName", "@HospitalitydbName", "@IMPdbName", "@LCCTdbName", "@LogdbName",
    "@MasterdbName", "@PCSdbName", "@PGdbName", "@PassengerdbName", "@RIdbName", "@RoleId",
    "@ScreenId", "@ScreenName", "@TSMdbName", "@TarifdbName", "@UserId", "@WFdbName",
    "@contentmgtdbName")
_PARAM_NAME_RE = re.compile(r"@[A-Za-z_]\w*")


def infra_key_collisions(sql: str) -> list[str]:
    """Parameter names that START with an infra static key but are longer than it."""
    masked = code_mask(sql)
    hits = set()
    for m in _PARAM_NAME_RE.finditer(masked):
        name = sql[m.start():m.end()]
        for key in _INFRA_STATIC_KEYS:
            if name.startswith(key) and len(name) > len(key):
                hits.add(name)
    return sorted(hits)


def lint(original: str, converted: str) -> list[Warning]:
    out: list[Warning] = []
    src_masked = code_mask(original)
    hits = sorted({(m.group(1) or m.group(2)) for m in _BOOL_PARAM_RE.finditer(src_masked)})
    if hits:
        out.append(Warning(
            "L021", Severity.WARNING,
            f"Parameter(s) {', '.join(hits[:6])} compared to 0/1. If the C# property is "
            f"`bool`, Npgsql sends a real BOOLEAN and PostgreSQL raises 42883 "
            f"'boolean = integer' -- write = TRUE/FALSE. If it is `int`, 0/1 is right. "
            f"Check the request DTO."))
    unbound = unbound_param_texts(converted or original)
    if unbound:
        out.append(Warning(
            "L030", Severity.WARNING,
            f"Parameter name(s) inside a longer string literal: {', '.join(unbound[:4])}. "
            f"QueryParameterProcessor binds only an EXACT '@Name' literal, so this text is "
            f"sent as-is and never filled in. Build it with || around a bound @param "
            f"(or pass the whole value as one parameter)."))
    collisions = infra_key_collisions(converted or original)
    if collisions:
        out.append(Warning(
            "L031", Severity.ERROR,
            f"Parameter name(s) {', '.join(collisions[:6])} start with a name ACS's "
            f"QueryParameterProcessor replaces as plain text (@UserId, @DBName, "
            f"@Community_Admin_OrgId ...) before the query runs, with no word boundary: "
            f"@UserIdList becomes 7List and the query breaks. Rename the parameter "
            f"(e.g. @ListOfUserIds) here and in the C# caller."))
    if not converted:
        return out
    masked = code_mask(converted)
    hits = sorted({(m.group(1) or m.group(2)) for m in _EMPTY_DATE_RE.finditer(masked)})
    if hits:
        out.append(Warning(
            "L022", Severity.WARNING,
            f"'' compared with / defaulted into date-like column(s) {', '.join(hits[:6])}. "
            f"T-SQL coerced '' to 1900-01-01; PostgreSQL raises 22007 for a timestamp "
            f"(22P02 for an integer). Use IS NULL, or CAST('1900-01-01' AS TIMESTAMP)."))
    m = _RESERVED_ALIAS_RE.search(masked)
    if m:
        out.append(Warning(
            "L023", Severity.ERROR,
            f"Alias `{m.group(1)}` is a PostgreSQL built-in: an unquoted reference "
            f"resolves to the BUILT-IN even with the alias in scope "
            f"(`CAST(LocalTime AS DATE)` -> 'cannot cast type time'). Rename the "
            f"alias (e.g. LocalDtTime)."))
    hits = sorted({m.group(1) for m in _LOOKUP_COMPARE_RE.finditer(converted)
                   if masked[m.start()] == converted[m.start()]})
    if hits:
        out.append(Warning(
            "L025", Severity.WARNING,
            f"Case-sensitive comparison of lookup/config column(s) {', '.join(hits[:6])} "
            f"to a literal. SQL Server compared case-INSENSITIVELY; PostgreSQL does not "
            f"('BookSlot' vs stored 'bookslot' = 0 rows, which switched Export "
            f"booking off for weeks). Use UPPER(col) = UPPER('...') after checking "
            f"the stored casing."))
    if _PLUS_LITERAL_RE.search(converted):
        out.append(Warning(
            "L026", Severity.WARNING,
            "A '+' string literal is in the output. ACS's runtime ConvertSQLToPostgreSQL "
            "rewrites + to || without respecting quotes, turning it into ' || '. Use "
            "CHR(43) (R-CHAT-11)."))
    for mm in _TOCHAR_MASK_RE.finditer(converted):
        mask = mm.group(1)
        if mask == "None" or "%" in mask or re.search(r"(DD|MM|YY)TH(H|M)", mask) or \
                re.search(r"(?<![A-Za-z\"])(fff(?!f)|tt(?![A-Za-z]))", mask):
            out.append(Warning(
                "R008", Severity.ERROR,
                f"Date mask '{mask}' is not a valid PostgreSQL pattern (strftime "
                f"'%...', 'None', or an unquoted 'T' before HH -- PostgreSQL reads "
                f"'TH' as an ordinal suffix). Quote literal letters: 'YYYY-MM-DD\"T\"HH24'."))
            break
    # imported here: core.totalrecords builds on this module's code_mask
    from core import totalrecords
    out += totalrecords.lint(converted)
    return out


def residue(converted: str) -> list[Warning]:
    """R007 -- the shared marker gate over the converted output."""
    from core import markers
    hard, soft = markers.find(converted)
    found = hard + soft
    if not found:
        return []
    return [Warning(
        "R007", Severity.ERROR,
        f"T-SQL survived conversion: {', '.join(found)}. The output would fail on "
        f"PostgreSQL (or is a hand rewrite -- control flow, #temp, dynamic SQL).")]
