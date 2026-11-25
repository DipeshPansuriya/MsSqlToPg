"""A054 (v2.16.0) -- SQL Server implicit conversions, made explicit with the target schema.

SQL Server converts silently where PostgreSQL refuses, so these PARSE and fail only at run time:

    int_col = text_col           42883 operator does not exist: integer = text
    text_col = 0                 42883 operator does not exist: text = integer
    id IN (SELECT value FROM STRING_TO_TABLE(...))      integer = text
    num_col LIKE '%' || @x || '%'                       numeric ~~ text
    COALESCE(int_col, '') / CASE ... THEN 1 ELSE '' END  22P02 invalid input syntax for type integer: ""
    date_col = '' / COALESCE(date_col, '')               22007 invalid input syntax for type date: ""
    fnGetLocalTime(text_col, @tz), TO_CHAR(text_col, ..) 42883 function ... (text, unknown) does not exist
    SELECT bool_col ... UNION SELECT 0                   42804 UNION types boolean and integer cannot be matched

Column types come from column_types.txt (schema_hints.column_types()) AND from the batch itself: a
temp table (`CREATE TABLE #t (c VARCHAR(10))`, `SELECT ... INTO #t`, `CREATE TEMPORARY TABLE t AS
SELECT`), a CTE, a table variable or a derived table declares its own columns, typed from the
definition or from the projected expressions. Without any of these only the schema-free CASE rule
runs. Each fix reproduces what SQL Server did:
    * a text side compared with a number is converted to that number type -- `CAST(NULLIF(TRIM(x), '')
      AS INTEGER)` (SQL Server also trimmed spaces; '' gives NULL instead of 0, so it matches nothing);
    * a number literal compared with a text column is quoted (`= '0'`), so non-numeric data cannot raise;
    * an integer column compared with TRUE/FALSE (a ^Is... name that is not a flag) gets its 1/0 back;
    * LIKE on a number or date converts it to text, as SQL Server converted it to varchar; on a
      boolean it becomes '1'/'0', the text SQL Server made of a BIT;
    * '' in a number context is 0, in a date context 1900-01-01 (SQL Server's conversion of '');
    * text passed to fnGetLocalTime / TO_CHAR is cast to TIMESTAMP;
    * a UNION column mixing a boolean branch with an integer one is INTEGER, as SQL Server's type
      precedence made it (bit + int -> int), so the boolean branch is cast to INTEGER.

The pass parses the generated PostgreSQL once, collects small text edits from node positions and applies
them back to front, so every untouched character of the output stays as it was. It never raises: any parse
problem leaves the SQL unchanged.

`Resolver` is also what A007 / A014 / A020 (core/autofix.py) ask before treating a ^Is... column as
boolean: a column the schema or the batch declares as anything else is left alone.
"""
from __future__ import annotations

import re

import sqlglot
from sqlglot import exp

from core import schema_hints

_NUMERIC = {"integer", "numeric"}
_DATELIKE = {"date", "timestamp"}
_PG_TYPE = {"integer": "INTEGER", "numeric": "NUMERIC"}
_SPLIT_FUNCS = {"string_to_table", "unnest", "regexp_split_to_table", "string_to_array"}
_SPLIT_ITEM_NAMES = ("value", "items", "item", "splitdata", "v")
_TIMESTAMP_ARG_FUNCS = {"fngetlocaltime", "to_char", "time_to_str"}
_LITERAL_FAMILY = {"intlit": "integer", "numlit": "numeric", "strlit": "text", "splittext": "text"}
_TEXT_FUNCS = (exp.DPipe, exp.Concat, exp.Upper, exp.Lower, exp.Trim, exp.Substring, exp.Left, exp.Right,
               exp.TimeToStr)
_PARAM_LITERAL = re.compile(r"^@[A-Za-z_]\w*$")
_ARITHMETIC = (exp.Add, exp.Sub, exp.Mul, exp.Div)
_COMPARISONS = (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE)
_DATE_UNITS = {"day", "days", "week", "weeks", "month", "months", "year", "years"}
_NULL_CAST = {"integer": "INTEGER", "numeric": "NUMERIC", "boolean": "BOOLEAN", "date": "DATE",
              "timestamp": "TIMESTAMP"}


def _span(node) -> tuple[int, int] | None:
    """(start, end) character offsets (end inclusive) of a Column or Literal in the parsed text."""
    if isinstance(node, exp.Column):
        name = node.this
        meta = getattr(name, "meta", None) or {}
        if "start" not in meta:
            return None
        start, end = meta["start"], meta["end"]
        for part in ("table", "db", "catalog"):
            qual = node.args.get(part)
            qmeta = getattr(qual, "meta", None) or {}
            if "start" in qmeta:
                start = min(start, qmeta["start"])
        return start, end
    if isinstance(node, exp.Literal):
        meta = node.meta or {}
        if "start" in meta:
            return meta["start"], meta["end"]
    return None


def _scope_nodes(node):
    """The query blocks (innermost first) whose FROM/target can name this node's tables."""
    cur = node.parent
    while cur is not None:
        if isinstance(cur, (exp.Select, exp.Update, exp.Delete)):
            yield cur
        cur = cur.parent


def _func_name(node) -> str:
    if isinstance(node, exp.Anonymous):
        return str(node.this).lower()
    return (node.sql_name() if hasattr(node, "sql_name") else type(node).__name__).lower()


def _contains_split(node) -> bool:
    for f in node.find_all(exp.Func):
        if _func_name(f) in _SPLIT_FUNCS or (isinstance(f, exp.Anonymous) and str(f.this).lower() in _SPLIT_FUNCS):
            return True
    return False


def _datatype_family(dt) -> str | None:
    if not isinstance(dt, exp.DataType):
        return None
    if dt.this in (exp.DataType.Type.BIT, exp.DataType.Type.BOOLEAN):
        return "boolean"   # T-SQL BIT is the boolean (A008 turns it into BOOLEAN later)
    try:
        family = schema_hints.type_family(dt.sql(dialect="postgres"))
    except Exception:
        return None
    return None if family == "other" else family


