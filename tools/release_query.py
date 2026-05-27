#!/usr/bin/env python3
"""
Unified Release Query — fast, concurrent pipeline for GoldImage release data.

Combines GitHub (release PR search), Sourcegraph/Gerrit (CR merge validation),
and Jira (Epic lookup) into a single command with ThreadPoolExecutor-based
parallelism.  Replaces the sequential MCP workflow for ~5x speed-up.

Usage:
  python3 tools/release_query.py --branch master --count 5
  python3 tools/release_query.py --branch master --count 7 --filter pc
  python3 tools/release_query.py --branch master --count 10 --with-sg-date --with-github-date
  python3 tools/release_query.py --branch master --count 5 --format json --output /tmp/out.json
  python3 tools/release_query.py --branch master --count 5 --no-cache

Environment variables (from tools/.env):
  GITHUB_TOKEN       — GitHub PAT
  SOURCEGRAPH_TOKEN  — Sourcegraph access token
  SOURCEGRAPH_URL    — Sourcegraph base URL (default: https://sourcegraph.ntnxdpro.com)
  JIRA_BASE_URL      — Jira server URL
  JIRA_API_TOKEN     — Jira Bearer token
"""

import argparse
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

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

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
GITHUB_API = "https://api.github.com"
DEFAULT_OWNER = "nutanix-core"
DEFAULT_REPO = "aos-goldimage-os"
DEFAULT_SG_URL = "https://sourcegraph.ntnxdpro.com"
SG_GERRIT_REPO = "nugerrit.ntnxdpro.com/main"
ENDOR_BASE = "http://endor.dyn.nutanix.com/GoldImages/Centos_SVM/Master"

RELEASE_RE = re.compile(r"^Release", re.IGNORECASE)
REVERT_RE = re.compile(r"^Revert\b", re.IGNORECASE)
PC_SPLIT_RE = re.compile(r"/PC:\s*", re.IGNORECASE)
SECONDARY_SPLIT_RE = re.compile(r"/(?=main-|sts-)", re.IGNORECASE)
EPIC_LINE_RE = re.compile(r"Epic'?s?\s*:\s*(.+)", re.IGNORECASE)
VERSION_RE = re.compile(r"[Rr]elease\s*[Gg]old\s+[Ii]mage\s+([\w.\-]+)")
VERSION_RHEL_RE = re.compile(r"(?:main-[\w.\-]+|sts-[\w.\-]+)-rhel(\d+)\.(\d+)-(.*)")

KERNEL_MAP = {"9": "5.14.0"}
CACHE_DIR = "/tmp"
CACHE_TTL = 300  # seconds

MAX_WORKERS = 10
RETRYABLE_HTTP = (429, 502, 503)
MAX_RETRIES = 3

# ---------------------------------------------------------------------------
# Env helpers
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
            os.environ.setdefault(key.strip(), val.strip().strip("\"'"))


def _env(name):
    val = os.environ.get(name, "").strip()
    if not val:
        raise ConfigError(f"{name} not set. Add to tools/.env or export it.")
    return val

# ---------------------------------------------------------------------------
# Generic HTTP with retry
# ---------------------------------------------------------------------------

def _http_get(url, headers, timeout=30):
    last_exc = None
    for attempt in range(1, MAX_RETRIES + 1):
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            last_exc = exc
            if exc.code in RETRYABLE_HTTP and attempt < MAX_RETRIES:
                time.sleep(2 ** attempt)
                continue
            break
        except urllib.error.URLError as exc:
            last_exc = exc
            if attempt < MAX_RETRIES:
                time.sleep(2 ** attempt)
                continue
            break

    if isinstance(last_exc, urllib.error.HTTPError):
        code = last_exc.code
        detail = ""
        try:
            detail = last_exc.read().decode("utf-8", errors="replace")[:500]
        except Exception:
            pass
        msg = f"HTTP {code}: {detail}"
        if code in (401, 403):
            raise AuthError(msg, status_code=code)
        if code == 429:
            raise RateLimitError(msg)
        raise HttpError(msg, status_code=code)
    if isinstance(last_exc, urllib.error.URLError):
        raise NetworkError(f"Network error: {last_exc}")
    raise last_exc  # type: ignore[misc]

# ---------------------------------------------------------------------------
# GitHub
# ---------------------------------------------------------------------------

