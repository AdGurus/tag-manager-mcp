from __future__ import annotations

from tag_manager_mcp.debug_log import DebugCallLogger, sanitize, sanitize_uri


def test_sanitize_redacts_nested_secrets() -> None:
    value = sanitize(
        {
            "access_token": "secret",
            "nested": {"private_key": "key", "name": "safe"},
        }
    )
    assert value == {
        "access_token": "[REDACTED]",
        "nested": {"private_key": "[REDACTED]", "name": "safe"},
    }
    assert "secret" not in sanitize_uri("https://example.test/path?access_token=secret&x=1")


def test_sqlite_logger_records_and_reads_events(tmp_path) -> None:
    debug = DebugCallLogger(
        {
            "GTM_DEBUG_LOG_ENABLED": "true",
            "GTM_DEBUG_DB_PATH": str(tmp_path / "debug.db"),
            "GTM_DEBUG_RETENTION_DAYS": "30",
            "GTM_DEBUG_MAX_PAYLOAD_BYTES": "10000",
        }
    )
    debug.log(
        kind="mcp_tool",
        name="test_tool",
        status="success",
        request={"client_secret": "hidden", "value": 1},
        response={"ok": True},
        call_id="abc",
    )

    status = debug.status()
    assert status["total_events"] == 1
    assert status["events_by_kind"] == {"mcp_tool": 1}
    row = debug.recent(limit=1)[0]
    assert row["call_id"] == "abc"
    assert row["request"] == {"client_secret": "[REDACTED]", "value": 1}
    assert row["response"] == {"ok": True}
