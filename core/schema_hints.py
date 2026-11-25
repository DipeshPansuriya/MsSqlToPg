"""Schema knowledge the converter cannot infer from the query text (A024).

A007/A014/A020 decide "is this column boolean?" from evidence inside the query:
a CAST(0/1 AS BIT) proof, the ^Is... naming convention, or a short allowlist.
That missed real flags -- `GATEINSTATUS`, `IsConsoleShipment`-style names with
a different prefix, `HasAirlineAccepted` -- and 938 `COALESCE(flag, 0)` /
`flag = 1` sites survived conversion on Import alone. Both PARSE and both fail
at runtime (42804 / 42883), so no grammar check can see them.

The fix is the target schema itself: a plain-text list of the column names that
are BOOLEAN in EVERY table that declares them (a name that is boolean in one
table and integer in another is excluded, so the mapping is never ambiguous).
Generate it with `boolean_columns_query.sql` and save the output as
`boolean_columns.txt`.

Lookup order (first file found wins):
    1. the path in the MSSQLTOPG_BOOLEAN_COLUMNS environment variable
    2. boolean_columns.txt next to the running executable
    3. boolean_columns.txt next to app.py (source checkout)

The file is optional. Without it the converter behaves exactly as v2.12.0.
One name per line, case-insensitive; blank lines and `#` comments ignored.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

FILE_NAME = "boolean_columns.txt"
ENV_VAR = "MSSQLTOPG_BOOLEAN_COLUMNS"

_cache: tuple[str | None, frozenset[str]] | None = None


def _candidates() -> list[Path]:
    paths: list[Path] = []
    env = os.environ.get(ENV_VAR)
    if env:
        paths.append(Path(env))
    if getattr(sys, "frozen", False):
        paths.append(Path(sys.executable).resolve().parent / FILE_NAME)
    paths.append(Path(__file__).resolve().parent.parent / FILE_NAME)
    return paths


def parse(text: str) -> frozenset[str]:
    names = set()
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip().strip('"').strip()
        # tolerate a pasted query result with a header row or a "|" grid
        line = line.strip("|").strip()
        if not line or line.lower() in ("column_name", "name", "col"):
            continue
        if line.replace("_", "").isalnum():
            names.add(line.lower())
    return frozenset(names)


def boolean_columns() -> frozenset[str]:
    """Lower-cased column names known to be BOOLEAN in the target schema."""
    global _cache
    for path in _candidates():
        key = str(path)
        if _cache is not None and _cache[0] == key:
            return _cache[1]
        try:
            if path.is_file():
                names = parse(path.read_text(encoding="utf-8-sig",
                                             errors="replace"))
                _cache = (key, names)
                return names
        except OSError:
            continue
    _cache = (None, frozenset())
    return _cache[1]


def source() -> str | None:
    """Which file the list came from (None = no list loaded)."""
    boolean_columns()
    return _cache[0] if _cache else None


def reset() -> None:
    """Forget the cached lists (tests, or after the files are edited)."""
    global _cache, _types_cache
    _cache = None
    _types_cache = None


# --------------------------------------------------------------------------
# v2.16.0 -- column types (A054) and the database-placeholder policy (A010)
# --------------------------------------------------------------------------
# column_types.txt holds one `table.column<TAB>type` line per column of the
# target PostgreSQL schema (generate it with tools/schema_from_ddl.py from
# `pg_dump --schema-only` or the EF model's create script). A054 uses it to
# reproduce the implicit conversions SQL Server applied silently and
# PostgreSQL refuses (integer = text, LIKE on a number, COALESCE(int, '')).
# Like boolean_columns.txt it is optional: without it A054 changes nothing.
# Lookup order: MSSQLTOPG_COLUMN_TYPES, next to the executable, next to app.py.

TYPES_FILE_NAME = "column_types.txt"
TYPES_ENV_VAR = "MSSQLTOPG_COLUMN_TYPES"
PLACEHOLDER_ENV_VAR = "MSSQLTOPG_DB_PLACEHOLDER"

_types_cache: tuple[str | None, dict[str, dict[str, str]]] | None = None

_TYPE_FAMILY = (
    (("smallint", "integer", "int", "int2", "int4", "int8", "bigint", "serial", "bigserial"), "integer"),
    (("numeric", "decimal", "real", "double precision", "float4", "float8", "money"), "numeric"),
    (("boolean", "bool"), "boolean"),
    (("timestamp", "timestamptz", "timestamp with time zone", "timestamp without time zone"), "timestamp"),
    (("date",), "date"),
    (("time", "time without time zone", "timetz"), "time"),
    (("uuid",), "uuid"),
    (("bytea",), "bytea"),
    (("text", "citext", "character varying", "varchar", "character", "char", "bpchar", "name", "json", "jsonb", "xml"), "text"),
)


def type_family(pg_type: str) -> str:
    """integer | numeric | boolean | timestamp | date | time | uuid | bytea | text | other."""
    base = pg_type.strip().lower().split("(")[0].strip()
    for names, family in _TYPE_FAMILY:
        if base in names:
            return family
    return "other"


def _types_candidates() -> list[Path]:
    paths: list[Path] = []
    env = os.environ.get(TYPES_ENV_VAR)
    if env:
        paths.append(Path(env))
    if getattr(sys, "frozen", False):
        paths.append(Path(sys.executable).resolve().parent / TYPES_FILE_NAME)
    paths.append(Path(__file__).resolve().parent.parent / TYPES_FILE_NAME)
    return paths


def parse_types(text: str) -> dict[str, dict[str, str]]:
    tables: dict[str, dict[str, str]] = {}
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or "." not in line.split()[0]:
            continue
        parts = line.split(None, 1)
        if len(parts) != 2:
            continue
        table, _, column = parts[0].lower().rpartition(".")
        if table and column:
            tables.setdefault(table, {})[column] = type_family(parts[1])
    return tables


def column_types() -> dict[str, dict[str, str]]:
    """{table: {column: type family}}, lower-cased; empty when no file is found."""
    global _types_cache
    for path in _types_candidates():
        key = str(path)
        if _types_cache is not None and _types_cache[0] == key:
            return _types_cache[1]
        try:
            if path.is_file():
                types = parse_types(path.read_text(encoding="utf-8-sig", errors="replace"))
                _types_cache = (key, types)
                return types
        except OSError:
            continue
    _types_cache = (None, {})
    return _types_cache[1]


def types_source() -> str | None:
    """Which file the column types came from (None = none loaded)."""
    column_types()
    return _types_cache[0] if _types_cache else None


def db_placeholder_policy() -> str:
    """How A010 treats `[@MasterdbName].[dbo].T`-style runtime placeholders.

    "drop"   (default, v2.16.0) -- one PostgreSQL database: the prefix is removed
             and the table is reached unqualified, like A009 does for `db.dbo.`.
    "schema" -- keep it as a quoted schema `"@MasterdbName".T` for a target where
             the substituted value names a PostgreSQL schema (pre-v2.16.0 output).
    """
    value = (os.environ.get(PLACEHOLDER_ENV_VAR) or "drop").strip().lower()
    return "schema" if value in ("schema", "keep") else "drop"
