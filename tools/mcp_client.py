#!/usr/bin/env python3
"""
Shared MCP Client — Thin synchronous wrapper around fastmcp for use by all
tool scripts (sourcegraph, confluence, ticket_validator).

Uses asyncio.run() to provide a synchronous interface over fastmcp's async Client.

Usage:
    from mcp_client import call_tool, list_tools, _get_env, load_mcp_config

    result = call_tool("gw-sourcegraph", "sourcegraph__commit_search", {
        "repos": ["nugerrit.ntnxdpro.com/main"],
        "messageTerms": ["Release"],
        "count": 10,
    })
"""

import asyncio
import json
import os
import sys

from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

# ---------------------------------------------------------------------------
# Environment helpers
# ---------------------------------------------------------------------------

_env_file_loaded = False


def _load_env_file(env_path=None):
    """Load .env file as fallback for missing env vars."""
    global _env_file_loaded
    if _env_file_loaded:
        return
    _env_file_loaded = True

    if env_path is None:
        candidates = [
            os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"),
            "tools/.env",
            ".env",
        ]
        for c in candidates:
            if os.path.isfile(c):
                env_path = c
                break
    if not env_path or not os.path.isfile(env_path):
        return

    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            os.environ.setdefault(key.strip(), val.strip().strip("\"'"))


def _get_env(name, default=None):
    """Get env var, falling back to .env file."""
    val = os.environ.get(name, "").strip()
    if val:
        return val
    _load_env_file()
    val = os.environ.get(name, "").strip()
    return val if val else default


# ---------------------------------------------------------------------------
# MCP config loading
# ---------------------------------------------------------------------------

MCP_JSON_PATHS = [
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".cursor", "rules", "mcp.json"),
    ".cursor/rules/mcp.json",
]


def _strip_json_comments(text):
    """Remove // comments from JSON text (not inside strings)."""
    lines = []
    for line in text.split("\n"):
        stripped = line.lstrip()
        if stripped.startswith("//"):
            continue
        if "//" in line:
            in_str = False
            result = []
            i = 0
            while i < len(line):
                ch = line[i]
                if ch == '"' and (i == 0 or line[i - 1] != '\\'):
                    in_str = not in_str
                if not in_str and line[i:i + 2] == '//':
                    break
                result.append(ch)
                i += 1
            lines.append("".join(result))
        else:
            lines.append(line)
    return "\n".join(lines)


def load_mcp_config(server_key):
    """
    Load MCP server URL and headers from .cursor/rules/mcp.json.

    Returns:
        (url: str, headers: dict)
    """
    for path in MCP_JSON_PATHS:
        abspath = os.path.abspath(path)
        if os.path.isfile(abspath):
            with open(abspath) as f:
                raw = f.read()
            cleaned = _strip_json_comments(raw)
            config = json.loads(cleaned)
            servers = config.get("mcpServers", {})
            if server_key in servers:
                server = servers[server_key]
                url = server.get("url", "")
                headers = dict(server.get("headers", {}))
                return url, headers
    raise RuntimeError(f"MCP server '{server_key}' not found in mcp.json")


# ---------------------------------------------------------------------------
# Synchronous MCP operations (wraps fastmcp async Client)
# ---------------------------------------------------------------------------

def _log(msg):
    print(f"[mcp-client] {msg}", file=sys.stderr, flush=True)


def call_tool(server_key, tool_name, arguments):
    """
    Call an MCP tool synchronously.

    Args:
        server_key: Key in mcp.json (e.g. "gw-sourcegraph", "atlassian")
        tool_name: Full tool name (e.g. "sourcegraph__commit_search")
        arguments: Dict of tool arguments

    Returns:
        Tool result as a dict with "content" key containing response parts.
    """
    url, headers = load_mcp_config(server_key)
    return asyncio.run(_async_call_tool(url, headers, tool_name, arguments))


async def _async_call_tool(url, headers, tool_name, arguments):
    transport = StreamableHttpTransport(url=url, headers=headers)
    client = Client(transport)
    async with client:
        result = await client.call_tool(tool_name, arguments)
        content = []
        if hasattr(result, "content"):
            for part in result.content:
                if hasattr(part, "text"):
                    content.append({"type": "text", "text": part.text})
                else:
                    content.append({"type": str(type(part).__name__), "data": str(part)})
        elif isinstance(result, (list, dict)):
            return result
        return {"content": content}


def list_tools(server_key):
    """List available tools on an MCP server."""
    url, headers = load_mcp_config(server_key)
    return asyncio.run(_async_list_tools(url, headers))


async def _async_list_tools(url, headers):
    transport = StreamableHttpTransport(url=url, headers=headers)
    client = Client(transport)
    async with client:
        tools = await client.list_tools()
        return [{"name": t.name, "description": t.description or ""} for t in tools]
