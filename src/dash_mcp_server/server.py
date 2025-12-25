from typing import Optional
import os
import importlib.metadata as metadata
import anyio
from contextlib import asynccontextmanager
import httpx
import subprocess
import json
import hashlib
import logging
import contextvars
import time
from uuid import uuid4
from http import HTTPStatus
from pathlib import Path
from urllib.parse import urlparse, parse_qs
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.server.fastmcp import Context
from mcp.server.streamable_http import (
    StreamableHTTPServerTransport,
    GET_STREAM_KEY,
    EventMessage,
    JSONRPCResponse,
    JSONRPCError,
    CONTENT_TYPE_JSON,
    MCP_SESSION_ID_HEADER,
    Response,
    logger as _http_logger,
)
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.shared.message import ServerMessageMetadata, SessionMessage
from starlette.requests import Request
from pydantic import BaseModel, Field

def _parse_env_bool(value: str | None) -> Optional[bool]:
    if value is None:
        return None
    value = value.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    return None


def _parse_csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def _parse_env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _parse_env_bool_with_default(name: str, default: bool) -> bool:
    value = _parse_env_bool(os.getenv(name))
    if value is None:
        return default
    return value


def _parse_env_int_optional(name: str) -> Optional[int]:
    raw = os.getenv(name)
    if not raw:
        return None
    raw = raw.strip().lower()
    if raw in {"none", "null"}:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _normalize_log_level(value: str | None) -> str:
    if not value:
        return "INFO"
    value = value.strip().upper()
    if value in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
        return value
    return "INFO"


def _transport_security_settings() -> TransportSecuritySettings:
    enabled_env = _parse_env_bool(os.getenv("DASH_MCP_DNS_REBINDING"))
    allowed_hosts = _parse_csv(os.getenv("DASH_MCP_ALLOWED_HOSTS"))
    allowed_origins = _parse_csv(os.getenv("DASH_MCP_ALLOWED_ORIGINS"))
    allow_any = "*" in allowed_hosts or "*" in allowed_origins

    if allow_any:
        return TransportSecuritySettings(enable_dns_rebinding_protection=False)

    if enabled_env is None and not allowed_hosts and not allowed_origins:
        return TransportSecuritySettings(enable_dns_rebinding_protection=False)

    return TransportSecuritySettings(
        enable_dns_rebinding_protection=enabled_env if enabled_env is not None else True,
        allowed_hosts=allowed_hosts,
        allowed_origins=allowed_origins,
    )


_MCP_HOST = os.getenv("DASH_MCP_HOST", "127.0.0.1")
_MCP_PORT = _parse_env_int("DASH_MCP_PORT", 8000)
_MCP_LOG_LEVEL = _normalize_log_level(os.getenv("DASH_MCP_LOG_LEVEL"))
_MCP_HTTP_COMPAT = _parse_env_bool_with_default("DASH_MCP_HTTP_COMPAT", True)
_MCP_JSON_RESPONSE = _parse_env_bool(os.getenv("DASH_MCP_JSON_RESPONSE"))
if _MCP_JSON_RESPONSE is None:
    _MCP_JSON_RESPONSE = _MCP_HTTP_COMPAT
_MCP_STATELESS_HTTP = _parse_env_bool(os.getenv("DASH_MCP_STATELESS_HTTP"))
if _MCP_STATELESS_HTTP is None:
    _MCP_STATELESS_HTTP = False
_MCP_TRANSPORT_SECURITY = _transport_security_settings()
_MCP_SHUTDOWN_TIMEOUT = _parse_env_int("DASH_MCP_SHUTDOWN_TIMEOUT", 3)
_MCP_KEEP_ALIVE_TIMEOUT = _parse_env_int_optional("DASH_MCP_KEEP_ALIVE_TIMEOUT")
_MCP_HTTP_LOG = _parse_env_bool_with_default("DASH_MCP_HTTP_LOG", False)
_MCP_HTTP_LOG_MAX = _parse_env_int("DASH_MCP_HTTP_LOG_MAX", 2000)
_MCP_HTTP_LOG_RESPONSE = _parse_env_bool_with_default("DASH_MCP_HTTP_LOG_RESPONSE", False)
_MCP_STRIP_TOOL_OUTPUT_SCHEMA = _parse_env_bool(os.getenv("DASH_MCP_STRIP_TOOL_OUTPUT_SCHEMA"))
if _MCP_STRIP_TOOL_OUTPUT_SCHEMA is None:
    _MCP_STRIP_TOOL_OUTPUT_SCHEMA = _MCP_HTTP_COMPAT
_MCP_SSE_CLIENT_TTL = _parse_env_int("DASH_MCP_SSE_CLIENT_TTL", 60)
_MCP_PREFER_SSE = _parse_env_bool_with_default("DASH_MCP_PREFER_SSE", False)
_MCP_SSE_CLIENT_NAMES = [name.lower() for name in _parse_csv(os.getenv("DASH_MCP_SSE_CLIENT_NAMES"))]
_MCP_SESSION_TTL = _parse_env_int("DASH_MCP_SESSION_TTL", 300)
_MCP_STATELESS_ON_NO_SESSION = _parse_env_bool(os.getenv("DASH_MCP_STATELESS_ON_NO_SESSION"))
if _MCP_STATELESS_ON_NO_SESSION is None:
    _MCP_STATELESS_ON_NO_SESSION = True
_MCP_ROUTE_RESPONSES_TO_GET = _parse_env_bool(os.getenv("DASH_MCP_ROUTE_RESPONSES_TO_GET"))
_MCP_DISABLE_SESSION_HEADER = _parse_env_bool_with_default(
    "DASH_MCP_DISABLE_SESSION_HEADER",
    True,
)
_MCP_CONTENT_REF_TTL = _parse_env_int("DASH_MCP_CONTENT_REF_TTL", 3600)
_DOC_CHUNK_BYTES = _parse_env_int("DASH_MCP_DOC_CHUNK_BYTES", 120_000)
_DOC_CHUNK_MAX_BYTES = _parse_env_int("DASH_MCP_DOC_CHUNK_MAX_BYTES", 1_000_000)
_DASH_LOAD_URL_BASE = os.getenv("DASH_MCP_DASH_BASE_URL")
if _DASH_LOAD_URL_BASE:
    _DASH_LOAD_URL_BASE = _DASH_LOAD_URL_BASE.strip().rstrip("/")
_ALLOWED_DASH_LOAD_HOSTS = _parse_csv(os.getenv("DASH_MCP_ALLOWED_DASH_HOSTS"))
if not _ALLOWED_DASH_LOAD_HOSTS:
    _ALLOWED_DASH_LOAD_HOSTS = ["127.0.0.1", "localhost", "::1"]
try:
    _SERVER_VERSION = metadata.version("dash-mcp-server")
except metadata.PackageNotFoundError:
    _SERVER_VERSION = "unknown"
_SERVER_FILE = Path(__file__).resolve()


def _hash_file(path: Path) -> Optional[str]:
    try:
        data = path.read_bytes()
    except OSError:
        return None
    return hashlib.sha256(data).hexdigest()


def _find_git_root(start: Path) -> Optional[Path]:
    for root in [start] + list(start.parents):
        if (root / ".git").exists():
            return root
    return None


def _resolve_git_commit(path: Path) -> Optional[str]:
    git_root = _find_git_root(path)
    if not git_root:
        return None
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=git_root,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


_SERVER_CODE_HASH = _hash_file(_SERVER_FILE)
_SERVER_GIT_COMMIT = _resolve_git_commit(_SERVER_FILE)

