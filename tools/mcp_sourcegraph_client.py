#!/usr/bin/env python3
"""
MCP Client for Sourcegraph — Fetches commit history with complete details
using the Model Context Protocol via fastmcp.

Connects to the Sourcegraph MCP server via the gateway and invokes
the `commit_search` tool to retrieve commit history.

Usage:
  # Search commits by message terms in a repo
  python3 tools/mcp_sourcegraph_client.py --repos "nugerrit.ntnxdpro.com/main" \
      --message "Release Gold image" --count 10

  # Search commits by author
  python3 tools/mcp_sourcegraph_client.py --repos "nugerrit.ntnxdpro.com/main" \
      --authors "svc.jenkins.autosub" --count 5

  # Search with date range
  python3 tools/mcp_sourcegraph_client.py --repos "nugerrit.ntnxdpro.com/main" \
      --message "Release" --after "2025-01-01" --before "2025-06-01"

  # Search with branch qualifier
  python3 tools/mcp_sourcegraph_client.py --repos "nugerrit.ntnxdpro.com/main@ganges-7.5.1.8-stable" \
      --message "Release Gold image" --count 20

  # List available MCP tools
  python3 tools/mcp_sourcegraph_client.py --list-tools

  # JSON output
  python3 tools/mcp_sourcegraph_client.py --repos "nugerrit.ntnxdpro.com/main" \
      --message "Release" --count 5 --format json --output /tmp/commits.json

Environment variables (from tools/.env):
  SOURCEGRAPH_TOKEN  — Sourcegraph access token (used as Bearer auth for MCP gateway)
"""

import json
import os
import sys
from datetime import datetime

try:
    from tools.mcp_client import call_tool as _mcp_call_tool, list_tools as _mcp_list_tools
except ImportError:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from mcp_client import call_tool as _mcp_call_tool, list_tools as _mcp_list_tools

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DEFAULT_SERVER_KEY = "gw-sourcegraph"
TOOL_PREFIX = "sourcegraph__"


# ---------------------------------------------------------------------------
# Commit Search
# ---------------------------------------------------------------------------

def search_commits(server_key, repos, message_terms=None, authors=None,
                   content_terms=None, files=None, after=None, before=None,
                   count=None):
    """
    Call the commit_search MCP tool and return commit results.

    Parameters match the Sourcegraph MCP commit_search tool schema:
      repos         - list of repo identifiers (e.g. ["nugerrit.ntnxdpro.com/main"])
      message_terms - list of terms to match in commit messages
      authors       - list of author names/emails to filter by
      content_terms - list of terms to match in commit content (diffs)
      files         - list of file path patterns
      after         - ISO date string, commits after this date
      before        - ISO date string, commits before this date
      count         - max number of results
    """
    arguments = {"repos": repos}

    if message_terms:
        arguments["messageTerms"] = message_terms
    if authors:
        arguments["authors"] = authors
    if content_terms:
        arguments["contentTerms"] = content_terms
    if files:
        arguments["files"] = files
    if after:
        arguments["after"] = after
    if before:
        arguments["before"] = before
    if count:
        arguments["count"] = count

    print(
        f"Searching commits: repos={repos}, message={message_terms}, "
        f"authors={authors}, after={after}, before={before}, count={count}",
        file=sys.stderr,
    )

    tool_name = f"{TOOL_PREFIX}commit_search"
    result = _mcp_call_tool(server_key, tool_name, arguments)
    return _extract_commits_from_result(result)


def _extract_commits_from_result(result):
    """Extract commit data from MCP tool call result."""
    commits = []
    content_parts = result.get("content", [])

    for part in content_parts:
        if part.get("type") == "text":
            text = part.get("text", "")
            try:
                parsed = json.loads(text)
                if isinstance(parsed, list):
                    commits.extend(parsed)
                elif isinstance(parsed, dict):
                    if "commits" in parsed:
                        commits.extend(parsed["commits"])
                    elif "results" in parsed:
                        commits.extend(parsed["results"])
                    else:
                        commits.append(parsed)
            except json.JSONDecodeError:
                commits.append({"raw_text": text})

    return commits



# ---------------------------------------------------------------------------
# Output Formatting
# ---------------------------------------------------------------------------

def format_commit_table(commits):
    """Format commits as a readable table."""
    if not commits:
        print("No commits found.")
        return

    print(f"\n{'='*120}")
    print(f"{'#':<4} {'Date':<22} {'Author':<30} {'SHA':<12} {'Message'}")
    print(f"{'='*120}")

    for i, commit in enumerate(commits, 1):
        date = _extract_field(commit, ["authorDate", "author_date", "date",
                                        "committerDate", "committedDate"])
        author = _extract_field(commit, ["author", "authorName", "author_name"])
        if isinstance(author, dict):
            author = author.get("name", "") or author.get("email", "")
        sha = _extract_field(commit, ["oid", "sha", "hash", "commit_id"])
        message = _extract_field(commit, ["message", "subject", "title"])

        if isinstance(message, str):
            message = message.split("\n")[0]

        date_short = _format_date(date) if date else "N/A"
        sha_short = (sha or "N/A")[:10]
        author_short = (str(author) or "N/A")[:28]
        msg_short = (str(message) or "N/A")[:60]

        print(f"{i:<4} {date_short:<22} {author_short:<30} {sha_short:<12} {msg_short}")

    print(f"{'='*120}")
    print(f"Total: {len(commits)} commit(s)\n")


def format_commit_detail(commits):
    """Format commits with full details."""
    if not commits:
        print("No commits found.")
        return

    for i, commit in enumerate(commits, 1):
        print(f"\n{'─'*80}")
        print(f"Commit #{i}")
        print(f"{'─'*80}")
        _print_commit_fields(commit, indent="  ")

    print(f"\n{'─'*80}")
    print(f"Total: {len(commits)} commit(s)")


def _print_commit_fields(obj, indent="", max_depth=3, current_depth=0):
    """Recursively print all fields of a commit object."""
    if current_depth >= max_depth:
        print(f"{indent}...")
        return
    if isinstance(obj, dict):
        for key, val in obj.items():
            if isinstance(val, (dict, list)):
                print(f"{indent}{key}:")
                _print_commit_fields(val, indent + "  ", max_depth, current_depth + 1)
            else:
                display_val = str(val)
                if len(display_val) > 200:
                    display_val = display_val[:200] + "..."
                print(f"{indent}{key}: {display_val}")
    elif isinstance(obj, list):
        for idx, item in enumerate(obj):
            if isinstance(item, (dict, list)):
                print(f"{indent}[{idx}]:")
                _print_commit_fields(item, indent + "  ", max_depth, current_depth + 1)
            else:
                print(f"{indent}[{idx}]: {item}")
    else:
        print(f"{indent}{obj}")


def _extract_field(obj, possible_keys):
    """Try multiple keys to extract a field from a dict."""
    if not isinstance(obj, dict):
        return None
    for key in possible_keys:
        if key in obj:
            return obj[key]
        for k, v in obj.items():
            if k.lower() == key.lower():
                return v
    return None


def _format_date(date_str):
    """Format an ISO date string to a shorter form."""
    if not date_str or date_str == "N/A":
        return "N/A"
    try:
        dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d %H:%M UTC")
    except (ValueError, AttributeError):
        return str(date_str)[:22]