def _table_key(table) -> str:
    """Lower-cased name a Table / DeclareItem is known by (temp '#' and table-variable '@' dropped)."""
    name = table.name if isinstance(table, exp.Table) else ""
    return (name or "").lstrip("#@").lower()


def _branch_selects(query) -> list:
    """The SELECT blocks of a set operation, left to right (a plain SELECT is its own branch)."""
    if isinstance(query, exp.Subquery):
        return _branch_selects(query.this)
    if isinstance(query, exp.SetOperation):
        return _branch_selects(query.left) + _branch_selects(query.right)
    if isinstance(query, exp.Select):
        return [query]
    return []


def _is_interval(node) -> bool:
    """INTERVAL '1 MINUTE', or INTERVAL '1 MINUTE' * n (how DATEADD with a variable amount converts)."""
    if isinstance(node, exp.Mul):
        return isinstance(node.left, exp.Interval) or isinstance(node.right, exp.Interval)
    return isinstance(node, exp.Interval)


def _norm(family: str | None) -> str | None:
    return _LITERAL_FAMILY.get(family, family) if family else None


def _merge(families) -> str | None:
    """One family for several branches (CASE / UNION): equal ones, integer+numeric -> numeric, else unknown."""
    found = {f for f in (_norm(x) for x in families) if f}
    if len(found) == 1:
        return found.pop()
    if found and found <= _NUMERIC:
        return "numeric"
    return None


