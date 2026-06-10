#!/usr/bin/env python3
"""
Release Query — Unified pipeline for extracting GoldImage release data.

Orchestrates:
  1. Sourcegraph MCP (commit_search, read_file) for Gerrit/GitHub commits
  2. Jira REST API for EPIC ticket resolution
  3. GitHub REST API for postmerge CircleCI status
  4. Output formatting (table / json / markdown)

Configuration:
  Reads .cursor/rules/mcp.json for MCP server connection via tools/mcp_client.py.

Usage (run from project root):
  # Last 5 PC releases from master
  python3 release_query.py --branch master --count 5 --filter pc

  # Last 10 AOS releases
  python3 release_query.py --branch master --count 10 --filter aos

  # All releases (AOS + PC split into separate rows)
  python3 release_query.py --branch master --count 5 --filter all

  # JSON output for Confluence tool
  python3 release_query.py --branch master --count 5 --filter pc --format json --output /tmp/releases.json

  # Validate endor URLs exist
  python3 release_query.py --branch master --count 3 --filter pc --validate-urls

  # With CI status
  python3 release_query.py --branch ganges-7.6 --count 5 --filter pc --ci-status

  # Update Confluence after extraction
  python3 release_query.py --branch master --count 5 --filter pc --format json --output /tmp/releases.json
  python3 -m tools.mcp_confluence_client --input-json /tmp/releases.json --branch master

All public functions can be imported and called independently by MCP tools:
  from release_query import fetch_gerrit_releases, parse_releases, format_table
"""

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime

from tools.mcp_client import call_tool as _mcp_call_tool, _get_env
from tools.mcp_sourcegraph_client import TOOL_PREFIX
from tools.mcp_github_client import fetch_postmerge_ci

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DEFAULT_SERVER_KEY = "gw-sourcegraph"
DEFAULT_REPO = "nugerrit.ntnxdpro.com/main"
GITHUB_REPO = "github.com/nutanix-core/aos-goldimage-os"
#BASE_URL = "https://endor.corp.nutanix.com"
BASE_URL = "http://uranus.corp.nutanix.com/~rahul.kumar3"
ENDOR_AOS_RHEL9_MASTER = f"{BASE_URL}/GoldImages/Centos_SVM/Master"
ENDOR_AOS_STS_BASE = f"{BASE_URL}/GoldImages/Centos_SVM/STS"
ENDOR_AOS_RHEL8_BASE = f"{BASE_URL}/GoldImages/Centos_SVM/STS"
ENDOR_PC_MASTER = f"{BASE_URL}/GoldImages/PC_GoldImages/pc"
ENDOR_PC_STS_BASE = f"{BASE_URL}/GoldImages/PC_GoldImages/pc"


