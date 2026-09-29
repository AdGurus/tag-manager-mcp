"""FastMCP tools for Google Tag Manager."""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Literal

from mcp.server.fastmcp import FastMCP

from .client import GTMClient
from .debug_log import call_id_context, get_debug_logger

logging.basicConfig(
    level=os.getenv("GTM_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


class LoggingFastMCP(FastMCP):
    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        debug_log = get_debug_logger()
        call_id = debug_log.new_call_id()
        token = call_id_context.set(call_id)
        started = time.monotonic()
        try:
            result = await super().call_tool(name, arguments)
            debug_log.log(
                kind="mcp_tool",
                name=name,
                request=arguments,
                response=result,
                status="success",
                duration_ms=(time.monotonic() - started) * 1000,
                call_id=call_id,
            )
            return result
        except Exception as exc:
            debug_log.log(
                kind="mcp_tool",
                name=name,
                request=arguments,
                status="error",
                duration_ms=(time.monotonic() - started) * 1000,
                error=exc,
                call_id=call_id,
            )
            raise
        finally:
            call_id_context.reset(token)


SERVER_INSTRUCTIONS = """
When a user identifies a container with a public ID such as GTM-W525XW4, always call
resolve_container_identifier first. Use the returned account_id and container_id for subsequent
tools. Never scan every account to locate a public identifier. Call list_workspaces before any
workspace-scoped operation; never assume workspace ID 1.
"""

mcp = LoggingFastMCP("Google Tag Manager", instructions=SERVER_INSTRUCTIONS)
_client: GTMClient | None = None
EntityKind = Literal["tags", "triggers", "variables", "folders"]


def get_client() -> GTMClient:
    global _client
    if _client is None:
        _client = GTMClient()
    return _client


@mcp.tool()
def auth_status() -> dict[str, Any]:
    """Verify configured credentials and return the accessible GTM accounts."""
    client = get_client()
    accounts = client.list_accounts()
    return {
        "ok": True,
        "auth_method": client.auth_method,
        "quota": client.quota_guard.status(),
        "accounts": accounts,
    }


@mcp.tool()
def quota_status() -> dict[str, Any]:
    """Return the local GTM request budget and rate-limit settings without using API quota."""
    return get_client().quota_guard.status()


@mcp.tool()
def debug_log_status() -> dict[str, Any]:
    """Return SQLite debug-log status and event counts."""
    return get_debug_logger().status()


@mcp.tool()
def debug_log_recent(
    limit: int = 50, kind: Literal["mcp_tool", "gtm_api"] | None = None, status: str | None = None
) -> list[dict[str, Any]]:
    """Return recent sanitized debug events. Limit is constrained to 1-500 rows."""
    return get_debug_logger().recent(limit=limit, kind=kind, status=status)


@mcp.tool()
def list_accounts() -> list[dict[str, Any]]:
    """List Google Tag Manager accounts accessible to the authenticated identity."""
    return get_client().list_accounts()


@mcp.tool()
def list_containers(account_id: str) -> list[dict[str, Any]]:
    """List containers in a GTM account."""
    return get_client().list_containers(account_id)


@mcp.tool()
def get_container(account_id: str, container_id: str) -> dict[str, Any]:
    """Get one GTM container."""
    return get_client().get_container(account_id, container_id)


@mcp.tool()
def resolve_container_identifier(identifier: str) -> dict[str, Any]:
    """Resolve GTM-* or destination IDs to the owning account and internal container IDs."""
    container = get_client().resolve_container_identifier(identifier)
    return {
        "identifier": identifier.strip().upper(),
        "account_id": container.get("accountId"),
        "container_id": container.get("containerId"),
        "container": container,
        "next_step": "Call list_workspaces with account_id and container_id before editing.",
    }


@mcp.tool()
def list_workspaces(account_id: str, container_id: str) -> list[dict[str, Any]]:
    """List workspaces in a GTM container. Never assume workspace ID 1."""
    return get_client().list_workspaces(account_id, container_id)


@mcp.tool()
def create_workspace(
    account_id: str, container_id: str, name: str, description: str = ""
) -> dict[str, Any]:
    """Create an isolated GTM workspace for changes."""
    return get_client().create_workspace(account_id, container_id, name, description)


@mcp.tool()
def get_workspace_status(
    account_id: str, container_id: str, workspace_id: str
) -> dict[str, Any]:
    """Return pending changes and merge conflicts for a workspace."""
    return get_client().workspace_status(account_id, container_id, workspace_id)


@mcp.tool()
def sync_workspace(
    account_id: str, container_id: str, workspace_id: str
) -> dict[str, Any]:
    """Synchronize a workspace with the latest container version and report conflicts."""
    return get_client().sync_workspace(account_id, container_id, workspace_id)


@mcp.tool()
def list_entities(
    kind: EntityKind, account_id: str, container_id: str, workspace_id: str
) -> list[dict[str, Any]]:
    """List tags, triggers, variables, or folders in a workspace."""
    return get_client().list_entities(kind, account_id, container_id, workspace_id)


@mcp.tool()
def get_entity(
    kind: EntityKind,
    account_id: str,
    container_id: str,
    workspace_id: str,
    entity_id: str,
) -> dict[str, Any]:
    """Get one tag, trigger, variable, or folder by ID."""
    return get_client().get_entity(kind, account_id, container_id, workspace_id, entity_id)


@mcp.tool()
def create_entity(
    kind: EntityKind,
    account_id: str,
    container_id: str,
    workspace_id: str,
    body: dict[str, Any],
) -> dict[str, Any]:
    """Create a tag, trigger, variable, or folder from a GTM API v2 JSON body."""
    return get_client().create_entity(kind, account_id, container_id, workspace_id, body)


@mcp.tool()
def update_entity(
    kind: EntityKind,
    account_id: str,
    container_id: str,
    workspace_id: str,
    entity_id: str,
    body: dict[str, Any],
    fingerprint: str | None = None,
) -> dict[str, Any]:
    """Update a tag, trigger, variable, or folder. Supply fingerprint for optimistic locking."""
    return get_client().update_entity(
        kind, account_id, container_id, workspace_id, entity_id, body, fingerprint
    )


@mcp.tool()
def delete_entity(
    kind: EntityKind,
    account_id: str,
    container_id: str,
    workspace_id: str,
    entity_id: str,
    confirm: bool = False,
) -> dict[str, Any]:
    """Delete an entity. Requires confirm=true because this operation is destructive."""
    if not confirm:
        return {"ok": False, "message": "Deletion requires confirm=true."}
    result = get_client().delete_entity(kind, account_id, container_id, workspace_id, entity_id)
    return {"ok": True, "result": result}


@mcp.tool()
def list_builtin_variables(
    account_id: str, container_id: str, workspace_id: str
) -> list[dict[str, Any]]:
    """List enabled built-in variables in a workspace."""
    return get_client().list_builtin_variables(account_id, container_id, workspace_id)


@mcp.tool()
def enable_builtin_variables(
    account_id: str,
    container_id: str,
    workspace_id: str,
    variable_types: list[str],
) -> list[dict[str, Any]]:
    """Enable one or more GTM built-in variable types."""
    if not variable_types:
        raise ValueError("variable_types cannot be empty")
    return get_client().enable_builtin_variables(
        account_id, container_id, workspace_id, variable_types
    )


@mcp.tool()
def disable_builtin_variables(
    account_id: str,
    container_id: str,
    workspace_id: str,
    variable_types: list[str],
    confirm: bool = False,
) -> dict[str, Any]:
    """Disable built-in variable types. Requires confirm=true."""
    if not confirm:
        return {"ok": False, "message": "Disabling variables requires confirm=true."}
    if not variable_types:
        raise ValueError("variable_types cannot be empty")
    result = get_client().disable_builtin_variables(
        account_id, container_id, workspace_id, variable_types
    )
    return {"ok": True, "result": result}


@mcp.tool()
def list_versions(account_id: str, container_id: str) -> list[dict[str, Any]]:
    """List version headers for a GTM container."""
    return get_client().list_versions(account_id, container_id)


@mcp.tool()
def create_version(
    account_id: str,
    container_id: str,
    workspace_id: str,
    name: str = "",
    notes: str = "",
) -> dict[str, Any]:
    """Create an unpublished version after checking the workspace for changes and conflicts."""
    client = get_client()
    status = client.workspace_status(account_id, container_id, workspace_id)
    if status.get("mergeConflict"):
        return {"ok": False, "message": "Workspace has merge conflicts.", "status": status}
    if not status.get("workspaceChange"):
        return {"ok": False, "message": "Workspace has no pending changes.", "status": status}
    result = client.create_version(account_id, container_id, workspace_id, name, notes)
    return {"ok": True, "result": result}


@mcp.tool()
def publish_version(
    account_id: str, container_id: str, version_id: str, confirm: bool = False
) -> dict[str, Any]:
    """Publish a container version live. Requires confirm=true."""
    if not confirm:
        return {
            "ok": False,
            "message": "Publishing makes changes live and requires confirm=true.",
        }
    result = get_client().publish_version(account_id, container_id, version_id)
    return {"ok": True, "result": result}


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