mcp = FastMCP(
    "Dash Documentation API",
    host=_MCP_HOST,
    port=_MCP_PORT,
    log_level=_MCP_LOG_LEVEL,
    json_response=_MCP_JSON_RESPONSE,
    stateless_http=_MCP_STATELESS_HTTP,
    transport_security=_MCP_TRANSPORT_SECURITY,
)


@mcp.resource(
    "dash://server-info",
    name="server-info",
    title="Dash MCP Server Info",
    description="Runtime and config info for the Dash MCP server",
    mime_type="application/json",
)
async def server_info() -> str:
    info = {
        "name": "Dash Documentation API",
        "version": _SERVER_VERSION,
        "config": {
            "host": _MCP_HOST,
            "port": _MCP_PORT,
            "log_level": _MCP_LOG_LEVEL,
            "json_response": _MCP_JSON_RESPONSE,
            "stateless_http": _MCP_STATELESS_HTTP,
            "shutdown_timeout": _MCP_SHUTDOWN_TIMEOUT,
            "keep_alive_timeout": _MCP_KEEP_ALIVE_TIMEOUT,
            "http_compat": _MCP_HTTP_COMPAT,
            "http_log": _MCP_HTTP_LOG,
            "http_log_max": _MCP_HTTP_LOG_MAX,
            "http_log_response": _MCP_HTTP_LOG_RESPONSE,
            "strip_tool_output_schema": _MCP_STRIP_TOOL_OUTPUT_SCHEMA,
            "sse_client_ttl": _MCP_SSE_CLIENT_TTL,
            "prefer_sse": _MCP_PREFER_SSE,
            "sse_client_names": _MCP_SSE_CLIENT_NAMES,
            "session_ttl": _MCP_SESSION_TTL,
            "stateless_on_no_session": _MCP_STATELESS_ON_NO_SESSION,
            "route_responses_to_get": _MCP_ROUTE_RESPONSES_TO_GET,
            "route_responses_to_get_auto": _MCP_ROUTE_RESPONSES_TO_GET is None,
            "disable_session_header": _MCP_DISABLE_SESSION_HEADER,
            "dash_load_url_base": _DASH_LOAD_URL_BASE,
        },
        "build": {
            "code_sha256": _SERVER_CODE_HASH,
            "git_commit": _SERVER_GIT_COMMIT,
            "module_path": str(_SERVER_FILE),
        },
        "transport_security": {
            "enable_dns_rebinding_protection": _MCP_TRANSPORT_SECURITY.enable_dns_rebinding_protection,
            "allowed_hosts": _MCP_TRANSPORT_SECURITY.allowed_hosts,
            "allowed_origins": _MCP_TRANSPORT_SECURITY.allowed_origins,
        },
    }
    return json.dumps(info, indent=2, sort_keys=True)


async def check_api_health(ctx: Context, port: int) -> bool:
    """Check if the Dash API server is responding at the given port."""
    base_url = f"http://127.0.0.1:{port}"
    try:
        with httpx.Client(timeout=5.0) as client:
            response = client.get(f"{base_url}/health")
            response.raise_for_status()
        await ctx.debug(f"Successfully connected to Dash API at {base_url}")
        return True
    except Exception as e:
        await ctx.debug(f"Health check failed for {base_url}: {e}")
        return False


async def working_api_base_url(ctx: Context) -> Optional[str]:
    dash_running = await ensure_dash_running(ctx)
    if not dash_running:
        return None
    
    port = await get_dash_api_port(ctx)
    if port is None:
        # Try to automatically enable the Dash API Server
        await ctx.info("The Dash API Server is not enabled. Attempting to enable it automatically...")
        try:
            subprocess.run(
                ["defaults", "write", "com.kapeli.dashdoc", "DHAPIServerEnabled", "YES"],
                check=True,
                timeout=10
            )
            subprocess.run(
                ["defaults", "write", "com.kapeli.dash-setapp", "DHAPIServerEnabled", "YES"],
                check=True,
                timeout=10
            )
            # Wait a moment for Dash to pick up the change
            import time
            time.sleep(2)
            
            # Try to get the port again
            port = await get_dash_api_port(ctx)
            if port is None:
                await ctx.error("Failed to enable Dash API Server automatically. Please enable it manually in Dash Settings > Integration")
                return None
            else:
                await ctx.info("Successfully enabled Dash API Server")
        except Exception as e:
            await ctx.error("Failed to enable Dash API Server automatically. Please enable it manually in Dash Settings > Integration")
            return None
    
    return f"http://127.0.0.1:{port}"


async def get_dash_api_port(ctx: Context) -> Optional[int]:
    """Get the Dash API port from the status.json file and verify the API server is responding."""
    status_file = Path.home() / "Library" / "Application Support" / "Dash" / ".dash_api_server" / "status.json"
    
    try:
        with open(status_file, 'r') as f:
            status_data = json.load(f)
            port = status_data.get('port')
            if port is None:
                port = 56728
                
        # Check if the API server is actually responding
        if await check_api_health(ctx, port):
            return port
        else:
            return None
            
    except (FileNotFoundError, json.JSONDecodeError, KeyError):
        return None


def check_dash_running() -> bool:
    """Check if Dash app is running by looking for the process."""
    try:
        # Use pgrep to check for Dash process
        result = subprocess.run(
            ["pgrep", "-f", "Dash"],
            capture_output=True,
            timeout=5
        )
        return result.returncode == 0
    except Exception:
        return False


async def ensure_dash_running(ctx: Context) -> bool:
    """Ensure Dash is running, launching it if necessary."""
    if not check_dash_running():
        await ctx.info("Dash is not running. Launching Dash...")
        try:
            # Launch Dash using the bundle identifier
            result = subprocess.run(
                ["open", "-g", "-j", "-b", "com.kapeli.dashdoc"],
                timeout=10
            )
            if result.returncode != 0:
                # Try Setapp bundle identifier
                subprocess.run(
                    ["open", "-g", "-j", "-b", "com.kapeli.dash-setapp"],
                    check=True,
                    timeout=10
                )
            # Wait a moment for Dash to start
            import time
            time.sleep(4)
            
            # Check again if Dash is now running
            if not check_dash_running():
                await ctx.error("Failed to launch Dash application")
                return False
            else:
                await ctx.info("Dash launched successfully")
                return True
        except subprocess.CalledProcessError:
            await ctx.error("Failed to launch Dash application")
            return False
        except Exception as e:
            await ctx.error(f"Error launching Dash: {e}")
            return False
    else:
        return True



class DocsetResult(BaseModel):
    """Information about a docset."""
    name: str = Field(description="Display name of the docset")
    identifier: str = Field(description="Unique identifier")
    platform: str = Field(description="Platform/type of the docset")
    full_text_search: str = Field(description="Full-text search status: 'not supported', 'disabled', 'indexing', or 'enabled'")
    notice: Optional[str] = Field(description="Optional notice about the docset status", default=None)


class DocsetResults(BaseModel):
    """Result from listing docsets."""
    docsets: list[DocsetResult] = Field(description="List of installed docsets", default_factory=list)
    error: Optional[str] = Field(description="Error message if there was an issue", default=None)


class SearchResult(BaseModel):
    """A search result from documentation."""
    name: str = Field(description="Name of the documentation entry")
    type: str = Field(description="Type of result (Function, Class, etc.)")
    platform: Optional[str] = Field(description="Platform of the result", default=None)
    load_url: str = Field(description="Opaque reference for fetch_documentation; pass this value to fetch content")
    content_ref: Optional[str] = Field(description="Same as load_url; reference for fetch_documentation", default=None)
    docset: Optional[str] = Field(description="Name of the docset", default=None)
    description: Optional[str] = Field(description="Additional description", default=None)
    language: Optional[str] = Field(description="Programming language (snippet results only)", default=None)
    tags: Optional[str] = Field(description="Tags (snippet results only)", default=None)


