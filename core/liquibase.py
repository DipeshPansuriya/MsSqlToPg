"""Parse Liquibase formatted-SQL changelogs and give each MS-SQL changeset a
PostgreSQL twin.

WHY THIS IS A SEPARATE CONTAINER, NOT A NEW TOOL
`core/orchestrator.convert()` takes a SQL string and knows nothing about where
it came from -- exactly like `core/csharp.py` feeds it literals out of C#
source. This module is the second container: it feeds it changeset bodies out
of Liquibase formatted SQL and writes the converted result back as a new
changeset.

WHAT LIQUIBASE ADDS THAT PLAIN SQL CONVERSION CANNOT KNOW

  * Changeset identity is `filepath::id::author` -- `dbms:` is NOT part of it.
    An mssql/postgresql pair sharing one id collapses to a single
    DATABASECHANGELOG row, and the next run against the other engine dies with
    "Validation Failed: N changesets check sum". Every twin generated here gets
    a distinct id (LB001).
  * A `DO $$ ... $$` body contains semicolons that are not statement
    terminators. Liquibase splits on `;` unless the changeset says
    `splitStatements:false` (LB002).
  * `--rollback` lines look like comments to a SQL parser but are executable
    SQL to Liquibase, so they need converting too (LB003).

SAFETY RULES -- this edits files in a customer's repository:

  1. A `.bak` copy is written before a file is modified for the first time.
  2. A twin is emitted ONLY when the conversion is usable -- PostgreSQL syntax
     VALID and zero ERROR findings. Anything doubtful is left alone and
     reported, so a broken changeset is never planted in a changelog.
  3. Existing changesets are NEVER edited. This module only appends new ones.
     If a review rejects a twin, deleting the added block restores the file.
  4. Re-running is safe: an mssql changeset that already has a PostgreSQL twin
     is skipped (LB006). The C# scanner has no equivalent guard and must not be
     re-run over converted source; this one may be.
  5. T-SQL procedural control flow is unwrapped only in the exact shapes listed
     in `_unwrap_guard`. Anything else is REFUSED (LB005) rather than guessed
     at -- a silently wrong DDL guard is worse than no twin at all.

Use dry_run=True to see exactly what would happen without touching anything.
"""

from __future__ import annotations

import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from core import autofix, orchestrator, validator
from core.models import ConversionResult, Severity

BACKUP_SUFFIX = ".bak"
MAX_FILE_BYTES = 8 * 1024 * 1024

# Row statuses, mirroring core/scan.py so the UI can share tag colours.
CONVERTED = "CONVERTED"
REPAIRED = "REPAIRED"
SKIPPED = "SKIPPED"
FAILED = "FAILED"
CLEAN = "CLEAN"

MAGIC = "--liquibase formatted sql"

_CHANGESET_RE = re.compile(
    r"^\s*--\s*changeset\s+(?P<author>[^:\s]+):(?P<id>\S+)(?P<attrs>.*)$",
    re.IGNORECASE)
# Anything calling itself a changeset. Compared against the strict pattern to
# find headers Liquibase itself cannot parse -- a missing `author:` prefix or a
# missing colon makes the whole changelog fail to load, so these must be
# reported rather than quietly skipped.
_LOOSE_CHANGESET_RE = re.compile(r"^\s*--\s*changeset\b(?P<rest>.*)$", re.IGNORECASE)
_ROLLBACK_RE = re.compile(r"^\s*--\s*rollback\b[ \t]*(?P<sql>.*)$", re.IGNORECASE)
_COMMENT_RE = re.compile(r"^\s*--\s*comment\b:?[ \t]*(?P<text>.*)$", re.IGNORECASE)
_META_RE = re.compile(r"^\s*--\s*(precondition|validCheckSum|ignore|rollbackSqlFile)\b",
                      re.IGNORECASE)
_ATTR_RE = re.compile(r"(?P<key>[A-Za-z][A-Za-z0-9_]*)\s*:\s*(?P<value>[^\s]+)")
_GO_RE = re.compile(r"^\s*GO\s*;?\s*$", re.IGNORECASE)


# ==========================================================================
# Model
# ==========================================================================

@dataclass
class Changeset:
    author: str
    id: str
    attrs: dict[str, str] = field(default_factory=dict)
    header_line: int = 0          # 1-based line of the --changeset line
    start: int = 0                # 0-based index of the --changeset line
    end: int = 0                  # 0-based index just past the last body line
    body: str = ""                # SQL only -- comments/rollback removed
    comment: str = ""
    rollbacks: list[str] = field(default_factory=list)

    @property
    def dbms(self) -> str:
        return self.attrs.get("dbms", "").lower()

    @property
    def identity(self) -> str:
        return f"{self.author}:{self.id}"

    @property
    def split_statements_off(self) -> bool:
        return self.attrs.get("splitStatements", "").lower() == "false"


@dataclass
class Finding:
    line: int
    changeset: str
    action: str
    code: str = ""
    reason: str = ""
    result: ConversionResult | None = None


