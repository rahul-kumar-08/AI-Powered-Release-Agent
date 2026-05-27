#!/usr/bin/env python3
"""
Sourcegraph Tool — Validates release CR merge status via Sourcegraph
commit search against the Gerrit repo (nugerrit.ntnxdpro.com/main).

The Gerrit repo is the source of truth for CR merges. CRs are auto-submitted
by svc.jenkins.autosub on Gerrit before syncing to GitHub. This tool queries
Sourcegraph's index of the Gerrit repo to confirm merge status and dates.

Splits combined AOS/PC release titles, queries Sourcegraph for merge
commits, detects reverts, and reports accurate merge status + dates for
each component independently.

All public functions raise typed exceptions from tools.exceptions so the
orchestrator can retry transient failures.

Usage:
  python3 tools/sourcegraph_tool.py --input-json /tmp/releases_master_5.json
  python3 tools/sourcegraph_tool.py --input-json /tmp/releases.json --format table
  python3 tools/sourcegraph_tool.py --pr-titles "Release gold image main-master-rhel9.7-9.2.0/PC:..."
  python3 tools/sourcegraph_tool.py --input-json /tmp/releases.json --output-json /tmp/validated.json

Environment variables (from tools/.env):
  SOURCEGRAPH_TOKEN  — Sourcegraph access token
  SOURCEGRAPH_URL    — Sourcegraph base URL (default: https://sourcegraph.ntnxdpro.com)
"""

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

try:
    from tools.exceptions import (
        ToolError, AuthError, ConfigError, HttpError, NetworkError, DataError,
        RateLimitError,
    )
except ModuleNotFoundError:
    from exceptions import (
        ToolError, AuthError, ConfigError, HttpError, NetworkError, DataError,
        RateLimitError,
    )