class SearchResults(BaseModel):
    """Result from searching documentation."""
    results: list[SearchResult] = Field(description="List of search results", default_factory=list)
    error: Optional[str] = Field(description="Error message if there was an issue", default=None)
    hint: Optional[str] = Field(description="How to fetch content for a result", default=None)


class DocPageFetchResult(BaseModel):
    """Result from fetching documentation content."""
    url: Optional[str] = Field(description="Opaque reference used for this fetch", default=None)
    content_ref: Optional[str] = Field(description="Same as url; reference used for this fetch", default=None)
    content: str = Field(description="Fetched content (may be truncated)", default="")
    content_type: Optional[str] = Field(description="Content-Type header from Dash", default=None)
    encoding: Optional[str] = Field(description="Detected text encoding", default=None)
    offset: int = Field(description="Byte offset used for this chunk", default=0)
    size_bytes: int = Field(description="Number of bytes returned in content", default=0)
    total_bytes: Optional[int] = Field(description="Total size in bytes, if known", default=None)
    truncated: bool = Field(description="Whether more data remains after this chunk", default=False)
    next_offset: Optional[int] = Field(description="Next offset to fetch, if truncated", default=None)
    error: Optional[str] = Field(description="Error message if there was an issue", default=None)
    hint: Optional[str] = Field(description="How to continue fetching content", default=None)