def _log(msg):
    print(f"[release-query] {msg}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# Endor URL Construction
# ---------------------------------------------------------------------------

def _parse_rhel8_version(version_str):
    """
    Parse rhel8 VERSION_GI into components for Endor URL construction.

    Example: "sts-ganges-7.3-rhel8.10-5.10.234-8.0.0"
      -> rhel_major=8, rhel_minor=10, kernel=5.10.234, release=8.0.0, branch_ver=7.3
    """
    m = re.match(
        r"^(?:main|sts)-(?:ganges-(?:pc\.)?)([\d.]+)-rhel(\d+)\.(\d+)-([\d.]+)-([\d.]+)$",
        version_str,
    )
    if m:
        return {
            "branch_ver": m.group(1),
            "rhel_major": m.group(2),
            "rhel_minor": m.group(3),
            "kernel": m.group(4),
            "release": m.group(5),
        }
    return None


def build_endor_urls(version_str, release_type, branch):
    """
    Construct changelog and RPM URLs based on release type, RHEL version, and branch.

    Master AOS rhel9:  http://endor.dyn.nutanix.com/GoldImages/Centos_SVM/Master/<VERSION_GI>/
    STS AOS rhel9:     https://endor-cache-2.corp.nutanix.com/GoldImages/Centos_SVM/STS/<branch_ver>/<VERSION_GI>/
    STS AOS rhel8:     https://endor.corp.nutanix.com/GoldImages/Centos_SVM/STS/<branch_ver>/RHEL...-SVM-...-k{kernel}-r{release}.x86_64/
    Master PC:         http://endor.dyn.nutanix.com/GoldImages/PC_GoldImages/pc/master/<VERSION_GI>/
    STS PC:            https://endor-cache-2.corp.nutanix.com/GoldImages/PC_GoldImages/pc/pc.<branch_ver>/<VERSION_GI>/
    """
    branch_ver = None
    if branch and branch != "master":
        m = re.match(r"ganges-([\d.]+)", branch)
        if m:
            branch_ver = m.group(1)

    if release_type == "pc":
        if branch_ver:
            pc_sub = f"pc.{branch_ver}"
            if "rhel8" in version_str:
                m = re.search(r"ganges-(pc\.[\d.]+)-rhel", version_str)
                if m:
                    pc_sub = m.group(1)
            base_dir = f"{ENDOR_PC_STS_BASE}/{pc_sub}/{version_str}"
        else:
            pc_branch = "master"
            if "rhel8" in version_str:
                m = re.search(r"ganges-(pc\.[\d.]+)-rhel", version_str)
                if m:
                    pc_branch = m.group(1)
            base_dir = f"{ENDOR_PC_MASTER}/{pc_branch}/{version_str}"
    elif "rhel8" in version_str:
        info = _parse_rhel8_version(version_str)
        if info:
            dir_name = (
                f"RHEL{info['rhel_major']}{info['rhel_minor']}-SVM-"
                f"{info['rhel_major']}.{info['rhel_minor']}-k{info['kernel']}-"
                f"r{info['release']}.x86_64"
            )
            base_dir = f"{ENDOR_AOS_RHEL8_BASE}/{info['branch_ver']}/{dir_name}"
        else:
            base_dir = f"{ENDOR_AOS_RHEL8_BASE}/{version_str}"
    elif branch_ver:
        base_dir = f"{ENDOR_AOS_STS_BASE}/{branch_ver}/{version_str}"
    else:
        base_dir = f"{ENDOR_AOS_RHEL9_MASTER}/{version_str}"

    return {
        "changelog": f"{base_dir}/changelog.txt",
        "rpm": f"{base_dir}/rpm.txt",
    }


def validate_url(url):
    """HEAD-check a URL, return True if accessible."""
    req = urllib.request.Request(url, method="HEAD")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status == 200
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Date Formatting
# ---------------------------------------------------------------------------

def format_merge_date(date_str):
    """Convert ISO date to DD-Mon-YYYY (e.g. '27-May-2026')."""
    if not date_str or date_str == "N/A":
        return "N/A"
    try:
        dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        return dt.strftime("%d-%b-%Y")
    except (ValueError, AttributeError):
        try:
            dt = datetime.strptime(date_str[:10], "%Y-%m-%d")
            return dt.strftime("%d-%b-%Y")
        except Exception:
            return date_str


# ---------------------------------------------------------------------------
# Release Data Fetching
# ---------------------------------------------------------------------------

def _sg_stream_search(query, count=50):
    """Search Sourcegraph via streaming API to get full (non-truncated) messages.

    Returns list of commit dicts matching the _extract_commits format.
    Falls back to empty list on any error.
    """
    sg_token = _resolve_sg_token()
    if not sg_token:
        return []

    sg_url = _get_env("SOURCEGRAPH_URL", "https://sourcegraph.ntnxdpro.com")
    import urllib.parse
    params = urllib.parse.urlencode({"q": f"{query} count:{count}"})
    url = f"{sg_url}/.api/search/stream?{params}"

    req = urllib.request.Request(url)
    req.add_header("Authorization", f"token {sg_token}")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8")
    except Exception:
        return []

    commits = []
    for block in body.split("\n\n"):
        lines = block.strip().split("\n")
        event_type = ""
        data_str = ""
        for line in lines:
            if line.startswith("event:"):
                event_type = line[6:].strip()
            elif line.startswith("data:"):
                data_str = line[5:]
        if event_type == "matches" and data_str:
            try:
                matches = json.loads(data_str)
            except json.JSONDecodeError:
                continue
            for m in matches:
                if not isinstance(m, dict):
                    continue
                title = (m.get("message") or "").split("\n")[0]
                commits.append({
                    "title": title,
                    "message": m.get("message", ""),
                    "date": m.get("committerDate") or m.get("authorDate", ""),
                    "commit": m.get("oid", ""),
                    "url": m.get("url", ""),
                    "author": m.get("authorName", ""),
                })
    return commits


def _resolve_sg_token():
    """Resolve Sourcegraph token from env or mcp.json."""
    token = _get_env("SOURCEGRAPH_TOKEN")
    if token:
        return token
    try:
        from tools.mcp_client import load_mcp_config
        _, headers = load_mcp_config("gw-sourcegraph")
        auth = headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            return auth[7:]
    except Exception:
        pass
    return None


def fetch_gerrit_releases(server_key, branch, count):
    """Fetch release commits from Gerrit via Sourcegraph streaming API.

    Uses the streaming API directly (not MCP) to avoid the 615-char
    message truncation imposed by the MCP gateway.  Falls back to MCP
    if the streaming API is unavailable.

    For non-master branches (e.g. ``ganges-7.6``), searches:
      1. ``rev:ganges-7.6-stable`` — the branch-specific AOS Gerrit branch
      2. ``rev:ganges-7.6-stable-pc`` — the branch-specific PC Gerrit branch
      3. default branch (no rev:) — catches commits not yet cherry-picked
    """
    repo_filter = r"repo:^nugerrit\.ntnxdpro\.com/main$"
    if branch == "master":
        query = f'type:commit {repo_filter} message:"Release gold image" message:"main-master"'
        commits = _sg_stream_search(query, count=count * 4)
        if commits:
            return commits
    else:
        # Search branch-specific Gerrit branches first
        all_commits = []
        seen_oids = set()
        gerrit_branch = f"{branch}-stable"
        for rev in (gerrit_branch, f"{gerrit_branch}-pc"):
            query = (
                f'type:commit {repo_filter} rev:{rev} '
                f'message:"Release gold image" '
            )
            for c in _sg_stream_search(query, count=count * 4):
                oid = c.get("commit", "")
                if oid not in seen_oids:
                    seen_oids.add(oid)
                    all_commits.append(c)

        # Also search default branch for older/unmatched commits
        query = f'type:commit {repo_filter} message:"Release gold image"'
        for c in _sg_stream_search(query, count=count * 4):
            oid = c.get("commit", "")
            if oid not in seen_oids:
                seen_oids.add(oid)
                all_commits.append(c)

        if all_commits:
            return all_commits

    # Fallback to MCP (messages may be truncated)
    _log("Streaming API unavailable, falling back to MCP commit_search")
    if branch == "master":
        repos = [DEFAULT_REPO]
        message_terms = ["Release", "gold image", "main-master"]
    else:
        repos = [DEFAULT_REPO]
        message_terms = ["Release", "gold image"]

    result = _mcp_call_tool(server_key, f"{TOOL_PREFIX}commit_search", {
        "repos": repos,
        "messageTerms": message_terms,
        "count": count * 4,
    })
    return _extract_commits(result)


def fetch_github_releases(server_key, branch, count):
    """Fetch release commits from GitHub (has full SHAs for read_file)."""
    if branch == "master":
        repos = [GITHUB_REPO]
        message_terms = ["Release", "gold image", "main-master"]
    else:
        repos = [f"{GITHUB_REPO}@{branch}"]
        message_terms = ["Release", "gold image"]

    result = _mcp_call_tool(server_key, f"{TOOL_PREFIX}commit_search", {
        "repos": repos,
        "messageTerms": message_terms,
        "count": count * 4,
    })
    return _extract_commits(result)




def fetch_version_gi(server_key, commit_sha):
    """
    Read services/variables.sh at a specific commit and extract VERSION_GI
    from the FLAVOR if/else block.

    if [ "$FLAVOR" = cvm ]; then VERSION_GI=<AOS> else VERSION_GI=<PC> fi

    Returns: {"aos": "<version>", "pc": "<version>"} or None on failure.
    """
    result = _mcp_call_tool(server_key, f"{TOOL_PREFIX}read_file", {
        "repo": GITHUB_REPO,
        "path": "services/variables.sh",
        "revision": commit_sha,
        "startLine": 60,
        "endLine": 120,
    })
    content = ""
    for part in result.get("content", []):
        if part.get("type") == "text":
            try:
                parsed = json.loads(part["text"])
                content = parsed.get("content", part["text"])
            except (json.JSONDecodeError, AttributeError):
                content = part["text"]

    if not content:
        return None

    aos_version = None
    pc_version = None
    lines = content.split("\n")

    in_flavor_cvm = False
    in_flavor_else = False
    depth = 0

    for line in lines:
        stripped = re.sub(r"^\d+:\s*", "", line).strip()

        if re.match(r'if\s+\[\s+"\$FLAVOR"\s*=\s*cvm\s*\]', stripped):
            in_flavor_cvm = True
            in_flavor_else = False
            depth = 1
            continue
        if in_flavor_cvm and stripped == "else":
            in_flavor_cvm = False
            in_flavor_else = True
            continue
        if (in_flavor_cvm or in_flavor_else) and stripped == "fi":
            depth -= 1
            if depth <= 0:
                in_flavor_cvm = False
                in_flavor_else = False
            continue

        gi_match = re.search(r'VERSION_GI="([^"]+)"', stripped)
        if gi_match:
            if in_flavor_cvm:
                aos_version = gi_match.group(1)
            elif in_flavor_else:
                pc_version = gi_match.group(1)

    if aos_version or pc_version:
        return {"aos": aos_version, "pc": pc_version}
    return None


def fetch_github_epics(server_key, branch="master"):
    """Fetch release commits with Epic's field from GitHub repo on the target branch."""
    if branch == "master":
        repos = [GITHUB_REPO]
    else:
        repos = [f"{GITHUB_REPO}@{branch}"]

    result = _mcp_call_tool(server_key, f"{TOOL_PREFIX}commit_search", {
        "repos": repos,
        "messageTerms": ["Release", "Epic"],
        "count": 50,
    })
    commits = _extract_commits(result)
    epic_map = {}
    for c in commits:
        title = re.sub(r"\s*\(#\d+\)$", "", c.get("title", "")).strip()
        msg = c.get("message", "")
        em = re.search(r"Epic'?s?\s*:\s*(.+?)(?:\n|$)", msg, re.IGNORECASE)
        if em:
            epic_map[title] = re.findall(r"(ENG-\d+)", em.group(1))
    return epic_map


def fetch_changelog_tickets(server_key, changelog_path, branch="master"):
    """DEPRECATED — kept as stub. Use search_jira_epic instead."""
    return {}


def search_jira_epic(version_raw, release_type):
    """
    Search Jira for the EPIC ticket matching a GoldImage version.

    For PC: looks for "PC" in the EPIC summary
    For AOS: picks the EPIC without "PC" prefix

    Returns: ticket key (str) or None
    """
    result = search_jira_epic_full(version_raw, release_type)
    return result["key"] if result else None


def search_jira_epic_full(version_raw, release_type):
    """
    Search Jira for the EPIC matching a GoldImage version.
    Tries direct Jira REST API first, falls back to Atlassian MCP.
    Returns full details: {"key": "ENG-xxx", "summary": "...", "jira_version": "..."}
    or None if not found.
    """
    jql = f'issuetype = Epic AND summary ~ "{version_raw}" ORDER BY created DESC'

    issues = _jira_search_rest(jql)
    if issues is None:
        issues = _jira_search_mcp(jql)
    if not issues:
        return None

    return _select_epic_from_issues(issues, release_type)


def _resolve_jira_token():
    """Resolve Jira token from env, .env, or mcp.json (in that order)."""
    token = _get_env("JIRA_API_TOKEN")
    if token:
        return token
    try:
        from tools.mcp_client import load_mcp_config
        _, headers = load_mcp_config("atlassian")
        token = headers.get("X-Atlassian-Jira-Personal-Token", "")
        if token:
            return token
    except Exception:
        pass
    return None


def _jira_search_rest(jql):
    """Search Jira via direct REST API. Returns list of issues or None on failure."""
    jira_url = _get_env("JIRA_BASE_URL", "https://jira.nutanix.com")
    jira_token = _resolve_jira_token()
    if not jira_token:
        return None

    import urllib.parse

    params = urllib.parse.urlencode({"jql": jql, "fields": "key,summary", "maxResults": 100})
    url = f"{jira_url}/rest/api/2/search?{params}"

    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Bearer {jira_token}")
    req.add_header("Content-Type", "application/json")

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
    except Exception:
        return None

    raw = data.get("issues", [])
    return [{"key": i["key"], "summary": i["fields"]["summary"]} for i in raw] if raw else []


def _jira_search_mcp(jql):
    """Search Jira via Atlassian MCP. Returns list of issues or None on failure.

    The MCP gateway may apply PII filtering that breaks JSON (unquoted
    placeholders like ``<PERSON_1>``). We try json.loads first, then fall
    back to regex extraction of key + summary fields.
    """
    try:
        result = _mcp_call_tool("atlassian", "atlassian__jira_search", {
            "jql": jql, "limit": 100,
        })
    except Exception:
        return None

    for part in result.get("content", []):
        if part.get("type") != "text":
            continue
        text = part.get("text", "")
        if not text:
            continue

        # Try standard JSON parse first
        try:
            parsed = json.loads(text)
            raw = parsed.get("issues", [])
            return [{"key": i["key"], "summary": i["summary"]} for i in raw] if raw else []
        except (json.JSONDecodeError, KeyError):
            pass

        # Fallback: PII-filtered response breaks JSON.  Extract key/summary
        # pairs via regex — these fields are not PII-filtered.
        # Only match Jira issue keys (PROJECT-NNN), not nested user keys.
        issues = []
        for m in re.finditer(r'"key"\s*:\s*"([A-Z]+-\d+)"', text):
            key = m.group(1)
            after = text[m.start():]
            sm = re.search(r'"summary"\s*:\s*"([^"]+)"', after)
            summary = sm.group(1) if sm else ""
            if key and summary:
                issues.append({"key": key, "summary": summary})
        return issues if issues else None

    return None


def _select_epic_from_issues(issues, release_type):
    """Pick the best EPIC from a list of Jira issues based on release type."""
    if not issues:
        return None

    selected = None
    if release_type == "pc":
        for issue in issues:
            summary = issue["summary"]
            if "PC" in summary or "pc" in summary:
                selected = issue
                break
        if not selected:
            selected = issues[0]
    else:
        for issue in issues:
            summary = issue["summary"]
            if "PC" not in summary and "pc" not in summary.split(":")[0]:
                selected = issue
                break
        if not selected:
            selected = issues[0]

    summary = selected["summary"]
    jira_version = _extract_version_from_jira_summary(summary)

    return {"key": selected["key"], "summary": summary, "jira_version": jira_version}


def fetch_gerrit_cr_from_jira(ticket_keys, branch):
    """Extract Gerrit Code Review URL from Jira ticket git-tracker comments.

    Searches ``===git tracker===`` comments on each ticket for one whose
    ``Branch`` or ``JIRA Version (branch equiv)`` matches *branch*.

    Args:
        ticket_keys: list of Jira ticket keys (e.g. ["ENG-933882"]).
        branch:      query branch (e.g. "ganges-7.6" or "master").

    Returns:
        The ``Code Review URL`` string, or "" if not found.
    """
    jira_token = _resolve_jira_token()
    jira_url = _get_env("JIRA_BASE_URL", "https://jira.nutanix.com")
    if not jira_token:
        _log("Cannot fetch Gerrit CR: no Jira token available")
        return ""

    # Normalise branch for matching: "ganges-7.6" -> "7.6"
    branch_short = re.sub(r'^ganges-', '', branch)

    for key in ticket_keys:
        try:
            url = f"{jira_url}/rest/api/2/issue/{key}?fields=comment"
            req = urllib.request.Request(url)
            req.add_header("Authorization", f"Bearer {jira_token}")
            req.add_header("Content-Type", "application/json")
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())
        except Exception:
            continue

        comments = (data.get("fields", {})
                        .get("comment", {})
                        .get("comments", []))
        for c in comments:
            body = c.get("body", "")
            if "===git tracker===" not in body:
                continue
            # Parse branch-like fields
            ver_match = re.search(
                r'JIRA Version \(branch equiv\)\s*:\s*(.+)', body)
            branch_match = re.search(r'Branch\s*:\s*(.+)', body)
            cr_match = re.search(
                r'Code Review URL\s*:\s*(https?://\S+)', body)
            if not cr_match:
                continue

            jira_ver = ver_match.group(1).strip() if ver_match else ""
            gerrit_branch = branch_match.group(1).strip() if branch_match else ""

            # Match: "master"=="master", "7.6"=="7.6",
            # or gerrit_branch starts with "ganges-7.6"
            if (branch_short == jira_ver
                    or branch_short == gerrit_branch
                    or branch == gerrit_branch
                    or gerrit_branch.startswith(f"ganges-{branch_short}")
                    or gerrit_branch.startswith(branch)):
                return cr_match.group(1)
    return ""


