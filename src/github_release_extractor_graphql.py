#!/usr/bin/env python3
"""
Extract release versions, commands, tickets, commit details, and CircleCI links
using GitHub GraphQL API.
"""

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from collections import defaultdict


RELEASE_PATTERNS = [
    r"\bv?\d+\.\d+\.\d+(?:[-+._][A-Za-z0-9]+)?\b",
    r"\brelease[\/:\s-]+v?\d+\.\d+\.\d+(?:[-+._][A-Za-z0-9]+)?\b",
]
TICKET_PATTERN = r"\b[A-Z][A-Z0-9]+-\d+\b"
COMMAND_HINT_PATTERN = re.compile(
    r"^\s*(?:\$|./|bash\s|python\s|pip\s|npm\s|yarn\s|make\s|kubectl\s|helm\s|docker\s|git\s)",
    re.IGNORECASE,
)


def normalize_release(value):
    value = (value or "").strip()
    m = re.search(r"(v?\d+\.\d+\.\d+(?:[-+._][A-Za-z0-9]+)?)", value, flags=re.IGNORECASE)
    if not m:
        return value.lower()
    return m.group(1).lower()


def extract_releases(text):
    found = set()
    for pattern in RELEASE_PATTERNS:
        for m in re.finditer(pattern, text or "", flags=re.IGNORECASE):
            found.add(normalize_release(m.group(0)))
    return found


def release_version_key(rel):
    m = re.search(r"v?(\d+)\.(\d+)\.(\d+)", rel or "", flags=re.IGNORECASE)
    if not m:
        return (-1, -1, -1, rel or "")
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)), rel or "")


def select_releases(releases, mode):
    if not releases:
        return set()
    if mode == "all":
        return set(releases)
    ordered = sorted(releases, key=release_version_key)
    if mode == "min":
        return {ordered[0]}
    # default/select max
    return {ordered[-1]}


def extract_tickets(text):
    return {m.group(0) for m in re.finditer(TICKET_PATTERN, text or "")}


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
        elif COMMAND_HINT_PATTERN.match(stripped):
            commands.add(stripped.strip())
    return commands


def extract_circleci_links(text):
    links = set()
    for m in re.finditer(r"https?://[^\s)>\"]+", text or "", flags=re.IGNORECASE):
        url = m.group(0).rstrip(".,")
        if "circleci.com" in url.lower():
            links.add(url)
    return links


def _truncate_error(detail, max_len=200):
    text = re.sub(r"<[^>]+>", " ", detail).strip()
    text = re.sub(r"\s+", " ", text)
    if len(text) > max_len:
        text = text[:max_len] + "... [truncated]"
    return text


def graphql_post(token, query, variables, max_retries=3):
    body = json.dumps({"query": query, "variables": variables}).encode("utf-8")
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": "Bearer %s" % token,
        "Content-Type": "application/json",
        "User-Agent": "cursor-ai-release-extractor-graphql",
    }
    import time as _time
    retryable_codes = (502, 503, 429)
    last_exc = None
    for attempt in range(1, max_retries + 1):
        req = urllib.request.Request("https://api.github.com/graphql", data=body, headers=headers)
        try:
            with urllib.request.urlopen(req) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            if payload.get("errors"):
                raise RuntimeError("GraphQL errors: %s" % json.dumps(payload.get("errors")))
            return payload.get("data") or {}
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            short = _truncate_error(detail)
            last_exc = RuntimeError("GraphQL HTTP %s: %s" % (exc.code, short))
            if exc.code in retryable_codes and attempt < max_retries:
                wait = 2 ** attempt
                sys.stderr.write("Retry %d/%d after HTTP %s (waiting %ds)...\n" % (attempt, max_retries, exc.code, wait))
                _time.sleep(wait)
                continue
            raise last_exc
        except urllib.error.URLError as exc:
            last_exc = RuntimeError("GraphQL network error: %s" % exc)
            if attempt < max_retries:
                wait = 2 ** attempt
                sys.stderr.write("Retry %d/%d after network error (waiting %ds)...\n" % (attempt, max_retries, wait))
                _time.sleep(wait)
                continue
            raise last_exc
    raise last_exc


