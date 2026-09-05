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
_BOOLEAN_NAME = re.compile(r"^is[A-Z_]", re.IGNORECASE)


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
    return name.lower() in proven or bool(_BOOLEAN_NAME.match(name))


def _int_literal(node) -> int | None:
    if isinstance(node, exp.Literal) and not node.is_string:
        try:
            value = int(str(node.this))
        except ValueError:
            return None
        return value if value in (0, 1) else None
    return None


def fix_bit_integer_comparison(expressions: list, original: str) -> Applied | None:
    proven = _boolean_columns(original)
    touched: set[str] = set()

    def boolean_column_in(node) -> str | None:
        """The boolean column this expression is really about, if any."""
        target = node
        if isinstance(node, exp.Coalesce):
            target = node.this
        if isinstance(target, exp.Column) and target.name:
            if _is_boolean_column(target.name, proven):
                return target.name
        return None

    for tree in expressions:
        for comparison in list(tree.find_all(exp.EQ, exp.NEQ)):
            left, right = comparison.this, comparison.expression
            for side, other in ((left, right), (right, left)):
                name = boolean_column_in(side)
                if name is None:
                    continue
                changed = False
                # the literal on the other side of the comparison
                value = _int_literal(other)
                if value is not None:
                    other.replace(exp.true() if value else exp.false())
                    changed = True
                # and the COALESCE default, if there is one
                if isinstance(side, exp.Coalesce):
                    for default in side.expressions:
                        value = _int_literal(default)
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
            value = _int_literal(cast.this)
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
# `qa_northwind_final.dbo.Inv_Invoice` should simply be `Inv_Invoice`, resolved via
# search_path.
#
# DELIBERATELY NARROW: only the SQL Server default schema `dbo` is stripped. A
# genuine schema qualifier (`reporting.Sales`) is preserved -- dropping that
# would silently repoint the query at a different table.

def fix_cross_database_qualifier(expressions: list, original: str) -> Applied | None:
    stripped: set[str] = set()
    for tree in expressions:
        # `dbo.fnGetLocalTime(...)` parses as Dot(Identifier(dbo), Anonymous).
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


ALL_FIXES = (
    fix_rowversion,
    fix_windows_timezone,
    fix_date_literals,
    fix_varchar_max,
    fix_clustered_index,
    fix_unsupported_builtins,
    fix_bit_integer_comparison,
    fix_bit_type,
    fix_cross_database_qualifier,
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
