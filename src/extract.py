"""Stage 1: Data extraction from Sourcegraph + GitHub."""

import json
import re
import urllib.request

from src.config import (
    _log, mcp_call_tool, _get_env,
    DEFAULT_REPO, GITHUB_REPO, TOOL_PREFIX,
)


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


def _resolve_fix_version_branches(branch):
    """Resolve latest Gerrit branches from Jira project fix versions.

    For non-master branches (e.g. ``ganges-7.5``), CRs are merged to
    specific Gerrit branches derived from fix versions (e.g.
    ``ganges-7.5.1.8-stable-pc``).  Since Gerrit branches accumulate
    commits from earlier branches, searching the latest one is enough
    to capture all releases.

    Returns a list of branch strings to search (may be empty).
    """
    if branch == "master":
        return []
    m = re.match(r"ganges-([\d.]+)", branch)
    if not m:
        return []
    branch_ver = m.group(1)

    from src.jira_client import _resolve_jira_token
    jira_token = _resolve_jira_token()
    jira_url = _get_env("JIRA_BASE_URL", "https://jira.nutanix.com")
    if not jira_token:
        return []

    url = f"{jira_url}/rest/api/2/project/ENG/versions"
    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Bearer {jira_token}")
    req.add_header("Content-Type", "application/json")

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            all_versions = json.loads(resp.read())
    except Exception:
        return []

    def _ver_key(v):
        return tuple(int(p) for p in v.split(".") if p.isdigit())

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

    MAX_CANDIDATES = 3
    versions_aos.sort(key=_ver_key, reverse=True)
    versions_pc.sort(key=_ver_key, reverse=True)

    branches = []
    for ver in versions_aos[:MAX_CANDIDATES]:
        branches.append(f"ganges-{ver}-stable")
    for ver in versions_pc[:MAX_CANDIDATES]:
        branches.append(f"ganges-{ver}-stable-pc")

    if branches:
        _log(f"Resolved fix-version Gerrit branches: {branches}")
    return branches


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


def fetch_gerrit_releases(server_key, branch, count):
    """Fetch release commits from Gerrit via Sourcegraph streaming API.

    Uses the streaming API directly (not MCP) to avoid the 615-char
    message truncation imposed by the MCP gateway.  Falls back to MCP
    if the streaming API is unavailable.
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

        query = f'type:commit {repo_filter} message:"Release gold image"'
        for c in _sg_stream_search(query, count=count * 4):
            oid = c.get("commit", "")
            if oid not in seen_oids:
                seen_oids.add(oid)
                all_commits.append(c)

        if all_commits:
            return all_commits

    _log("Streaming API unavailable, falling back to MCP commit_search")
    if branch == "master":
        repos = [DEFAULT_REPO]
        message_terms = ["Release", "gold image", "main-master"]
    else:
        repos = [DEFAULT_REPO]
        message_terms = ["Release", "gold image"]

    result = mcp_call_tool(server_key, f"{TOOL_PREFIX}commit_search", {
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

    result = mcp_call_tool(server_key, f"{TOOL_PREFIX}commit_search", {
        "repos": repos,
        "messageTerms": message_terms,
        "count": count * 4,
    })
    return _extract_commits(result)


def fetch_version_gi(server_key, commit_sha):
    """Read services/variables.sh at a specific commit and extract VERSION_GI.

    Returns: {"aos": "<version>", "pc": "<version>"} or None on failure.
    """
    result = mcp_call_tool(server_key, f"{TOOL_PREFIX}read_file", {
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

    result = mcp_call_tool(server_key, f"{TOOL_PREFIX}commit_search", {
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