def github_search_releases(token, owner, repo, branch, count):
    """Single GitHub search call — returns release PR dicts with title, body, merged_at.

    Revert handling: builds a per-version timeline of (merged_at, action)
    events. If the most-recent event for a goldimage version is a revert
    (i.e. reverted and never re-released), every release PR containing that
    version is excluded.
    """
    query = (
        f"repo:{owner}/{repo} is:pr is:merged base:{branch} "
        f"Release in:title sort:updated-desc"
    )
    params = urllib.parse.urlencode({
        "q": query, "per_page": min(max(count * 5, 50), 100),
        "sort": "updated", "order": "desc",
    })
    url = f"{GITHUB_API}/search/issues?{params}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    body = _http_get(url, headers)
    data = json.loads(body)
    items = data.get("items", [])

    version_events = {}  # version_str → [(merged_at, 'release'|'revert')]
    reverted_numbers = set()
    releases = []

    for item in items:
        title = item.get("title", "")
        merged_at = (item.get("pull_request") or {}).get("merged_at")
        if not merged_at:
            continue
        is_revert = bool(REVERT_RE.match(title))
        # RELEASE_RE is ^-anchored, so it won't match revert titles like
        # 'Revert "Release ..."'.  For reverts, check "release" anywhere.
        if is_revert:
            if not re.search(r"release", title, re.IGNORECASE):
                continue
        elif not RELEASE_RE.search(title):
            continue

        versions = [m.group(1) for m in VERSION_RE.finditer(title)]
        action = "revert" if is_revert else "release"
        for v in versions:
            version_events.setdefault(v, []).append((merged_at, action))

        if is_revert:
            for text in (title, item.get("body") or ""):
                for m in re.finditer(r"#(\d+)", text):
                    reverted_numbers.add(int(m.group(1)))
                for m in re.finditer(r"/pull/(\d+)", text):
                    reverted_numbers.add(int(m.group(1)))
            continue

        releases.append({
            "number": item["number"],
            "title": title,
            "body": item.get("body") or "",
            "merged_at": merged_at,
        })

    # Versions whose latest event is a revert → currently reverted
    reverted_versions = set()
    for version, events in version_events.items():
        events.sort(key=lambda x: x[0], reverse=True)
        if events[0][1] == "revert":
            reverted_versions.add(version)

    filtered = []
    for r in releases:
        if r["number"] in reverted_numbers:
            continue
        versions = [m.group(1) for m in VERSION_RE.finditer(r["title"])]
        if versions and any(v in reverted_versions for v in versions):
            continue
        filtered.append(r)

    filtered.sort(key=lambda r: r["merged_at"], reverse=True)
    return filtered

# ---------------------------------------------------------------------------
# Sourcegraph / Gerrit
# ---------------------------------------------------------------------------

def sg_commit_search(token, base_url, heading, count=5, gerrit_ref=None):
    """Query Sourcegraph for commits matching heading on Gerrit repo.

    If *gerrit_ref* is supplied (e.g. "ganges-7.5.1.4-stable-pc"), the query
    uses ``rev:<gerrit_ref>`` so that only commits on that specific Gerrit
    branch are searched.
    """
    rev_clause = f" rev:{gerrit_ref}" if gerrit_ref else ""
    query = (
        f'type:commit repo:^{SG_GERRIT_REPO}${rev_clause}'
        f' message:"{heading}" count:{count}'
    )
    params = urllib.parse.urlencode({"q": query})
    url = f"{base_url.rstrip('/')}/.api/search/stream?{params}"
    headers = {"Authorization": f"token {token}", "Accept": "text/event-stream"}
    body = _http_get(url, headers)

    commits = []
    for line in body.split("\n"):
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload:
            continue
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
                "date": item.get("committerDate") or item.get("authorDate", ""),
            })
    return commits


def sg_check_heading(sg_token, sg_url, heading, gerrit_ref=None):
    """Check one heading against Sourcegraph. Returns (status, date_str).

    Return values for *status*:
      - ``True``  — commit found on Gerrit (merged)
      - ``False`` — revert commit found (positively reverted)
      - ``None``  — no commit found (unknown / not yet merged)

    *gerrit_ref* (e.g. "ganges-7.5.1.4-stable-pc") is forwarded to
    ``sg_commit_search`` so non-master branches are searched on the correct
    Gerrit branch.
    """
    if not heading:
        return None, None
    try:
        commits = sg_commit_search(sg_token, sg_url, heading, gerrit_ref=gerrit_ref)
    except ToolError:
        return None, None
    if not commits:
        return None, None
    commits.sort(key=lambda c: c["date"], reverse=True)
    if REVERT_RE.match(commits[0].get("message", "")):
        return False, None
    return True, commits[0]["date"]


