"""Single source of truth for the application version.

Shown in the window title, printed by the CLI, and stamped into every log
entry -- so a log excerpt always identifies which build produced it.

Bump MINOR for new rules or features, PATCH for fixes.
"""

APP_NAME = "MS-SQL to PostgreSQL Converter"
AUTHOR = "Vasu Ritesh"
VERSION = "2.18.1"

# Bumped whenever a linter rule is added/changed, so a log tells you which
# rule set was in force. The full rule book lives in CONVERSION_RULES.md.
RULES = ("L001-L032 lint, A001-A061 auto-fix, R001-R008 resolvability, "
         "LB001-LB010 Liquibase, PC01-PC16 converted-PG repair, "
         "interpolated-string conversion, "
         "whitespace-preserving output, PG grammar validation, "
         "runtime-resolvability gate, residual-T-SQL gate, shared T-SQL marker set, "
         "boolean_columns.txt + column_types.txt schema hints, C# project scan, T-SQL audit, "
         "Liquibase changelog scan + repair")


def title() -> str:
    return f"{APP_NAME}  v{VERSION}"


def banner() -> str:
    return f"{APP_NAME} v{VERSION}  ({RULES})  -- Created by {AUTHOR}"
