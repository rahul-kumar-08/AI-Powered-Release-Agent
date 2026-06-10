#!/usr/bin/env python3
"""
MCP Sourcegraph Ticket Validator — Validates Jira EPIC/ticket IDs against
release commits using the Sourcegraph MCP server via fastmcp.

For a given release (by version string or latest N releases), this script:
1. Finds the release commit on Gerrit via Sourcegraph commit_search
2. Extracts all referenced ticket IDs (Tickets Resolved, Epic's) from commit message
3. Validates each ticket by searching Sourcegraph for commits mentioning that ticket
4. Reports validation status: which tickets are properly associated with the release

Usage:
  # Validate tickets for a specific release version
  python3 tools/mcp_ticket_validator.py --version "main-master-rhel9.7-10.0.0"

  # Validate last N releases from master
  python3 tools/mcp_ticket_validator.py --branch master --count 3

  # Validate a specific branch release
  python3 tools/mcp_ticket_validator.py --branch ganges-7.5 --count 1 \
      --repo "nugerrit.ntnxdpro.com/main@ganges-7.5.1.8-stable"

  # Validate with JSON output
  python3 tools/mcp_ticket_validator.py --branch master --count 2 --format json

  # Validate from a pre-fetched release JSON
  python3 tools/mcp_ticket_validator.py --input-json /tmp/all_master_releases.json --count 5

Environment variables (from tools/.env):
  SOURCEGRAPH_TOKEN   — Sourcegraph access token (Bearer auth for MCP gateway)
"""

import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

try:
    from tools.mcp_client import call_tool as _mcp_call_tool, _get_env
except ImportError:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from mcp_client import call_tool as _mcp_call_tool, _get_env

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DEFAULT_SERVER_KEY = "gw-sourcegraph"
DEFAULT_REPO = "nugerrit.ntnxdpro.com/main"
TOOL_PREFIX = "sourcegraph__"