def _extract_release_num(version_or_heading):
    """Extract the kernel-release suffix used for SG matching.

    ``sts-ganges-pc.7.5-rhel8.10-5.10.237-7.0.0`` → ``5.10.237-7.0.0``
    ``main-master-rhel9.7-9.2.0``                  → ``9.2.0``
    """
    m = VERSION_RHEL_RE.search(version_or_heading or "")
    if m:
        return m.group(3)  # rest after rhelX.Y-
    return None


def sg_batch_lookup(sg_token, sg_url, gerrit_ref, comp_type, count=50):
    """Fetch ALL GI release commits from a single Gerrit branch.

    Returns a dict mapping release-number → (merged_bool, date_str).
    """
    try:
        commits = sg_commit_search(
            sg_token, sg_url, "Release Gold image",
            count=count, gerrit_ref=gerrit_ref,
        )
    except ToolError:
        return {}

    result = {}
    for c in commits:
        msg = c.get("message", "")
        if "release gold image" not in msg.lower():
            continue
        date = c.get("date", "")
        if REVERT_RE.match(msg):
            ver = VERSION_RE.search(msg)
            if ver:
                rel = _extract_release_num(ver.group(1))
                if rel:
                    result[rel] = (False, None)
            continue
        ver = VERSION_RE.search(msg)
        if ver:
            rel = _extract_release_num(ver.group(1))
            if rel and rel not in result:
                result[rel] = (True, date)
    return result

# ---------------------------------------------------------------------------
# Jira
# ---------------------------------------------------------------------------

def jira_epic_search(jira_url, jira_token, version_string):
    """Search Jira for an Epic matching the version.

    Returns ``{"key": "ENG-123", "fix_versions": ["pc.7.5.1.8"]}``
    or ``None`` if no Epic found.
    """
    jql = f'issuetype = Epic AND summary ~ "{version_string}" ORDER BY created DESC'
    params = urllib.parse.urlencode({
        "jql": jql, "fields": "key,summary,fixVersions", "maxResults": 3,
    })
    url = f"{jira_url.rstrip('/')}/rest/api/2/search?{params}"
    headers = {
        "Authorization": f"Bearer {jira_token}",
        "Accept": "application/json",
    }
    try:
        body = _http_get(url, headers)
        data = json.loads(body)
        issues = data.get("issues", [])
        if issues:
            issue = issues[0]
            fields = issue.get("fields", {})
            fv_list = fields.get("fixVersions") or []
            fix_versions = [v.get("name", "") for v in fv_list if v.get("name")]
            return {"key": issue["key"], "fix_versions": fix_versions}
    except ToolError:
        pass
    return None


def jira_get_epic(jira_url, jira_token, epic_key):
    """Fetch a single EPIC by key and return its fix versions.

    Returns ``{"key": "ENG-123", "fix_versions": ["pc.7.5.1.8"]}``
    or ``None`` on failure.
    """
    params = urllib.parse.urlencode({"fields": "key,summary,fixVersions"})
    url = f"{jira_url.rstrip('/')}/rest/api/2/issue/{epic_key}?{params}"
    headers = {
        "Authorization": f"Bearer {jira_token}",
        "Accept": "application/json",
    }
    try:
        body = _http_get(url, headers)
        data = json.loads(body)
        fields = data.get("fields", {})
        fv_list = fields.get("fixVersions") or []
        fix_versions = [v.get("name", "") for v in fv_list if v.get("name")]
        return {"key": data["key"], "fix_versions": fix_versions}
    except ToolError:
        pass
    return None


GIT_TRACKER_RE = re.compile(
    r"===git tracker===.*?"
    r"Committed Date:\s*'(.+?)'"
    r".*?Branch:\s*(\S+)"
    r".*?Code Review URL:\s*(\S+)",
    re.DOTALL,
)


