"""Test isolation for the schema hint files (v2.16.0).

The shipped boolean_columns.txt / column_types.txt are generated from the target
schema and change whenever it does. The suite pins the hand-confirmed baseline
list and NO column types, so results never depend on what ships next to app.py;
tests of A024/A054 point the env vars at their own files. The placeholder policy
is the v2.16.0 default ("drop") unless a test sets MSSQLTOPG_DB_PLACEHOLDER.
"""
from pathlib import Path

import pytest

from core import schema_hints

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def _pinned_schema_hints(monkeypatch):
    monkeypatch.setenv(schema_hints.ENV_VAR, str(FIXTURES / "boolean_columns_baseline.txt"))
    monkeypatch.setenv(schema_hints.TYPES_ENV_VAR, str(FIXTURES / "column_types_empty.txt"))
    monkeypatch.delenv(schema_hints.PLACEHOLDER_ENV_VAR, raising=False)
    schema_hints.reset()
    yield
    schema_hints.reset()
