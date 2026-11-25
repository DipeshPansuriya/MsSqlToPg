"""Safe conversion support for interpolated C# SQL strings ($"...", $@"...").

The scan previously refused every interpolated string: `{expr}` holes are
dynamic and cannot be transpiled. This module makes the SAFE subset convertible
by masking each hole with an opaque SQL identifier, converting the SQL around
it, and restoring the holes afterwards.

REFUSAL CASES (return None -- caller reports "convert by hand"):
- `{{` or `}}` escape sequences (literal braces; unescaping and re-escaping
  through the rewrite round-trip is not worth the risk)
- a hole containing a quote character (nested strings / ternaries -- brace
  matching inside them needs a full C# expression parser)
- a hole containing braces (nested interpolation)
- unbalanced braces
- after conversion: any hole sentinel missing or duplicated (the converter
  moved or dropped it -- restoring would corrupt the query)
"""

from __future__ import annotations

import re

_SENTINEL = "MSPGHOLE{n}Z"
_SENTINEL_RE = re.compile(r"MSPGHOLE(\d+)Z")


def mask_holes(sql: str) -> tuple[str, list[str]] | None:
    """Replace every `{expr}` hole with an opaque identifier.

    Returns (masked_sql, holes) where holes[i] is the original `{expr}` text
    for sentinel i, or None when the string is not safely maskable.
    """
    if "{{" in sql or "}}" in sql:
        return None
    if _SENTINEL_RE.search(sql):
        return None  # sentinel collision -- astronomically unlikely, refuse

    out: list[str] = []
    holes: list[str] = []
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        if ch == "}":
            return None  # unbalanced
        if ch != "{":
            out.append(ch)
            i += 1
            continue
        j = i + 1
        while j < n and sql[j] not in "}{'\"":
            j += 1
        if j >= n or sql[j] != "}":
            return None  # nested brace, quote inside hole, or unterminated
        holes.append(sql[i:j + 1])
        out.append(_SENTINEL.format(n=len(holes) - 1))
        i = j + 1
    # A $-string with zero holes is fine: masked == original, restore is a no-op.
    return "".join(out), holes


def restore_holes(sql: str, holes: list[str]) -> str | None:
    """Put the original `{expr}` holes back. None if any sentinel is missing
    or appears more than once (conversion reshaped the query around it)."""
    for index in range(len(holes)):
        if sql.count(_SENTINEL.format(n=index)) != 1:
            return None
    extra = [m for m in _SENTINEL_RE.finditer(sql)
             if int(m.group(1)) >= len(holes)]
    if extra:
        return None
    return _SENTINEL_RE.sub(lambda m: holes[int(m.group(1))], sql)
