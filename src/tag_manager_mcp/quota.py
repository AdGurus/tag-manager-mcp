"""Cross-process quota guardrails for the Google Tag Manager API."""

from __future__ import annotations

import fcntl
import json
import os
import threading
import time
from collections.abc import Callable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

PACIFIC = ZoneInfo("America/Los_Angeles")


class LocalQuotaExceeded(RuntimeError):
    """Raised before a request would exceed the configured daily safety budget."""


class QuotaGuard:
    """Persist request timing and daily usage so concurrent MCP processes cooperate."""

    def __init__(
        self,
        env: Mapping[str, str] | None = None,
        *,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        env = env if env is not None else os.environ
        self.minimum_interval = _nonnegative_float(
            env.get("GTM_MIN_REQUEST_INTERVAL_SECONDS", "4.1"),
            "GTM_MIN_REQUEST_INTERVAL_SECONDS",
        )
        self.daily_budget = _positive_int(
            env.get("GTM_DAILY_REQUEST_BUDGET", "9000"),
            "GTM_DAILY_REQUEST_BUDGET",
        )
        configured_path = env.get("GTM_QUOTA_STATE_FILE")
        self.state_file = (
            Path(configured_path).expanduser()
            if configured_path
            else Path.home() / ".config/gtm-mcp/quota-state.json"
        )
        self._clock = clock
        self._sleep = sleep
        self._thread_lock = threading.Lock()

    def before_request(self) -> dict[str, Any]:
        """Wait for the rate window, reserve daily quota, and return current usage."""
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        lock_file = self.state_file.with_suffix(self.state_file.suffix + ".lock")

        with self._thread_lock, lock_file.open("a+", encoding="utf-8") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            state = self._read_state()
            now = self._clock()
            pacific_date = datetime.fromtimestamp(now, tz=PACIFIC).date().isoformat()

            if state.get("pacific_date") != pacific_date:
                state = {"pacific_date": pacific_date, "request_count": 0}

            request_count = int(state.get("request_count", 0))
            if request_count >= self.daily_budget:
                raise LocalQuotaExceeded(
                    f"Local GTM daily safety budget exhausted: {request_count}/"
                    f"{self.daily_budget} requests for {pacific_date} (Pacific time)."
                )

            last_request = float(state.get("last_request_epoch", 0.0))
            wait_seconds = max(0.0, self.minimum_interval - (now - last_request))
            if wait_seconds:
                self._sleep(wait_seconds)
                now = self._clock()

            state.update(
                {
                    "pacific_date": pacific_date,
                    "request_count": request_count + 1,
                    "last_request_epoch": now,
                }
            )
            self._write_state(state)
            return {
                "pacific_date": pacific_date,
                "request_count": request_count + 1,
                "daily_budget": self.daily_budget,
                "remaining": self.daily_budget - request_count - 1,
            }

    def status(self) -> dict[str, Any]:
        """Read local quota usage without reserving a request."""
        state = self._read_state()
        count = int(state.get("request_count", 0))
        return {
            "pacific_date": state.get("pacific_date"),
            "request_count": count,
            "daily_budget": self.daily_budget,
            "remaining": max(0, self.daily_budget - count),
            "minimum_interval_seconds": self.minimum_interval,
            "state_file": str(self.state_file),
        }

    def _read_state(self) -> dict[str, Any]:
        try:
            return json.loads(self.state_file.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError, TypeError):
            return {}

    def _write_state(self, state: dict[str, Any]) -> None:
        temporary = self.state_file.with_suffix(self.state_file.suffix + ".tmp")
        temporary.write_text(json.dumps(state, indent=2), encoding="utf-8")
        temporary.chmod(0o600)
        temporary.replace(self.state_file)


def _nonnegative_float(value: str, name: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number") from exc
    if parsed < 0:
        raise ValueError(f"{name} must be non-negative")
    return parsed


def _positive_int(value: str, name: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if parsed <= 0:
        raise ValueError(f"{name} must be positive")
    return parsed