DEFAULT_REPO = "nugerrit.ntnxdpro.com/main"
DEFAULT_SG_URL = "https://sourcegraph.ntnxdpro.com"
REVERT_RE = re.compile(r"^Revert\b", re.IGNORECASE)
PC_SPLIT_RE = re.compile(r"/PC:\s*", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------

def load_env(env_path="tools/.env"):
    if not os.path.isfile(env_path):
        return
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            val = val.strip().strip("\"'")
            os.environ.setdefault(key.strip(), val)


def _require_env(name):
    val = os.environ.get(name, "").strip()
    if not val:
        raise ConfigError(f"{name} is not set. Add it to tools/.env or export it.")
    return val


# ---------------------------------------------------------------------------
# Title parsing
# ---------------------------------------------------------------------------

def split_aos_pc(title):
    """
    Split a combined release PR title into AOS and PC headings.

    "Release gold image main-master-rhel9.7-9.2.0/PC:Release gold image main-master-rhel9.7-7.1.0"
    -> ("Release gold image main-master-rhel9.7-9.2.0",
        "Release gold image main-master-rhel9.7-7.1.0")

    Returns (aos_heading, pc_heading_or_None).
    """
    parts = PC_SPLIT_RE.split(title, maxsplit=1)
    aos = parts[0].strip()
    pc = parts[1].strip() if len(parts) > 1 else None
    return aos, pc


def extract_version_from_heading(heading):
    """Extract the version identifier from a heading string."""
    m = re.search(
        r"[Rr]elease\s+[Gg]old\s+[Ii]mage\s+([\w.\-]+)",
        heading,
    )
    return m.group(1) if m else heading


# ---------------------------------------------------------------------------
# Sourcegraph HTTP client
# ---------------------------------------------------------------------------

def sourcegraph_commit_search(token, base_url, repo, message_term, count=10):
    """
    Search Sourcegraph for commits whose message contains message_term.

    Uses the stream API (SSE). Returns a list of commit dicts with keys:
    oid, message, authorDate, committerDate, authorName.
    """
    query = f'type:commit repo:^{repo}$ message:"{message_term}" count:{count}'
    params = urllib.parse.urlencode({"q": query})
    url = f"{base_url.rstrip('/')}/.api/search/stream?{params}"

    headers = {
        "Authorization": f"token {token}",
        "Accept": "text/event-stream",
    }
    req = urllib.request.Request(url, headers=headers)

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
        except Exception:
            pass
        msg = f"Sourcegraph HTTP {exc.code}: {detail}"
        if exc.code in (401, 403):
            raise AuthError(msg, status_code=exc.code)
        if exc.code == 429:
            raise RateLimitError(msg)
        raise HttpError(msg, status_code=exc.code)
    except urllib.error.URLError as exc:
        raise NetworkError(f"Sourcegraph network error: {exc}")

    return _parse_sse_commits(body)


def _parse_sse_commits(body):
    """Parse Server-Sent Events body and extract commit match objects."""
    commits = []
    for line in body.split("\n"):
        line = line.strip()
        if not line.startswith("data: "):
            continue
        payload = line[6:]
        try:
            items = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if not isinstance(items, list):
            continue
        for item in items:
            if item.get("type") != "commit":
                continue
            commits.append({
                "oid": item.get("oid", ""),
                "message": (item.get("message") or "").split("\n", 1)[0],
                "author_date": item.get("authorDate", ""),
                "committer_date": item.get("committerDate", ""),
                "author": item.get("authorName", ""),
            })
    return commits


# ---------------------------------------------------------------------------
# Validation logic
# ---------------------------------------------------------------------------

def is_revert_commit(commit):
    """True if the commit message is a Revert of a release."""
    return bool(REVERT_RE.match(commit.get("message", "")))


def _check_heading_merged(token, base_url, repo, heading):
    """
    Query Sourcegraph for a heading and determine merge status.

    Returns (merged: bool, merge_date: str|None, commit_sha: str|None).

    Logic: find all commits matching the heading, sort newest-first.
    If the newest non-revert commit exists and is not followed by a
    newer revert of the same commit, it counts as merged.
    """
    if not heading:
        return False, None, None

    commits = sourcegraph_commit_search(token, base_url, repo, heading, count=10)
    if not commits:
        return False, None, None

    commits.sort(key=lambda c: c["committer_date"], reverse=True)

    newest = commits[0]
    if is_revert_commit(newest):
        return False, None, None

    return True, newest["committer_date"], newest["oid"]


def validate_release(token, base_url, repo, title, pr_number=None):
    """
    Validate one release PR title against Sourcegraph.

    Returns a dict with AOS and PC validation results.
    """
    aos_heading, pc_heading = split_aos_pc(title)

    aos_merged, aos_date, aos_sha = _check_heading_merged(
        token, base_url, repo, aos_heading,
    )
    pc_merged, pc_date, pc_sha = _check_heading_merged(
        token, base_url, repo, pc_heading,
    )

    return {
        "pr_number": pr_number,
        "full_title": title,
        "aos_heading": aos_heading,
        "aos_version": extract_version_from_heading(aos_heading),
        "aos_merged": aos_merged,
        "aos_merge_date": aos_date or "N/A",
        "aos_commit_sha": aos_sha,
        "pc_heading": pc_heading or "N/A",
        "pc_version": extract_version_from_heading(pc_heading) if pc_heading else "N/A",
        "pc_merged": pc_merged,
        "pc_merge_date": pc_date or "N/A",
        "pc_commit_sha": pc_sha,
    }


# ---------------------------------------------------------------------------
# Input parsing
# ---------------------------------------------------------------------------

def load_releases_from_json(input_json_path):
    """
    Load release data produced by github_tool.py.

    The JSON is a dict keyed by release_key, each with a "release_title",
    "release_pr", and "merged_at".
    """
    with open(input_json_path) as f:
        data = json.load(f)

    releases = []
    if isinstance(data, dict):
        for key, obj in data.items():
            title = obj.get("release_title", "")
            if not title:
                prs = obj.get("prs", [])
                if prs:
                    title = prs[0].get("title", "")
            if not title or not re.match(r"^Release", title, re.IGNORECASE):
                continue
            if REVERT_RE.match(title):
                continue
            releases.append({
                "title": title,
                "pr_number": obj.get("release_pr") or obj.get("number"),
            })
    elif isinstance(data, list):
        for obj in data:
            title = obj.get("release_title") or obj.get("title", "")
            if not title or not re.match(r"^Release", title, re.IGNORECASE):
                continue
            if REVERT_RE.match(title):
                continue
            releases.append({
                "title": title,
                "pr_number": obj.get("release_pr") or obj.get("number"),
            })
    return releases


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def print_table(results):
    """Print validation results as a table."""
    fmt = "| %-6s | %-55s | %-8s | %-22s | %-55s | %-8s | %-22s |"
    hdr = fmt % ("PR#", "AOS Heading", "Merged", "AOS Merge Date",
                 "PC Heading", "Merged", "PC Merge Date")
    sep = "|" + "-" * (len(hdr) - 2) + "|"
    print(hdr)
    print(sep)
    for r in results:
        pr = str(r.get("pr_number") or "N/A")
        aos_h = r["aos_heading"][:55]
        pc_h = str(r["pc_heading"])[:55]
        print(fmt % (
            pr,
            aos_h,
            "YES" if r["aos_merged"] else "NO",
            r["aos_merge_date"],
            pc_h,
            "YES" if r["pc_merged"] else "NO",
            r["pc_merge_date"],
        ))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    load_env()

    parser = argparse.ArgumentParser(
        description="Sourcegraph Tool — Validate release PR merge status"
    )
    input_group = parser.add_mutually_exclusive_group(required=True)
    input_group.add_argument(
        "--input-json",
        help="Path to release extractor JSON (from github_tool.py)",
    )
    input_group.add_argument(
        "--pr-titles",
        help="Comma-separated PR titles to validate directly",
    )
    parser.add_argument("--output-json", help="Save validated output to JSON")
    parser.add_argument(
        "--format", choices=["table", "json"], default="table",
        help="Output format (default: table)",
    )
    parser.add_argument(
        "--repo", default=DEFAULT_REPO,
        help=f"Sourcegraph repo path (default: {DEFAULT_REPO})",
    )
    args = parser.parse_args()

    token = _require_env("SOURCEGRAPH_TOKEN")
    base_url = os.environ.get("SOURCEGRAPH_URL", DEFAULT_SG_URL).strip()

    if args.input_json:
        releases = load_releases_from_json(args.input_json)
    else:
        titles = [t.strip() for t in args.pr_titles.split(";;") if t.strip()]
        releases = [{"title": t, "pr_number": None} for t in titles]

    if not releases:
        raise DataError("No release PR titles to validate.")

    print(f"Validating {len(releases)} release(s) against Sourcegraph...",
          file=sys.stderr, flush=True)

    results = []
    for rel in releases:
        title = rel["title"]
        pr_num = rel.get("pr_number")
        print(f"  Checking: {title[:80]}...", file=sys.stderr, flush=True)
        result = validate_release(token, base_url, args.repo, title, pr_number=pr_num)
        results.append(result)
        status_aos = "merged" if result["aos_merged"] else "NOT merged"
        status_pc = "merged" if result["pc_merged"] else "NOT merged"
        print(f"    AOS: {status_aos}  |  PC: {status_pc}", file=sys.stderr)

    if args.output_json:
        with open(args.output_json, "w") as f:
            json.dump(results, f, indent=2)
        print(f"Saved validated JSON to: {args.output_json}", file=sys.stderr)

    if args.format == "json":
        print(json.dumps(results, indent=2))
    else:
        print_table(results)

    merged_aos = sum(1 for r in results if r["aos_merged"])
    merged_pc = sum(1 for r in results if r["pc_merged"])
    print(f"\nSummary: {merged_aos}/{len(results)} AOS merged, "
          f"{merged_pc}/{len(results)} PC merged", file=sys.stderr)


if __name__ == "__main__":
    try:
        main()
    except ToolError as exc:
        print(f"Error [{type(exc).__name__}]: {exc}", file=sys.stderr)
        sys.exit(2 if isinstance(exc, ConfigError) else 1)