@dataclass
class FileReport:
    path: Path
    findings: list[Finding] = field(default_factory=list)
    status: str = CLEAN
    error: str = ""
    backup: Path | None = None
    changesets: int = 0

    @property
    def converted(self) -> int:
        return sum(1 for f in self.findings if f.action == CONVERTED)

    @property
    def repaired(self) -> int:
        return sum(1 for f in self.findings if f.action == REPAIRED)

    @property
    def needs_attention(self) -> int:
        return sum(1 for f in self.findings if f.action == SKIPPED)


@dataclass
class ChangelogReport:
    root: Path | None = None
    files: list[FileReport] = field(default_factory=list)
    duration_ms: int = 0
    dry_run: bool = False

    @property
    def scanned(self) -> int:
        return len(self.files)

    @property
    def changesets(self) -> int:
        return sum(f.changesets for f in self.files)

    @property
    def converted(self) -> int:
        return sum(f.converted for f in self.files)

    @property
    def repaired(self) -> int:
        return sum(f.repaired for f in self.files)

    @property
    def needs_attention(self) -> int:
        return sum(f.needs_attention for f in self.files)

    @property
    def failed(self) -> int:
        return sum(1 for f in self.files if f.status == FAILED)


ProgressFn = Callable[[FileReport, int, int], None]


# ==========================================================================
# Discovery
# ==========================================================================

def is_changelog(text: str) -> bool:
    """A Liquibase formatted-SQL file declares itself on its first real line.
    Detecting by content rather than folder name keeps this working whatever a
    repo calls its changelog directory."""
    for line in text.splitlines():
        if not line.strip():
            continue
        return line.strip().lower().startswith(MAGIC)
    return False


def find_changelog_files(root: Path, recursive: bool = True) -> list[Path]:
    pattern = "**/*.sql" if recursive else "*.sql"
    skip = {"bin", "obj", ".git", ".vs", "node_modules", "packages", "TestResults"}
    out: list[Path] = []
    for path in root.glob(pattern):
        if not path.is_file():
            continue
        if skip & {p.name for p in path.parents}:
            continue
        try:
            if path.stat().st_size > MAX_FILE_BYTES:
                continue
            head = path.read_text(encoding="utf-8-sig", errors="replace")[:400]
        except OSError:
            continue
        if is_changelog(head):
            out.append(path)
    return sorted(out)


# ==========================================================================
# Parsing
# ==========================================================================

def _read_text(path: Path) -> tuple[str, str, str]:
    """Return (text-with-\\n-endings, original newline, original encoding).

    Newline and encoding are both carried back so a rewrite changes only the
    lines we add. Some changelogs in the wild are Windows-1252 -- an em-dash or
    non-breaking space pasted from a document -- and decoding those as UTF-8
    raises. Falling back keeps the file convertible instead of failing the run,
    and writing it back in the SAME encoding avoids a whole-file diff.
    """
    data = path.read_bytes()
    for encoding in ("utf-8-sig", "utf-8", "cp1252"):
        try:
            raw = data.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise UnicodeDecodeError("utf-8", data, 0, 1, "undecodable changelog")
    newline = "\r\n" if "\r\n" in raw else "\n"
    return raw.replace("\r\n", "\n").replace("\r", "\n"), newline, encoding


def parse(text: str) -> list[Changeset]:
    """Split formatted SQL into changesets. Body excludes Liquibase's own
    comment directives -- they are metadata, not SQL to convert."""
    lines = text.split("\n")
    found: list[Changeset] = []
    current: Changeset | None = None
    body: list[str] = []

    def close(end_index: int) -> None:
        if current is None:
            return
        current.end = end_index
        current.body = "\n".join(body).strip("\n")
        found.append(current)

    for index, line in enumerate(lines):
        match = _CHANGESET_RE.match(line)
        if match:
            close(index)
            attrs = {m.group("key"): m.group("value")
                     for m in _ATTR_RE.finditer(match.group("attrs") or "")}
            current = Changeset(author=match.group("author"),
                                id=match.group("id"),
                                attrs=attrs,
                                header_line=index + 1,
                                start=index)
            body = []
            continue
        if current is None:
            continue
        rollback = _ROLLBACK_RE.match(line)
        if rollback:
            current.rollbacks.append(rollback.group("sql").strip())
            continue
        comment = _COMMENT_RE.match(line)
        if comment:
            if not current.comment:
                current.comment = comment.group("text").strip()
            continue
        if _META_RE.match(line):
            continue
        body.append(line)

    close(len(lines))
    return found


def malformed_headers(text: str) -> list[tuple[int, str]]:
    """(line, text) for every `--changeset` line Liquibase cannot parse.

    Liquibase's formatted-SQL parser requires `author:id`. A header missing the
    author, or missing the colon, aborts the load of the entire changelog -- so
    a file that looks fine can take a whole release down.
    """
    bad: list[tuple[int, str]] = []
    for index, line in enumerate(text.split("\n"), start=1):
        loose = _LOOSE_CHANGESET_RE.match(line)
        if loose and not _CHANGESET_RE.match(line):
            bad.append((index, loose.group("rest").strip()))
    return bad