def fetch_ticket_summaries(ticket_keys):
    """Fetch Jira summaries for a list of ticket keys.

    Returns a dict mapping ticket key -> summary string.
    Falls back to MCP if direct REST fails.
    Batches into chunks of 50 to avoid JQL/URI length limits.
    """
    if not ticket_keys:
        return {}

    valid = [k for k in ticket_keys if re.match(r'^[A-Z]+-\d+$', k)]
    if not valid:
        return {}

    result = {}
    batch_size = 50
    for i in range(0, len(valid), batch_size):
        batch = valid[i:i + batch_size]
        jql = f"key in ({','.join(batch)})"
        issues = _jira_search_rest(jql)
        if issues is None:
            issues = _jira_search_mcp(jql)
        if issues:
            for iss in issues:
                result[iss["key"]] = iss.get("summary", "")

    return result


def _extract_version_from_jira_summary(summary):
    """
    Extract the goldimage version from a Jira EPIC summary.
    Strips prefixes like 'Release Gold image ', 'PC:Release gold image ', etc.
    """
    cleaned = re.sub(r"(?i)^(PC\s*:\s*)?Release\s+[Gg]old\s+image\s+", "", summary).strip()
    cleaned = re.sub(r"\s*\(#\d+\)$", "", cleaned).strip()
    return cleaned


def validate_version_with_jira(heading_version, file_version, release_type):
    """
    Validate GoldImage version using 3-way comparison: heading vs Jira vs variables.sh.

    Resolution:
      1. Search Jira with heading version
      2. If Jira EPIC contains same version as heading → CONFIRMED (use heading)
      3. If Jira not found with heading, try file version
      4. If Jira matches file version → use file version (heading was overridden)
      5. If all differ → flag for manual verification

    Returns:
        {
            "confirmed_version": str,  — the version to use in the table
            "epic_key": str or None,
            "source": str,             — "heading+jira", "file+jira", "heading_only", "file_only"
            "jira_version": str or None,
        }
    """
    # Step 1: Search Jira with heading version
    if heading_version:
        jira_result = search_jira_epic_full(heading_version, release_type)
        if jira_result:
            jira_ver = jira_result["jira_version"]
            # Check if Jira version matches heading (may be substring match)
            if jira_ver and (heading_version in jira_ver or jira_ver in heading_version
                            or heading_version == jira_ver):
                return {
                    "confirmed_version": heading_version,
                    "epic_key": jira_result["key"],
                    "source": "heading+jira",
                    "jira_version": jira_ver,
                }

    # Step 2: If heading lookup failed, try file version
    if file_version and file_version != heading_version:
        jira_result = search_jira_epic_full(file_version, release_type)
        if jira_result:
            jira_ver = jira_result["jira_version"]
            if jira_ver and (file_version in jira_ver or jira_ver in file_version
                            or file_version == jira_ver):
                return {
                    "confirmed_version": file_version,
                    "epic_key": jira_result["key"],
                    "source": "file+jira",
                    "jira_version": jira_ver,
                }

    # Step 3: No Jira confirmation — use heading as default (source of truth)
    if heading_version:
        return {
            "confirmed_version": heading_version,
            "epic_key": None,
            "source": "heading_only",
            "jira_version": None,
        }

    # Step 4: No heading — fall back to file
    return {
        "confirmed_version": file_version,
        "epic_key": None,
        "source": "file_only",
        "jira_version": None,
    }


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


# ---------------------------------------------------------------------------
# Release Parsing & Row Building
# ---------------------------------------------------------------------------

def _extract_heading_versions(title_clean):
    """
    Extract GoldImage versions from commit heading (source of truth).

    Strip 'Release gold image ' / 'Release Gold image ' prefix.
    Combined release title formats:
      - "Release gold image <AOS>/PC:Release gold image <PC>"
      - "Release gold image <AOS>/PC : Release gold image <PC>"
      - "Release gold image <AOS>/Release gold image <PC>"  (older format)

    Returns: {"aos": "<version>" or None, "pc": "<version>" or None}
    """
    heading_aos = None
    heading_pc = None

    # Try splitting on /PC: or /PC : (newer format)
    pc_split = re.split(r"/PC\s*:\s*", title_clean, maxsplit=1)
    if len(pc_split) == 2:
        aos_part = pc_split[0].strip()
        pc_part = pc_split[1].strip()
        heading_aos = re.sub(r"(?i)^release\s+gold\s+image\s+", "", aos_part).strip()
        heading_pc = re.sub(r"(?i)^release\s+gold\s+image\s+", "", pc_part).strip()
        return {"aos": _clean_version(heading_aos), "pc": _clean_version(heading_pc)}

    # Try splitting on /Release gold image (older combined format with prefix on PC part)
    combined_split = re.split(r"/Release\s+[Gg]old\s+image\s+", title_clean, maxsplit=1)
    if len(combined_split) == 2:
        aos_part = combined_split[0].strip()
        pc_part = combined_split[1].strip()
        heading_aos = re.sub(r"(?i)^release\s+gold\s+image\s+", "", aos_part).strip()
        heading_pc = pc_part.strip()
        return {"aos": _clean_version(heading_aos), "pc": _clean_version(heading_pc)}

    # Strip prefix for remaining cases
    single = re.sub(r"(?i)^release\s+gold\s+image\s+", "", title_clean).strip()
    single = re.sub(r"\s*\(#\d+\)$", "", single).strip()

    # Try splitting on / for combined format without prefix on PC part
    # e.g. "main-master-rhel9.6-7.4.0/main-master-rhel9.6-5.3.0"
    if "/" in single:
        parts = single.split("/", 1)
        left = parts[0].strip()
        right = parts[1].strip()
        if re.match(r"^main-", left) and re.match(r"^main-", right):
            return {"aos": _clean_version(left), "pc": _clean_version(right)}

    # Single-component release
    if single:
        if "ganges-pc" in single.lower() or re.match(r".*-pc\.", single.lower()):
            heading_pc = single
        else:
            heading_aos = single

    return {"aos": _clean_version(heading_aos), "pc": _clean_version(heading_pc)}


def _clean_version(ver):
    """Strip trailing PR number references and whitespace from a version string."""
    if not ver:
        return None
    ver = re.sub(r"\s*\(#\d+\)$", "", ver).strip()
    return ver if ver else None


def _extract_rhel_suffix(v):
    """'main-ganges-7.6-rhel9.7-9.1.0' -> 'rhel9.7-9.1.0'"""
    m = re.search(r'(rhel\d+\.\d+-\d+\.\d+\.\d+)$', v)
    return m.group(1) if m else None


def _extract_num_suffix(v):
    """'main-ganges-7.6-rhel9.7-9.1.0' -> '9.1.0'"""
    m = re.search(r'(\d+\.\d+\.\d+)$', v)
    return m.group(1) if m else None


