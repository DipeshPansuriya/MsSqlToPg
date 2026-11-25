"""A058 (v2.16.0) -- T-SQL IF / ELSE flattened into guarded statements.

PostgreSQL has no IF outside a function body, and sqlglot cannot parse one: a
batch such as

    DECLARE @SearchVal NVARCHAR(25) SET @SearchVal = '@SearchString'
    IF (@SearchVal = '') BEGIN SELECT ... FROM T END
    ELSE BEGIN SELECT ... FROM T WHERE Name LIKE '%' + @SearchVal + '%' END

came out EMPTY (SG-PARSE). Exactly one branch of an IF runs, so the batch can be
written without the IF: every statement of a branch is GUARDED by the branch's
condition, and the result sets the branches return at the same position are
joined with UNION ALL -- the branch whose guard is false contributes no rows:

    SELECT * FROM (SELECT ... FROM T) AS _if1
      WHERE CASE WHEN (@SearchVal = '') THEN 1 ELSE 0 END = 1
    UNION ALL
    SELECT * FROM (SELECT ... FROM T WHERE ...) AS _if2
      WHERE CASE WHEN (@SearchVal = '') THEN 1 ELSE 0 END = 0

`CASE WHEN (c) THEN 1 ELSE 0 END = 0` is "c is not TRUE": T-SQL runs the ELSE
branch when the condition is NULL, which `NOT (c)` would not reproduce. The
DECLARE / SET outside the IF are then inlined by A031 as before.

Statements a guard can carry: a result SELECT (wrapped as above), SELECT ... INTO
#t (the guard goes on the feed), INSERT ... SELECT / single-row VALUES, UPDATE and
DELETE (the guard joins their WHERE), and CREATE / DROP of a temp table that only
that branch uses (DDL runs unguarded: the table is created and dropped, empty,
when the branch is not taken). REFUSED -- the batch is left for a hand rewrite:
SET / SELECT @var / DECLARE inside a branch (a conditional assignment), EXEC,
WHILE, RETURN, RAISERROR, TRY / TRANSACTION blocks, MERGE, CTE statements inside
a branch, branches returning a different number of result sets, and paired result
sets whose column count or names differ (one UNION ALL must describe both).

Text level: runs before sqlglot sees the batch (in core/pgfixes.pre_parse), on the
code mask, so nothing inside a string or a comment is ever read as a keyword.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

_TOKEN_RE = re.compile(r"\[[^\]\n]*\]|'[^'\n]*'|\"[^\"\n]*\"|[A-Za-z_@#][\w@#$]*|\d+(?:\.\d+)?|[();,]|[^\s\w]")

_STATEMENT_START = frozenset("""SELECT INSERT UPDATE DELETE MERGE SET DECLARE IF BEGIN WITH CREATE DROP ALTER
TRUNCATE EXEC EXECUTE PRINT RETURN RAISERROR THROW WHILE GOTO OPEN CLOSE FETCH DEALLOCATE USE GRANT""".split())
_BLOCK_REFUSED = frozenset("TRY CATCH TRAN TRANSACTION DISTRIBUTED".split())
_GUARDABLE = frozenset("SELECT INSERT UPDATE DELETE CREATE DROP".split())
# a keyword right after these continues the expression (`SELECT Id, Open FROM t`, `AS Close`)
_NO_START_AFTER = frozenset(", . = + - * / < > % AS SELECT BY ON AND OR NOT IN FROM JOIN WHERE THEN WHEN ELSE "
                            "DISTINCT TOP INTO IS LIKE BETWEEN".split())
_WRITES = re.compile(r"(?i)\b(INSERT|UPDATE|DELETE|MERGE|TRUNCATE|CREATE|DROP|ALTER|INTO)\b")


class Refused(Exception):
    """The batch cannot be flattened; the message says why."""


@dataclass
class _Tok:
    text: str        # upper-cased token
    start: int
    end: int         # exclusive


@dataclass
class _Stmt:
    kind: str
    start: int
    end: int         # exclusive


@dataclass
class _If:
    cond: tuple[int, int]
    then: list = field(default_factory=list)
    other: list = field(default_factory=list)


def _tokens(masked: str) -> list[_Tok]:
    return [_Tok(m.group(0).upper(), m.start(), m.end()) for m in _TOKEN_RE.finditer(masked)]


class _Parser:
    def __init__(self, masked: str):
        self.masked = masked
        self.toks = _tokens(masked)
        self.n = len(self.toks)

    def word(self, i: int) -> str:
        return self.toks[i].text if 0 <= i < self.n else ""

    # items := { ';' | IF | BEGIN ... END | statement }
    def items(self, i: int, in_block: bool) -> tuple[list, int]:
        out: list = []
        while i < self.n:
            w = self.word(i)
            if w == ";":
                i += 1
            elif w == "END":
                if in_block:
                    return out, i
                raise Refused("END without BEGIN")
            elif w == "ELSE":
                raise Refused("ELSE without IF")
            else:
                item, i = self.one(i)
                out.append(item)
        if in_block:
            raise Refused("BEGIN without END")
        return out, i

    def one(self, i: int):
        w = self.word(i)
        if w == "IF":
            return self.if_(i)
        if w == "BEGIN":
            if self.word(i + 1) in _BLOCK_REFUSED:
                raise Refused(f"BEGIN {self.word(i + 1)}")
            body, j = self.items(i + 1, True)
            return body, j + 1          # past END
        return self.statement(i)

    def if_(self, i: int):
        j, depth = i + 1, 0
        while j < self.n:
            w = self.word(j)
            if w == "(":
                depth += 1
            elif w == ")":
                depth -= 1
            elif depth == 0 and w in _STATEMENT_START and j > i + 1:
                break
            j += 1
        if j >= self.n or j == i + 1:
            raise Refused("IF without a statement")
        cond = (self.toks[i + 1].start, self.toks[j - 1].end)
        then, k = self.one(j)
        while self.word(k) == ";":
            k += 1
        other: list = []
        if self.word(k) == "ELSE":
            other, k = self.one(k + 1)
        return _If(cond, _as_list(then), _as_list(other)), k

    def statement(self, i: int):
        kind = self.word(i)
        j, depth, case = i + 1, 0, 0
        state = {"feed": False, "set": False, "main": False}
        prev = kind
        while j < self.n:
            w = self.word(j)
            if w == "(":
                depth += 1
            elif w == ")":
                depth -= 1
                if depth < 0:
                    raise Refused("unbalanced parentheses")
            elif depth == 0:
                if w == ";":
                    return _Stmt(kind, self.toks[i].start, self.toks[j - 1].end), j + 1
                if w == "CASE":
                    case += 1
                elif w == "END":
                    if case:
                        case -= 1
                    else:
                        return _Stmt(kind, self.toks[i].start, self.toks[j - 1].end), j
                elif not case:
                    if w == "ELSE":
                        return _Stmt(kind, self.toks[i].start, self.toks[j - 1].end), j
                    if (w in _STATEMENT_START and prev not in _NO_START_AFTER
                            and not self._continues(kind, w, prev, j, state)):
                        return _Stmt(kind, self.toks[i].start, self.toks[j - 1].end), j
            prev = w
            j += 1
        return _Stmt(kind, self.toks[i].start, self.toks[-1].end), self.n

    def _continues(self, kind: str, w: str, prev: str, j: int, state: dict) -> bool:
        if w in ("SELECT", "INSERT", "UPDATE", "DELETE", "MERGE") and kind == "WITH" and not state["main"]:
            state["main"] = w            # the statement the CTEs belong to
            return True
        if kind == "WITH" and state["main"]:
            kind = state["main"]         # WITH c AS (...) INSERT ... SELECT: the INSERT's rules
        if kind == "MERGE":
            return True
        if w == "SELECT":
            if prev in ("UNION", "ALL", "EXCEPT", "INTERSECT"):
                return True
            if kind == "INSERT" and not state["feed"]:
                state["feed"] = True
                return True
        if w == "SET" and kind == "UPDATE" and not state["set"]:
            state["set"] = True
            return True
        if w == "IF" and prev in ("TABLE", "INDEX", "VIEW", "SCHEMA"):
            return True          # DROP TABLE IF EXISTS
        if w == "FETCH" and prev in ("ROWS", "ROW"):
            return True          # OFFSET n ROWS FETCH NEXT
        if w == "WITH":
            # a CTE starts a statement; WITH TIES / ROLLUP / CHECK OPTION do not
            return not (self.word(j + 2) == "AS" or self.word(j + 2) == "(")
        if w in ("EXEC", "EXECUTE") and kind == "INSERT" and not state["feed"]:
            raise Refused("INSERT ... EXEC")
        return False


def _as_list(part) -> list:
    return part if isinstance(part, list) else [part]


def _has_if(items) -> bool:
    for item in items:
        if isinstance(item, _If):
            return True
        if isinstance(item, list) and _has_if(item):
            return True
    return False


# ---- flattening -----------------------------------------------------------------------------------

_CLAUSE_AFTER_WHERE = re.compile(r"\(|\)|\b(?:GROUP\s+BY|HAVING|ORDER\s+BY|OPTION|OFFSET|WINDOW)\b", re.I)
_WHERE_OR_CLAUSE = re.compile(r"\(|\)|\b(?:WHERE|GROUP\s+BY|HAVING|ORDER\s+BY|OPTION|OFFSET|WINDOW)\b", re.I)
_WRITE_TARGET = re.compile(
    r"(?i)\b(?:INSERT\s+INTO|INSERT|UPDATE|DELETE\s+FROM|DELETE|INTO|TRUNCATE\s+TABLE|"
    r"DROP\s+TABLE(?:\s+IF\s+EXISTS)?|CREATE\s+TABLE|MERGE\s+INTO|MERGE)\s+([#@\w.\[\]]+)")
_READ_SOURCE = re.compile(r"(?i)\b(?:FROM|JOIN)\s+([#@\w.\[\]]+)(?:\s+(?:AS\s+)?([A-Za-z_]\w*))?")


def _name(raw: str) -> str:
    return raw.replace("[", "").replace("]", "").split(".")[-1].lower()


def _clause_at_depth0(masked: str, pattern, start: int = 0) -> list[tuple[int, str]]:
    found, depth = [], 0
    for m in pattern.finditer(masked, start):
        t = m.group(0)
        if t == "(":
            depth += 1
        elif t == ")":
            depth -= 1
        elif depth == 0:
            found.append((m.start(), re.sub(r"\s+", " ", t.upper())))
    return found


def _inject_where(text: str, masked: str, guard: str) -> str:
    """`guard` AND-ed into the statement's own top-level WHERE (or a WHERE added before GROUP BY ...)."""
    clauses = _clause_at_depth0(masked, _WHERE_OR_CLAUSE)
    where = next((p for p, w in clauses if w == "WHERE"), None)
    if where is None:
        nxt = next((p for p, w in clauses if w != "WHERE"), len(text))
        return f"{text[:nxt].rstrip()} WHERE {guard} {text[nxt:]}".rstrip()
    after = [p for p, w in _clause_at_depth0(masked, _CLAUSE_AFTER_WHERE, where) if p > where]
    end = after[0] if after else len(text)
    body = text[where + len("WHERE"):end].strip()
    return f"{text[:where]}WHERE ({guard}) AND ({body}) {text[end:]}".rstrip()