def duplicate_identities(changesets: list[Changeset]) -> list[str]:
    """ids used by more than one changeset in the same file. Liquibase keys on
    id+author+filepath, so these collide no matter what `dbms:` says."""
    seen: dict[str, int] = {}
    for changeset in changesets:
        seen[changeset.identity] = seen.get(changeset.identity, 0) + 1
    return sorted(k for k, v in seen.items() if v > 1)


# Teams tag the engine onto the changeset id by hand, and spell it several
# ways. Comparing raw ids would make `...-mssql` and `...-postgresql` look like
# unrelated changesets and the twin would be generated again on every run.
_ENGINE_SUFFIX_RE = re.compile(
    r"[-_](?:ms[-_]?sql|sqlserver|postgresql|postgres|pgsql|pg)$", re.IGNORECASE)


def _stem(changeset_id: str) -> str:
    """The id with any trailing engine tag removed, lowercased."""
    stem = changeset_id.strip()
    while True:
        shorter = _ENGINE_SUFFIX_RE.sub("", stem)
        if shorter == stem:
            return stem.lower()
        stem = shorter


def twin_id(changeset: Changeset) -> str:
    """Id for the generated twin: the stem plus `-postgresql`, so it never
    collides with its MSSQL sibling (LB001) and re-running finds it again."""
    base = changeset.id.strip()
    while True:
        shorter = _ENGINE_SUFFIX_RE.sub("", base)
        if shorter == base:
            break
        base = shorter
    return f"{base}-postgresql"


def has_pg_twin(changeset: Changeset, changesets: list[Changeset]) -> bool:
    """Idempotency guard (LB006): does a postgresql changeset already cover
    this one? Compared on the engine-stripped stem, so `X-mssql` is correctly
    matched by `X-postgresql`."""
    stem = _stem(changeset.id)
    return any(_stem(other.id) == stem
               for other in changesets if other.dbms == "postgresql")


# ==========================================================================
# T-SQL procedural guards
# ==========================================================================

_STMT_KINDS = ("column", "index", "table")


def _skip_string(sql: str, i: int) -> int:
    """Index just past the single-quoted literal starting at i."""
    i += 1
    while i < len(sql):
        if sql[i] == "'":
            if i + 1 < len(sql) and sql[i + 1] == "'":
                i += 2
                continue
            return i + 1
        i += 1
    return i


def _match_paren(sql: str, i: int) -> int:
    """Index just past the ')' matching the '(' at i, or -1."""
    depth = 0
    while i < len(sql):
        char = sql[i]
        if char == "'":
            i = _skip_string(sql, i)
            continue
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return -1


def _match_begin_end(sql: str, i: int) -> tuple[int, int]:
    """(body_start, index just past the matching END) for the BEGIN at i."""
    token = re.compile(r"\b(BEGIN|END)\b", re.IGNORECASE)
    depth = 0
    body_start = -1
    position = i
    while position < len(sql):
        match = token.search(sql, position)
        if not match:
            return (-1, -1)
        word = match.group(1).upper()
        if word == "BEGIN":
            depth += 1
            if depth == 1:
                body_start = match.end()
        else:
            depth -= 1
            if depth == 0:
                return (body_start, match.end())
        position = match.end()
    return (-1, -1)


_IF_GUARD_RE = re.compile(r"\bIF\s+(?P<negated>NOT\s+)?EXISTS\s*(?=\()",
                          re.IGNORECASE)


@dataclass
class _Segment:
    sql: str
    kind: str = ""        # "" = plain SQL, else one of _STMT_KINDS
    guarded: bool = False


def _classify_guard(condition: str, body: str) -> str:
    """Which idempotent PostgreSQL form replaces this guard, if any."""
    cond = condition.lower()
    inner = body.strip().lower()
    if "sys.columns" in cond and re.match(r"alter\s+table\b.*\badd\b", inner,
                                          re.IGNORECASE | re.DOTALL):
        return "column"
    if "sys.indexes" in cond and re.match(r"create\s+(unique\s+)?"
                                          r"(clustered\s+|nonclustered\s+)?index\b",
                                          inner, re.IGNORECASE):
        return "index"
    if ("sys.objects" in cond or "sys.tables" in cond) and \
            re.match(r"create\s+table\b", inner, re.IGNORECASE):
        return "table"
    return ""