def _match_gerrit_and_extract(version, gerrit_commits, excluded_titles,
                              github_message, release_type="AOS"):
    """Match the best Gerrit commit for a single version and extract changelog fields.

    Scores each Gerrit commit against *version* (the heading version for one
    component — AOS or PC) and extracts type-specific changelog fields from
    the best match.

    *release_type* ("AOS" or "PC") is used to prefer Gerrit commits whose
    title matches the component — e.g. ``PC : Release gold image ...`` for PC,
    and non-PC-prefixed titles for AOS.

    Returns dict with keys: release_pr_link, gerrit_cr_url, gerrit_date,
    tickets_resolved, associated_prs, commit_message.
    """
    if not version:
        return {
            "release_pr_link": "",
            "gerrit_cr_url": "",
            "gerrit_date": None,
            "tickets_resolved": [],
            "associated_prs": [],
            "commit_message": github_message,
        }

    # Initial values from the GitHub commit message (fallback)
    tickets_match = re.search(
        r'Tickets\s+Resolved\s*:\s*(.+?)(?:\n|$)', github_message, re.IGNORECASE)
    tickets_resolved = (
        [t.strip() for t in tickets_match.group(1).split(",") if t.strip()]
        if tickets_match else []
    )

    associated_prs = []
    prs_block = re.search(
        r"PR'?s?\s+included\s+in\s+GI\s*[:;](.*?)(?:\n\n|\nChange-Id:|\nEpic|\nTarget|\Z)",
        github_message, re.IGNORECASE | re.DOTALL)
    if prs_block:
        for line in prs_block.group(1).strip().split("\n"):
            line = line.strip().lstrip("-").strip()
            if line and ("github.com" in line or line.startswith("http")):
                associated_prs.append(line)

    pr_num_match = re.search(r'\(#(\d+)\)\s*$', github_message.split("\n")[0])
    release_pr_link = (
        f"https://github.com/nutanix-core/aos-goldimage-os/pull/{pr_num_match.group(1)}"
        if pr_num_match else ""
    )

    # Score Gerrit commits against this single version
    _is_pc = release_type.upper() == "PC"

    def _score(gc_title):
        score = 0
        if version in gc_title:
            score += 100
        elif _extract_rhel_suffix(version) and _extract_rhel_suffix(version) in gc_title:
            score += 10
        elif _extract_num_suffix(version) and _extract_num_suffix(version) in gc_title:
            score += 1
        if score == 0:
            return 0
        # Boost/penalize based on PC prefix matching the requested type
        has_pc_prefix = bool(re.match(r'^PC\s*:', gc_title, re.IGNORECASE))
        if _is_pc and has_pc_prefix:
            score += 50
        elif _is_pc and not has_pc_prefix:
            score -= 25
        elif not _is_pc and has_pc_prefix:
            score -= 25
        return score

    best_gc = None
    best_score = 0
    for gc in gerrit_commits:
        gc_title = gc.get("title", "")
        if gc_title in excluded_titles:
            continue
        s = _score(gc_title)
        if s > best_score:
            best_score = s
            best_gc = gc

    gerrit_cr_url = ""
    gerrit_date = None
    if best_gc:
        gerrit_cr_url = best_gc.get("url", "")
        gerrit_date = best_gc.get("date")
        gc_msg = best_gc.get("message", "")
        tm = re.search(r'Tickets\s+Resolved\s*:\s*(.+?)(?:\n|$)',
                       gc_msg, re.IGNORECASE)
        if tm:
            tickets_resolved = [
                t.strip() for t in tm.group(1).split(",") if t.strip()]
        pb = re.search(
            r"PR'?s?\s+included\s+in\s+GI\s*[:;](.*?)(?:\n\n|\nChange-Id:|\Z)",
            gc_msg, re.IGNORECASE | re.DOTALL)
        if pb:
            prs_from_gerrit = []
            for line in pb.group(1).strip().split("\n"):
                line = line.strip().lstrip("-").strip()
                if line and ("github.com" in line or line.startswith("http")):
                    prs_from_gerrit.append(line)
            if prs_from_gerrit:
                associated_prs = prs_from_gerrit
        rp = re.search(r'GI\s+Release\s+PR\s*:\s*(https?://\S+)',
                       gc_msg, re.IGNORECASE)
        if rp:
            release_pr_link = rp.group(1)

    return {
        "release_pr_link": release_pr_link,
        "gerrit_cr_url": gerrit_cr_url,
        "gerrit_date": gerrit_date,
        "tickets_resolved": tickets_resolved,
        "associated_prs": associated_prs,
        "commit_message": github_message,
    }


def parse_releases(server_key, github_commits, gerrit_commits, github_epics, branch, filter_type):
    """
    Parse release commits using commit heading as source of truth for GoldImage Version.

    Resolution flow:
      1. Extract version from COMMIT HEADING (authoritative)
      2. Read services/variables.sh for VERSION_GI (validation)
      3. Compare heading vs variables.sh — flag mismatches
      4. Use heading version for EPIC lookup and table display
      5. Fall back to variables.sh only if heading cannot be parsed

    A reverted release is excluded unless it was re-merged later with the same heading.
    """
    # Build revert set from both Gerrit and GitHub commits
    reverted_titles = set()
    remerged_titles = set()

    all_commits = sorted(
        github_commits + gerrit_commits,
        key=lambda x: x.get("date", ""),
    )

    for c in all_commits:
        title = c.get("title", "")
        if title.startswith("Revert"):
            m = re.search(r'"(.+?)"', title)
            if m:
                reverted_raw = m.group(1)
                reverted_clean = re.sub(r"\s*\(#\d+\)$", "", reverted_raw).strip()
                reverted_titles.add(reverted_clean)
        elif re.match(r"^(Release|PC\s*:)", title, re.IGNORECASE):
            title_clean = re.sub(r"\s*\(#\d+\)$", "", title).strip()
            if title_clean in reverted_titles:
                remerged_titles.add(title_clean)

    excluded_titles = reverted_titles - remerged_titles

    rows = []
    mismatches = []
    seen_versions = set()

    sorted_commits = sorted(github_commits, key=lambda x: x.get("date", ""), reverse=True)

    for c in sorted_commits:
        title = c.get("title", "")
        message = c.get("message", "")
        date = c.get("date", "N/A")
        commit_sha = c.get("commit", "")

        if title.startswith("Revert"):
            continue
        title_clean = re.sub(r"\s*\(#\d+\)$", "", title).strip()
        if title_clean in excluded_titles:
            continue
        if not re.match(r"^(Release|PC\s*:)", title, re.IGNORECASE):
            continue

        # Step 1: Extract version from COMMIT HEADING (source of truth)
        heading_versions = _extract_heading_versions(title_clean)
        heading_aos = heading_versions.get("aos")
        heading_pc = heading_versions.get("pc")

        # Step 2: Read services/variables.sh for VERSION_GI (validation)
        file_versions = fetch_version_gi(server_key, commit_sha)
        file_aos = file_versions.get("aos") if file_versions else None
        file_pc = file_versions.get("pc") if file_versions else None

        # Step 3: Validate heading version with Jira (3-way: heading vs Jira vs file)
        aos_validation = None
        pc_validation = None

        if heading_aos or file_aos:
            aos_validation = validate_version_with_jira(heading_aos, file_aos, "aos")
        if heading_pc or file_pc:
            pc_validation = validate_version_with_jira(heading_pc, file_pc, "pc")

        aos_version = aos_validation["confirmed_version"] if aos_validation else None
        pc_version = pc_validation["confirmed_version"] if pc_validation else None

        if not aos_version and not pc_version:
            _log(f"  Skipping {commit_sha[:8]}: no version confirmed")
            continue

        # Detect mismatches between confirmed version and variables.sh
        if aos_version and file_aos and aos_version != file_aos:
            mismatches.append({
                "commit": commit_sha,
                "type": "AOS",
                "heading_version": heading_aos,
                "file_version": file_aos,
                "confirmed_version": aos_version,
                "source": aos_validation["source"] if aos_validation else "unknown",
                "epic_key": aos_validation.get("epic_key") if aos_validation else None,
            })
        if pc_version and file_pc and pc_version != file_pc:
            mismatches.append({
                "commit": commit_sha,
                "type": "PC",
                "heading_version": heading_pc,
                "file_version": file_pc,
                "confirmed_version": pc_version,
                "source": pc_validation["source"] if pc_validation else "unknown",
                "epic_key": pc_validation.get("epic_key") if pc_validation else None,
            })

        # Resolve EPICs using heading version (not variables.sh version)
        epic_match = re.search(r"Epic'?s?\s*:\s*(.+?)(?:\n|$)", message, re.IGNORECASE)
        gerrit_epics = re.findall(r"(ENG-\d+)", epic_match.group(1)) if epic_match else []

        gh_epics = []
        if title_clean in github_epics:
            gh_epics = github_epics[title_clean]
        else:
            for gh_title, epics in github_epics.items():
                if aos_version and aos_version in gh_title:
                    # Verify the match is in the AOS portion (before /PC:), not
                    # a coincidental match in the PC portion of another commit.
                    aos_part = gh_title.split("/PC:")[0] if "/PC:" in gh_title else gh_title
                    if aos_version in aos_part:
                        gh_epics = epics
                        break

        is_combined = bool(aos_version and pc_version)

        # Extract changelog fields per-type from their respective Gerrit
        # branch commits.  For combined releases, AOS and PC have separate
        # commits on ganges-X.Y-stable vs ganges-X.Y-stable-pc with
        # different tickets, PRs, release PR links, and CR URLs.
        _aos_extra = _match_gerrit_and_extract(
            aos_version, gerrit_commits, excluded_titles, message,
            release_type="AOS"
        ) if aos_version else None
        _pc_extra = _match_gerrit_and_extract(
            pc_version, gerrit_commits, excluded_titles, message,
            release_type="PC"
        ) if pc_version else None

        # Per-type Gerrit dates (AOS and PC may merge on different dates)
        aos_gerrit_date = _aos_extra["gerrit_date"] if _aos_extra else None
        pc_gerrit_date = _pc_extra["gerrit_date"] if _pc_extra else None
        # Fallback: use whichever type has a date for the shared gerrit_date
        gerrit_date_any = aos_gerrit_date or pc_gerrit_date

        # Build AOS row (Jira-confirmed version)
        # Track (version, type) tuples to avoid cross-type dedup when AOS and PC
        # from different commits share the same version string.
        if aos_version and filter_type in ("all", "aos") and (aos_version, "AOS") not in seen_versions:
            seen_versions.add((aos_version, "AOS"))
            aos_epic = _resolve_epic(
                "aos", gerrit_epics, gh_epics, aos_version, message, is_combined=is_combined
            )
            if _is_valid_ticket(aos_epic):
                _upgrade_mismatch_source(mismatches, commit_sha, "AOS", aos_epic)
            else:
                aos_epic = (aos_validation.get("epic_key") if aos_validation else None)
                if not aos_epic or not _is_valid_ticket(aos_epic):
                    aos_epic = "--"
            aos_merge = aos_gerrit_date or gerrit_date_any or date
            urls = build_endor_urls(aos_version, "aos", branch)
            row_data = {
                "goldimage_version": aos_version,
                "type": "AOS",
                "main_ticket": aos_epic,
                "changelog_url": urls["changelog"],
                "rpm_url": urls["rpm"],
                "merge_date": format_merge_date(aos_merge),
                "github_date": format_merge_date(date),
                "sg_date": format_merge_date(aos_gerrit_date) if aos_gerrit_date else "N/A",
                "notes": branch,
                "commit": commit_sha,
            }
            row_data.update(_aos_extra)
            rows.append(row_data)

        # Build PC row (Jira-confirmed version)
        if pc_version and filter_type in ("all", "pc") and (pc_version, "PC") not in seen_versions:
            seen_versions.add((pc_version, "PC"))
            pc_epic = _resolve_epic(
                "pc", gerrit_epics, gh_epics, pc_version, message, is_combined=is_combined
            )
            if _is_valid_ticket(pc_epic):
                _upgrade_mismatch_source(mismatches, commit_sha, "PC", pc_epic)
            else:
                pc_epic = (pc_validation.get("epic_key") if pc_validation else None)
                if not pc_epic or not _is_valid_ticket(pc_epic):
                    pc_epic = "--"
            pc_merge = pc_gerrit_date or gerrit_date_any or date
            urls = build_endor_urls(pc_version, "pc", branch)
            row_data = {
                "goldimage_version": pc_version,
                "type": "PC",
                "main_ticket": pc_epic,
                "changelog_url": urls["changelog"],
                "rpm_url": urls["rpm"],
                "merge_date": format_merge_date(pc_merge),
                "github_date": format_merge_date(date),
                "sg_date": format_merge_date(pc_gerrit_date) if pc_gerrit_date else "N/A",
                "notes": branch,
                "commit": commit_sha,
            }
            row_data.update(_pc_extra)
            rows.append(row_data)

    if mismatches:
        _log(f"Version mismatches detected: {len(mismatches)} (heading vs variables.sh)")
        for mm in mismatches:
            _log(f"  [{mm['type']}] commit {mm['commit'][:7]}: "
                 f"heading={mm['heading_version']} vs file={mm['file_version']}")

    return rows, mismatches


