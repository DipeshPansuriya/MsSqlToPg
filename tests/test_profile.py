"""The project profile: application-specific names come from a local JSON file."""
import pytest

from core import orchestrator, pgcheck, profile
from tools import fix_tsql_for_postgres as fixer


def use(monkeypatch, tmp_path, text):
    path = tmp_path / "project_profile.json"
    path.write_text(text, encoding="utf-8")
    monkeypatch.setenv(profile.ENV_VAR, str(path))
    profile.reset()
    return path


def codes(sql):
    return {w.code for w in orchestrator.convert(sql).warnings}


def test_values_are_normalised(monkeypatch, tmp_path):
    path = use(monkeypatch, tmp_path,
               '{"literal_safe_parameters": ["@UserId"], "prefix_replaced_parameters": ["UserId"],'
               ' "boolean_columns": ["DefaultFlag"], "local_time_functions": ["fnToLocalTime"]}')
    active = profile.current()
    assert active.literal_safe_parameters == {"userid"}
    assert active.prefix_replaced_parameters == ("@UserId",)
    assert active.boolean_columns == {"defaultflag"}
    assert profile.is_local_time_function("FNTOLOCALTIME")
    assert active.source == str(path) and active.problem is None


def test_empty_profile_turns_name_rules_off(monkeypatch, tmp_path):
    use(monkeypatch, tmp_path, "{}")
    assert "L031" not in codes("SELECT a FROM t WHERE b IN @UserIdList")
    assert "A025" not in codes("SELECT fnToLocalTime(CAST(a.D AS DATETIME), @Tz) FROM a")
    sql = "SELECT fnToLocalTime(CAST(x.D AS TIMESTAMP), @Tz) AS t"
    assert pgcheck.repair_local_time_calls(sql) == (sql, 0)
    assert not fixer.find_manual("SELECT fnToLocalTime(d, @Tz)")
    assert fixer.apply_fixes("SELECT dbo.fnToLocalTime(d, @Tz)")[1] == []


def test_profile_turns_name_rules_on(monkeypatch, tmp_path):
    use(monkeypatch, tmp_path,
        '{"prefix_replaced_parameters": ["@UserId"], "local_time_functions": ["fnToLocalTime"]}')
    assert "L031" in codes("SELECT a FROM t WHERE b IN @UserIdList")
    assert "A025" in codes("SELECT fnToLocalTime(CAST(a.D AS DATETIME), @Tz) FROM a")
    assert [c for c, _, _ in fixer.apply_fixes("SELECT dbo.fnToLocalTime(d, @Tz)")[1]] == ["F007-dbo-prefix"]
    assert [c for c, _, _ in fixer.find_manual("SELECT fnToLocalTime(d, @Tz)")] == ["M002-udf"]


@pytest.mark.parametrize("text", ["{not json", "[1, 2]", '{"boolean_columns": "x"}'])
def test_broken_file_is_reported_and_conversion_still_works(monkeypatch, tmp_path, text):
    use(monkeypatch, tmp_path, text)
    assert profile.current().problem
    assert "IGNORED" in profile.describe()
    assert orchestrator.convert("SELECT TOP 1 a FROM t").sql


def test_shipped_example_is_valid():
    from pathlib import Path
    example = Path(__file__).resolve().parent.parent / "project_profile.example.json"
    assert profile.parse(example.read_text(encoding="utf-8")).local_time_functions