def parse_check_rollup(rollup_node):
    result = {"links": set(), "circleci_links": set(), "check_runs": []}
    if not isinstance(rollup_node, dict):
        return result
    contexts = (((rollup_node.get("contexts") or {}).get("nodes")) or [])
    for c in contexts:
        if not isinstance(c, dict):
            continue
        t = c.get("__typename") or ""
        if t == "CheckRun":
            check_run = {
                "name": c.get("name") or "",
                "status": c.get("status") or "",
                "conclusion": c.get("conclusion") or "",
                "details_url": c.get("detailsUrl") or "",
                "url": c.get("url") or "",
                "started_at": c.get("startedAt") or "",
                "completed_at": c.get("completedAt") or "",
            }
            result["check_runs"].append(check_run)
            for key in ("detailsUrl", "url"):
                v = c.get(key) or ""
                if not v:
                    continue
                result["links"].add(v)
                if "circleci.com" in v.lower():
                    result["circleci_links"].add(v)
        elif t == "StatusContext":
            v = c.get("targetUrl") or ""
            if v:
                result["links"].add(v)
                if "circleci.com" in v.lower():
                    result["circleci_links"].add(v)
    return result


def fetch_prs_graphql(owner, repo, pr_limit):
    query = """
    query($owner: String!, $repo: String!, $prFirst: Int!, $after: String) {
      repository(owner: $owner, name: $repo) {
        defaultBranchRef { name }
        pullRequests(first: $prFirst, after: $after, orderBy: {field: CREATED_AT, direction: DESC}, states: [OPEN, MERGED, CLOSED]) {
          pageInfo { hasNextPage endCursor }
          nodes {
            number
            title
            body
            url
            state
            baseRefName
            createdAt
            mergedAt
          }
        }
      }
    }
    """

    prs = []
    after = None
    while len(prs) < pr_limit:
        first = min(50, pr_limit - len(prs))
        data = graphql_post(
            os.getenv("GITHUB_TOKEN", ""),
            query,
            {"owner": owner, "repo": repo, "prFirst": first, "after": after},
        )
        repository = data.get("repository") or {}
        pr_conn = repository.get("pullRequests") or {}
        nodes = pr_conn.get("nodes") or []
        prs.extend(nodes)
        page_info = pr_conn.get("pageInfo") or {}
        if not page_info.get("hasNextPage"):
            break
        after = page_info.get("endCursor")
    return prs[:pr_limit], ((data.get("repository") or {}).get("defaultBranchRef") or {}).get("name") or ""


def fetch_pr_detail_graphql(owner, repo, number):
    query = """
    query($owner: String!, $repo: String!, $number: Int!) {
      repository(owner: $owner, name: $repo) {
        pullRequest(number: $number) {
          number
          title
          body
          url
          createdAt
          updatedAt
          closedAt
          mergedAt
          headRefOid
          baseRefName
          mergeCommit {
            oid
            url
            message
            committedDate
            statusCheckRollup {
              contexts(first: 50) {
                nodes {
                  __typename
                  ... on CheckRun { name status conclusion detailsUrl url startedAt completedAt }
                  ... on StatusContext { context state targetUrl createdAt }
                }
              }
            }
          }
          comments(first: 50) { nodes { body url createdAt } }
          reviewThreads(first: 20) { nodes { comments(first: 20) { nodes { body url createdAt } } } }
          commits(first: 50) {
            nodes {
              commit {
                oid
                url
                message
                authoredDate
                committedDate
                author { name email }
                statusCheckRollup {
                  contexts(first: 50) {
                    nodes {
                      __typename
                      ... on CheckRun { name status conclusion detailsUrl url startedAt completedAt }
                      ... on StatusContext { context state targetUrl createdAt }
                    }
                  }
                }
              }
            }
          }
        }
      }
    }
    """
    data = graphql_post(
        os.getenv("GITHUB_TOKEN", ""),
        query,
        {"owner": owner, "repo": repo, "number": number},
    )
    return ((data.get("repository") or {}).get("pullRequest") or {})