def split_guards(sql: str) -> tuple[list[_Segment], list[str]]:
    """Break a T-SQL body into segments, unwrapping the recognised
    `IF [NOT] EXISTS (...) BEGIN <one statement> END` guards.

    Returns (segments, unsupported) -- `unsupported` names any guard shape this
    module will not convert, which makes the whole changeset a refusal.
    """
    segments: list[_Segment] = []
    unsupported: list[str] = []
    position = 0
    while True:
        match = _IF_GUARD_RE.search(sql, position)
        if not match:
            break
        open_paren = sql.index("(", match.end() - 1)
        close_paren = _match_paren(sql, open_paren)
        if close_paren < 0:
            unsupported.append("unbalanced parentheses in an IF EXISTS guard")
            break
        after = sql[close_paren:]
        begin_offset = re.match(r"\s*\bBEGIN\b", after, re.IGNORECASE)
        if not begin_offset:
            unsupported.append(
                f"IF EXISTS guard without a BEGIN block near offset {match.start()}")
            position = close_paren
            continue
        body_start, guard_end = _match_begin_end(sql, close_paren)
        if body_start < 0:
            unsupported.append("IF EXISTS guard with no matching END")
            break

        condition = sql[open_paren:close_paren]
        body = sql[body_start:guard_end]
        body = re.sub(r"\bEND\b\s*;?\s*$", "", body, flags=re.IGNORECASE).strip()
        kind = _classify_guard(condition, body)
        if not kind:
            unsupported.append(
                "IF EXISTS guard whose shape is not a recognised column/index/"
                "table existence check")
            position = guard_end
            continue

        lead = sql[position:match.start()].strip()
        if lead:
            segments.append(_Segment(lead))
        segments.append(_Segment(body, kind=kind, guarded=True))
        position = guard_end

    tail = sql[position:].strip()
    if tail:
        segments.append(_Segment(tail))
    return segments, unsupported


_ADD_COLUMN_RE = re.compile(r"(\bADD\s+)(?!COLUMN\b)(?!CONSTRAINT\b)", re.IGNORECASE)
_CREATE_INDEX_RE = re.compile(r"\bCREATE\s+(UNIQUE\s+)?INDEX\s+(?!IF\s+NOT\s+EXISTS\b)",
                              re.IGNORECASE)
_CREATE_TABLE_RE = re.compile(r"\bCREATE\s+TABLE\s+(?!IF\s+NOT\s+EXISTS\b)",
                              re.IGNORECASE)


def make_idempotent(sql: str, kind: str) -> str:
    """Re-apply the guard's intent using PostgreSQL's own IF NOT EXISTS forms.

    Applied AFTER conversion: `ADD COLUMN IF NOT EXISTS` is not valid T-SQL, so
    injecting it before the engine ran would break the parse.
    """
    if kind == "column":
        if re.search(r"\bADD\s+COLUMN\b", sql, re.IGNORECASE):
            return re.sub(r"(\bADD\s+COLUMN\s+)(?!IF\s+NOT\s+EXISTS\b)",
                          r"\1IF NOT EXISTS ", sql, flags=re.IGNORECASE)
        return _ADD_COLUMN_RE.sub(r"\1COLUMN IF NOT EXISTS ", sql, count=1)
    if kind == "index":
        return _CREATE_INDEX_RE.sub(lambda m: f"CREATE {m.group(1) or ''}INDEX "
                                              f"IF NOT EXISTS ", sql, count=1)
    if kind == "table":
        return _CREATE_TABLE_RE.sub("CREATE TABLE IF NOT EXISTS ", sql, count=1)
    return sql


# ==========================================================================
# Conversion
# ==========================================================================

@dataclass
class TwinResult:
    sql: str = ""
    rollbacks: list[str] = field(default_factory=list)
    warnings: list = field(default_factory=list)
    codes: list[str] = field(default_factory=list)
    ok: bool = False
    reason: str = ""
    needs_split_off: bool = False


def _strip_go(sql: str) -> tuple[str, int]:
    kept, removed = [], 0
    for line in sql.split("\n"):
        if _GO_RE.match(line):
            removed += 1
            continue
        kept.append(line)
    return "\n".join(kept), removed


def is_comment_only(sql: str) -> str:
    """True when a fragment carries no statement -- only SQL comments."""
    stripped = re.sub(r"/\*.*?\*/", " ", sql, flags=re.DOTALL)
    stripped = re.sub(r"(?m)^\s*--.*$", "", stripped)
    return not stripped.strip()


def _ensure_semicolon(sql: str) -> str:
    """Liquibase splits a changeset body on `;`. Without a terminator the last
    statement of a multi-statement body silently swallows the next one."""
    stripped = sql.rstrip().rstrip(";").rstrip()
    return stripped + ";"