def _is_valid_ticket(key):
    """Return True if the key looks like a valid Jira ticket (e.g. ENG-123456)."""
    if not key or key == "--":
        return False
    return bool(re.match(r"[A-Z]+-\d+", key))


def _upgrade_mismatch_source(mismatches, commit_sha, rtype, epic_key):
    """
    When an EPIC is resolved via commit message/fallback and it uses the heading version,
    that confirms heading+Jira alignment. Upgrade the mismatch source accordingly.
    """
    for mm in mismatches:
        if mm["commit"] == commit_sha and mm["type"] == rtype:
            if mm["source"] in ("heading_only", "unknown"):
                mm["source"] = "heading+jira"
                mm["epic_key"] = epic_key
            break


def _resolve_epic(rtype, gerrit_epics, gh_epics, version, message, is_combined):
    """
    Resolve EPIC ticket with priority:
    1. Explicit Epic's field (Gerrit or GitHub) — only if ticket looks complete
    2. Jira EPIC search (using raw VERSION_GI)
    3. Tickets Resolved first relevant ticket
    """
    if gh_epics:
        if is_combined and len(gh_epics) >= 2:
            candidate = gh_epics[1] if rtype == "pc" else gh_epics[0]
        else:
            candidate = gh_epics[0]
        if _is_valid_ticket(candidate):
            return candidate

    if gerrit_epics:
        if is_combined and len(gerrit_epics) >= 2:
            candidate = gerrit_epics[1] if rtype == "pc" else gerrit_epics[0]
        else:
            candidate = gerrit_epics[0]
        if _is_valid_ticket(candidate):
            return candidate

    epic = search_jira_epic(version, rtype)
    if epic:
        return epic

    tickets_match = re.search(r"Tickets?\s*Resolved\s*:\s*(.+?)(?:\n|$)", message, re.IGNORECASE)
    if tickets_match:
        all_tix = re.findall(r"(ENG-\d+)", tickets_match.group(1))
        if all_tix:
            return all_tix[0]

    return "--"


def _is_valid_ticket(ticket):
    """Check if a ticket ID looks complete (not truncated). Valid: ENG-887591, Invalid: ENG-9."""
    m = re.match(r"^ENG-(\d+)$", ticket)
    return m is not None and len(m.group(1)) >= 5


# ---------------------------------------------------------------------------
# Output Formatters
# ---------------------------------------------------------------------------

def format_table(rows, validate_urls=False, with_github_date=False, with_sg_date=False):
    """Format rows as the standard GoldImage release table."""
    if not rows:
        print("No releases found.")
        return

    if validate_urls:
        _log("Validating URLs...")
        for row in rows:
            row["changelog_valid"] = validate_url(row["changelog_url"])
            row["rpm_valid"] = validate_url(row["rpm_url"])
            if not row["changelog_valid"]:
                row["changelog_url"] = "Data not found"
            if not row["rpm_valid"]:
                row["rpm_url"] = "Data not found"

    hdr = f"| {'GoldImage Version':<45} | {'Main Ticket':<14} | {'Change Log':<90} | {'RPM List':<90} | {'Merge Date':<12} |"
    sep = f"|{'-'*47}|{'-'*16}|{'-'*92}|{'-'*92}|{'-'*14}|"
    if with_github_date:
        hdr += f" {'PR Merge Date':<14} |"
        sep += f"{'-'*16}|"
    if with_sg_date:
        hdr += f" {'CR Merge Date':<14} |"
        sep += f"{'-'*16}|"
    hdr += f" {'Notes':<8} |"
    sep += f"{'-'*10}|"

    print(f"\n{hdr}")
    print(sep)

    for row in rows:
        line = f"| {row['goldimage_version']:<45} | {row['main_ticket']:<14} | {row['changelog_url']:<90} | {row['rpm_url']:<90} | {row['merge_date']:<12} |"
        if with_github_date:
            line += f" {row.get('github_date', 'N/A'):<14} |"
        if with_sg_date:
            line += f" {row.get('sg_date', 'N/A'):<14} |"
        line += f" {row['notes']:<8} |"
        print(line)

    print(f"\nTotal: {len(rows)} release(s)")


def format_markdown(rows, validate_urls=False, with_github_date=False, with_sg_date=False):
    """Format as a cleaner markdown table with linked URLs."""
    if not rows:
        print("No releases found.")
        return

    if validate_urls:
        _log("Validating URLs...")
        for row in rows:
            if not validate_url(row["changelog_url"]):
                row["changelog_url"] = ""
            if not validate_url(row["rpm_url"]):
                row["rpm_url"] = ""

    hdr = "| GoldImage Version | Main Ticket | Change Log | RPM List | Merge Date |"
    sep = "|---|---|---|---|---|"
    if with_github_date:
        hdr += " PR Merge Date |"
        sep += "---|"
    if with_sg_date:
        hdr += " CR Merge Date |"
        sep += "---|"
    hdr += " Notes |"
    sep += "---|"

    print(f"\n{hdr}")
    print(sep)

    for row in rows:
        cl = f"[changelog]({row['changelog_url']})" if row["changelog_url"] else "Data not found"
        rpm = f"[rpm]({row['rpm_url']})" if row["rpm_url"] else "Data not found"
        line = f"| {row['goldimage_version']} | {row['main_ticket']} | {cl} | {rpm} | {row['merge_date']} |"
        if with_github_date:
            line += f" {row.get('github_date', 'N/A')} |"
        if with_sg_date:
            line += f" {row.get('sg_date', 'N/A')} |"
        line += f" {row['notes']} |"
        print(line)

    print(f"\nTotal: {len(rows)} release(s)")


# ---------------------------------------------------------------------------
# Artifactory RPM download
# ---------------------------------------------------------------------------

ARTIFACTORY_BASE = (
    "https://artifactory.dyn.ntnxdpro.com:443/artifactory/"
    "local-canaveral-generic/nutanix-core/aos-goldimage/os/"
    "build-artifacts/{build_num}"
)
ARTIFACTORY_API_STORAGE = (
    "https://artifactory.dyn.ntnxdpro.com:443/artifactory/"
    "api/storage/local-canaveral-generic/nutanix-core/aos-goldimage/os/"
    "build-artifacts/{build_num}"
)