def build_release_window_map(all_prs, release_title_regex, latest_release_pr_count, base_branch):
    def is_revert_pr(pr):
        title = (pr.get("title") or "").strip().lower()
        return title.startswith("revert")

    def revert_references_release(revert_pr, release_pr):
        text = "%s\n%s" % ((revert_pr.get("title") or ""), (revert_pr.get("body") or ""))
        text_l = text.lower()
        if "#%s" % (release_pr.get("number")) in text:
            return True
        release_title = (release_pr.get("title") or "").strip().lower()
        if release_title and release_title in text_l:
            return True
        return False

    release_prs = []
    for pr in all_prs:
        title = pr.get("title") or ""
        merged_at = pr.get("mergedAt") or ""
        # Release boundaries must be based only on merged release PRs.
        if not merged_at:
            continue
        if base_branch and (pr.get("baseRefName") or "") != base_branch:
            continue
        if release_title_regex and not re.search(release_title_regex, title):
            continue
        release_prs.append(pr)

    reverted_release_numbers = set()
    for candidate in release_prs:
        c_merged_at = candidate.get("mergedAt") or ""
        if not c_merged_at:
            continue
        for pr in all_prs:
            p_merged_at = pr.get("mergedAt") or ""
            if not p_merged_at or p_merged_at <= c_merged_at:
                continue
            if base_branch and (pr.get("baseRefName") or "") != base_branch:
                continue
            if not is_revert_pr(pr):
                continue
            if revert_references_release(pr, candidate):
                reverted_release_numbers.add(candidate.get("number"))
                break

    release_prs = [
        pr for pr in release_prs if pr.get("number") not in reverted_release_numbers
    ]

    release_prs.sort(
        key=lambda x: (x.get("mergedAt") or ""),
        reverse=True,
    )

    if latest_release_pr_count > 0:
        selected_release_prs = release_prs[:latest_release_pr_count]
    else:
        selected_release_prs = release_prs

    release_numbers = set([(pr.get("number")) for pr in release_prs])
    associated_map = {}
    selected_numbers = set()
    for pr in selected_release_prs:
        number = pr.get("number")
        if number is not None:
            selected_numbers.add(number)

    for i, current in enumerate(release_prs):
        current_number = current.get("number")
        if current_number not in selected_numbers:
            continue

        current_merged = current.get("mergedAt") or current.get("createdAt") or ""
        previous = release_prs[i + 1] if i + 1 < len(release_prs) else None
        previous_merged = ""
        if previous:
            previous_merged = previous.get("mergedAt") or previous.get("createdAt") or ""

        associated_prs = []
        for pr in all_prs:
            merged_at = pr.get("mergedAt") or ""
            if not merged_at:
                continue
            if base_branch and (pr.get("baseRefName") or "") != base_branch:
                continue
            if previous_merged and merged_at <= previous_merged:
                continue
            if current_merged and merged_at > current_merged:
                continue
            if pr.get("number") in release_numbers:
                continue
            associated_prs.append(
                {
                    "number": pr.get("number"),
                    "title": pr.get("title") or "",
                    "url": pr.get("url") or "",
                    "state": pr.get("state") or "",
                    "created_at": pr.get("createdAt") or "",
                    "merged_at": merged_at,
                }
            )
        associated_prs.sort(key=lambda x: x.get("merged_at") or "", reverse=True)
        associated_map[current_number] = {
            "current_release_merged_at": current_merged,
            "previous_release_merged_at": previous_merged,
            "previous_release_pr": {
                "number": previous.get("number"),
                "title": previous.get("title") or "",
                "url": previous.get("url") or "",
                "merged_at": previous.get("mergedAt") or "",
            }
            if previous
            else {},
            "associated_prs_since_previous_release": associated_prs,
        }

    return selected_release_prs, associated_map