class Resolver:
    """Type families of columns and expressions in one batch: column_types.txt plus the batch's own
    temp tables, table variables, CTEs and derived tables. Families: integer, numeric, boolean, text,
    date, timestamp, time, uuid, bytea; expressions also give intlit / numlit / strlit (literals) and
    splittext (an item of a STRING_TO_TABLE source); None means unknown."""

    def __init__(self, trees, types: dict[str, dict[str, str]] | None = None):
        base = schema_hints.column_types() if types is None else types
        self.types: dict[str, dict[str, str | None]] = dict(base)
        self.local: set[str] = set()
        self._projections: dict[int, list[tuple[str | None, str | None]]] = {}
        for tree in trees:
            try:
                self._collect(tree)
            except Exception:
                continue

    # ---- the batch's own tables -------------------------------------------------------------------
    def _define(self, key: str, columns: dict[str, str | None]) -> None:
        if key and columns:
            self.types[key] = columns   # a temp table / CTE shadows a schema table of the same name
            self.local.add(key)         # ...and its column order is the definition's

    def _collect(self, tree) -> None:
        for cte in tree.find_all(exp.CTE):
            cols = self.query_columns(cte.this)
            alias = cte.args.get("alias")
            names = [c.name.lower() for c in (alias.columns if alias is not None else [])]
            if names:
                fams = [f for _, f in self.projection(cte.this)]
                cols = {n: (fams[i] if i < len(fams) else None) for i, n in enumerate(names)}
            self._define((cte.alias_or_name or "").lower(), cols)
        if isinstance(tree, exp.Create) and str(tree.args.get("kind") or "").upper() == "TABLE":
            target = tree.this
            if isinstance(target, exp.Schema) and isinstance(target.this, exp.Table):
                self._define(_table_key(target.this), self._column_defs(target.expressions))
            elif isinstance(target, exp.Table) and tree.expression is not None:
                self._define(_table_key(target), self.query_columns(tree.expression))
        for select in tree.find_all(exp.Select):
            into = select.args.get("into")
            if into is not None and isinstance(into.this, exp.Table):
                self._define(_table_key(into.this), self.query_columns(select))
        if isinstance(tree, exp.Alter) and isinstance(tree.this, exp.Table):
            added = self._column_defs(tree.args.get("actions") or [])
            if added:
                key = _table_key(tree.this)
                self._define(key, {**self.types.get(key, {}), **added})
        if isinstance(tree, exp.Declare):
            for item in tree.expressions:
                kind = item.args.get("kind")
                if isinstance(kind, exp.Schema):
                    names = item.this if isinstance(item.this, list) else [item.this]
                    for var in names:
                        self._define(str(getattr(var, "name", "") or "").lstrip("@").lower(),
                                     self._column_defs(kind.expressions))

    @staticmethod
    def _column_defs(defs) -> dict[str, str | None]:
        return {d.name.lower(): _datatype_family(d.args.get("kind"))
                for d in defs if isinstance(d, exp.ColumnDef) and d.name}

    # ---- projections ------------------------------------------------------------------------------
    def projection(self, query) -> list[tuple[str | None, str | None]]:
        """[(column name, family)] a query returns, in order (names lower-cased, None if unnamed)."""
        if isinstance(query, exp.Subquery):
            return self.projection(query.this)
        if isinstance(query, exp.SetOperation):
            left, right = self.projection(query.left), self.projection(query.right)
            return [(name, _merge([fam] + ([right[i][1]] if i < len(right) else [])))
                    for i, (name, fam) in enumerate(left)]
        if not isinstance(query, exp.Select):
            return []
        key = id(query)
        if key in self._projections:
            return self._projections[key]
        self._projections[key] = []   # recursion guard
        out: list[tuple[str | None, str | None]] = []
        srcs = self.sources(query)
        for item in query.expressions:
            if isinstance(item, exp.Star):
                for src in srcs.values():
                    out.extend((self._source_columns(src) or {}).items())
            elif isinstance(item, exp.Column) and isinstance(item.this, exp.Star):
                src = srcs.get((item.table or "").lower())
                out.extend((self._source_columns(src) or {}).items() if src else [])
            elif isinstance(item, exp.Alias):
                out.append((item.alias.lower() or None, _norm(self.expr(item.this))))
            elif isinstance(item, exp.Column):
                out.append((item.name.lower() or None, _norm(self.column(item))))
            else:
                out.append((None, _norm(self.expr(item))))
        self._projections[key] = out
        return out

    def query_columns(self, query) -> dict[str, str | None]:
        cols: dict[str, str | None] = {}
        for name, fam in self.projection(query):
            if name and name not in cols:
                cols[name] = fam
        return cols

    # ---- name resolution ---------------------------------------------------------------------------
    def sources(self, scope) -> dict[str, tuple]:
        """alias -> ("table", name) | ("query", select) | ("split",) | ("other",) for one query block."""
        found: dict[str, tuple] = {}
        items = []
        if isinstance(scope, (exp.Update, exp.Delete)) and isinstance(scope.this, exp.Table):
            items.append(scope.this)
        frm = scope.args.get("from_") or scope.args.get("from")
        if frm is not None:
            items.append(frm.this)
            items.extend(j.this for j in (frm.this.args.get("joins") or []) if isinstance(frm.this, exp.Expression))
        items.extend(j.this for j in (scope.args.get("joins") or []))
        for using in scope.args.get("using") or []:
            items.append(using)
        for item in items:
            if isinstance(item, exp.Table):
                alias = (item.alias or item.name or "").lower()
                if isinstance(item.this, exp.Func):
                    found[alias] = ("split",) if _contains_split(item) else ("other",)
                else:
                    found[alias] = ("table", _table_key(item))
            elif isinstance(item, exp.Subquery):
                alias = (item.alias or "").lower()
                found[alias] = ("split",) if _contains_split(item) else ("query", item.this)
            elif isinstance(item, (exp.Lateral, exp.Unnest, exp.Func)):
                found[(item.alias or "").lower()] = ("split",) if _contains_split(item) else ("other",)
        return found

    def _source_columns(self, src) -> dict[str, str | None] | None:
        """The columns one FROM source declares (None = not known)."""
        if src is None:
            return None
        if src[0] == "table":
            return self.types.get(src[1])
        if src[0] == "query":
            return self.query_columns(src[1]) or None
        if src[0] == "split":
            return {n: "splittext" for n in _SPLIT_ITEM_NAMES}
        return None

    def column(self, col: exp.Column) -> str | None:
        """Type family of a column, 'splittext' for a split item, None if unknown."""
        name = (col.name or "").lower()
        qual = (col.table or "").lower()
        for scope in _scope_nodes(col):
            srcs = self.sources(scope)
            if qual:
                if qual in srcs:
                    cols = self._source_columns(srcs[qual])
                    return cols.get(name) if cols else None
                continue
            known = [self._source_columns(s) for s in srcs.values()]
            declaring = [c[name] for c in known if c and name in c]
            if len(declaring) == 1:
                return declaring[0]
            if declaring:
                return _merge(declaring) if len(set(declaring)) == 1 else None
            if srcs and any(c is None for c in known):
                return None
            # every source is known and none has it: a correlated reference to an outer block
        return None

    def expr(self, node) -> str | None:
        if isinstance(node, exp.Column):
            return self.column(node)
        if isinstance(node, (exp.Cast, exp.TryCast)):
            return _datatype_family(node.to)
        if isinstance(node, exp.Convert):
            return _datatype_family(node.this)
        if isinstance(node, exp.Literal):
            if node.is_string:
                # '@Name' is not text: ACS binds the exact '@Name' literal as a parameter
                return None if _PARAM_LITERAL.match(str(node.this)) else "strlit"
            return "intlit" if str(node.this).lstrip("-").isdigit() else "numlit"
        if isinstance(node, (exp.Add, exp.Sub)) and _is_interval(node.right):
            # DATEADD(MINUTE, n, col) -> col + INTERVAL '1 MINUTE' [* n]: a timestamp in PostgreSQL
            return "timestamp" if _norm(self.expr(node.left)) in _DATELIKE else None
        if isinstance(node, exp.Add):
            left, right = _norm(self.expr(node.left)), _norm(self.expr(node.right))
            if left == right == "text":
                return "text"
            return _merge([left, right]) if left in _NUMERIC and right in _NUMERIC else None
        if isinstance(node, (exp.Paren, exp.Alias)):
            return self.expr(node.this)
        if isinstance(node, exp.Boolean):
            return "boolean"
        if isinstance(node, exp.Coalesce):
            args = [node.this] + list(node.expressions)
            if any(isinstance(a, exp.Boolean) for a in args):
                return "boolean"
            return self.expr(node.this)
        if isinstance(node, exp.Case):
            branches = [i.args.get("true") for i in node.args.get("ifs") or []] + [node.args.get("default")]
            return _merge(self.expr(b) for b in branches if b is not None and not isinstance(b, exp.Null))
        if isinstance(node, exp.Count):
            return "integer"
        if isinstance(node, exp.Window):
            return "integer" if isinstance(node.this, exp.RowNumber) else self.expr(node.this)
        if isinstance(node, (exp.Max, exp.Min)):
            return _norm(self.expr(node.this))
        if isinstance(node, _TEXT_FUNCS):
            return "text"
        return None

    def not_boolean(self, col) -> bool:
        """True when the schema or the batch declares this column as something other than boolean."""
        if not isinstance(col, exp.Column):
            return False
        try:
            family = self.column(col)
        except Exception:
            return False
        return family is not None and family != "boolean"


def _wrap(text: str, span: tuple[int, int], before: str, after: str) -> tuple[int, int, str]:
    return span[0], span[1], before + text[span[0]:span[1] + 1] + after


_SELECT_LIST_WORDS = ("SELECT", "DISTINCT", "ALL")


