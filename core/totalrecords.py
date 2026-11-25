"""A059 / A060 / L032 (v2.17.0) and their converted-code twins PC15 / PC16 --
statements that return a TotalRecords / TotalCount column, kept in a shape a
filterString pager can wrap.

Some data-access layers page a grid query when the Dapper call carries a
`filterString` parameter, editing the SQL text before it runs:

  1. a statement splitter cuts the batch at EVERY line whose first
     word is SELECT/INSERT/UPDATE/DELETE/DROP/CREATE/ALTER/IF/SET/DECLARE --
     subquery lines included; it is not statement-aware;
  2. every piece that starts with SELECT and mentions TotalRecords/TotalCount
     (failing that, the last SELECT piece) becomes
     `SELECT * FROM (<piece up to its last ORDER BY> WHERE <filter>)
     FilteredRecords <that ORDER BY> OFFSET @skipRecords ROWS FETCH NEXT
     @TopRecords ROWS ONLY`.

sqlglot's pretty printer puts a subquery's SELECT on its own line. So
`SELECT *, (SELECT COUNT(*) AS TotalRecords FROM t) AS TotalRecords FROM t`,
harmless on one line in the T-SQL, became a piece that starts INSIDE the
parentheses; the pager wrapped that piece and PostgreSQL raised 42601 `syntax
error at or near "FROM"` (2026-10-21).
"""

from __future__ import annotations

import re

import sqlglot
from sqlglot import expressions as exp

from core import validator
from core.models import Severity, Warning
from core.pgfixes import code_mask

_TOTAL_RE = re.compile(r"\b(?:TotalRecords|TotalCount)\b", re.IGNORECASE)

# `(SELECT COUNT(*) [AS x] FROM t [alias]) [AS] TotalRecords` -- an uncorrelated
# count of one whole table; anything else between FROM and ')' fails the match.
_IDENT = r'(?:"[^"]+"|\w+)'
_COUNT_SUBQUERY_RE = re.compile(
    rf"\(\s*SELECT\s+COUNT\s*\(\s*(?:\*|1)\s*\)(?:\s+AS\s+{_IDENT})?\s+FROM\s+"
    rf"(?P<table>(?:{_IDENT}\.)?{_IDENT})(?:\s+(?:AS\s+)?{_IDENT})?\s*\)\s*(?:AS\s+)?"
    rf"(?P<alias>TotalRecords|TotalCount)\b",
    re.IGNORECASE)

# Clauses that make the outer query return something other than every row of
# its table once -- COUNT(*) OVER () would then count a different set.
_OUTER_BLOCKERS = ("where", "group", "having", "qualify", "limit", "offset", "fetch",
                   "distinct", "joins", "laterals", "into")

_LINE_SELECT_RE = re.compile(r"[ \t]*SELECT\b", re.IGNORECASE)
_PAGER_SPLIT_RE = re.compile(r"[ \t]*(?:SELECT|INSERT|UPDATE|DELETE|DROP|CREATE|ALTER|IF|SET|DECLARE)\b",
                             re.IGNORECASE)
_GLUE_AFTER_WORD_RE = re.compile(r"\b(?:UNION|ALL|EXCEPT|INTERSECT)$", re.IGNORECASE)


def _statements(masked: str) -> list[tuple[int, int]]:
    """(start, end) of each top-level statement; `;` excluded."""
    spans, start, depth = [], 0, 0
    for i, ch in enumerate(masked):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(depth - 1, 0)
        elif ch == ";" and depth == 0:
            spans.append((start, i))
            start = i + 1
    if masked[start:].strip():
        spans.append((start, len(masked)))
    return spans


def _starts_with_select(masked_stmt: str) -> bool:
    return re.match(r"\s*SELECT\b", masked_stmt, re.IGNORECASE) is not None


def _depth_at(masked: str, start: int, end: int) -> int:
    return masked.count("(", start, end) - masked.count(")", start, end)


def _table_key(text: str) -> tuple[str, ...]:
    return tuple(part.strip('"').lower() for part in re.findall(_IDENT, text))


