# mcp-server-dash

A Model Context Protocol (MCP) server that provides tools to interact with the [Dash](https://kapeli.com/dash) documentation browser API.

Dash 8 is required. You can download Dash 8 at https://blog.kapeli.com/dash-8.

<a href="https://glama.ai/mcp/servers/@Kapeli/dash-mcp-server">
  <img width="380" height="200" src="https://glama.ai/mcp/servers/@Kapeli/dash-mcp-server/badge" alt="Dash Server MCP server" />
</a>

## Overview

The Dash MCP server provides tools for accessing and searching documentation directly from Dash, the macOS documentation browser. MCP clients can:

- List installed docsets
- Search across docsets and code snippets
- Fetch documentation content via Dash load URLs (with pagination)
- Enable full-text search for specific docsets

### Notice

This is a work in progress. Any suggestions are welcome!

## Tools

1. **list_installed_docsets**
   - Lists all installed documentation sets in Dash
2. **search_documentation**
   - Searches across docsets and snippets
3. **fetch_documentation**
   - Fetches raw documentation content using the `load_url` from search results (supports chunking for large pages)
4. **enable_docset_fts**
   - Enables full-text search for a specific docset

## Resources

- `dash://server-info` — server runtime/config info (JSON)

## Requirements

- macOS (required for Dash app)
- [Dash](https://kapeli.com/dash) installed
- Python 3.11.4 or higher
- uv

## Server configuration

Environment variables for the server process:

- `DASH_MCP_TRANSPORT`: `stdio` (default), `sse`, or `streamable-http`
- `DASH_MCP_HOST`: bind address (default `127.0.0.1`)
- `DASH_MCP_PORT`: bind port (default `8000`)
- `DASH_MCP_LOG_LEVEL`: `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`
- `DASH_MCP_HTTP_COMPAT`: `true`/`false` (default `true`) - relaxes Accept checks and prefers JSON/stateless HTTP
- `DASH_MCP_JSON_RESPONSE`: `true`/`false` (default `true` when `DASH_MCP_HTTP_COMPAT` is on)
- `DASH_MCP_STATELESS_HTTP`: `true`/`false` (default `true` when `DASH_MCP_HTTP_COMPAT` is on)
- `DASH_MCP_DNS_REBINDING`: `true`/`false` (default `false`)
- `DASH_MCP_ALLOWED_HOSTS`: comma-separated Host header allowlist
- `DASH_MCP_ALLOWED_ORIGINS`: comma-separated Origin allowlist
- `DASH_MCP_SHUTDOWN_TIMEOUT`: graceful shutdown timeout in seconds (default `3`)
- `DASH_MCP_KEEP_ALIVE_TIMEOUT`: keep-alive timeout in seconds (optional)
- `DASH_MCP_HTTP_LOG`: `true`/`false` (default `false`) - log incoming HTTP JSON-RPC bodies
- `DASH_MCP_HTTP_LOG_MAX`: max log length for HTTP bodies (default `2000`)
- `DASH_MCP_SSE_CLIENT_TTL`: seconds to prefer SSE for clients that opened `GET /mcp` (default `60`)
- `DASH_MCP_PREFER_SSE`: `true`/`false` (default `false`) - prefer SSE when client accepts both JSON and SSE
- `DASH_MCP_SSE_CLIENT_NAMES`: comma-separated client names (from initialize) that should prefer SSE responses
- `DASH_MCP_DASH_BASE_URL`: override host/port used in returned Dash `load_url` values (e.g. `http://host:port`)
- `DASH_MCP_DOC_CHUNK_BYTES`: default max bytes returned by `fetch_documentation` (default `120000`)
- `DASH_MCP_DOC_CHUNK_MAX_BYTES`: hard cap for `fetch_documentation` max bytes (default `1000000`)
- `DASH_MCP_ALLOWED_DASH_HOSTS`: comma-separated allowlist for `load_url` hosts (`*` to allow any; default: `127.0.0.1,localhost,::1`)

If you set `DASH_MCP_ALLOWED_HOSTS` or `DASH_MCP_ALLOWED_ORIGINS`, DNS rebinding
protection is enabled automatically unless `DASH_MCP_DNS_REBINDING=false`.
Use `*` in either allowlist to disable the check and allow any host/origin.
When `DASH_MCP_HTTP_COMPAT` is enabled, the server accepts `POST` requests that only
declare `Accept: application/json`, suppresses session IDs by default, and prefers
JSON responses over SSE to improve client compatibility. It also accepts legacy
method names like `listResources`/`listTools` and maps them to the MCP spec names.
For no-params methods, empty list or empty string params are normalized to `{}`.
If a client opens `GET /mcp`, the server can prefer SSE responses for that client
for a short time window when `DASH_MCP_PREFER_SSE=true`.
You can also force SSE for specific clients by setting `DASH_MCP_SSE_CLIENT_NAMES`
to their `clientInfo.name` values (e.g. `mcp-router`).

`dash://server-info` includes a `build` section with `code_sha256` and `git_commit`
to help identify exactly which server build is running.

## Configuration

### Using uvx

```bash
brew install uv
```

#### in `claude_desktop_config.json`

```json
{
  "mcpServers": {
      "dash-api": {
          "command": "uvx",
          "args": [
              "--from",
              "git+https://github.com/Kapeli/dash-mcp-server.git",
              "dash-mcp-server"
          ]
      }
  }
}
```

#### in `Claude Code`

```bash
claude mcp add dash-api -- uvx --from "git+https://github.com/Kapeli/dash-mcp-server.git" "dash-mcp-server"
```
