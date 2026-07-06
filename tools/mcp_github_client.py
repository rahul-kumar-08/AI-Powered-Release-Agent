#!/usr/bin/env python3
"""
MCP GitHub Client — Fetches commit details, PR information, and CircleCI
postmerge status using the GitHub MCP server via fastmcp.

MCP tools provide commit details, PR lookups, and search.  Commit statuses
(CircleCI) are fetched via the GitHub REST API because no MCP tool exposes
them.  The PAT is resolved from the MCP server config in mcp.json, falling
back to GITHUB_TOKEN env / tools/.env.

Usage:
  # Get commit details
  python3 tools/mcp_github_client.py commit bb6526aa --repo nutanix-core/aos-goldimage-os

  # Get CircleCI postmerge status for a commit
  python3 tools/mcp_github_client.py ci bb6526aa --repo nutanix-core/aos-goldimage-os

  # Get CircleCI status for multiple commits
  python3 tools/mcp_github_client.py ci sha1 sha2 sha3

  # Get PR details
  python3 tools/mcp_github_client.py pr 2414 --repo nutanix-core/aos-goldimage-os

  # List commits on a branch
  python3 tools/mcp_github_client.py list-commits --repo nutanix-core/aos-goldimage-os --branch ganges-7.6

  # List available GitHub MCP tools
  python3 tools/mcp_github_client.py list-tools

Environment variables (from tools/.env):
  GITHUB_TOKEN  — GitHub PAT (fallback; primary source is mcp.json)
"""

import asyncio
import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime

from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

try:
    from tools.mcp_client import _get_env
    from src.logger import Log
except ImportError:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from mcp_client import _get_env
    from src.logger import Log

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DEFAULT_SERVER_KEY = "github"
DEFAULT_OWNER = "nutanix-core"
DEFAULT_REPO = "aos-goldimage-os"
GITHUB_API_BASE = "https://api.github.com"

_MCP_JSON_PATHS = [
    os.path.join(os.path.dirname(os.path.abspath(__file__)),
                 "..", ".cursor", "rules", "mcp.json"),
    ".cursor/rules/mcp.json",
]




# ---------------------------------------------------------------------------
# MCP config + token resolution
# ---------------------------------------------------------------------------

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


def _load_mcp_server_config(server_key=DEFAULT_SERVER_KEY):
    """Load MCP server URL and headers from mcp.json.

    Returns:
        (url, headers) tuple.

    Raises RuntimeError if the server key is not found.
    """
    for path in _MCP_JSON_PATHS:
        abspath = os.path.abspath(path)
        if not os.path.isfile(abspath):
            continue
        try:
            with open(abspath) as f:
                raw = f.read()
            config = json.loads(_strip_json_comments(raw))
            servers = config.get("mcpServers", {})
            if server_key in servers:
                srv = servers[server_key]
                return srv.get("url", ""), dict(srv.get("headers", {}))
        except (json.JSONDecodeError, OSError):
            continue
    raise RuntimeError(
        f"MCP server '{server_key}' not found in mcp.json"
    )


def _resolve_github_token():
    """Resolve GitHub PAT from env, .env, or mcp.json Authorization header."""
    token = _get_env("GITHUB_TOKEN")
    if token:
        return token

    try:
        _, headers = _load_mcp_server_config(DEFAULT_SERVER_KEY)
        auth = headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            return auth[7:]
    except RuntimeError:
        pass

    return None


# ---------------------------------------------------------------------------
# fastmcp async helpers
# ---------------------------------------------------------------------------

async def _async_call_tool(url, headers, tool_name, arguments):
    """Call a single MCP tool and return structured content."""
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
                    content.append({
                        "type": type(part).__name__,
                        "data": str(part),
                    })
        elif isinstance(result, (list, dict)):
            return result
        return {"content": content}


async def _async_list_tools(url, headers):
    """List available tools on the MCP server."""
    transport = StreamableHttpTransport(url=url, headers=headers)
    client = Client(transport)
    async with client:
        tools = await client.list_tools()
        return [
            {"name": t.name, "description": t.description or ""}
            for t in tools
        ]