def _projection_span(sql: str, mask: str, item) -> tuple[int, int] | None:
    """(start, end) of a select-list item's expression (the part before ` AS alias`)."""
    if isinstance(item, exp.Alias):
        ident = item.args.get("alias")
        meta = getattr(ident, "meta", None) or {}
        if "start" not in meta:
            return None
        head = mask[:meta["start"]]
        m = re.search(r"\s+AS\s+$", head, re.I)
        if not m:
            return None
        end = m.start() - 1
    elif isinstance(item, (exp.Column, exp.Literal)):
        return _span(item)
    else:
        return None
    depth, i = 0, end
    while i >= 0:
        c = mask[i]
        if c == ")":
            depth += 1
        elif c == "(":
            if depth == 0:
                break
            depth -= 1
        elif depth == 0 and c == ",":
            break
        elif depth == 0 and c.isalpha() and not (mask[i + 1:i + 2].isalnum() or mask[i + 1:i + 2] == "_"):
            j = i
            while j > 0 and (mask[j - 1].isalnum() or mask[j - 1] == "_"):
                j -= 1
            if mask[j:i + 1].upper() in _SELECT_LIST_WORDS:
                break
        i -= 1
    start = i + 1
    while start <= end and mask[start].isspace():
        start += 1
    return (start, end) if start <= end else None


def apply(sql: str) -> tuple[str, int]:
    """Returns (sql, number of conversions made explicit)."""
    try:
        trees = [t for t in sqlglot.parse(sql, read="postgres") if t is not None]
    except Exception:
        return sql, 0
    try:
        R = Resolver(trees)
    except Exception:
        return sql, 0
    edits: list[tuple[int, int, str]] = []
    try:
        _collect_edits(sql, trees, R, edits)
    except Exception:
        return sql, 0

    if not edits:
        return sql, 0
    # drop overlaps (keep the earliest-found edit), then apply back to front
    edits = sorted(set(edits))
    kept: list[tuple[int, int, str]] = []
    for e in edits:
        if kept and e[0] <= kept[-1][1]:
            continue
        kept.append(e)
    out = sql
    for start, end, text in reversed(kept):
        out = out[:start] + text + out[end + 1:]
    return out, len(kept)


def _call_span(mask: str, node) -> tuple[int, int] | None:
    """(start, end) of a function call `NAME( ... )` from the name token's position."""
    meta = getattr(node, "meta", None) or {}
    if "start" not in meta:
        return None
    i = meta["end"] + 1
    while i < len(mask) and mask[i].isspace():
        i += 1
    if i >= len(mask) or mask[i] != "(":
        return None
    depth = 0
    for j in range(i, len(mask)):
        if mask[j] == "(":
            depth += 1
        elif mask[j] == ")":
            depth -= 1
            if depth == 0:
                return meta["start"], j
    return None


def _expr_call_span(mask: str, node) -> tuple[int, int] | None:
    """`_call_span`, and for CAST (no position of its own) the `CAST(` found just before its value."""
    span = _call_span(mask, node)
    if span or not isinstance(node, exp.Cast):
        return span
    inner = node.this
    while isinstance(inner, exp.Paren):
        inner = inner.this
    first = _first_start(inner)
    if first is None:
        return None
    m = re.search(r"CAST\s*\((?:\s*\()*\s*$", mask[:first], re.I)
    if not m:
        return None
    open_ = mask.index("(", m.start())
    depth = 0
    for j in range(open_, len(mask)):
        if mask[j] == "(":
            depth += 1
        elif mask[j] == ")":
            depth -= 1
            if depth == 0:
                return m.start(), j
    return None


def _first_start(node) -> int | None:
    if isinstance(node, (exp.Column, exp.Literal)):
        sp = _span(node)
        return sp[0] if sp else None
    if isinstance(node, exp.Binary):
        return _first_start(node.left)
    return None


def _last_end(node) -> int | None:
    if isinstance(node, (exp.Column, exp.Literal)):
        sp = _span(node)
        return sp[1] if sp else None
    if isinstance(node, exp.Binary):
        return _last_end(node.right)
    return None


def _numeric_context(node, R: Resolver) -> str | None:
    """The number family `node` is compared with or computed against, if any."""
    parent = node.parent
    while isinstance(parent, exp.Paren):
        node, parent = parent, parent.parent
    if isinstance(parent, _COMPARISONS + _ARITHMETIC):
        other = parent.right if parent.left is node else parent.left
        family = _norm(R.expr(other))
        if family in _NUMERIC:
            return family
        if isinstance(parent, _ARITHMETIC):
            return _numeric_context(parent, R)
    return None


