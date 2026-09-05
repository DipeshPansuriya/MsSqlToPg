"""Cross-layer data contracts.

Every engine returns a ConversionResult. The UI never learns which engine
produced it, so the UI stays decoupled from conversion.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Severity(Enum):
    ERROR = "error"      # output is unusable, or statements were lost
    WARNING = "warning"  # output is probably wrong, needs review
    INFO = "info"        # advisory only


# Sort order for display: worst first.
_SEVERITY_RANK = {Severity.ERROR: 0, Severity.WARNING: 1, Severity.INFO: 2}


@dataclass(frozen=True)
class Warning:
    code: str            # "L001", "SG-UNSUPPORTED", "API-TRUNCATED"
    severity: Severity
    message: str
    line: int | None = None

    def __str__(self) -> str:
        where = f" (line {self.line})" if self.line else ""
        return f"{self.code}: {self.message}{where}"


@dataclass
class ConversionResult:
    sql: str = ""                                 # converted SQL, "" on failure
    warnings: list[Warning] = field(default_factory=list)
    ok: bool = True                               # False = conversion failed outright
    duration_ms: int = 0
    validation: object = None                     # core.validator.Validation

    def add(self, code: str, severity: Severity, message: str,
            line: int | None = None) -> None:
        self.warnings.append(Warning(code, severity, message, line))

    def fail(self, code: str, message: str) -> "ConversionResult":
        """Mark this result as an outright failure. Engines never raise."""
        self.ok = False
        self.sql = ""
        self.add(code, Severity.ERROR, message)
        return self

    @property
    def sorted_warnings(self) -> list[Warning]:
        return sorted(self.warnings, key=lambda w: _SEVERITY_RANK[w.severity])

    @property
    def has_errors(self) -> bool:
        return any(w.severity is Severity.ERROR for w in self.warnings)

    @property
    def is_usable(self) -> bool:
        """Safe to copy into a target database without further review?"""
        return self.ok and bool(self.sql.strip()) and not self.has_errors