def _outer_reads_whole_table(stmt_sql: str, table: tuple[str, ...]) -> bool:
    try:
        tree = sqlglot.parse_one(validator._mask_dapper_parameters(stmt_sql), read="postgres")
    except Exception:
        return False
    if not isinstance(tree, exp.Select) or any(tree.args.get(k) for k in _OUTER_BLOCKERS):
        return False
    source = tree.args.get("from_")
    if source is None or not isinstance(source.this, exp.Table):
        return False
    outer = source.this
    if tuple(p.lower() for p in (outer.db, outer.name) if p) != table:
        return False
    for item in tree.expressions:
        for node in item.walk(prune=lambda n: isinstance(n, (exp.Subquery, exp.Window))):
            if isinstance(node, exp.AggFunc):
                return False
    return True


def window_counts(sql: str) -> tuple[str, int]:
    """A059 / PC15 -- `(SELECT COUNT(*) FROM t) AS TotalRecords` -> `COUNT(*) OVER() AS TotalRecords`.

    Exactly the same value on every row, because the rewrite is refused unless
    the outer query reads every row of that same table once (no WHERE, JOIN,
    GROUP BY, DISTINCT, LIMIT, aggregate). It is also the house shape for
    paged lists. Under a grid filter the window counts the FILTERED rows, so
    the pager total matches the rows shown; the subquery always counted every
    row (on SQL Server too, where the one-line subquery did not break the
    pager)."""
    masked = code_mask(sql)
    edits = []
    for start, end in _statements(masked):
        stmt_masked = masked[start:end]
        for m in _COUNT_SUBQUERY_RE.finditer(stmt_masked):
            if _depth_at(stmt_masked, 0, m.start()) != 0:
                continue
            if not _outer_reads_whole_table(sql[start:end], _table_key(m.group("table"))):
                continue
            edits.append((start + m.start(), start + m.end(), f"COUNT(*) OVER() AS {m.group('alias')}"))
    for a, b, text in reversed(edits):
        sql = sql[:a] + text + sql[b:]
    return sql, len(edits)


def _has_comment(sql: str, masked: str, start: int, end: int) -> bool:
    """code_mask turns comment text into spaces and keeps literal quotes, so a
    non-blank source character over a masked space is comment text."""
    return any(masked[i] == " " and not sql[i].isspace() for i in range(start, end))


def glue_nested_selects(sql: str) -> tuple[str, int]:
    """A060 / PC16 -- a nested SELECT in a TotalRecords statement stays on the
    line of its `(`.

    Whitespace only. The pager cannot cut at `(SELECT`, so the statement stays
    one piece and is wrapped whole, whatever the subquery computes (A059 covers
    only the plain whole-table count). Never across a comment: joining onto a
    `--` line would comment the SELECT out, so that case is left to L032."""
    masked = code_mask(sql)
    edits = []
    for start, end in _statements(masked):
        stmt_masked = masked[start:end]
        if not _TOTAL_RE.search(stmt_masked) or not _starts_with_select(stmt_masked):
            continue
        first_line_end = stmt_masked.find("\n", len(stmt_masked) - len(stmt_masked.lstrip()))
        pos = first_line_end
        while 0 <= pos < len(stmt_masked):
            line_start = pos + 1
            nxt = stmt_masked.find("\n", line_start)
            line_end = len(stmt_masked) if nxt < 0 else nxt
            m = _LINE_SELECT_RE.match(stmt_masked, line_start, line_end)
            if m and _depth_at(stmt_masked, 0, line_start) > 0:
                select_at = line_start + len(m.group(0)) - len("SELECT")
                prev_code = stmt_masked[:line_start].rstrip()
                gap_start = len(prev_code)
                if not _has_comment(sql, masked, start + gap_start, start + select_at):
                    if prev_code.endswith("("):
                        edits.append((start + gap_start, start + select_at, ""))
                    elif _GLUE_AFTER_WORD_RE.search(prev_code):
                        edits.append((start + gap_start, start + select_at, " "))
            pos = nxt
    for a, b, text in reversed(edits):
        sql = sql[:a] + text + sql[b:]
    return sql, len(edits)


# --------------------------------------------------------------------------
# the pager itself -- a port of its logic, used by L032
# --------------------------------------------------------------------------

_SPLIT_RE = re.compile(r"(?=^\s*(SELECT|INSERT|UPDATE|DELETE|DROP|CREATE|ALTER|IF|SET|DECLARE)\b)",
                       re.IGNORECASE | re.MULTILINE)