def _select_tree(text: str):
    try:
        import sqlglot
        return sqlglot.parse_one(text, read="tsql")
    except Exception:
        return None


def _needs_wrap(tree) -> bool:
    """A guard in WHERE is exact for a plain SELECT; a set operation, or an aggregate without GROUP BY
    (one row even when nothing matches), takes the guard outside."""
    from sqlglot import exp
    if not isinstance(tree, exp.Select):
        return True
    if tree.args.get("group") is None:
        for item in tree.expressions:
            for agg in item.find_all(exp.AggFunc):
                if not agg.find_ancestor(exp.Window):
                    return True
    return False


class _Flattener:
    def __init__(self, sql: str, masked: str):
        self.sql, self.masked = sql, masked
        self.counter = 0
        self.ifs = 0
        self.temps: dict[str, tuple] = {}

    def text(self, stmt: _Stmt) -> str:
        return self.sql[stmt.start:stmt.end].strip()

    def mask(self, stmt: _Stmt) -> str:
        return self.masked[stmt.start:stmt.end].strip()

    def alias(self) -> str:
        self.counter += 1
        return f"_if{self.counter}"

    def guard(self, guards) -> str:
        return " AND ".join(f"CASE WHEN ({c}) THEN 1 ELSE 0 END = {1 if truth else 0}" for c, truth in guards)

    # temp tables' columns, for comparing `SELECT * FROM #t` result sets
    def collect_temps(self, items) -> None:
        for item in items:
            if isinstance(item, list):
                self.collect_temps(item)
            elif isinstance(item, _If):
                self.collect_temps(item.then)
                self.collect_temps(item.other)
            elif item.kind in ("CREATE", "SELECT"):
                self._define_temp(self.text(item))

    def _define_temp(self, text: str) -> None:
        from sqlglot import exp
        tree = _select_tree(text)
        if isinstance(tree, exp.Create) and isinstance(tree.this, exp.Schema) and isinstance(tree.this.this, exp.Table):
            cols = tuple(c.name.lower() for c in tree.this.expressions if isinstance(c, exp.ColumnDef))
            self.temps["#" + tree.this.this.name.lower()] = cols
        elif isinstance(tree, exp.Select) and isinstance(tree.args.get("into"), exp.Into):
            names = self.signature_of(tree)
            if names is not None:
                self.temps["#" + tree.args["into"].this.name.lower()] = names

    def signature_of(self, tree):
        """Lower-cased output column names (None = cannot tell)."""
        from sqlglot import exp
        while isinstance(tree, (exp.Subquery, exp.SetOperation)):
            tree = tree.this if isinstance(tree, exp.Subquery) else tree.left
        if not isinstance(tree, exp.Select):
            return None
        sources = {}
        frm = tree.args.get("from_") or tree.args.get("from")
        tables = ([frm.this] if frm is not None else []) + [j.this for j in tree.args.get("joins") or []]
        for t in tables:
            if isinstance(t, exp.Table):
                key = ("#" if t.this.args.get("temporary") else "") + t.name.lower()
                sources[(t.alias or t.name).lower()] = key
        names: list[str] = []
        for item in tree.expressions:
            star = isinstance(item, exp.Star) or (isinstance(item, exp.Column) and isinstance(item.this, exp.Star))
            if not star:
                names.append((item.alias_or_name or "").lower())
                continue
            wanted = [sources.get(item.table.lower())] if isinstance(item, exp.Column) and item.table else \
                list(sources.values())
            if not wanted or any(w is None for w in wanted):
                return None
            for key in wanted:
                if key in self.temps:
                    names.extend(self.temps[key])
                elif len(wanted) == 1:
                    names.append("*" + key)      # the same permanent table compares equal
                else:
                    return None
        return tuple(names)

    def events(self, items, guards) -> list[tuple[str, str, object]]:
        """[(kind, text, info)] kind = 'stmt' | 'result'; info = the result's column signature."""
        out = []
        for item in items:
            if isinstance(item, list):
                out.extend(self.events(item, guards))
            elif isinstance(item, _If):
                self.ifs += 1
                out.extend(self.merge(item, guards))
            else:
                out.append(self.guarded(item, guards))
        return out

    def _check_condition(self, item: _If) -> None:
        cond = self.masked[item.cond[0]:item.cond[1]]
        if not re.search(r"(?i)\bSELECT\b", cond):
            return
        # every guarded statement re-evaluates the condition: no table it reads may change after the IF
        read = {_name(m.group(1)) for m in _READ_SOURCE.finditer(cond)}
        rest = self.masked[item.cond[0]:]
        aliases = {}
        for m in _READ_SOURCE.finditer(self.masked):
            if m.group(2):
                aliases[m.group(2).lower()] = _name(m.group(1))
        written = set()
        for m in _WRITE_TARGET.finditer(rest):
            target = _name(m.group(1))
            written.add(aliases.get(target, target))
        if read & written:
            raise Refused("an IF condition reads tables the batch changes")

    def merge(self, item: _If, guards) -> list:
        self._check_condition(item)
        cond = self.sql[item.cond[0]:item.cond[1]].strip()
        then = self.events(item.then, guards + [(cond, True)])
        other = self.events(item.other, guards + [(cond, False)])
        t_res = [e for e in then if e[0] == "result"]
        o_res = [e for e in other if e[0] == "result"]
        if len(t_res) != len(o_res):
            raise Refused("the branches return a different number of result sets")
        for a, b in zip(t_res, o_res):
            if a[2] is None or b[2] is None or a[2] != b[2]:
                raise Refused("paired result sets differ in their columns")

        def segments(events):
            segs, cur = [], []
            for e in events:
                if e[0] == "result":
                    segs.append(cur)
                    cur = []
                else:
                    cur.append(e)
            segs.append(cur)
            return segs

        t_seg, o_seg = segments(then), segments(other)
        out = []
        for k in range(len(t_res) + 1):
            out.extend(t_seg[k] + o_seg[k])
            if k < len(t_res):
                out.append(("result", t_res[k][1] + "\nUNION ALL\n" + o_res[k][1], t_res[k][2]))
        return out

    def guard_select(self, text: str, masked: str, g: str, *, branch: bool) -> str:
        tree = _select_tree(text)
        if tree is None or _needs_wrap(tree):
            return f"SELECT * FROM ({text}) AS {self.alias()} WHERE {g}"
        out = _inject_where(text, masked, g)
        if branch and (_clause_at_depth0(masked, re.compile(r"\(|\)|\bORDER\s+BY\b", re.I))
                       or re.match(r"(?i)SELECT\s+(DISTINCT\s+)?TOP\b", masked)):
            out = f"({out})"       # a UNION ALL operand with its own ORDER BY / TOP
        return out

    def guarded(self, stmt: _Stmt, guards):
        text, masked = self.text(stmt), self.mask(stmt)
        kind = stmt.kind
        if kind == "SELECT" and re.match(r"(?i)SELECT\s+@\w+\s*=", masked):
            if guards:
                raise Refused("SELECT @var = ... inside an IF branch")
            return ("stmt", text, None)
        if not guards:
            if kind == "SELECT" and not _top_level_into(masked):
                return ("result", text, None)
            return ("stmt", text, None)
        if kind not in _GUARDABLE:
            raise Refused(f"{kind} inside an IF branch")
        if re.search(r"(?i)\bFOR\s+(XML|JSON)\b", masked):
            raise Refused("FOR XML / FOR JSON inside an IF branch")
        g = self.guard(guards)
        if kind == "SELECT":
            into = _top_level_into(masked)
            tree = _select_tree(text)
            if into:
                if tree is not None and not _needs_wrap(tree):
                    return ("stmt", _inject_where(text, masked, g), None)
                s, e = into
                feed = text[:s] + text[e:]
                return ("stmt", f"SELECT * {text[s:e].strip()} FROM ({feed}) AS {self.alias()} WHERE {g}", None)
            return ("result", self.guard_select(text, masked, g, branch=True),
                    self.signature_of(tree) if tree is not None else None)
        if kind == "INSERT":
            feed = _top_level_word(masked, "SELECT")
            if feed is not None:
                return ("stmt", text[:feed] + self.guard_select(text[feed:], masked[feed:], g, branch=False), None)
            values = _top_level_word(masked, "VALUES")
            if values is not None:
                rows = text[values + len("VALUES"):].strip()
                if not (rows.startswith("(") and rows.endswith(")")) or _top_level_word(
                        masked[values + 6:].strip(), ",") is not None:
                    raise Refused("multi-row INSERT ... VALUES inside an IF branch")
                return ("stmt", f"{text[:values]}SELECT {rows[1:-1]} WHERE {g}", None)
            raise Refused("INSERT form inside an IF branch")
        if kind in ("UPDATE", "DELETE"):
            if re.search(r"(?i)\bOUTPUT\b", masked):
                raise Refused("OUTPUT inside an IF branch")
            where = _top_level_word(masked, "WHERE")
            if where is None:
                return ("stmt", f"{text} WHERE {g}", None)
            body = text[where + len("WHERE"):]
            return ("stmt", f"{text[:where]}WHERE ({g}) AND ({body.strip()})", None)
        # CREATE / DROP: only temp tables, run unguarded
        if not re.match(r"(?i)(CREATE|DROP)\s+TABLE\s+(IF\s+EXISTS\s+)?#\w+", masked):
            raise Refused(f"{kind} of a permanent object inside an IF branch")
        return ("stmt", text, None)