def fetch_commit_history_graphql(owner, repo, branch, commit_limit):
    query = """
    query($owner: String!, $repo: String!, $branch: String!, $first: Int!, $after: String) {
      repository(owner: $owner, name: $repo) {
        ref(qualifiedName: $branch) {
          target {
            ... on Commit {
              history(first: $first, after: $after) {
                pageInfo { hasNextPage endCursor }
                nodes {
                  oid
                  url
                  message
                  authoredDate
                  committedDate
                  author { name email }
                  statusCheckRollup {
                    contexts(first: 100) {
                      nodes {
                        __typename
                        ... on CheckRun { name status conclusion detailsUrl url startedAt completedAt }
                        ... on StatusContext { context state targetUrl createdAt }
                      }
                    }
                  }
                }
              }
            }
          }
        }
      }
    }
    """
    commits = []
    after = None
    while len(commits) < commit_limit:
        first = min(50, commit_limit - len(commits))
        data = graphql_post(
            os.getenv("GITHUB_TOKEN", ""),
            query,
            {
                "owner": owner,
                "repo": repo,
                "branch": "refs/heads/%s" % branch,
                "first": first,
                "after": after,
            },
        )
        repository = data.get("repository") or {}
        ref = repository.get("ref") or {}
        target = ref.get("target") or {}
        history = target.get("history") or {}
        nodes = history.get("nodes") or []
        commits.extend(nodes)
        page_info = history.get("pageInfo") or {}
        if not page_info.get("hasNextPage"):
            break
        after = page_info.get("endCursor")
    return commits[:commit_limit]


def parse_pr_comment_texts(pr):
    texts = []
    for c in ((pr.get("comments") or {}).get("nodes") or []):
        if isinstance(c, dict):
            texts.append(c.get("body") or "")
    for t in ((pr.get("reviewThreads") or {}).get("nodes") or []):
        for c in (((t.get("comments") or {}).get("nodes")) or []):
            if isinstance(c, dict):
                texts.append(c.get("body") or "")
    return texts


