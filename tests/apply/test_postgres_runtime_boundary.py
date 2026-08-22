from __future__ import annotations

from pathlib import Path

from capybot.apply.sql_session import _compile_sql
from capybot.apply.tasks import _analysis_mode


def test_postgres_session_compiles_qmark_parameters():
    sql = _compile_sql("SELECT * FROM boss_messages WHERE id=? AND message_id=?")

    assert "id=%s AND message_id=%s" in sql


def test_runtime_sql_uses_native_postgres_conflict_syntax():
    root = Path(__file__).resolve().parents[2] / "capybot"
    source = "\n".join(path.read_text(encoding="utf-8") for path in root.rglob("*.py"))

    assert "INSERT OR IGNORE" not in source
    assert "INSERT OR REPLACE" not in source


def test_import_trigger_modes():
    assert _analysis_mode({"new_message_count": 0}) == "skipped"
    assert _analysis_mode({"new_message_count": 1}) == "opportunity_agent"
    assert _analysis_mode({"new_message_count": 1, "analysis_mode": ""}) == "opportunity_agent"
