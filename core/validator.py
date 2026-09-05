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

    try:
        pglast.parse_sql(sql)      # the actual grammar check
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