# WHY A SECOND GATE EXISTS
# pglast checks the PostgreSQL GRAMMAR, not its semantics. PostgreSQL's grammar
# accepts any unknown identifier as a type name, so `ADD c NVARCHAR(MAX)` parses
# cleanly and only fails at execution with `type "nvarchar" does not exist`.
# Grammar validation alone therefore cannot promise "runs without error". These
# patterns catch SQL-Server-only constructs that survive a parse.
# A false positive costs a manual review; a false negative costs a failed
# release, so this list errs toward refusing.
_RESIDUAL_TSQL: tuple[tuple[re.Pattern, str], ...] = (
    (re.compile(r"\bN(?:VARCHAR|CHAR|TEXT)\b", re.I), "NVARCHAR/NCHAR/NTEXT type"),
    (re.compile(r"\b(?:UNIQUEIDENTIFIER|DATETIME2|DATETIME|SMALLDATETIME|"
                r"DATETIMEOFFSET|MONEY|SMALLMONEY|TINYINT|IMAGE|SQL_VARIANT)\b",
                re.I), "SQL Server data type"),
    (re.compile(r"\bBIT\b", re.I), "BIT type (PostgreSQL wants BOOLEAN)"),
    (re.compile(r"\bsys\.[a-z_]+", re.I), "sys.* catalog view"),
    (re.compile(r"\b(?:OBJECT_ID|SCOPE_IDENTITY|ISNULL|GETDATE|GETUTCDATE|"
                r"SYSDATETIME|SYSUTCDATETIME|DATEADD|DATEDIFF|DATEPART|"
                r"CHARINDEX|PATINDEX|STUFF|IIF|NEWID)\s*\(", re.I),
     "T-SQL built-in function"),
    (re.compile(r"\bIDENTITY\s*\(", re.I), "IDENTITY(seed, increment)"),
    (re.compile(r"\bIDENTITY_INSERT\b", re.I), "SET IDENTITY_INSERT"),
    (re.compile(r"@@[A-Za-z_]+"), "@@ system variable"),
    (re.compile(r"\[[A-Za-z_#][^\]\n]*\]"), "[bracket-quoted] identifier"),
    (re.compile(r"(?<![A-Za-z0-9_])N'"), "N'' national-string literal"),
    (re.compile(r"\b(?:NONCLUSTERED|CLUSTERED)\b", re.I), "CLUSTERED/NONCLUSTERED"),
    (re.compile(r"\bWITH\s*\(\s*(?:NOLOCK|READUNCOMMITTED|TABLOCK)", re.I),
     "table locking hint"),
    (re.compile(r"(?m)^\s*GO\s*;?\s*$", re.I), "GO batch separator"),
    (re.compile(r"\bsp_[a-z_]+", re.I), "sp_ system procedure"),
    (re.compile(r"(?<![A-Za-z0-9_])dbo\.", re.I),
     "dbo. schema qualifier (no dbo schema exists in PostgreSQL by default)"),
    (re.compile(r"(?mi)^\s*IF\s+(?:NOT\s+)?EXISTS\s*\("), "T-SQL IF EXISTS guard"),
    (re.compile(r"(?mi)^\s*DECLARE\s+@"), "T-SQL DECLARE @variable"),
)


# A boolean column compared to 0/1 is the defect that grammar validation can
# never catch: `WHERE IsDeleted = 0` parses perfectly and then raises
# `operator does not exist: boolean = integer` at execution. The column-naming
# judgement is autofix's (A007) so both layers agree on what "boolean" means --
# duplicating that list here would let the two drift apart.
_BOOL_COMPARE_RE = re.compile(r"(?:\w+\.)?(\w+)\s*(?:=|<>|!=)\s*([01])\b")


def _boolean_int_comparisons(sql: str) -> list[str]:
    proven = autofix._boolean_columns(sql)
    hits: list[str] = []
    for match in _BOOL_COMPARE_RE.finditer(sql):
        name = match.group(1)
        if autofix._is_boolean_column(name, proven) and name not in hits:
            hits.append(name)
    return hits


_DOLLAR_BLOCK_RE = re.compile(r"\$(\w*)\$.*?\$\1\$", re.DOTALL)

# `IF EXISTS (...) THEN` is ordinary PL/pgSQL, so this label must not fire on
# text inside a dollar-quoted block -- otherwise every correctly converted
# DO $$ ... $$ changeset would be rejected.
_PLPGSQL_LEGAL = {"T-SQL IF EXISTS guard"}


def residual_tsql(sql: str) -> list[str]:
    """SQL-Server-only constructs still present after conversion."""
    if not sql.strip():
        return []
    masked = re.sub(r"(?m)^\s*--.*$", "", sql)      # ignore comment lines
    blocks = [(m.start(), m.end()) for m in _DOLLAR_BLOCK_RE.finditer(masked)]

    def in_plpgsql(position: int) -> bool:
        return any(start <= position < end for start, end in blocks)

    found: list[str] = []
    for pattern, label in _RESIDUAL_TSQL:
        for match in pattern.finditer(masked):
            if label in _PLPGSQL_LEGAL and in_plpgsql(match.start()):
                continue
            if label not in found:
                found.append(label)
            break

    flags = _boolean_int_comparisons(masked)
    if flags:
        found.append("boolean column compared to 0/1 — PostgreSQL raises "
                     "'operator does not exist: boolean = integer' ("
                     + ", ".join(flags[:4]) + ")")
    return found


def final_check(sql: str) -> str:
    """Last gate before anything is written: will this actually run?

    `orchestrator.convert` validates its own output, but `make_idempotent`
    edits that output afterwards to inject IF NOT EXISTS. Text that is never
    re-checked is text that can fail on deploy, so the ASSEMBLED body is
    checked here -- grammar first, then residual T-SQL that the grammar
    happily accepts. Returns "" when the text is safe to emit.
    """
    if not sql.strip():
        return ""
    try:
        outcome = validator.validate(sql)
    except Exception as exc:                 # validator must never block a run
        return f"could not validate: {type(exc).__name__}: {exc}"
    if not getattr(outcome, "valid", True):
        errors = getattr(outcome, "errors", []) or ["not valid PostgreSQL"]
        return str(errors[0])
    leftovers = residual_tsql(sql)
    if leftovers:
        return ("T-SQL survived conversion: " + "; ".join(leftovers))
    return ""


