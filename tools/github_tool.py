#!/usr/bin/env python3
"""
GitHub Tool — Extracts release PRs, associated PRs, and CI status from
GitHub using the REST API for the Release Agent.

Usage:
    python3 tools/github_tool.py --repo nutanix-core/aos-goldimage-os --branch master --count 1
    python3 tools/github_tool.py --branch master --count 3 --output-json /tmp/releases.json
    python3 tools/github_tool.py --branch master --count 2 --ci-status
    python3 tools/github_tool.py --commit-status <SHA>
    python3 tools/github_tool.py --branch master --count 1 --mode both
    python3 tools/github_tool.py --branch master --count 2 --pr-title-regex "^Release"

Requires: GITHUB_TOKEN environment variable (or in tools/.env)
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta

try:
    from tools.exceptions import (
        ToolError, AuthError, ConfigError, RateLimitError, HttpError,
        NetworkError, NotFoundError, DataError,
    )
except ModuleNotFoundError:
    from exceptions import (
        ToolError, AuthError, ConfigError, RateLimitError, HttpError,
        NetworkError, NotFoundError, DataError,
    )

DEFAULT_OWNER = "nutanix-core"
DEFAULT_REPO = "aos-goldimage-os"
DEFAULT_RELEASE_REGEX = r"^Release"
REVERT_TITLE_RE = re.compile(r"^Revert\b", re.IGNORECASE)
TICKET_PATTERN = re.compile(r"\b[A-Z][A-Z0-9]+-\d+\b")
CIRCLECI_URL_RE = re.compile(r"https?://[^\s)>\"]+circleci[^\s)>\"]*", re.IGNORECASE)
COMMAND_HINT_RE = re.compile(
    r"^\s*(?:\$|./|bash\s|python\s|pip\s|npm\s|yarn\s|make\s|kubectl\s|helm\s|docker\s|git\s)",
    re.IGNORECASE,
)

RETRYABLE_HTTP_CODES = (429, 502, 503)
MAX_RETRIES = 3


_env_file_loaded = False


def _load_env_file(env_path="tools/.env"):
    """Load .env file once, only as fallback when runtime env lacks a variable."""
    global _env_file_loaded
    if _env_file_loaded:
        return
    _env_file_loaded = True
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


def load_env(env_path="tools/.env"):
    """Compatibility wrapper — triggers lazy .env load."""
    _load_env_file(env_path)


def _require_env(name):
    """Return env var: check runtime environment first, fall back to .env file."""
    val = os.environ.get(name, "").strip()
    if val:
        return val
    _load_env_file()
    val = os.environ.get(name, "").strip()
    if not val:
        raise ConfigError(f"{name} is not set. Add it to tools/.env or export it.")
    return val


# ---------------------------------------------------------------------------
# Core API with retry
# ---------------------------------------------------------------------------

def github_api(endpoint, token, params=None):
    """Call GitHub REST API with retry/backoff. Raises typed exceptions."""
    url = "https://api.github.com%s" % endpoint
    if params:
        qs = "&".join(
            "%s=%s" % (k, urllib.request.quote(str(v)))
            for k, v in params.items()
        )
        url = "%s?%s" % (url, qs)
    headers = {
        "Authorization": "Bearer %s" % token,
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    last_exc = None
    for attempt in range(1, MAX_RETRIES + 1):
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            last_exc = exc
            if exc.code in RETRYABLE_HTTP_CODES and attempt < MAX_RETRIES:
                wait = 2 ** attempt
                sys.stderr.write(
                    "Retry %d/%d after HTTP %s (waiting %ds)...\n"
                    % (attempt, MAX_RETRIES, exc.code, wait)
                )
                time.sleep(wait)
                continue
            break
        except urllib.error.URLError as exc:
            last_exc = exc
            if attempt < MAX_RETRIES:
                wait = 2 ** attempt
                sys.stderr.write(
                    "Retry %d/%d after network error (waiting %ds)...\n"
                    % (attempt, MAX_RETRIES, wait)
                )
                time.sleep(wait)
                continue
            break

    if isinstance(last_exc, urllib.error.HTTPError):
        code = last_exc.code
        detail = ""
        try:
            detail = last_exc.read().decode("utf-8", errors="replace")[:500]
        except Exception:
            pass
        msg = "GitHub API HTTP %d on %s: %s" % (code, endpoint, detail)
        if code in (401, 403):
            raise AuthError(msg, status_code=code)
        if code == 404:
            raise NotFoundError(msg)
        if code == 429:
            retry_after = last_exc.headers.get("Retry-After")
            raise RateLimitError(msg, retry_after=retry_after)
        raise HttpError(msg, status_code=code)

    if isinstance(last_exc, urllib.error.URLError):
        raise NetworkError("GitHub API network error on %s: %s" % (endpoint, last_exc))

    raise last_exc


def github_api_paginated(endpoint, token, params=None, max_items=500):
    """Fetch all pages from a GitHub REST list endpoint up to max_items."""
    params = dict(params or {})
    params.setdefault("per_page", 100)
    all_items = []
    page = 1
    while len(all_items) < max_items:
        params["page"] = page
        items = github_api(endpoint, token, params)
        if not items:
            break
        all_items.extend(items)
        if len(items) < int(params["per_page"]):
            break
        page += 1
    return all_items[:max_items]


# ---------------------------------------------------------------------------
# MCP Tool Call equivalents
# ---------------------------------------------------------------------------

def mcp_search_pull_requests(token, owner, repo, branch, title_keyword, count):
    """
    Equivalent MCP call:
    {
        "tool": "github-search_pull_requests",
        "parameters": {
            "query": "repo:<owner>/<repo> is:merged base:<branch> <keyword> in:title ...",
            "owner": "<owner>",
            "repo": "<repo>",
            "perPage": <count * 3>
        }
    }
    """
    query = (
        "repo:%s/%s is:pr is:merged base:%s %s in:title sort:updated-desc"
        % (owner, repo, branch, title_keyword)
    )
    data = github_api("/search/issues", token, {
        "q": query,
        "per_page": min(count * 3, 100),
        "sort": "updated",
        "order": "desc",
    })
    return data.get("items", [])


def mcp_get_pull_request(token, owner, repo, pr_number):
    """
    Equivalent MCP call:
    {
        "tool": "github-pull_request_read",
        "parameters": {
            "method": "get",
            "owner": "<owner>",
            "repo": "<repo>",
            "pullNumber": <pr_number>
        }
    }
    """
    return github_api("/repos/%s/%s/pulls/%s" % (owner, repo, pr_number), token)


def mcp_list_pull_requests(token, owner, repo, branch, max_items=500):
    """
    Equivalent MCP call (paginated):
    {
        "tool": "github-list_pull_requests",
        "parameters": {
            "owner": "<owner>",
            "repo": "<repo>",
            "state": "closed",
            "base": "<branch>",
            "sort": "updated",
            "direction": "desc",
            "perPage": 100
        }
    }
    """
    return github_api_paginated(
        "/repos/%s/%s/pulls" % (owner, repo),
        token,
        {"state": "closed", "base": branch, "sort": "updated", "direction": "desc"},
        max_items=max_items,
    )


def mcp_get_pr_files(token, owner, repo, pr_number):
    """
    Equivalent MCP call:
    {
        "tool": "github-pull_request_read",
        "parameters": {
            "method": "get_files",
            "owner": "<owner>",
            "repo": "<repo>",
            "pullNumber": <pr_number>
        }
    }
    """
    return github_api("/repos/%s/%s/pulls/%s/files" % (owner, repo, pr_number), token)


def mcp_get_pr_comments(token, owner, repo, pr_number):
    """
    Equivalent MCP call:
    {
        "tool": "github-pull_request_read",
        "parameters": {
            "method": "get_comments",
            "owner": "<owner>",
            "repo": "<repo>",
            "pullNumber": <pr_number>
        }
    }
    """
    return github_api_paginated(
        "/repos/%s/%s/issues/%s/comments" % (owner, repo, pr_number),
        token, max_items=100,
    )


def mcp_get_pr_review_comments(token, owner, repo, pr_number):
    """
    Equivalent MCP call:
    {
        "tool": "github-pull_request_read",
        "parameters": {
            "method": "get_review_comments",
            "owner": "<owner>",
            "repo": "<repo>",
            "pullNumber": <pr_number>
        }
    }
    """
    return github_api_paginated(
        "/repos/%s/%s/pulls/%s/comments" % (owner, repo, pr_number),
        token, max_items=100,
    )


def mcp_get_commit_status(token, owner, repo, commit_sha):
    """Fetches combined commit status for ANY commit SHA."""
    return github_api(
        "/repos/%s/%s/commits/%s/status" % (owner, repo, commit_sha), token
    )


def mcp_get_commit_check_runs(token, owner, repo, commit_sha):
    """Fetches check runs for ANY commit SHA."""
    return github_api(
        "/repos/%s/%s/commits/%s/check-runs" % (owner, repo, commit_sha), token
    )


def mcp_list_commits(token, owner, repo, branch, since=None, until=None, max_items=500):
    """
    Equivalent MCP call:
    {
        "tool": "github-list_commits",
        "parameters": {
            "owner": "<owner>",
            "repo": "<repo>",
            "sha": "<branch>",
            "since": "...",
            "until": "..."
        }
    }
    """
    params = {"sha": branch}
    if since:
        params["since"] = since
    if until:
        params["until"] = until
    return github_api_paginated(
        "/repos/%s/%s/commits" % (owner, repo), token, params, max_items=max_items
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def get_merge_commit_sha(token, owner, repo, branch, merged_at):
    """Resolve merge commit SHA by matching the merge timestamp on the target branch."""
    dt = datetime.strptime(merged_at.replace("Z", ""), "%Y-%m-%dT%H:%M:%S")
    since = (dt - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    until = (dt + timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    commits = mcp_list_commits(token, owner, repo, branch, since=since, until=until, max_items=5)
    if commits:
        return commits[0].get("sha")
    return None


def get_ci_status_for_commit(token, owner, repo, commit_sha):
    """Returns parsed CI status (statuses + check runs) for a given commit SHA."""
    result = {"commit_sha": commit_sha, "statuses": [], "check_runs": []}
    try:
        status_data = mcp_get_commit_status(token, owner, repo, commit_sha)
        for s in status_data.get("statuses", []):
            result["statuses"].append({
                "context": s.get("context", ""),
                "state": s.get("state", ""),
                "target_url": s.get("target_url", ""),
                "description": s.get("description", ""),
                "created_at": s.get("created_at", ""),
            })
    except Exception as e:
        result["status_error"] = str(e)
    try:
        checks_data = mcp_get_commit_check_runs(token, owner, repo, commit_sha)
        for c in checks_data.get("check_runs", []):
            result["check_runs"].append({
                "name": c.get("name", ""),
                "status": c.get("status", ""),
                "conclusion": c.get("conclusion", ""),
                "html_url": c.get("html_url", ""),
                "details_url": c.get("details_url", ""),
            })
    except Exception as e:
        result["checks_error"] = str(e)
    return result


def extract_release_key(title):
    m = re.search(r"(\d+\.\d+\.\d+(?:[-._]\S+)?)", title or "")
    return m.group(1) if m else title


def extract_tickets(text):
    return sorted(set(TICKET_PATTERN.findall(text or "")))


def extract_circleci_links(text):
    links = set()
    for m in CIRCLECI_URL_RE.finditer(text or ""):
        links.add(m.group(0).rstrip(".,"))
    return sorted(links)


def extract_commands(text):
    commands = set()
    lines = (text or "").splitlines()
    inside_fence = False
    for line in lines:
        stripped = line.rstrip()
        if stripped.strip().startswith("```"):
            inside_fence = not inside_fence
            continue
        if inside_fence and stripped.strip():
            commands.add(stripped.strip())
            continue
        lowered = stripped.lower()
        if "command:" in lowered:
            _, _, value = stripped.partition(":")
            if value.strip():
                commands.add(value.strip())
        elif COMMAND_HINT_RE.match(stripped):
            commands.add(stripped.strip())
    return sorted(commands)


def is_revert_of_release(pr, release_re):
    title = pr.get("title", "")
    return bool(REVERT_TITLE_RE.match(title) and release_re.search(title))


def parse_comment_texts(token, owner, repo, pr_number):
    """Fetch and combine all comment and review comment bodies for a PR."""
    texts = []
    try:
        comments = mcp_get_pr_comments(token, owner, repo, pr_number)
        for c in comments:
            if isinstance(c, dict):
                texts.append(c.get("body") or "")
    except Exception:
        pass
    try:
        reviews = mcp_get_pr_review_comments(token, owner, repo, pr_number)
        for c in reviews:
            if isinstance(c, dict):
                texts.append(c.get("body") or "")
    except Exception:
        pass
    return texts


# ---------------------------------------------------------------------------
# Release extraction logic
# ---------------------------------------------------------------------------

def build_releases(token, owner, repo, branch, count, release_re, history_limit,
                   include_comments=False, mode="prs"):
    title_keyword = "Release"
    m = re.search(r"\^?(\w+)", release_re.pattern)
    if m:
        title_keyword = m.group(1)

    step_total = 4 if mode in ("commits", "both") else 3
    step = [0]

    def log(msg):
        step[0] += 1
        print("[%d/%d] %s" % (step[0], step_total, msg))

    log("Searching release PRs on '%s'..." % branch)
    candidates = mcp_search_pull_requests(token, owner, repo, branch, title_keyword, count)

    release_prs = []
    reverted_numbers = set()

    for item in candidates:
        title = item.get("title", "")
        merged_at = (item.get("pull_request") or {}).get("merged_at")
        if not merged_at:
            continue
        if is_revert_of_release(item, release_re):
            ref_match = re.search(r"#(\d+)", title)
            if ref_match:
                reverted_numbers.add(int(ref_match.group(1)))
            body = item.get("body") or ""
            body_ref = re.search(r"#(\d+)", body)
            if body_ref:
                reverted_numbers.add(int(body_ref.group(1)))
            continue
        if release_re.search(title):
            release_prs.append({
                "number": item["number"],
                "title": title,
                "merged_at": merged_at,
            })

    release_prs = [r for r in release_prs if r["number"] not in reverted_numbers]
    release_prs.sort(key=lambda r: r["merged_at"], reverse=True)
    release_prs = release_prs[:count]

    if not release_prs:
        print("No release PRs found.")
        return []

    need_prev = count + 1
    all_candidates = mcp_search_pull_requests(
        token, owner, repo, branch, title_keyword, need_prev
    )
    all_release_dates = []
    for item in all_candidates:
        title = item.get("title", "")
        merged_at = (item.get("pull_request") or {}).get("merged_at")
        if merged_at and release_re.search(title) and not is_revert_of_release(item, release_re):
            if item["number"] not in reverted_numbers:
                all_release_dates.append({"merged_at": merged_at, "number": item["number"]})
    all_release_dates.sort(key=lambda r: r["merged_at"], reverse=True)

    log("Fetching merged PRs on '%s' (limit %d)..." % (branch, history_limit))
    all_prs = mcp_list_pull_requests(token, owner, repo, branch, max_items=history_limit)

    log("Building release windows...")
    results = []
    for rel in release_prs:
        window_end = rel["merged_at"]
        idx = next(
            (j for j, rd in enumerate(all_release_dates) if rd["number"] == rel["number"]),
            None,
        )
        window_start = None
        if idx is not None and idx + 1 < len(all_release_dates):
            window_start = all_release_dates[idx + 1]["merged_at"]

        associated = []
        for pr in all_prs:
            pr_merged = pr.get("merged_at")
            if not pr_merged:
                continue
            pr_title = pr.get("title", "")
            if release_re.search(pr_title) or REVERT_TITLE_RE.match(pr_title):
                continue
            if pr_merged <= window_end and (window_start is None or pr_merged > window_start):
                associated.append({
                    "number": pr["number"],
                    "title": pr_title,
                    "merged_at": pr_merged,
                    "url": pr.get("html_url", ""),
                })
        associated.sort(key=lambda p: p["merged_at"], reverse=True)

        pr_detail = mcp_get_pull_request(token, owner, repo, rel["number"])
        body_text = pr_detail.get("body") or ""
        head_sha = pr_detail.get("head", {}).get("sha", "")
        base_branch = pr_detail.get("base", {}).get("ref", "")

        combined_text = body_text
        comment_tickets = []
        comment_commands = []
        comment_circleci = []
        if include_comments:
            comment_texts = parse_comment_texts(token, owner, repo, rel["number"])
            combined_text = "\n".join([body_text] + comment_texts)
            for ct in comment_texts:
                comment_tickets.extend(extract_tickets(ct))
                comment_commands.extend(extract_commands(ct))
                comment_circleci.extend(extract_circleci_links(ct))

        tickets = extract_tickets(combined_text)
        commands = extract_commands(combined_text)
        circleci_links = extract_circleci_links(combined_text)

        associated_commits = []
        if mode in ("commits", "both") and window_start:
            log("Fetching commits between releases for PR #%d..." % rel["number"])
            since_dt = datetime.strptime(window_start.replace("Z", ""), "%Y-%m-%dT%H:%M:%S")
            until_dt = datetime.strptime(window_end.replace("Z", ""), "%Y-%m-%dT%H:%M:%S")
            raw_commits = mcp_list_commits(
                token, owner, repo, branch,
                since=(since_dt - timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                until=(until_dt + timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                max_items=500,
            )
            for c in raw_commits:
                commit_obj = c.get("commit", {})
                committed_at = commit_obj.get("committer", {}).get("date", "")
                if not committed_at:
                    continue
                if window_start and committed_at <= window_start:
                    continue
                if committed_at > window_end:
                    continue
                associated_commits.append({
                    "sha": c.get("sha", ""),
                    "message": commit_obj.get("message", "").split("\n")[0],
                    "author": commit_obj.get("author", {}).get("name", ""),
                    "committed_date": committed_at,
                    "url": c.get("html_url", ""),
                })
            associated_commits.sort(key=lambda x: x["committed_date"], reverse=True)

        results.append({
            "release_key": extract_release_key(rel["title"]),
            "release_title": rel["title"],
            "release_pr": rel["number"],
            "merged_at": rel["merged_at"],
            "head_sha": head_sha,
            "base_branch": base_branch,
            "tickets": tickets,
            "commands": commands,
            "circleci_links": circleci_links,
            "associated_prs": associated,
            "associated_commits": associated_commits,
            "url": pr_detail.get("html_url", ""),
        })

    return results


def enrich_with_ci_status(token, owner, repo, releases, branch):
    """Add premerge and postmerge CI status to each release."""
    print("[CI] Fetching CI status for %d release(s)..." % len(releases))
    for rel in releases:
        head_sha = rel.get("head_sha", "")
        if head_sha:
            print("  [CI] Premerge status for PR #%d (head: %s)..." % (rel["release_pr"], head_sha[:8]))
            rel["premerge_ci"] = get_ci_status_for_commit(token, owner, repo, head_sha)

        merge_sha = get_merge_commit_sha(token, owner, repo, branch, rel["merged_at"])
        if merge_sha:
            print("  [CI] Postmerge status for PR #%d (merge: %s)..." % (rel["release_pr"], merge_sha[:8]))
            rel["merge_commit_sha"] = merge_sha
            rel["postmerge_ci"] = get_ci_status_for_commit(token, owner, repo, merge_sha)
        else:
            rel["merge_commit_sha"] = None
            rel["postmerge_ci"] = {"error": "Could not resolve merge commit SHA"}


# ---------------------------------------------------------------------------
# Output formatters
# ---------------------------------------------------------------------------

def print_table(releases):
    sep = "-" * 140
    print("\n" + sep)
    print("%-30s | %-50s | %-25s | Associated PRs" % ("Release Key", "Release Title", "Merged At (UTC)"))
    print(sep)
    for rel in releases:
        assoc_strs = [
            "#%d %s (%s)" % (a["number"], a["title"][:60], a["merged_at"])
            for a in rel["associated_prs"]
        ]
        assoc_cell = "; ".join(assoc_strs) if assoc_strs else "(none)"
        print("%-30s | %-50s | %-25s | %s" % (
            rel["release_key"], rel["release_title"][:50], rel["merged_at"], assoc_cell
        ))
    print(sep)


def print_ci_table(releases):
    """Print CI status table for premerge and postmerge."""
    sep = "=" * 130
    for rel in releases:
        print("\n" + sep)
        print("Release: %s" % rel["release_title"])
        print("PR: #%d  |  Merged: %s" % (rel["release_pr"], rel["merged_at"]))
        print(sep)

        pre = rel.get("premerge_ci", {})
        post = rel.get("postmerge_ci", {})

        print("\n  PREMERGE CI  (head commit: %s)" % rel.get("head_sha", "N/A")[:12])
        print("  " + "-" * 110)
        print("  %-12s %-50s %-12s URL" % ("Type", "Job/Check", "Status"))
        print("  " + "-" * 110)
        for s in pre.get("statuses", []):
            print("  %-12s %-50s %-12s %s" % ("status", s["context"], s["state"], s.get("target_url", "")))
        for c in pre.get("check_runs", []):
            conclusion = c.get("conclusion") or c.get("status", "")
            print("  %-12s %-50s %-12s %s" % ("check", c["name"], conclusion, c.get("html_url", "")))
        if not pre.get("statuses") and not pre.get("check_runs"):
            print("  (none found)")

        merge_sha = rel.get("merge_commit_sha") or "N/A"
        display_sha = merge_sha[:12] if merge_sha != "N/A" else "N/A"
        print("\n  POSTMERGE CI  (merge commit: %s)" % display_sha)
        print("  " + "-" * 110)
        print("  %-12s %-50s %-12s URL" % ("Type", "Job/Check", "Status"))
        print("  " + "-" * 110)
        if isinstance(post, dict) and post.get("error"):
            print("  Error: %s" % post["error"])
        else:
            for s in post.get("statuses", []):
                print("  %-12s %-50s %-12s %s" % ("status", s["context"], s["state"], s.get("target_url", "")))
            for c in post.get("check_runs", []):
                conclusion = c.get("conclusion") or c.get("status", "")
                print("  %-12s %-50s %-12s %s" % ("check", c["name"], conclusion, c.get("html_url", "")))
            if not post.get("statuses") and not post.get("check_runs"):
                print("  (none found)")

    print("\n" + "=" * 130)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    token = _require_env("GITHUB_TOKEN")

    parser = argparse.ArgumentParser(
        description="MCP GitHub PR Client for release extraction"
    )
    parser.add_argument(
        "--repo", default="%s/%s" % (DEFAULT_OWNER, DEFAULT_REPO),
        help="Repository in owner/name format (default: %s/%s)" % (DEFAULT_OWNER, DEFAULT_REPO),
    )
    parser.add_argument("--branch", default="master", help="Target branch (default: master)")
    parser.add_argument("--count", type=int, default=1, help="Number of releases to fetch (default: 1)")
    parser.add_argument("--output-json", help="Save results to JSON file")
    parser.add_argument("--pr-number", type=int, help="Get details for a specific PR number")
    parser.add_argument("--pr-files", type=int, help="Get changed files for a specific PR number")
    parser.add_argument(
        "--ci-status", action="store_true",
        help="Include premerge + postmerge CircleCI status for each release",
    )
    parser.add_argument("--commit-status", metavar="SHA", help="Get CI status for a specific commit SHA")
    parser.add_argument(
        "--pr-title-regex", default=DEFAULT_RELEASE_REGEX,
        help='Filter release PRs by title regex (default: "%s")' % DEFAULT_RELEASE_REGEX,
    )
    parser.add_argument(
        "--history-pr-limit", type=int, default=500,
        help="How many recent PRs to scan for release windows (default: 500)",
    )
    parser.add_argument(
        "--mode", choices=["prs", "commits", "both"], default="prs",
        help="Data source: prs (default), commits, or both",
    )
    parser.add_argument(
        "--include-comments", action="store_true",
        help="Parse PR comments and review threads for tickets/commands/links",
    )
    args = parser.parse_args()

    if "/" not in args.repo:
        raise ConfigError("--repo must be owner/name format, got: %s" % args.repo)
    owner, repo = args.repo.split("/", 1)

    try:
        release_re = re.compile(args.pr_title_regex, re.IGNORECASE)
    except re.error as e:
        raise DataError("Invalid --pr-title-regex: %s" % e)

    if args.commit_status:
        print("Fetching CI status for commit %s..." % args.commit_status[:12])
        ci = get_ci_status_for_commit(token, owner, repo, args.commit_status)
        print(json.dumps(ci, indent=2))
        return

    if args.pr_number:
        pr = mcp_get_pull_request(token, owner, repo, args.pr_number)
        print(json.dumps(pr, indent=2))
        return

    if args.pr_files:
        files = mcp_get_pr_files(token, owner, repo, args.pr_files)
        for f in files:
            print("  %10s  %s" % (f.get("status", "?"), f.get("filename", "?")))
        return

    releases = build_releases(
        token, owner, repo, args.branch, args.count,
        release_re, args.history_pr_limit,
        include_comments=args.include_comments,
        mode=args.mode,
    )

    if not releases:
        print("No releases found.")
        return

    if args.ci_status:
        enrich_with_ci_status(token, owner, repo, releases, args.branch)
        print_ci_table(releases)
    else:
        print_table(releases)

    if args.output_json:
        with open(args.output_json, "w") as f:
            json.dump(releases, f, indent=2)
            f.write("\n")
        print("\nJSON saved to %s" % args.output_json)


if __name__ == "__main__":
    try:
        main()
    except ToolError as exc:
        print("Error [%s]: %s" % (type(exc).__name__, exc), file=sys.stderr)
        sys.exit(2 if isinstance(exc, ConfigError) else 1)
