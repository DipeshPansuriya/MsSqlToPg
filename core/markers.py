"""The T-SQL marker set -- ONE definition shared by every layer (v2.13.0).

Three places need to answer "is there still T-SQL in this text?":

    R007      -- the gate on converted output (orchestrator)
    fragments -- C# literals that are not whole statements (core/csharp.py)
    --audit   -- the repo-wide inventory of what is left (core/audit.py)

Before v2.13.0 each had its own list and they disagreed: the output gate knew
nothing about CURSOR, @@ROWCOUNT, OUTPUT INSERTED, OBJECT_ID, SET NOCOUNT,
OPENXML/.nodes(), SWITCHOFFSET, QUOTENAME, DATETIME2/TINYINT or an unquoted
DATEADD unit -- all of which the campaign scanner (scripts/pg-conversion/
tsqlscan.py) was finding in constants the converter had passed. This is that
scanner's list, promoted to the single source of truth.

Matching rules (each learned the hard way):
  * SQL comments are stripped first -- converted files carry explanatory
    comments ("#master is a session temp table") that are not code.
  * String-literal CONTENTS are blanked, except for the N'' check.
  * $$ ... $$ PL/pgSQL bodies are removed before the control-flow checks.
  * PostgreSQL's own `DROP TABLE IF EXISTS` / `ADD COLUMN IF NOT EXISTS` /
    `END IF` are not T-SQL IF blocks.
"""

from __future__ import annotations

import re

# ---- blockers that need a whole-constant rewrite ---------------------------
HARD = {
    "IF/BEGIN control flow": r"__TSQL_IF__|\bBEGIN\s+TRAN|\bELSE\s+IF\b|\bELSE\s+BEGIN\b",
    "DECLARE @var": r"\bDECLARE\s+@",
    "SET @var": r"\bSET\s+@\w+\s*=",
    "WHILE loop": r"\bWHILE\b",
    "#temp table": r"(\bINTO|\bFROM|\bJOIN|\bTABLE|\bUPDATE|\bINSERT\s+INTO|\bINSERT)\s+#\w+",
    # `\bEXEC\s*\w` (the campaign scanner's form) also matched ExecutedDate /
    # ExecutedFlag column names -- EXEC must be a whole word here.
    "dynamic SQL / EXEC": r"\bsp_executesql\b|\bEXEC(?:UTE)?\s*\(|\bEXEC(?:UTE)?\s+(?:@|sp_|usp|dbo\.|\[?\w)",
    "TRY/CATCH": r"\bBEGIN\s+TRY\b|\bBEGIN\s+CATCH\b",
    "XML (OPENXML/FOR XML/.nodes)": r"\bOPENXML\b|\bFOR\s+XML\b|sp_xml_\w+|\.nodes\s*\(",
    "CURSOR": r"\bCURSOR\s+(LOCAL\s+|FAST_FORWARD\s+|STATIC\s+|READ_ONLY\s+)*FOR\b|\bFETCH\s+NEXT\s+FROM\b|@@FETCH_STATUS",
    "OPENJSON / FOR JSON": r"\bOPENJSON\b|\bFOR\s+JSON\b",
    "@@ variables / OUTPUT": (r"@@(ROWCOUNT|IDENTITY|ERROR|TRANCOUNT)|\$\$(ROWCOUNT|IDENTITY|ERROR|TRANCOUNT)\b"
                              r"|\bSCOPE_IDENTITY\s*\(|\bOUTPUT\s+(INSERTED|DELETED)\."
                              r"|\bRETURNING\s+(INSERTED|DELETED)\."),
    "sys.* catalog": r"\bsys\.\w+|\bOBJECT_ID\s*\(",
}

# ---- mechanical-ish, but still fails on PostgreSQL -------------------------
SOFT = {
    "SELECT TOP": r"\bSELECT\s+(DISTINCT\s+)?TOP\s*\(?\s*[\d@]",
    "ISNULL(": r"\bISNULL\s*\(",
    "WITH(NOLOCK)": r"\bWITH\s*\(\s*NOLOCK\s*\)|\(\s*NOLOCK\s*\)",
    "dbo.": r"\bdbo\s*\.",
    "CROSS/OUTER APPLY": r"\b(CROSS|OUTER)\s+APPLY\b",
    "STUFF(": r"\bSTUFF\s*\(",
    "CONVERT(": (r"(?<![\w.])CONVERT\s*\(\s*(N?VARCHAR|VARCHAR|NVARCHAR|INT|BIT|DATE|DATETIME|"
                 r"DECIMAL|NUMERIC|BIGINT|CHAR|FLOAT|TIME|MONEY|UNIQUEIDENTIFIER)\b"),
    "DATEADD/DATEDIFF unquoted unit": r"\bDATE(ADD|DIFF)\s*\(\s*(?!')\w+\s*,",
    "DATEPART/DATENAME/EOMONTH": r"\b(DATEPART|DATENAME|EOMONTH|DATEFROMPARTS)\s*\(",
    "LEN(": r"(?<![\w.])LEN\s*\(",
    "IIF(": r"(?<![\w.])IIF\s*\(",
    "TRY_CAST/TRY_CONVERT": r"\bTRY_(CAST|CONVERT)\s*\(",
    "SYSDATETIME/SWITCHOFFSET": (r"\b(SYSDATETIME|SYSDATETIMEOFFSET|SWITCHOFFSET|TODATETIMEOFFSET)\s*\("
                                 r"|\bAT\s+TIME\s+ZONE\s+'(India|Arabian|W\.|E\.|Central|Pacific|Eastern|"
                                 r"Singapore|UTC\b[^']*Standard)"),
    "REPLICATE/SPACE/QUOTENAME": r"\b(REPLICATE|QUOTENAME)\s*\(|(?<![\w.])SPACE\s*\(",
    "N'unicode literal": r"(?<![\w'])N'",
    "[bracket] identifiers": r"\[(dbo|[A-Z][A-Za-z_]*(ID|Id|Name|No|Date|Code))\]",
    "NVARCHAR/UNIQUEIDENTIFIER/DATETIME types": (r"\b(NVARCHAR|UNIQUEIDENTIFIER|SMALLDATETIME|TINYINT|UTINYINT|"
                                                 r"DATETIME2|DATETIMEOFFSET|SMALLMONEY|SQL_VARIANT)\b"
                                                 r"|\bAS\s+DATETIME\s*\)|::\s*DATETIME\b"),
    "SET NOCOUNT": r"\bSET\s+NOCOUNT\b",
    "string + concat": r"'\s*\+\s*(?![\d@])\w|\w\s*\+\s*'",
}

