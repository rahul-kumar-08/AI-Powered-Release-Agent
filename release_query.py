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
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib import parse, error
from urllib.request import urlopen, Request

import pandas as pd
import paramiko

from src.endor import publish_to_endor, rewrite_urls_to_endor
from src.formatter import format_table as _fmt
from src.logger import Log
from tools.mcp_client import load_mcp_config
from tools.mcp_client import call_tool as _mcp_call_tool, _get_env, validate_mcp_tokens
from tools.mcp_confluence_client import upload_releases
from tools.mcp_github_client import fetch_postmerge_ci
from tools.mcp_sourcegraph_client import TOOL_PREFIX
from tools.mcp_client import load_mcp_config
from tools.mcp_confluence_client import (
        get_confluence_page_releases, parse_date as _conf_parse_date,
    )
from tools.mcp_confluence_client import TABLE_COLUMNS
from src.formatter import _compute_maxcolwidths


# ---------------------------------------------------------------------------
# Configuration (loaded from tools/.env via _get_env)
# ---------------------------------------------------------------------------

DEFAULT_SERVER_KEY = "gw-sourcegraph"

DEFAULT_REPO = _get_env("DEFAULT_REPO")
GITHUB_REPO = _get_env("GITHUB_REPO")
BASE_URL = _get_env("BASE_URL")
ARTIFACTORY_BASE = _get_env("ARTIFACTORY_BASE")
ARTIFACTORY_API_STORAGE = _get_env("ARTIFACTORY_API_STORAGE")

_BASE = BASE_URL.rstrip("/") if BASE_URL else ""
ENDOR_AOS_RHEL9_MASTER = f"{_BASE}/GoldImages/Centos_SVM/Master"
ENDOR_AOS_STS_BASE = f"{_BASE}/GoldImages/Centos_SVM/STS"
ENDOR_AOS_RHEL8_BASE = f"{_BASE}/GoldImages/Centos_SVM/STS"
ENDOR_PC_MASTER = f"{_BASE}/GoldImages/PC_GoldImages/pc"
ENDOR_PC_STS_BASE = f"{_BASE}/GoldImages/PC_GoldImages/pc"




# ---------------------------------------------------------------------------
# Pipeline Status Tracking
# ---------------------------------------------------------------------------

_pipeline_stats = {}


def _print_pipeline_status():
    """Print a summary table of all pipeline stages and their results."""
    s = _pipeline_stats
    if not s:
        return

    stages = []
    stages.append(("Releases Extracted", f"{s.get('rows', 0)} release(s)"))
    stages.append(("Gerrit Commits",     f"{s.get('gerrit_commits', 0)} commit(s)"))
    stages.append(("GitHub Commits",     f"{s.get('github_commits', 0)} commit(s)"))

    if "ci_commits" in s:
        ci_detail = f"{s['ci_commits']} commit(s) checked"
        ci_ok = s.get("ci_success", 0)
        ci_fail = s.get("ci_failure", 0)
        ci_pending = s.get("ci_pending", 0)
        parts = []
        if ci_ok:
            parts.append(f"{ci_ok} success")
        if ci_fail:
            parts.append(f"{ci_fail} failure")
        if ci_pending:
            parts.append(f"{ci_pending} pending")
        if parts:
            ci_detail += f" ({', '.join(parts)})"
        stages.append(("CI Status", ci_detail))
    elif s.get("ci_skipped"):
        stages.append(("CI Status", "skipped (--no-ci-status)"))

    if "rpm_downloaded" in s:
        stages.append(("RPM Download", f"{s['rpm_downloaded']} file(s) downloaded"))
    if "changelogs" in s:
        stages.append(("Changelog", f"{s['changelogs']} file(s) generated"))
    if "sftp_uploaded" in s:
        stages.append(("SFTP Upload", f"{s['sftp_uploaded']} file(s) uploaded"))
    if "endor_published" in s or "endor_skipped" in s:
        pub = s.get("endor_published", 0)
        skip = s.get("endor_skipped", 0)
        fail = s.get("endor_failed", 0)
        parts = []
        if pub:
            parts.append(f"{pub} published")
        if skip:
            parts.append(f"{skip} already exist")
        if fail:
            parts.append(f"{fail} failed")
        stages.append(("Endor Publish", ", ".join(parts) if parts else "0 versions"))
    elif s.get("endor_skipped_flag"):
        stages.append(("Endor Publish", "skipped (--no-publish-endor)"))
    if "confluence_added" in s or "confluence_skipped" in s:
        added = s.get("confluence_added", 0)
        skipped = s.get("confluence_skipped", 0)
        stages.append(("Confluence", f"+{added} added, {skipped} skipped"))
    elif s.get("confluence_skipped_flag"):
        stages.append(("Confluence", "skipped (--no-upload-confluence)"))

    if os.environ.get("_RELEASE_AGENT_SUBPROCESS"):
        return

    
    df = pd.DataFrame(stages, columns=["Stage", "Result"])
    print(f"\n{'=' * 26} PIPELINE STATUS {'=' * 26}", file=sys.stderr)
    print(df.to_markdown(index=False, tablefmt="simple"), file=sys.stderr)
    print("-" * 68, file=sys.stderr)


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


PC_TARBALL_BRANCHES = {"ganges-7.3", "ganges-7.5"}


def _needs_gi_tarball(branch, rtype):
    """GI tarball only applies to PC on ganges-7.3 and ganges-7.5."""
    return branch in PC_TARBALL_BRANCHES and rtype.upper() == "PC"


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

    urls = {
        "changelog": f"{base_dir}/changelog.txt",
        "rpm": f"{base_dir}/rpm.txt",
    }

    if _needs_gi_tarball(branch, release_type):
        urls["gi_tarball"] = f"{base_dir}/pcvm.tar.xz"

    return urls


def validate_url(url):
    """HEAD-check a URL, return True if accessible."""
    req = Request(url, method="HEAD")
    try:
        with urlopen(req, timeout=10) as resp:
            return resp.status == 200
    except Exception:
        print(f"[release-query] URL not accessible: {url}", file=sys.stderr)
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
    
    params = parse.urlencode({"q": f"{query} count:{count}"})
    url = f"{sg_url}/.api/search/stream?{params}"

    req = Request(url)
    req.add_header("Authorization", f"token {sg_token}")
    try:
        with urlopen(req, timeout=30) as resp:
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
        
        _, headers = load_mcp_config("gw-sourcegraph")
        auth = headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            return auth[7:]
    except Exception:
        pass
    return None


