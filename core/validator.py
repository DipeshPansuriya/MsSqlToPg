"""PostgreSQL syntax validation using the real PostgreSQL grammar.

This is qualitatively different from core/linter.py:

  linter    -- a BLOCKLIST of defects someone thought to write a rule for.
               Blind to anything it has no rule for.
  validator -- an ALLOWLIST defined by PostgreSQL itself. `pglast` embeds
               libpg_query, the actual C parser the server uses, so a query it
               accepts is guaranteed to be syntactically valid PostgreSQL.

Only the second can catch unknown unknowns.

WHAT IT CANNOT CATCH -- valid syntax with wrong semantics:
    CAST(x AS BIT)        parses: BIT is a real PG type, just the wrong one
    SELECT a + ' - ' + b  parses: fails at RUNTIME with "operator text + text"
    SELECT ISNULL(a, 0)   parses: fails at RUNTIME, function does not exist
Those need PREPARE against a live database, or a result-set comparison.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

try:
    import pglast
    from pglast.parser import ParseError
    AVAILABLE = True
except ImportError:  # pragma: no cover - pglast is a pinned dependency
    pglast = None
    ParseError = Exception
    AVAILABLE = False

# pglast reports "syntax error at or near "MAX", at index 26"
_INDEX_RE = re.compile(r"at index (\d+)")

# A Dapper placeholder: @Name, not preceded by another @ (T-SQL @@system vars),
# a word character (email@host inside an identifier) or a $ (already masked).
_DAPPER_PARAM_RE = re.compile(r"(?<![\w@$])@[A-Za-z_][A-Za-z0-9_]*")


def _mask_dapper_parameters(sql: str) -> str:
    """Rewrite Dapper `@Name` placeholders to PostgreSQL `$1` for validation only.

    Npgsql accepts `@Name` and it must survive byte-for-byte in the emitted SQL
    (see restore_dapper_parameters), so this masking is confined to the grammar
    check and never touches the converted output.

    It is required because libpg_query accepts `@Name` only by accident: in an
    expression position `@` parses as PostgreSQL's prefix absolute-value
    operator, so `WHERE b = @Foo` reads as `@(Foo)` and validates. In the
    restricted positions of the grammar no prefix operator is allowed, and the
    same placeholder is a hard syntax error:

        FETCH NEXT @TopRecords ROWS ONLY   -> syntax error at or near "@"
        FETCH NEXT $1          ROWS ONLY   -> valid

    That single position was enough to fail every paginated query, which in
    practice meant nearly all of them, and a failed validation makes the whole
    conversion "needs review" so the source is left as T-SQL.

    Every placeholder becomes `$1` regardless of name: the parser only checks
    grammar, so the numbers need not be distinct, and a constant two-character
    replacement always fits inside the shortest possible `@x`. The remainder is
    padded with spaces so the masked text is the same length as the original and
    reported error offsets still point at the right character.

    Literals and comments are skipped -- an `@` inside 'user@example.com' or a
    commented-out block is not a placeholder.
    """
    out: list[str] = []
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]

        # Single-quoted string literal ('' escapes an embedded quote)
        if ch == "'":
            j = i + 1
            while j < n:
                if sql[j] == "'":
                    if j + 1 < n and sql[j + 1] == "'":
                        j += 2
                        continue
                    j += 1
                    break
                j += 1
            out.append(sql[i:j])
            i = j
            continue

        # Double-quoted identifier ("" escapes an embedded quote)
        if ch == '"':
            j = i + 1
            while j < n:
                if sql[j] == '"':
                    if j + 1 < n and sql[j + 1] == '"':
                        j += 2
                        continue
                    j += 1
                    break
                j += 1
            out.append(sql[i:j])
            i = j
            continue

        # Line comment
        if sql.startswith("--", i):
            j = sql.find("\n", i)
            j = n if j == -1 else j
            out.append(sql[i:j])
            i = j
            continue

        # Block comment (PostgreSQL nests them)
        if sql.startswith("/*", i):
            depth, j = 1, i + 2
            while j < n and depth:
                if sql.startswith("/*", j):
                    depth, j = depth + 1, j + 2
                elif sql.startswith("*/", j):
                    depth, j = depth - 1, j + 2
                else:
                    j += 1
            out.append(sql[i:j])
            i = j
            continue

        # Placeholder
        match = _DAPPER_PARAM_RE.match(sql, i)
        if match:
            token = match.group(0)
            out.append("$1".ljust(len(token)))
            i = match.end()
            continue

        out.append(ch)
        i += 1

    return _mask_dapper_in_list("".join(out))


# `IN @Ids` is Dapper's LIST-EXPANSION syntax, not SQL. Dapper rewrites it to
# `IN (@Ids1, @Ids2, ...)` before execution, so the stored text is not valid on
# ANY database -- and after `@Ids` is masked to `$1` the grammar still rejects
# `IN $1`, because IN requires a parenthesised list or a subquery.
#
# Left unmasked this failed every query using the idiom (138 occurrences across
# 26 files in one codebase), and a failed validation makes the whole conversion "needs
# review", so the source stayed T-SQL. The emitted SQL is untouched: `IN @Ids`
# must survive byte-for-byte for Dapper to expand it.
_IN_LIST_RE = re.compile(r"\bIN\s+\$1[ \t]*", re.IGNORECASE)


def _mask_dapper_in_list(masked: str) -> str:
    """Turn `IN $1` into `IN ($1)` so the grammar check accepts it.

    Padded back to the original width where the placeholder was long enough
    (`@Ids` and up, which is effectively all of them) so reported offsets keep
    pointing at the right character. A shorter name simply shifts later columns
    by a character or two in the error message -- the verdict stays correct.
    """
    def _replace(match: "re.Match[str]") -> str:
        return "IN ($1)".ljust(len(match.group(0)))

    return _IN_LIST_RE.sub(_replace, masked)


@dataclass
class Validation:
    valid: bool = True
    checked: bool = True          # False when pglast is unavailable
    errors: list[str] = field(default_factory=list)

    @property
    def verdict(self) -> str:
        if not self.checked:
            return "NOT CHECKED"
        return "VALID" if self.valid else "INVALID"


def _position(sql: str, index: int) -> tuple[int, int]:
    """Convert a character offset to (line, column), both 1-based."""
    prefix = sql[:index]
    line = prefix.count("\n") + 1
    column = index - (prefix.rfind("\n") + 1) + 1
    return line, column


def validate(sql: str) -> Validation:
    """Parse `sql` with the real PostgreSQL grammar. Never raises."""
    if not AVAILABLE:
        return Validation(valid=True, checked=False,
                          errors=["pglast is not installed -- syntax not verified"])
    if not sql.strip():
        return Validation(valid=False, errors=["No SQL to validate."])

    # Dapper placeholders are masked to $1 for the grammar check only; the
    # masking is length-preserving, so offsets below still index into `sql`.
    masked = _mask_dapper_parameters(sql)

    try:
        pglast.parse_sql(masked)   # the actual grammar check
        return Validation(valid=True)
    except ParseError as exc:
        message = str(exc).strip()
        match = _INDEX_RE.search(message)
        if match:
            line, column = _position(sql, int(match.group(1)))
            message = _INDEX_RE.sub(f"at line {line}, column {column}", message)
        return Validation(valid=False, errors=[message])
    except Exception as exc:  # never let a validator crash a conversion
        return Validation(valid=False,
                          errors=[f"{type(exc).__name__}: {exc}"])
