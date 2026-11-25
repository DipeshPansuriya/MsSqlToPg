"""Find SQL string literals in C# source, and put converted SQL back.

This module rewrites files IN PLACE, so it uses a real tokenizer rather than a
regex. A regex would happily match inside a `//` comment or a `/* */` block and
corrupt source that merely mentions SQL. The scanner tracks comments, char
literals and every C# string form, so only genuine string literals are ever
considered.

Supported literal forms:

    @"..."          verbatim     -- "" is an escaped quote
    "..."           regular      -- \\" is an escaped quote
    \"\"\"...\"\"\"       raw (C# 11)  -- no escaping inside

Interpolated forms ($"...", $@"...") are DETECTED but never rewritten: the
`{expr}` holes are dynamic SQL, and substituting around them is not safe.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

VERBATIM = "verbatim"
REGULAR = "regular"
RAW = "raw"

# A string is treated as SQL only with a leading DML/DDL keyword AND a
# structural keyword. "SELECT a name" is not SQL; "SELECT a FROM t" is.
#
# Leading `;` is allowed before the keyword: `;WITH cte AS (...)` is the standard
# T-SQL defensive idiom for starting a CTE. Without this the literal did not look
# like SQL at all, so it was never offered to the converter AND never reported --
# a silent miss rather than a "needs review" row.
_LEADING = r"(?:SELECT|INSERT|UPDATE|DELETE|MERGE|WITH|CREATE|ALTER|DROP|TRUNCATE|EXEC|EXECUTE)"
# v2.13.0: batches that START with a T-SQL preamble. The pre-pass now converts
# the common ones (single-assignment DECLAREs, IF OBJECT_ID temp guards, SET
# NOCOUNT), so these must reach the converter instead of being reported as
# fragments. Shapes are specific enough that English prose cannot match.
_LEADING_BATCH = (r"(?:DECLARE\s+@|IF\s+(?:OBJECT_ID|EXISTS|NOT\s+EXISTS)\s*\(|"
                  r"SET\s+(?:NOCOUNT|ANSI_NULLS|QUOTED_IDENTIFIER|XACT_ABORT)\b|"
                  r"SET\s+TRANSACTION\s+ISOLATION\b)")
_STRUCTURAL = r"(?:FROM|INTO|JOIN|SET|VALUES|WHERE|TABLE|INDEX|VIEW|PROCEDURE)"
_SQL_SHAPE = re.compile(rf"^[\s;]*(?:{_LEADING}\b|{_LEADING_BATCH}).*\b{_STRUCTURAL}\b",
                        re.IGNORECASE | re.DOTALL)
# v2.14.0, Check-PG only: converted constants routinely open with a
# `-- PostgreSQL. Converted ...` header, which hid the whole statement from the
# repairs. The Scan tab keeps the strict shape on purpose -- there a commented
# literal is usually already-converted code that must NOT be re-read as T-SQL.
_SQL_SHAPE_AFTER_COMMENTS = re.compile(
    rf"^(?:\s|;|--[^\n]*(?:\n|$)|/\*.*?\*/)*(?:{_LEADING}\b|{_LEADING_BATCH}).*\b{_STRUCTURAL}\b",
    re.IGNORECASE | re.DOTALL)
_LEADING_COMMENTS = re.compile(r"^(?:\s|;|--[^\n]*(?:\n|$)|/\*.*?\*/)*", re.DOTALL)

# v2.18.0: batches that open with T-SQL control flow -- `IF (@x = 'Y') BEGIN ...`,
# `if('@T' = 'GSA') select ...`, `IF '@orgType' = 'Agent'`, `BEGIN DECLARE @t TABLE ... END`.
# They used to fail the shape test and land in find_sql_fragments, so the converter (which flattens
# IF/ELSE since A058) never saw them: 7 of 14 such Stock constants convert cleanly once offered.
# The condition must read like SQL (a parameter, NOT/EXISTS, a call or a comparison) and the body
# must hold a real statement, so "If (you) select a value from the list" is not SQL; a BEGIN
# batch must also end with END.
_CONTROL_HEAD = re.compile(
    r"^[\s;]*(?:IF\s*\(\s*(?:'?@|NOT\b|EXISTS\b|\w+\s*\(|[\w.\[\]]+\s*(?:=|<>|!=|>=|<=|<|>|IS\b|IN\b|LIKE\b))"
    r"|IF\s+'?@\w|IF\s+NOT\s+'?@\w"
    r"|BEGIN\s+(?:DECLARE\s+@|IF\b|SET\s+@|SELECT\b|INSERT\b|UPDATE\b|DELETE\b|DROP\b|CREATE\b|WITH\b|TRUNCATE\b))",
    re.IGNORECASE)
_CONTROL_BODY = re.compile(
    r"\bSELECT\b.*?\bFROM\b|\bINSERT\s+INTO\b|\bUPDATE\b.*?\bSET\b|\bDELETE\s+FROM\b", re.IGNORECASE | re.DOTALL)
_ENDS_WITH_END = re.compile(r"\bEND\s*;?\s*$", re.IGNORECASE)


def _control_batch(text: str) -> bool:
    if not (_CONTROL_HEAD.match(text) and _CONTROL_BODY.search(text)):
        return False
    return not re.match(r"^[\s;]*BEGIN\b", text, re.IGNORECASE) or bool(_ENDS_WITH_END.search(text))


def _commented_tsql(text: str) -> bool:
    """v2.18.0: a T-SQL batch behind a leading comment (`-- Drop temp tables\\nDROP TABLE IF EXISTS #t`).
    Only with T-SQL markers in it: converted PostgreSQL routinely opens with a `-- PostgreSQL ...`
    header, and the Scan tab must never re-read that as T-SQL."""
    rest = text[_LEADING_COMMENTS.match(text).end():]
    if rest == text or not (_SQL_SHAPE.match(rest) or _control_batch(rest)):
        return False
    return bool(tsql_markers(rest))


@dataclass
class SqlLiteral:
    kind: str            # verbatim | regular | raw
    start: int           # index of the literal INCLUDING its prefix/quotes
    end: int             # index just past the closing quote(s)
    sql: str             # decoded SQL text
    line: int            # 1-based line of the literal's start
    interpolated: bool   # $ prefix -- contains {expr} holes


def looks_like_sql(text: str) -> bool:
    return bool(_SQL_SHAPE.match(text)) or _control_batch(text) or _commented_tsql(text)


# A SQL FRAGMENT is a literal that is not a whole statement -- a scalar
# subquery, a CASE expression, a WHERE predicate, a column mapping -- that C#
# splices into a larger query at runtime:
#
#     ColMap_Count_GHAName = "(SELECT TOP 1 OrgName FROM Organization WITH(NOLOCK) ...)"
#     DateFilter_CTE_ShipmentDate = "Job.ShipmentDate >= @InputDateFrom AND ..."
#
# These fail the whole-statement shape test, so they were neither converted NOR
# reported -- the worst outcome, because the T-SQL inside them still reaches the
# database. They cannot be safely REWRITTEN (a fragment has no FROM clause, so
# its table aliases and the surrounding context are unknowable), but they must
# be REPORTED so a human converts them.
#
# Detection is by unambiguous T-SQL markers only. A fragment of plain ANSI SQL
# needs no conversion and is not worth a finding.
_TSQL_MARKERS = (
    (r"WITH\s*\(\s*NOLOCK\s*\)",          "WITH (NOLOCK)"),
    (r"\bISNULL\s*\(",                    "ISNULL()"),
    (r"\bTOP\s*\(?\s*\d",                 "TOP n"),
    (r"\bNVARCHAR\b|\bVARCHAR\s*\(\s*MAX", "NVARCHAR / VARCHAR(MAX)"),
    (r"\bGET(?:UTC)?DATE\s*\(",           "GETDATE()"),
    (r"#\w+",                             "#temp table"),
    (r"\bdbo\.",                          "dbo. schema"),
    (r"\bAS\s+BIT\b",                     "CAST(... AS BIT)"),
    (r"\bCONVERT\s*\(\s*(?:N?VARCHAR|DATE|DATETIME|BIT|INT)\b", "CONVERT()"),
    (r"(?<![A-Za-z0-9_'])N'",             "N'...' literal"),
    (r"\[[A-Za-z_]\w*\]",                 "[bracketed identifier]"),
    (r"\bTRY_(?:CONVERT|CAST)\s*\(",      "TRY_CONVERT / TRY_CAST"),
    (r"\bOUTER\s+APPLY\b|\bCROSS\s+APPLY\b", "APPLY"),
    (r"\bSTRING_SPLIT\s*\(",              "STRING_SPLIT()"),
    (r"\bIIF\s*\(",                       "IIF()"),
)
_MARKER_RES = [(re.compile(p, re.IGNORECASE), label) for p, label in _TSQL_MARKERS]

# Cheap guard: something must look SQL-ish at all before markers are consulted,
# so an ordinary message is not reported. Two ways to qualify:
#
#   strong -- a clause keyword no English sentence uses that way;
#   weak   -- a comparison operator, which covers a bare predicate fragment
#             like `ISNULL(a,0)=0 AND b=1` that has no clause keyword.
#
# `AND` alone is NOT enough: "Press [Enter] and we will send it" matched it and
# the bracket marker, and got reported as SQL.
_FRAGMENT_STRONG = re.compile(
    r"\bSELECT\b|\bFROM\b|\bWHERE\b|\bCASE\s+WHEN\b|\bJOIN\b|\bCONCAT\s*\(",
    re.IGNORECASE)
_FRAGMENT_OPERATOR = re.compile(r"(?:=|<>|!=|>=|<=|<|>)")


def tsql_markers(text: str) -> list[str]:
    """T-SQL constructs present in `text`, de-duplicated, in a stable order.

    v2.13.0: the local list is joined by the shared marker set
    (core/markers), so a fragment carrying CURSOR / @@ROWCOUNT / SET @var /
    OPENJSON / sys.* is reported too -- the campaign scanner found those in
    `+=` tails and local-variable SQL that this list alone never flagged."""
    from core import markers
    stripped = re.sub(r"--[^\n]*", " ", text)
    stripped = re.sub(r"/\*.*?\*/", " ", stripped, flags=re.DOTALL)
    found = [label for rx, label in _MARKER_RES if rx.search(stripped)]
    hard, _soft = markers.find(text)
    for label in hard:
        if label not in found:
            found.append(label)
    return found


def looks_like_sql_fragment(text: str) -> bool:
    """True for a non-statement literal that still carries T-SQL."""
    if looks_like_sql(text) or not text.strip():
        return False
    sql_ish = (_FRAGMENT_STRONG.search(text)
               or _FRAGMENT_OPERATOR.search(text))
    return bool(sql_ish) and bool(tsql_markers(text))


def decode(kind: str, body: str) -> str:
    if kind == VERBATIM:
        return body.replace('""', '"')
    if kind == REGULAR:
        return (body.replace('\\"', '"').replace("\\r", "\r")
                    .replace("\\n", "\n").replace("\\t", "\t")
                    .replace("\\\\", "\\"))
    return body                                    # raw: no escaping


def encode(kind: str, sql: str) -> str:
    """Render `sql` back into a C# literal of the same kind."""
    if kind == VERBATIM:
        return '@"' + sql.replace('"', '""') + '"'
    if kind == RAW:
        # Raw strings cannot contain their own delimiter; widen it if needed.
        fence = '"""'
        while fence in sql:
            fence += '"'
        return f"{fence}\n{sql}\n{fence}"
    return '"' + (sql.replace("\\", "\\\\").replace('"', '\\"')
                     .replace("\n", "\\n").replace("\r", "\\r")) + '"'


