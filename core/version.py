"""Single source of truth for the application version.

Shown in the window title, printed by the CLI, and stamped into every log
entry -- so a log excerpt always identifies which build produced it.

Bump MINOR for new rules or features, PATCH for fixes.
"""

APP_NAME = "MS-SQL to PostgreSQL Converter"
VERSION = "2.0.0"

# Bumped whenever a linter rule is added/changed, so a log tells you which
# rule set was in force. Currently L001-L014 plus the pglast validator.
RULES = "L001-L014 lint, A001-A009 auto-fix, PG grammar validation, C# project scan"


def title() -> str:
    return f"{APP_NAME}  v{VERSION}"


def banner() -> str:
    return f"{APP_NAME} v{VERSION}  ({RULES})"
