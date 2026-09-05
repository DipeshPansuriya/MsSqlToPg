"""Query Insights -- analysis presented ALONGSIDE the SQL, never inside it.

Replaces the earlier inline-comment annotator. Two reasons that was wrong:

  1. The converted SQL should be paste-ready. Comments belong in a panel.
  2. Inline comments hid a contradiction. A comment claimed
     "BIT -> BOOLEAN" while the warning panel simultaneously reported that BIT
     had survived. Both came from the same run.

Every "changed" claim here is VERIFIED: a rule may only assert `X -> Y` if the
T-SQL form is actually ABSENT from the output. Detecting that Y is present is not
evidence that X is gone.

Claims are also matched against SQL with string literals masked, so a query
selecting the text 'true' is never mistaken for a boolean conversion.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from core.linter import mask_literals
from core.models import Severity, Warning

_FLAGS = re.IGNORECASE | re.MULTILINE


# --------------------------------------------------------------------------
# Statement splitting -- respects $$ blocks, literals and comments
# --------------------------------------------------------------------------

def split_statements(sql: str) -> list[str]:
    """Split on top-level semicolons, keeping $$ blocks and literals intact."""
    statements: list[str] = []
    buffer: list[str] = []
    i, n = 0, len(sql)
    in_single = in_line_comment = in_block_comment = False
    dollar_tag: str | None = None

    while i < n:
        ch = sql[i]
        nxt = sql[i + 1] if i + 1 < n else ""

        if in_line_comment:
            buffer.append(ch)
            if ch == "\n":
                in_line_comment = False
            i += 1
            continue
        if in_block_comment:
            buffer.append(ch)
            if ch == "*" and nxt == "/":
                buffer.append(nxt)
                in_block_comment = False
                i += 2
                continue
            i += 1
            continue
        if dollar_tag is not None:
            if sql.startswith(dollar_tag, i):
                buffer.append(dollar_tag)
                i += len(dollar_tag)
                dollar_tag = None
                continue
            buffer.append(ch)
            i += 1
            continue
        if in_single:
            buffer.append(ch)
            if ch == "'":
                if nxt == "'":
                    buffer.append(nxt)
                    i += 2
                    continue
                in_single = False
            i += 1
            continue

        if ch == "-" and nxt == "-":
            in_line_comment = True
        elif ch == "/" and nxt == "*":
            in_block_comment = True
        elif ch == "'":
            in_single = True
        elif ch == "$":
            tag = re.match(r"\$[A-Za-z_]?\w*\$", sql[i:])
            if tag:
                dollar_tag = tag.group(0)
                buffer.append(dollar_tag)
                i += len(dollar_tag)
                continue
        elif ch == ";":
            statements.append("".join(buffer).strip())
            buffer = []
            i += 1
            continue

        buffer.append(ch)
        i += 1

    tail = "".join(buffer).strip()
    if tail:
        statements.append(tail)
    return [s for s in statements if s]


# --------------------------------------------------------------------------
# WHAT the query does
# --------------------------------------------------------------------------

_DO_BLOCK_RE = re.compile(r"^\s*DO\s*\$", re.IGNORECASE)


def describe(statement: str) -> str:
    if _DO_BLOCK_RE.match(statement):
        return _describe_do_block(statement)
    try:
        import sqlglot
        from sqlglot import expressions as exp
        tree = sqlglot.parse_one(statement, read="postgres")
    except Exception:
        return _describe_by_keyword(statement)
    if tree is None:
        return _describe_by_keyword(statement)

    tables = [t.name for t in tree.find_all(exp.Table)]
    first = tables[0] if tables else None

    if isinstance(tree, exp.Create):
        kind = (tree.args.get("kind") or "OBJECT").upper()
        # CREATE TABLE wraps the table in a Schema node (it carries the column
        # definitions), so tree.this.name is empty -- descend one level.
        target = tree.this
        if isinstance(target, exp.Schema):
            target = target.this
        name = target.name if target is not None and hasattr(target, "name") else ""
        if kind == "TABLE":
            temp = "TEMPORARY" in statement.upper()[:60] or "TEMP" in statement.upper()[:60]
            return f"Create {'a temporary' if temp else 'a'} table \"{name}\"".rstrip()
        return f"Create {kind.lower()} \"{name}\"".rstrip()
    if isinstance(tree, exp.Insert):
        return f"Insert rows into \"{first}\"" if first else "Insert rows"
    if isinstance(tree, exp.Update):
        return f"Update rows in \"{first}\"" if first else "Update rows"
    if isinstance(tree, exp.Delete):
        return f"Delete rows from \"{first}\"" if first else "Delete rows"
    if isinstance(tree, exp.Merge):
        return f"Merge into \"{first}\"" if first else "Merge rows"
    if isinstance(tree, exp.Drop):
        return f"Drop \"{first}\"" if first else "Drop object"
    if isinstance(tree, (exp.Select, exp.Union, exp.Subquery)):
        return _describe_select(tree)
    return _describe_by_keyword(statement)


def _describe_select(tree) -> str:
    from sqlglot import expressions as exp

    if isinstance(tree, exp.Union):
        # find_all() includes this node, so N Union nodes == N+1 branches.
        branches = len(list(tree.find_all(exp.Union))) + 1
        return f"Combine {branches} result sets with UNION"

    tables: list[str] = []
    for table in tree.find_all(exp.Table):
        if table.name and table.name not in tables:
            tables.append(table.name)

    selected = tree.expressions if hasattr(tree, "expressions") else []
    star = any(isinstance(e, exp.Star) for e in selected)
    columns = "all columns" if (star or not selected) else f"{len(selected)} columns"

    parts = [f"Select {columns}"]
    if tables:
        parts[0] += f' from "{tables[0]}"'
        if len(tables) > 1:
            parts.append(f"joined with {len(tables) - 1} other table(s)")
    if len(list(tree.find_all(exp.CTE))):
        parts.append(f"using {len(list(tree.find_all(exp.CTE)))} CTE(s)")
    if next(tree.find_all(exp.Group), None):
        parts.append("aggregated by group")
    if next(tree.find_all(exp.Window), None):
        parts.append("with window function(s)")
    if next(tree.find_all(exp.Limit), None):
        parts.append("row-limited")
    return ", ".join(parts)


def _describe_do_block(statement: str) -> str:
    body = statement.upper()
    bits = []
    if "DECLARE" in body:
        bits.append("declares local variables")
    if "EXECUTE" in body:
        bits.append("runs dynamic SQL")
    return "Anonymous PL/pgSQL block" + (" -- " + ", ".join(bits) if bits else "")


def _describe_by_keyword(statement: str) -> str:
    word = re.match(r"\s*(\w+)", statement)
    return f"{word.group(1).upper()} statement" if word else "SQL statement"


# --------------------------------------------------------------------------
# WHAT CHANGED -- every claim is verified
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Change:
    note: str
    original: str              # the T-SQL form that must be present in the source
    converted: str | None = None   # the PostgreSQL form expected in the output
    must_be_gone: str | None = None  # the T-SQL form that must be ABSENT to claim success


# `must_be_gone` is what stops the tool lying. Claiming "BIT -> BOOLEAN" while
# CAST(x AS BIT) is still in the output is worse than saying nothing.
_CHANGES = (
    Change("TOP n -> LIMIT n", r"\bTOP\b", r"\bLIMIT\b", r"\bTOP\b"),
    Change("ISNULL() -> COALESCE()", r"\bISNULL\s*\(", r"\bCOALESCE\s*\(",
           r"\bISNULL\s*\("),
    Change("CONVERT(varchar, value, style) -> TO_CHAR()", r"\bCONVERT\s*\(",
           r"\bTO_CHAR\s*\(", r"\bCONVERT\s*\("),
    Change("CHARINDEX() -> POSITION()", r"\bCHARINDEX\s*\(", r"\bPOSITION\s*\(",
           r"\bCHARINDEX\s*\("),
    Change("CROSS/OUTER APPLY -> JOIN LATERAL", r"\b(?:CROSS|OUTER)\s+APPLY\b",
           r"\bLATERAL\b", r"\bAPPLY\b"),
    Change("IDENTITY(seed, step) -> GENERATED AS IDENTITY", r"\bIDENTITY\s*\(",
           r"\bGENERATED\b", r"\bIDENTITY\s*\("),
    Change("GETDATE() -> CURRENT_TIMESTAMP", r"\bGETDATE\s*\(",
           r"\bCURRENT_TIMESTAMP\b", r"\bGETDATE\s*\("),
    Change("DATEADD() -> INTERVAL arithmetic", r"\bDATEADD\s*\(",
           r"\bINTERVAL\b", r"\bDATEADD\s*\("),
    Change("String concatenation '+' -> '||'", r"\+\s*'|'\s*\+", r"\|\|",
           r"\+\s*'|'\s*\+"),
    Change("NVARCHAR -> VARCHAR (PostgreSQL text is Unicode already)",
           r"\bNVARCHAR\b", r"\bVARCHAR\b", r"\bNVARCHAR\b"),
    Change("VARCHAR(MAX) -> TEXT", r"\b(?:N)?VARCHAR\s*\(\s*MAX\s*\)", r"\bTEXT\b",
           r"\b(?:N)?VARCHAR\s*\(\s*MAX\s*\)"),
    Change("BIT -> BOOLEAN", r"\bBIT\b", r"\bBOOLEAN\b", r"(?<![\w.])BIT\b"),
    Change("EXEC() -> EXECUTE", r"\bEXEC\s*\(", r"\bEXECUTE\b", r"\bEXEC\s*\("),
    Change("DECLARE/EXEC wrapped in a PL/pgSQL DO block",
           r"\bDECLARE\s+@", r"\bDO\s*\$", r"\bDECLARE\s+@"),
    Change("WITH (NOLOCK) removed -- PostgreSQL readers never block writers",
           r"\bNOLOCK\b", None, r"\bNOLOCK\b"),
    Change("[bracket] identifiers -> \"double quotes\"", r"\[\w+\]", r'"\w+"',
           r"\[\w+\]"),
)


def changes(original: str, converted: str) -> list[str]:
    """Verified conversion notes. A claim is dropped if it cannot be substantiated."""
    src = mask_literals(original)
    out = mask_literals(converted)
    notes: list[str] = []
    for rule in _CHANGES:
        if not re.search(rule.original, src, _FLAGS):
            continue                                   # never was in the source
        if rule.converted and not re.search(rule.converted, out, _FLAGS):
            continue                                   # replacement not present
        if rule.must_be_gone and re.search(rule.must_be_gone, out, _FLAGS):
            continue                                   # the T-SQL form SURVIVED
        notes.append(rule.note)
    return notes


def unconverted(original: str, converted: str) -> list[str]:
    """T-SQL constructs that were expected to change but are still present.

    This is the honest counterpart to changes(): the same rules, reported when
    the claim FAILS. It is what a false "BIT -> BOOLEAN" note was hiding.
    """
    src = mask_literals(original)
    out = mask_literals(converted)
    notes: list[str] = []
    for rule in _CHANGES:
        if not rule.must_be_gone:
            continue
        if not re.search(rule.original, src, _FLAGS):
            continue
        if re.search(rule.must_be_gone, out, _FLAGS):
            head = rule.note.split(" -> ")[0].split(" --")[0]
            notes.append(f"{head} is STILL PRESENT in the output -- not converted")
    return notes


# --------------------------------------------------------------------------
# Stats
# --------------------------------------------------------------------------

def stats(original: str, converted: str) -> dict[str, object]:
    data: dict[str, object] = {
        "Statements": len(split_statements(converted)),
        "Input size": f"{len(original):,} chars",
        "Output size": f"{len(converted):,} chars",
    }
    try:
        import sqlglot
        from sqlglot import expressions as exp
        trees = sqlglot.parse(converted, read="postgres")
        tables, joins, ctes = set(), 0, 0
        for tree in trees:
            if tree is None:
                continue
            for table in tree.find_all(exp.Table):
                if table.name:
                    tables.add(table.name)
            joins += len(list(tree.find_all(exp.Join)))
            ctes += len(list(tree.find_all(exp.CTE)))
        if tables:
            data["Tables"] = ", ".join(sorted(tables))
        if joins:
            data["Joins"] = joins
        if ctes:
            data["CTEs"] = ctes
    except Exception:
        pass

    params = sorted(set(re.findall(r"(?<!@)@([A-Za-z_]\w*)",
                                   mask_literals(converted))))
    if params:
        data["Parameters"] = ", ".join("@" + p for p in params)
    return data


# --------------------------------------------------------------------------
# Public result
# --------------------------------------------------------------------------

@dataclass
class QueryInsights:
    syntax: str = ""              # VALID / INVALID / NOT CHECKED
    what: list[str] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)
    attention: list[str] = field(default_factory=list)
    stats: dict = field(default_factory=dict)

    @property
    def is_empty(self) -> bool:
        return not (self.what or self.changed or self.attention or self.syntax)


def analyze(original: str, converted: str,
            warnings: list[Warning] | None = None,
            validation=None) -> QueryInsights:
    """Build the insight set shown next to the converted SQL."""
    result = QueryInsights()
    if not converted.strip():
        return result

    if validation is None:
        from core import validator
        validation = validator.validate(converted)
    result.syntax = validation.verdict

    result.what = [describe(s) for s in split_statements(converted)]
    result.changed = changes(original, converted)
    result.stats = stats(original, converted)

    result.attention = list(unconverted(original, converted))
    for warning in warnings or []:
        if warning.severity is Severity.INFO:
            continue
        where = f" (line {warning.line})" if warning.line else ""
        entry = f"{warning.code}: {warning.message}{where}"
        if entry not in result.attention:
            result.attention.append(entry)
    return result


def render_text(insights: QueryInsights) -> str:
    """Plain-text rendering for the CLI."""
    if insights.is_empty:
        return ""
    lines: list[str] = []
    if insights.syntax:
        lines.append(f"PostgreSQL syntax: {insights.syntax}")
        lines.append("")
    if insights.what:
        lines.append("What this query does:")
        lines += [f"  - {w}" for w in insights.what]
    if insights.changed:
        lines.append("")
        lines.append("What changed from T-SQL:")
        lines += [f"  - {c}" for c in insights.changed]
    if insights.attention:
        lines.append("")
        lines.append("Needs attention:")
        lines += [f"  ! {a}" for a in insights.attention]
    if insights.stats:
        lines.append("")
        lines.append("Query facts:")
        lines += [f"  - {k}: {v}" for k, v in insights.stats.items()]
    return "\n".join(lines)