def _call_tool(tool_name, arguments, server_key=DEFAULT_SERVER_KEY):
    """Synchronous wrapper: load config, call tool, return result dict."""
    url, headers = _load_mcp_server_config(server_key)
    return asyncio.run(_async_call_tool(url, headers, tool_name, arguments))


def _list_tools(server_key=DEFAULT_SERVER_KEY):
    """Synchronous wrapper: list tools on the GitHub MCP server."""
    url, headers = _load_mcp_server_config(server_key)
    return asyncio.run(_async_list_tools(url, headers))


# ---------------------------------------------------------------------------
# MCP result parsing helpers
# ---------------------------------------------------------------------------

def _extract_text(result):
    """Extract concatenated text from MCP tool result content parts."""
    parts = []
    for p in result.get("content", []):
        if p.get("type") == "text":
            parts.append(p.get("text", ""))
    return "\n".join(parts)


def _parse_json(text):
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None


# ---------------------------------------------------------------------------
# MCP-based Operations (GitHub)
# ---------------------------------------------------------------------------

def get_commit(owner, repo, sha, server_key=DEFAULT_SERVER_KEY):
    """Fetch commit details via GitHub MCP ``get_commit`` tool."""
    result = _call_tool("get_commit", {
        "owner": owner, "repo": repo, "sha": sha,
    }, server_key)
    return _parse_json(_extract_text(result))


def get_pull_request(owner, repo, pull_number, server_key=DEFAULT_SERVER_KEY):
    """Fetch pull request details via GitHub MCP ``pull_request_read`` tool."""
    result = _call_tool("pull_request_read", {
        "owner": owner, "repo": repo, "pullNumber": int(pull_number),
    }, server_key)
    return _parse_json(_extract_text(result))


def list_commits(owner, repo, branch="main", per_page=30,
                 server_key=DEFAULT_SERVER_KEY):
    """List commits on a branch via GitHub MCP ``list_commits`` tool."""
    result = _call_tool("list_commits", {
        "owner": owner, "repo": repo, "branch": branch, "perPage": per_page,
    }, server_key)
    data = _parse_json(_extract_text(result))
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        return data.get("commits", data.get("results", []))
    return []


def search_commits(owner, repo, query, server_key=DEFAULT_SERVER_KEY):
    """Search commits via GitHub MCP ``search_commits`` tool."""
    result = _call_tool("search_commits", {
        "q": f"repo:{owner}/{repo} {query}",
    }, server_key)
    data = _parse_json(_extract_text(result))
    if isinstance(data, dict):
        return data.get("items", data.get("commits", []))
    return data if isinstance(data, list) else []


# ---------------------------------------------------------------------------
# REST API — Commit Statuses (CircleCI)
# ---------------------------------------------------------------------------

def _github_rest_get(path, token=None):
    """Make an authenticated GET request to the GitHub REST API."""
    token = token or _resolve_github_token()
    if not token:
        return None

    url = f"{GITHUB_API_BASE}{path}"
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except (urllib.error.HTTPError, urllib.error.URLError, OSError) as e:
        Log.error(f"REST API error for {path}: {e}")
        return None


def fetch_commit_statuses(owner, repo, sha, token=None):
    """Fetch all commit statuses from GitHub REST API.

    Returns the raw list of status objects.
    """
    data = _github_rest_get(
        f"/repos/{owner}/{repo}/commits/{sha}/statuses", token,
    )
    return data if isinstance(data, list) else []