def build_release_map_from_prs(prs, pr_title_regex, release_selection, associated_map=None):
    release_map = defaultdict(
        lambda: {
            "tickets": set(),
            "commands": set(),
            "circleci_links": set(),
            "check_run_links": set(),
            "premerge_circleci_links": set(),
            "postmerge_circleci_links": set(),
            "premerge_check_runs": [],
            "postmerge_check_runs": [],
            "prs": [],
        }
    )

    for pr in prs:
        title = pr.get("title") or ""
        if pr_title_regex and not re.search(pr_title_regex, title):
            continue

        comment_texts = parse_pr_comment_texts(pr)
        pr_commits = []
        premerge_links = set()
        postmerge_links = set()
        premerge_check_runs = []
        postmerge_check_runs = []
        check_run_links = set()
        text_circleci = set()

        for node in ((pr.get("commits") or {}).get("nodes") or []):
            c = node.get("commit") or {}
            msg = c.get("message") or ""
            one = {
                "sha": c.get("oid") or "",
                "url": c.get("url") or "",
                "message": msg,
                "author": ((c.get("author") or {}).get("name") or ""),
                "authored_date": c.get("authoredDate") or "",
                "committed_date": c.get("committedDate") or "",
            }
            rollup = parse_check_rollup(c.get("statusCheckRollup"))
            premerge_links.update(rollup["circleci_links"])
            check_run_links.update(rollup["links"])
            premerge_check_runs.extend(rollup["check_runs"])
            text_circleci.update(extract_circleci_links(msg))
            pr_commits.append(one)

        merge_commit = pr.get("mergeCommit") or {}
        merge_rollup = parse_check_rollup(merge_commit.get("statusCheckRollup"))
        postmerge_links.update(merge_rollup["circleci_links"])
        check_run_links.update(merge_rollup["links"])
        postmerge_check_runs.extend(merge_rollup["check_runs"])
        if merge_commit:
            text_circleci.update(extract_circleci_links(merge_commit.get("message") or ""))

        combined_text = "\n".join([title, pr.get("body") or ""] + comment_texts)
        text_circleci.update(extract_circleci_links(combined_text))
        all_circleci = set()
        all_circleci.update(text_circleci)
        all_circleci.update(premerge_links)
        all_circleci.update(postmerge_links)

        releases = extract_releases(combined_text)
        if not releases:
            # fallback: try in commit messages
            commit_blob = "\n".join([(x.get("message") or "") for x in pr_commits])
            releases = extract_releases(commit_blob)
        if not releases:
            # Keep release PR windows even when semantic version is absent.
            releases = {"release-pr-%s" % (pr.get("number"))}
        else:
            releases = select_releases(releases, release_selection)

        tickets = set()
        commands = set()
        tickets.update(extract_tickets(combined_text))
        commands.update(extract_commands(combined_text))
        for c in pr_commits:
            tickets.update(extract_tickets(c.get("message") or ""))
            commands.update(extract_commands(c.get("message") or ""))

        for rel in releases:
            release_map[rel]["tickets"].update(tickets)
            release_map[rel]["commands"].update(commands)
            release_map[rel]["circleci_links"].update(all_circleci)
            release_map[rel]["check_run_links"].update(check_run_links)
            release_map[rel]["premerge_circleci_links"].update(premerge_links)
            release_map[rel]["postmerge_circleci_links"].update(postmerge_links)
            release_map[rel]["premerge_check_runs"].extend(premerge_check_runs)
            release_map[rel]["postmerge_check_runs"].extend(postmerge_check_runs)
            release_map[rel]["prs"].append(
                {
                    "number": pr.get("number"),
                    "url": pr.get("url") or "",
                    "title": title,
                    "created_at": pr.get("createdAt") or "",
                    "updated_at": pr.get("updatedAt") or "",
                    "closed_at": pr.get("closedAt") or "",
                    "merged_at": pr.get("mergedAt") or "",
                    "base_branch": pr.get("baseRefName") or "",
                    "head_sha": pr.get("headRefOid") or "",
                    "merge_commit_sha": merge_commit.get("oid") or "",
                    "tickets": sorted(tickets),
                    "commands": sorted(commands),
                    "circleci_links": sorted(all_circleci),
                    "check_run_links": sorted(check_run_links),
                    "premerge_circleci_links": sorted(premerge_links),
                    "postmerge_circleci_links": sorted(postmerge_links),
                    "premerge_check_runs": premerge_check_runs,
                    "postmerge_check_runs": postmerge_check_runs,
                    "commits": pr_commits,
                    "previous_release_pr": (associated_map or {}).get(pr.get("number"), {}).get("previous_release_pr", {}),
                    "associated_prs_since_previous_release": (
                        (associated_map or {}).get(pr.get("number"), {}).get(
                            "associated_prs_since_previous_release", []
                        )
                    ),
                    "associated_commits_since_previous_release": (
                        (associated_map or {}).get(pr.get("number"), {}).get(
                            "associated_commits_since_previous_release", []
                        )
                    ),
                }
            )
    return release_map


def build_release_map_from_commits(commits, release_selection):
    release_map = defaultdict(
        lambda: {
            "tickets": set(),
            "commands": set(),
            "circleci_links": set(),
            "check_run_links": set(),
            "premerge_circleci_links": set(),
            "postmerge_circleci_links": set(),
            "premerge_check_runs": [],
            "postmerge_check_runs": [],
            "commits": [],
        }
    )
    for c in commits:
        text = c.get("message") or ""
        releases = extract_releases(text)
        if not releases:
            continue
        releases = select_releases(releases, release_selection)
        tickets = extract_tickets(text)
        commands = extract_commands(text)
        ci_links = set()
        ci_links.update(extract_circleci_links(text))
        rollup = parse_check_rollup(c.get("statusCheckRollup"))
        ci_links.update(rollup["circleci_links"])
        for rel in releases:
            release_map[rel]["tickets"].update(tickets)
            release_map[rel]["commands"].update(commands)
            release_map[rel]["circleci_links"].update(ci_links)
            release_map[rel]["check_run_links"].update(rollup["links"])
            release_map[rel]["premerge_check_runs"].extend(rollup["check_runs"])
            release_map[rel]["commits"].append(
                {
                    "sha": c.get("oid") or "",
                    "url": c.get("url") or "",
                    "message": text,
                    "author": ((c.get("author") or {}).get("name") or ""),
                    "authored_date": c.get("authoredDate") or "",
                    "committed_date": c.get("committedDate") or "",
                    "check_run_links": sorted(rollup["links"]),
                    "check_runs": rollup["check_runs"],
                }
            )
    return release_map