TICKET_RE = re.compile(r"(ENG-\d+)", re.IGNORECASE)
EPIC_RE = re.compile(r"Epic'?s?\s*:\s*(.+?)(?:\n|$)", re.IGNORECASE)
TICKETS_RESOLVED_RE = re.compile(r"Tickets?\s*Resolved\s*:\s*(.+?)(?:\n|$)", re.IGNORECASE)
RELEASE_TITLE_RE = re.compile(r"Release\s+[Gg]old\s+image\s+", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Sourcegraph Operations
# ---------------------------------------------------------------------------

def sg_commit_search(server_key, repos, message_terms, count=10):
    """Search commits via Sourcegraph MCP."""
    arguments = {"repos": repos, "messageTerms": message_terms, "count": count}
    result = _mcp_call_tool(server_key, f"{TOOL_PREFIX}commit_search", arguments)
    return _extract_commits(result)


def sg_keyword_search(server_key, query):
    """Run a keyword search via Sourcegraph MCP."""
    result = _mcp_call_tool(server_key, f"{TOOL_PREFIX}keyword_search", {"query": query})
    return _extract_text(result)


def _extract_commits(result):
    commits = []
    for part in result.get("content", []):
        if part.get("type") == "text":
            try:
                parsed = json.loads(part["text"])
                if isinstance(parsed, list):
                    commits.extend(parsed)
                elif isinstance(parsed, dict):
                    commits.extend(parsed.get("commits", parsed.get("results", [parsed])))
            except (json.JSONDecodeError, KeyError):
                pass
    return commits


def _extract_text(result):
    texts = []
    for part in result.get("content", []):
        if part.get("type") == "text":
            texts.append(part.get("text", ""))
    return "\n".join(texts)


# ---------------------------------------------------------------------------
# Release & Ticket Extraction
# ---------------------------------------------------------------------------

def find_releases(server_key, repo, branch, count):
    """Find release commits on a branch."""
    repos = [repo] if branch == "master" else [f"{repo}@{branch}"]
    message_terms = ["Release", "gold image"]
    if branch == "master":
        message_terms.append("main-master")

    commits = sg_commit_search(server_key, repos, message_terms, count=count * 3)

    releases = []
    for c in sorted(commits, key=lambda x: x.get("date", ""), reverse=True):
        title = c.get("title", "")
        if not re.match(r"^(Release|PC\s*:)", title, re.IGNORECASE):
            continue
        if title.startswith("Revert"):
            continue
        releases.append(c)
        if len(releases) >= count:
            break

    return releases


def extract_tickets_from_message(message):
    """
    Extract ticket IDs from a release commit message.
    Returns dict with 'epics' and 'resolved_tickets' lists.
    """
    result = {"epics": [], "resolved_tickets": [], "all_tickets": []}

    epic_match = EPIC_RE.search(message)
    if epic_match:
        epic_line = epic_match.group(1)
        result["epics"] = TICKET_RE.findall(epic_line)

    resolved_match = TICKETS_RESOLVED_RE.search(message)
    if resolved_match:
        resolved_line = resolved_match.group(1)
        result["resolved_tickets"] = TICKET_RE.findall(resolved_line)

    all_tickets = set(result["epics"] + result["resolved_tickets"])
    all_mentioned = TICKET_RE.findall(message)
    all_tickets.update(all_mentioned)
    result["all_tickets"] = sorted(all_tickets)

    return result


def extract_version_from_title(title):
    """Extract the GoldImage version identifier from a release title."""
    cleaned = RELEASE_TITLE_RE.sub("", title)
    return cleaned.strip()


# ---------------------------------------------------------------------------
# Ticket Validation via Sourcegraph
# ---------------------------------------------------------------------------

def validate_ticket_in_sourcegraph(server_key, ticket_id, repo, release_commit_sha):
    """
    Validate a single ticket by searching Sourcegraph for commits that reference it.

    Returns a validation dict with:
      - found: bool (ticket found in any commit)
      - in_release_commit: bool (ticket is in the expected release commit)
      - commit_count: int (how many commits reference this ticket)
      - commits: list of commit summaries referencing this ticket
      - status: str (VALID, FOUND_ELSEWHERE, NOT_FOUND)
    """
    try:
        commits = sg_commit_search(
            server_key, [repo], message_terms=[ticket_id], count=10
        )
    except RuntimeError:
        return {
            "ticket": ticket_id,
            "found": False,
            "in_release_commit": False,
            "commit_count": 0,
            "commits": [],
            "status": "ERROR",
            "detail": "Sourcegraph search failed",
        }

    found_in_release = False
    commit_summaries = []

    for c in commits:
        sha = c.get("commit", c.get("oid", ""))[:7]
        title = c.get("title", c.get("message", "")).split("\n")[0][:80]
        date = c.get("date", "N/A")

        if sha and release_commit_sha and sha.startswith(release_commit_sha[:7]):
            found_in_release = True

        commit_summaries.append({
            "sha": sha,
            "title": title,
            "date": date,
        })

    if found_in_release:
        status = "VALID"
    elif commits:
        status = "FOUND_ELSEWHERE"
    else:
        status = "NOT_FOUND"

    return {
        "ticket": ticket_id,
        "found": len(commits) > 0,
        "in_release_commit": found_in_release,
        "commit_count": len(commits),
        "commits": commit_summaries,
        "status": status,
    }


def validate_release_tickets(server_key, release, repo, max_workers=5):
    """
    Validate all tickets for a single release.

    Searches Sourcegraph for each ticket ID found in the release commit message
    and reports whether it's properly associated with the release.
    """
    title = release.get("title", "")
    message = release.get("message", "")
    commit_sha = release.get("commit", "")
    date = release.get("date", "N/A")

    ticket_info = extract_tickets_from_message(message)

    print(
        f"\n  Validating release: {title[:70]}",
        file=sys.stderr,
    )
    print(
        f"  Commit: {commit_sha} | Date: {date}",
        file=sys.stderr,
    )
    print(
        f"  Epics: {ticket_info['epics'] or 'None found'}",
        file=sys.stderr,
    )
    print(
        f"  Resolved tickets: {len(ticket_info['resolved_tickets'])} ticket(s)",
        file=sys.stderr,
    )

    all_tickets = ticket_info["all_tickets"]
    if not all_tickets:
        return {
            "release_title": title,
            "release_version": extract_version_from_title(title),
            "release_commit": commit_sha,
            "release_date": date,
            "epics": [],
            "resolved_tickets": [],
            "validations": [],
            "summary": {"total": 0, "valid": 0, "found_elsewhere": 0, "not_found": 0, "error": 0},
        }

    validations = []
    for i, ticket_id in enumerate(all_tickets):
        print(
            f"    [{i+1}/{len(all_tickets)}] Checking {ticket_id}...",
            file=sys.stderr, end="",
        )
        v = validate_ticket_in_sourcegraph(server_key, ticket_id, repo, commit_sha)
        validations.append(v)
        print(f" {v['status']} ({v['commit_count']} commits)", file=sys.stderr)

    summary = {
        "total": len(validations),
        "valid": sum(1 for v in validations if v["status"] == "VALID"),
        "found_elsewhere": sum(1 for v in validations if v["status"] == "FOUND_ELSEWHERE"),
        "not_found": sum(1 for v in validations if v["status"] == "NOT_FOUND"),
        "error": sum(1 for v in validations if v["status"] == "ERROR"),
    }

    return {
        "release_title": title,
        "release_version": extract_version_from_title(title),
        "release_commit": commit_sha,
        "release_date": date,
        "epics": ticket_info["epics"],
        "resolved_tickets": ticket_info["resolved_tickets"],
        "validations": validations,
        "summary": summary,
    }


# ---------------------------------------------------------------------------
# Output Formatting
# ---------------------------------------------------------------------------

def print_validation_table(results):
    """Print validation results in tabular format."""
    for result in results:
        print(f"\n{'='*100}")
        print(f"Release: {result['release_title'][:90]}")
        print(f"Commit:  {result['release_commit']}  |  Date: {result['release_date']}")
        if result["epics"]:
            print(f"Epic(s): {', '.join(result['epics'])}")
        print(f"{'='*100}")

        validations = result.get("validations", [])
        if not validations:
            print("  No tickets found in this release.")
            continue

        print(f"\n  {'#':<4} {'Ticket':<14} {'Status':<18} {'Commits':<8} {'Type':<8} {'Found In'}")
        print(f"  {'─'*4} {'─'*14} {'─'*18} {'─'*8} {'─'*8} {'─'*50}")

        epics_set = set(result.get("epics", []))
        resolved_set = set(result.get("resolved_tickets", []))

        for i, v in enumerate(validations, 1):
            ticket = v["ticket"]
            status = v["status"]

            if ticket in epics_set:
                ttype = "EPIC"
            elif ticket in resolved_set:
                ttype = "RESOLVE"
            else:
                ttype = "OTHER"

            if status == "VALID":
                status_display = "VALID"
            elif status == "FOUND_ELSEWHERE":
                status_display = "FOUND_ELSEWHERE"
            elif status == "NOT_FOUND":
                status_display = "NOT_FOUND"
            else:
                status_display = "ERROR"

            found_in = ""
            if v.get("commits"):
                first = v["commits"][0]
                found_in = f"{first['sha'][:7]} ({first['date']}) {first['title'][:35]}"

            print(f"  {i:<4} {ticket:<14} {status_display:<18} {v['commit_count']:<8} {ttype:<8} {found_in}")

        s = result["summary"]
        print(f"\n  Summary: {s['total']} tickets | "
              f"{s['valid']} VALID | "
              f"{s['found_elsewhere']} FOUND_ELSEWHERE | "
              f"{s['not_found']} NOT_FOUND | "
              f"{s['error']} ERROR")

    print(f"\n{'='*100}")
    total_tickets = sum(r["summary"]["total"] for r in results)
    total_valid = sum(r["summary"]["valid"] for r in results)
    total_elsewhere = sum(r["summary"]["found_elsewhere"] for r in results)
    total_not_found = sum(r["summary"]["not_found"] for r in results)
    print(f"Overall: {len(results)} release(s), {total_tickets} tickets validated")
    print(f"  VALID: {total_valid} | FOUND_ELSEWHERE: {total_elsewhere} | NOT_FOUND: {total_not_found}")
    print(f"{'='*100}\n")


def print_validation_detail(results):
    """Print detailed validation with commit references for each ticket."""
    for result in results:
        print(f"\n{'━'*100}")
        print(f"Release: {result['release_title']}")
        print(f"Commit:  {result['release_commit']}  |  Date: {result['release_date']}")
        if result["epics"]:
            print(f"Epic(s): {', '.join(result['epics'])}")
        print(f"Resolved: {', '.join(result['resolved_tickets'][:10])}"
              + ("..." if len(result["resolved_tickets"]) > 10 else ""))
        print(f"{'━'*100}")

        for v in result.get("validations", []):
            status_mark = {"VALID": "+", "FOUND_ELSEWHERE": "~",
                           "NOT_FOUND": "!", "ERROR": "X"}.get(v["status"], "?")
            print(f"\n  [{status_mark}] {v['ticket']} — {v['status']}")
            if v.get("commits"):
                for c in v["commits"][:5]:
                    print(f"      {c['sha'][:7]} | {c['date']} | {c['title'][:60]}")
            else:
                print(f"      (no commits found referencing this ticket)")