def fetch_postmerge_ci(commit_sha, owner=DEFAULT_OWNER, repo=DEFAULT_REPO,
                       token=None):
    """Fetch postmerge CircleCI status for a commit.

    Queries GitHub commit statuses and filters for CircleCI postmerge jobs.

    Returns::

        {
            "cvm": {
                "url": "<circleci_link>",
                "state": "success|pending|failure",
                "description": "...",
                "created_at": "2026-06-03T13:26:31Z",
                "updated_at": "2026-06-03T13:26:31Z",
                "context": "ci/circleci_enterprise: postmerge_gi_cvm_x86",
                "creator": "username",
            },
            "pcvm": { ... },
        }
    """
    statuses = fetch_commit_statuses(owner, repo, commit_sha, token)
    if not statuses:
        return {}

    result = {}
    for s in statuses:
        context = s.get("context", "")
        entry = {
            "url": s.get("target_url", ""),
            "state": s.get("state", ""),
            "description": s.get("description", ""),
            "created_at": s.get("created_at", ""),
            "updated_at": s.get("updated_at", ""),
            "context": context,
            "creator": (s.get("creator") or {}).get("login", ""),
        }

        if "postmerge_gi_cvm" in context and "cvm" not in result:
            result["cvm"] = entry
        elif "postmerge_gi_pcvm" in context and "pcvm" not in result:
            result["pcvm"] = entry

        if "cvm" in result and "pcvm" in result:
            break

    return result


def fetch_postmerge_ci_batch(commits, owner=DEFAULT_OWNER,
                             repo=DEFAULT_REPO):
    """Fetch postmerge CI status for multiple commits.

    Args:
        commits: list of dicts with ``commit`` key containing SHA, or
                 plain SHA strings.

    Returns:
        dict mapping commit SHA -> :func:`fetch_postmerge_ci` result
    """
    token = _resolve_github_token()
    results = {}
    for c in commits:
        sha = c.get("commit", c) if isinstance(c, dict) else str(c)
        if sha:
            results[sha] = fetch_postmerge_ci(sha, owner, repo, token)
    return results


# ---------------------------------------------------------------------------
# Output Formatting
# ---------------------------------------------------------------------------

def _format_date(date_str):
    if not date_str:
        return "N/A"
    try:
        dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d %H:%M UTC")
    except (ValueError, AttributeError):
        return str(date_str)[:22]


def format_ci_table(ci_data, commit_sha=""):
    """Print CircleCI postmerge status in table format."""
    if not ci_data:
        print(f"No postmerge CI status found for {commit_sha[:10]}.")
        return

    print(f"\nCircleCI Postmerge Status for {commit_sha[:10]}")
    print(f"{'─'*90}")
    print(f"{'Job':<12} {'State':<12} {'Description':<45} {'Date':<22}")
    print(f"{'─'*90}")

    for job in ("cvm", "pcvm"):
        info = ci_data.get(job)
        if not info:
            print(f"{job.upper():<12} {'--':<12} {'(not found)':<45} {'--':<22}")
            continue
        state = info.get("state", "--")
        desc = info.get("description", "")[:43]
        date = _format_date(info.get("created_at"))
        url = info.get("url", "")
        print(f"{job.upper():<12} {state:<12} {desc:<45} {date:<22}")
        if url:
            print(f"{'':>12} URL: {url}")

    print(f"{'─'*90}")


def format_commit_table(commit_data):
    """Print commit details in a readable format."""
    if not commit_data:
        print("No commit data.")
        return

    sha = commit_data.get("sha", "N/A")
    msg = commit_data.get("commit", {}).get("message", "N/A").split("\n")[0]
    author = commit_data.get("commit", {}).get("author", {})
    date = author.get("date", "N/A")
    name = author.get("name", "N/A")
    stats = commit_data.get("stats", {})
    files = commit_data.get("files", [])

    print(f"\nCommit: {sha}")
    print(f"Author: {name}")
    print(f"Date:   {_format_date(date)}")
    print(f"Message: {msg}")
    print(f"Stats: +{stats.get('additions', 0)} -{stats.get('deletions', 0)} "
          f"({stats.get('total', 0)} changes)")
    if files:
        print(f"Files ({len(files)}):")
        for f in files:
            print(f"  {f.get('status', '?'):>10} {f.get('filename', '?')}")
