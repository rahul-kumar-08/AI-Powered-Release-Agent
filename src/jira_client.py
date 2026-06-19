"""Stage 2: Jira EPIC resolution, status filtering, git-tracker."""

import json
import re
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

from src.config import _log, mcp_call_tool, _get_env


def _resolve_jira_token():
    """Resolve Jira token from env, .env, or mcp.json (in that order)."""
    token = _get_env("JIRA_TOKEN")
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

    params = urllib.parse.urlencode({"jql": jql, "fields": "key,summary,status", "maxResults": 100})
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
    results = []
    for i in raw:
        status_obj = i.get("fields", {}).get("status", {})
        results.append({
            "key": i["key"],
            "summary": i["fields"]["summary"],
            "status": status_obj.get("name", "Unknown") if status_obj else "Unknown",
        })
    return results if results else []


def _jira_search_mcp(jql):
    """Search Jira via Atlassian MCP. Returns list of issues or None on failure."""
    try:
        result = mcp_call_tool("atlassian", "atlassian__jira_search", {
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

        try:
            parsed = json.loads(text)
            raw = parsed.get("issues", [])
            return [{"key": i["key"], "summary": i["summary"]} for i in raw] if raw else []
        except (json.JSONDecodeError, KeyError):
            pass

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


def _extract_version_from_jira_summary(summary):
    """Extract the goldimage version from a Jira EPIC summary."""
    cleaned = re.sub(r"(?i)^(PC\s*:\s*)?Release\s+[Gg]old\s+image\s+", "", summary).strip()
    cleaned = re.sub(r"\s*\(#\d+\)$", "", cleaned).strip()
    return cleaned


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

    return {
        "key": selected["key"],
        "summary": summary,
        "jira_version": jira_version,
        "status": selected.get("status", "Unknown"),
    }


def search_jira_epic(version_raw, release_type):
    """Search Jira for the EPIC ticket matching a GoldImage version.

    Returns: ticket key (str) or None
    """
    result = search_jira_epic_full(version_raw, release_type)
    return result["key"] if result else None


def search_jira_epic_full(version_raw, release_type):
    """Search Jira for the EPIC matching a GoldImage version.

    Returns full details: {"key": ..., "summary": ..., "jira_version": ..., "status": ...}
    or None if not found.
    """
    jql = f'issuetype = Epic AND summary ~ "{version_raw}" ORDER BY created DESC'

    issues = _jira_search_rest(jql)
    if issues is None:
        issues = _jira_search_mcp(jql)
    if not issues:
        return None

    return _select_epic_from_issues(issues, release_type)


def fetch_epic_statuses(ticket_keys):
    """Batch-fetch Jira statuses for a list of EPIC ticket keys.

    Returns dict mapping ticket key -> status name (e.g. "Closed", "In Progress").
    """
    if not ticket_keys:
        return {}

    valid_keys = [k for k in ticket_keys if re.match(r'^[A-Z]+-\d+$', k)]
    if not valid_keys:
        return {}

    jira_url = _get_env("JIRA_BASE_URL", "https://jira.nutanix.com")
    jira_token = _resolve_jira_token()
    if not jira_token:
        return {}

    import urllib.parse

    keys_jql = ", ".join(valid_keys)
    jql = f'key in ({keys_jql})'
    params = urllib.parse.urlencode({
        "jql": jql, "fields": "key,status", "maxResults": len(valid_keys),
    })
    url = f"{jira_url}/rest/api/2/search?{params}"

    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Bearer {jira_token}")
    req.add_header("Content-Type", "application/json")

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
    except Exception:
        return {}

    statuses = {}
    for issue in data.get("issues", []):
        key = issue["key"]
        status_obj = issue.get("fields", {}).get("status", {})
        statuses[key] = status_obj.get("name", "Unknown") if status_obj else "Unknown"
    return statuses


def filter_rows_by_epic_status(rows):
    """Remove rows whose EPIC ticket is not in Closed status.

    Returns (kept_rows, removed_rows).
    """
    epic_keys = set()
    for row in rows:
        m = re.search(r'([A-Z]+-\d+)', row.get("main_ticket", ""))
        if m:
            epic_keys.add(m.group(1))

    statuses = fetch_epic_statuses(list(epic_keys))

    kept, removed = [], []
    for row in rows:
        m = re.search(r'([A-Z]+-\d+)', row.get("main_ticket", ""))
        if m:
            key = m.group(1)
            status = statuses.get(key, "Unknown")
            row["epic_status"] = status
            if status.lower() != "closed":
                removed.append(row)
                continue
        kept.append(row)
    return kept, removed


def fetch_gerrit_cr_from_jira(ticket_keys, branch):
    """Extract Gerrit CR URL and merged date from Jira git-tracker comments."""
    empty = {"cr_url": "", "merged_date": None}
    jira_token = _resolve_jira_token()
    jira_url = _get_env("JIRA_BASE_URL", "https://jira.nutanix.com")
    if not jira_token:
        _log("Cannot fetch Gerrit CR: no Jira token available")
        return empty

    branch_short = re.sub(r'^ganges-', '', branch)

    candidates = _search_git_tracker_comments(
        ticket_keys, branch_short, branch, jira_url, jira_token)

    if not candidates:
        epic_children = _fetch_epic_children(ticket_keys, jira_url, jira_token)
        if epic_children:
            _log(f"EPIC has no git-tracker; checking {len(epic_children)} child issue(s)")
            candidates = _search_git_tracker_comments(
                epic_children, branch_short, branch, jira_url, jira_token)

    if not candidates:
        return empty

    best = max(candidates, key=lambda x: x.get("merged_date") or "")
    return best


def _search_git_tracker_comments(ticket_keys, branch_short, branch,
                                 jira_url, jira_token):
    """Search git-tracker comments on a list of Jira tickets."""
    candidates = []
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
            ver_match = re.search(
                r'JIRA Version \(branch equiv\)\s*:\s*(.+)', body)
            branch_match = re.search(r'Branch\s*:\s*(.+)', body)
            cr_match = re.search(
                r'Code Review URL\s*:\s*(https?://\S+)', body)
            if not cr_match:
                continue

            jira_ver = ver_match.group(1).strip() if ver_match else ""
            gerrit_branch = branch_match.group(1).strip() if branch_match else ""

            if (branch_short == jira_ver
                    or branch_short == gerrit_branch
                    or branch == gerrit_branch
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
    """Fetch child issue keys of one or more EPIC tickets."""
    import urllib.parse

    children = []
    seen = set()

    for key in epic_keys:
        if not re.match(r'^[A-Z]+-\d+$', key):
            continue

        jql = f'"Epic Link" = {key}'
        params = urllib.parse.urlencode({
            "jql": jql, "fields": "key", "maxResults": 20,
        })
        try:
            url = f"{jira_url}/rest/api/2/search?{params}"
            req = urllib.request.Request(url)
            req.add_header("Authorization", f"Bearer {jira_token}")
            req.add_header("Content-Type", "application/json")
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())
            for issue in data.get("issues", []):
                k = issue.get("key", "")
                if k and k not in seen:
                    seen.add(k)
                    children.append(k)
        except Exception:
            pass

        try:
            url = f"{jira_url}/rest/api/2/issue/{key}?fields=issuelinks,subtasks"
            req = urllib.request.Request(url)
            req.add_header("Authorization", f"Bearer {jira_token}")
            req.add_header("Content-Type", "application/json")
            with urllib.request.urlopen(req, timeout=10) as resp:
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


def validate_version_with_jira(heading_version, file_version, release_type):
    """Validate GoldImage version — CR commit heading is always the source of truth."""
    if not heading_version:
        return None

    epic_key = None
    jira_version = None
    jira_result = search_jira_epic_full(heading_version, release_type)
    if jira_result:
        epic_key = jira_result["key"]
        jira_version = jira_result["jira_version"]

    return {
        "confirmed_version": heading_version,
        "epic_key": epic_key,
        "source": "heading",
        "jira_version": jira_version,
    }


def resolve_merge_dates_from_jira(rows, branch):
    """Update row merge dates using Jira git-tracker comment timestamps."""
    from src.version import format_merge_date

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

    _log("Resolving CR merged dates from Jira git-tracker comments...")
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

    _log(f"CR merged dates resolved: {updated}/{len(rows)} rows updated from Jira")