def _resolve_fix_version_branches(branch):
    """Resolve latest Gerrit branches from Jira project fix versions.

    For non-master branches (e.g. ``ganges-7.5``), CRs are merged to
    specific Gerrit branches derived from fix versions (e.g.
    ``ganges-7.5.1.8-stable-pc``).  Since Gerrit branches accumulate
    commits from earlier branches, searching the latest one is enough
    to capture all releases.

    Uses the Jira project versions API to discover available fix
    versions, then returns the top candidates (sorted by version
    descending) for both AOS and PC.

    Returns a list of branch strings to search (may be empty).
    """
    if branch == "master":
        return []
    m = re.match(r"ganges-([\d.]+)", branch)
    if not m:
        return []
    branch_ver = m.group(1)

    jira_token = _resolve_jira_token()
    jira_url = _get_env("JIRA_BASE_URL", "https://jira.nutanix.com")
    if not jira_token:
        return []

    url = f"{jira_url}/rest/api/2/project/ENG/versions"
    req = Request(url)
    req.add_header("Authorization", f"Bearer {jira_token}")
    req.add_header("Content-Type", "application/json")

    try:
        with urlopen(req, timeout=15) as resp:
            all_versions = json.loads(resp.read())
    except Exception:
        return []

    def _ver_key(v):
        return tuple(int(p) for p in v.split(".") if p.isdigit())

    # Collect fix versions matching this branch for AOS and PC.
    # AOS versions: "7.5.1.8", "7.5.1.3", etc.
    # PC versions: "pc.7.5.1.8" → "7.5.1.8", etc.
    # Only keep versions with more parts than the base (e.g. 7.5.1.x not 7.5).
    base_depth = len(branch_ver.split("."))
    versions_aos = []
    versions_pc = []
    for v in all_versions:
        name = v.get("name", "")
        if name.startswith(f"pc.{branch_ver}."):
            stripped = name[3:]
            if len(stripped.split(".")) > base_depth:
                versions_pc.append(stripped)
        elif name.startswith(f"{branch_ver}.") and not name.startswith("pc."):
            if len(name.split(".")) > base_depth:
                versions_aos.append(name)

    # Sort descending and take top candidates to search.
    # Not all Jira versions have corresponding Gerrit branches yet,
    # so we try the top 3 — the Sourcegraph search is fast and
    # duplicates are deduped by commit OID.
    MAX_CANDIDATES = 3
    versions_aos.sort(key=_ver_key, reverse=True)
    versions_pc.sort(key=_ver_key, reverse=True)

    branches = []
    for ver in versions_aos[:MAX_CANDIDATES]:
        branches.append(f"ganges-{ver}-stable")
    for ver in versions_pc[:MAX_CANDIDATES]:
        branches.append(f"ganges-{ver}-stable-pc")

    if branches:
        Log.info(f"Resolved fix-version Gerrit branches: {branches}")
    return branches


def fetch_gerrit_releases(server_key, branch, count):
    """Fetch release commits from Gerrit via Sourcegraph streaming API.

    Uses the streaming API directly (not MCP) to avoid the 615-char
    message truncation imposed by the MCP gateway.  Falls back to MCP
    if the streaming API is unavailable.

    For non-master branches (e.g. ``ganges-7.6``), searches:
      1. Fix-version-specific branches (e.g. ``ganges-7.5.1.8-stable-pc``)
         resolved from Jira EPIC fix versions — these carry the latest commits.
      2. ``rev:ganges-7.6-stable`` — the base AOS Gerrit branch
      3. ``rev:ganges-7.6-stable-pc`` — the base PC Gerrit branch
      4. default branch (no rev:) — catches commits not yet cherry-picked
    """
    escaped_repo = re.escape(DEFAULT_REPO)
    repo_filter = f"repo:^{escaped_repo}$"
    if branch == "master":
        query = f'type:commit {repo_filter} message:"Release gold image" message:"main-master"'
        commits = _sg_stream_search(query, count=count * 4)
        if commits:
            return commits
    else:
        all_commits = []
        seen_oids = set()

        # Search fix-version-specific Gerrit branches first (latest
        # branches accumulate all earlier commits, so one search per
        # type is enough).
        fix_branches = _resolve_fix_version_branches(branch)
        for rev in fix_branches:
            query = (
                f'type:commit {repo_filter} rev:{rev} '
                f'message:"Release gold image" '
            )
            for c in _sg_stream_search(query, count=count * 4):
                oid = c.get("commit", "")
                if oid not in seen_oids:
                    seen_oids.add(oid)
                    all_commits.append(c)

        # Also search base branch Gerrit branches
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
    Log.info("Streaming API unavailable, falling back to MCP commit_search")
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
    """Fetch release commits from GitHub (has full SHAs for read_file).

    For non-master branches, uses the Sourcegraph streaming API to avoid
    the MCP 615-char message truncation that limits results. Only searches
    the specific branch (not default) to avoid pulling in unrelated commits.
    """
    escaped_repo = re.escape(GITHUB_REPO)
    repo_filter = f"repo:^{escaped_repo}$"

    if branch == "master":
        query = f'type:commit {repo_filter} message:"Release gold image" message:"main-master"'
        commits = _sg_stream_search(query, count=count * 4)
        if commits:
            return commits
    else:
        query = (
            f'type:commit {repo_filter} rev:{branch} '
            f'message:"Release gold image"'
        )
        commits = _sg_stream_search(query, count=count * 4)
        if commits:
            return commits

    # Fallback to MCP commit_search if streaming API unavailable
    Log.info("GitHub streaming API unavailable, falling back to MCP commit_search")
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
    token = _get_env("JIRA_TOKEN")
    if token:
        return token
    try:
        
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


    params = parse.urlencode({"jql": jql, "fields": "key,summary", "maxResults": 100})
    url = f"{jira_url}/rest/api/2/search?{params}"

    req = Request(url)
    req.add_header("Authorization", f"Bearer {jira_token}")
    req.add_header("Content-Type", "application/json")

    try:
        with urlopen(req, timeout=10) as resp:
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
    """Extract Gerrit CR URL and merged date from Jira git-tracker comments.

    Searches ``===git tracker===`` comments on each ticket for one whose
    ``Branch`` or ``JIRA Version (branch equiv)`` matches *branch*.

    If no git-tracker comment is found on the provided tickets, searches
    the EPIC's child issues (via ``"Epic Link" = <key>`` JQL) for
    git-tracker comments as a fallback.

    The comment's ``created`` timestamp (from the Jira API) is used as the
    authoritative CR **merged date**, since the git-tracker bot posts the
    comment immediately after the CR is merged on Gerrit.

    Args:
        ticket_keys: list of Jira ticket keys (e.g. ["ENG-933882"]).
        branch:      query branch (e.g. "ganges-7.6" or "master").

    Returns:
        dict with ``cr_url`` (str) and ``merged_date`` (ISO str or None).
        Returns ``{"cr_url": "", "merged_date": None}`` if not found.
    """
    empty = {"cr_url": "", "merged_date": None}
    jira_token = _resolve_jira_token()
    jira_url = _get_env("JIRA_BASE_URL", "https://jira.nutanix.com")
    if not jira_token:
        Log.error("Cannot fetch Gerrit CR: no Jira token available")
        return empty

    branch_short = re.sub(r'^ganges-', '', branch)

    candidates = _search_git_tracker_comments(
        ticket_keys, branch_short, branch, jira_url, jira_token)

    if not candidates:
        # Fallback: search child issues of each EPIC for git-tracker comments
        epic_children = _fetch_epic_children(ticket_keys, jira_url, jira_token)
        if epic_children:
            Log.info(f"EPIC has no git-tracker; checking {len(epic_children)} child issue(s)")
            candidates = _search_git_tracker_comments(
                epic_children, branch_short, branch, jira_url, jira_token)

    if not candidates:
        return empty

    best = max(candidates, key=lambda x: x.get("merged_date") or "")
    return best