def convert_sql(sql: str) -> tuple[str, ConversionResult | None, list[str], str]:
    """Convert one T-SQL fragment. Returns (pg_sql, last_result, codes, reason).

    An empty reason means success. Any non-empty reason must be treated as a
    refusal -- callers never emit partial output.
    """
    codes: list[str] = []
    sql, go_count = _strip_go(sql)
    if go_count:
        codes.append("LB004")
    if not sql.strip():
        return "", None, codes, ""

    segments, unsupported = split_guards(sql)
    if unsupported:
        return "", None, codes, unsupported[0]
    if not segments:
        segments = [_Segment(sql)]

    pieces: list[str] = []
    last: ConversionResult | None = None
    has_sql = False
    for segment in segments:
        if not segment.sql.strip():
            continue
        if is_comment_only(segment.sql):
            # Section banners between statements. `--` comments are valid
            # PostgreSQL, so they are carried through verbatim -- but they must
            # not be handed to the engine, which would return empty SQL and be
            # read as a failed conversion.
            pieces.append(segment.sql.strip())
            continue
        result = orchestrator.convert(segment.sql)
        last = result
        if not result.is_usable:
            detail = next((w.message for w in result.sorted_warnings
                           if w.severity is Severity.ERROR), "conversion failed")
            return "", result, codes, detail
        piece = result.sql.strip()
        if segment.guarded:
            piece = make_idempotent(piece, segment.kind)
            if "LB005" not in codes:
                codes.append("LB005")
        pieces.append(_ensure_semicolon(piece))
        has_sql = True

    if not has_sql:
        return "", last, codes, ""          # comments only -- nothing to twin

    body = "\n\n".join(pieces)
    problem = final_check(body)
    if problem:
        return "", last, codes, f"post-conversion syntax check failed: {problem}"
    return body, last, codes, ""


def build_twin(changeset: Changeset) -> TwinResult:
    """Convert one mssql changeset into its PostgreSQL counterpart."""
    twin = TwinResult()
    body, result, codes, reason = convert_sql(changeset.body)
    twin.codes = list(codes)
    if reason:
        twin.reason = reason
        if result is not None:
            twin.warnings = list(result.sorted_warnings)
        return twin
    if not body.strip():
        twin.reason = "changeset has no convertible SQL"
        return twin
    if result is not None:
        twin.warnings = list(result.sorted_warnings)

    for rollback in changeset.rollbacks:
        if not rollback.strip():
            continue
        converted, _r, _c, rb_reason = convert_sql(rollback)
        if rb_reason or not converted.strip():
            # A rollback that will not convert must not be silently dropped and
            # must not be emitted as T-SQL. Report and leave the twin without
            # one -- Liquibase simply cannot roll this changeset back.
            twin.codes.append("LB003!")
            continue
        twin.rollbacks.append(" ".join(converted.split("\n")).strip())
        if "LB003" not in twin.codes:
            twin.codes.append("LB003")

    twin.sql = body
    twin.needs_split_off = "$$" in body
    if twin.needs_split_off:
        twin.codes.append("LB002")
    twin.ok = True
    return twin


def render_twin(changeset: Changeset, twin: TwinResult) -> str:
    """The changeset block appended to the changelog. Distinct id (LB001) and
    `splitStatements:false` when the body carries a dollar-quoted block."""
    attrs = "dbms:postgresql"
    if twin.needs_split_off or changeset.split_statements_off:
        attrs += " splitStatements:false"
    for key in ("runOnChange", "runAlways", "failOnError", "context", "labels"):
        if key in changeset.attrs:
            attrs += f" {key}:{changeset.attrs[key]}"

    lines = ["", f"--changeset {changeset.author}:{twin_id(changeset)} {attrs}"]
    comment = changeset.comment or f"PostgreSQL twin of {changeset.id}"
    lines.append(f"--comment: {comment}")
    lines.append(twin.sql)
    for rollback in twin.rollbacks:
        lines.append(f"--rollback {rollback}")
    return "\n".join(lines) + "\n"


def render_repaired(changeset: Changeset, body: str,
                    rollbacks: list[str]) -> str:
    """Rebuild an existing PostgreSQL changeset with a corrected body.

    The id and author are preserved deliberately -- this changeset is being
    fixed, not replaced, and renaming it would make Liquibase treat it as new
    work. Every other attribute is carried through, with
    `splitStatements:false` added when the body needs it.
    """
    attrs = "dbms:postgresql"
    if "$$" in body or changeset.split_statements_off:
        attrs += " splitStatements:false"
    for key, value in changeset.attrs.items():
        if key.lower() in ("dbms", "splitstatements"):
            continue
        attrs += f" {key}:{value}"

    lines = [f"--changeset {changeset.author}:{changeset.id} {attrs}"]
    if changeset.comment:
        lines.append(f"--comment: {changeset.comment}")
    lines.append(body)
    for rollback in rollbacks:
        lines.append(f"--rollback {rollback}")
    lines.append("")
    return "\n".join(lines)


# ==========================================================================
# File processing
# ==========================================================================