def _collect_edits(sql: str, trees, R: Resolver, edits: list) -> None:
    from core import pgfixes
    mask = pgfixes.code_mask(sql)
    handled: set[int] = set()

    def to_number(other, family):
        sp = _span(other)
        if sp:
            edits.append(_wrap(sql, sp, "CAST(NULLIF(TRIM(", f"), '') AS {_PG_TYPE.get(family, 'INTEGER')})"))

    for tree in trees:
        for cmp in tree.find_all(*_COMPARISONS):
            left, right = cmp.left, cmp.right
            for a, b in ((left, right), (right, left)):
                ta, tb = R.expr(a), R.expr(b)
                if ta in _NUMERIC and isinstance(b, exp.Column) and tb in ("text", "splittext"):
                    to_number(b, ta)
                    break
                if (ta in _NUMERIC and isinstance(b, exp.Coalesce) and isinstance(b.this, exp.Column)
                        and R.expr(b.this) in ("text", "splittext")):
                    # id = COALESCE(text_col, 0): SQL Server converted the text; cast it inside
                    to_number(b.this, ta)
                    handled.add(id(b))
                    break
                if ta in _NUMERIC and isinstance(b, exp.Func) and _norm(tb) == "text" and id(b) not in handled:
                    # id = CAST((@VarSelected) AS TEXT) -- an inlined NVARCHAR variable (A031)
                    sp = _expr_call_span(mask, b)
                    if sp:
                        edits.append(_wrap(sql, sp, "CAST(NULLIF(TRIM(", f"), '') AS {_PG_TYPE.get(ta, 'INTEGER')})"))
                        handled.add(id(b))
                    break
                if ta in _NUMERIC and isinstance(b, exp.Literal) and b.is_string and str(b.this) == "":
                    # COALESCE(@Top, 0) = '': SQL Server read '' as 0
                    sp = _span(b)
                    if sp:
                        edits.append((sp[0], sp[1], "0"))
                    break
                if ta == "text" and isinstance(a, exp.Column) and tb == "intlit":
                    sp = _span(b)
                    if sp:
                        edits.append((sp[0], sp[1], f"'{b.this}'"))
                    break
                if ta in _DATELIKE and isinstance(b, exp.Literal) and b.is_string and str(b.this) == "":
                    sp = _span(b)
                    if sp:
                        edits.append((sp[0], sp[1], "'1900-01-01'"))
                    break
                if (ta in _NUMERIC and isinstance(a, exp.Column) and isinstance(b, exp.Boolean)
                        and isinstance(cmp, (exp.EQ, exp.NEQ))):
                    # f.IsOceanConfig = TRUE where #OceanFlag made it `CASE ... THEN 1 ELSE 0 END`
                    sp = _boolean_span(sql, b, a)
                    if sp:
                        edits.append((sp[0], sp[1], "1" if b.this else "0"))
                    break
        for node in tree.find_all(exp.In):
            left = node.this
            if R.expr(left) not in _NUMERIC:
                continue
            query = node.args.get("query")
            select = query.this if isinstance(query, exp.Subquery) else query
            if isinstance(select, exp.Select) and len(select.expressions) == 1 and _contains_split(select):
                proj = select.expressions[0]
                proj = proj.this if isinstance(proj, exp.Alias) else proj
                family = _PG_TYPE.get(R.expr(left), "INTEGER")
                if isinstance(proj, exp.Column) and R.expr(proj) in (None, "text", "splittext"):
                    to_number(proj, R.expr(left))
                elif isinstance(proj, exp.Func) and _norm(R.expr(proj)) in (None, "text"):
                    # IN (SELECT LTRIM(RTRIM(value)) FROM STRING_TO_TABLE(...))
                    sp = _call_span(mask, proj)
                    if sp:
                        edits.append(_wrap(sql, sp, "CAST(NULLIF(TRIM(", f"), '') AS {family})"))
        for like in tree.find_all(exp.Like, exp.ILike):
            left = like.this
            if not isinstance(left, exp.Column):
                continue
            family = R.column(left)
            sp = _span(left)
            if not sp:
                continue
            if family == "boolean":
                # SQL Server matched a BIT as the text '1'/'0'
                col = sql[sp[0]:sp[1] + 1]
                edits.append((sp[0], sp[1], f"CASE {col} WHEN TRUE THEN '1' WHEN FALSE THEN '0' END"))
            elif family in (_NUMERIC | _DATELIKE):
                edits.append(_wrap(sql, sp, "CAST(", " AS TEXT)"))
        for co in tree.find_all(exp.Coalesce):
            first = R.expr(co.this)
            for arg in co.expressions:
                sp = _span(arg) if isinstance(arg, exp.Literal) else None
                if not sp:
                    continue
                if arg.is_string and str(arg.this) == "":
                    if first in _NUMERIC:
                        edits.append((sp[0], sp[1], "0"))
                    elif first in _DATELIKE:
                        edits.append((sp[0], sp[1], "'1900-01-01'"))
                elif (not arg.is_string and first in ("text", "splittext") and isinstance(co.this, exp.Column)
                      and id(co) not in handled):
                    family = _numeric_context(co, R)
                    if family:
                        # COALESCE(text_col, 0) + 1: the text becomes the number (SQL Server's conversion)
                        to_number(co.this, family)
                    else:
                        # ISNULL(text_col, 0) is text in SQL Server: the default becomes '0'
                        edits.append((sp[0], sp[1], f"'{arg.this}'"))
        for case in tree.find_all(exp.Case):
            branches = [i.args.get("true") for i in case.args.get("ifs") or []] + [case.args.get("default")]
            branches = [b for b in branches if b is not None]
            kinds = [R.expr(b) for b in branches]
            int_lits = [b for b, k in zip(branches, kinds) if k == "intlit"]
            to_text = isinstance(case.parent, exp.Cast) and _datatype_family(case.parent.to) == "text"
            if to_text and any(k in ("text", "strlit") for k in kinds):
                # CAST(CASE ... THEN ListId ... THEN Identifier END AS TEXT): the numbers become text
                for b, k in zip(branches, kinds):
                    if isinstance(b, exp.Column) and k in (_NUMERIC | _DATELIKE):
                        sp = _span(b)
                        if sp:
                            edits.append(_wrap(sql, sp, "CAST(", " AS TEXT)"))
            if not int_lits:
                continue
            if any(k == "boolean" for k in kinds):
                # CASE ... THEN COALESCE(flag, FALSE) ELSE 1 END: 0/1 are the bit values
                for b in int_lits:
                    sp = _span(b)
                    if sp and str(b.this) in ("0", "1"):
                        edits.append((sp[0], sp[1], "TRUE" if str(b.this) == "1" else "FALSE"))
            elif any(k == "text" or (k == "strlit" and str(b.this) != "") for b, k in zip(branches, kinds)):
                # CASE ... THEN 0 ELSE CAST(x AS VARCHAR) END: the number becomes text
                for b in int_lits:
                    sp = _span(b)
                    if sp:
                        edits.append((sp[0], sp[1], f"'{b.this}'"))
            else:
                # CASE ... THEN 1 ELSE '' END: SQL Server read '' as 0
                for b, k in zip(branches, kinds):
                    if k == "strlit" and str(b.this) == "":
                        sp = _span(b)
                        if sp:
                            edits.append((sp[0], sp[1], "0"))
        for node in tree.find_all(exp.In):
            left = node.this
            query = node.args.get("query")
            select = query.this if isinstance(query, exp.Subquery) else query
            if not (isinstance(left, exp.Column) and R.expr(left) == "text"):
                continue
            if isinstance(select, exp.Select) and len(select.expressions) == 1:
                proj = select.expressions[0]
                proj = proj.this if isinstance(proj, exp.Alias) else proj
                family = R.expr(proj) if isinstance(proj, exp.Column) else None
                if family in _NUMERIC:
                    to_number(left, family)
        for fn in tree.find_all(exp.Func):
            name = _func_name(fn)
            if name not in _TIMESTAMP_ARG_FUNCS:
                continue
            first = fn.this if not isinstance(fn, exp.Anonymous) else (fn.expressions[0] if fn.expressions else None)
            if not (isinstance(first, exp.Column) and R.column(first) == "text"):
                continue
            sp = _span(first)
            if not sp:
                continue
            if name == "fngetlocaltime":
                # fnGetLocalTime(@date DATETIME, ...) converted its text argument; the timestamptz
                # overload is the one that maps Windows zone ids (A025)
                edits.append(_wrap(sql, sp, "CAST(", " AS TIMESTAMPTZ)"))
                continue
            # TO_CHAR came from CONVERT(VARCHAR, x, style), which returns text x UNCHANGED
            call = _call_span(mask, fn)
            if call:
                edits.append((call[0], call[1], sql[sp[0]:sp[1] + 1]))
        for agg in tree.find_all(exp.GroupConcat):
            # STRING_AGG(int_col, ','): SQL Server converted the value to text; PostgreSQL has no
            # string_agg(integer, text)
            value = agg.this.expressions[0] if isinstance(agg.this, exp.Distinct) and agg.this.expressions \
                else agg.this
            family = R.column(value) if isinstance(value, exp.Column) else None
            sp = _span(value) if family else None
            if sp and family == "boolean":
                col = sql[sp[0]:sp[1] + 1]
                edits.append((sp[0], sp[1], f"CASE {col} WHEN TRUE THEN '1' WHEN FALSE THEN '0' END"))
            elif sp and family in (_NUMERIC | _DATELIKE | {"uuid"}):
                edits.append(_wrap(sql, sp, "CAST(", " AS TEXT)"))
        for add in tree.find_all(exp.Add):
            _concat_edit(sql, mask, add, R, edits)
            _date_add_edit(sql, mask, add, R, edits)
        for ins in tree.find_all(exp.Insert):
            _insert_select_edits(ins, R, to_number)
        _union_edits(sql, tree, R, edits)