def jira_epic_git_tracker(jira_url, jira_token, epic_key):
    """Parse the ===git tracker=== comment from an EPIC ticket.

    Returns ``{"branch": "ganges-7.6-stable", "commit_date": "...",
    "cr_url": "https://..."}`` or ``None``.
    """
    url = (
        f"{jira_url.rstrip('/')}/rest/api/2/issue/{epic_key}"
        f"/comment?maxResults=10&orderBy=-created"
    )
    headers = {
        "Authorization": f"Bearer {jira_token}",
        "Accept": "application/json",
    }
    try:
        body = _http_get(url, headers)
        data = json.loads(body)
        for comment in data.get("comments", []):
            text = comment.get("body", "")
            m = GIT_TRACKER_RE.search(text)
            if not m:
                continue
            raw_date, branch, cr_url = m.group(1), m.group(2), m.group(3)
            iso_date = None
            try:
                normalized = re.sub(r"\s+", " ", raw_date).strip()
                dt = datetime.strptime(normalized, "%a %b %d %H:%M:%S UTC %Y")
                iso_date = dt.strftime("%Y-%m-%dT%H:%M:%SZ")
            except ValueError:
                pass
            return {"branch": branch, "commit_date": iso_date, "cr_url": cr_url}
    except ToolError:
        pass
    return None

# ---------------------------------------------------------------------------
# Title / version parsing
# ---------------------------------------------------------------------------

def split_title(title):
    """Split combined title into AOS and PC headings."""
    parts = PC_SPLIT_RE.split(title, maxsplit=1)
    if len(parts) == 2:
        return parts[0].strip(), parts[1].strip()
    parts = SECONDARY_SPLIT_RE.split(title, maxsplit=1)
    if len(parts) == 2:
        prefix = parts[0].strip()
        second = parts[1].strip()
        if not RELEASE_RE.match(second):
            second = f"Release gold image {second}"
        return prefix, second
    return title.strip(), None


def extract_version(heading):
    """Extract version from heading like 'Release gold image main-master-rhel9.7-9.2.0'."""
    m = VERSION_RE.search(heading or "")
    return m.group(1) if m else None


def parse_rhel(version):
    """Parse version string → (rhel_major, rhel_minor, release, kernel_or_None).

    Handles both master-style (``main-master-rhel…``) and ganges-style
    (``sts-ganges-…-rhel…``) version strings.

    Examples::
        'main-master-rhel9.7-9.2.0'          → (9, 7, '9.2.0', None)
        'main-master-rhel8.10-5.10.237-4.3.0' → (8, 10, '4.3.0', '5.10.237')
        'sts-ganges-7.5-rhel8.10-5.10.237-7.0.1' → (8, 10, '7.0.1', '5.10.237')
        'sts-ganges-pc.7.5-rhel8.10-5.10.237-7.0.1' → (8, 10, '7.0.1', '5.10.237')
    """
    if not version:
        return None
    m = VERSION_RHEL_RE.match(version)
    if not m:
        return None
    major, minor, rest = int(m.group(1)), int(m.group(2)), m.group(3)
    if major <= 8:
        parts = rest.split("-", 1)
        if len(parts) == 2:
            return major, minor, parts[1], parts[0]
        return major, minor, rest, None
    return major, minor, rest, None


def build_goldimage_version(version, rhel_info):
    """Build display version.

    For master branches: ``main-master-rhel9.7-5.14.0-9.2.0``
    For non-master main-: ``main-ganges-7.6-rhel9.7-5.14.0-9.0.0``
    For sts-: preserves original version string.
    """
    if not version:
        return "—"
    if not version.startswith("main-"):
        return version
    if not rhel_info:
        return version
    major, minor, release, explicit_kernel = rhel_info
    kernel = explicit_kernel or KERNEL_MAP.get(str(major), "5.14.0")
    prefix = version.split("-rhel")[0]
    return f"{prefix}-rhel{major}.{minor}-{kernel}-{release}"


def build_endor_urls(rhel_info):
    """Build (changelog_url, rpm_url) from parsed RHEL info."""
    if not rhel_info:
        return "—", "—"
    major, minor, release, explicit_kernel = rhel_info
    kernel = explicit_kernel or KERNEL_MAP.get(str(major), "5.14.0")
    tag = f"RHEL{major}{minor}"
    folder = f"{tag}-SVM-{major}.{minor}-k{kernel}-r{release}.x86_64"
    base = f"{ENDOR_BASE}/{folder}"
    return f"[changelog]({base}/changelog.txt)", f"[rpm]({base}/rpm.txt)"


def extract_epics_from_body(body):
    """Parse Epic's: line from PR body. Returns list of ticket keys."""
    m = EPIC_LINE_RE.search(body or "")
    if not m:
        return []
    raw = m.group(1).strip()
    tickets = re.findall(r"[A-Z][A-Z0-9]+-\d+", raw)
    return tickets