def _walk_literals(text: str, accept) -> list[SqlLiteral]:
    """Every string literal in `text` for which `accept(sql)` is true.

    Walks the source tracking comments and literal state, so nothing inside a
    comment or a char literal is ever returned. One walk shared by the
    statement and fragment scans, so they can never disagree about what counts
    as a literal.
    """
    found: list[SqlLiteral] = []
    i, n = 0, len(text)

    while i < n:
        ch = text[i]
        nxt = text[i + 1] if i + 1 < n else ""

        # -- comments ------------------------------------------------------
        if ch == "/" and nxt == "/":
            end = text.find("\n", i)
            i = n if end == -1 else end + 1
            continue
        if ch == "/" and nxt == "*":
            end = text.find("*/", i + 2)
            i = n if end == -1 else end + 2
            continue

        # -- char literal 'x' ---------------------------------------------
        if ch == "'":
            i += 1
            while i < n and text[i] != "'":
                i += 2 if text[i] == "\\" else 1
            i += 1
            continue

        # -- string literals ----------------------------------------------
        prefix_start = i
        interpolated = False
        j = i
        while j < n and text[j] in "@$":
            interpolated = interpolated or text[j] == "$"
            j += 1
        verbatim = "@" in text[prefix_start:j]

        if j < n and text[j] == '"':
            literal = _read_literal(text, j, verbatim)
            if literal is None:
                i = j + 1
                continue
            kind, body, end = literal
            sql = decode(kind, body)
            if accept(sql):
                found.append(SqlLiteral(
                    kind=kind, start=prefix_start, end=end, sql=sql,
                    line=text.count("\n", 0, prefix_start) + 1,
                    interpolated=interpolated,
                ))
            i = end
            continue

        i = prefix_start + 1 if j == prefix_start else j

    return found


