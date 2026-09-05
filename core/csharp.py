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
_LEADING = r"(?:SELECT|INSERT|UPDATE|DELETE|MERGE|WITH|CREATE|ALTER|DROP|TRUNCATE|EXEC|EXECUTE)"
_STRUCTURAL = r"(?:FROM|INTO|JOIN|SET|VALUES|WHERE|TABLE|INDEX|VIEW|PROCEDURE)"
_SQL_SHAPE = re.compile(rf"^\s*{_LEADING}\b.*\b{_STRUCTURAL}\b",
                        re.IGNORECASE | re.DOTALL)


@dataclass
class SqlLiteral:
    kind: str            # verbatim | regular | raw
    start: int           # index of the literal INCLUDING its prefix/quotes
    end: int             # index just past the closing quote(s)
    sql: str             # decoded SQL text
    line: int            # 1-based line of the literal's start
    interpolated: bool   # $ prefix -- contains {expr} holes


def looks_like_sql(text: str) -> bool:
    return bool(_SQL_SHAPE.match(text))


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


def find_sql_literals(text: str) -> list[SqlLiteral]:
    """Every string literal in `text` whose content looks like SQL.

    Walks the source tracking comments and literal state, so nothing inside a
    comment or a char literal is ever returned.
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
            if looks_like_sql(sql):
                found.append(SqlLiteral(
                    kind=kind, start=prefix_start, end=end, sql=sql,
                    line=text.count("\n", 0, prefix_start) + 1,
                    interpolated=interpolated,
                ))
            i = end
            continue

        i = prefix_start + 1 if j == prefix_start else j

    return found


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


def replace_literals(text: str, replacements: list[tuple[SqlLiteral, str]]) -> str:
    """Swap literals for new C# literal text. Applied right-to-left so earlier
    offsets stay valid."""
    result = text
    for literal, new_text in sorted(replacements, key=lambda p: -p[0].start):
        result = result[:literal.start] + new_text + result[literal.end:]
    return result


