# Google Tag Manager MCP (Python)

An internal-use MCP server for managing Google Tag Manager through the Tag Manager API v2.
It uses your Google user identity by default, so it can access the GTM accounts already shared
with you. A service account can be configured as a fallback.

The server exposes account/container discovery, workspace isolation and synchronization, CRUD
for tags/triggers/variables/folders, built-in variables, container versions, and guarded publishing.
It uses the raw GTM API JSON shape for entity bodies so it does not hide advanced GTM options.

## Requirements

- Python 3.10+
- A Google Cloud project with **Tag Manager API** enabled
- For user authentication: an OAuth 2.0 **Desktop app** client
- For fallback authentication: a service account that has been added as a GTM user

## Install

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
```

## Preferred authentication: your Google account

1. In Google Cloud Console, enable the Tag Manager API.
2. Configure the OAuth consent screen. For internal use, choose **Internal** when your Google
   Workspace organization permits it; otherwise add your account as a test user.
3. Create an OAuth client ID with application type **Desktop app** and download its JSON file.
4. Set the two paths and authorize once:

```bash
export GTM_AUTH_MODE=user
export GTM_OAUTH_CLIENT_SECRETS=/absolute/path/to/client_secret.json
export GTM_OAUTH_TOKEN_FILE="$HOME/.config/gtm-mcp/token.json"
.venv/bin/tag-manager-auth
```

The local callback binds only to `127.0.0.1`. The refresh token is written with mode `0600`.
Do not put either credential file in this repository.

After the first authorization, `GTM_OAUTH_CLIENT_SECRETS` is only needed if the saved grant is
removed or revoked. Keep `GTM_OAUTH_TOKEN_FILE` configured in the MCP client environment.

## Service-account fallback

Add the service account email under **Admin > Account User Management** or **Container User
Management** in GTM, then configure one of:

```bash
export GTM_AUTH_MODE=service-account
export GTM_SERVICE_ACCOUNT_FILE=/absolute/path/to/service-account.json
```

or, for a secret-injected environment:

```bash
export GTM_SERVICE_ACCOUNT_JSON='{"type":"service_account", ...}'
```

With `GTM_AUTH_MODE=auto` (the default), resolution order is:

1. Existing user OAuth token (or interactive OAuth when explicitly run through `tag-manager-auth`)
2. `GTM_SERVICE_ACCOUNT_JSON`
3. `GTM_SERVICE_ACCOUNT_FILE` / `GOOGLE_APPLICATION_CREDENTIALS`

Application Default Credentials are also supported explicitly with `GTM_AUTH_MODE=adc`.

## MCP client configuration

Run `pwd` and replace `/absolute/path/to/tag-manager-mcp` below.

```json
{
  "mcpServers": {
    "tag-manager-local": {
      "command": "/absolute/path/to/tag-manager-mcp/.venv/bin/tag-manager-mcp",
      "args": [],
      "env": {
        "GTM_AUTH_MODE": "auto",
        "GTM_OAUTH_TOKEN_FILE": "/Users/you/.config/gtm-mcp/token.json",
        "GTM_SERVICE_ACCOUNT_FILE": "/optional/fallback/service-account.json"
      }
    }
  }
}
```

The server uses stdio transport. Logs go to stderr and will not corrupt MCP messages.

## Quota guardrails

Google publishes a limit of 10,000 requests per project per day and 0.25 QPS, enforced as 25
requests in a rolling 100-second window. The server therefore defaults to:

- At least 4.1 seconds between API requests, coordinated across local MCP processes.
- A persistent 9,000-request daily safety budget, leaving 10% headroom.
- Up to three retries for `429` and quota-related `403` responses, using `Retry-After` when
  supplied or exponential backoff with jitter otherwise.
- No automatic retry for non-quota failures, avoiding accidental duplicate writes.

State is stored at `~/.config/gtm-mcp/quota-state.json`. Inspect it through the zero-cost
`quota_status` MCP tool. Configure the behavior with `GTM_MIN_REQUEST_INTERVAL_SECONDS`,
`GTM_DAILY_REQUEST_BUDGET`, `GTM_QUOTA_MAX_RETRIES`, `GTM_QUOTA_BACKOFF_SECONDS`, and
`GTM_QUOTA_STATE_FILE`.

## SQLite debug log

Every MCP tool invocation and outbound GTM API attempt is written to `debug.db` in the project
root by the supplied `.env` configuration. Related MCP and API rows share a `call_id`. Rows include sanitized
inputs and outputs, request method/resource, duration, HTTP status, retry attempt, process ID, and
error details. OAuth tokens, authorization values, client secrets, private keys, refresh tokens,
and credential objects are redacted. Payloads are capped at 256 KiB per field.

The database uses WAL mode for concurrent Codex and Claude Desktop processes and automatically
removes rows older than 30 days. Use `debug_log_status` and `debug_log_recent` to inspect it through
MCP, or open the database directly with SQLite. Logging is best-effort and cannot block GTM calls.

Configure it with `GTM_DEBUG_LOG_ENABLED`, `GTM_DEBUG_DB_PATH`, `GTM_DEBUG_RETENTION_DAYS`, and
`GTM_DEBUG_MAX_PAYLOAD_BYTES`.

## Tools

| Tool | Purpose |
|---|---|
| `auth_status`, `quota_status`, `debug_log_status`, `debug_log_recent` | Auth and diagnostics |
| `list_accounts` | Discover accessible accounts |
| `resolve_container_identifier` | Resolve `GTM-*`/destination IDs to account and container IDs |
| `list_containers`, `get_container` | Container discovery |
| `list_workspaces`, `create_workspace` | Workspace discovery and creation |
| `get_workspace_status`, `sync_workspace` | Check changes/conflicts and synchronize |
| `list_entities`, `get_entity` | Read tags, triggers, variables, or folders |
| `create_entity`, `update_entity` | Create or update those resources from API JSON |
| `delete_entity` | Delete an entity; requires `confirm=true` |
| `list_builtin_variables`, `enable_builtin_variables`, `disable_builtin_variables` | Built-in variable management |
| `list_versions`, `create_version` | Inspect and snapshot versions |
| `publish_version` | Publish live; requires `confirm=true` |

Always call `list_workspaces`; do not assume the workspace ID is `1`. For updates, first read the
entity and pass its `fingerprint` to prevent overwriting a concurrent edit.

When only a public identifier is known, call `resolve_container_identifier("GTM-W525XW4")`. It
uses Google's direct container lookup endpoint and returns `account_id` plus `container_id` without
scanning every accessible account. The server instructions explicitly direct MCP clients to use
this resolution workflow automatically.

### Example tag body

`create_entity(kind="tags", ...)` accepts the documented GTM v2 tag resource body:

```json
{
  "name": "GA4 - generate_lead",
  "type": "gaawe",
  "parameter": [
    {"type": "template", "key": "eventName", "value": "generate_lead"},
    {"type": "tagReference", "key": "measurementId", "value": "{{GA4 Measurement ID}}"}
  ],
  "firingTriggerId": ["42"]
}
```

GTM tag and parameter type codes vary by template. Read a comparable existing entity first or use
the Tag Manager API reference instead of guessing the payload.

## Development

```bash
.venv/bin/pytest
.venv/bin/ruff check .
```

## Design references

This implementation was informed by:

- [neep305/mcp-for-gtm](https://github.com/neep305/mcp-for-gtm), particularly its Python,
  FastMCP, and installed-app OAuth approach.
- [paolobietolini/gtm-mcp-server](https://github.com/paolobietolini/gtm-mcp-server), particularly
  its wider API surface, service-account mode, workspace conflict checks, and explicit confirmation
  before publishing.

The code in this repository is a new Python implementation rather than a mechanical port.