def _extract_build_number(ci_url):
    """Extract the CircleCI build number from a target_url.

    Example URL:
        https://circleci4.corp.p10y.ntnxdpro.com/gh/nutanix-core/aos-goldimage-os/28658
    Returns: '28658'
    """
    if not ci_url:
        return None
    m = re.search(r'/(\d+)$', ci_url)
    return m.group(1) if m else None


_rpm_url_cache = {}


def _resolve_rpm_url(build_num, rtype, art_token=None):
    """Resolve the rpm.txt download URL by listing the build directory.

    Lists ``build-artifacts/{build_num}/`` via the Artifactory storage API
    and matches files against ``*rpm.txt`` for the requested type (cvm/pcvm).
    Results are cached per build_num so repeated calls (current + previous
    release) don't make extra API requests.
    """
    cache_key = build_num
    if cache_key not in _rpm_url_cache:
        _rpm_url_cache[cache_key] = _list_build_dir(build_num, art_token)

    children = _rpm_url_cache[cache_key]
    base = ARTIFACTORY_BASE.format(build_num=build_num)
    prefix = "pcvm" if rtype.upper() == "PC" else "cvm"

    for uri in children:
        if re.search(r'rpm\.txt$', uri) and uri.startswith(f"/{prefix}-"):
            return f"{base}{uri}"

    # Plain rpm.txt (master / older builds that don't use prefixed names)
    for uri in children:
        if uri == "/rpm.txt":
            return f"{base}/rpm.txt"

    return None


def _list_build_dir(build_num, art_token=None):
    """List file URIs inside a build-artifacts directory. Returns list of URI strings."""
    dir_url = ARTIFACTORY_API_STORAGE.format(build_num=build_num)
    req = urllib.request.Request(dir_url)
    if art_token:
        req.add_header("Authorization", f"Bearer {art_token}")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
        return [c.get("uri", "") for c in data.get("children", [])]
    except Exception:
        return []


def _download_one_rpm(url, dest_path, art_token=None):
    """Download a single rpm.txt file. Returns (dest_path, size) or None."""
    dest_dir = os.path.dirname(dest_path)
    os.makedirs(dest_dir, exist_ok=True)
    req = urllib.request.Request(url)
    if art_token:
        req.add_header("Authorization", f"Bearer {art_token}")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = resp.read()
        with open(dest_path, "wb") as f:
            f.write(data)
        return dest_path, len(data)
    except (urllib.error.HTTPError, urllib.error.URLError, OSError) as e:
        _log(f"  Download failed for {dest_path}: {e}")
        return None


