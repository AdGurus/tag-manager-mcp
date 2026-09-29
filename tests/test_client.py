from __future__ import annotations

import pytest
from googleapiclient.errors import HttpError
from httplib2 import Response

from tag_manager_mcp.client import (
    GTMClient,
    _validate_body,
    container_path,
    parse_container_identifier,
    workspace_path,
)
from tag_manager_mcp.quota import QuotaGuard


def test_resource_paths() -> None:
    assert container_path("123", "456") == "accounts/123/containers/456"
    assert workspace_path("123", "456", "7") == "accounts/123/containers/456/workspaces/7"


@pytest.mark.parametrize("value", ["", "accounts/123"])
def test_resource_ids_reject_empty_or_paths(value: str) -> None:
    with pytest.raises(ValueError):
        container_path(value, "456")


def test_create_tag_body_requires_name_and_type() -> None:
    with pytest.raises(ValueError, match="name"):
        _validate_body("tags", {"type": "gaawe"})
    with pytest.raises(ValueError, match="type"):
        _validate_body("tags", {"name": "GA4 event"})
    _validate_body("tags", {"name": "GA4 event", "type": "gaawe"})


def test_parse_container_identifier() -> None:
    assert parse_container_identifier(" gtm-w525xw4 ") == ("tagId", "GTM-W525XW4")
    assert parse_container_identifier("AW-123456789") == (
        "destinationId",
        "AW-123456789",
    )
    with pytest.raises(ValueError, match="public tag"):
        parse_container_identifier("W525XW4")


def test_list_all_follows_page_tokens(tmp_path) -> None:
    class Request:
        def __init__(self, response: dict) -> None:
            self.response = response

        def execute(self) -> dict:
            return self.response

    seen: list[str | None] = []

    def request(token: str | None) -> Request:
        seen.append(token)
        if token is None:
            return Request({"tag": [{"tagId": "1"}], "nextPageToken": "next"})
        return Request({"tag": [{"tagId": "2"}]})

    guard = QuotaGuard(
        {
            "GTM_MIN_REQUEST_INTERVAL_SECONDS": "0",
            "GTM_DAILY_REQUEST_BUDGET": "10",
            "GTM_QUOTA_STATE_FILE": str(tmp_path / "quota.json"),
        }
    )
    client = GTMClient(service=object(), quota_guard=guard)
    assert client._list_all(request, "tag") == [{"tagId": "1"}, {"tagId": "2"}]
    assert seen == [None, "next"]


def test_execute_retries_quota_response(tmp_path) -> None:
    class Request:
        attempts = 0

        def execute(self) -> dict:
            self.attempts += 1
            if self.attempts == 1:
                response = Response({"status": "429", "retry-after": "0"})
                raise HttpError(response, b'{"error":{"message":"Quota exceeded"}}')
            return {"ok": True}

    guard = QuotaGuard(
        {
            "GTM_MIN_REQUEST_INTERVAL_SECONDS": "0",
            "GTM_DAILY_REQUEST_BUDGET": "10",
            "GTM_QUOTA_STATE_FILE": str(tmp_path / "quota.json"),
        }
    )
    client = GTMClient(service=object(), quota_guard=guard)
    client.max_quota_retries = 1
    request = Request()

    assert client._execute(lambda: request) == {"ok": True}
    assert request.attempts == 2
    assert guard.status()["request_count"] == 2
