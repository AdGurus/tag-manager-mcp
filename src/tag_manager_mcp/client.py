"""Thin, testable wrapper around Google Tag Manager API v2."""

from __future__ import annotations

import logging
import os
import random
import re
import time
from collections.abc import Callable
from typing import Any

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from .auth import CredentialProvider
from .debug_log import get_debug_logger, request_details
from .quota import QuotaGuard

logger = logging.getLogger(__name__)


class GTMError(RuntimeError):
    """A normalized GTM API failure."""


def account_path(account_id: str) -> str:
    return f"accounts/{_id(account_id, 'account_id')}"


def container_path(account_id: str, container_id: str) -> str:
    return f"{account_path(account_id)}/containers/{_id(container_id, 'container_id')}"


def workspace_path(account_id: str, container_id: str, workspace_id: str) -> str:
    workspace_id = _id(workspace_id, "workspace_id")
    return f"{container_path(account_id, container_id)}/workspaces/{workspace_id}"


def _id(value: str, label: str) -> str:
    value = str(value).strip()
    if not value or "/" in value:
        raise ValueError(f"{label} must be a non-empty resource ID, not a path")
    return value


class GTMClient:
    def __init__(
        self,
        service: Any | None = None,
        provider: CredentialProvider | None = None,
        quota_guard: QuotaGuard | None = None,
    ) -> None:
        self._service = service
        self._provider = provider or CredentialProvider()
        self.quota_guard = quota_guard or QuotaGuard()
        self.max_quota_retries = int(os.getenv("GTM_QUOTA_MAX_RETRIES", "3"))
        self.quota_backoff_seconds = float(os.getenv("GTM_QUOTA_BACKOFF_SECONDS", "5"))
        self.debug_log = get_debug_logger()
        self.auth_method: str | None = None

    @property
    def service(self) -> Any:
        if self._service is None:
            auth = self._provider.get_credentials(interactive=False)
            self.auth_method = auth.method
            self._service = build(
                "tagmanager", "v2", credentials=auth.credentials, cache_discovery=False
            )
        return self._service

    def _execute(self, request_factory: Callable[[], Any]) -> dict[str, Any]:
        for attempt in range(self.max_quota_retries + 1):
            request = request_factory()
            method, uri, body = request_details(request)
            started = time.monotonic()
            try:
                self.quota_guard.before_request()
                response = request.execute()
                self.debug_log.log(
                    kind="gtm_api",
                    name="gtm_api_request",
                    method=method,
                    resource=uri,
                    request=body,
                    response=response,
                    status="success",
                    duration_ms=(time.monotonic() - started) * 1000,
                    attempt=attempt + 1,
                    http_status=200,
                )
                return response
            except HttpError as exc:
                message = getattr(exc, "reason", None) or str(exc)
                status = getattr(getattr(exc, "resp", None), "status", None)
                self.debug_log.log(
                    kind="gtm_api",
                    name="gtm_api_request",
                    method=method,
                    resource=uri,
                    request=body,
                    status="error",
                    duration_ms=(time.monotonic() - started) * 1000,
                    attempt=attempt + 1,
                    http_status=status,
                    error=exc,
                )
                if not _is_quota_error(exc, message) or attempt >= self.max_quota_retries:
                    raise GTMError(f"Google Tag Manager API error: {message}") from exc

                delay = _retry_after(exc)
                if delay is None:
                    delay = self.quota_backoff_seconds * (2**attempt) + random.uniform(0, 1)
                logger.warning(
                    "GTM quota response; retrying in %.1fs (%d/%d)",
                    delay,
                    attempt + 1,
                    self.max_quota_retries,
                )
                time.sleep(delay)
            except Exception as exc:
                self.debug_log.log(
                    kind="gtm_api",
                    name="gtm_api_request",
                    method=method,
                    resource=uri,
                    request=body,
                    status="error",
                    duration_ms=(time.monotonic() - started) * 1000,
                    attempt=attempt + 1,
                    error=exc,
                )
                raise
        raise AssertionError("unreachable")

    @staticmethod
    def _items(response: dict[str, Any], key: str) -> list[dict[str, Any]]:
        return response.get(key, [])

    def _list_all(
        self, request_factory: Callable[[str | None], Any], key: str
    ) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        page_token: str | None = None
        while True:
            response = self._execute(lambda token=page_token: request_factory(token))
            items.extend(self._items(response, key))
            page_token = response.get("nextPageToken")
            if not page_token:
                return items

    def list_accounts(self) -> list[dict[str, Any]]:
        accounts = self.service.accounts()
        return self._list_all(
            lambda token: accounts.list(pageToken=token) if token else accounts.list(),
            "account",
        )

    def list_containers(self, account_id: str) -> list[dict[str, Any]]:
        parent = account_path(account_id)
        containers = self.service.accounts().containers()
        return self._list_all(
            lambda token: containers.list(parent=parent, pageToken=token)
            if token
            else containers.list(parent=parent),
            "container",
        )

    def get_container(self, account_id: str, container_id: str) -> dict[str, Any]:
        path = container_path(account_id, container_id)
        return self._execute(lambda: self.service.accounts().containers().get(path=path))

    def resolve_container_identifier(self, identifier: str) -> dict[str, Any]:
        parameter, normalized = parse_container_identifier(identifier)
        containers = self.service.accounts().containers()
        return self._execute(lambda: containers.lookup(**{parameter: normalized}))

    def list_workspaces(self, account_id: str, container_id: str) -> list[dict[str, Any]]:
        parent = container_path(account_id, container_id)
        workspaces = self.service.accounts().containers().workspaces()
        return self._list_all(
            lambda token: workspaces.list(parent=parent, pageToken=token)
            if token
            else workspaces.list(parent=parent),
            "workspace",
        )

    def create_workspace(
        self, account_id: str, container_id: str, name: str, description: str = ""
    ) -> dict[str, Any]:
        parent = container_path(account_id, container_id)
        body = {"name": _name(name), "description": description}
        return self._execute(
            lambda: self.service.accounts().containers().workspaces().create(
                parent=parent, body=body
            )
        )

    def workspace_status(
        self, account_id: str, container_id: str, workspace_id: str
    ) -> dict[str, Any]:
        path = workspace_path(account_id, container_id, workspace_id)
        return self._execute(
            lambda: self.service.accounts().containers().workspaces().getStatus(path=path)
        )

    def sync_workspace(
        self, account_id: str, container_id: str, workspace_id: str
    ) -> dict[str, Any]:
        path = workspace_path(account_id, container_id, workspace_id)
        return self._execute(
            lambda: self.service.accounts().containers().workspaces().sync(path=path)
        )

    def list_entities(
        self, kind: str, account_id: str, container_id: str, workspace_id: str
    ) -> list[dict[str, Any]]:
        resource, response_key = self._workspace_resource(kind)
        parent = workspace_path(account_id, container_id, workspace_id)
        api = resource()
        return self._list_all(
            lambda token: api.list(parent=parent, pageToken=token)
            if token
            else api.list(parent=parent),
            response_key,
        )

    def get_entity(
        self,
        kind: str,
        account_id: str,
        container_id: str,
        workspace_id: str,
        entity_id: str,
    ) -> dict[str, Any]:
        resource, _ = self._workspace_resource(kind)
        entity_id = _id(entity_id, f"{kind}_id")
        path = f"{workspace_path(account_id, container_id, workspace_id)}/{kind}/{entity_id}"
        return self._execute(lambda: resource().get(path=path))

    def create_entity(
        self,
        kind: str,
        account_id: str,
        container_id: str,
        workspace_id: str,
        body: dict[str, Any],
    ) -> dict[str, Any]:
        _validate_body(kind, body)
        resource, _ = self._workspace_resource(kind)
        parent = workspace_path(account_id, container_id, workspace_id)
        return self._execute(lambda: resource().create(parent=parent, body=body))

    def update_entity(
        self,
        kind: str,
        account_id: str,
        container_id: str,
        workspace_id: str,
        entity_id: str,
        body: dict[str, Any],
        fingerprint: str | None = None,
    ) -> dict[str, Any]:
        _validate_body(kind, body, update=True)
        resource, _ = self._workspace_resource(kind)
        entity_id = _id(entity_id, f"{kind}_id")
        path = f"{workspace_path(account_id, container_id, workspace_id)}/{kind}/{entity_id}"
        kwargs: dict[str, Any] = {"path": path, "body": body}
        if fingerprint:
            kwargs["fingerprint"] = fingerprint
        return self._execute(lambda: resource().update(**kwargs))

    def delete_entity(
        self,
        kind: str,
        account_id: str,
        container_id: str,
        workspace_id: str,
        entity_id: str,
    ) -> dict[str, Any]:
        resource, _ = self._workspace_resource(kind)
        entity_id = _id(entity_id, f"{kind}_id")
        path = f"{workspace_path(account_id, container_id, workspace_id)}/{kind}/{entity_id}"
        return self._execute(lambda: resource().delete(path=path))

    def list_versions(self, account_id: str, container_id: str) -> list[dict[str, Any]]:
        parent = container_path(account_id, container_id)
        headers = self.service.accounts().containers().version_headers()
        return self._list_all(
            lambda token: headers.list(parent=parent, pageToken=token)
            if token
            else headers.list(parent=parent),
            "containerVersionHeader",
        )

    def create_version(
        self,
        account_id: str,
        container_id: str,
        workspace_id: str,
        name: str = "",
        notes: str = "",
    ) -> dict[str, Any]:
        path = workspace_path(account_id, container_id, workspace_id)
        body = {key: value for key, value in {"name": name, "notes": notes}.items() if value}
        return self._execute(
            lambda: self.service.accounts().containers().workspaces().create_version(
                path=path, body=body
            )
        )

    def publish_version(
        self, account_id: str, container_id: str, version_id: str
    ) -> dict[str, Any]:
        version_id = _id(version_id, "version_id")
        path = f"{container_path(account_id, container_id)}/versions/{version_id}"
        return self._execute(
            lambda: self.service.accounts().containers().versions().publish(path=path)
        )

    def list_builtin_variables(
        self, account_id: str, container_id: str, workspace_id: str
    ) -> list[dict[str, Any]]:
        parent = workspace_path(account_id, container_id, workspace_id)
        resource = self.service.accounts().containers().workspaces().built_in_variables()
        return self._list_all(
            lambda token: resource.list(parent=parent, pageToken=token)
            if token
            else resource.list(parent=parent),
            "builtInVariable",
        )

    def enable_builtin_variables(
        self,
        account_id: str,
        container_id: str,
        workspace_id: str,
        variable_types: list[str],
    ) -> list[dict[str, Any]]:
        parent = workspace_path(account_id, container_id, workspace_id)
        resource = self.service.accounts().containers().workspaces().built_in_variables()
        response = self._execute(
            lambda: resource.create(parent=parent, type=variable_types)
        )
        return response.get("builtInVariable", [])

    def disable_builtin_variables(
        self,
        account_id: str,
        container_id: str,
        workspace_id: str,
        variable_types: list[str],
    ) -> dict[str, Any]:
        path = workspace_path(account_id, container_id, workspace_id)
        resource = self.service.accounts().containers().workspaces().built_in_variables()
        return self._execute(lambda: resource.delete(path=path, type=variable_types))

    def _workspace_resource(self, kind: str) -> tuple[Callable[[], Any], str]:
        allowed = {
            "tags": "tag",
            "triggers": "trigger",
            "variables": "variable",
            "folders": "folder",
        }
        if kind not in allowed:
            raise ValueError(f"unsupported entity kind: {kind}")
        workspaces = self.service.accounts().containers().workspaces()
        return getattr(workspaces, kind), allowed[kind]