def _version_tuple(v):
    """Convert dotted version string to a tuple of ints for numeric comparison."""
    try:
        return tuple(int(x) for x in v.split("."))
    except ValueError:
        return (0,)


def _gerrit_ref_version_key(ref):
    """Extract numeric version tuple from a Gerrit ref like 'ganges-7.3.1.10-stable-pc'."""
    m = re.search(r"ganges-([\d.]+)-stable", ref)
    if m:
        return _version_tuple(m.group(1))
    return (0,)


def resolve_gerrit_ref_from_fix_versions(fix_versions, comp_type):
    """Build the Gerrit branch name from EPIC fix versions.

    Accepts a list like ``["pc.7.5.1.3", "pc.7.5.1.8"]``, strips the
    ``pc.``/``aos.`` prefix, sorts numerically, and returns the Gerrit
    branch for the **latest** (highest) version.

    Returns ``None`` when no usable fix version is found.

    Examples::
        (["pc.7.5.1.8"], "PC")  → "ganges-7.5.1.8-stable-pc"
        (["7.5.1.4"],     "AOS") → "ganges-7.5.1.4-stable"
        ([],              "PC")  → None
    """
    if not fix_versions:
        return None

    cleaned = []
    for fv in fix_versions:
        v = re.sub(r"^(pc|aos|ganges)[.\-]", "", fv, flags=re.IGNORECASE)
        if re.match(r"[\d.]+$", v):
            cleaned.append(v)
    if not cleaned:
        return None

    latest = max(cleaned, key=_version_tuple)
    suffix = "-stable-pc" if comp_type == "PC" else "-stable"
    return f"ganges-{latest}{suffix}"


def format_date(iso_str):
    """Convert ISO date to DD-Mon-YYYY."""
    if not iso_str:
        return "—"
    try:
        dt = datetime.strptime(iso_str[:10], "%Y-%m-%d")
        return dt.strftime("%d-%b-%Y")
    except (ValueError, TypeError):
        return iso_str[:10]

# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

