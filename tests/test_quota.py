from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from tag_manager_mcp.quota import LocalQuotaExceeded, QuotaGuard


def test_guard_spaces_requests_and_tracks_daily_budget(tmp_path) -> None:
    now = [datetime(2026, 6, 22, tzinfo=ZoneInfo("UTC")).timestamp()]
    sleeps: list[float] = []

    def clock() -> float:
        return now[0]

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        now[0] += seconds

    guard = QuotaGuard(
        {
            "GTM_MIN_REQUEST_INTERVAL_SECONDS": "4.1",
            "GTM_DAILY_REQUEST_BUDGET": "2",
            "GTM_QUOTA_STATE_FILE": str(tmp_path / "quota.json"),
        },
        clock=clock,
        sleep=sleep,
    )

    assert guard.before_request()["request_count"] == 1
    assert guard.before_request()["request_count"] == 2
    assert sleeps == [pytest.approx(4.1)]
    with pytest.raises(LocalQuotaExceeded):
        guard.before_request()


def test_guard_resets_on_new_pacific_day(tmp_path) -> None:
    now = [datetime(2026, 6, 22, 12, tzinfo=ZoneInfo("UTC")).timestamp()]
    guard = QuotaGuard(
        {
            "GTM_MIN_REQUEST_INTERVAL_SECONDS": "0",
            "GTM_DAILY_REQUEST_BUDGET": "1",
            "GTM_QUOTA_STATE_FILE": str(tmp_path / "quota.json"),
        },
        clock=lambda: now[0],
    )
    guard.before_request()
    now[0] += 86_400
    assert guard.before_request()["request_count"] == 1