def _concat_edit(sql: str, mask: str, add, R: Resolver, edits: list) -> None:
    """`a.Code + b.Name` on two text columns: SQL Server's string +, PostgreSQL's ||."""
    left, right = _norm(R.expr(add.left)), _norm(R.expr(add.right))
    if left != "text" or right != "text":
        return
    if all(isinstance(n, exp.Literal) for n in (add.left, add.right)):
        return
    end, start = _last_end(add.left), _first_start(add.right)
    if end is None or start is None or start <= end:
        return
    between = mask[end + 1:start]
    if between.strip() != "+":
        return
    plus = end + 1 + between.index("+")
    edits.append((plus, plus, "||"))


def _date_add_edit(sql: str, mask: str, add, R: Resolver, edits: list) -> None:
    """`date_col + INTERVAL '1 DAY'` is a TIMESTAMP in PostgreSQL; DATEADD on a DATE returned a DATE
    (a recursive CTE refuses the mismatch: 'column has type date in non-recursive term')."""
    if not isinstance(add.left, exp.Column) or _norm(R.expr(add.left)) != "date":
        return
    interval = add.right.left if isinstance(add.right, exp.Mul) else add.right
    if not isinstance(interval, exp.Interval):
        return
    unit = str(interval.args.get("unit") or "").lower()
    text = str(interval.this.this if isinstance(interval.this, exp.Literal) else "").lower()
    if not (unit in _DATE_UNITS or (text.split() and text.split()[-1] in _DATE_UNITS)):
        return
    sp = _span(add.left)
    if not sp:
        return
    m = re.compile(r"\s*\+\s*INTERVAL\s*'[^']*'(?:\s*(?:DAYS?|WEEKS?|MONTHS?|YEARS?)\b)?", re.I).match(mask, sp[1] + 1)
    if not m:
        return
    end = m.end() - 1
    if isinstance(add.right, exp.Mul):
        tail = _last_end(add.right.right)
        if tail is None or not re.fullmatch(r"\s*\*\s*", mask[m.end():_first_start(add.right.right) or 0]):
            return
        end = tail
    edits.append(_wrap(sql, (sp[0], end), "CAST(", " AS DATE)"))


def _insert_select_edits(ins, R: Resolver, to_number) -> None:
    """INSERT INTO t (int_col, ...) SELECT text_col, ...: SQL Server converted on assignment."""
    target = ins.this
    if isinstance(target, exp.Schema) and isinstance(target.this, exp.Table):
        key = _table_key(target.this)
        columns = [c.name.lower() for c in target.expressions if isinstance(c, (exp.Identifier, exp.Column))]
    elif isinstance(target, exp.Table) and _table_key(target) in R.local:
        key = _table_key(target)
        columns = list(R.types.get(key, {}))
    else:
        return
    declared = R.types.get(key) or {}
    for select in _branch_selects(ins.expression):
        for pos, item in enumerate(select.expressions):
            if pos >= len(columns):
                break
            family = declared.get(columns[pos])
            inner = item.this if isinstance(item, exp.Alias) else item
            if family in _NUMERIC and isinstance(inner, exp.Column) and R.expr(inner) in ("text", "splittext"):
                to_number(inner, family)