def run_pipeline(branch, count, owner, repo, filter_type, with_sg, with_gh, no_cache):
    load_env()
    gh_token = _env("GITHUB_TOKEN")

    sg_token = os.environ.get("SOURCEGRAPH_TOKEN", "").strip()
    sg_url = os.environ.get("SOURCEGRAPH_URL", DEFAULT_SG_URL).strip()
    jira_url = os.environ.get("JIRA_BASE_URL", "").strip()
    jira_token = os.environ.get("JIRA_API_TOKEN", "").strip()

    cache_key = f"{owner}_{repo}_{branch}_{count}_{filter_type}"
    cache_path = os.path.join(CACHE_DIR, f"release_cache_{hashlib.md5(cache_key.encode()).hexdigest()[:12]}.json")

    if not no_cache and os.path.isfile(cache_path):
        age = time.time() - os.path.getmtime(cache_path)
        if age < CACHE_TTL:
            with open(cache_path) as f:
                cached = json.load(f)
            print(f"[cache] Using cached result ({int(age)}s old)", file=sys.stderr)
            return cached

    t0 = time.time()
    print(f"[1/4] Searching GitHub releases on {branch} (count={count})...", file=sys.stderr, flush=True)
    raw_releases = github_search_releases(gh_token, owner, repo, branch, count)
    if not raw_releases:
        raise DataError(f"No release PRs found on {branch}")

    print(f"[2/4] Parsing {len(raw_releases)} release PR(s)...", file=sys.stderr, flush=True)

    rows = []
    epic_keys_needed = []  # (row_index, epic_key) — EPICs to fetch from Jira
    jira_search_tasks = [] # (row_index, version_string) — fallback search

    is_non_master = branch != "master"

    for rel in raw_releases:
        title = rel["title"]
        body = rel["body"]
        gh_date = rel["merged_at"]
        pr_num = rel["number"]

        if title.startswith("PC:") or title.startswith("PC :"):
            cleaned = re.sub(r"^PC\s*:\s*", "", title, flags=re.IGNORECASE)
            aos_heading, pc_heading = None, cleaned
        else:
            aos_heading, pc_heading = split_title(title)

        epics = extract_epics_from_body(body)

        components = []
        if aos_heading and pc_heading:
            if filter_type in ("all", "aos"):
                components.append(("AOS", aos_heading, epics[0] if epics else None))
            if filter_type in ("all", "pc"):
                epic_idx = 1 if len(epics) > 1 else 0
                components.append(("PC", pc_heading, epics[epic_idx] if epic_idx < len(epics) else None))
        elif pc_heading:
            if filter_type in ("all", "pc"):
                components.append(("PC", pc_heading, epics[0] if epics else None))
        elif aos_heading:
            # Detect PC-only releases by version string (e.g. "sts-ganges-pc.7.3-...")
            is_pc_version = bool(re.search(r"ganges-pc\.", aos_heading, re.IGNORECASE))
            if is_pc_version:
                if filter_type in ("all", "pc"):
                    components.append(("PC", aos_heading, epics[0] if epics else None))
            else:
                if filter_type in ("all", "aos"):
                    components.append(("AOS", aos_heading, epics[0] if epics else None))
                if filter_type == "pc" and is_non_master:
                    components.append(("PC", aos_heading, epics[0] if epics else None))

        for comp_type, heading, epic in components:
            version = extract_version(heading)
            rhel_info = parse_rhel(version)
            gi_version = build_goldimage_version(version, rhel_info)
            changelog, rpm = build_endor_urls(rhel_info)
            tag = f" ({comp_type})" if (aos_heading and pc_heading) else ""

            row = {
                "goldimage_version": gi_version + tag,
                "main_ticket": epic or None,
                "changelog": changelog,
                "rpm": rpm,
                "gh_merge_date": gh_date,
                "sg_merge_date": None,
                "sg_merged": None,
                "pr_number": pr_num,
                "heading": heading,
                "version": version,
                "release_num": _extract_release_num(version),
                "comp_type": comp_type,
                "fix_versions": [],
                "gerrit_ref": None,
            }
            idx = len(rows)
            rows.append(row)

            if epic and jira_url and jira_token:
                epic_keys_needed.append((idx, epic))
            elif not epic and jira_url and jira_token and version:
                jira_search_tasks.append((idx, version))

    # ---- Step 3a: Fetch EPIC fix versions from Jira (non-master needs them
    #      for Gerrit branch resolution; master just fills main_ticket) ----
    epic_cache = {}  # epic_key → {"key": ..., "fix_versions": [...]}

    if epic_keys_needed or jira_search_tasks:
        unique_epics = {ek for _, ek in epic_keys_needed}
        total_jira = len(unique_epics) + len(jira_search_tasks)
        print(f"[3/5] Fetching {total_jira} Jira EPIC(s) for fix versions...",
              file=sys.stderr, flush=True)

        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            epic_futures = {}
            for ek in unique_epics:
                fut = pool.submit(jira_get_epic, jira_url, jira_token, ek)
                epic_futures[fut] = ek
            search_futures = {}
            for idx, version in jira_search_tasks:
                fut = pool.submit(jira_epic_search, jira_url, jira_token, version)
                search_futures[fut] = idx

            for fut in as_completed(list(epic_futures) + list(search_futures)):
                if fut in epic_futures:
                    ek = epic_futures[fut]
                    try:
                        result = fut.result()
                        if result:
                            epic_cache[ek] = result
                    except Exception as exc:
                        print(f"  Warning: Jira fetch for {ek} failed: {exc}",
                              file=sys.stderr)
                elif fut in search_futures:
                    idx = search_futures[fut]
                    try:
                        result = fut.result()
                        if result:
                            rows[idx]["main_ticket"] = result["key"]
                            rows[idx]["fix_versions"] = result.get("fix_versions", [])
                    except Exception as exc:
                        print(f"  Warning: Jira search failed: {exc}",
                              file=sys.stderr)

        for idx, ek in epic_keys_needed:
            info = epic_cache.get(ek)
            if info:
                rows[idx]["fix_versions"] = info.get("fix_versions", [])
    else:
        print(f"[3/5] No Jira lookups needed.", file=sys.stderr, flush=True)

    # ---- Step 3b: Resolve Gerrit branches from fix versions ----
    if is_non_master:
        gerrit_refs = {}  # comp_type → latest Gerrit branch
        for row in rows:
            fv = row.get("fix_versions", [])
            ctype = row.get("comp_type")
            ref = resolve_gerrit_ref_from_fix_versions(fv, ctype)
            if ref:
                row["gerrit_ref"] = ref
                prev = gerrit_refs.get(ctype)
                if not prev or _gerrit_ref_version_key(ref) > _gerrit_ref_version_key(prev):
                    gerrit_refs[ctype] = ref
    else:
        gerrit_refs = {}

    # ---- Step 3c: Git tracker fallback for rows without gerrit_ref ----
    if is_non_master and jira_url and jira_token:
        tracker_tasks = {}  # epic_key → set of row indices
        for idx, row in enumerate(rows):
            if row.get("gerrit_ref"):
                continue
            ek = row.get("main_ticket")
            if ek and ek != "—":
                tracker_tasks.setdefault(ek, set()).add(idx)

        if tracker_tasks:
            print(f"[3c/5] Git tracker fallback for {len(tracker_tasks)} EPIC(s)...",
                  file=sys.stderr, flush=True)
            tracker_cache = {}
            with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
                futures = {
                    pool.submit(jira_epic_git_tracker, jira_url, jira_token, ek): ek
                    for ek in tracker_tasks
                }
                for fut in as_completed(futures):
                    ek = futures[fut]
                    try:
                        result = fut.result()
                        if result:
                            tracker_cache[ek] = result
                    except Exception as exc:
                        print(f"  Warning: git tracker for {ek} failed: {exc}",
                              file=sys.stderr)

            for ek, row_indices in tracker_tasks.items():
                info = tracker_cache.get(ek)
                if not info:
                    continue
                branch = info["branch"]
                for idx in row_indices:
                    rows[idx]["gerrit_ref"] = branch
                    if info.get("commit_date"):
                        rows[idx]["sg_merge_date"] = info["commit_date"]
                        rows[idx]["sg_merged"] = True
                    # Update gerrit_refs for batch Sourcegraph lookup
                    ctype = rows[idx]["comp_type"]
                    prev = gerrit_refs.get(ctype)
                    if not prev or _gerrit_ref_version_key(branch) > _gerrit_ref_version_key(prev):
                        gerrit_refs[ctype] = branch

    # ---- Step 4: Sourcegraph lookup ----
    if sg_token and is_non_master and gerrit_refs:
        sg_lookups = {}  # comp_type → {release_num → (merged, date)}
        print(f"[4/5] Batch Sourcegraph lookup on {len(gerrit_refs)} Gerrit branch(es)...",
              file=sys.stderr, flush=True)

        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            sg_futures = {}
            for ctype, ref in gerrit_refs.items():
                fut = pool.submit(sg_batch_lookup, sg_token, sg_url, ref, ctype, 50)
                sg_futures[fut] = ctype

            for fut in as_completed(sg_futures):
                ctype = sg_futures[fut]
                try:
                    sg_lookups[ctype] = fut.result()
                except Exception as exc:
                    print(f"  Warning: SG batch lookup failed for {ctype}: {exc}",
                          file=sys.stderr)

        for row in rows:
            rel_num = row.get("release_num")
            ctype = row.get("comp_type")
            if rel_num and ctype in sg_lookups and row.get("sg_merge_date") is None:
                entry = sg_lookups[ctype].get(rel_num)
                if entry:
                    row["sg_merged"], row["sg_merge_date"] = entry

    elif sg_token:
        sg_tasks = []
        for idx, row in enumerate(rows):
            if row.get("sg_merge_date") is not None:
                continue
            sg_tasks.append((idx, row["version"] or row["heading"]))

        print(f"[4/5] Running {len(sg_tasks)} Sourcegraph lookups (master)...",
              file=sys.stderr, flush=True)

        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            futures = {}
            for idx, heading in sg_tasks:
                fut = pool.submit(sg_check_heading, sg_token, sg_url, heading)
                futures[fut] = idx

            for fut in as_completed(futures):
                idx = futures[fut]
                try:
                    merged, date_str = fut.result()
                    rows[idx]["sg_merged"] = merged
                    rows[idx]["sg_merge_date"] = date_str
                except Exception as exc:
                    print(f"  Warning: SG lookup failed: {exc}", file=sys.stderr)

    else:
        print(f"[4/5] Sourcegraph token not set, skipping.",
              file=sys.stderr, flush=True)

    # Filter out rows where Sourcegraph confirms a revert (sg_merged == False).
    if sg_token:
        rows = [r for r in rows if r["sg_merged"] is not False]

    for r in rows:
        if not r["main_ticket"]:
            r["main_ticket"] = "—"

    rows.sort(key=lambda r: r["sg_merge_date"] or r["gh_merge_date"] or "", reverse=True)

    # Deduplicate by version (keep the one with the latest gh_merge_date)
    seen_versions = set()
    deduped = []
    for r in rows:
        v = r.get("version") or r.get("heading", "")
        if v in seen_versions:
            continue
        seen_versions.add(v)
        deduped.append(r)
    rows = deduped[:count]

    elapsed = time.time() - t0
    print(f"[5/5] Done in {elapsed:.1f}s — {len(rows)} row(s)", file=sys.stderr, flush=True)

    output = {
        "branch": branch,
        "count": count,
        "filter": filter_type,
        "rows": rows,
        "elapsed_s": round(elapsed, 1),
    }

    try:
        with open(cache_path, "w") as f:
            json.dump(output, f)
    except OSError:
        pass

    return output