_CASE_SENSITIVE = {"[bracket] identifiers", "N'unicode literal"}
HARD_RES = {k: re.compile(v, re.I | re.S) for k, v in HARD.items()}
SOFT_RES = {k: (re.compile(v, re.S) if k in _CASE_SENSITIVE else re.compile(v, re.I | re.S))
            for k, v in SOFT.items()}

_DOLLAR = re.compile(r"(\$\w*\$)[\s\S]*?\1")
_PG_IF = re.compile(
    r"\b(TABLE|INDEX|COLUMN|SCHEMA|VIEW|SEQUENCE|TYPE|CONSTRAINT|FUNCTION|TRIGGER|"
    r"EXTENSION|PROCEDURE)\s+IF\s+(NOT\s+)?EXISTS\b|\bEND\s+IF\b", re.I)
_TSQL_IF = re.compile(r"\bIF\b\s*(\(|NOT\b|EXISTS\b|@|\w)", re.I)
_INTERVAL = re.compile(r"\bINTERVAL\s*'[^']*'|'[^']*'\s*::\s*interval\b", re.I)
# v2.16.0: DATE '1900-01-01' + time / TIMESTAMP '...' - x are typed literals, not text + text.
_TYPED_LITERAL = re.compile(r"\b(?:DATE|TIME|TIMESTAMP|TIMESTAMPTZ)\s+'[^']*'", re.I)
_STRING = re.compile(r"'(?:[^']|'')*'")


def strip_comments(sql: str) -> str:
    """Drop -- and /* */ comments, respecting string literals."""
    out, i, n = [], 0, len(sql)
    in_str = False
    while i < n:
        c = sql[i]
        if in_str:
            out.append(c)
            if c == "'":
                in_str = False
            i += 1
            continue
        if c == "'":
            in_str = True
            out.append(c)
            i += 1
            continue
        if sql.startswith("--", i):
            j = sql.find("\n", i)
            i = n if j < 0 else j
            continue
        if sql.startswith("/*", i):
            j = sql.find("*/", i + 2)
            i = n if j < 0 else j + 2
            out.append(" ")
            continue
        out.append(c)
        i += 1
    return "".join(out)


def blank_strings(sql: str) -> str:
    return _STRING.sub("''", sql)


def _control_view(sql: str) -> str:
    sql = _DOLLAR.sub(" ", sql)
    sql = _PG_IF.sub(" ", sql)
    return _TSQL_IF.sub(" __TSQL_IF__ ", sql)


def find(sql: str) -> tuple[list[str], list[str]]:
    """(hard, soft) marker labels present in `sql`, in a stable order."""
    if not sql or not sql.strip():
        return [], []
    no_comments = strip_comments(sql)
    if not no_comments.strip():
        return [], []
    blanked = blank_strings(no_comments)
    control = _control_view(blanked)
    hard = [k for k, rx in HARD_RES.items() if rx.search(control)]
    soft = []
    no_dollar = _DOLLAR.sub(" ", blanked)
    for k, rx in SOFT_RES.items():
        if k == "N'unicode literal":
            # token-aware: N must PREFIX a literal. A regex over raw text also
            # matched the CLOSING quote of a string ending in " N" ('Y / N').
            text = _DOLLAR.sub(" ", no_comments)
            if any(s.start() >= 1 and text[s.start() - 1] in "Nn"
                   and (s.start() < 2 or not (text[s.start() - 2].isalnum() or text[s.start() - 2] == "_"))
                   for s in _STRING.finditer(text)):
                soft.append(k)
            continue
        if k == "string + concat":
            target = _TYPED_LITERAL.sub("TL", _INTERVAL.sub("IV", _DOLLAR.sub(" ", no_comments)))
            target = blank_strings(target)
        else:
            target = no_dollar
        if rx.search(target):
            soft.append(k)
    soft.sort(key=list(SOFT).index)
    return hard, soft


def labels(sql: str) -> list[str]:
    hard, soft = find(sql)
    return hard + soft