def _boolean_span(sql: str, node, near) -> tuple[int, int] | None:
    """Offsets of a TRUE/FALSE keyword (sqlglot keeps no position for it): the nearest one after `near`
    (or before it), outside string literals."""
    from core import pgfixes
    anchor = _span(near)
    if anchor is None:
        return None
    mask = pgfixes.code_mask(sql)
    word = "TRUE" if node.this else "FALSE"
    pattern = re.compile(rf"\b{word}\b", re.I)
    after = pattern.search(mask, anchor[1] + 1)
    between = mask[anchor[1] + 1:after.start()] if after else ""
    if after and re.fullmatch(r"\s*(?:=|<>|!=)\s*", between):
        return after.start(), after.end() - 1
    before = [m for m in pattern.finditer(mask, 0, anchor[0])]
    if before:
        m = before[-1]
        if re.fullmatch(r"\s*(?:=|<>|!=)\s*", mask[m.end():anchor[0]]):
            return m.start(), m.end() - 1
    return None


def _union_edits(sql: str, tree, R: Resolver, edits: list) -> None:
    """UNION column = boolean in one branch, integer in another -> the boolean branch cast to INTEGER."""
    from core import pgfixes
    mask = None
    for setop in tree.find_all(exp.Union):
        if isinstance(setop.parent, exp.SetOperation):
            continue   # handled from the top of the chain
        branches = _branch_selects(setop)
        if len(branches) < 2:
            continue
        width = min(len(b.expressions) for b in branches)
        for pos in range(width):
            items = [b.expressions[pos] for b in branches]
            if any(isinstance(i, exp.Star) for i in items):
                break
            fams = [_norm(R.expr(i.this if isinstance(i, exp.Alias) else i)) for i in items]
            known = {f for f in fams if f}
            nulls = [i for i in items if isinstance(i.this if isinstance(i, exp.Alias) else i, exp.Null)]
            if nulls and len(known) == 1 and next(iter(known)) in _NULL_CAST:
                # `SELECT DISTINCT ..., NULL AS IsActive` resolves the NULL as text before the UNION
                # sees `1 AS IsActive` in the other branch (42804): give it the other branch's type
                mask = mask or pgfixes.code_mask(sql)
                for item in nulls:
                    sp = _projection_span(sql, mask, item)
                    if sp and mask[sp[0]:sp[1] + 1].strip().upper() == "NULL":
                        edits.append((sp[0], sp[1], f"CAST(NULL AS {_NULL_CAST[next(iter(known))]})"))
                continue
            if "boolean" not in fams or "integer" not in fams:
                continue
            if any(f not in ("boolean", "integer", None) for f in fams):
                continue
            mask = mask or pgfixes.code_mask(sql)
            for item, fam in zip(items, fams):
                if fam != "boolean":
                    continue
                inner = item.this if isinstance(item, exp.Alias) else item
                if isinstance(inner, exp.Boolean):
                    sp = _projection_span(sql, mask, item)
                    if sp and mask[sp[0]:sp[1] + 1].strip().upper() in ("TRUE", "FALSE"):
                        edits.append((sp[0], sp[1], "1" if inner.this else "0"))
                    continue
                sp = _projection_span(sql, mask, item)
                if sp:
                    edits.append(_wrap(sql, sp, "CAST(", " AS INTEGER)"))


# ==================================================================================================
# A061 (v2.18.0) -- Dapper parameters cast to the type of what they are compared with.
#
# SQL Server converted a parameter to the column's type whatever the C# sent: `Owner_OrgId = '@OrgCode'`
# with a C# string, `Org_Code = @AgentOrgId` (varchar) with a C# int. Npgsql binds the .NET type -- text,
# integer -- and PostgreSQL raises 42883 `integer = text` / `character varying = integer`. The converter
# cannot see the C# type, but a CAST to the column's own type is right for every type Npgsql can send:
#     number / date / timestamp -> CAST(NULLIF(TRIM(CAST(@x AS TEXT)), '') AS BIGINT | NUMERIC | DATE | TIMESTAMP)
#     text                      -> CAST(@x AS TEXT)
# The text round trip accepts an int, a numeric string, a DateTime or NULL alike; '' becomes NULL (SQL
# Server read '' as 0 / 1900-01-01 -- matching nothing is safer than matching the 0 rows). BIGINT, not
# INTEGER, so a C# long never overflows; int4 = int8 is a native, indexable comparison.
# A parameter compared with '' becomes CAST(@x AS TEXT) = '' (an int @x made '' raise 22P02); in
# COALESCE / NULLIF it takes the default's type: COALESCE(@x, '') text, COALESCE(@x, 0) a number.
# Left alone: parameters against a number literal (@Flag = 1, see L021) or a non-empty text literal
# ('@BusinessType' = 'Agent'), a boolean, another parameter, an unknown type, and the names
# QueryParameterProcessor splices in as text (@UserId, @...dbName).
# Known limit: varchar = int compared numerically in SQL Server ('007' = 7); the cast compares text.
# ==================================================================================================

_PARAM_CAST_TYPE = {"integer": "BIGINT", "numeric": "NUMERIC", "date": "DATE", "timestamp": "TIMESTAMP"}
_CMP_OP = r"(?:=|<>|!=|>=|<=|<|>)"


def _param_name(node) -> str | None:
    """'x' for a Dapper parameter @x or the exact '@x' literal ACS binds, else None."""
    from core import pgfixes
    if isinstance(node, exp.Parameter):
        name = str(getattr(node.this, "name", "") or "")
    elif isinstance(node, exp.Literal) and node.is_string and _PARAM_LITERAL.match(str(node.this)):
        name = str(node.this)[1:]
    else:
        return None
    if not name or name.lower() in pgfixes._STATIC_TEXT_PARAMS:
        return None
    return name


def _param_cast(text: str, family: str) -> str:
    if family == "text":
        return f"CAST({text} AS TEXT)"
    return f"CAST(NULLIF(TRIM(CAST({text} AS TEXT)), '') AS {_PARAM_CAST_TYPE[family]})"


