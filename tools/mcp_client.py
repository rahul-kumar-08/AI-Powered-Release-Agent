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


import re as _re

_VAR_PATTERN = _re.compile(r"\$?\{(\w+)\}")


def _resolve_vars(value):
    """Replace ``${VAR}`` or ``{VAR}`` placeholders with values from .env / environment."""
    def _sub(m):
        return _get_env(m.group(1)) or m.group(0)
    return _VAR_PATTERN.sub(_sub, value)


def load_mcp_config(server_key):
    """
    Load MCP server URL and headers from .cursor/rules/mcp.json.

    Placeholder tokens like ``${GITHUB_TOKEN}`` or ``{SOURCEGRAPH_TOKEN}``
    in header values are resolved from ``tools/.env`` / environment variables.

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
                headers = {k: _resolve_vars(v) for k, v in server.get("headers", {}).items()}
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


# ---------------------------------------------------------------------------
# MCP Token / Server Validation
# ---------------------------------------------------------------------------

import urllib.request
import urllib.error


def _validate_sourcegraph(headers):
    """Validate Sourcegraph token via the streaming API.

    Makes a minimal search that returns quickly to verify auth.
    """
    token = headers.get("Authorization", "")
    if token.startswith("Bearer "):
        token = token[7:]
    token = token or _get_env("SOURCEGRAPH_TOKEN")
    if not token:
        return False, "No Sourcegraph token configured (set SOURCEGRAPH_TOKEN in tools/.env)"

    sg_url = _get_env("SOURCEGRAPH_URL", "https://sourcegraph.ntnxdpro.com")
    url = f"{sg_url}/.api/search/stream?q=type:repo+count:1"
    req = urllib.request.Request(url)
    req.add_header("Authorization", f"token {token}")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status == 200:
                return True, "OK"
            return False, f"HTTP {resp.status}"
    except urllib.error.HTTPError as e:
        if e.code == 401:
            return False, "Token expired or invalid (401 Unauthorized)"
        if e.code == 403:
            return False, "Token lacks permissions (403 Forbidden)"
        return False, f"HTTP {e.code}: {e.reason}"
    except Exception as e:
        return False, f"Connection failed: {e}"


def _validate_github(headers):
    """Validate GitHub PAT via the REST API ``/user`` endpoint."""
    token = headers.get("Authorization", "")
    if token.startswith("Bearer "):
        token = token[7:]
    token = token or _get_env("GITHUB_TOKEN")
    if not token:
        return False, "No GitHub token configured (set GITHUB_TOKEN in tools/.env)"

    req = urllib.request.Request("https://api.github.com/user", headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
    })
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return True, "OK"
    except urllib.error.HTTPError as e:
        if e.code == 401:
            return False, "Token expired or invalid (401 Unauthorized)"
        if e.code == 403:
            body = ""
            try:
                body = e.read().decode()[:300]
            except Exception:
                pass
            if "SAML" in body or "SSO" in body:
                return False, ("Token valid but NOT authorized for organization SAML SSO. "
                               "Go to https://github.com/settings/tokens → Configure SSO → "
                               "Authorize for 'nutanix-core'")
            return False, f"Token lacks permissions (403 Forbidden)"
        return False, f"HTTP {e.code}: {e.reason}"
    except Exception as e:
        return False, f"Connection failed: {e}"


def _validate_github_org_access(headers, org="nutanix-core"):
    """Validate GitHub PAT has access to the target organization (SAML SSO check)."""
    token = headers.get("Authorization", "")
    if token.startswith("Bearer "):
        token = token[7:]
    token = token or _get_env("GITHUB_TOKEN")
    if not token:
        return False, "No token"

    req = urllib.request.Request(
        f"https://api.github.com/repos/{org}/aos-goldimage-os", headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
        })
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return True, "OK"
    except urllib.error.HTTPError as e:
        if e.code == 403:
            body = ""
            try:
                body = e.read().decode()[:500]
            except Exception:
                pass
            if "SAML" in body or "SSO" in body:
                return False, (f"Token NOT authorized for '{org}' SAML SSO. "
                               f"Visit https://github.com/orgs/{org}/sso to authorize your PAT")
            return False, f"No access to {org} (403 Forbidden)"
        if e.code == 404:
            return False, f"Repository not found or no access (404)"
        return False, f"HTTP {e.code}: {e.reason}"
    except Exception as e:
        return False, f"Connection failed: {e}"


def _validate_jira(headers):
    """Validate Jira token via the REST API ``/myself`` endpoint."""
    token = (headers.get("X-Atlassian-Jira-Personal-Token")
             or _get_env("JIRA_TOKEN"))
    jira_url = (headers.get("X-Atlassian-Jira-Url")
                or _get_env("JIRA_BASE_URL", "https://jira.nutanix.com"))
    if not token:
        return False, "No Jira token configured (set JIRA_TOKEN in tools/.env)"

    req = urllib.request.Request(f"{jira_url}/rest/api/2/myself", headers={
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return True, "OK"
    except urllib.error.HTTPError as e:
        if e.code == 401:
            return False, "Jira token expired or invalid (401 Unauthorized)"
        if e.code == 403:
            return False, "Jira token lacks permissions (403 Forbidden)"
        return False, f"HTTP {e.code}: {e.reason}"
    except Exception as e:
        return False, f"Connection failed: {e}"


def _validate_confluence(headers):
    """Validate Confluence token via a basic content API call."""
    token = (headers.get("X-Atlassian-Confluence-Personal-Token")
             or _get_env("CONFLUENCE_TOKEN"))
    confluence_url = (headers.get("X-Atlassian-Confluence-Url")
                      or _get_env("CONFLUENCE_BASE_URL"))
    if not token or not confluence_url:
        return False, "No Confluence token or URL configured (set CONFLUENCE_TOKEN in tools/.env)"

    confluence_url = confluence_url.rstrip("/")
    req = urllib.request.Request(
        f"{confluence_url}/rest/api/content?limit=1", headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        })
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return True, "OK"
    except urllib.error.HTTPError as e:
        if e.code == 401:
            return False, "Confluence token expired or invalid (401 Unauthorized)"
        if e.code == 403:
            return False, "Confluence token lacks permissions (403 Forbidden)"
        return False, f"HTTP {e.code}: {e.reason}"
    except Exception as e:
        return False, f"Connection failed: {e}"


def _validate_jenkins(headers=None):
    """Validate Jenkins credentials via the ``/api/json`` endpoint."""
    import base64
    user = _get_env("JENKINS_USER")
    token = _get_env("JENKINS_TOKEN")
    base = _get_env("JENKINS_BASE")
    if not user or not token:
        return False, "JENKINS_USER / JENKINS_TOKEN not set in tools/.env"
    if not base:
        return False, "JENKINS_BASE not set in tools/.env"

    creds = base64.b64encode(f"{user}:{token}".encode()).decode()
    req = urllib.request.Request(
        f"{base.rstrip('/')}/api/json?tree=mode", headers={
            "Authorization": f"Basic {creds}",
        })
    import ssl
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=10) as resp:
            return True, "OK"
    except urllib.error.HTTPError as e:
        if e.code == 401:
            return False, "Jenkins credentials invalid (401 Unauthorized)"
        if e.code == 403:
            return False, "Jenkins credentials lack permissions (403 Forbidden)"
        return False, f"HTTP {e.code}: {e.reason}"
    except Exception as e:
        return False, f"Connection failed: {e}"


_SERVER_VALIDATORS = {
    "gw-sourcegraph": ("Sourcegraph", _validate_sourcegraph, True),
    "github":         ("GitHub",      _validate_github,      False),
    "atlassian":      ("Jira",        _validate_jira,        True),
}

_STANDALONE_VALIDATORS = {
    "jenkins": ("Jenkins", _validate_jenkins, False),
}

# Additional checks run after the primary validator
_EXTRA_VALIDATORS = {
    "github": [
        ("GitHub Org Access", _validate_github_org_access),
    ],
    "atlassian": [
        ("Confluence", _validate_confluence),
    ],
}


def validate_mcp_tokens(required_servers=None, github_org="nutanix-core"):
    """Validate tokens for all configured MCP servers.

    Checks each server's token by making a lightweight API call.
    Prints a status table and terminates with ``sys.exit(1)`` if any
    **critical** server fails.

    Args:
        required_servers: list of server keys that must pass (default:
            all servers marked critical in ``_SERVER_VALIDATORS``).
        github_org: GitHub organization to check SSO access for.

    Returns:
        dict mapping server key → (ok: bool, message: str).
    """
    import pandas as pd

    results = {}
    critical_failures = []
    validation_rows = []

    for server_key, (label, validator, _) in _SERVER_VALIDATORS.items():
        try:
            _, headers = load_mcp_config(server_key)
        except RuntimeError:
            msg = f"Server '{server_key}' not found in mcp.json"
            results[server_key] = (False, msg)
            critical_failures.append((label, msg))
            validation_rows.append((label, "FAIL", msg))
            continue

        ok, msg = validator(headers)
        results[server_key] = (ok, msg)
        validation_rows.append((label, "OK" if ok else "FAIL", msg))

        if ok and server_key in _EXTRA_VALIDATORS:
            for extra_label, extra_validator in _EXTRA_VALIDATORS[server_key]:
                eok, emsg = extra_validator(headers)
                results[f"{server_key}:{extra_label}"] = (eok, emsg)
                validation_rows.append((extra_label, "OK" if eok else "WARN", emsg))

        if not ok:
            critical_failures.append((label, msg))

    for server_key, (label, validator, _) in _STANDALONE_VALIDATORS.items():
        if required_servers and server_key not in required_servers:
            continue
        ok, msg = validator()
        results[server_key] = (ok, msg)
        validation_rows.append((label, "OK" if ok else "FAIL", msg))
        if not ok:
            critical_failures.append((label, msg))

    df = pd.DataFrame(validation_rows, columns=["Server", "Status", "Message"])
    print("\n" + "=" * 60, file=sys.stderr)
    print("  MCP Server Token Validation", file=sys.stderr)
    print("=" * 60, file=sys.stderr)
    print(df.to_markdown(index=False, tablefmt="simple"), file=sys.stderr)
    print("=" * 60, file=sys.stderr)

    if critical_failures:
        print("\nCRITICAL: The following servers failed validation:\n",
              file=sys.stderr)
        df_fail = pd.DataFrame(critical_failures, columns=["Server", "Error"])
        print(df_fail.to_markdown(index=False, tablefmt="simple"), file=sys.stderr)
        print("\nFix the token(s) above and re-run. "
              "Tokens are configured in .cursor/rules/mcp.json and tools/.env\n",
              file=sys.stderr)
        sys.exit(1)

    return results