# ---------------------------------------------------------------------------
# Formatters
# ---------------------------------------------------------------------------

def format_table(data, with_sg, with_gh):
    rows = data["rows"]
    if not rows:
        return "No releases found."

    has_notes = any(r.get("gerrit_ref") for r in rows)

    lines = []
    headers = ["GoldImage Version", "Main Ticket", "Change Log", "RPM List"]
    if with_sg:
        headers.append("Sourcegraph Merge Date")
    if with_gh:
        headers.append("GitHub Merge Date")
    if not with_sg and not with_gh:
        headers.append("Merge Date")
    if has_notes:
        headers.append("Notes")

    lines.append("| " + " | ".join(headers) + " |")
    lines.append("|" + "|".join(["---"] * len(headers)) + "|")

    for r in rows:
        cols = [
            r["goldimage_version"],
            r["main_ticket"],
            r["changelog"],
            r["rpm"],
        ]
        if with_sg:
            cols.append(format_date(r["sg_merge_date"]))
        if with_gh:
            cols.append(format_date(r["gh_merge_date"]))
        if not with_sg and not with_gh:
            cols.append(format_date(r["sg_merge_date"] or r["gh_merge_date"]))
        if has_notes:
            cols.append(r.get("gerrit_ref") or "—")
        lines.append("| " + " | ".join(cols) + " |")

    return "\n".join(lines)