def _compared_family(R: Resolver, node) -> str | None:
    """Type family a parameter compared with `node` must take, or None to leave it alone."""
    raw = R.expr(node)
    if raw == "strlit":
        return "text" if isinstance(node, exp.Literal) and str(node.this) == "" else None
    if raw in ("intlit", "numlit"):
        # `@Flag = 1` is a C# int or bool flag: the cast would only add noise, and a bool needs
        # TRUE/FALSE instead (L021 reports that case)
        return None
    family = _norm(raw)
    return family if family in _PARAM_CAST_TYPE or family == "text" else None


def _node_start(mask: str, node) -> int | None:
    start = _first_start(node)
    if start is None and isinstance(node, exp.Func):
        sp = _expr_call_span(mask, node)
        start = sp[0] if sp else None
    return start


def _node_end(mask: str, node) -> int | None:
    end = _last_end(node)
    if end is None and isinstance(node, exp.Func):
        sp = _expr_call_span(mask, node)
        end = sp[1] if sp else None
    return end


def _param_span(mask: str, param, name: str, other, param_is_right: bool) -> tuple[int, int] | None:
    """Offsets of one parameter of a comparison. A quoted '@x' has its own position; a bare @x has none
    in sqlglot, so it is found next to the other operand, across the comparison operator."""
    if isinstance(param, exp.Literal):
        return _span(param)
    word = "@" + re.escape(name)
    if param_is_right:
        end = _node_end(mask, other)
        m = re.compile(rf"\s*{_CMP_OP}\s*({word})(?![\w@])").match(mask, end + 1) if end is not None else None
    else:
        start = _node_start(mask, other)
        m = re.search(rf"(?<![\w@])({word})\s*{_CMP_OP}\s*$", mask[:start]) if start is not None else None
    return (m.start(1), m.end(1) - 1) if m else None


def _first_arg_param_span(mask: str, call, param) -> tuple[int, int] | None:
    """A bare @x that opens a call's argument list: `COALESCE(@x, ...)`, found left of the next argument."""
    nxt = call.expressions[0] if isinstance(call, exp.Coalesce) else call.expression
    start = _node_start(mask, nxt)
    if start is None:
        return None
    name = re.escape(str(getattr(param.this, "name", "") or ""))
    m = re.search(rf"\(\s*(@{name})\s*,\s*$", mask[:start])
    return (m.start(1), m.end(1) - 1) if m else None


def _between_param_spans(mask: str, between) -> list[tuple[int, int]]:
    end = _node_end(mask, between.this)
    if end is None:
        return []
    m = re.compile(r"\s+BETWEEN\s+(@\w+|'_*')\s+AND\s+(@\w+|'_*')(?![\w@])", re.I).match(mask, end + 1)
    if not m:
        return []
    spans = []
    for group, node in ((1, between.args.get("low")), (2, between.args.get("high"))):
        if _param_name(node):
            spans.append((m.start(group), m.end(group) - 1))
    return spans


def cast_params(sql: str) -> tuple[str, int]:
    """A061. Returns (sql, number of parameters cast). Never raises: a parse problem changes nothing."""
    from core import pgfixes
    try:
        trees = [t for t in sqlglot.parse(sql, read="postgres") if t is not None]
        R = Resolver(trees)
    except Exception:
        return sql, 0
    mask = pgfixes.code_mask(sql)
    edits: list[tuple[int, int, str]] = []
    try:
        for tree in trees:
            for cmp in tree.find_all(*_COMPARISONS):
                for param, other, is_right in ((cmp.right, cmp.left, True), (cmp.left, cmp.right, False)):
                    name = _param_name(param)
                    if not name or _param_name(other) or isinstance(other, exp.Parameter):
                        continue
                    family = _compared_family(R, other)
                    sp = _param_span(mask, param, name, other, is_right) if family else None
                    if sp:
                        edits.append((sp[0], sp[1], _param_cast(sql[sp[0]:sp[1] + 1], family)))
            for call in tree.find_all(exp.Coalesce, exp.Nullif):
                # COALESCE(@x, '') / NULLIF(@x, ''): the text default makes PostgreSQL read '' as @x's
                # type, so an int @x raises 22P02 -- the text is what the T-SQL compared.
                # COALESCE(@x, 0): a C# string, or a null int? (the infra binds null as TEXT), raises
                # 42804 text vs integer -- SQL Server compared the number.
                if isinstance(call.parent, exp.Func) and _func_name(call.parent) in _SPLIT_FUNCS:
                    continue   # A022's STRING_TO_TABLE(NULLIF(@csv, ''), ','): a CSV list is always text
                first = call.this
                rest = list(call.expressions) if isinstance(call, exp.Coalesce) else [call.expression]
                if not (_param_name(first) and rest and all(
                        isinstance(a, exp.Literal) and not _param_name(a) for a in rest)):
                    continue
                if all(a.is_string for a in rest):
                    family = "text"
                elif not any(a.is_string for a in rest):
                    family = "numeric" if any(not str(a.this).lstrip("-").isdigit() for a in rest) else "integer"
                else:
                    continue
                sp = _span(first) if isinstance(first, exp.Literal) else _first_arg_param_span(mask, call, first)
                if sp:
                    edits.append((sp[0], sp[1], _param_cast(sql[sp[0]:sp[1] + 1], family)))
            for between in tree.find_all(exp.Between):
                family = _compared_family(R, between.this)
                if family:
                    for sp in _between_param_spans(mask, between):
                        edits.append((sp[0], sp[1], _param_cast(sql[sp[0]:sp[1] + 1], family)))
    except Exception:
        return sql, 0
    # Idempotent by construction: a cast parameter is a Cast operand now, which _param_name rejects.
    if not edits:
        return sql, 0
    kept: list[tuple[int, int, str]] = []
    for e in sorted(set(edits)):
        if kept and e[0] <= kept[-1][1]:
            continue
        kept.append(e)
    out = sql
    for start, end, text in reversed(kept):
        out = out[:start] + text + out[end + 1:]
    return out, len(kept)
