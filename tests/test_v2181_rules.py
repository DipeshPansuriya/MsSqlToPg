"""v2.18.1: A007 also rewrites a quoted '0'/'1' compared with a boolean column.

T-SQL `ISNULL(sdty.InHouse,'0') = '0'` (Stock GetStockAllotmentListQuery) came out as
`COALESCE(sdty.InHouse, FALSE) = '0'`: A020 fixed the COALESCE default, but A007 read only
0/1 and 'true'/'false' as boolean literals, so the quoted '0' on the other side was left.
"""
from __future__ import annotations

import re

import pytest

from core import orchestrator, schema_hints


def flat(sql: str) -> str:
    return re.sub(r"\s+", " ", sql).strip()


def codes(result) -> list[str]:
    return [w.code for w in result.warnings]


def convert(sql: str):
    r = orchestrator.convert(sql)
    assert r.validation is not None and r.validation.valid, r.sql
    return r


@pytest.fixture
def inhouse_boolean(tmp_path, monkeypatch):
    """InHouse follows no naming convention: only the schema list makes it boolean."""
    path = tmp_path / "boolean_columns.txt"
    path.write_text("inhouse\nisdeleted\n", encoding="utf-8")
    monkeypatch.setenv(schema_hints.ENV_VAR, str(path))
    schema_hints.reset()
    yield
    schema_hints.reset()


def test_isnull_string_default_compared_with_string_zero(inhouse_boolean):
    r = convert("SELECT s.StockId FROM Stocks s "
                "INNER JOIN Stock_DocumentType sdty ON sdty.Id = s.Doc_Type "
                "WHERE ISNULL(sdty.InHouse,'0') = '0'")
    out = flat(r.sql)
    assert "COALESCE(sdty.InHouse, FALSE) = FALSE" in out
    assert "'0'" not in out
    assert "A007" in codes(r)


def test_string_one_not_equal_and_reversed_operands(inhouse_boolean):
    r = convert("SELECT a FROM Stock_DocumentType sdty "
                "WHERE ISNULL(sdty.InHouse,'1') <> '1' AND '0' = ISNULL(sdty.InHouse, 0)")
    out = flat(r.sql)
    assert "COALESCE(sdty.InHouse, TRUE) <> TRUE" in out
    assert "FALSE = COALESCE(sdty.InHouse, FALSE)" in out


def test_bare_column_and_case_condition(inhouse_boolean):
    r = convert("SELECT CASE WHEN sdty.InHouse = '1' THEN 'Y' ELSE 'N' END AS x "
                "FROM Stock_DocumentType sdty")
    out = flat(r.sql)
    assert "sdty.InHouse = TRUE" in out
    assert "THEN 'Y' ELSE 'N'" in out       # result values are not comparisons


def test_is_convention_column_with_string_digit():
    r = convert("SELECT a FROM t WHERE ISNULL(IsDeleted, 0) = '0' AND IsActive = '1'")
    out = flat(r.sql)
    assert "COALESCE(IsDeleted, FALSE) = FALSE" in out
    assert "IsActive = TRUE" in out


def test_non_boolean_columns_keep_their_string_digits():
    r = convert("SELECT a FROM t WHERE Status = '0' AND ISNULL(Remarks,'0') = '0'")
    out = flat(r.sql)
    assert "Status = '0'" in out and "COALESCE(Remarks, '0') = '0'" in out
    assert "A007" not in codes(r)


def test_set_clause_string_digit_is_not_booleanised():
    # the SET guard (DocNoList): a string assigned in SET may be a VARCHAR payload
    r = convert("UPDATE t SET IsDeleted = '0' WHERE a = 1")
    assert "IsDeleted = '0'" in flat(r.sql)


def test_is_column_declared_as_text_in_the_batch_is_left_alone():
    r = convert("CREATE TABLE #t (IsRefundExpire VARCHAR(10)); "
                "SELECT a FROM #t WHERE IsRefundExpire = '1'")
    assert "IsRefundExpire = '1'" in flat(r.sql)
