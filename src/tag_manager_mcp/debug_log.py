"""Best-effort SQLite debug logging for MCP and GTM API calls."""

from __future__ import annotations

import contextvars
import json
import logging
import os
import sqlite3
import threading
import uuid
from collections.abc import Mapping
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(PROJECT_ROOT / ".env", override=False)

logger = logging.getLogger(__name__)
call_id_context: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "gtm_mcp_call_id", default=None
)

SENSITIVE_KEYS = {
    "access_token",
    "authorization",
    "client_secret",
    "credentials",
    "id_token",
    "oauth_token",
    "private_key",
    "refresh_token",
    "service_account_json",
}


class DebugCallLogger:
    def __init__(self, env: Mapping[str, str] | None = None) -> None:
        env = env if env is not None else os.environ
        self.enabled = env.get("GTM_DEBUG_LOG_ENABLED", "true").lower() not in {
            "0",
            "false",
            "no",
        }
        configured = env.get("GTM_DEBUG_DB_PATH")
        self.path = (
            Path(configured).expanduser()
            if configured
            else Path.home() / ".config/gtm-mcp/debug.db"
        )
        self.max_payload_bytes = int(env.get("GTM_DEBUG_MAX_PAYLOAD_BYTES", "262144"))
        self.retention_days = int(env.get("GTM_DEBUG_RETENTION_DAYS", "30"))
        self._lock = threading.Lock()
        if self.enabled:
            try:
                self._initialize()
            except (OSError, sqlite3.Error) as exc:
                self.enabled = False
                logger.warning("Could not initialize GTM debug database: %s", exc)

    def new_call_id(self) -> str:
        return uuid.uuid4().hex

    def log(
        self,
        *,
        kind: str,
        name: str,
        status: str,
        request: Any = None,
        response: Any = None,
        method: str | None = None,
        resource: str | None = None,
        duration_ms: float | None = None,
        attempt: int | None = None,
        http_status: int | None = None,
        error: BaseException | None = None,
        call_id: str | None = None,
    ) -> None:
        if not self.enabled:
            return
        try:
            request_json = self._serialize(request)
            response_json = self._serialize(response)
            with self._lock, self._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO call_log (
                        timestamp_utc, call_id, kind, name, method, resource, status,
                        http_status, duration_ms, attempt, request_json, response_json,
                        error_type, error_message, process_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        datetime.now(UTC).isoformat(),
                        call_id or call_id_context.get(),
                        kind,
                        name,
                        method,
                        sanitize_uri(resource),
                        status,
                        http_status,
                        round(duration_ms, 3) if duration_ms is not None else None,
                        attempt,
                        request_json,
                        response_json,
                        type(error).__name__ if error else None,
                        _truncate(str(error), self.max_payload_bytes) if error else None,
                        os.getpid(),
                    ),
                )
        except Exception as exc:  # debug logging must never break GTM operations
            logger.warning("Could not write GTM debug log: %s", exc)

    def status(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "enabled": self.enabled,
            "database_path": str(self.path),
            "retention_days": self.retention_days,
            "max_payload_bytes": self.max_payload_bytes,
        }
        if not self.enabled or not self.path.exists():
            return result | {"total_events": 0, "events_by_kind": {}}
        try:
            with self._connect() as connection:
                total = connection.execute("SELECT COUNT(*) FROM call_log").fetchone()[0]
                rows = connection.execute(
                    "SELECT kind, COUNT(*) AS count FROM call_log GROUP BY kind"
                ).fetchall()
            return result | {
                "total_events": total,
                "events_by_kind": {row["kind"]: row["count"] for row in rows},
            }
        except sqlite3.Error as exc:
            return result | {"error": str(exc)}

    def recent(
        self, *, limit: int = 50, kind: str | None = None, status: str | None = None
    ) -> list[dict[str, Any]]:
        if not self.enabled:
            return []
        limit = max(1, min(limit, 500))
        clauses: list[str] = []
        parameters: list[Any] = []
        if kind:
            clauses.append("kind = ?")
            parameters.append(kind)
        if status:
            clauses.append("status = ?")
            parameters.append(status)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        query = f"SELECT * FROM call_log{where} ORDER BY id DESC LIMIT ?"  # noqa: S608
        parameters.append(limit)
        with self._connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [_row_to_dict(row) for row in rows]

    def _initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode=WAL;
                PRAGMA synchronous=NORMAL;
                CREATE TABLE IF NOT EXISTS call_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp_utc TEXT NOT NULL,
                    call_id TEXT,
                    kind TEXT NOT NULL,
                    name TEXT NOT NULL,
                    method TEXT,
                    resource TEXT,
                    status TEXT NOT NULL,
                    http_status INTEGER,
                    duration_ms REAL,
                    attempt INTEGER,
                    request_json TEXT,
                    response_json TEXT,
                    error_type TEXT,
                    error_message TEXT,
                    process_id INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_call_log_timestamp ON call_log(timestamp_utc);
                CREATE INDEX IF NOT EXISTS idx_call_log_call_id ON call_log(call_id);
                CREATE INDEX IF NOT EXISTS idx_call_log_kind_status ON call_log(kind, status);
                """
            )
            cutoff = (datetime.now(UTC) - timedelta(days=self.retention_days)).isoformat()
            connection.execute("DELETE FROM call_log WHERE timestamp_utc < ?", (cutoff,))
        self.path.chmod(0o600)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    def _serialize(self, value: Any) -> str | None:
        if value is None:
            return None
        safe = sanitize(value)
        serialized = json.dumps(safe, ensure_ascii=False, default=str)
        return _truncate(serialized, self.max_payload_bytes)


_logger: DebugCallLogger | None = None
_logger_lock = threading.Lock()


def get_debug_logger() -> DebugCallLogger:
    global _logger
    if _logger is None:
        with _logger_lock:
            if _logger is None:
                _logger = DebugCallLogger()
    return _logger


def sanitize(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): "[REDACTED]" if str(key).lower() in SENSITIVE_KEYS else sanitize(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [sanitize(item) for item in value]
    if isinstance(value, bytes):
        try:
            return sanitize(json.loads(value.decode("utf-8")))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return f"[BINARY {len(value)} bytes]"
    if hasattr(value, "model_dump"):
        return sanitize(value.model_dump(mode="json"))
    return value


def sanitize_uri(uri: str | None) -> str | None:
    if not uri:
        return uri
    try:
        parts = urlsplit(uri)
        query = [
            (key, "[REDACTED]" if key.lower() in SENSITIVE_KEYS else value)
            for key, value in parse_qsl(parts.query, keep_blank_values=True)
        ]
        return urlunsplit(
            (parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment)
        )
    except ValueError:
        return uri


def request_details(request: Any) -> tuple[str | None, str | None, Any]:
    method = getattr(request, "method", None)
    uri = getattr(request, "uri", None)
    body = getattr(request, "body", None)
    if isinstance(body, str):
        with suppress(json.JSONDecodeError):
            body = json.loads(body)
    return method, uri, body


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    result = dict(row)
    for key in ("request_json", "response_json"):
        if result.get(key):
            raw = result[key]
            with suppress(json.JSONDecodeError):
                result[key.removesuffix("_json")] = json.loads(raw)
                result.pop(key)
    return result


def _truncate(value: str, max_bytes: int) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value
    suffix = f"...[TRUNCATED; original_bytes={len(encoded)}]"
    room = max(0, max_bytes - len(suffix.encode("utf-8")))
    return encoded[:room].decode("utf-8", errors="ignore") + suffix
