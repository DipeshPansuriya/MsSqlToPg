"""Automatic correction of unambiguous T-SQL -> PostgreSQL defects.

Every fix here rewrites the parsed AST before PostgreSQL is generated, and every
fix is DISCLOSED as a warning. Nothing is corrected silently.

The bar for inclusion is strict: a fix belongs here only when there is exactly
one defensible answer. Anything requiring a judgement call about the target
schema (cross-database references, user-defined functions, genuinely ambiguous
dates) is left alone and reported by the linter instead.

AST rewrites, not text substitution -- so a column merely NAMED `timestamp`, or
the literal string 'VARCHAR(MAX)' inside a comment, is never touched.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from sqlglot import expressions as exp


@dataclass(frozen=True)
class Applied:
    code: str
    count: int
    note: str


# --------------------------------------------------------------------------
# A001 -- T-SQL TIMESTAMP is ROWVERSION, not a datetime
# --------------------------------------------------------------------------

def fix_rowversion(expressions: list, original: str) -> Applied | None:
    """In T-SQL, `TIMESTAMP` is a deprecated synonym for `ROWVERSION` -- an
    8-byte BINARY row-version stamp -- so it converts to BYTEA and then fails
    with "cannot cast type date to bytea".

    Almost nobody writing `CAST(d AS timestamp)` means a rowversion.

    SAFETY RULE: if the source says `rowversion` ANYWHERE, the author knows the
    distinction and may genuinely be using it -- change nothing.
    """
    if re.search(r"\browversion\b", original, re.IGNORECASE):
        return None

    count = 0
    for tree in expressions:
        for datatype in tree.find_all(exp.DataType):
            if datatype.this == exp.DataType.Type.ROWVERSION:
                datatype.set("this", exp.DataType.Type.TIMESTAMP)
                count += 1
    if not count:
        return None
    return Applied(
        "A001", count,
        f"{count} use(s) of T-SQL TIMESTAMP treated as DATETIME2 "
        f"(-> PostgreSQL TIMESTAMP). Strictly it means ROWVERSION (binary), "
        f"which cannot hold a date. Your source never says ROWVERSION, so a "
        f"date/time was assumed. If any really are rowversion columns, write "
        f"ROWVERSION in the source and reconvert.")


# --------------------------------------------------------------------------
# A002 -- Windows timezone names
# --------------------------------------------------------------------------
# PostgreSQL only knows IANA names. A Windows name raises at runtime:
#   ERROR: time zone "India Standard Time" not recognized
_WINDOWS_TO_IANA = {
    "india standard time": "Asia/Kolkata",
    "pakistan standard time": "Asia/Karachi",
    "bangladesh standard time": "Asia/Dhaka",
    "sri lanka standard time": "Asia/Colombo",
    "nepal standard time": "Asia/Kathmandu",
    "arabian standard time": "Asia/Dubai",
    "arab standard time": "Asia/Riyadh",
    "israel standard time": "Asia/Jerusalem",
    "singapore standard time": "Asia/Singapore",
    "se asia standard time": "Asia/Bangkok",
    "china standard time": "Asia/Shanghai",
    "taipei standard time": "Asia/Taipei",
    "tokyo standard time": "Asia/Tokyo",
    "korea standard time": "Asia/Seoul",
    "aus eastern standard time": "Australia/Sydney",
    "gmt standard time": "Europe/London",
    "w. europe standard time": "Europe/Berlin",
    "central europe standard time": "Europe/Budapest",
    "romance standard time": "Europe/Paris",
    "russian standard time": "Europe/Moscow",
    "eastern standard time": "America/New_York",
    "central standard time": "America/Chicago",
    "mountain standard time": "America/Denver",
    "pacific standard time": "America/Los_Angeles",
    "e. south america standard time": "America/Sao_Paulo",
    "south africa standard time": "Africa/Johannesburg",
    "utc": "UTC",
}


def fix_windows_timezone(expressions: list, original: str) -> Applied | None:
    """Rewrite known Windows timezone names to their IANA equivalents.

    Only names in the table are converted. An unrecognised Windows name is left
    alone so L012 still reports it -- guessing a timezone would silently shift
    every timestamp in the result.
    """
    changed: list[str] = []
    for tree in expressions:
        for literal in tree.find_all(exp.Literal):
            if not literal.is_string:
                continue
            iana = _WINDOWS_TO_IANA.get(str(literal.this).strip().lower())
            if iana and iana != literal.this:
                changed.append(f"'{literal.this}' -> '{iana}'")
                literal.set("this", iana)
    if not changed:
        return None
    unique = sorted(set(changed))
    return Applied(
        "A002", len(changed),
        f"{len(changed)} Windows timezone name(s) rewritten to IANA: "
        f"{', '.join(unique)}. PostgreSQL does not recognise Windows names.")


# --------------------------------------------------------------------------
# A003 -- DD-MM-YYYY date literals
# --------------------------------------------------------------------------
# PostgreSQL's default DateStyle is MDY, so '31-07-2026' errors outright.
# '05-07-2026' would SILENTLY become 7 May instead of 5 July -- so only
# provably-DD-MM values (day > 12) are corrected. Ambiguous ones are left for
# L014 to report, because guessing would corrupt data without any error.
_DMY = re.compile(r"^(\d{2})-(\d{2})-(\d{4})(.*)$")


def fix_date_literals(expressions: list, original: str) -> Applied | None:
    changed: list[str] = []
    for tree in expressions:
        for literal in tree.find_all(exp.Literal):
            if not literal.is_string:
                continue
            match = _DMY.match(str(literal.this))
            if not match:
                continue
            day, month, year, tail = match.groups()
            if int(day) <= 12:
                continue        # ambiguous -- refuse to guess, L014 reports it
            iso = f"{year}-{month}-{day}{tail}"
            changed.append(f"'{literal.this}' -> '{iso}'")
            literal.set("this", iso)
    if not changed:
        return None
    return Applied(
        "A003", len(changed),
        f"{len(changed)} DD-MM-YYYY date literal(s) rewritten to ISO: "
        f"{', '.join(sorted(set(changed)))}. Only values with day > 12 are "
        f"corrected; genuinely ambiguous dates are reported by L014 instead.")


# --------------------------------------------------------------------------
# A004 -- VARCHAR(MAX) / NVARCHAR(MAX)
# --------------------------------------------------------------------------

def fix_varchar_max(expressions: list, original: str) -> Applied | None:
    count = 0
    for tree in expressions:
        for datatype in tree.find_all(exp.DataType):
            if datatype.this not in (exp.DataType.Type.VARCHAR,
                                     exp.DataType.Type.NVARCHAR):
                continue
            params = [p.sql().strip().upper() for p in datatype.expressions]
            if params == ["MAX"]:
                datatype.set("this", exp.DataType.Type.TEXT)
                datatype.set("expressions", None)
                count += 1
    if not count:
        return None
    return Applied(
        "A004", count,
        f"{count} VARCHAR(MAX)/NVARCHAR(MAX) changed to TEXT. PostgreSQL has "
        f"no MAX length.")


# --------------------------------------------------------------------------
# A005 -- CLUSTERED INDEX
# --------------------------------------------------------------------------

def fix_clustered_index(expressions: list, original: str) -> Applied | None:
    """PostgreSQL has no CLUSTERED index; the keyword is a hard syntax error.

    Physical clustering is a separate one-off command (`CLUSTER tbl USING ix`),
    not an index property, so dropping the keyword is the only sane mapping.
    """
    count = 0
    for tree in expressions:
        if not isinstance(tree, exp.Create):
            continue
        kind = (tree.args.get("kind") or "")
        if isinstance(kind, str) and "CLUSTERED" in kind.upper():
            tree.set("kind", "INDEX")
            count += 1
    if not count:
        return None
    return Applied(
        "A005", count,
        f"{count} CLUSTERED INDEX changed to plain INDEX. PostgreSQL has no "
        f"CLUSTERED indexes -- use 'CLUSTER <table> USING <index>' afterwards "
        f"if you need physical ordering.")


# --------------------------------------------------------------------------
# A006 -- T-SQL builtins sqlglot leaves alone
# --------------------------------------------------------------------------
# sqlglot already maps LEN, NEWID, SYSDATETIME, GETDATE, ISNULL, CHARINDEX and
# friends. What it leaves as an Anonymous call is what breaks at runtime with
# "function ... does not exist". Only exact, unambiguous equivalents belong
# here -- SCOPE_IDENTITY, for instance, has no safe one-liner and is excluded.
_BUILTIN_REPLACEMENTS = {
    "GETUTCDATE": "(NOW() AT TIME ZONE 'UTC')",
    "SYSUTCDATETIME": "(NOW() AT TIME ZONE 'UTC')",
}


def fix_unsupported_builtins(expressions: list, original: str) -> Applied | None:
    import sqlglot

    changed: list[str] = []
    for tree in expressions:
        for call in list(tree.find_all(exp.Anonymous)):
            name = str(call.name or "").upper()
            replacement = _BUILTIN_REPLACEMENTS.get(name)
            if not replacement or call.expressions:
                continue    # only zero-argument forms are safe to swap
            call.replace(sqlglot.parse_one(replacement, read="postgres"))
            changed.append(f"{name}() -> {replacement}")
    if not changed:
        return None
    return Applied(
        "A006", len(changed),
        f"{len(changed)} T-SQL function call(s) replaced: "
        f"{', '.join(sorted(set(changed)))}. These do not exist in PostgreSQL "
        f"and fail with 'function ... does not exist'.")


# --------------------------------------------------------------------------
# A007 -- BIT columns compared to integers
# --------------------------------------------------------------------------
# SQL Server silently coerces BIT <-> integer, so `ISNULL(IsDeleted, 0) = 0` is
# fine there. Once BIT is migrated to PostgreSQL BOOLEAN it fails with
#     ERROR: COALESCE types boolean and integer cannot be matched
#
# The converter cannot see the target schema, so it needs evidence that a column
# is boolean. Two signals, both drawn from the query itself:
#
#   1. PROOF     -- the column appears with CAST(0/1 AS BIT) somewhere in the
#                   source. That is an explicit boolean in the author's own SQL.
#   2. CONVENTION-- the name matches ^Is[A-Z]... (IsDeleted, IsActive, IsLTL).
#                   Near-universal for flag columns.
#
# Anything else is left alone. The columns actually treated as boolean are
# listed in the warning so the assumption can be checked against the schema.
# ^is followed by anything (Is2FA has a digit after the prefix, IsDeleted a
# capital) -- the old ^is[A-Z_] pattern missed Is2FA.
_BOOLEAN_NAME = re.compile(r"^is[A-Za-z0-9_]", re.IGNORECASE)

# Flag columns that are boolean in the target schema but do not follow the Is*
# naming convention come from the project profile ("boolean_columns").


def _boolean_columns(original: str) -> set[str]:
    """Column names with evidence of being boolean, lower-cased."""
    proven = set()
    # Signal 1: used alongside an explicit CAST(n AS BIT) in the same predicate.
    for match in re.finditer(
            r"(?:ISNULL|COALESCE)\s*\(\s*(?:\w+\.)?(\w+)\s*,\s*CAST\s*\(\s*[01]\s+AS\s+BIT\s*\)",
            original, re.IGNORECASE):
        proven.add(match.group(1).lower())
    for match in re.finditer(
            r"(?:\w+\.)?(\w+)\s*=\s*CAST\s*\(\s*[01]\s+AS\s+BIT\s*\)",
            original, re.IGNORECASE):
        proven.add(match.group(1).lower())
    return proven


def _is_boolean_column(name: str, proven: set[str]) -> bool:
    # A024 (v2.13.0): the target schema's own list, when boolean_columns.txt
    # is present -- names like ACTIVESTATUS / HasAccepted follow no
    # naming convention and were the ones that survived conversion.
    from core import profile, schema_hints
    lowered = name.lower()
    return (lowered in proven or lowered in profile.current().boolean_columns
            or lowered in schema_hints.boolean_columns()
            or bool(_BOOLEAN_NAME.match(name)))


def _type_resolver(expressions):
    """v2.16.0: column types from column_types.txt and the batch's own temp tables / CTEs
    (core/typefix.Resolver), so a ^Is... column DECLARED as something else -- `IsOfferExpire
    VARCHAR(10)` in a #temp, `CASE ... THEN 1 ELSE 0 END AS IsRemoteConfig INTO #RemoteFlag`,
    Ref_Unit.IsoId INT -- is not rewritten as a boolean. None when it cannot be built."""
    from core import typefix
    try:
        return typefix.Resolver([t for t in expressions if t is not None])
    except Exception:
        return None


def _declared_not_boolean(resolver, column) -> bool:
    return resolver is not None and resolver.not_boolean(column)


def _bit_case_branches(node) -> list | None:
    """The 0/1 branches of `CASE ... THEN 1 ELSE 0 END` (parenthesised or not), else None."""
    while isinstance(node, exp.Paren):
        node = node.this
    if not isinstance(node, exp.Case):
        return None
    branches = [i.args.get("true") for i in node.args.get("ifs") or []] + [node.args.get("default")]
    values = [b for b in branches if b is not None and not isinstance(b, exp.Null)]
    if not values or any(_bool_literal(b) is None or b.is_string for b in values):
        return None
    return values


def _bool_literal(node) -> bool | None:
    """The boolean a 0/1 or 'true'/'false' literal stands for, else None."""
    if not isinstance(node, exp.Literal):
        return None
    if node.is_string:
        lowered = str(node.this).strip().lower()
        if lowered == "true":
            return True
        if lowered == "false":
            return False
        return None
    try:
        value = int(str(node.this))
    except ValueError:
        return None
    return bool(value) if value in (0, 1) else None


def fix_bit_integer_comparison(expressions: list, original: str) -> Applied | None:
    proven = _boolean_columns(original)
    touched: set[str] = set()
    resolver = _type_resolver(expressions)

    def boolean_column_in(node) -> str | None:
        """The boolean column this expression is really about, if any."""
        target = node
        if isinstance(node, exp.Coalesce):
            target = node.this
        while isinstance(target, exp.Paren):
            target = target.this
        if isinstance(target, exp.Column) and target.name:
            if (_is_boolean_column(target.name, proven)
                    and not _declared_not_boolean(resolver, target)):
                return target.name
        # v2.16.0: CAST(x AS BIT) is a boolean by definition (an inlined `DECLARE @f BIT`)
        to = target.args.get("to") if isinstance(target, exp.Cast) else None
        if isinstance(to, exp.DataType) and to.this in (exp.DataType.Type.BIT, exp.DataType.Type.BOOLEAN):
            return "CAST(... AS BIT)"
        return None

    for tree in expressions:
        for comparison in list(tree.find_all(exp.EQ, exp.NEQ)):
            # `UPDATE t SET x = y` parses as an EQ node too. An ASSIGNMENT of
            # 'true'/'false' is usually a VARCHAR payload (T-SQL flag columns
            # are BIT set with 0/1, not strings) -- rewriting it to a boolean
            # breaks the INSERT/UPDATE at runtime ('column is of type character
            # varying but expression is of type boolean'). In a SET clause,
            # only 0/1 integer literals count as boolean evidence.
            in_set_clause = isinstance(comparison.parent, exp.Update)
            left, right = comparison.this, comparison.expression
            for side, other in ((left, right), (right, left)):
                name = boolean_column_in(side)
                if name is None:
                    continue
                changed = False
                # the literal on the other side of the comparison
                # (0/1, 'true'/'false' and, v2.18.1, '0'/'1' string compares:
                # T-SQL `ISNULL(sdty.InHouse,'0') = '0'` left
                # `COALESCE(sdty.InHouse, FALSE) = '0'` once A020 fixed the default)
                value = _bool_default(other)
                if value is not None and not (
                        in_set_clause
                        and isinstance(other, exp.Literal) and other.is_string):
                    other.replace(exp.true() if value else exp.false())
                    changed = True
                # `flag = CASE WHEN @Status = 'Active' THEN 1 ELSE 0 END` (v2.16.0):
                # every branch is a bit value, so each becomes TRUE/FALSE
                for branch in _bit_case_branches(other) or []:
                    branch.replace(exp.true() if _bool_literal(branch) else exp.false())
                    changed = True
                # and the COALESCE default, if there is one
                if isinstance(side, exp.Coalesce):
                    for default in side.expressions:
                        value = _bool_literal(default)
                        if value is not None:
                            default.replace(exp.true() if value else exp.false())
                            changed = True
                if changed:
                    touched.add(name)
                break

    if not touched:
        return None
    listed = ", ".join(sorted(touched)[:8])
    more = f" (+{len(touched) - 8} more)" if len(touched) > 8 else ""
    return Applied(
        "A007", len(touched),
        f"{len(touched)} BIT-style column(s) compared against 0/1 rewritten to "
        f"TRUE/FALSE: {listed}{more}. PostgreSQL cannot compare BOOLEAN with "
        f"INTEGER ('COALESCE types boolean and integer cannot be matched'). "
        f"VERIFY these really are boolean in your PostgreSQL schema -- if any "
        f"is an integer column, revert that one by hand.")


# --------------------------------------------------------------------------
# A020 -- boolean columns used as values (not comparisons)
# --------------------------------------------------------------------------
# A007 fixes boolean COMPARISONS. But a BIT column is also USED as a value:
#
#     CAST(COALESCE(SLV.IsActive, 0) AS VARCHAR(10)) AS IsActive
#
# After BIT -> BOOLEAN migration this fails at RUNTIME with 'COALESCE types
# boolean and integer cannot be matched' (42804) -- valid grammar, so the
# validator never sees it. And even without the COALESCE, PostgreSQL casts a
# boolean to 'true'/'false' where T-SQL CONVERT produced '1'/'0' -- silently
# breaking every UI check for == '1'. Found hundreds of instances in a single
# query map, in queries that had already passed conversion.
#
# Two rewrites, same column evidence as A007 (disclosed, verify the schema):
#   1. COALESCE(IsX, 0/1/'0'/'1') anywhere      -> COALESCE(IsX, FALSE/TRUE)
#   2. CAST(<boolean value> AS char type)       -> CASE ... THEN '1' ELSE '0'
#      preserving T-SQL's '1'/'0' text exactly (NULL stays NULL for the bare
#      column form, matching CONVERT semantics).

def _bool_default(node) -> bool | None:
    """Like _bool_literal, but also reads '0'/'1' STRING defaults -- the
    T-SQL `ISNULL(IsActive, '0')` idiom."""
    value = _bool_literal(node)
    if value is not None:
        return value
    if isinstance(node, exp.Literal) and node.is_string:
        text = str(node.this).strip()
        if text in ("0", "1"):
            return text == "1"
    return None


def fix_boolean_value_use(expressions: list, original: str) -> Applied | None:
    proven = _boolean_columns(original)
    touched: set[str] = set()
    resolver = _type_resolver(expressions)

    def bool_col(node) -> str | None:
        if (isinstance(node, exp.Column) and node.name
                and _is_boolean_column(node.name, proven)
                and not _declared_not_boolean(resolver, node)):
            return node.name
        return None

    for tree in expressions:
        # 1. Integer/'0'/'1' defaults on a boolean COALESCE, in ANY context.
        for co in list(tree.find_all(exp.Coalesce)):
            name = bool_col(co.this)
            if name is None:
                continue
            for default in co.expressions:
                value = _bool_default(default)
                if value is not None:
                    default.replace(exp.true() if value else exp.false())
                    touched.add(name)

        # 2. Boolean rendered as text: emit '1'/'0' like T-SQL CONVERT did.
        # T-SQL CONVERT parses as exp.Convert (this=DataType, expression=value)
        # -- the mirror of exp.Cast (this=value, to=DataType).
        for cast in list(tree.find_all(exp.Cast, exp.Convert)):
            try:
                to = cast.this if isinstance(cast, exp.Convert) else cast.to
                if not to.is_type(*_TEXT_TYPES):
                    continue
            except Exception:
                continue
            inner = (cast.expression if isinstance(cast, exp.Convert)
                     else cast.this)
            if isinstance(inner, exp.Coalesce) and bool_col(inner.this):
                # COALESCE default guarantees non-NULL -> plain ELSE '0'.
                case = exp.Case(
                    ifs=[exp.If(this=inner.copy(),
                                true=exp.Literal.string("1"))],
                    default=exp.Literal.string("0"))
                cast.replace(case)
                touched.add(inner.this.name)
            elif bool_col(inner):
                # NULL must stay NULL, exactly like CONVERT(NVARCHAR, bit).
                case = exp.Case(
                    ifs=[exp.If(this=inner.copy(),
                                true=exp.Literal.string("1")),
                         exp.If(this=exp.Not(this=exp.Paren(this=inner.copy())),
                                true=exp.Literal.string("0"))])
                cast.replace(case)
                touched.add(inner.name)

    if not touched:
        return None
    listed = ", ".join(sorted(touched)[:8])
    more = f" (+{len(touched) - 8} more)" if len(touched) > 8 else ""
    return Applied(
        "A020", len(touched),
        f"{len(touched)} boolean column(s) used as VALUES normalised: "
        f"COALESCE integer defaults -> FALSE/TRUE (PostgreSQL raises 42804 "
        f"'COALESCE types boolean and integer cannot be matched' at runtime), "
        f"and casts to text -> CASE producing '1'/'0' exactly as T-SQL "
        f"CONVERT did (a plain cast would yield 'true'/'false' and break "
        f"consumers comparing to '1'): {listed}{more}. VERIFY these are "
        f"boolean in your PostgreSQL schema.")


# --------------------------------------------------------------------------
# A008 -- T-SQL BIT is a boolean; PostgreSQL BIT is a bit-string
# --------------------------------------------------------------------------
# Unambiguous: in T-SQL, BIT holds 0/1/NULL and IS the boolean type. In
# PostgreSQL, BIT is a fixed-length bit STRING, so CAST(0 AS BIT) yields B'0'
# and comparing it to a boolean column fails.
#
#   CAST(0 AS BIT) -> FALSE          CAST(1 AS BIT) -> TRUE
#   CAST(x AS BIT) -> CAST(x AS BOOLEAN)
#   column BIT     -> column BOOLEAN

def fix_bit_type(expressions: list, original: str) -> Applied | None:
    literals = 0
    casts = 0
    columns = 0

    for tree in expressions:
        for cast in list(tree.find_all(exp.Cast)):
            to = cast.args.get("to")
            if to is None or to.this != exp.DataType.Type.BIT:
                continue
            value = _bool_literal(cast.this)
            if value is not None:
                cast.replace(exp.true() if value else exp.false())
                literals += 1
            else:
                to.set("this", exp.DataType.Type.BOOLEAN)
                casts += 1

        # column declarations: `IsActive BIT` -> `IsActive BOOLEAN`
        for datatype in tree.find_all(exp.DataType):
            if datatype.this == exp.DataType.Type.BIT and not datatype.expressions:
                datatype.set("this", exp.DataType.Type.BOOLEAN)
                columns += 1

    total = literals + casts + columns
    if not total:
        return None
    parts = []
    if literals:
        parts.append(f"{literals} CAST(0/1 AS BIT) -> FALSE/TRUE")
    if casts:
        parts.append(f"{casts} CAST(x AS BIT) -> CAST(x AS BOOLEAN)")
    if columns:
        parts.append(f"{columns} BIT column type -> BOOLEAN")
    return Applied(
        "A008", total,
        f"{'; '.join(parts)}. In T-SQL, BIT IS the boolean type; in PostgreSQL "
        f"BIT is a fixed-length bit-string, so the original would compare a "
        f"bit-string against a boolean and fail.")


# --------------------------------------------------------------------------
# A009 -- database.dbo.Table qualifiers
# --------------------------------------------------------------------------
# PostgreSQL has no cross-database queries, and `dbo` is SQL Server's default
# schema with no PostgreSQL counterpart. Once everything lives in ONE database,
# `qa_app_final.dbo.Fin_Bill` should simply be `Fin_Bill`, resolved via
# search_path.
#
# DELIBERATELY NARROW: only the SQL Server default schema `dbo` is stripped. A
# genuine schema qualifier (`reporting.Sales`) is preserved -- dropping that
# would silently repoint the query at a different table.

def fix_cross_database_qualifier(expressions: list, original: str) -> Applied | None:
    stripped: set[str] = set()
    for tree in expressions:
        # `dbo.fnToLocalTime(...)` parses as Dot(Identifier(dbo), Anonymous).
        # PostgreSQL has no dbo schema, so the call fails with
        # "schema dbo does not exist" -- unwrap it to a bare function call.
        for dot in list(tree.find_all(exp.Dot)):
            owner = dot.this
            if (isinstance(owner, exp.Identifier)
                    and owner.name.lower() == "dbo"
                    and isinstance(dot.expression, exp.Func)):
                stripped.add("dbo.<function>")
                dot.replace(dot.expression)

        for table in tree.find_all(exp.Table):
            # Runtime placeholders (@AppDbName.dbo.T) are A010's concern.
            # Dropping one silently repoints the query at the current
            # database -- never strip them here, whatever the schema part.
            if (placeholder_name(table.args.get("catalog"))
                    or placeholder_name(table.args.get("db"))):
                continue
            catalog, schema = table.text("catalog"), table.text("db")
            if not catalog and schema.lower() != "dbo":
                continue
            if schema and schema.lower() != "dbo":
                # real schema -- drop only the database part, keep the schema
                if catalog:
                    stripped.add(f"{catalog}.{schema}.")
                    table.set("catalog", None)
                continue
            if catalog or schema:
                stripped.add(".".join(p for p in (catalog, schema) if p) + ".")
                table.set("catalog", None)
                table.set("db", None)

    if not stripped:
        return None
    return Applied(
        "A009", len(stripped),
        f"Removed database/schema qualifier(s): {', '.join(sorted(stripped))}. "
        f"PostgreSQL has no cross-database queries and no 'dbo' schema, so "
        f"these tables now resolve through search_path in the current database. "
        f"A non-dbo schema (e.g. reporting.Sales) is preserved.")


# --------------------------------------------------------------------------
# A010 -- runtime database-name placeholders (@AppDbName.dbo.Table)
# --------------------------------------------------------------------------
# Some codebases substitute database names into the query TEXT at runtime:
#     FROM [@AppDbName].[dbo].[Ref_Region]
# where the application string-replaces @AppDbName with a real database
# name before executing. The placeholder is load-bearing: dropping it (what
# A009 would do) silently repoints the query at the CURRENT database, where
# the table does not exist.
#
# PostgreSQL cannot query across databases at all, so the placeholder is kept
# as a QUOTED SCHEMA qualifier instead:
#     FROM "@AppDbName".Ref_Region
# "@AppDbName" is a legal quoted identifier, so grammar validation passes,
# and the runtime substitution keeps working -- the substituted value must
# now be the PostgreSQL SCHEMA holding those tables (exact case: it lands
# inside double quotes), whether that schema is native or a postgres_fdw
# foreign schema.
#
# REFUSAL CASE: a placeholder combined with a real schema
# ([@X].[reporting].[T]) has no two-level PostgreSQL equivalent. Left alone
# (A009 skips placeholders too). PostgreSQL's grammar ACCEPTS a three-part
# name at parse time (it only rejects it at runtime), so the leftover cannot
# be caught by validation -- L015 flags the surviving catalog instead.

_PLACEHOLDER = re.compile(r"^@\w+$")


def placeholder_name(node) -> str | None:
    """`@Name` in an identifier position, in either shape sqlglot produces.

    Bracketed `[@AppDbName]` parses as Identifier('@AppDbName');
    unbracketed `@AppDbName` parses as Parameter(Var('AppDbName')).
    Returns the canonical `@Name` text, or None if `node` is neither.
    """
    if isinstance(node, exp.Parameter):
        name = node.this.name if node.this is not None else ""
        return f"@{name}" if name else None
    if isinstance(node, exp.Identifier) and _PLACEHOLDER.match(node.name or ""):
        return node.name
    return None


def fix_placeholder_database_qualifier(expressions: list,
                                       original: str) -> Applied | None:
    # Same disambiguator as the Dapper-parameter rule: a name that is
    # DECLAREd is a genuine T-SQL local (e.g. @xmlData.nodes(...)), not a
    # database-name placeholder -- leave it for L004 to report.
    from core import schema_hints
    from core.linter import declared_variables
    declared = declared_variables(original)
    drop = schema_hints.db_placeholder_policy() == "drop"

    converted: set[str] = set()
    for tree in expressions:
        for table in tree.find_all(exp.Table):
            cat_ph = placeholder_name(table.args.get("catalog"))
            db_node = table.args.get("db")
            db_ph = placeholder_name(db_node)
            if cat_ph and cat_ph.lower() in declared:
                cat_ph = None
            if db_ph and db_ph.lower() in declared:
                db_ph = None

            if drop:
                # v2.16.0 default: one PostgreSQL database. The placeholder only
                # named the SQL Server database, so it goes -- like A009's
                # `db.dbo.` -- and a real schema behind it is kept.
                if cat_ph:
                    schema = table.text("db")
                    table.set("catalog", None)
                    if not schema or schema.lower() == "dbo" or placeholder_name(db_node):
                        table.set("db", None)
                    converted.add(cat_ph)
                elif db_ph and not table.args.get("catalog"):
                    table.set("db", None)
                    converted.add(db_ph)
                continue

            if cat_ph:
                schema = table.text("db")
                if not db_ph and schema and schema.lower() != "dbo":
                    continue  # refusal: placeholder + real schema -> L015
                table.set("db", exp.Identifier(this=cat_ph, quoted=True))
                table.set("catalog", None)
                converted.add(cat_ph)
                continue

            # Two-part form @DbName.Table: already in schema position. A
            # Parameter or unquoted @name renders bare -- invalid PostgreSQL.
            # Normalize to a quoted identifier either way; the disclosure
            # (substitute a SCHEMA now, not a database) applies regardless.
            if db_ph and not table.args.get("catalog"):
                if not (isinstance(db_node, exp.Identifier)
                        and db_node.args.get("quoted")):
                    table.set("db", exp.Identifier(this=db_ph, quoted=True))
                converted.add(db_ph)

    if not converted:
        return None
    if drop:
        return Applied(
            "A010", len(converted),
            f"Runtime database-name placeholder(s) removed: "
            f"{', '.join(sorted(converted))}. The target is ONE PostgreSQL "
            f"database, so the tables are reached unqualified (search_path), "
            f"as A009 does for `db.dbo.`. If your platform maps each SQL Server "
            f"database to a PostgreSQL schema instead, set "
            f"MSSQLTOPG_DB_PLACEHOLDER=schema (or --keep-db-placeholders) to "
            f"keep them as quoted schemas.")
    return Applied(
        "A010", len(converted),
        f"Runtime database-name placeholder(s) kept as QUOTED SCHEMA "
        f"qualifier(s): {', '.join(sorted(converted))}. PostgreSQL cannot "
        f"query across databases, so the value your application substitutes "
        f"at runtime must now be the PostgreSQL schema that holds those "
        f"tables (exact case -- it lands inside double quotes), not a "
        f"database name.")


# --------------------------------------------------------------------------
# A017 -- N'...' national string literals
# --------------------------------------------------------------------------

def fix_national_string(expressions: list, original: str) -> Applied | None:
    """T-SQL's `N'text'` national literal has no PostgreSQL equivalent, and
    leaving it alone is not harmless: PostgreSQL reads `N 'text'` as the
    type-qualified literal syntax (`int4 '5'`), so it PARSES and then fails at
    execution with `type "n" does not exist`. Grammar validation cannot catch
    it -- it is valid syntax with the wrong meaning.

    Unwrapping to a plain `'text'` is lossless: PostgreSQL text is Unicode
    already, which is the only thing the N prefix bought in T-SQL.

    Ordered BEFORE A011 on purpose. sqlglot parses `N'...'` as exp.National,
    not exp.Literal, so a chain like `Code + N' - ' + Name` looked to A011 like
    it held no string literal and was refused as untyped -- leaving a `+`
    between two text columns that PostgreSQL rejects. Unwrapping first turns it
    into an ordinary string literal and the chain converts.
    """
    count = 0
    for tree in expressions:
        for node in list(tree.find_all(exp.National)):
            value = node.this
            text = value.this if isinstance(value, exp.Expression) else value
            node.replace(exp.Literal.string(text))
            count += 1
    if not count:
        return None
    return Applied(
        "A017", count,
        f"{count} N'...' national string literal(s) unwrapped to '...'. "
        f"PostgreSQL has no N-prefixed literal -- it would parse N'x' as a "
        f"type-qualified literal and fail at runtime with 'type \"n\" does "
        f"not exist'. PostgreSQL text is already Unicode.")


# --------------------------------------------------------------------------
# A011 -- string concatenation with `+`
# --------------------------------------------------------------------------

def _flatten_add(node):
    """Yield the operands of a left-nested chain of exp.Add nodes in order."""
    if isinstance(node, exp.Add):
        yield from _flatten_add(node.this)
        yield from _flatten_add(node.expression)
    else:
        yield node


# Functions whose result is text regardless of their arguments. Conservative:
# only functions that can never return a number belong here.
_TEXT_FUNC_NAMES = {
    "CONCAT", "CONCAT_WS", "UPPER", "LOWER", "LTRIM", "RTRIM", "TRIM",
    "REPLACE", "SUBSTRING", "LEFT", "RIGHT", "REVERSE", "STUFF", "SPACE",
    "REPLICATE", "TRANSLATE", "QUOTENAME", "STRING_AGG", "FORMAT", "STR",
}

_TEXT_TYPES = (
    exp.DataType.Type.VARCHAR, exp.DataType.Type.NVARCHAR,
    exp.DataType.Type.CHAR, exp.DataType.Type.NCHAR,
    exp.DataType.Type.TEXT,
)


def _is_textual(node) -> bool:
    """Conservative proof that an expression yields text.

    Extends the original string-literal-only evidence: `ISNULL(x, '')`,
    `CAST(x AS NVARCHAR(MAX))` and `CONCAT(...)` prove a `+` chain is text just
    as surely as a bare literal does. Anything unproven still returns False.
    """
    if isinstance(node, exp.Literal):
        return node.is_string
    if isinstance(node, exp.National):
        return True
    if isinstance(node, exp.Paren):
        return _is_textual(node.this)
    if isinstance(node, exp.Add):
        return any(_is_textual(op) for op in _flatten_add(node))
    if isinstance(node, exp.DPipe):
        return True
    if isinstance(node, exp.Coalesce):
        return any(_is_textual(a) for a in [node.this, *node.expressions])
    if isinstance(node, exp.Case):
        branches = [w.args.get("true") for w in node.args.get("ifs", [])]
        branches.append(node.args.get("default"))
        return any(_is_textual(b) for b in branches if b is not None)
    if isinstance(node, (exp.Concat, exp.ConcatWs, exp.Upper, exp.Lower,
                         exp.Trim, exp.Substring, exp.Left, exp.Right)):
        return True
    if isinstance(node, exp.Cast):   # TryCast subclasses Cast
        return node.to.is_type(*_TEXT_TYPES)
    if isinstance(node, exp.Anonymous):
        return str(node.this or "").upper() in _TEXT_FUNC_NAMES
    return False


def fix_string_concat(expressions: list, original: str) -> Applied | None:
    """`'%' + @SearchedVal + '%'` is text concatenation in T-SQL but a type
    error in PostgreSQL (`operator does not exist: unknown + text`).

    A `+` chain is rewritten to `||` ONLY when at least one operand provably
    yields text (see _is_textual) -- that proves the chain is text (T-SQL
    would coerce the rest).

    Chain-top detection is by IMMEDIATE parent, not any ancestor: a chain
    inside a function argument (`name + isnull(' (' + code + ')', '')`) is an
    independent chain, and the old find_ancestor test skipped it through the
    function boundary -- both chains were left as `+` and refused.

    REFUSAL: a chain with no textual operand (`a + b`, `col + @param`) has no
    type information -- left alone for L001 and human review. A chain that
    also contains a NUMERIC literal is contradictory -- left alone.
    """
    count = 0
    for tree in expressions:
        # Topmost node of each chain only; replacing it rewrites the chain.
        for node in list(tree.find_all(exp.Add)):
            if isinstance(node.parent, exp.Add):
                continue
            operands = list(_flatten_add(node))
            has_text = any(_is_textual(op) for op in operands)
            has_number = any(
                isinstance(op, exp.Literal) and not op.is_string
                for op in operands)
            if not has_text or has_number:
                continue
            new = operands[0]
            for op in operands[1:]:
                new = exp.DPipe(this=new, expression=op)
            node.replace(new)
            count += 1
    if not count:
        return None
    return Applied(
        "A011", count,
        f"{count} `+` string-concatenation chain(s) rewritten to `||`. Only "
        f"chains with textual evidence (a string literal, ISNULL/COALESCE "
        f"with a text argument, CAST to a char type, or a text function) were "
        f"changed; `col + col` and `col + @param` chains carry no type "
        f"information and are reported by L001 instead.")


# --------------------------------------------------------------------------
# A012 -- STRING_SPLIT table function
# --------------------------------------------------------------------------

def fix_string_split(expressions: list, original: str) -> Applied | None:
    """`SELECT value FROM STRING_SPLIT(@Ids, ',')` has no direct PostgreSQL
    equivalent; `string_to_table(@Ids, ',')` (PG14+) is the drop-in. T-SQL's
    result column is named `value`, so the alias is set to `(value)` to keep
    references working: bare -> `AS value`; `AS s` -> `AS s(value)`.

    REFUSAL: the 3-argument form (enable_ordinal) -- its ordinal column has no
    single defensible mapping; left alone and surfaced by the validator.
    """
    count = 0
    for tree in expressions:
        for node in list(tree.find_all(exp.Anonymous)):
            if str(node.this).upper() != "STRING_SPLIT":
                continue
            if len(node.expressions) != 2:
                continue
            node.set("this", "STRING_TO_TABLE")
            # FROM STRING_SPLIT(..) is a Table; CROSS/OUTER APPLY STRING_SPLIT(..) a
            # Lateral (v2.16.0) -- without `(value)` its column would be named after
            # the alias ("SplitValues"), and `value` would not exist
            table = node.find_ancestor(exp.Table, exp.Lateral)
            if table is not None:
                alias = table.args.get("alias")
                if alias is None:
                    table.set("alias", exp.TableAlias(
                        this=exp.to_identifier("value")))
                elif not alias.columns:
                    alias.set("columns", [exp.to_identifier("value")])
            count += 1
    if not count:
        return None
    return Applied(
        "A012", count,
        f"{count} STRING_SPLIT call(s) rewritten to STRING_TO_TABLE (PG14+) "
        f"with the result column aliased as `value` to match T-SQL. Requires "
        f"PostgreSQL 14 or later.")


# --------------------------------------------------------------------------
# A014 -- empty string seeded into a boolean-shaped column
# --------------------------------------------------------------------------

def fix_boolean_empty_string(expressions: list, original: str) -> Applied | None:
    """`INSERT (Is2FA, ...) VALUES ('', ...)` / `SET Is2FA = ''` works on SQL
    Server (implicit conversion of '' to 0) but PostgreSQL raises
    'invalid input syntax for type boolean: ""'. The empty string becomes FALSE
    for columns with boolean evidence (Is* convention or the known-flag list).

    Column positions are read from the INSERT's schema.expressions ONLY --
    walking find_all(Identifier) would count the table name and shift every
    VALUES index by one (silent wrong-column rewrite).
    """
    proven = _boolean_columns(original)
    resolver = _type_resolver(expressions)
    count = 0
    for tree in expressions:
        for insert in tree.find_all(exp.Insert):
            schema = insert.this
            if not isinstance(schema, exp.Schema):
                continue
            declared = {}
            if resolver is not None and isinstance(schema.this, exp.Table):
                declared = resolver.types.get(schema.this.name.lstrip("#@").lower()) or {}
            cols = [c.name if declared.get(c.name.lower()) in (None, "boolean") else ""
                    for c in schema.expressions]
            values = insert.expression
            if not isinstance(values, exp.Values):
                continue
            for row in values.expressions:
                for idx, item in enumerate(row.expressions):
                    if idx >= len(cols):
                        break
                    if (_is_boolean_column(cols[idx], proven)
                            and isinstance(item, exp.Literal)
                            and item.is_string and str(item.this) == ""):
                        item.replace(exp.false())
                        count += 1
        for update in tree.find_all(exp.Update):
            for assignment in update.expressions:
                if not isinstance(assignment, exp.EQ):
                    continue
                target, value = assignment.this, assignment.expression
                if (isinstance(target, exp.Column) and target.name
                        and _is_boolean_column(target.name, proven)
                        and not _declared_not_boolean(resolver, target)
                        and isinstance(value, exp.Literal)
                        and value.is_string and str(value.this) == ""):
                    value.replace(exp.false())
                    count += 1
    if not count:
        return None
    return Applied(
        "A014", count,
        f"{count} empty-string value(s) written to boolean-shaped columns "
        f"replaced with FALSE. PostgreSQL rejects '' as a boolean "
        f"('invalid input syntax for type boolean').")


# --------------------------------------------------------------------------
# A015 -- TRY_CAST
# --------------------------------------------------------------------------

def fix_try_cast(expressions: list, original: str) -> Applied | None:
    """PostgreSQL has no TRY_CAST. The nearest drop-in is CAST -- disclosed
    loudly because the semantics differ: TRY_CAST returns NULL on a bad value,
    CAST raises. If the column can hold unparseable data, the query needs a
    guarded expression instead."""
    count = 0
    for tree in expressions:
        for node in list(tree.find_all(exp.TryCast)):
            node.replace(exp.Cast(this=node.this.copy(),
                                  to=node.args["to"].copy()))
            count += 1
    if not count:
        return None
    return Applied(
        "A015", count,
        f"{count} TRY_CAST call(s) rewritten to CAST. NOTE the semantic "
        f"change: TRY_CAST yields NULL for an unconvertible value, CAST "
        f"raises an error -- verify the source data is always castable.")


# --------------------------------------------------------------------------
# A021 -- numeric CAST of a Dapper parameter must tolerate ''
# --------------------------------------------------------------------------
# A common guard idiom is `@SelectedVal = '' OR Id = TRY_CAST(@SelectedVal AS
# INT)`. T-SQL is forgiving twice over: TRY_CAST('' ) is NULL, and even plain
# CAST('' AS INT) is 0. PostgreSQL is neither -- CAST('' AS INT) raises 22P02,
# and because the planner constant-folds casts of bound parameter values, the
# error fires at PLAN time even when the other OR arm is true.
#
# NULLIF makes the cast total: '' -> NULL, exactly TRY_CAST's behaviour, and
# a parameter bound as a real integer round-trips through TEXT unchanged.
# Non-numeric garbage still errors -- disclosed, since TRY_CAST would not.

_NUMERIC_TARGETS = (
    exp.DataType.Type.INT, exp.DataType.Type.BIGINT,
    exp.DataType.Type.SMALLINT, exp.DataType.Type.TINYINT,
    exp.DataType.Type.DECIMAL, exp.DataType.Type.DOUBLE,
    exp.DataType.Type.FLOAT,
)


def fix_param_numeric_cast(expressions: list, original: str) -> Applied | None:
    count = 0
    for tree in expressions:
        for cast in list(tree.find_all(exp.Cast, exp.Convert)):
            is_convert = isinstance(cast, exp.Convert)
            to = cast.this if is_convert else cast.args.get("to")
            inner = cast.expression if is_convert else cast.this
            if not isinstance(inner, exp.Parameter):
                continue
            try:
                if not to.is_type(*_NUMERIC_TARGETS):
                    continue
            except Exception:
                continue
            safe = exp.Nullif(
                this=exp.Cast(this=inner.copy(),
                              to=exp.DataType.build("TEXT")),
                expression=exp.Literal.string(""))
            if is_convert:
                cast.set("expression", safe)
            else:
                cast.set("this", safe)
            count += 1
    if not count:
        return None
    return Applied(
        "A021", count,
        f"{count} numeric cast(s) of a query parameter made empty-string "
        f"safe: CAST(@p AS INT) -> CAST(NULLIF(CAST(@p AS TEXT), '') AS "
        f"INT). PostgreSQL raises 22P02 on CAST('' AS INT) at PLAN time even "
        f"inside a guarded OR; '' now yields NULL like T-SQL TRY_CAST. A "
        f"non-numeric value still errors -- T-SQL TRY_CAST returned NULL.")


# --------------------------------------------------------------------------
# A022 -- user-defined `dbo.Split(x, sep)` table function
# --------------------------------------------------------------------------
# The MSSQL databases carry a user-defined `dbo.Split` returning a table with
# an `items` column (`SELECT CAST(items AS INT) FROM dbo.Split(@Ids, ','))`).
# The PostgreSQL baseline has no such function -- verified against the
# Liquibase changelogs -- so the call fails at runtime with 'function split
# does not exist'. STRING_TO_TABLE (PG14+) is the drop-in; the alias
# `split(items)` keeps every `items` reference working.

def _empty_safe(arg):
    """NULLIF(x, '') -- string_to_table is strict, so NULL in means ZERO rows
    out, matching dbo.Split('') and preventing CAST('' AS INT) errors on the
    single '' row PostgreSQL would otherwise return for an empty CSV."""
    if isinstance(arg, exp.Nullif):
        return None                      # already safe -- idempotency
    return exp.Nullif(this=arg.copy(), expression=exp.Literal.string(""))


def fix_acs_split(expressions: list, original: str) -> Applied | None:
    count = 0
    for tree in expressions:
        for table in list(tree.find_all(exp.Table)):
            func = table.this
            args = None
            is_split = False
            if isinstance(func, exp.Split):
                args = [func.this, func.expression]
                is_split = True
            elif isinstance(func, exp.Anonymous):
                fname = str(func.this).upper()
                if fname == "SPLIT":
                    args = list(func.expressions)
                    is_split = True
                elif fname == "STRING_TO_TABLE":
                    # Already converted (by A012 or an earlier pass) -- only
                    # ensure the empty-CSV guard is present.
                    args = list(func.expressions)
            if args is None or len(args) != 2 or any(a is None for a in args):
                continue
            if is_split:
                safe = _empty_safe(args[0])
                replacement = exp.Anonymous(
                    this="STRING_TO_TABLE",
                    expressions=[safe if safe is not None else args[0].copy(),
                                 args[1].copy()])
                table.set("this", replacement)
                table.set("db", None)
                table.set("catalog", None)
                alias = table.args.get("alias")
                if alias is None:
                    table.set("alias", exp.TableAlias(
                        this=exp.to_identifier("split"),
                        columns=[exp.to_identifier("items")]))
                elif not alias.columns:
                    alias.set("columns", [exp.to_identifier("items")])
                count += 1
            else:
                safe = _empty_safe(args[0])
                if safe is not None:
                    args[0].replace(safe)
                    count += 1
    if not count:
        return None
    return Applied(
        "A022", count,
        f"{count} CSV-split table call(s) normalised: dbo.Split(...) -> "
        f"STRING_TO_TABLE(...) AS split(items) (PG14+; no Split function "
        f"exists in the PostgreSQL schema), and the CSV argument wrapped in "
        f"NULLIF(x, '') so an empty parameter yields ZERO rows like "
        f"dbo.Split('') -- PostgreSQL would return one '' row and any "
        f"CAST(items AS INT) on it would raise 22P02.")


# --------------------------------------------------------------------------
# A019 -- T-SQL UPDATE/DELETE targets a FROM alias
# --------------------------------------------------------------------------

def _retarget_update_with_joins(upd, src, joins, from_key) -> bool:
    """v2.16.0: `UPDATE S SET ... FROM t S [INNER] JOIN x ON c1 [JOIN y ON c2] WHERE w`
    -> `UPDATE t AS S SET ... FROM x, y WHERE c1 AND c2 AND (w)`.

    PostgreSQL does not let a JOIN ... ON inside the FROM list reference the
    UPDATE target ("invalid reference to FROM-clause entry"), so every ON
    condition moves to WHERE and the joined tables become a comma list -- exact
    for inner joins. Outer / cross / APPLY / USING joins keep the target's rows
    even without a match; UPDATE ... FROM cannot say that, so they are refused
    (L018 reports them).
    """
    for join in joins:
        kind = (join.args.get("kind") or "").upper()
        if join.args.get("side") or kind not in ("", "INNER") or join.args.get("using") \
                or not join.args.get("on") or not isinstance(join.this, (exp.Table, exp.Subquery)):
            return False
    target = src.copy()
    target.set("joins", None)
    first = joins[0].this.copy()
    rest = [exp.Join(this=j.this.copy()) for j in joins[1:]]
    if rest:
        first.set("joins", rest)
    conditions = [j.args["on"].copy() for j in joins]
    where = upd.args.get("where")
    if where is not None:
        conditions.append(exp.paren(where.this.copy(), copy=False))
    combined = conditions[0]
    for cond in conditions[1:]:
        combined = exp.and_(combined, cond, copy=False)
    upd.set("this", target)
    upd.set(from_key, exp.From(this=first))
    upd.set("joins", None)
    upd.set("where", exp.Where(this=combined))
    return True


def _retarget_update_via_ctid(upd, src) -> bool:
    """v2.16.0: `UPDATE S SET ... FROM t S [INNER JOIN m ON ..] LEFT JOIN x ON .. WHERE w`
    -> `UPDATE t AS S_upd SET ... FROM t S [INNER JOIN m ..] LEFT JOIN x .. WHERE S_upd.ctid = S.ctid AND (w)`.

    An outer join must keep every row of t, which UPDATE ... FROM can only say by
    joining t to itself: the FROM list is left exactly as written (so every `S.`
    still names the row being joined) and the target is the same row, matched on
    its physical row id. Exact for tables and temp tables; not for a view or a
    foreign table (no ctid)."""
    alias = src.alias or src.name
    if not alias or not isinstance(src.this, exp.Identifier):
        return False
    used = {(t.alias or t.name or "").lower() for t in upd.find_all(exp.Table)}
    new = f"{alias}_upd"
    while new.lower() in used:
        new += "_"
    target = exp.Table(this=src.this.copy(), alias=exp.TableAlias(this=exp.to_identifier(new)))
    for part in ("db", "catalog"):
        if src.args.get(part) is not None:
            target.set(part, src.args[part].copy())
    match = exp.EQ(this=exp.column("ctid", table=new), expression=exp.column("ctid", table=alias))
    where = upd.args.get("where")
    combined = match if where is None else exp.and_(match, exp.paren(where.this.copy(), copy=False), copy=False)
    upd.set("this", target)
    upd.set("where", exp.Where(this=combined))
    return True


def fix_dml_alias_target(expressions: list, original: str) -> Applied | None:
    """T-SQL allows `UPDATE S SET ... FROM templst S` and `DELETE S FROM
    templst S`. PostgreSQL resolves the UPDATE/DELETE target BEFORE the FROM
    clause, so `UPDATE S` means a real table named S -- the statement passes
    grammar validation and fails at runtime with 'relation "s" does not
    exist'. The worst class of defect: valid syntax, wrong meaning.

    Fixed only in the unambiguous single-table case: the FROM clause holds
    exactly one table (no joins) whose alias is the update target, so the
    rewrite `UPDATE templst AS S SET ... [WHERE ...]` is exact. A FROM with
    joins needs its join conditions moved into WHERE -- that is a judgement
    call, refused here and reported as L018 by the linter.
    """
    count = 0
    for tree in expressions:
        for upd in list(tree.find_all(exp.Update)):
            target = upd.this
            if not isinstance(target, exp.Table) or target.alias:
                continue
            if target.args.get("db") or target.args.get("catalog"):
                continue
            # sqlglot >= 30 names the arg "from_"; older versions "from".
            from_key = "from_" if "from_" in upd.arg_types else "from"
            frm = upd.args.get(from_key)
            if frm is None:
                continue
            src = frm.this
            if not isinstance(src, exp.Table):
                continue
            # `UPDATE S ... FROM t S` -- or, v2.16.0, `UPDATE t ... FROM t JOIN ...`
            # (the target repeated unaliased: "table name specified more than once")
            if (src.alias or src.name or "").lower() != target.name.lower():
                continue
            # sqlglot 30 nests joins on the FROM's first table node.
            joins = list(src.args.get("joins") or []) + list(upd.args.get("joins") or [])
            if not joins:
                upd.set("this", src.copy())
                upd.set(from_key, None)
                count += 1
                continue
            if _retarget_update_with_joins(upd, src, joins, from_key):
                count += 1
            elif _retarget_update_via_ctid(upd, src):
                count += 1
        for dele in list(tree.find_all(exp.Delete)):
            tables = dele.args.get("tables") or []
            target = dele.this
            if len(tables) != 1 or not isinstance(target, exp.Table):
                continue
            named = tables[0]
            if not isinstance(named, exp.Table):
                continue
            if named.name.lower() in {(target.alias or "").lower(),
                                      target.name.lower()}:
                dele.set("tables", [])
                count += 1
    if not count:
        return None
    return Applied(
        "A019", count,
        f"{count} UPDATE/DELETE statement(s) re-targeted from a FROM alias to "
        f"the real table (`UPDATE S ... FROM t S` -> `UPDATE t AS S ...`). "
        f"PostgreSQL resolves the target before FROM, so the T-SQL form "
        f"passes validation but fails at runtime with 'relation does not "
        f"exist'. Inner joins become a FROM list with their ON in WHERE; an "
        f"outer join keeps the FROM as written and matches the target row on "
        f"ctid (`WHERE S_upd.ctid = S.ctid`), which needs a table, not a view.")


# --------------------------------------------------------------------------
# A055 -- UPDATE ... SET alias.column
# --------------------------------------------------------------------------

def fix_update_set_qualifier(expressions: list, original: str) -> Applied | None:
    """T-SQL writes `UPDATE c SET c.StateCode = ...`; a PostgreSQL SET target is a
    column of the target table and cannot be qualified -- `c.StateCode` is read as
    field StateCode of a column named c: 'column "c" of relation "tempcity" does not
    exist' (42703) at runtime, after the grammar check passed. The qualifier only
    ever names the target, so it is dropped."""
    count = 0
    for tree in expressions:
        for upd in tree.find_all(exp.Update):
            for assignment in upd.expressions or []:
                col = assignment.this if isinstance(assignment, exp.EQ) else None
                if isinstance(col, exp.Column) and col.args.get("table") is not None:
                    for part in ("table", "db", "catalog"):
                        col.set(part, None)
                    count += 1
    if not count:
        return None
    return Applied(
        "A055", count,
        f"{count} qualified UPDATE ... SET target(s) (`SET c.Col = ...`) written as "
        f"the bare column: PostgreSQL reads `c.Col` in SET as a field of a column "
        f"named c and fails at runtime (42703).")


# --------------------------------------------------------------------------
# A056 -- recursive CTE without RECURSIVE
# --------------------------------------------------------------------------

def fix_recursive_cte(expressions: list, original: str) -> Applied | None:
    """T-SQL has no RECURSIVE keyword: a CTE that reads its own name is recursive.
    PostgreSQL needs `WITH RECURSIVE`, otherwise the self reference is an unknown
    table -- 'relation "datecte" does not exist' at runtime."""
    count = 0
    for tree in expressions:
        for with_ in tree.find_all(exp.With):
            if with_.args.get("recursive"):
                continue
            for cte in with_.expressions:
                name = (cte.alias_or_name or "").lower()
                body = cte.this
                if name and body is not None and any(
                        (t.name or "").lower() == name and not t.args.get("db")
                        for t in body.find_all(exp.Table)):
                    with_.set("recursive", True)
                    count += 1
                    break
    if not count:
        return None
    return Applied(
        "A056", count,
        f"{count} recursive CTE(s) marked WITH RECURSIVE (T-SQL infers recursion "
        f"from the self reference; PostgreSQL needs the keyword).")


# --------------------------------------------------------------------------
# A057 -- SELECT DISTINCT ... ORDER BY an expression that is not selected
# --------------------------------------------------------------------------

def fix_distinct_order_by(expressions: list, original: str) -> Applied | None:
    """`SELECT DISTINCT CAST(t.Id AS NVARCHAR) AS Value, ... ORDER BY t.Name` is
    refused by SQL Server (Msg 145) and PostgreSQL (42P10) alike: the rows are
    already collapsed when ORDER BY runs, so a column that is not selected has no
    single value. The dropdown queries that carry it mean "the distinct rows,
    ordered by the name they were built from", so the rewrite is

        SELECT <same list> ... GROUP BY 1, 2, ... ORDER BY MIN(t.Name)

    -- the same rows and columns as DISTINCT (every output column is a group key),
    ordered by the smallest (ASC) / largest (DESC) value in each group, which is
    THE value whenever the expression follows from the selected columns."""
    count = 0
    for tree in expressions:
        for select in tree.find_all(exp.Select):
            distinct = select.args.get("distinct")
            order = select.args.get("order")
            if distinct is None or distinct.args.get("on") or order is None or select.args.get("group"):
                continue
            items = select.expressions
            if not items or any(isinstance(i, exp.Star) or i.find(exp.AggFunc, exp.Window, exp.Star)
                                for i in items):
                continue
            aliases = {i.alias.lower() for i in items if isinstance(i, exp.Alias) and i.alias}
            aliases |= {i.name.lower() for i in items if isinstance(i, exp.Column)}
            selected = {(i.this if isinstance(i, exp.Alias) else i).sql().lower() for i in items}
            missing = []
            for ordered in order.expressions:
                key = ordered.this
                if key.sql().lower() in selected:
                    continue
                if isinstance(key, exp.Column) and not key.table and key.name.lower() in aliases:
                    continue
                if isinstance(key, exp.Literal) and not key.is_string:
                    continue   # ORDER BY 2 -- an output column already
                missing.append(ordered)
            if not missing:
                continue
            for ordered in missing:
                agg = exp.Max if ordered.args.get("desc") else exp.Min
                ordered.set("this", agg(this=ordered.this.copy()))
            select.set("distinct", None)
            select.set("group", exp.Group(expressions=[exp.Literal.number(n + 1) for n in range(len(items))]))
            count += 1
    if not count:
        return None
    return Applied(
        "A057", count,
        f"{count} SELECT DISTINCT(s) ordered by an expression that is not selected "
        f"rewritten to GROUP BY 1..n with ORDER BY MIN(expr) / MAX(expr) for DESC. "
        f"The source is refused by SQL Server too (Msg 145, 'ORDER BY items must "
        f"appear in the select list if SELECT DISTINCT is specified'), so VERIFY "
        f"the order is the intended one; the rows and columns are DISTINCT's.")


# --------------------------------------------------------------------------
# A018 -- missing statement terminators between T-SQL statements
# --------------------------------------------------------------------------
# T-SQL tolerates whole batches with no `;` between statements. sqlglot cannot
# segment them: the parse either fails outright (SG-PARSE) or falls back to a
# passthrough Command node, and either way the query is refused. This was the
# single largest cause of skipped queries in the C# scans.
#
# This fix is TEXT-level and runs BEFORE parsing (the engine calls it only when
# the initial parse failed or fell back), so unlike every other rule it cannot
# use the AST. It is therefore deliberately conservative:
#
#   * a `;` is inserted only before a line that STARTS a new statement keyword
#     at parenthesis depth 0  -- derived tables, subqueries and CTE bodies are
#     never split;
#   * never when the previous code line ends in a continuation token
#     (`,` `(` `+` `AND` `UNION` ...) -- a wrapped clause is not a new statement;
#   * never between `INSERT INTO t [(cols)]` and its `SELECT` feed;
#   * never between a `WITH cte AS (...)` chain and its body statement;
#   * the `;` lands on the last CODE line, skipping comment-only lines, so a
#     trailing `--OFFSET ...` comment cannot swallow it.
#
# The engine accepts the result ONLY if the retried parse yields real
# statements (no ParseError, no Command fallback), and the scan's existing
# rewrite gate (grammar VALID + zero ERROR findings) still applies after that.
# A wrong guess therefore cannot corrupt source -- it can only leave the query
# refused, which is where it started.

_STMT_STARTER = re.compile(
    r"^\s*(SELECT|INSERT|UPDATE|DELETE|CREATE|DROP|TRUNCATE|MERGE|WITH)\b",
    re.IGNORECASE)

# A previous line ending in one of these is mid-statement -- never terminate it.
_CONTINUATION_END = re.compile(
    r"(?:[,(+\-*/=<>&|]"
    r"|\bAND\b|\bOR\b|\bNOT\b|\bIN\b|\bEXISTS\b|\bUNION\b|\bALL\b"
    r"|\bEXCEPT\b|\bINTERSECT\b|\bAS\b|\bON\b|\bFROM\b|\bJOIN\b|\bINTO\b"
    r"|\bWHERE\b|\bHAVING\b|\bTHEN\b|\bELSE\b|\bWHEN\b|\bCASE\b|\bBEGIN\b"
    r"|\bSET\b|\bBY\b|\bTOP\b|\bDISTINCT\b|\bLIKE\b|\bBETWEEN\b|\bIS\b"
    r"|\bCROSS\b|\bOUTER\b|\bAPPLY\b|\bLIKE\b)\s*$",
    re.IGNORECASE)


def _scrub(sql: str) -> str:
    """Same-length copy with string contents and comments blanked, newlines
    kept, so structural scanning cannot be fooled by quoted or commented text."""
    out = list(sql)
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        if ch == "'":
            j = i + 1
            while j < n:
                if sql[j] == "'":
                    if j + 1 < n and sql[j + 1] == "'":
                        j += 2
                        continue
                    break
                j += 1
            for k in range(i + 1, min(j, n)):
                if out[k] != "\n":
                    out[k] = " "
            i = j + 1
        elif ch == "-" and sql[i + 1:i + 2] == "-":
            j = sql.find("\n", i)
            j = n if j == -1 else j
            for k in range(i, j):
                out[k] = " "
            i = j
        elif ch == "/" and sql[i + 1:i + 2] == "*":
            depth, j = 1, i + 2          # T-SQL block comments nest
            while j < n and depth:
                if sql[j:j + 2] == "/*":
                    depth, j = depth + 1, j + 2
                elif sql[j:j + 2] == "*/":
                    depth, j = depth - 1, j + 2
                else:
                    j += 1
            for k in range(i, min(j, n)):
                if out[k] != "\n":
                    out[k] = " "
            i = j
        else:
            i += 1
    return "".join(out)


def infer_statement_terminators(sql: str) -> tuple[str, int]:
    """Insert the `;` terminators a T-SQL batch omitted. Returns (sql, count);
    count 0 means nothing looked like an unterminated statement boundary."""
    scrubbed = _scrub(sql)
    orig_lines = sql.split("\n")
    code_lines = scrubbed.split("\n")

    depth = 0                   # paren depth at the START of the current line
    head: str | None = None     # first keyword of the statement being read
    insert_feed_pending = False  # INSERT seen; its SELECT/VALUES not yet
    with_body_pending = False    # WITH seen; its body statement not yet
    last_code_idx: int | None = None
    inserted = 0

    def begin(word: str) -> None:
        nonlocal head, insert_feed_pending, with_body_pending
        head = word
        insert_feed_pending = word == "INSERT"
        with_body_pending = word == "WITH"

    for idx, code in enumerate(code_lines):
        stripped = code.strip()
        if not stripped:
            continue            # blank or comment-only line

        starter = _STMT_STARTER.match(code) if depth == 0 else None
        if starter:
            word = starter.group(1).upper()
            prev = ("" if last_code_idx is None
                    else code_lines[last_code_idx].rstrip())
            if head is None or prev.endswith(";"):
                begin(word)     # fresh statement, nothing to terminate
            elif _CONTINUATION_END.search(prev):
                pass            # wrapped clause -- same statement continues
            elif insert_feed_pending and word == "SELECT":
                insert_feed_pending = False   # the INSERT's feed, not new work
            elif with_body_pending and word in (
                    "SELECT", "INSERT", "UPDATE", "DELETE"):
                with_body_pending = False     # the CTE body
                if word == "INSERT":
                    insert_feed_pending = True
            else:
                # The `;` goes where the CODE ends, read off the scrubbed
                # (same-length, comments blanked) line: appended to the raw
                # line it would land inside a trailing `--` comment and vanish.
                line = orig_lines[last_code_idx]
                cut = len(code_lines[last_code_idx].rstrip("\r").rstrip())
                orig_lines[last_code_idx] = line[:cut] + ";" + line[cut:]
                inserted += 1
                begin(word)

        if insert_feed_pending and re.search(
                r"\b(SELECT|VALUES)\b", stripped, re.IGNORECASE):
            insert_feed_pending = False
        if depth == 0 and ";" in stripped:
            head = None
            insert_feed_pending = with_body_pending = False

        depth = max(0, depth + stripped.count("(") - stripped.count(")"))
        last_code_idx = idx

    return "\n".join(orig_lines), inserted


def terminator_note(count: int) -> Applied:
    return Applied(
        "A018", count,
        f"{count} missing statement terminator(s) inserted: the batch ran "
        f"T-SQL statements together with no `;`, which cannot be segmented. "
        f"Boundaries were inferred conservatively (never inside parentheses, "
        f"a wrapped clause, an INSERT...SELECT feed or a CTE body) and the "
        f"result was accepted only because it then parsed as real statements.")


ALL_FIXES = (
    fix_rowversion,
    fix_windows_timezone,
    fix_date_literals,
    fix_varchar_max,
    fix_clustered_index,
    fix_unsupported_builtins,
    fix_bit_integer_comparison,
    fix_boolean_value_use,
    fix_bit_type,
    fix_placeholder_database_qualifier,
    fix_cross_database_qualifier,
    # A017 must precede A011: it turns N'...' into a plain string literal, which
    # is what lets a `col + N' - ' + col` chain be recognised as text.
    fix_national_string,
    fix_string_concat,
    fix_string_split,
    fix_boolean_empty_string,
    fix_try_cast,
    # A021 runs after A015 so TRY_CAST has already collapsed to CAST.
    fix_param_numeric_cast,
    fix_acs_split,
    fix_dml_alias_target,
    fix_update_set_qualifier,
    fix_recursive_cte,
    fix_distinct_order_by,
)


def apply_all(expressions: list, original: str) -> list[Applied]:
    """Run every auto-fix over the AST in place. Returns what was applied."""
    trees = [t for t in expressions if t is not None]
    applied = []
    for fix in ALL_FIXES:
        result = fix(trees, original)
        if result is not None:
            applied.append(result)
    return applied


# ==========================================================================
# TEXT-LEVEL PRE-PARSE REWRITES
#
# These run BEFORE sqlglot sees the SQL. sqlglot has no representation for
# `FOR XML PATH` / `FOR JSON PATH` -- it reports "Unsupported query option"
# and DROPS the construct, which would silently turn a scalar string-agg
# subquery into a multi-row subquery. Rewriting the idiom to its PostgreSQL
# equivalent first means sqlglot never meets the unsupported option.
# ==========================================================================

def _skip_string(text: str, i: int) -> int:
    """`i` points at the opening quote; return the index just past the close.
    Doubled quotes ('') are an escaped quote, not a terminator."""
    i += 1
    n = len(text)
    while i < n:
        if text[i] == "'":
            if i + 1 < n and text[i + 1] == "'":
                i += 2
                continue
            return i + 1
        i += 1
    return n


def _match_paren(text: str, i: int) -> int:
    """`i` points just past an opening '('; return the index of its match.

    String literals are skipped, so a '(' or ')' inside a literal cannot
    throw the depth count off. Returns -1 when unbalanced.
    """
    depth = 1
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "'":
            i = _skip_string(text, i)
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


_STUFF_RE = re.compile(r"(?i)\bSTUFF\s*\(")
_FOR_XML_TAIL_RE = re.compile(r"(?is)\s*FOR\s+XML\s+PATH\s*\(\s*''\s*\)\s*$")
# The length argument is a number -- or LEN(','), whose quoted comma a plain
# `[^,]+` refused (v2.14.0, Getproducttypesearch).
_STUFF_ARGS_RE = re.compile(
    r"(?is)^\s*\((.*)\)\s*,\s*1\s*,\s*(?:LEN\s*\(\s*N?'[^']*'\s*\)|[^,]+?)\s*,\s*''\s*$")
_SEP_FIRST_RE = re.compile(
    r"(?is)^SELECT\s+(DISTINCT\s+)?'([^']*)'\s*\+\s*(.+?)\s+FROM\s+(.*)$")
_ORDER_BY_RE = re.compile(r"(?is)\bORDER\s+BY\s+(.+)$")

_KEEP = "STUFF KEEP ("


def _drop_line_comments(text: str) -> str:
    """`text` without its `--` comments (newlines kept, literals respected)."""
    out, i, n = [], 0, len(text)
    while i < n:
        ch = text[i]
        if ch == "'":
            j = i + 1
            while j < n:
                if text[j] == "'":
                    if j + 1 < n and text[j + 1] == "'":
                        j += 2
                        continue
                    break
                j += 1
            out.append(text[i:j + 1])
            i = j + 1
            continue
        if text.startswith("--", i):
            j = text.find("\n", i)
            i = n if j < 0 else j
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def rewrite_for_xml_path(sql: str) -> tuple:
    """A023 -- the T-SQL string-join idiom becomes STRING_AGG.

    `STUFF((SELECT ', ' + x FROM ... FOR XML PATH('')),1,n,'')`
      becomes
    `(SELECT STRING_AGG(x, ', ' [ORDER BY ...]) FROM ...)`

    sqlglot drops the `FOR XML PATH` option, leaving a multi-row subquery
    where a scalar was meant, so this must happen on the text before parsing.

    Only the separator-first shape (`SELECT ', ' + expr`) is rewritten -- the
    form the idiom always takes, because STUFF exists purely to strip the
    leading separator. `SELECT DISTINCT` becomes `STRING_AGG(DISTINCT ...)`
    and a trailing ORDER BY is carried into the aggregate, which also makes
    the output deterministic where T-SQL's was not.

    Note the STUFF length argument is almost always 1 while the separator is
    two characters (', '), so T-SQL left a stray leading space on every value.
    STRING_AGG has no separator to strip, so that artefact disappears --
    callers that split on ',' and trim are unaffected.

    REFUSAL: any STUFF that is not this idiom is left untouched for the
    linter, including a STUFF with no FOR XML tail (a genuine string edit).
    """
    count = 0
    for _ in range(200):                      # bounded: each pass rewrites one
        m = _STUFF_RE.search(sql)
        if not m:
            break
        close = _match_paren(sql, m.end())
        if close < 0:
            sql = sql[:m.start()] + _KEEP + sql[m.end():]
            continue
        inner = sql[m.end():close]
        if not re.search(r"(?is)\bFOR\s+XML\s+PATH\b", inner):
            sql = sql[:m.start()] + _KEEP + sql[m.end():]
            continue
        args = _STUFF_ARGS_RE.match(inner)
        if not args:
            sql = sql[:m.start()] + _KEEP + sql[m.end():]
            continue
        # The body is moved INTO a STRING_AGG and a ')' is appended after it:
        # a `--` comment it carried would swallow that ')' (v2.14.0).
        select_body = _FOR_XML_TAIL_RE.sub("", _drop_line_comments(args.group(1))).strip()
        parts = _SEP_FIRST_RE.match(select_body)
        if not parts:
            sql = sql[:m.start()] + _KEEP + sql[m.end():]
            continue
        distinct = "DISTINCT " if parts.group(1) else ""
        separator = parts.group(2)
        expression = parts.group(3).strip()
        remainder = parts.group(4).strip()
        order_by = ""
        order = _ORDER_BY_RE.search(remainder)
        if order:
            order_by = " ORDER BY " + order.group(1).strip()
            remainder = remainder[:order.start()].strip()
        replacement = ("(SELECT STRING_AGG(" + distinct + expression + ", '"
                       + separator + "'" + order_by + ") FROM " + remainder
                       + ")")
        sql = sql[:m.start()] + replacement + sql[close + 1:]
        count += 1
    return sql.replace(_KEEP, "STUFF("), count


_FOR_JSON_RE = re.compile(r"(?is)\bFOR\s+JSON\s+(PATH|AUTO)\b")


def find_for_json(sql: str) -> int:
    """L019 evidence -- count surviving `FOR JSON PATH/AUTO` clauses.

    Deliberately NOT auto-converted. `FOR JSON PATH` with dotted aliases
    (`x AS [Root.Header.Id]`) builds a NESTED document, `JSON_QUERY(...)`
    embeds raw JSON and `WITHOUT_ARRAY_WRAPPER` collapses the array -- the
    PostgreSQL equivalent is a hand-built json_build_object tree whose shape
    depends on the alias hierarchy. Guessing it would change a payload an
    external system consumes, so the tool reports and refuses.
    """
    return len(_FOR_JSON_RE.findall(sql))


_COMMENT_PARAM_RE = re.compile(r"(?m)--[^\n]*?(@\w+)")


def find_params_in_comments(sql: str) -> list:
    """L020 evidence -- parameter names mentioned inside `--` comments.

    A query pre-processor that substitutes parameters as raw text strips the quotes from the `'@Param'` form wherever it
    appears, comments included. Npgsql's parser does skip comments, so a
    BOUND name survives -- but an UNBOUND one left in prose is a live hazard.
    Write the name without the `@`.
    """
    return _COMMENT_PARAM_RE.findall(sql)