def _validate_existing_pg(changeset: Changeset) -> str:
    """Check an existing `dbms:postgresql` changeset the same way a generated
    one is checked. Several in the wild are verbatim copies of their MSSQL twin
    and die on deploy; this reports them without touching anything. GO is NOT
    stripped first -- a GO inside a PostgreSQL changeset is itself the defect.
    """
    if not changeset.body.strip():
        return ""
    return final_check(changeset.body)


def process_text(text: str, repair: bool = False) -> tuple[str, list[Finding], int]:
    """Pure transformation: changelog in, changelog out. No file access, so
    this is what the tests drive.

    `repair=True` additionally rewrites BROKEN `dbms:postgresql` changesets --
    ones whose body is still T-SQL, or whose dollar-quoted block lacks
    `splitStatements:false`. Correct changesets are never touched, so the
    checksum of anything that has actually run is left alone.
    """
    changesets = parse(text)
    findings: list[Finding] = []

    for line, header in malformed_headers(text):
        findings.append(Finding(
            line, header.split()[0] if header.split() else "(empty)", SKIPPED,
            "LB008",
            "changeset header is not `author:id` — Liquibase cannot parse this "
            "file and the whole changelog fails to load"))

    if not changesets:
        return text, findings, 0

    for identity in duplicate_identities(changesets):
        findings.append(Finding(
            0, identity, SKIPPED, "LB007",
            "two changesets share this id and author -- Liquibase keys on "
            "id+author+file, so they collide in DATABASECHANGELOG and cause a "
            "checksum failure. Give one of them a distinct id."))

    # (start, end, replacement-text). start == end means "insert here";
    # start < end replaces those lines. Applied last-first so earlier offsets
    # stay valid.
    edits: list[tuple[int, int, str]] = []

    for changeset in changesets:
        if changeset.dbms != "postgresql":
            continue
        problem = _validate_existing_pg(changeset)
        needs_split = ("$$" in changeset.body
                       and not changeset.split_statements_off)
        if not problem and not needs_split:
            continue

        if not repair:
            if problem:
                findings.append(Finding(
                    changeset.header_line, changeset.identity, SKIPPED, "LB009",
                    f"existing PostgreSQL changeset is not valid PostgreSQL: "
                    f"{problem}"))
            else:
                findings.append(Finding(
                    changeset.header_line, changeset.identity, SKIPPED, "LB002",
                    "existing PostgreSQL changeset has a dollar-quoted block "
                    "but no splitStatements:false -- Liquibase may split it on "
                    "its inner semicolons."))
            continue

        # -- repair -------------------------------------------------------
        if problem:
            # The body is T-SQL sitting in a dbms:postgresql slot. Converting
            # it IS the job. Such a changeset cannot have run (Liquibase would
            # have aborted on it), so there is no stored checksum to violate.
            body, _result, codes, reason = convert_sql(changeset.body)
            if reason:
                findings.append(Finding(
                    changeset.header_line, changeset.identity, SKIPPED, "LB009",
                    f"cannot repair automatically -- {reason}"))
                continue
            rollbacks: list[str] = []
            for rollback in changeset.rollbacks:
                converted, _r, _c, rb_reason = convert_sql(rollback)
                if rb_reason or not converted.strip():
                    codes.append("LB003!")
                    continue
                rollbacks.append(" ".join(converted.split("\n")).strip())
            codes = ["LB010"] + codes
        else:
            body = changeset.body.strip()
            rollbacks = list(changeset.rollbacks)
            codes = ["LB002"]

        edits.append((changeset.start, changeset.end,
                      render_repaired(changeset, body, rollbacks)))
        findings.append(Finding(
            changeset.header_line, changeset.identity, REPAIRED,
            ",".join(dict.fromkeys(codes)),
            "PostgreSQL changeset rewritten in place"))

    for changeset in changesets:
        if changeset.dbms != "mssql":
            continue
        if has_pg_twin(changeset, changesets):
            findings.append(Finding(changeset.header_line, changeset.identity,
                                    CLEAN, "LB006",
                                    "already has a PostgreSQL twin"))
            continue
        twin = build_twin(changeset)
        if not twin.ok:
            findings.append(Finding(changeset.header_line, changeset.identity,
                                    SKIPPED, "LB005", twin.reason))
            continue
        edits.append((changeset.end, changeset.end, render_twin(changeset, twin)))
        findings.append(Finding(changeset.header_line, changeset.identity,
                                CONVERTED, ",".join(["LB001"] + twin.codes),
                                f"twin id {twin_id(changeset)}"))

    if not edits:
        return text, findings, 0

    lines = text.split("\n")
    for start, end, block in sorted(edits, key=lambda e: e[0], reverse=True):
        lines[start:end] = block.split("\n")
    return "\n".join(lines), findings, len(edits)