_IS_STMT_RE = re.compile(r"\s*(SELECT|INSERT|UPDATE|DELETE|DROP|CREATE|ALTER|IF|SET|DECLARE)", re.IGNORECASE)
_SELECT_ASSIGN_RE = re.compile(r"\bSELECT\s+@\w+\s*=", re.IGNORECASE)
_TRAILING_WORD_RE = re.compile(r"\b(?:END|ELSE|BEGIN)$", re.IGNORECASE)


def _split_statements(script: str) -> list[str]:
    """The statement splitter: Regex.Split keeps the captured
    keyword, so the parts alternate keyword / piece."""
    parts = _SPLIT_RE.split(script)
    statements, i = [], 0
    while i < len(parts):
        if parts[i] and _IS_STMT_RE.match(parts[i]):
            if i + 1 < len(parts):
                statements.append(parts[i + 1].strip())
                i += 1
            else:
                statements.append("")
        i += 1
    return statements


def _trim_trailing(stmt: str) -> str:
    """_trailingTrimRegex `(\\s*;|\\s*\\bEND\\b|\\s*\\bELSE\\b|\\s*\\bBEGIN\\b|\\s+)+$`,
    written as a loop: the nested quantifiers backtrack exponentially in Python."""
    while True:
        trimmed = stmt.rstrip()
        if trimmed.endswith(";"):
            stmt = trimmed[:-1]
            continue
        m = _TRAILING_WORD_RE.search(trimmed)
        if m:
            stmt = trimmed[:m.start()]
            continue
        return trimmed


def _split_order_by(query: str) -> tuple[str, str]:
    index = query.upper().rfind("ORDER BY")
    if index < 0:
        return query, ""
    order = query[index:]
    offset = order.upper().find("OFFSET")
    if offset >= 0:
        order = order[:offset].rstrip()
    return query[:index], order


def infra_wrap(sql: str, filter_sql: str) -> str:
    """The SQL a filterString pager runs for a
    query called with filterString (PostgreSQL provider, no parsed ORDER BY)."""
    statements = _split_statements(sql)
    candidates = [s for s in statements
                  if s.upper().startswith("SELECT")
                  and ("TOTALRECORDS" in s.upper() or "TOTALCOUNT" in s.upper())
                  and "INTO #" not in s.upper() and "INTO @" not in s.upper()
                  and not _SELECT_ASSIGN_RE.search(s)]
    if not candidates:
        candidates = [s for s in statements if s.upper().startswith("SELECT")][-1:]
    out, cursor = [], 0
    for stmt in candidates:
        core = _trim_trailing(stmt)
        if not core.strip():
            continue
        index = sql.find(core, cursor)
        if index < 0:
            continue
        base, order = _split_order_by(core.replace(";", ""))
        where = f"WHERE {filter_sql}" if filter_sql.strip() else ""
        replacement = (f"SELECT *\n FROM ({base} \n {where} ) FilteredRecords\n "
                       f"{order or 'ORDER BY 1'}\n OFFSET @skipRecords ROWS FETCH NEXT @TopRecords ROWS ONLY")
        out.append(sql[cursor:index])
        out.append(replacement)
        cursor = index + len(core)
    out.append(sql[cursor:])
    return "".join(out)


def lint(sql: str) -> list[Warning]:
    """L032 -- run the converted SQL through the pager (with a grid filter, the
    stricter case) and check the result against PostgreSQL's real grammar.

    A WARNING, not an ERROR: only the C# caller knows whether it passes
    filterString, and a query that is never paged runs fine."""
    if not sql or not _TOTAL_RE.search(sql):
        return []
    if not validator.validate(sql).valid:
        return []
    if validator.validate(infra_wrap(sql, "1 = 1")).valid:
        return []
    return [Warning(
        "L032", Severity.WARNING,
        "A statement returning TotalRecords/TotalCount breaks when the C# call passes "
        "filterString: a pager that wraps "
        "the piece starting at a line-leading SELECT that mentions TotalRecords produces "
        "SQL that is not valid PostgreSQL (42601 at run time). Usual causes: a nested "
        "SELECT on its own line after a comment, the statement's own WHERE (the pager adds "
        "a second one), LIMIT before OFFSET. Keep `SELECT ..., COUNT(*) OVER() AS "
        "TotalRecords FROM t ORDER BY x` on its own lines and put filters in a CTE before it.")]