def merge_release_maps(target, source):
    for rel, data in source.items():
        if rel not in target:
            target[rel] = {
                "tickets": set(),
                "commands": set(),
                "circleci_links": set(),
                "check_run_links": set(),
                "premerge_circleci_links": set(),
                "postmerge_circleci_links": set(),
                "premerge_check_runs": [],
                "postmerge_check_runs": [],
                "prs": [],
                "commits": [],
            }
        target[rel]["tickets"].update(data.get("tickets", set()))
        target[rel]["commands"].update(data.get("commands", set()))
        target[rel]["circleci_links"].update(data.get("circleci_links", set()))
        target[rel]["check_run_links"].update(data.get("check_run_links", set()))
        target[rel]["premerge_circleci_links"].update(data.get("premerge_circleci_links", set()))
        target[rel]["postmerge_circleci_links"].update(data.get("postmerge_circleci_links", set()))
        target[rel]["premerge_check_runs"].extend(data.get("premerge_check_runs", []))
        target[rel]["postmerge_check_runs"].extend(data.get("postmerge_check_runs", []))
        target[rel]["prs"].extend(data.get("prs", []))
        target[rel]["commits"].extend(data.get("commits", []))
    return target


def save_output_json(output_obj, output_path):
    with open(output_path, "w") as f:
        json.dump(output_obj, f, indent=2)
        f.write("\n")


def parse_args():
    parser = argparse.ArgumentParser(
        description="Extract release/version/ticket/CI data from GitHub GraphQL"
    )
    parser.add_argument("--repo", required=True, help="Repository in owner/name format")
    parser.add_argument("--token", default=os.getenv("GITHUB_TOKEN", ""), help="GitHub token")
    parser.add_argument("--release", default="", help="Optional release filter, example v1.2.3")
    parser.add_argument("--mode", choices=["prs", "commits", "both"], default="both")
    parser.add_argument("--branch", default="", help="Commit branch for commit mode (default: repo default)")
    parser.add_argument(
        "--base-branch",
        default="",
        help="Only include PRs merged into this base branch (default: --branch or repo default)",
    )
    parser.add_argument("--limit", type=int, default=100, help="PR/commit limit")
    parser.add_argument("--pr-title-regex", default="", help='Filter PRs by title regex, example "^Release"')
    parser.add_argument(
        "--latest-release-pr-count",
        type=int,
        default=0,
        help="If >0, take latest N PRs matching release regex/title regex",
    )
    parser.add_argument(
        "--release-selection",
        choices=["max", "min", "all"],
        default="max",
        help="When multiple releases are found in one PR/commit, choose max, min, or all (default: max)",
    )
    parser.add_argument("--output-json", default="", help="Save result to this file path")
    parser.add_argument(
        "--history-pr-limit",
        type=int,
        default=500,
        help="How many recent PRs to inspect to build release windows (default: 500)",
    )
    parser.add_argument(
        "--history-commit-limit",
        type=int,
        default=2000,
        help="How many recent commits to inspect to build release windows (default: 2000)",
    )
    return parser.parse_args()