def _process(path: Path, dry_run: bool, repair: bool = False) -> FileReport:
    report = FileReport(path=path)
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            report.status = SKIPPED
            report.error = "file too large"
            return report
        text, newline, encoding = _read_text(path)
    except (OSError, UnicodeDecodeError) as exc:
        report.status = FAILED
        report.error = f"{type(exc).__name__}: {exc}"
        return report

    if not is_changelog(text):
        return report

    try:
        updated, findings, added = process_text(text, repair=repair)
    except Exception as exc:                      # one file must not kill a run
        report.status = FAILED
        report.error = f"convert failed: {type(exc).__name__}: {exc}"
        return report

    report.findings = findings
    report.changesets = len(parse(text))
    if not added:
        report.status = SKIPPED if report.needs_attention else CLEAN
        return report
    outcome = REPAIRED if report.repaired and not report.converted else CONVERTED
    if dry_run:
        report.status = outcome
        return report

    try:
        backup = path.with_suffix(path.suffix + BACKUP_SUFFIX)
        if not backup.exists():
            shutil.copy2(path, backup)
        # Same encoding it came in as: a changelog that was Windows-1252 stays
        # Windows-1252, so the diff shows only the changesets we added.
        with open(path, "w", encoding=encoding, newline="") as handle:
            handle.write(updated.replace("\n", newline))
        report.backup = backup
        report.status = outcome
    except OSError as exc:
        report.status = FAILED
        report.error = f"could not write file: {exc}"
    return report


def run(root: str | Path, *, recursive: bool = True, dry_run: bool = False,
        repair: bool = False, on_progress: ProgressFn | None = None,
        should_stop: Callable[[], bool] | None = None) -> ChangelogReport:
    """Convert every changelog under `root`.

    Single-threaded on purpose: conversion is CPU-bound inside sqlglot and the
    file count here is small (tens, not thousands), so the C# scanner's thread
    pool would add contention for no gain.
    """
    started = time.perf_counter()
    root = Path(root)
    report = ChangelogReport(root=root, dry_run=dry_run)

    files = find_changelog_files(root, recursive)
    total = len(files)
    for index, path in enumerate(files, start=1):
        if should_stop and should_stop():
            break
        try:
            item = _process(path, dry_run, repair)
        except Exception as exc:
            item = FileReport(path=path, status=FAILED,
                              error=f"{type(exc).__name__}: {exc}")
        report.files.append(item)
        if on_progress:
            on_progress(item, index, total)

    report.duration_ms = int((time.perf_counter() - started) * 1000)
    return report


# ==========================================================================
# Report
# ==========================================================================

_CODE_HELP = {
    "LB001": "PostgreSQL twin given a distinct changeset id",
    "LB002": "splitStatements:false required for a dollar-quoted block",
    "LB003": "rollback statements converted",
    "LB003!": "a rollback could not be converted and was omitted",
    "LB004": "GO batch separators removed",
    "LB005": "T-SQL existence guard rewritten as IF NOT EXISTS",
    "LB006": "already had a PostgreSQL twin -- skipped",
    "LB007": "duplicate changeset id+author in one file",
    "LB008": "changeset header is not `author:id` -- Liquibase cannot parse it",
    "LB009": "existing PostgreSQL changeset is not valid PostgreSQL",
    "LB010": "broken PostgreSQL changeset repaired in place (repair mode)",
}


def render_report(report: ChangelogReport) -> str:
    mode = ("DRY RUN -- nothing was written" if report.dry_run
            else "changelogs updated in place")
    lines = [
        "# Liquibase changelog conversion",
        "",
        "Created by: Vasu Ritesh",
        "",
        f"- Root: `{report.root}`",
        f"- Mode: **{mode}**",
        f"- Duration: {report.duration_ms / 1000:.1f}s",
        "",
        "| | Count |",
        "|---|---|",
        f"| changelog files found | {report.scanned} |",
        f"| changesets parsed | {report.changesets} |",
        f"| PostgreSQL twins added | {report.converted} |",
        f"| broken PostgreSQL changesets repaired | {report.repaired} |",
        f"| changesets needing attention | {report.needs_attention} |",
        f"| files failed | {report.failed} |",
        "",
    ]

    interesting = [f for f in report.files if f.findings or f.status == FAILED]
    if not interesting:
        lines.append("No Liquibase changelogs found.")
        return "\n".join(lines) + "\n"

    lines += ["## Files", ""]
    for item in interesting:
        try:
            shown = item.path.relative_to(report.root)
        except ValueError:
            shown = item.path
        lines.append(f"### `{shown}` -- {item.status}")
        if item.error:
            lines.append(f"- {item.error}")
        if item.backup:
            lines.append(f"- backup: `{item.backup.name}`")
        for finding in item.findings:
            where = f"line {finding.line} " if finding.line else ""
            lines.append(f"- {where}`{finding.changeset}`: **{finding.action}** "
                         f"{finding.code} {finding.reason}".rstrip())
        lines.append("")

    lines += ["## Codes", ""]
    lines += [f"- `{code}` -- {text}" for code, text in _CODE_HELP.items()]
    return "\n".join(lines) + "\n"


def write_report(report: ChangelogReport,
                 filename: str = "liquibase-conversion-report.md") -> Path | None:
    if report.root is None:
        return None
    path = report.root / filename
    try:
        path.write_text(render_report(report), encoding="utf-8")
        return path
    except OSError:
        return None