def _search_git_tracker_comments(ticket_keys, branch_short, branch,
                                 jira_url, jira_token):
    """Search git-tracker comments on a list of Jira tickets.

    Returns list of candidate dicts with ``cr_url`` and ``merged_date``.
    Stops early once a ticket yields matches.
    """
    candidates = []
    for key in ticket_keys:
        try:
            url = f"{jira_url}/rest/api/2/issue/{key}?fields=comment"
            req = Request(url)
            req.add_header("Authorization", f"Bearer {jira_token}")
            req.add_header("Content-Type", "application/json")
            with urlopen(req, timeout=10) as resp:
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
            ver_match = re.search(
                r'JIRA Version \(branch equiv\)\s*:\s*(.+)', body)
            branch_match = re.search(r'Branch\s*:\s*(.+)', body)
            cr_match = re.search(
                r'Code Review URL\s*:\s*(https?://\S+)', body)
            if not cr_match:
                continue

            jira_ver = ver_match.group(1).strip() if ver_match else ""
            gerrit_branch = branch_match.group(1).strip() if branch_match else ""

            is_master = (branch == "master")
            if (branch_short == jira_ver
                    or branch_short == gerrit_branch
                    or branch == gerrit_branch
                    or (is_master and gerrit_branch == "main")
                    or gerrit_branch.startswith(f"ganges-{branch_short}")
                    or gerrit_branch.startswith(branch)):
                comment_created = c.get("created", "")
                candidates.append({
                    "cr_url": cr_match.group(1),
                    "merged_date": comment_created or None,
                })

        if candidates:
            break

    return candidates