def to_serializable(release_map, release_filter):
    output = {}
    items = sorted(release_map.items())
    for rel, data in items:
        if release_filter and normalize_release(release_filter) != rel:
            continue
        output[rel] = {
            "tickets": sorted(data["tickets"]),
            "commands": sorted(data["commands"]),
            "circleci_links": sorted(data["circleci_links"]),
            "check_run_links": sorted(data["check_run_links"]),
            "premerge_circleci_links": sorted(data["premerge_circleci_links"]),
            "postmerge_circleci_links": sorted(data["postmerge_circleci_links"]),
            "premerge_check_runs": data["premerge_check_runs"],
            "postmerge_check_runs": data["postmerge_check_runs"],
            "prs": data["prs"],
            "commits": data["commits"],
        }
    if release_filter:
        key = normalize_release(release_filter)
        if key not in output:
            output[key] = {
                "tickets": [],
                "commands": [],
                "circleci_links": [],
                "check_run_links": [],
                "premerge_circleci_links": [],
                "postmerge_circleci_links": [],
                "premerge_check_runs": [],
                "postmerge_check_runs": [],
                "prs": [],
                "commits": [],
            }
    return output


def main():
    args = parse_args()
    if not args.token:
        print("Error: missing token. Set GITHUB_TOKEN or pass --token", file=sys.stderr)
        return 2
    if "/" not in args.repo:
        print("Error: --repo must be owner/name", file=sys.stderr)
        return 2
    if args.limit <= 0:
        print("Error: --limit must be > 0", file=sys.stderr)
        return 2

    owner, repo = args.repo.split("/", 1)
    release_map = {}

    try:
        history_limit = args.history_pr_limit if args.mode in ("prs", "both") else 1
        prs, default_branch = fetch_prs_graphql(owner, repo, history_limit)
        branch = (args.branch or default_branch or "master").strip()
        base_branch = (args.base_branch or branch).strip()

        if args.mode in ("prs", "both"):
            regex_for_latest = args.pr_title_regex or r"^Release"
            filtered_prs, associated_map = build_release_window_map(
                prs, regex_for_latest, args.latest_release_pr_count, base_branch
            )
            all_window_commits = fetch_commit_history_graphql(
                owner, repo, branch, args.history_commit_limit
            )
            for release_pr in filtered_prs:
                pr_number = release_pr.get("number")
                meta = associated_map.get(pr_number) or {}
                current_merged = meta.get("current_release_merged_at") or ""
                previous_merged = meta.get("previous_release_merged_at") or ""
                commits_between = []
                for c in all_window_commits:
                    committed_at = c.get("committedDate") or ""
                    if not committed_at:
                        continue
                    if previous_merged and committed_at <= previous_merged:
                        continue
                    if current_merged and committed_at > current_merged:
                        continue
                    commits_between.append(
                        {
                            "sha": c.get("oid") or "",
                            "url": c.get("url") or "",
                            "message": c.get("message") or "",
                            "author": ((c.get("author") or {}).get("name") or ""),
                            "committed_date": committed_at,
                        }
                    )
                commits_between.sort(key=lambda x: x.get("committed_date") or "", reverse=True)
                meta["associated_commits_since_previous_release"] = commits_between
                associated_map[pr_number] = meta

            detailed_prs = []
            for pr in filtered_prs:
                num = pr.get("number")
                if num is None:
                    continue
                detail = fetch_pr_detail_graphql(owner, repo, int(num))
                if detail:
                    detailed_prs.append(detail)

            pr_map = build_release_map_from_prs(
                detailed_prs,
                args.pr_title_regex or None,
                args.release_selection,
                associated_map=associated_map,
            )
            release_map = merge_release_maps(release_map, pr_map)

        if args.mode in ("commits", "both"):
            commits = fetch_commit_history_graphql(owner, repo, branch, args.limit)
            commit_map = build_release_map_from_commits(commits, args.release_selection)
            release_map = merge_release_maps(release_map, commit_map)

        out = to_serializable(release_map, args.release)
        if args.output_json:
            save_output_json(out, args.output_json)
            print("Saved output JSON to: %s" % args.output_json)
        else:
            print(json.dumps(out, indent=2))
        return 0
    except RuntimeError as exc:
        print("Error: %s" % str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