def estimate_tokens(obj) -> int:
    """Estimate token count for a serialized object. Rough approximation: 1 token ≈ 4 characters."""
    if isinstance(obj, str):
        return max(1, len(obj) // 4)
    elif isinstance(obj, (list, tuple)):
        return sum(estimate_tokens(item) for item in obj)
    elif isinstance(obj, dict):
        return sum(estimate_tokens(k) + estimate_tokens(v) for k, v in obj.items())
    elif hasattr(obj, 'model_dump'):  # Pydantic model
        return estimate_tokens(obj.model_dump())
    else:
        return max(1, len(str(obj)) // 4)


def _rewrite_load_url(load_url: str) -> str:
    if not _DASH_LOAD_URL_BASE:
        return load_url
    base = urlparse(_DASH_LOAD_URL_BASE)
    if not base.scheme or not base.netloc:
        return load_url
    parsed = urlparse(load_url)
    base_path = base.path.rstrip("/")
    new_path = f"{base_path}{parsed.path}" if base_path else parsed.path
    return base._replace(path=new_path, params="", query=parsed.query, fragment="").geturl()


@mcp.tool()
async def list_installed_docsets(ctx: Context) -> DocsetResults:
    """List all installed documentation sets in Dash. An empty list is returned if the user has no docsets installed. 
    Results are automatically truncated if they would exceed 25,000 tokens."""
    try:
        base_url = await working_api_base_url(ctx)
        if base_url is None:
            return DocsetResults(error="Failed to connect to Dash API Server. Please ensure Dash is running and the API server is enabled (in Dash Settings > Integration).")
        await ctx.debug("Fetching installed docsets from Dash API")
        
        with httpx.Client(timeout=30.0) as client:
            response = client.get(f"{base_url}/docsets/list")
            response.raise_for_status()
            result = response.json()
        
        docsets = result.get("docsets", [])
        await ctx.info(f"Found {len(docsets)} installed docsets")
        
        # Build result list with token limit checking
        token_limit = 25000
        current_tokens = 100  # Base overhead for response structure
        limited_docsets = []
        
        for docset in docsets:
            docset_info = DocsetResult(
                name=docset["name"],
                identifier=docset["identifier"],
                platform=docset["platform"],
                full_text_search=docset["full_text_search"],
                notice=docset.get("notice")
            )
            
            # Estimate tokens for this docset
            docset_tokens = estimate_tokens(docset_info)
            
            if current_tokens + docset_tokens > token_limit:
                await ctx.warning(f"Token limit reached. Returning {len(limited_docsets)} of {len(docsets)} docsets to stay under 25k token limit.")
                break
                
            limited_docsets.append(docset_info)
            current_tokens += docset_tokens
        
        if len(limited_docsets) < len(docsets):
            await ctx.info(f"Returned {len(limited_docsets)} docsets (truncated from {len(docsets)} due to token limit)")
        
        return DocsetResults(docsets=limited_docsets)
        
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            await ctx.warning("No docsets found. Install some in Settings > Downloads.")
            return DocsetResults(error="No docsets found. Instruct the user to install some docsets in Settings > Downloads.")
        return DocsetResults(error=f"HTTP error: {e}")
    except Exception as e:
        await ctx.error(f"Failed to get installed docsets: {e}")
        return DocsetResults(error=f"Failed to get installed docsets: {e}")


@mcp.tool()
async def search_documentation(
    ctx: Context,
    query: str,
    docset_identifiers: str,
    search_snippets: bool = True,
    max_results: int = 100,
) -> SearchResults:
    """
    Search for documentation across docset identifiers and snippets.
    Each result includes an opaque load_url reference for fetch_documentation.
    
    Args:
        query: The search query string
        docset_identifiers: Comma-separated list of docset identifiers to search in (from list_installed_docsets)
        search_snippets: Whether to include snippets in search results
        max_results: Maximum number of results to return (1-1000)
    
    Results are automatically truncated if they would exceed 25,000 tokens.
    """
    if not query.strip():
        await ctx.error("Query cannot be empty")
        return SearchResults(error="Query cannot be empty")
    
    if not docset_identifiers.strip():
        await ctx.error("docset_identifiers cannot be empty. Get the docset identifiers using list_installed_docsets")
        return SearchResults(error="docset_identifiers cannot be empty. Get the docset identifiers using list_installed_docsets")
    
    if max_results < 1 or max_results > 1000:
        await ctx.error("max_results must be between 1 and 1000")
        return SearchResults(error="max_results must be between 1 and 1000")
    
    try:
        base_url = await working_api_base_url(ctx)
        if base_url is None:
            return SearchResults(error="Failed to connect to Dash API Server. Please ensure Dash is running and the API server is enabled (in Dash Settings > Integration).")
        
        params = {
            "query": query,
            "docset_identifiers": docset_identifiers,
            "search_snippets": search_snippets,
            "max_results": max_results,
        }
        
        await ctx.debug(f"Searching Dash API with query: '{query}'")
        
        with httpx.Client(timeout=30.0) as client:
            response = client.get(f"{base_url}/search", params=params)
            response.raise_for_status()
            result = response.json()
        
        # Check for warning message in response
        warning_message = None
        if "message" in result:
            warning_message = result["message"]
            await ctx.warning(warning_message)
        
        results = result.get("results", [])
        # Filter out empty dict entries (Dash API returns [{}] for no results)
        results = [r for r in results if r]

        if not results and ' ' in query:
            return SearchResults(
                results=[],
                error="Nothing found. Try to search for fewer terms.",
                hint="Use fetch_documentation with a result load_url to retrieve content.",
            )

        await ctx.info(f"Found {len(results)} results")
        
        # Build result list with token limit checking
        token_limit = 25000
        current_tokens = 100  # Base overhead for response structure
        limited_results = []
        
        for item in results:
            raw_load_url = item.get("load_url")
            content_ref = _register_content_ref(raw_load_url) if raw_load_url else None
            search_result = SearchResult(
                name=item["name"],
                type=item["type"],
                platform=item.get("platform"),
                load_url=content_ref or "",
                content_ref=content_ref,
                docset=item.get("docset"),
                description=item.get("description"),
                language=item.get("language"),
                tags=item.get("tags"),
            )
            
            # Estimate tokens for this result
            result_tokens = estimate_tokens(search_result)
            
            if current_tokens + result_tokens > token_limit:
                await ctx.warning(f"Token limit reached. Returning {len(limited_results)} of {len(results)} results to stay under 25k token limit.")
                break
                
            limited_results.append(search_result)
            current_tokens += result_tokens
        
        if len(limited_results) < len(results):
            await ctx.info(f"Returned {len(limited_results)} results (truncated from {len(results)} due to token limit)")
        
        return SearchResults(
            results=limited_results,
            error=warning_message,
            hint="Use fetch_documentation with load_url (content_ref) to retrieve content. "
            "If truncated, call again with offset=next_offset.",
        )
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 400:
            error_text = e.response.text
            if "Docset with identifier" in error_text and "not found" in error_text:
                await ctx.error("Invalid docset identifier. Run list_installed_docsets to see available docsets.")
                return SearchResults(error="Invalid docset identifier. Run list_installed_docsets to see available docsets, then use the exact identifier from that list.")
            elif "No docsets found" in error_text:
                await ctx.error("No valid docsets found for search.")
                return SearchResults(error="No valid docsets found for search. Either provide valid docset identifiers from list_installed_docsets, or set search_snippets=true to search snippets only.")
            else:
                await ctx.error(f"Bad request: {error_text}")
                return SearchResults(error=f"Bad request: {error_text}. Please ensure Dash is running and the API server is enabled (in Dash Settings > Integration).")
        elif e.response.status_code == 403:
            error_text = e.response.text
            if "API access blocked due to Dash trial expiration" in error_text:
                await ctx.error("Dash trial expired. Purchase Dash to continue using the API.")
                return SearchResults(error="Your Dash trial has expired. Purchase Dash at https://kapeli.com/dash to continue using the API. During trial expiration, API access is blocked.")
            else:
                await ctx.error(f"Forbidden: {error_text}")
                return SearchResults(error=f"Forbidden: {error_text}. Please ensure Dash is running and the API server is enabled (in Dash Settings > Integration).")
        await ctx.error(f"HTTP error: {e}")
        return SearchResults(error=f"HTTP error: {e}. Please ensure Dash is running and the API server is enabled (in Dash Settings > Integration).")
    except Exception as e:
        await ctx.error(f"Search failed: {e}")
        return SearchResults(error=f"Search failed: {e}. Please ensure Dash is running and the API server is enabled (in Dash Settings > Integration).")


def _parse_content_length(value: str | None) -> Optional[int]:
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _extract_charset(content_type: str | None) -> Optional[str]:
    if not content_type:
        return None
    parts = [part.strip() for part in content_type.split(";")]
    for part in parts[1:]:
        if part.lower().startswith("charset="):
            return part.split("=", 1)[1].strip().strip('"')
    return None


def _load_host_allowed(hostname: Optional[str]) -> bool:
    if not hostname:
        return False
    if "*" in _ALLOWED_DASH_LOAD_HOSTS:
        return True
    return hostname in _ALLOWED_DASH_LOAD_HOSTS


@mcp.tool()
async def fetch_documentation(
    ctx: Context,
    load_url: str,
    offset: int = 0,
    max_bytes: int = _DOC_CHUNK_BYTES,
) -> DocPageFetchResult:
    """
    Fetch raw documentation content from a Dash load_url reference, with optional pagination.

    Args:
        load_url: The opaque load_url returned by search_documentation (pass as-is)
        offset: Byte offset to start reading from (>= 0)
        max_bytes: Max bytes to return in this response (1 to DASH_MCP_DOC_CHUNK_MAX_BYTES)
    """
    if not load_url or not load_url.strip():
        return DocPageFetchResult(error="load_url cannot be empty")
    if offset < 0:
        return DocPageFetchResult(error="offset must be >= 0")
    if max_bytes < 1:
        return DocPageFetchResult(error="max_bytes must be >= 1")
    if max_bytes > _DOC_CHUNK_MAX_BYTES:
        await ctx.warning(f"max_bytes too large; clamping to {_DOC_CHUNK_MAX_BYTES}")
        max_bytes = _DOC_CHUNK_MAX_BYTES

    content_ref, resolved_url = _normalize_content_ref(load_url.strip())
    if not resolved_url:
        return DocPageFetchResult(
            url=content_ref,
            content_ref=content_ref,
            error="Unknown content reference. Re-run search_documentation to get a fresh load_url.",
            hint="Call search_documentation again to obtain a new load_url/content_ref.",
        )

    parsed = urlparse(resolved_url)
    if parsed.scheme not in {"http", "https"}:
        return DocPageFetchResult(error="load_url must use http or https")
    if not _load_host_allowed(parsed.hostname):
        return DocPageFetchResult(error="load_url host is not allowed")
    if "/Dash/" not in parsed.path or not parsed.path.endswith("/load"):
        return DocPageFetchResult(error="load_url does not look like a Dash load endpoint")
    query_params = parse_qs(parsed.query)
    has_request_key = "request_key" in query_params
    has_docc_params = "base_path" in query_params and "docc_file" in query_params
    if not has_request_key and not has_docc_params:
        return DocPageFetchResult(
            error="load_url missing request_key or docc_file/base_path parameters",
            hint="Pass the load_url from search_documentation (content_ref) without modification.",
        )
    if parsed.hostname in {"127.0.0.1", "localhost", "::1"}:
        dash_running = await ensure_dash_running(ctx)
        if not dash_running:
            return DocPageFetchResult(error="Dash app is not running and could not be launched")

    try:
        with httpx.Client(timeout=30.0) as client:
            with client.stream("GET", resolved_url, follow_redirects=False) as response:
                response.raise_for_status()
                content_type = response.headers.get("Content-Type")
                total_bytes = _parse_content_length(response.headers.get("Content-Length"))
                target_end = offset + max_bytes
                bytes_read = 0
                bytes_kept = 0
                chunks: list[bytes] = []
                limit_reached = False

                for chunk in response.iter_bytes():
                    if not chunk:
                        continue
                    chunk_len = len(chunk)
                    next_read = bytes_read + chunk_len
                    if next_read <= offset:
                        bytes_read = next_read
                        continue

                    start = max(0, offset - bytes_read)
                    end = min(chunk_len, target_end - bytes_read)
                    if start < end:
                        chunks.append(chunk[start:end])
                        bytes_kept += end - start
                    bytes_read = next_read
                    if bytes_read >= target_end:
                        limit_reached = True
                        break

        raw_content = b"".join(chunks)
        encoding = _extract_charset(content_type) or "utf-8"
        try:
            content_text = raw_content.decode(encoding, errors="replace")
        except LookupError:
            encoding = "utf-8"
            content_text = raw_content.decode(encoding, errors="replace")

        truncated = False
        if total_bytes is not None:
            truncated = (offset + bytes_kept) < total_bytes
        elif limit_reached:
            truncated = True

        next_offset = (offset + bytes_kept) if truncated else None

        hint = None
        if truncated and next_offset is not None:
            hint = "Response truncated. Call fetch_documentation again with offset=next_offset."
        return DocPageFetchResult(
            url=content_ref,
            content_ref=content_ref,
            content=content_text,
            content_type=content_type,
            encoding=encoding,
            offset=offset,
            size_bytes=bytes_kept,
            total_bytes=total_bytes,
            truncated=truncated,
            next_offset=next_offset,
            hint=hint,
        )
    except httpx.HTTPStatusError as e:
        return DocPageFetchResult(error=f"HTTP error: {e}")
    except httpx.RequestError as e:
        return DocPageFetchResult(error=f"Request error: {e}")
    except Exception as e:
        return DocPageFetchResult(error=f"Failed to fetch documentation: {e}")


@mcp.tool()
async def enable_docset_fts(ctx: Context, identifier: str) -> bool:
    """
    Enable full-text search for a specific docset.
    
    Args:
        identifier: The docset identifier (from list_installed_docsets)
        
    Returns:
        True if FTS was successfully enabled, False otherwise
    """
    if not identifier.strip():
        await ctx.error("Docset identifier cannot be empty")
        return False

    try:
        base_url = await working_api_base_url(ctx)
        if base_url is None:
            return False
        
        await ctx.debug(f"Enabling FTS for docset: {identifier}")
        
        with httpx.Client(timeout=30.0) as client:
            response = client.get(f"{base_url}/docsets/enable_fts", params={"identifier": identifier})
            response.raise_for_status()
            result = response.json()
        
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 400:
            await ctx.error(f"Bad request: {e.response.text}")
            return False
        elif e.response.status_code == 404:
            await ctx.error(f"Docset not found: {identifier}")
            return False
        await ctx.error(f"HTTP error: {e}")
        return False
    except Exception as e:
        await ctx.error(f"Failed to enable FTS: {e}")
        return False
    return True

def main():
    transport = os.getenv("DASH_MCP_TRANSPORT", "stdio").strip().lower()
    if not transport:
        transport = "stdio"
    transport = transport.replace("_", "-")
    mount_path = os.getenv("DASH_MCP_MOUNT_PATH")
    if transport == "stdio":
        mcp.run(transport=transport, mount_path=mount_path)
        return
    if transport == "sse":
        anyio.run(_run_uvicorn_app, mcp.sse_app(mount_path))
        return
    if transport == "streamable-http":
        anyio.run(_run_uvicorn_app, mcp.streamable_http_app())
        return
    raise ValueError(f"Unknown transport: {transport}")


async def _run_uvicorn_app(app) -> None:
    import uvicorn

    app = _ensure_http_compat_app(app)

    config_kwargs = {
        "app": app,
        "host": _MCP_HOST,
        "port": _MCP_PORT,
        "log_level": _MCP_LOG_LEVEL.lower(),
        "timeout_graceful_shutdown": _MCP_SHUTDOWN_TIMEOUT,
    }
    if _MCP_KEEP_ALIVE_TIMEOUT is not None:
        config_kwargs["timeout_keep_alive"] = _MCP_KEEP_ALIVE_TIMEOUT
    config = uvicorn.Config(**config_kwargs)
    server = uvicorn.Server(config)
    await server.serve()


_METHOD_ALIASES = {
    "listResources": "resources/list",
    "list_resources": "resources/list",
    "listTools": "tools/list",
    "list_tools": "tools/list",
    "listResourceTemplates": "resources/templates/list",
    "list_resource_templates": "resources/templates/list",
    "readResource": "resources/read",
    "read_resource": "resources/read",
    "callTool": "tools/call",
    "call_tool": "tools/call",
}

_NO_PARAMS_METHODS = {
    "resources/list",
    "tools/list",
    "resources/templates/list",
    "ping",
}

_RESPONSE_MODE = contextvars.ContextVar("dash_mcp_response_mode", default=None)
_SSE_CLIENTS: dict[str, float] = {}
_SSE_PREFERRED_CLIENTS: dict[str, float] = {}
_CLIENT_NAMES: dict[str, str] = {}
_CLIENT_NAMES_BY_UA: dict[str, str] = {}
_CLIENT_NAMES_BY_CLIENT_ID: dict[str, str] = {}
_SESSION_BY_CLIENT: dict[str, tuple[str, float]] = {}
_SESSION_TO_CLIENT: dict[str, str] = {}
_CONTENT_REF_PREFIX = "dash://content/"
_CONTENT_REF_BY_ID: dict[str, tuple[str, float]] = {}


def _rewrite_method_aliases(payload):
    if isinstance(payload, dict):
        if "jsonrpc" not in payload:
            payload["jsonrpc"] = "2.0"
        method = payload.get("method")
        if method in _METHOD_ALIASES:
            payload["method"] = _METHOD_ALIASES[method]
            method = payload["method"]
        params = payload.get("params")
        if method in _NO_PARAMS_METHODS and isinstance(params, list) and not params:
            payload["params"] = {}
        elif method in _NO_PARAMS_METHODS and isinstance(params, str) and not params.strip():
            payload["params"] = {}
    elif isinstance(payload, list):
        for item in payload:
            if isinstance(item, dict):
                if "jsonrpc" not in item:
                    item["jsonrpc"] = "2.0"
                method = item.get("method")
                if method in _METHOD_ALIASES:
                    item["method"] = _METHOD_ALIASES[method]
                    method = item["method"]
                params = item.get("params")
                if method in _NO_PARAMS_METHODS and isinstance(params, list) and not params:
                    item["params"] = {}
                elif method in _NO_PARAMS_METHODS and isinstance(params, str) and not params.strip():
                    item["params"] = {}


def _wrap_http_compat(app):
    async def compat(scope, receive, send):
        if scope.get("type") != "http":
            return await app(scope, receive, send)

        method = scope.get("method")
        if method != "POST":
            if method == "GET":
                _record_sse_client(scope)
            return await app(scope, receive, send)

        headers = list(scope.get("headers") or [])
        accept_values = [value for key, value in headers if key.lower() == b"accept"]
        accept = b",".join(accept_values).decode("latin-1") if accept_values else ""
        accept_lower = accept.lower()
        has_json = "application/json" in accept_lower
        has_sse = "text/event-stream" in accept_lower

        body = b""
        more_body = True
        while more_body:
            message = await receive()
            body += message.get("body", b"")
            more_body = message.get("more_body", False)

        new_body = body
        if body:
            try:
                payload = json.loads(body)
            except json.JSONDecodeError:
                payload = None
            if payload is not None:
                _record_client_info(payload, scope)
                _rewrite_method_aliases(payload)
                new_body = json.dumps(payload).encode("utf-8")

        parts = [accept] if accept else []
        if not has_json:
            parts.append("application/json")
        if not has_sse:
            parts.append("text/event-stream")
        new_accept = ", ".join(parts)

        new_headers = [(key, value) for key, value in headers if key.lower() != b"accept"]
        new_headers.append((b"accept", new_accept.encode("latin-1")))
        if new_body != body:
            new_headers = [(key, value) for key, value in new_headers if key.lower() != b"content-length"]
            new_headers.append((b"content-length", str(len(new_body)).encode("ascii")))

        new_scope = dict(scope)
        new_scope["headers"] = new_headers
        sent = False

        async def new_receive():
            nonlocal sent
            if sent:
                return {"type": "http.request", "body": b"", "more_body": False}
            sent = True
            return {"type": "http.request", "body": new_body, "more_body": False}

        method_name = payload.get("method") if isinstance(payload, dict) else None
        response_mode = _select_response_mode(scope, accept_lower, method_name)
        if payload is not None:
            _log_http_request(scope, body, new_body, payload, accept, response_mode)
        token = _RESPONSE_MODE.set(response_mode)
        try:
            return await app(new_scope, new_receive, send)
        finally:
            _RESPONSE_MODE.reset(token)

    return compat


def _log_http_request(
    scope,
    original_body: bytes,
    rewritten_body: bytes,
    payload,
    accept_header: str,
    response_mode: Optional[str],
) -> None:
    if not _MCP_HTTP_LOG:
        return
    client = scope.get("client")
    client_str = f"{client[0]}:{client[1]}" if client else "unknown"
    client_name = _client_name_for_scope(scope)
    client_key = _client_key_from_scope(scope)
    client_id = _hash_client_key(client_key) if client_key else None
    if client_name and client_id:
        client_tag = f"{client_str}({client_name}|{client_id})"
    elif client_name:
        client_tag = f"{client_str}({client_name})"
    elif client_id:
        client_tag = f"{client_str}({client_id})"
    else:
        client_tag = client_str
    original_text = _truncate_log_text(original_body)
    rewritten_text = _truncate_log_text(rewritten_body)
    method = payload.get("method") if isinstance(payload, dict) else None
    path = scope.get("path", "")
    if original_text == rewritten_text:
        print(
            f"[dash-mcp][http] {client_tag} {path} method={method} accept={accept_header} "
            f"mode={response_mode} body={original_text}"
        )
        return
    print(
        f"[dash-mcp][http] {client_tag} {path} method={method} accept={accept_header} "
        f"mode={response_mode} body={original_text} rewritten={rewritten_text}"
    )


def _truncate_log_text(body: bytes) -> str:
    try:
        text = body.decode("utf-8", errors="replace")
    except Exception:
        text = repr(body)
    if len(text) > _MCP_HTTP_LOG_MAX:
        return text[:_MCP_HTTP_LOG_MAX] + "...<truncated>"
    return text


def _log_http_response_payload(payload: str, mode: str) -> None:
    if not _MCP_HTTP_LOG_RESPONSE:
        return
    text = payload
    if len(text) > _MCP_HTTP_LOG_MAX:
        text = text[:_MCP_HTTP_LOG_MAX] + "...<truncated>"
    response_id = None
    try:
        parsed = json.loads(payload)
        if isinstance(parsed, dict):
            response_id = parsed.get("id")
    except Exception:
        response_id = None
    id_part = f" id={response_id}" if response_id is not None else ""
    print(f"[dash-mcp][http] response mode={mode}{id_part} payload={text}")


def _strip_tool_output_schema(payload: dict) -> dict:
    if not _MCP_STRIP_TOOL_OUTPUT_SCHEMA:
        return payload
    result = payload.get("result")
    if not isinstance(result, dict):
        return payload
    tools = result.get("tools")
    if not isinstance(tools, list):
        return payload
    changed = False
    new_tools: list[object] = []
    for tool in tools:
        if isinstance(tool, dict) and "outputSchema" in tool:
            tool = dict(tool)
            tool.pop("outputSchema", None)
            changed = True
        new_tools.append(tool)
    if not changed:
        return payload
    new_payload = dict(payload)
    new_result = dict(result)
    new_result["tools"] = new_tools
    new_payload["result"] = new_result
    return new_payload


def _record_client_info(payload, scope) -> None:
    if not isinstance(payload, dict):
        return
    if payload.get("method") != "initialize":
        return
    params = payload.get("params") or {}
    client_info = params.get("clientInfo") or {}
    name = client_info.get("name")
    if not name:
        return
    user_agent = _header_value(scope, "user-agent")
    if user_agent:
        _CLIENT_NAMES_BY_UA[user_agent] = str(name)
    for header_name in ("x-mcp-client-id", "x-client-id"):
        header_value = _header_value(scope, header_name)
        if header_value:
            _CLIENT_NAMES_BY_CLIENT_ID[f"{header_name}:{header_value}"] = str(name)
    ip = _client_ip(scope)
    if not ip:
        return
    _CLIENT_NAMES[ip] = str(name)
    if not _MCP_SSE_CLIENT_NAMES:
        return
    if str(name).lower() in _MCP_SSE_CLIENT_NAMES:
        _SSE_PREFERRED_CLIENTS[ip] = time.monotonic()


def _should_route_responses_to_get(scope) -> bool:
    if _MCP_ROUTE_RESPONSES_TO_GET is True:
        return True
    if _MCP_ROUTE_RESPONSES_TO_GET is False:
        return False
    headers = scope.get("headers") or []
    accept = ""
    for key, value in headers:
        if key.lower() == b"accept":
            accept = value.decode("latin-1").lower()
            break
    return "text/event-stream" in accept


def _client_ip(scope) -> Optional[str]:
    client = scope.get("client")
    if not client:
        return None
    return client[0]


def _header_value(scope, name: str) -> Optional[str]:
    headers = scope.get("headers") or []
    name_bytes = name.lower().encode("latin-1")
    for key, value in headers:
        if key.lower() == name_bytes:
            return value.decode("latin-1")
    return None


def _hash_client_key(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def _cleanup_content_refs(now: float) -> None:
    if _MCP_CONTENT_REF_TTL <= 0:
        return
    for ref, (_url, timestamp) in list(_CONTENT_REF_BY_ID.items()):
        if now - timestamp > _MCP_CONTENT_REF_TTL:
            _CONTENT_REF_BY_ID.pop(ref, None)


def _register_content_ref(load_url: str) -> Optional[str]:
    if not load_url:
        return None
    ref_id = hashlib.sha256(load_url.encode("utf-8")).hexdigest()[:16]
    ref = f"{_CONTENT_REF_PREFIX}{ref_id}"
    now = time.monotonic()
    _CONTENT_REF_BY_ID[ref] = (load_url, now)
    _cleanup_content_refs(now)
    return ref


def _resolve_content_ref(value: str) -> Optional[str]:
    if not value or not value.startswith(_CONTENT_REF_PREFIX):
        return None
    now = time.monotonic()
    entry = _CONTENT_REF_BY_ID.get(value)
    if not entry:
        return None
    load_url, timestamp = entry
    if _MCP_CONTENT_REF_TTL > 0 and now - timestamp > _MCP_CONTENT_REF_TTL:
        _CONTENT_REF_BY_ID.pop(value, None)
        return None
    _CONTENT_REF_BY_ID[value] = (load_url, now)
    return load_url


def _normalize_content_ref(value: str) -> tuple[Optional[str], Optional[str]]:
    if not value:
        return None, None
    if value.startswith(_CONTENT_REF_PREFIX):
        resolved = _resolve_content_ref(value)
        return value, resolved
    ref = _register_content_ref(value)
    return ref, value


def _client_key_from_scope(scope) -> Optional[str]:
    for header_name in ("x-mcp-client-id", "x-client-id"):
        header_value = _header_value(scope, header_name)
        if header_value:
            return f"{header_name}:{header_value}"
    user_agent = _header_value(scope, "user-agent")
    if user_agent:
        client_name = _CLIENT_NAMES_BY_UA.get(user_agent)
        if client_name:
            return f"name:{client_name.lower()}|ua:{user_agent}"
        return f"ua:{user_agent}"
    return None


def _client_name_for_scope(scope) -> Optional[str]:
    for header_name in ("x-mcp-client-id", "x-client-id"):
        header_value = _header_value(scope, header_name)
        if header_value:
            client_name = _CLIENT_NAMES_BY_CLIENT_ID.get(f"{header_name}:{header_value}")
            if client_name:
                return client_name
    user_agent = _header_value(scope, "user-agent")
    if user_agent:
        client_name = _CLIENT_NAMES_BY_UA.get(user_agent)
        if client_name:
            return client_name
    ip = _client_ip(scope)
    if ip:
        client_name = _CLIENT_NAMES.get(ip)
        if client_name:
            return client_name
    return None


def _client_session_key(request: Request, scope) -> Optional[str]:
    client_key = _client_key_from_scope(scope)
    if not client_key:
        return None
    return f"client:{_hash_client_key(client_key)}"


def _record_sse_client(scope) -> None:
    if _MCP_SSE_CLIENT_TTL <= 0:
        return
    ip = _client_ip(scope)
    if not ip:
        return
    _SSE_CLIENTS[ip] = time.monotonic()


def _select_response_mode(scope, accept_lower: str, method_name: Optional[str]) -> Optional[str]:
    if not _MCP_HTTP_COMPAT:
        return None
    has_json = "application/json" in accept_lower
    has_sse = "text/event-stream" in accept_lower

    if has_sse and not has_json:
        return "sse"
    if has_json:
        return "json"

    ip = _client_ip(scope)
    if ip and ip in _SSE_PREFERRED_CLIENTS:
        age = time.monotonic() - _SSE_PREFERRED_CLIENTS[ip]
        if _MCP_SSE_CLIENT_TTL <= 0 or age <= _MCP_SSE_CLIENT_TTL:
            return "sse"
        _SSE_PREFERRED_CLIENTS.pop(ip, None)

    if ip and ip in _SSE_CLIENTS:
        age = time.monotonic() - _SSE_CLIENTS[ip]
        if age <= _MCP_SSE_CLIENT_TTL:
            return "sse"
        _SSE_CLIENTS.pop(ip, None)

    if _MCP_PREFER_SSE:
        return "sse"
    return "json"


def _ensure_http_compat_app(app):
    if not _MCP_HTTP_COMPAT:
        return app
    if getattr(app, "_dash_http_compat", False):
        return app
    compat = _wrap_http_compat(app)
    setattr(compat, "_dash_http_compat", True)
    return compat


def _apply_http_compat_patch() -> None:
    if not _MCP_HTTP_COMPAT:
        return

    original_streamable_http_app = mcp.streamable_http_app

    def wrapped_streamable_http_app(*args, **kwargs):
        return _ensure_http_compat_app(original_streamable_http_app(*args, **kwargs))

    mcp.streamable_http_app = wrapped_streamable_http_app  # type: ignore[assignment]

    original_sse_app = mcp.sse_app

    def wrapped_sse_app(*args, **kwargs):
        return _ensure_http_compat_app(original_sse_app(*args, **kwargs))

    mcp.sse_app = wrapped_sse_app  # type: ignore[assignment]


_apply_http_compat_patch()


def _select_json_response(manager: StreamableHTTPSessionManager, scope) -> bool:
    mode = _RESPONSE_MODE.get()
    if mode == "json":
        return True
    if mode == "sse":
        return False
    return manager.json_response


def _patch_session_manager() -> None:
    if getattr(StreamableHTTPSessionManager, "_dash_http_compat", False):
        return

    original_handle_stateless = StreamableHTTPSessionManager._handle_stateless_request
    original_handle_stateful = StreamableHTTPSessionManager._handle_stateful_request

    async def handle_compat_stateless_request(self, scope, receive, send):
        json_response = _select_json_response(self, scope)
        http_transport = StreamableHTTPServerTransport(
            mcp_session_id=None,
            is_json_response_enabled=json_response,
            event_store=None,
            security_settings=self.security_settings,
        )

        async def run_stateless_server(*, task_status=anyio.TASK_STATUS_IGNORED):
            async with http_transport.connect() as streams:
                read_stream, write_stream = streams
                task_status.started()
                try:
                    await self.app.run(
                        read_stream,
                        write_stream,
                        self.app.create_initialization_options(),
                        stateless=True,
                    )
                except Exception:
                    logging.getLogger(__name__).exception("Stateless session crashed")

        assert self._task_group is not None
        await self._task_group.start(run_stateless_server)
        await http_transport.handle_request(scope, receive, send)
        await http_transport.terminate()

    async def handle_stateless_request(self, scope, receive, send):
        if not self.stateless:
            return await original_handle_stateless(self, scope, receive, send)

        json_response = _select_json_response(self, scope)
        http_transport = StreamableHTTPServerTransport(
            mcp_session_id=None,
            is_json_response_enabled=json_response,
            event_store=None,
            security_settings=self.security_settings,
        )
        http_transport._dash_route_responses_to_get = _should_route_responses_to_get(scope)

        async def run_stateless_server(*, task_status=anyio.TASK_STATUS_IGNORED):
            async with http_transport.connect() as streams:
                read_stream, write_stream = streams
                task_status.started()
                try:
                    await self.app.run(
                        read_stream,
                        write_stream,
                        self.app.create_initialization_options(),
                        stateless=True,
                    )
                except Exception:
                    logging.getLogger(__name__).exception("Stateless session crashed")

        assert self._task_group is not None
        await self._task_group.start(run_stateless_server)
        await http_transport.handle_request(scope, receive, send)
        await http_transport.terminate()

    async def handle_stateful_request(self, scope, receive, send):
        if self.stateless:
            return await original_handle_stateful(self, scope, receive, send)

        request = Request(scope, receive)
        request_mcp_session_id = request.headers.get("mcp-session-id")

        if request_mcp_session_id is not None and request_mcp_session_id in self._server_instances:
            transport = self._server_instances[request_mcp_session_id]
            transport.is_json_response_enabled = _select_json_response(self, scope)
            await transport.handle_request(scope, receive, send)
            return

        if request_mcp_session_id is None:
            prefer_stateful = _should_route_responses_to_get(scope)
            if _MCP_STATELESS_ON_NO_SESSION and not prefer_stateful:
                return await handle_compat_stateless_request(self, scope, receive, send)

            client_key = _client_session_key(request, scope)
            if client_key:
                entry = _SESSION_BY_CLIENT.get(client_key)
                if entry:
                    session_id, last_seen = entry
                    if _MCP_SESSION_TTL > 0 and time.monotonic() - last_seen > _MCP_SESSION_TTL:
                        _SESSION_BY_CLIENT.pop(client_key, None)
                        _SESSION_TO_CLIENT.pop(session_id, None)
                    elif session_id in self._server_instances:
                        _SESSION_BY_CLIENT[client_key] = (session_id, time.monotonic())
                        transport = self._server_instances[session_id]
                        transport.is_json_response_enabled = _select_json_response(self, scope)
                        await transport.handle_request(scope, receive, send)
                        return
                    else:
                        _SESSION_BY_CLIENT.pop(client_key, None)
                        _SESSION_TO_CLIENT.pop(session_id, None)

            async with self._session_creation_lock:
                if client_key:
                    entry = _SESSION_BY_CLIENT.get(client_key)
                    if entry:
                        session_id, last_seen = entry
                        if _MCP_SESSION_TTL > 0 and time.monotonic() - last_seen > _MCP_SESSION_TTL:
                            _SESSION_BY_CLIENT.pop(client_key, None)
                            _SESSION_TO_CLIENT.pop(session_id, None)
                        elif session_id in self._server_instances:
                            _SESSION_BY_CLIENT[client_key] = (session_id, time.monotonic())
                            transport = self._server_instances[session_id]
                            transport.is_json_response_enabled = _select_json_response(self, scope)
                            await transport.handle_request(scope, receive, send)
                            return
                        else:
                            _SESSION_BY_CLIENT.pop(client_key, None)
                            _SESSION_TO_CLIENT.pop(session_id, None)

                new_session_id = uuid4().hex
                json_response = _select_json_response(self, scope)
                http_transport = StreamableHTTPServerTransport(
                    mcp_session_id=new_session_id,
                    is_json_response_enabled=json_response,
                    event_store=self.event_store,
                    security_settings=self.security_settings,
                )
                http_transport._dash_route_responses_to_get = _should_route_responses_to_get(scope)

                assert http_transport.mcp_session_id is not None
                self._server_instances[http_transport.mcp_session_id] = http_transport
                if client_key:
                    _SESSION_BY_CLIENT[client_key] = (http_transport.mcp_session_id, time.monotonic())
                    _SESSION_TO_CLIENT[http_transport.mcp_session_id] = client_key

                async def run_server(*, task_status=anyio.TASK_STATUS_IGNORED):
                    async with http_transport.connect() as streams:
                        read_stream, write_stream = streams
                        task_status.started()
                        try:
                            await self.app.run(
                                read_stream,
                                write_stream,
                                self.app.create_initialization_options(),
                                stateless=False,
                            )
                        except Exception as e:
                            logging.getLogger(__name__).error(
                                f"Session {http_transport.mcp_session_id} crashed: {e}",
                                exc_info=True,
                            )
                        finally:
                            if (
                                http_transport.mcp_session_id
                                and http_transport.mcp_session_id in self._server_instances
                                and not http_transport.is_terminated
                            ):
                                self._server_instances.pop(http_transport.mcp_session_id, None)
                            if http_transport.mcp_session_id:
                                client_key_inner = _SESSION_TO_CLIENT.pop(http_transport.mcp_session_id, None)
                                if client_key_inner:
                                    existing = _SESSION_BY_CLIENT.get(client_key_inner)
                                    if existing and existing[0] == http_transport.mcp_session_id:
                                        _SESSION_BY_CLIENT.pop(client_key_inner, None)

                assert self._task_group is not None
                await self._task_group.start(run_server)
                await http_transport.handle_request(scope, receive, send)
                return

        await original_handle_stateful(self, scope, receive, send)

    StreamableHTTPSessionManager._handle_stateless_request = handle_stateless_request  # type: ignore[assignment]
    StreamableHTTPSessionManager._handle_stateful_request = handle_stateful_request  # type: ignore[assignment]
    StreamableHTTPSessionManager._dash_http_compat = True


_patch_session_manager()


def _patch_http_transport_logging() -> None:
    if getattr(StreamableHTTPServerTransport, "_dash_http_log_response", False):
        return

    original_create_json_response = StreamableHTTPServerTransport._create_json_response
    original_create_event_data = StreamableHTTPServerTransport._create_event_data
    original_connect = StreamableHTTPServerTransport.connect
    original_validate_session = StreamableHTTPServerTransport._validate_session

    def create_json_response(self, response_message, status_code=HTTPStatus.OK, headers=None):
        if response_message is not None:
            try:
                payload_obj = response_message.model_dump(by_alias=True, exclude_none=True)
                payload_obj = _strip_tool_output_schema(payload_obj)
                payload = json.dumps(payload_obj, separators=(",", ":"), ensure_ascii=True)
            except Exception:
                payload = repr(response_message)
            _log_http_response_payload(payload, "json")
            response_headers = {"Content-Type": CONTENT_TYPE_JSON}
            if headers:
                response_headers.update(headers)
            if self.mcp_session_id and not _MCP_DISABLE_SESSION_HEADER:
                response_headers[MCP_SESSION_ID_HEADER] = self.mcp_session_id
            return Response(payload, status_code=status_code, headers=response_headers)
        return original_create_json_response(self, response_message, status_code, headers)

    def create_event_data(self, event_message):
        event_data = original_create_event_data(self, event_message)
        data_payload = event_data.get("data")
        if isinstance(data_payload, str):
            try:
                payload_obj = json.loads(data_payload)
                payload_obj = _strip_tool_output_schema(payload_obj)
                data_payload = json.dumps(payload_obj, separators=(",", ":"), ensure_ascii=True)
                event_data["data"] = data_payload
            except Exception:
                pass
            _log_http_response_payload(data_payload, "sse")
        return event_data

    StreamableHTTPServerTransport._create_json_response = create_json_response  # type: ignore[assignment]
    StreamableHTTPServerTransport._create_event_data = create_event_data  # type: ignore[assignment]

    @asynccontextmanager
    async def connect(self):
        read_stream_writer, read_stream = anyio.create_memory_object_stream[SessionMessage | Exception](0)
        write_stream, write_stream_reader = anyio.create_memory_object_stream[SessionMessage](0)

        self._read_stream_writer = read_stream_writer
        self._read_stream = read_stream
        self._write_stream_reader = write_stream_reader
        self._write_stream = write_stream

        async with anyio.create_task_group() as tg:
            async def message_router():
                try:
                    async for session_message in write_stream_reader:
                        message = session_message.message
                        target_request_id = None
                        if isinstance(message.root, JSONRPCResponse | JSONRPCError):
                            response_id = str(message.root.id)
                            target_request_id = response_id
                        elif (
                            session_message.metadata is not None
                            and isinstance(session_message.metadata, ServerMessageMetadata)
                            and session_message.metadata.related_request_id is not None
                        ):
                            target_request_id = str(session_message.metadata.related_request_id)

                        request_stream_id = target_request_id if target_request_id is not None else GET_STREAM_KEY

                        event_id = None
                        if self._event_store:
                            event_id = await self._event_store.store_event(request_stream_id, message)
                            _http_logger.debug(f"Stored {event_id} from {request_stream_id}")

                        def can_route_to_get() -> bool:
                            if request_stream_id == GET_STREAM_KEY:
                                return False
                            if not getattr(self, "_dash_route_responses_to_get", False):
                                return False
                            return GET_STREAM_KEY in self._request_streams

                        async def send_to(stream_id: str) -> bool:
                            if stream_id not in self._request_streams:
                                return False
                            try:
                                await self._request_streams[stream_id][0].send(EventMessage(message, event_id))
                                return True
                            except (anyio.BrokenResourceError, anyio.ClosedResourceError):
                                self._request_streams.pop(stream_id, None)
                                return False

                        sent_primary = await send_to(request_stream_id)
                        if can_route_to_get():
                            await send_to(GET_STREAM_KEY)
                        elif not sent_primary:
                            logging.debug(
                                f"""Request stream {request_stream_id} not found
                                for message. Still processing message as the client
                                might reconnect and replay."""
                            )
                except anyio.ClosedResourceError:
                    return
                except Exception:
                    _http_logger.exception("Error in message router")

            tg.start_soon(message_router)

            try:
                yield read_stream, write_stream
            finally:
                for stream_id in list(self._request_streams.keys()):
                    await self._clean_up_memory_streams(stream_id)
                self._request_streams.clear()

                try:
                    await read_stream_writer.aclose()
                    await read_stream.aclose()
                    await write_stream_reader.aclose()
                    await write_stream.aclose()
                except Exception as e:
                    _http_logger.debug(f"Error closing streams: {e}")

    StreamableHTTPServerTransport.connect = connect  # type: ignore[assignment]

    async def validate_session(self, request, send):
        if _MCP_DISABLE_SESSION_HEADER:
            return True
        return await original_validate_session(self, request, send)

    StreamableHTTPServerTransport._validate_session = validate_session  # type: ignore[assignment]
    StreamableHTTPServerTransport._dash_http_log_response = True


_patch_http_transport_logging()


if __name__ == "__main__":
    main()