def find_sql_literals(text: str, *, allow_leading_comments: bool = False) -> list[SqlLiteral]:
    """Every string literal whose content is a whole SQL statement.
    `allow_leading_comments`: Check-PG only -- see _SQL_SHAPE_AFTER_COMMENTS."""
    if allow_leading_comments:
        return _walk_literals(text, lambda s: bool(_SQL_SHAPE_AFTER_COMMENTS.match(s))
                              or _control_batch(s[_LEADING_COMMENTS.match(s).end():]))
    return _walk_literals(text, looks_like_sql)


def find_sql_fragments(text: str) -> list[SqlLiteral]:
    """Literals that carry T-SQL but are NOT whole statements.

    Reported, never rewritten: a fragment has no FROM clause of its own, so the
    aliases it references and the context it will be spliced into are
    unknowable. See looks_like_sql_fragment.
    """
    return _walk_literals(text, looks_like_sql_fragment)


def _read_literal(text: str, quote_at: int, verbatim: bool):
    """Return (kind, body, end_index) for the literal starting at `quote_at`."""
    n = len(text)

    # raw string: three or more quotes
    if not verbatim and text.startswith('"""', quote_at):
        fence_len = 0
        while quote_at + fence_len < n and text[quote_at + fence_len] == '"':
            fence_len += 1
        fence = '"' * fence_len
        body_start = quote_at + fence_len
        end = text.find(fence, body_start)
        if end == -1:
            return None
        return RAW, text[body_start:end].strip("\r\n"), end + fence_len

    if verbatim:
        i = quote_at + 1
        body = []
        while i < n:
            if text[i] == '"':
                if i + 1 < n and text[i + 1] == '"':
                    body.append('""')
                    i += 2
                    continue
                return VERBATIM, "".join(body), i + 1
            body.append(text[i])
            i += 1
        return None

    i = quote_at + 1
    body = []
    while i < n:
        if text[i] == "\\" and i + 1 < n:
            body.append(text[i:i + 2])
            i += 2
            continue
        if text[i] == '"':
            return REGULAR, "".join(body), i + 1
        if text[i] == "\n":
            return None                 # regular strings cannot span lines
        body.append(text[i])
        i += 1
    return None