def _name(value: str) -> str:
    value = value.strip()
    if not value or len(value) > 256:
        raise ValueError("name must contain 1 to 256 characters")
    return value


def parse_container_identifier(identifier: str) -> tuple[str, str]:
    normalized = identifier.strip().upper()
    if not re.fullmatch(r"[A-Z]{1,5}-[A-Z0-9]+", normalized):
        raise ValueError(
            "identifier must be a public tag or destination ID such as GTM-W525XW4 or "
            "AW-123456789"
        )
    parameter = "tagId" if normalized.startswith("GTM-") else "destinationId"
    return parameter, normalized


def _validate_body(kind: str, body: dict[str, Any], *, update: bool = False) -> None:
    if not isinstance(body, dict) or not body:
        raise ValueError("body must be a non-empty JSON object")
    if "name" in body:
        _name(str(body["name"]))
    elif not update:
        raise ValueError(f"{kind} body requires a name")
    if kind in {"tags", "triggers", "variables"} and not update and not body.get("type"):
        raise ValueError(f"{kind} body requires a type")


def _is_quota_error(exc: HttpError, message: str) -> bool:
    status = getattr(getattr(exc, "resp", None), "status", None)
    lowered = message.lower()
    quota_message = any(
        marker in lowered
        for marker in ("quota", "rate limit", "ratelimit", "resource_exhausted")
    )
    return status == 429 or (status == 403 and quota_message)


def _retry_after(exc: HttpError) -> float | None:
    response = getattr(exc, "resp", None)
    if response is None:
        return None
    value = response.get("retry-after")
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return None