def _fetch_epic_children(epic_keys, jira_url, jira_token):
    """Fetch child issue keys of one or more EPIC tickets.

    Tries ``"Epic Link" = <key>`` JQL first, then falls back to
    the issue's ``issuelinks`` and ``subtasks`` fields.

    Returns a list of child ticket keys (strings).
    """

    children = []
    seen = set()

    for key in epic_keys:
        if not re.match(r'^[A-Z]+-\d+$', key):
            continue

        # JQL search for issues with this Epic Link
        jql = f'"Epic Link" = {key}'
        params = parse.urlencode({
            "jql": jql, "fields": "key", "maxResults": 20,
        })
        try:
            url = f"{jira_url}/rest/api/2/search?{params}"
            req = Request(url)
            req.add_header("Authorization", f"Bearer {jira_token}")
            req.add_header("Content-Type", "application/json")
            with urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())
            for issue in data.get("issues", []):
                k = issue.get("key", "")
                if k and k not in seen:
                    seen.add(k)
                    children.append(k)
        except Exception:
            pass

        # Also check issuelinks and subtasks on the EPIC itself
        try:
            url = f"{jira_url}/rest/api/2/issue/{key}?fields=issuelinks,subtasks"
            req = Request(url)
            req.add_header("Authorization", f"Bearer {jira_token}")
            req.add_header("Content-Type", "application/json")
            with urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())
            for link in data.get("fields", {}).get("issuelinks", []):
                for direction in ("inwardIssue", "outwardIssue"):
                    linked = link.get(direction, {})
                    k = linked.get("key", "")
                    if k and k not in seen:
                        seen.add(k)
                        children.append(k)
            for st in data.get("fields", {}).get("subtasks", []):
                k = st.get("key", "")
                if k and k not in seen:
                    seen.add(k)
                    children.append(k)
        except Exception:
            pass

        if children:
            break

    return children


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
        r'Tickets?\s+Resolved\s*:\s*(.+?)(?:\n|$)', github_message, re.IGNORECASE)
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
        f"https://{GITHUB_REPO}/pull/{pr_num_match.group(1)}"
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
        # Boost based on PC prefix matching the requested type.
        # Prefer type-matched commits but don't penalize below zero —
        # combined releases may only have one Gerrit commit for both types.
        has_pc_prefix = bool(re.match(r'^PC\s*:', gc_title, re.IGNORECASE))
        if _is_pc and has_pc_prefix:
            score += 50
        elif not _is_pc and not has_pc_prefix:
            score += 50
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
        tm = re.search(r'Tickets?\s+Resolved\s*:\s*(.+?)(?:\n|$)',
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
            Log.info(f"  Skipping {commit_sha[:8]}: no version confirmed")
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
            urls = build_endor_urls(aos_version, "aos", branch)
            row_data = {
                "goldimage_version": aos_version,
                "type": "AOS",
                "main_ticket": aos_epic,
                "changelog_url": urls["changelog"],
                "rpm_url": urls["rpm"],
                "merge_date": "N/A",
                "github_date": format_merge_date(date),
                "sg_date": "N/A",
                "gerrit_date": None,
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
            urls = build_endor_urls(pc_version, "pc", branch)
            row_data = {
                "goldimage_version": pc_version,
                "type": "PC",
                "main_ticket": pc_epic,
                "changelog_url": urls["changelog"],
                "rpm_url": urls["rpm"],
                "merge_date": "N/A",
                "github_date": format_merge_date(date),
                "sg_date": "N/A",
                "gerrit_date": None,
                "notes": branch,
                "commit": commit_sha,
            }
            if urls.get("gi_tarball"):
                row_data["gi_tarball_url"] = urls["gi_tarball"]
            row_data.update(_pc_extra)
            rows.append(row_data)

    if mismatches:
        Log.info(f"Version mismatches detected: {len(mismatches)} (heading vs variables.sh)")
        for mm in mismatches:
            Log.info(f"  [{mm['type']}] commit {mm['commit'][:7]}: "
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
    _fmt(rows, validate_urls, with_github_date, with_sg_date)


def format_markdown(rows, validate_urls=False, with_github_date=False, with_sg_date=False):
    """Format as a cleaner markdown table with linked URLs."""
    _fmt(rows, validate_urls, with_github_date, with_sg_date)


# ---------------------------------------------------------------------------
# Artifactory RPM download
# ---------------------------------------------------------------------------




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


def _resolve_tarball_url(build_num, art_token=None):
    """Resolve the .tar.xz download URL from the build directory (PC only)."""
    cache_key = build_num
    if cache_key not in _rpm_url_cache:
        _rpm_url_cache[cache_key] = _list_build_dir(build_num, art_token)

    children = _rpm_url_cache[cache_key]
    base = ARTIFACTORY_BASE.format(build_num=build_num)

    for uri in children:
        if uri.endswith(".tar.xz"):
            Log.info(f"[PC] Found tarball in build {build_num}: {uri}")
            return f"{base}{uri}"

    Log.error(f"[PC] No .tar.xz file found in build {build_num}. "
              f"Files in directory: {children}")
    return None


def _list_build_dir(build_num, art_token=None):
    """List file URIs inside a build-artifacts directory. Returns list of URI strings."""
    dir_url = ARTIFACTORY_API_STORAGE.format(build_num=build_num)
    req = Request(dir_url)
    if art_token:
        req.add_header("Authorization", f"Bearer {art_token}")
    try:
        with urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
        return [c.get("uri", "") for c in data.get("children", [])]
    except Exception:
        return []


def _download_one_rpm(url, dest_path, art_token=None):
    """Download a single file. Returns (dest_path, size) or None.

    Uses streaming for large files (.tar.xz) with extended timeout.
    """
    dest_dir = os.path.dirname(dest_path)
    os.makedirs(dest_dir, exist_ok=True)
    req = Request(url)
    if art_token:
        req.add_header("Authorization", f"Bearer {art_token}")

    is_tarball = dest_path.endswith(".tar.xz")
    timeout = 600 if is_tarball else 30

    try:
        with urlopen(req, timeout=timeout) as resp:
            if is_tarball:
                size = 0
                with open(dest_path, "wb") as f:
                    while True:
                        chunk = resp.read(1024 * 1024)
                        if not chunk:
                            break
                        f.write(chunk)
                        size += len(chunk)
            else:
                data = resp.read()
                size = len(data)
                with open(dest_path, "wb") as f:
                    f.write(data)
        return dest_path, size
    except (error.HTTPError, error.URLError, OSError) as e:
        Log.error(f"Download failed for {dest_path}: {e}")
        return None


def download_rpm_artifacts(rows, prev_rows, output_dir="goldimage",
                           filter_type="all", branch=None):
    """Download rpm.txt and old_rpm.txt from Artifactory for AOS and/or PC.

    For each release row, downloads the current release's rpm.txt and the
    previous release's rpm.txt (saved as old_rpm.txt) concurrently::

        <output_dir>/<goldimage_version>/AOS/rpm.txt      (current)
        <output_dir>/<goldimage_version>/AOS/old_rpm.txt  (previous)
        <output_dir>/<goldimage_version>/PC/rpm.txt       (current)
        <output_dir>/<goldimage_version>/PC/old_rpm.txt   (previous)

    For PC on ganges-7.3/ganges-7.5, also downloads the .tar.xz file
    and saves it as pcvm.tar.xz.

    Args:
        rows:        current release rows (with ci_cvm / ci_pcvm populated)
        prev_rows:   dict mapping release type ("AOS"/"PC") to the list of
                     previous-release rows, one per current row, in the same
                     order.  A value of None means no previous release exists.
        output_dir:  base directory for downloads.
        filter_type: "all" downloads both AOS and PC, "aos" only AOS,
                     "pc" only PC.
        branch:      branch name (used to determine GI tarball eligibility).

    Returns:
        list of dicts: rtype, version, file, path.
    """
    

    art_token = (_get_env("ARTIFACTORY_TOKEN")
                 or _get_env("ARTIFACTORY_API_KEY"))

    ci_key_for_type = {"AOS": "ci_cvm", "PC": "ci_pcvm"}
    allowed_types = {"aos": {"AOS"}, "pc": {"PC"}, "all": {"AOS", "PC"}}
    allowed = allowed_types.get(filter_type, {"AOS", "PC"})
    tasks = []

    Log.info("Resolving Artifactory RPM URLs...")

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

            if _needs_gi_tarball(branch, rtype):
                tarball_url = _resolve_tarball_url(build_num, art_token)
                if tarball_url:
                    tarball_dest = os.path.join(
                        output_dir, version, rtype, "pcvm.tar.xz")
                    tasks.append((rtype, version, "pcvm.tar.xz",
                                  tarball_url, tarball_dest))

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
        Log.info("No builds to download")
        return downloaded

    Log.info(f"Downloading {len(tasks)} file(s) in parallel...")

    with ThreadPoolExecutor(max_workers=4) as pool:
        future_map = {}
        for rtype, version, label, url, dest in tasks:
            Log.info(f"[{rtype}] {label} build → {os.path.basename(dest)} "
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
                Log.info(f"[{rtype}] Saved {os.path.basename(dest)} → "
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
        Log.info(f"Fetching Jira summaries for {len(all_ticket_keys)} ticket(s)...")
        ticket_summaries = fetch_ticket_summaries(list(all_ticket_keys))

    # Fetch Gerrit CR URLs + merged dates from Jira git-tracker comments
    Log.info("Fetching Gerrit CR URLs and merged dates from Jira ticket comments...")
    cr_cache = {}  # ticket_keys_tuple -> {"cr_url": ..., "merged_date": ...}

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
            return ver, {"cr_url": "", "merged_date": None}
        cache_key = tuple(sorted(keys))
        if cache_key in cr_cache:
            return ver, cr_cache[cache_key]
        result = fetch_gerrit_cr_from_jira(keys, branch)
        cr_cache[cache_key] = result
        return ver, result

    cr_data = {}
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(_fetch_cr_for_row, r): r for r in rows}
        for fut in as_completed(futures):
            ver, result = fut.result()
            if result.get("cr_url") or result.get("merged_date"):
                cr_data[ver] = result

    generated = []

    ci_key_map = {"AOS": "ci_cvm", "PC": "ci_pcvm"}

    for row in rows:
        version = row.get("goldimage_version", "unknown")
        rtype = row.get("type", "AOS")
        ci_key = ci_key_map.get(rtype, "ci_cvm")

        # Override gerrit_cr_url with the one from Jira comments if found
        if version in cr_data:
            vdata = cr_data[version]
            if vdata.get("cr_url"):
                row["gerrit_cr_url"] = vdata["cr_url"]

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
                    Log.info(f"[{rtype}] changelog.txt: {version} "
                         f"({len(diff_output.splitlines())} diff lines)")
                else:
                    with open(changelog_path, "a") as f:
                        f.write("(no RPM changes)\n")
                    Log.info(f"[{rtype}] changelog.txt: {version} "
                         f"(no RPM changes)")
            except (subprocess.TimeoutExpired, OSError) as e:
                Log.error(f"[{rtype}] diff failed for {version}: {e}")
        else:
            Log.info(f"[{rtype}] changelog.txt: {version} "
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
        SFTP_REMOTE_PATH — absolute remote path that maps to BASE_URL
        (e.g. /mnt/phxitafsprd1/security/security/rahul_kumar).

    Returns:
        list of dicts with keys: rtype, version, file, remote_path.
    """

    host = _get_env("SFTP_HOST")
    username = _get_env("SFTP_USERNAME")
    password = _get_env("SFTP_PASSWORD")
    port = int(_get_env("SFTP_PORT", "22"))
    remote_base = _get_env("SFTP_REMOTE_PATH") or _get_env("SFTP_REMOTE_BASE")

    if not host or not username:
        Log.error("SFTP upload skipped: SFTP_HOST or SFTP_USERNAME not set in .env")
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

            file_pairs = [
                ("changelog.txt", "changelog_url"),
                ("rpm.txt", "rpm_url"),
            ]
            if row.get("gi_tarball_url"):
                file_pairs.append(("pcvm.tar.xz", "gi_tarball_url"))

            for filename, url_key in file_pairs:
                url = row.get(url_key, "")
                if not url or url == "Data not found":
                    continue

                local_path = os.path.join(output_dir, version, rtype, filename)
                if not os.path.isfile(local_path):
                    if filename == "pcvm.tar.xz":
                        Log.error(f"[{rtype}] {filename} not found locally at "
                                  f"{local_path} — Artifactory download may have failed")
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
                Log.info(f"[{rtype}] Uploaded {filename} → sftp://{host}{remote_path}")

    except Exception as e:
        Log.error(f"SFTP upload error: {e}")
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
    """Upload release rows to Confluence using a single parent page.

    Reads ``CONFLUENCE_PAGE_ID`` from ``tools/.env``. Both AOS and PC
    releases are routed under the same parent page where child pages are
    discovered/created per branch.

    Returns:
        list of dicts with keys: release_type, added, skipped, total, page_id.
    """
   
    fallback_id = _get_env("CONFLUENCE_PAGE_ID")
    page_id_map = {"AOS": fallback_id, "PC": fallback_id}

    if not any(page_id_map.values()):
        Log.error("Confluence upload skipped: no page ID set in tools/.env "
                  "(need CONFLUENCE_PAGE_ID)")
        return []

    types_in_rows = set(r.get("type", "AOS").upper() for r in rows)
    if filter_type == "aos":
        types_in_rows &= {"AOS"}
    elif filter_type == "pc":
        types_in_rows &= {"PC"}

    results = []
    for rtype in sorted(types_in_rows):
        parent_id = page_id_map.get(rtype)
        if not parent_id:
            Log.error(f"[{rtype}] Confluence upload skipped: no page ID configured")
            continue
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
            Log.info(f"[{rtype}] Confluence: +{result.get('added', 0)} rows, "
                 f"{result.get('skipped', 0)} skipped, "
                 f"{result.get('total', 0)} total on page {result.get('page_id', '?')}")
        except Exception as e:
            Log.error(f"[{rtype}] Confluence upload error: {e}")
            results.append({"release_type": rtype, "added": 0, "error": str(e)})

    return results


def format_json(rows, output_path=None):
    """Output as JSON — dict keyed by goldimage version."""
    keyed = {row.get("goldimage_version", "unknown"): row for row in rows}
    data = json.dumps(keyed, indent=2)
    if output_path:
        with open(output_path, "w") as f:
            f.write(data)
        Log.info(f"Saved {len(rows)} releases to: {output_path}")
    else:
        print(data)


# ---------------------------------------------------------------------------
# Jira-based Gerrit merge date resolution
# ---------------------------------------------------------------------------

def _resolve_merge_dates_from_jira(rows, branch):
    """Update row merge dates using Jira git-tracker comment timestamps.

    For each row, queries the Jira tickets (main_ticket + tickets_resolved)
    for ``===git tracker===`` comments matching the branch. The comment's
    ``created`` timestamp is the authoritative CR merged date — it is set
    by the git-tracker bot immediately after the CR is merged on Gerrit.

    Updates ``merge_date``, ``sg_date``, and ``gerrit_date`` in place.
    """
    cache = {}

    def _lookup(row):
        ver = row.get("goldimage_version", "")
        keys = []
        main_tkt = row.get("main_ticket", "")
        m = re.search(r'([A-Z]+-\d+)', main_tkt)
        if m:
            keys.append(m.group(1))
        for t in row.get("tickets_resolved", []):
            key = t.split()[0].rstrip(" -:")
            if re.match(r'^[A-Z]+-\d+$', key) and key not in keys:
                keys.append(key)
        if not keys:
            return ver, None
        cache_key = tuple(sorted(keys))
        if cache_key in cache:
            return ver, cache[cache_key]
        result = fetch_gerrit_cr_from_jira(keys, branch)
        cache[cache_key] = result
        return ver, result

    Log.info("Resolving CR merged dates from Jira git-tracker comments...")
    merged_map = {}
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(_lookup, r): r for r in rows}
        for fut in as_completed(futures):
            ver, result = fut.result()
            if result and result.get("merged_date"):
                merged_map[ver] = result

    updated = 0
    for row in rows:
        ver = row.get("goldimage_version", "")
        if ver not in merged_map:
            continue
        jira_merged = merged_map[ver]["merged_date"]
        cr_url = merged_map[ver].get("cr_url", "")
        formatted = format_merge_date(jira_merged)
        if formatted != "N/A":
            row["merge_date"] = formatted
            row["sg_date"] = formatted
            row["gerrit_date"] = jira_merged
            if cr_url:
                row["gerrit_cr_url"] = cr_url
            updated += 1

    Log.info(f"CR merged dates resolved: {updated}/{len(rows)} rows updated from Jira")


# ---------------------------------------------------------------------------
# Confluence Auto-Count Pre-Stage
# ---------------------------------------------------------------------------

def _display_confluence_releases(confluence_data, branch):
    """Display the latest Confluence release per type as a pandas DataFrame.

    Shows only one row per type (the newest entry on each page).
    Columns: Type | Releases | Date | Branch
    """
    if not confluence_data:
        return

    records = []
    for rtype, page_data in confluence_data.items():
        latest = page_data.get("latest", {})
        records.append({
            "Type": rtype,
            "Releases": latest.get("version", "N/A"),
            "Date": latest.get("merge_date", "N/A"),
            "Branch": branch,
        })

    if not records:
        return

    df = pd.DataFrame(records, columns=["Type", "Releases", "Date", "Branch"])
    print(f"\n{'=' * 22} CONFLUENCE LATEST RELEASES {'=' * 22}", file=sys.stderr)
    print(df.to_markdown(index=False, tablefmt="simple"), file=sys.stderr)
    print("-" * 70, file=sys.stderr)




def _compute_count_from_confluence(branch, filter_type, server_key):
    """Pre-stage: look up Confluence, display existing releases, count newer ones.

    1. Look up Confluence page for branch/type, extract all existing rows
    2. Display them as a table (Type | Releases | Date | Branch)
    3. Fetch generous batch of commits (30) from branch
    4. Version match: find position of Confluence latest in commit list
    5. Date confirm: verify counted releases have dates newer than Confluence entry
    6. Return the count, or None if lookup fails
    """

    fallback_id = _get_env("CONFLUENCE_PAGE_ID")

    lookup_types = []
    if filter_type in ("all", "aos"):
        lookup_types.append(("AOS", fallback_id))
    if filter_type in ("all", "pc"):
        lookup_types.append(("PC", fallback_id))

    confluence_latest = {}
    confluence_all = {}
    for rtype, parent_id in lookup_types:
        if not parent_id:
            Log.info(f"  [{rtype}] No Confluence page ID configured, skipping lookup")
            continue
        try:
            page_data = get_confluence_page_releases(
                "atlassian", parent_id, branch, rtype)
            if page_data:
                latest_entry = dict(page_data["latest"])
                # Enrich with full row data from the first (newest) row
                first_row = page_data["rows"][0] if page_data["rows"] else []
                if len(first_row) > 1:
                    latest_entry["ticket"] = first_row[1]
                if len(first_row) > 2:
                    latest_entry["changelog"] = first_row[2]
                if len(first_row) > 3:
                    latest_entry["rpm"] = first_row[3]
                confluence_latest[rtype] = latest_entry
                confluence_all[rtype] = page_data
            else:
                Log.info(f"  [{rtype}] No existing entries on Confluence")
        except Exception as e:
            Log.error(f"  [{rtype}] Confluence lookup error: {e}")

    if not confluence_latest:
        return None, {}, set()

    # Display existing Confluence releases before running the pipeline
    _display_confluence_releases(confluence_all, branch)

    # Fetch a generous batch of release commits to scan
    SCAN_COUNT = 30
    gerrit_commits = fetch_gerrit_releases(server_key, branch, SCAN_COUNT)
    github_commits = fetch_github_releases(server_key, branch, SCAN_COUNT)

    sorted_commits = sorted(
        github_commits, key=lambda x: x.get("date", ""), reverse=True)

    # Extract RHEL versions from recent commits to validate against Confluence
    _rhel_re = re.compile(r"rhel(\d+\.\d+)")
    commit_rhel_versions_by_type = {"AOS": set(), "PC": set()}
    for c in sorted_commits:
        title = c.get("title", "")
        if title.startswith("Revert"):
            continue
        if not re.match(r"^(Release|PC\s*:)", title, re.IGNORECASE):
            continue
        title_clean = re.sub(r"\s*\(#\d+\)$", "", title).strip()
        h_vers = _extract_heading_versions(title_clean)
        for t, ver in h_vers.items():
            if not ver:
                continue
            m = _rhel_re.search(ver)
            if m:
                commit_rhel_versions_by_type[t.upper()].add(m.group(1))

    MAX_AUTO_COUNT = 10

    skipped_types = set()
    counts_per_type = {}
    for rtype, conf_entry in confluence_latest.items():
        conf_version = conf_entry["version"]
        conf_date_str = conf_entry["merge_date"]
        conf_date = _conf_parse_date(conf_date_str)

        # Validate RHEL version match: skip if Confluence tracks a different RHEL
        conf_rhel_match = _rhel_re.search(conf_version)
        if conf_rhel_match:
            conf_rhel = conf_rhel_match.group(1)
            current_rhel_set = commit_rhel_versions_by_type.get(rtype, set())
            if current_rhel_set and conf_rhel not in current_rhel_set:
                Log.info(f"  [{rtype}] Confluence latest uses rhel{conf_rhel} but "
                         f"current commits use rhel{sorted(current_rhel_set)} — "
                         f"skipping (different RHEL version)")
                skipped_types.add(rtype)
                continue

        # Walk commits, find position of the Confluence latest version
        position = None
        for idx, c in enumerate(sorted_commits):
            title = c.get("title", "")
            if title.startswith("Revert"):
                continue
            title_clean = re.sub(r"\s*\(#\d+\)$", "", title).strip()
            if not re.match(r"^(Release|PC\s*:)", title, re.IGNORECASE):
                continue

            heading_versions = _extract_heading_versions(title_clean)
            h_ver = heading_versions.get(rtype.lower())
            if h_ver and h_ver == conf_version:
                position = idx
                break

        if position is not None:
            # position = index of the Confluence latest in the list;
            # everything above it (indices 0..position-1) is newer.
            # This naturally excludes the Confluence latest itself.
            candidate_count = position
            Log.info(f"  [{rtype}] Confluence baseline version '{conf_version}' "
                     f"found in commit history at position {position}; "
                     f"identified {candidate_count} higher/newer release(s) "
                     f"above baseline.")
        else:
            # Fallback: version not found in commits, use date-only counting.
            # Only count commits strictly newer than Confluence date (excludes it).
            Log.info(f"  [{rtype}] Confluence baseline version '{conf_version}' "
                     "not found in current commits; "
                     "counting higher/newer releases using Confluence merge-date "
                     "comparison.")
            if conf_date == datetime.min:
                Log.info(f"  [{rtype}] No valid Confluence date, cannot determine count")
                continue
            candidate_count = 0
            for c in sorted_commits:
                title = c.get("title", "")
                if title.startswith("Revert"):
                    continue
                if not re.match(r"^(Release|PC\s*:)", title, re.IGNORECASE):
                    continue
                # Extract version to explicitly skip the Confluence latest
                title_clean = re.sub(r"\s*\(#\d+\)$", "", title).strip()
                h_vers = _extract_heading_versions(title_clean)
                h_ver = h_vers.get(rtype.lower())
                if h_ver and h_ver == conf_version:
                    break
                commit_date_str = c.get("date", "")
                if commit_date_str:
                    commit_dt = datetime.fromisoformat(
                        commit_date_str.replace("Z", "+00:00")).replace(tzinfo=None)
                    if commit_dt > conf_date:
                        candidate_count += 1
                    else:
                        break
            Log.info(f"  [{rtype}] Higher/newer releases counted from Confluence "
                     f"date baseline: {candidate_count}.")

        # Date confirmation: verify counted commits are newer than Confluence date
        if position is not None and conf_date != datetime.min:
            confirmed = 0
            for c in sorted_commits[:position]:
                title = c.get("title", "")
                if title.startswith("Revert"):
                    continue
                if not re.match(r"^(Release|PC\s*:)", title, re.IGNORECASE):
                    continue
                commit_date_str = c.get("date", "")
                if commit_date_str:
                    try:
                        commit_dt = datetime.fromisoformat(
                            commit_date_str.replace("Z", "+00:00")
                        ).replace(tzinfo=None)
                        if commit_dt > conf_date:
                            confirmed += 1
                    except (ValueError, AttributeError):
                        confirmed += 1
                else:
                    confirmed += 1
            if confirmed < candidate_count:
                Log.info(f"  [{rtype}] Date confirmation trimmed count: "
                         f"{candidate_count} -> {confirmed}")
                candidate_count = confirmed

        counts_per_type[rtype] = min(candidate_count, MAX_AUTO_COUNT)

    if not counts_per_type:
        return None, {}, skipped_types

    final_count = max(counts_per_type.values())
    Log.info(f"Auto-count result: {final_count} (per-type: {counts_per_type})")
    # Remove skipped types from confluence_latest so they don't appear in display
    for st in skipped_types:
        confluence_latest.pop(st, None)
    return final_count, confluence_latest, skipped_types


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
    parser.add_argument("--count", type=int, default=None,
                        help="Number of releases. When omitted, auto-determines by "
                             "looking up Confluence for the latest entry and counting "
                             "only newer releases since then.")
    parser.add_argument("--since-confluence", action="store_true",
                        help="Explicitly trigger Confluence-based auto-count even when "
                             "--count is provided (implied when --count is omitted).")
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
    parser.add_argument("--generate-changelog", action="store_true", default=True,
                        help="Generate changelog.txt from template (default: on)")
    parser.add_argument("--upload-sftp", action="store_true", default=True,
                        help=argparse.SUPPRESS)
    parser.add_argument("--upload-confluence", action="store_true", default=True,
                        help=argparse.SUPPRESS)
    parser.add_argument("--publish-endor", action="store_true", default=True,
                        help=argparse.SUPPRESS)
    parser.add_argument("--no-upload", action="store_true",
                        help="Skip SFTP upload, Jenkins endor publish, and Confluence upload")
    parser.add_argument("--force-publish-endor", action="store_true",
                        help="Force republish to endor even if already present")
    _default_rpm_dir = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "releases"
    )
    parser.add_argument("--rpm-dir", default=_default_rpm_dir,
                        help=f"Directory for downloaded rpm.txt files (default: {_default_rpm_dir})")
    parser.add_argument("--server", default=DEFAULT_SERVER_KEY,
                        help="MCP server key from mcp.json")

    args = parser.parse_args()

    server_key = args.server

    # --no-* flags override defaults (process early so validation knows what's needed)
    if args.no_upload:
        args.upload_sftp = False
        args.publish_endor = False
        args.upload_confluence = False
    if args.no_ci_status:
        args.ci_status = False

    # Validate MCP server tokens before starting the pipeline.
    # Skip when launched as a subprocess of agent_runner.py (already validated).
    if not os.environ.get("_RELEASE_AGENT_SUBPROCESS"):
        required = [server_key, "atlassian"]
        if args.ci_status:
            required.append("github")
        if args.publish_endor:
            required.append("jenkins")
        validate_mcp_tokens(required_servers=required)

    # -----------------------------------------------------------------------
    # Pre-stage: Confluence Auto-Count
    # When --count is omitted (or --since-confluence is set), look up the
    # latest release on Confluence and count how many newer releases exist.
    # -----------------------------------------------------------------------
    DEFAULT_COUNT = 5
    _confluence_versions = {}  # type -> {"version": ..., "merge_date": ...}
    _skipped_types = set()  # types with RHEL mismatch (no valid Confluence baseline)
    if args.count is None or args.since_confluence:
        Log.info("Auto-count mode: looking up Confluence for latest entry...")
        computed, _confluence_versions, _skipped_types = _compute_count_from_confluence(
            args.branch, args.filter, server_key)
        if computed is not None and computed >= 0:
            computed_new = computed if computed > 0 else 0
            Log.info(f"Auto-count resolved: {computed_new} new release(s) "
                     f"since last Confluence update")
            if args.count is not None:
                # Explicit user count should be honored; Confluence lookup is used
                # as baseline context, not a hard cap.
                effective_count = args.count
                Log.info("Confluence baseline checked: "
                         f"{computed_new} new release(s) found; honoring requested count={args.count}")
            else:
                effective_count = computed_new
        else:
            if args.count is not None:
                effective_count = args.count
                Log.info("Confluence lookup failed or empty — "
                         f"honoring requested count: {effective_count}")
            else:
                effective_count = DEFAULT_COUNT
                Log.info(f"Confluence lookup failed or empty — "
                         f"falling back to default count: {effective_count}")
    else:
        effective_count = args.count

    if effective_count == 0:
        Log.info("No new releases found since last Confluence update — "
                 "skipping pipeline.")
        sys.stderr.flush()
        print(f"\nNo new release found. Latest from Confluence for "
              f"branch '{args.branch}':")
        
        records = []
        for rtype, entry in _confluence_versions.items():
            records.append({
                "GoldImage Version": entry["version"],
                "Main Ticket": entry.get("ticket", "--"),
                "Change Log": entry.get("changelog", "--"),
                "RPM List": entry.get("rpm", "--"),
                "Merge Date": entry["merge_date"],
                "Notes": args.branch,
            })
        if records:
            df = pd.DataFrame(records, columns=TABLE_COLUMNS)
            maxcol = _compute_maxcolwidths(df.columns.tolist())
            print("\n" + df.to_markdown(index=False, maxcolwidths=maxcol))
            print(f"\nTotal: {len(records)} release(s) (from Confluence)")
        _pipeline_stats["rows"] = 0
        _print_pipeline_status()
        return

    # Fetch data
    Log.info(f"Fetching releases: branch={args.branch}, count={effective_count}, "
             f"filter={args.filter}")

    # Fetch extra history so the previous release is available for diffs and,
    # when filter=all, per-type targets (N AOS + N PC) can be satisfied.
    fetch_count = effective_count + 1
    if args.filter == "all":
        fetch_count = max(fetch_count, effective_count * 3)

    gerrit_commits = fetch_gerrit_releases(server_key, args.branch, fetch_count)
    Log.info(f"Gerrit: {len(gerrit_commits)} commits")
    _pipeline_stats["gerrit_commits"] = len(gerrit_commits)

    github_commits = fetch_github_releases(server_key, args.branch, fetch_count)
    Log.info(f"GitHub: {len(github_commits)} commits")
    _pipeline_stats["github_commits"] = len(github_commits)

    github_epics = fetch_github_epics(server_key, args.branch)
    Log.info(f"GitHub EPICs: {len(github_epics)} releases with Epic field")

    Log.info("Extracting versions from commit headings + validating against variables.sh...")
    rows, mismatches = parse_releases(
        server_key, github_commits, gerrit_commits, github_epics,
        args.branch, args.filter,
    )

    # Resolve authoritative CR merged dates from Jira git-tracker comments.
    # The git-tracker bot posts a comment immediately when the CR is merged
    # on Gerrit; the comment's 'created' timestamp is the actual merge date.
    _resolve_merge_dates_from_jira(rows, args.branch)

    # -----------------------------------------------------------------------
    # Filter out Confluence latest and older releases (auto-count mode only).
    # Only keep rows strictly newer than what's already on Confluence.
    # Also exclude types with no valid Confluence baseline (RHEL mismatch).
    # -----------------------------------------------------------------------
    # Apply newer-than-Confluence filtering only for true auto-delta mode
    # (when user did not provide an explicit count).
    apply_confluence_delta_filter = args.count is None
    if (_confluence_versions or _skipped_types) and apply_confluence_delta_filter:
        from tools.mcp_confluence_client import parse_date as _conf_parse_date

        conf_versions_set = {v["version"].lower() for v in _confluence_versions.values()}
        conf_dates = {}
        for rtype, entry in _confluence_versions.items():
            conf_dates[rtype] = _conf_parse_date(entry["merge_date"])

        filtered = []
        for row in rows:
            ver = row.get("goldimage_version", "").lower()
            rtype = row.get("type", "AOS").upper()
            # Exclude types with no valid Confluence baseline (RHEL mismatch)
            if rtype in _skipped_types:
                Log.info(f"  Excluding '{row.get('goldimage_version')}' "
                         f"({rtype} Confluence page tracks different RHEL version)")
                continue
            # Exclude if version matches the Confluence latest
            if ver in conf_versions_set:
                Log.info(f"  Excluding '{row.get('goldimage_version')}' "
                         f"(already on Confluence)")
                continue
            # Exclude if merge date is older than or equal to Confluence latest
            conf_dt = conf_dates.get(rtype)
            if conf_dt and conf_dt != datetime.min:
                row_date_str = row.get("merge_date", "N/A")
                row_dt = _conf_parse_date(row_date_str)
                if row_dt != datetime.min and row_dt <= conf_dt:
                    Log.info(f"  Excluding '{row.get('goldimage_version')}' "
                             f"(date {row_date_str} <= Confluence latest)")
                    continue
            filtered.append(row)

        if len(filtered) < len(rows):
            Log.info(f"Filtered: {len(rows)} -> {len(filtered)} rows "
                     f"(removed Confluence latest and older)")
        rows = filtered

        # If all rows were filtered out, no new releases — show Confluence latest
        if not rows:
            Log.info("No new releases after filtering — showing Confluence latest.")
            sys.stderr.flush()
            _pipeline_stats["rows"] = 0
            _print_pipeline_status()
            print(f"\nNo new release found. Latest from Confluence for "
                  f"branch '{args.branch}':")
            
            records = []
            for rtype, entry in _confluence_versions.items():
                records.append({
                    "GoldImage Version": entry["version"],
                    "Main Ticket": entry.get("ticket", "--"),
                    "Change Log": entry.get("changelog", "--"),
                    "RPM List": entry.get("rpm", "--"),
                    "Merge Date": entry["merge_date"],
                    "Notes": args.branch,
                })
            if records:
                df = pd.DataFrame(records, columns=TABLE_COLUMNS)
                maxcol = _compute_maxcolwidths(df.columns.tolist())
                print("\n" + df.to_markdown(index=False, maxcolwidths=maxcol))
                print(f"\nTotal: {len(records)} release(s) (from Confluence)")
            return

    display_count = effective_count
    # Always keep all_rows so we can find the previous release per type
    all_rows = rows
    if args.filter == "all":
        # Per-type slicing: return up to `count` rows for each type.
        aos_rows = [r for r in rows if r.get("type", "AOS").upper() == "AOS"][:display_count]
        pc_rows = [r for r in rows if r.get("type", "AOS").upper() == "PC"][:display_count]
        rows = aos_rows + pc_rows
        rows.sort(key=lambda x: x.get("gerrit_date") or x.get("date", ""), reverse=True)
    else:
        rows = rows[:display_count]
    Log.info(f"Output: {len(rows)} rows")
    _pipeline_stats["rows"] = len(rows)

    # Add branch as explicit field in every row
    for row in all_rows:
        row["branch"] = args.branch

    # Fetch postmerge CI status for all rows (including extras for prev release)
    if args.ci_status:
        Log.info("Fetching postmerge CircleCI status from GitHub...")
        ci_rows = all_rows
        seen_shas = set()
        for row in ci_rows:
            sha = row.get("commit", "")
            if not sha:
                row["ci_cvm"] = {}
                row["ci_pcvm"] = {}
                continue
            if sha in seen_shas:
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
        Log.info(f"CI status fetched for {len(seen_shas)} unique commit(s)")
        _pipeline_stats["ci_commits"] = len(seen_shas)
        ci_states = []
        for row in all_rows:
            for k in ("ci_cvm", "ci_pcvm"):
                st = row.get(k, {}).get("state")
                if st:
                    ci_states.append(st)
        _pipeline_stats["ci_success"] = ci_states.count("success")
        _pipeline_stats["ci_failure"] = ci_states.count("failure")
        _pipeline_stats["ci_pending"] = ci_states.count("pending")
    else:
        _pipeline_stats["ci_skipped"] = True

    # Fetch Jira ticket summaries for all rows (ticket key → description)
    all_ticket_keys = set()
    for row in all_rows:
        for t in row.get("tickets_resolved", []):
            key = t.split()[0].rstrip(" -:")
            if re.match(r'^[A-Z]+-\d+$', key):
                all_ticket_keys.add(key)
        m = re.search(r'([A-Z]+-\d+)', row.get("main_ticket", ""))
        if m:
            all_ticket_keys.add(m.group(1))
    ticket_summaries = {}
    if all_ticket_keys:
        Log.info(f"Fetching Jira summaries for {len(all_ticket_keys)} ticket(s)...")
        ticket_summaries = fetch_ticket_summaries(list(all_ticket_keys))

    # Embed ticket summaries into each row
    for row in all_rows:
        resolved_with_summary = []
        for t in row.get("tickets_resolved", []):
            key = t.split()[0].rstrip(" -:")
            summary = ticket_summaries.get(key, "")
            resolved_with_summary.append({
                "key": key,
                "summary": summary,
            })
        row["tickets_resolved_details"] = resolved_with_summary
        m = re.search(r'([A-Z]+-\d+)', row.get("main_ticket", ""))
        if m:
            row["main_ticket_summary"] = ticket_summaries.get(m.group(1), "")

    # Build prev_rows: maps type -> {version: prev_row}
    ci_key_map = {"AOS": "ci_cvm", "PC": "ci_pcvm"}
    prev_rows = {"AOS": {}, "PC": {}}
    for rtype, ci_key in ci_key_map.items():
        typed = [r for r in all_rows if r.get("type") == rtype]
        for i in range(len(typed) - 1):
            cur_ver = typed[i].get("goldimage_version", "")
            prev_rows[rtype][cur_ver] = typed[i + 1]

    # Resolve Jira merged dates for previous-release rows too
    prev_row_list = []
    for rtype_map in prev_rows.values():
        for pr in rtype_map.values():
            if pr not in prev_row_list:
                prev_row_list.append(pr)
    if prev_row_list:
        _resolve_merge_dates_from_jira(prev_row_list, args.branch)

    # Attach previous_release section with only PRs and CI builds
    _prev_keep_keys = {
        "goldimage_version", "type", "main_ticket",
        "associated_prs", "release_pr_link",
        "ci_cvm", "ci_pcvm",
    }
    for row in rows:
        rtype = row.get("type", "AOS")
        ver = row.get("goldimage_version", "")
        prev = prev_rows.get(rtype, {}).get(ver)
        if prev:
            row["previous_release"] = {
                k: v for k, v in prev.items()
                if k in _prev_keep_keys
            }
        else:
            row["previous_release"] = None

    # Download rpm.txt + old_rpm.txt from Artifactory if requested
    if args.download_rpm:
        downloaded = download_rpm_artifacts(rows, prev_rows, args.rpm_dir,
                                           args.filter, branch=args.branch)
        for d in downloaded:
            Log.info(f"[{d['rtype']}] {d['file']} → {d['path']}")
        _pipeline_stats["rpm_downloaded"] = len(downloaded)

    # Generate changelog.txt if requested (after RPMs are on disk)
    if args.generate_changelog:
        Log.info("Generating changelog.txt from template...")
        changelogs = generate_changelog(rows, prev_rows, args.rpm_dir,
                                        args.filter, args.branch)
        for cl in changelogs:
            Log.info(f"[{cl['rtype']}] changelog → {cl['path']}")
        _pipeline_stats["changelogs"] = len(changelogs)

    # Upload changelog + rpm to SFTP server
    if args.upload_sftp:
        Log.info("Uploading files to SFTP server...")
        sftp_results = upload_to_sftp(rows, args.rpm_dir, args.filter)
        Log.info(f"SFTP upload complete: {len(sftp_results)} file(s) uploaded")
        _pipeline_stats["sftp_uploaded"] = len(sftp_results)

    # Publish to endor via Jenkins PUBLISH_GOLD_IMAGE
    if args.publish_endor:
        Log.info("Publishing to endor via Jenkins...")
        endor_results = publish_to_endor(
            rows, args.filter,
            dry_run=False, force=args.force_publish_endor,
        )
        published = [r for r in endor_results if r.get("success")]
        skipped = [r for r in endor_results if r.get("skipped")]
        failed = [r for r in endor_results
                  if not r.get("success") and not r.get("skipped") and not r.get("dry_run")]
        Log.info(f"Endor publish complete: {len(published)} published, "
             f"{len(skipped)} already exist, {len(failed)} failed")
        _pipeline_stats["endor_published"] = len(published)
        _pipeline_stats["endor_skipped"] = len(skipped)
        _pipeline_stats["endor_failed"] = len(failed)

        if published or skipped:
            rewrite_urls_to_endor(rows, args.branch)
    else:
        _pipeline_stats["endor_skipped_flag"] = True

    # Upload release table to Confluence
    if args.upload_confluence:
        Log.info("Uploading release table to Confluence...")
        confluence_results = upload_to_confluence(rows, args.branch, args.filter)
        total_added = sum(r.get("added", 0) for r in confluence_results)
        total_skipped = sum(r.get("skipped", 0) for r in confluence_results)
        Log.info(f"Confluence upload complete: {total_added} added, {total_skipped} already exist")
        _pipeline_stats["confluence_added"] = total_added
        _pipeline_stats["confluence_skipped"] = total_skipped
    else:
        _pipeline_stats["confluence_skipped_flag"] = True

    # Always save JSON to releases/release_data.json (after ALL enrichment)
    default_json_path = os.path.join(args.rpm_dir, "release_data.json")
    os.makedirs(os.path.dirname(default_json_path), exist_ok=True)
    format_json(rows, default_json_path)

    # Output in requested format
    gh_date = args.with_github_date
    sg_date = args.with_sg_date
    if args.format == "json":
        if args.output and args.output != default_json_path:
            format_json(rows, args.output)
    elif args.format == "markdown":
        format_markdown(rows, args.validate_urls, with_github_date=gh_date, with_sg_date=sg_date)
    else:
        format_table(rows, args.validate_urls, with_github_date=gh_date, with_sg_date=sg_date)

    # Print pipeline status summary
    _print_pipeline_status()

    # Print version validation summary if any mismatches (filtered by requested type)
    filtered_mismatches = mismatches
    if args.filter in ("aos", "pc"):
        filtered_mismatches = [
            mm for mm in mismatches if mm.get("type", "").lower() == args.filter
        ]
    if filtered_mismatches:
        records = []
        for mm in filtered_mismatches:
            source = mm.get("source", "unknown")
            if source == "heading+jira":
                verdict = "Heading + Jira agree → variables.sh needs fix"
            elif source == "file+jira":
                verdict = "Jira + file agree → heading overridden"
            elif source == "heading_only":
                verdict = "No Jira confirmation → verify manually"
            else:
                verdict = "All differ → verify manually"
            records.append({
                "Release": mm.get("confirmed_version", "--"),
                "EPIC": mm.get("epic_key") or "--",
                "Heading Version": mm.get("heading_version") or "--",
                "Actual in variables.sh": mm.get("file_version") or "--",
                "Verdict": verdict,
            })
        df = pd.DataFrame(records)
        tw = shutil.get_terminal_size((120, 24)).columns
        fixed = 14 + 12
        flexible_cols = 3
        sep_overhead = 6 * 3
        flex_width = max(20, (tw - fixed - sep_overhead) // flexible_cols)
        maxcol = [flex_width, 12, flex_width, flex_width, flex_width]
        print(f"\n**Version Mismatch Summary** ({len(filtered_mismatches)} release(s) affected)\n")
        print(df.to_markdown(index=False, maxcolwidths=maxcol))
        print()


if __name__ == "__main__":
    main()