def format_json(data):
    clean_rows = []
    for r in data["rows"]:
        row_out = {
            "goldimage_version": r["goldimage_version"],
            "main_ticket": r["main_ticket"],
            "changelog": r["changelog"],
            "rpm": r["rpm"],
            "sg_merge_date": r["sg_merge_date"],
            "gh_merge_date": r["gh_merge_date"],
            "pr_number": r["pr_number"],
        }
        if r.get("gerrit_ref"):
            row_out["gerrit_ref"] = r["gerrit_ref"]
        clean_rows.append(row_out)
    return json.dumps({"branch": data["branch"], "rows": clean_rows}, indent=2)

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Unified Release Query — fast concurrent pipeline"
    )
    parser.add_argument("--branch", default="master", help="Branch (default: master)")
    parser.add_argument("--count", type=int, default=5, help="Number of releases (default: 5)")
    parser.add_argument("--filter", dest="filter_type", choices=["all", "aos", "pc"],
                        default="all", help="Filter: all, aos, pc (default: all)")
    parser.add_argument("--with-sg-date", action="store_true",
                        help="Show Sourcegraph/Gerrit merge date column")
    parser.add_argument("--with-github-date", action="store_true",
                        help="Show GitHub PR merge date column")
    parser.add_argument("--format", dest="fmt", choices=["table", "json"],
                        default="table", help="Output format (default: table)")
    parser.add_argument("--output", help="Save output to file")
    parser.add_argument("--no-cache", action="store_true", help="Skip cache")
    parser.add_argument("--repo", default=f"{DEFAULT_OWNER}/{DEFAULT_REPO}",
                        help=f"Repository (default: {DEFAULT_OWNER}/{DEFAULT_REPO})")
    args = parser.parse_args()

    if "/" not in args.repo:
        raise ConfigError(f"--repo must be owner/name, got: {args.repo}")
    owner, repo = args.repo.split("/", 1)

    data = run_pipeline(
        args.branch, args.count, owner, repo,
        args.filter_type, args.with_sg_date, args.with_github_date,
        args.no_cache,
    )

    if args.fmt == "json":
        output = format_json(data)
    else:
        output = format_table(data, args.with_sg_date, args.with_github_date)

    print(output)

    if args.output:
        with open(args.output, "w") as f:
            f.write(output + "\n")
        print(f"Saved to {args.output}", file=sys.stderr)


if __name__ == "__main__":
    try:
        main()
    except ToolError as exc:
        print(f"Error [{type(exc).__name__}]: {exc}", file=sys.stderr)
        sys.exit(2 if isinstance(exc, ConfigError) else 1)