def download_rpm_artifacts(rows, prev_rows, output_dir="goldimage",
                           filter_type="all"):
    """Download rpm.txt and old_rpm.txt from Artifactory for AOS and/or PC.

    For each release row, downloads the current release's rpm.txt and the
    previous release's rpm.txt (saved as old_rpm.txt) concurrently::

        <output_dir>/<goldimage_version>/AOS/rpm.txt      (current)
        <output_dir>/<goldimage_version>/AOS/old_rpm.txt  (previous)
        <output_dir>/<goldimage_version>/PC/rpm.txt       (current)
        <output_dir>/<goldimage_version>/PC/old_rpm.txt   (previous)

    Artifactory stores rpm.txt with versioned filenames (e.g.
    ``cvm-<ver>-<branch>-<sha7>-<build>-x86_64-rpm.txt``).  The
    ``_resolve_rpm_url`` helper resolves the correct filename via a HEAD
    check, falling back to a directory listing if needed.

    Args:
        rows:        current release rows (with ci_cvm / ci_pcvm populated)
        prev_rows:   dict mapping release type ("AOS"/"PC") to the list of
                     previous-release rows, one per current row, in the same
                     order.  A value of None means no previous release exists.
        output_dir:  base directory for downloads.
        filter_type: "all" downloads both AOS and PC, "aos" only AOS,
                     "pc" only PC.

    Returns:
        list of dicts: rtype, version, file, path.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    art_token = (_get_env("ARTIFACTORY_TOKEN")
                 or _get_env("ARTIFACTORY_API_KEY"))

    ci_key_for_type = {"AOS": "ci_cvm", "PC": "ci_pcvm"}
    allowed_types = {"aos": {"AOS"}, "pc": {"PC"}, "all": {"AOS", "PC"}}
    allowed = allowed_types.get(filter_type, {"AOS", "PC"})
    tasks = []

    _log("Resolving Artifactory RPM URLs...")

    for row in rows:
        version = row.get("goldimage_version", "unknown")
        rtype = row.get("type", "AOS").upper()
        if rtype not in allowed:
            continue
        ci_key = ci_key_for_type.get(rtype, "ci_cvm")

        build_num = _extract_build_number(
            row.get(ci_key, {}).get("url", ""))
        if build_num:
            url = _resolve_rpm_url(build_num, rtype, art_token)
            if url:
                dest = os.path.join(output_dir, version, rtype, "rpm.txt")
                tasks.append((rtype, version, "rpm.txt", url, dest))

        prev_row = prev_rows.get(rtype, {}).get(version)
        if prev_row:
            prev_build = _extract_build_number(
                prev_row.get(ci_key, {}).get("url", ""))
            if prev_build:
                prev_url = _resolve_rpm_url(prev_build, rtype, art_token)
                if prev_url:
                    prev_ver = prev_row.get("goldimage_version", "unknown")
                    prev_dest = os.path.join(
                        output_dir, version, rtype, "old_rpm.txt")
                    tasks.append((rtype, version,
                                  f"old_rpm.txt (from {prev_ver})",
                                  prev_url, prev_dest))

    downloaded = []
    if not tasks:
        _log("No builds to download")
        return downloaded

    _log(f"Downloading {len(tasks)} file(s) in parallel...")

    with ThreadPoolExecutor(max_workers=4) as pool:
        future_map = {}
        for rtype, version, label, url, dest in tasks:
            _log(f"[{rtype}] {label} build → {os.path.basename(dest)} "
                 f"(version {version})")
            fut = pool.submit(_download_one_rpm, url, dest, art_token)
            future_map[fut] = (rtype, version, label, dest)

        for fut in as_completed(future_map):
            rtype, version, label, dest = future_map[fut]
            result = fut.result()
            if result:
                path, size = result
                downloaded.append({"rtype": rtype, "version": version,
                                   "file": label, "path": path})
                _log(f"[{rtype}] Saved {os.path.basename(dest)} → "
                     f"{path} ({size} bytes)")

    return downloaded


# ---------------------------------------------------------------------------
# Changelog generation
# ---------------------------------------------------------------------------

_TEMPLATE_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "templates", "changelog.template",
)


def _load_template():
    """Read the changelog template file."""
    with open(_TEMPLATE_PATH) as f:
        return f.read()


def _fill_template(template, row, prev_version, ticket_summaries=None):
    """Populate the changelog template with release row data."""
    ticket_summaries = ticket_summaries or {}
    current_ver = row.get("goldimage_version", "N/A")
    main_ticket = row.get("main_ticket", "--")
    # Extract bare ticket key — template wraps it in Jira browse URL
    m = re.search(r'([A-Z]+-\d+)', main_ticket)
    main_ticket_key = m.group(1) if m else main_ticket
    release_pr = row.get("release_pr_link", "")
    gerrit_cr = row.get("gerrit_cr_url", "")
    associated_prs = row.get("associated_prs", [])
    tickets = row.get("tickets_resolved", [])

    text = template
    text = text.replace("{Current_Gold_Image_Version}", current_ver)
    text = text.replace("{Previous_Gold_Image_Version}", prev_version)
    text = text.replace("{MAIN_JIRA_EPIC}", main_ticket_key)
    text = text.replace("{RELEASE_PR_LINK}", release_pr)
    text = text.replace("{GERRIT_CR_LINK}", gerrit_cr)

    # Expand associated_prs loop ({% for %}...{% endfor %})
    prs_block = re.search(
        r'\{%\s*for\s+PR\s+in\s+associated_prs\s*%\}(.*?)\{%\s*endfor\s*%\}',
        text, re.DOTALL)
    if prs_block:
        if associated_prs:
            pr_lines = "\n".join(f"- {pr}" for pr in associated_prs)
        else:
            pr_lines = "- N/A"
        text = text[:prs_block.start()] + pr_lines + text[prs_block.end():]

    # Expand tickets loop ({% for %}...{% else %})
    # Template filters out tickets containing "Release"
    tkt_block = re.search(
        r'\{%\s*for\s+jira_id\s+in\s+JIRA_TICKETS_FOR_PR\s*%\}(.*?)\{%\s*else\s*%\}',
        text, re.DOTALL)
    if tkt_block:
        if tickets:
            tkt_lines = []
            for t in tickets:
                key = t.split()[0].rstrip(" -:")
                summary = ticket_summaries.get(key, "")
                label = f"{key} - {summary}" if summary else t
                if "Release" in label:
                    continue
                tkt_lines.append(label)
            tkt_text = "\n".join(tkt_lines) if tkt_lines else "N/A"
        else:
            tkt_text = "N/A"
        text = text[:tkt_block.start()] + tkt_text + text[tkt_block.end():]

    return text


def generate_changelog(rows, prev_rows, output_dir, filter_type="all",
                       branch="master"):
    """Generate changelog.txt for each release version.

    For each row:
      1. Fill the changelog template with extracted data.
      2. Write changelog.txt into ``<output_dir>/<version>/<AOS|PC>/``.
      3. Run ``diff -y old_rpm.txt rpm.txt --suppress-common-lines``
         and append the output to changelog.txt.

    Args:
        rows:        current release rows (enriched with changelog fields).
        prev_rows:   dict mapping type -> {version: prev_row} (same as
                     used by download_rpm_artifacts).
        output_dir:  base releases directory.
        filter_type: "all", "aos", or "pc".
        branch:      GitHub branch name used to match Gerrit CR by branch.
    """
    import subprocess
    from concurrent.futures import ThreadPoolExecutor, as_completed

    template = _load_template()

    # Batch-fetch Jira summaries for all tickets across all rows
    all_ticket_keys = set()
    for row in rows:
        for t in row.get("tickets_resolved", []):
            key = t.split()[0].rstrip(" -:")
            if re.match(r'^[A-Z]+-\d+$', key):
                all_ticket_keys.add(key)
    ticket_summaries = {}
    if all_ticket_keys:
        _log(f"Fetching Jira summaries for {len(all_ticket_keys)} ticket(s)...")
        ticket_summaries = fetch_ticket_summaries(list(all_ticket_keys))

    # Fetch Gerrit CR URLs from Jira git-tracker comments (parallel per row)
    _log("Fetching Gerrit CR URLs from Jira ticket comments...")
    cr_url_cache = {}  # ticket_keys_tuple -> cr_url

    def _fetch_cr_for_row(row):
        ver = row.get("goldimage_version", "")
        tickets = row.get("tickets_resolved", [])
        keys = [t.split()[0].rstrip(" -:") for t in tickets
                if re.match(r'^[A-Z]+-\d+$', t.split()[0].rstrip(" -:"))]
        main_tkt = row.get("main_ticket", "")
        epic_match = re.search(r'([A-Z]+-\d+)', main_tkt)
        if epic_match:
            epic_key = epic_match.group(1)
            if epic_key not in keys:
                keys.append(epic_key)
        if not keys:
            return ver, ""
        cache_key = tuple(sorted(keys))
        if cache_key in cr_url_cache:
            return ver, cr_url_cache[cache_key]
        cr = fetch_gerrit_cr_from_jira(keys, branch)
        cr_url_cache[cache_key] = cr
        return ver, cr

    cr_urls = {}
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(_fetch_cr_for_row, r): r for r in rows}
        for fut in as_completed(futures):
            ver, cr = fut.result()
            if cr:
                cr_urls[ver] = cr

    generated = []

    ci_key_map = {"AOS": "ci_cvm", "PC": "ci_pcvm"}

    for row in rows:
        version = row.get("goldimage_version", "unknown")
        rtype = row.get("type", "AOS")
        ci_key = ci_key_map.get(rtype, "ci_cvm")

        # Override gerrit_cr_url with the one from Jira comments if found
        if version in cr_urls:
            row["gerrit_cr_url"] = cr_urls[version]

        build_num = _extract_build_number(
            row.get(ci_key, {}).get("url", ""))
        if not build_num:
            continue

        dest_dir = os.path.join(output_dir, version, rtype)
        changelog_path = os.path.join(dest_dir, "changelog.txt")
        rpm_path = os.path.join(dest_dir, "rpm.txt")
        old_rpm_path = os.path.join(dest_dir, "old_rpm.txt")

        prev_row = prev_rows.get(rtype, {}).get(version)
        prev_version = (prev_row.get("goldimage_version", "N/A")
                        if prev_row else "N/A")

        content = _fill_template(template, row, prev_version,
                                 ticket_summaries)

        os.makedirs(dest_dir, exist_ok=True)
        with open(changelog_path, "w") as f:
            f.write(content)

        # Append RPM diff if both files exist
        if os.path.isfile(old_rpm_path) and os.path.isfile(rpm_path):
            try:
                result = subprocess.run(
                    ["diff", "-y", old_rpm_path, rpm_path,
                     "--suppress-common-lines"],
                    capture_output=True, text=True, timeout=30,
                )
                diff_output = result.stdout
                if diff_output:
                    with open(changelog_path, "a") as f:
                        f.write(diff_output)
                    _log(f"[{rtype}] changelog.txt: {version} "
                         f"({len(diff_output.splitlines())} diff lines)")
                else:
                    with open(changelog_path, "a") as f:
                        f.write("(no RPM changes)\n")
                    _log(f"[{rtype}] changelog.txt: {version} "
                         f"(no RPM changes)")
            except (subprocess.TimeoutExpired, OSError) as e:
                _log(f"[{rtype}] diff failed for {version}: {e}")
        else:
            _log(f"[{rtype}] changelog.txt: {version} "
                 f"(rpm files missing, skipping diff)")

        generated.append({"rtype": rtype, "version": version,
                          "path": changelog_path})

    return generated


# ---------------------------------------------------------------------------
# SFTP Upload
# ---------------------------------------------------------------------------

def _sftp_makedirs(sftp, remote_dir):
    """Recursively create directories on the SFTP server."""
    dirs_to_create = []
    current = remote_dir
    while current and current not in ("/", "."):
        try:
            sftp.stat(current)
            break
        except IOError:
            dirs_to_create.append(current)
            current = os.path.dirname(current)

    for d in reversed(dirs_to_create):
        try:
            sftp.mkdir(d)
        except IOError:
            pass


def upload_to_sftp(rows, output_dir, filter_type="all"):
    """Upload generated changelog.txt and rpm.txt to the SFTP server.

    Derives the remote path from each row's changelog_url / rpm_url by
    stripping ``BASE_URL``.  The SFTP server CWD is assumed to be the
    web root (e.g. ``/public_html`` for Apache ``~user``), so the URL's
    relative path is used directly — no extra prefix needed.

    Env vars (read from tools/.env):
        SFTP_HOST, SFTP_USERNAME, SFTP_PASSWORD, SFTP_PORT (default 22).
        SFTP_REMOTE_BASE (optional) — prefix prepended to the relative
        URL path.  Defaults to empty (CWD is already the web root).

    Returns:
        list of dicts with keys: rtype, version, file, remote_path.
    """
    try:
        import paramiko
    except ImportError:
        _log("SFTP upload skipped: paramiko not installed (pip install paramiko)")
        return []

    host = _get_env("SFTP_HOST")
    username = _get_env("SFTP_USERNAME")
    password = _get_env("SFTP_PASSWORD")
    port = int(_get_env("SFTP_PORT", "22"))
    remote_base = _get_env("SFTP_REMOTE_BASE", "")

    if not host or not username:
        _log("SFTP upload skipped: SFTP_HOST or SFTP_USERNAME not set in .env")
        return []

    transport = None
    sftp = None
    uploaded = []

    try:
        transport = paramiko.Transport((host, port))
        transport.connect(username=username, password=password)
        sftp = paramiko.SFTPClient.from_transport(transport)

        for row in rows:
            version = row.get("goldimage_version", "unknown")
            rtype = row.get("type", "AOS")

            for filename, url_key in [("changelog.txt", "changelog_url"),
                                      ("rpm.txt", "rpm_url")]:
                url = row.get(url_key, "")
                if not url or url == "Data not found":
                    continue

                local_path = os.path.join(output_dir, version, rtype, filename)
                if not os.path.isfile(local_path):
                    continue

                relative = url.replace(BASE_URL, "").lstrip("/")
                if remote_base:
                    remote_path = f"{remote_base.rstrip('/')}/{relative}"
                else:
                    remote_path = relative

                remote_dir = os.path.dirname(remote_path)
                _sftp_makedirs(sftp, remote_dir)

                sftp.put(local_path, remote_path)
                uploaded.append({
                    "rtype": rtype, "version": version,
                    "file": filename, "remote_path": remote_path,
                })
                _log(f"[{rtype}] Uploaded {filename} → sftp://{host}/{remote_path}")

    except Exception as e:
        _log(f"SFTP upload error: {e}")
    finally:
        if sftp:
            sftp.close()
        if transport:
            transport.close()

    return uploaded


# ---------------------------------------------------------------------------
# Confluence Upload
# ---------------------------------------------------------------------------

def upload_to_confluence(rows, branch, filter_type="all"):
    """Upload release rows to Confluence, auto-routing to child pages.

    Reads ``CONFLUENCE_PAGE_ID`` from ``tools/.env`` as the parent page.
    For each release type present in *rows* (AOS, PC, or both when
    *filter_type* is ``all``), finds or creates a child page named
    ``<TYPE> Release <branch>`` and upserts the table rows.

    Returns:
        list of dicts with keys: release_type, added, skipped, total, page_id.
    """
    try:
        from tools.mcp_confluence_client import upload_releases
    except ImportError:
        _log("Confluence upload skipped: tools.mcp_confluence_client not available")
        return []

    parent_id = _get_env("CONFLUENCE_PAGE_ID")
    if not parent_id:
        _log("Confluence upload skipped: CONFLUENCE_PAGE_ID not set in tools/.env")
        return []

    types_in_rows = set(r.get("type", "AOS").upper() for r in rows)
    if filter_type == "aos":
        types_in_rows &= {"AOS"}
    elif filter_type == "pc":
        types_in_rows &= {"PC"}

    results = []
    for rtype in sorted(types_in_rows):
        type_rows = [r for r in rows if r.get("type", "AOS").upper() == rtype]
        if not type_rows:
            continue
        try:
            result = upload_releases(
                "atlassian",
                parent_id=parent_id,
                branch=branch,
                rows=type_rows,
                release_type=rtype,
                force_rebuild=False,
                dry_run=False,
            )
            result["release_type"] = rtype
            results.append(result)
            _log(f"[{rtype}] Confluence: +{result.get('added', 0)} rows, "
                 f"{result.get('skipped', 0)} skipped, "
                 f"{result.get('total', 0)} total on page {result.get('page_id', '?')}")
        except Exception as e:
            _log(f"[{rtype}] Confluence upload error: {e}")
            results.append({"release_type": rtype, "added": 0, "error": str(e)})

    return results


def format_json(rows, output_path=None):
    """Output as JSON."""
    data = json.dumps(rows, indent=2)
    if output_path:
        with open(output_path, "w") as f:
            f.write(data)
        _log(f"Saved {len(rows)} releases to: {output_path}")
    else:
        print(data)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Release Query — GoldImage release pipeline (Sourcegraph + Jira + GitHub CI)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python3 release_query.py --branch master --count 5 --filter pc
  python3 release_query.py --branch master --count 10 --filter aos
  python3 release_query.py --branch master --count 5 --filter all
  python3 release_query.py --branch master --count 5 --filter pc --format json --output /tmp/releases.json
  python3 release_query.py --branch master --count 3 --filter pc --validate-urls
        """,
    )
    parser.add_argument("--branch", default="master", help="Branch (default: master)")
    parser.add_argument("--count", type=int, default=5, help="Number of releases (default: 5)")
    parser.add_argument("--filter", choices=["all", "aos", "pc"], default="all",
                        help="Filter: all, aos, or pc (default: all)")
    parser.add_argument("--format", choices=["table", "markdown", "json"], default="table",
                        help="Output format (default: table)")
    parser.add_argument("--output", help="Save JSON output to file")
    parser.add_argument("--validate-urls", action="store_true",
                        help="HEAD-check changelog/RPM URLs")
    parser.add_argument("--with-github-date", action="store_true",
                        help="Add GitHub PR merge date column")
    parser.add_argument("--with-sg-date", action="store_true",
                        help="Add Sourcegraph/Gerrit CR merge date column")
    parser.add_argument("--ci-status", action="store_true", default=True,
                        help="Fetch postmerge CircleCI status (default: on)")
    parser.add_argument("--no-ci-status", action="store_true",
                        help="Skip postmerge CircleCI status fetch")
    parser.add_argument("--download-rpm", action="store_true", default=True,
                        help="Download rpm.txt from Artifactory (default: on)")
    parser.add_argument("--no-download-rpm", action="store_true",
                        help="Skip RPM download from Artifactory")
    parser.add_argument("--generate-changelog", action="store_true", default=True,
                        help="Generate changelog.txt from template (default: on)")
    parser.add_argument("--no-generate-changelog", action="store_true",
                        help="Skip changelog generation")
    parser.add_argument("--upload-sftp", action="store_true", default=True,
                        help="Upload changelog/rpm to SFTP server (default: on)")
    parser.add_argument("--no-upload-sftp", action="store_true",
                        help="Skip SFTP upload")
    parser.add_argument("--upload-confluence", action="store_true", default=True,
                        help="Upload release table to Confluence (default: on)")
    parser.add_argument("--no-upload-confluence", action="store_true",
                        help="Skip Confluence upload")
    _default_rpm_dir = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "releases"
    )
    parser.add_argument("--rpm-dir", default=_default_rpm_dir,
                        help=f"Directory for downloaded rpm.txt files (default: {_default_rpm_dir})")
    parser.add_argument("--server", default=DEFAULT_SERVER_KEY,
                        help="MCP server key from mcp.json")

    args = parser.parse_args()

    server_key = args.server

    # Fetch data
    _log(f"Fetching releases: branch={args.branch}, count={args.count}, filter={args.filter}")

    gerrit_commits = fetch_gerrit_releases(server_key, args.branch, args.count)
    _log(f"Gerrit: {len(gerrit_commits)} commits")

    github_commits = fetch_github_releases(server_key, args.branch, args.count)
    _log(f"GitHub: {len(github_commits)} commits")

    github_epics = fetch_github_epics(server_key, args.branch)
    _log(f"GitHub EPICs: {len(github_epics)} releases with Epic field")

    _log("Extracting versions from commit headings + validating against variables.sh...")
    rows, mismatches = parse_releases(
        server_key, github_commits, gerrit_commits, github_epics,
        args.branch, args.filter,
    )

    # --no-* flags override defaults
    if args.no_upload_confluence:
        args.upload_confluence = False
    if args.no_upload_sftp:
        args.upload_sftp = False
    if args.no_generate_changelog:
        args.generate_changelog = False
    if args.no_download_rpm:
        args.download_rpm = False
    if args.no_ci_status:
        args.ci_status = False

    # --upload-sftp implies --generate-changelog implies --download-rpm implies --ci-status
    if args.upload_sftp:
        args.generate_changelog = True
    if args.generate_changelog:
        args.download_rpm = True
    if args.download_rpm:
        args.ci_status = True

    display_count = args.count
    if args.download_rpm:
        # Keep extra rows so we can find one previous release per type
        # (combined AOS/PC releases produce 2 rows per commit)
        all_rows = rows
        rows = rows[:display_count]
    else:
        all_rows = None
        rows = rows[:display_count]
    _log(f"Output: {len(rows)} rows")

    # Fetch postmerge CI status if requested
    if args.ci_status:
        _log("Fetching postmerge CircleCI status from GitHub...")
        # When downloading, we also need CI for the previous releases
        ci_rows = all_rows if all_rows else rows
        seen_shas = set()
        for row in ci_rows:
            sha = row.get("commit", "")
            if not sha:
                row["ci_cvm"] = {}
                row["ci_pcvm"] = {}
                continue
            if sha in seen_shas:
                # Same commit already fetched — reuse CI data
                for prev in ci_rows:
                    if prev.get("commit") == sha and prev.get("ci_cvm"):
                        row["ci_cvm"] = prev["ci_cvm"]
                        row["ci_pcvm"] = prev["ci_pcvm"]
                        break
                continue
            seen_shas.add(sha)
            ci = fetch_postmerge_ci(sha)
            row["ci_cvm"] = ci.get("cvm", {})
            row["ci_pcvm"] = ci.get("pcvm", {})
        _log(f"CI status fetched for {len(seen_shas)} unique commit(s)")

    # Download rpm.txt + old_rpm.txt from Artifactory if requested
    if args.download_rpm:
        # Build prev_rows based on CI key availability, not row type.
        # Combined releases (AOS+PC in one commit) produce AOS-type rows
        # that also carry ci_pcvm data — these must count as PC sources.
        ci_key_map = {"AOS": "ci_cvm", "PC": "ci_pcvm"}
        prev_rows = {"AOS": {}, "PC": {}}
        for rtype, ci_key in ci_key_map.items():
            has_ci = [r for r in all_rows
                      if _extract_build_number(
                          r.get(ci_key, {}).get("url", ""))]
            for i in range(len(has_ci) - 1):
                cur_ver = has_ci[i].get("goldimage_version", "")
                prev_rows[rtype][cur_ver] = has_ci[i + 1]

        downloaded = download_rpm_artifacts(rows, prev_rows, args.rpm_dir,
                                           args.filter)
        for d in downloaded:
            _log(f"[{d['rtype']}] {d['file']} → {d['path']}")

    # Generate changelog.txt if requested (after RPMs are on disk)
    if args.generate_changelog:
        _log("Generating changelog.txt from template...")
        changelogs = generate_changelog(rows, prev_rows, args.rpm_dir,
                                        args.filter, args.branch)
        for cl in changelogs:
            _log(f"[{cl['rtype']}] changelog → {cl['path']}")

    # Upload changelog + rpm to SFTP server
    if args.upload_sftp:
        _log("Uploading files to SFTP server...")
        sftp_results = upload_to_sftp(rows, args.rpm_dir, args.filter)
        _log(f"SFTP upload complete: {len(sftp_results)} file(s) uploaded")

    # Upload release table to Confluence
    if args.upload_confluence:
        _log("Uploading release table to Confluence...")
        confluence_results = upload_to_confluence(rows, args.branch, args.filter)
        total_added = sum(r.get("added", 0) for r in confluence_results)
        total_skipped = sum(r.get("skipped", 0) for r in confluence_results)
        _log(f"Confluence upload complete: {total_added} added, {total_skipped} already exist")

    # Output
    gh_date = args.with_github_date
    sg_date = args.with_sg_date
    if args.format == "json":
        format_json(rows, args.output)
    elif args.format == "markdown":
        format_markdown(rows, args.validate_urls, with_github_date=gh_date, with_sg_date=sg_date)
    else:
        format_table(rows, args.validate_urls, with_github_date=gh_date, with_sg_date=sg_date)

    # Print version validation summary if any mismatches
    if mismatches:
        print(f"\n**Version Mismatch Summary** ({len(mismatches)} release(s) affected)\n")
        hdr = (f"| {'Release':<40} | {'EPIC':<12} | {'Heading Version':<40} "
               f"| {'Actual in variables.sh':<40} | {'Verdict':<42} |")
        sep = f"|{'-'*42}|{'-'*14}|{'-'*42}|{'-'*42}|{'-'*44}|"
        print(hdr)
        print(sep)
        for mm in mismatches:
            source = mm.get("source", "unknown")
            epic = mm.get("epic_key") or "--"
            confirmed = mm.get("confirmed_version", "--")
            heading = mm.get("heading_version") or "--"
            file_ver = mm.get("file_version") or "--"
            if source == "heading+jira":
                verdict = "Heading + Jira agree → variables.sh needs fix"
            elif source == "file+jira":
                verdict = "Jira + file agree → heading overridden"
            elif source == "heading_only":
                verdict = "No Jira confirmation → verify manually"
            else:
                verdict = "All differ → verify manually"
            print(f"| {confirmed:<40} | {epic:<12} | {heading:<40} | {file_ver:<40} | {verdict:<42} |")
        print()


if __name__ == "__main__":
    main()