def line_indent(text: str, index: int) -> str:
    """The leading whitespace of the line containing `index`."""
    start = text.rfind("\n", 0, index) + 1
    line = text[start:]
    return line[:len(line) - len(line.lstrip(" \t"))]


def reindent(sql: str, indent: str) -> str:
    """Lay converted SQL out inside the C# file, matching the surrounding code.

    sqlglot pretty-prints from column 0. Aligning to the `@"` column would push
    the text far to the right and make the file unreadable, so the SQL is
    indented one level in from the STATEMENT -- the style these files use.
    """
    lines = sql.splitlines()
    if not lines:
        return sql
    body = "\n".join(indent + line if line.strip() else line for line in lines)
    return "\n" + body + "\n" + indent


_BUILDER_CALL_BEFORE = re.compile(r"\b([A-Za-z_]\w*)\s*\.\s*(?:Append|AppendLine|AppendFormat|Insert)\s*\(\s*(?:\d+\s*,\s*)?$")
_ASSIGN_BEFORE = re.compile(r"\b([A-Za-z_]\w*)\s*=\s*$")


def _code_before(text: str, start: int, span: int = 400) -> str:
    """The C# just before a literal (whitespace trimmed)."""
    return text[max(0, start - span):start].rstrip()


def composed_query(text: str, literal: SqlLiteral) -> str | None:
    """Why this literal is only ONE PIECE of a query C# assembles at runtime, or None.

    A piece converted on its own ends in `;`, gains DROP/CREATE guards and leaves the
    pieces appended after it in T-SQL -- `...);` + ` AND (x = cast(@y as bit))` + ` GROUP BY ...`
    breaks at runtime although each piece validated. Such a query is converted as a whole, by hand.
    """
    before = _code_before(text, literal.start)
    after = text[literal.end:literal.end + 200].lstrip()
    if before.endswith("+") or after.startswith("+") and not after.startswith("++"):
        return "concatenated with `+`"
    if before.endswith("+="):
        return "appended with `+=`"
    m = _BUILDER_CALL_BEFORE.search(before)
    if m:
        var = re.escape(m.group(1))
        calls = list(re.finditer(r"\b" + var + r"\s*\.\s*(?:Append|AppendLine|AppendFormat|Insert)\s*\(", text))
        # the builder's own method: between the declarations of this builder around the literal
        decls = [d.start() for d in re.finditer(r"\b" + var + r"\s*=\s*new\s+StringBuilder\b", text)]
        lo = max([d for d in decls if d < literal.start], default=0)
        hi = min([d for d in decls if d > literal.start], default=len(text))
        if sum(1 for c in calls if lo <= c.start() < hi) > 1:
            return f"one of several `{m.group(1)}.Append(...)` pieces"
    m = _ASSIGN_BEFORE.search(before)
    if m and re.search(r"\b" + re.escape(m.group(1)) + r"\s*\+=", text[literal.end:]):
        return f"`{m.group(1)}` is extended later with `+=`"
    return None


def replace_literals(text: str, replacements: list[tuple[SqlLiteral, str]]) -> str:
    """Swap literals for new C# literal text. Applied right-to-left so earlier
    offsets stay valid."""
    result = text
    for literal, new_text in sorted(replacements, key=lambda p: -p[0].start):
        result = result[:literal.start] + new_text + result[literal.end:]
    return result