def _top_level_word(masked: str, word: str) -> int | None:
    """Offset of `word` at parenthesis depth 0 (skipping the statement's first word)."""
    depth = 0
    pattern = re.compile(r"\(|\)|," if word == "," else r"\(|\)|\b" + word + r"\b", re.I)
    for m in pattern.finditer(masked):
        t = m.group(0)
        if t == "(":
            depth += 1
        elif t == ")":
            depth -= 1
        elif depth == 0 and m.start() > 0:
            return m.start()
    return None


def _top_level_into(masked: str) -> tuple[int, int] | None:
    """(start, end) of `INTO #name` at depth 0 in a SELECT."""
    pos = _top_level_word(masked, "INTO")
    if pos is None:
        return None
    m = re.compile(r"INTO\s+[#\w.]+", re.I).match(masked, pos)
    return (m.start(), m.end()) if m else None


def terminate(sql: str, masked: str) -> tuple[str, bool]:
    """`;` after every statement of a control-flow-free batch that has none (T-SQL does not need
    them; A031 finds a SET's value by its terminator). Text is otherwise untouched. The flag says
    whether every statement end is now a `;` (False: the batch could not be segmented)."""
    try:
        items, _ = _Parser(masked).items(0, False)
    except Exception:
        return sql, False
    if any(not isinstance(i, _Stmt) for i in items):
        return sql, False
    cuts = [st.end for st in items[:-1] if not masked[st.end:].lstrip().startswith(";")]
    for pos in reversed(cuts):
        sql = sql[:pos] + ";" + sql[pos:]
    return sql, True


def flatten(sql: str, masked: str) -> tuple[str, int, str | None]:
    """Returns (sql, number of IFs flattened, refusal reason). Unchanged SQL when refused or no IF."""
    try:
        parser = _Parser(masked)
        items, _ = parser.items(0, False)
    except Refused as why:
        return sql, 0, str(why)
    except Exception as exc:          # a malformed batch is not this rule's business
        return sql, 0, f"{type(exc).__name__}"
    if not _has_if(items):
        return sql, 0, None
    flat = _Flattener(sql, masked)
    try:
        flat.collect_temps(items)
        events = flat.events(items, [])
    except Refused as why:
        return sql, 0, str(why)
    return ";\n".join(text for _, text, _ in events) + ";", flat.ifs, None
