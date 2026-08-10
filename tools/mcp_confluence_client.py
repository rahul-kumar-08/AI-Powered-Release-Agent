#!/usr/bin/env python3
"""
MCP Confluence Client — Uploads release table data to Confluence via
the Atlassian MCP server configured in .cursor/rules/mcp.json.

Features:
  - Dual parent pages: AOS_CONFLUENCE_PAGE_ID / PC_CONFLUENCE_PAGE_ID
  - Smart page routing: recursively discovers descendant pages and matches
    by branch version + RHEL version (e.g. "Master-el9", "Modern STS - 7.6",
    "PC.7.6", "Master-RHEL9.6")
  - Deduplication: reads existing table rows, skips versions already present
  - Sorted rebuild: all rows sorted by merge date (newest first)
  - Supports both markdown and storage-format output

Usage:
  # Upload from JSON (auto-detect AOS/PC, auto-route to child page)
  python3 tools/mcp_confluence_client.py --input-json /tmp/releases.json --branch master

  # Specify type explicitly
  python3 tools/mcp_confluence_client.py --input-json /tmp/releases.json --branch ganges-7.6 --type PC

  # Dry-run: show what would be uploaded without writing
  python3 tools/mcp_confluence_client.py --input-json /tmp/releases.json --branch master --dry-run

  # Force rebuild: rewrite the entire table even if no new rows
  python3 tools/mcp_confluence_client.py --input-json /tmp/releases.json --branch master --force-rebuild

Environment (from tools/.env):
  AOS_CONFLUENCE_PAGE_ID — Parent page ID for AOS releases
  PC_CONFLUENCE_PAGE_ID  — Parent page ID for PC releases
  CONFLUENCE_PAGE_ID     — Fallback parent page ID (used if type-specific ID not set)
"""

import json
import os
import re
import sys
from datetime import datetime

try:
    from tools.mcp_client import call_tool as _mcp_call_tool, _get_env
    from src.logger import Log
except ImportError:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from mcp_client import call_tool as _mcp_call_tool, _get_env
    from src.logger import Log

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

TOOL_PREFIX = "atlassian__"


def _write_server_key(server_key):
    """Use dedicated write endpoint key for Atlassian server."""
    if server_key == "atlassian":
        return "atlassian-write"
    return f"{server_key}-write"


def _tool_prefix_for_server(server_key):
    """Resolve MCP tool prefix from server key."""
    if server_key == "atlassian-write":
        return "atlassian-write__"
    return TOOL_PREFIX


def _extract_text(result):
    parts = []
    for p in result.get("content", []):
        if p.get("type") == "text":
            parts.append(p.get("text", ""))
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Confluence Operations
# ---------------------------------------------------------------------------

def get_child_pages(server_key, parent_id):
    result = _mcp_call_tool(server_key, f"{TOOL_PREFIX}confluence_get_page_children", {
        "parent_id": str(parent_id),
        "limit": 50,
        "include_content": False,
    })
    text = _extract_text(result)
    try:
        data = json.loads(text)
        if isinstance(data, list):
            return data
        return data.get("results", data.get("children", []))
    except (json.JSONDecodeError, AttributeError):
        pass
    pages = []
    for m in re.finditer(r'"id"\s*:\s*"?(\d+)"?.*?"title"\s*:\s*"([^"]+)"', text):
        pages.append({"id": m.group(1), "title": m.group(2)})
    return pages


def get_page_content(server_key, page_id):
    result = _mcp_call_tool(server_key, f"{TOOL_PREFIX}confluence_get_page", {
        "page_id": str(page_id),
        "convert_to_markdown": True,
    })
    text = _extract_text(result)
    try:
        data = json.loads(text)
        meta = data.get("metadata", data)
        content = meta.get("content", {})
        if isinstance(content, dict):
            return content.get("value", "")
        return str(content)
    except (json.JSONDecodeError, AttributeError):
        pass
    # PII filters may break JSON; extract content.value via regex
    m = re.search(r'"value"\s*:\s*"(.*?)"\s*[,}]', text, re.DOTALL)
    if m:
        raw = m.group(1)
        return raw.replace("\\n", "\n").replace('\\"', '"').replace("\\\\", "\\")
    return text


def create_page(server_key, space_key, title, content, parent_id):
    write_server_key = _write_server_key(server_key)
    write_tool_prefix = _tool_prefix_for_server(write_server_key)
    payload = {
        "space_key": space_key,
        "title": title,
        "content": content,
        "parent_id": str(parent_id),
        "content_format": "markdown",
    }
    result = _mcp_call_tool(write_server_key, f"{write_tool_prefix}confluence_create_page", payload)
    text = _extract_text(result)
    m = re.search(r'"id"\s*:\s*"?(\d+)"?', text)
    page_id = m.group(1) if m else None
    Log.info(f"Created page: {title} (id={page_id})")
    return page_id


def update_page(server_key, page_id, title, content, version_comment=""):
    write_server_key = _write_server_key(server_key)
    write_tool_prefix = _tool_prefix_for_server(write_server_key)
    payload = {
        "page_id": str(page_id),
        "title": title,
        "content": content,
        "content_format": "markdown",
        "is_minor_edit": False,
        "version_comment": version_comment or "Release table update",
    }
    result = _mcp_call_tool(write_server_key, f"{write_tool_prefix}confluence_update_page", payload)
    return _extract_text(result)


def update_page_storage(server_key, page_id, title, content, version_comment=""):
    write_server_key = _write_server_key(server_key)
    write_tool_prefix = _tool_prefix_for_server(write_server_key)
    payload = {
        "page_id": str(page_id),
        "title": title,
        "content": content,
        "content_format": "storage",
        "is_minor_edit": False,
        "version_comment": version_comment or "Release table update",
    }
    result = _mcp_call_tool(write_server_key, f"{write_tool_prefix}confluence_update_page", payload)
    return _extract_text(result)


# ---------------------------------------------------------------------------
# Page Routing
# ---------------------------------------------------------------------------

def get_space_key(server_key, page_id):
    result = _mcp_call_tool(server_key, f"{TOOL_PREFIX}confluence_get_page", {
        "page_id": str(page_id),
        "convert_to_markdown": True,
    })
    text = _extract_text(result)
    try:
        data = json.loads(text)
        meta = data.get("metadata", data)
        space = meta.get("space", {})
        key = space.get("key", "")
        if key:
            return key
    except (json.JSONDecodeError, AttributeError):
        pass
    m = re.search(r'"key"\s*:\s*"([^"]+)"', text)
    return m.group(1) if m else ""


def _collect_all_pages(server_key, parent_id, depth=0, max_depth=3):
    """Recursively collect all descendant pages up to *max_depth* levels."""
    if depth >= max_depth:
        return []
    children = get_child_pages(server_key, parent_id)
    all_pages = []
    for child in children:
        child_id = str(child.get("id", ""))
        all_pages.append(child)
        if child_id:
            all_pages.extend(
                _collect_all_pages(server_key, child_id, depth + 1, max_depth))
    return all_pages


def _extract_branch_ver(branch):
    """'ganges-7.6' → '7.6',  'master' → None."""
    m = re.match(r"ganges-([\d.]+)", branch)
    return m.group(1) if m else None


def _extract_rhel_major(version_str):
    """'main-master-rhel9.7-10.0.0' → '9'."""
    m = re.search(r"rhel(\d+)", version_str)
    return m.group(1) if m else None


def find_target_page(server_key, parent_id, branch, release_type,
                     rows=None, space_key=None):
    """Find the correct child/descendant page for a set of release rows.

    Page hierarchy follows the existing Confluence structure:
      AOS: Master-el9, Modern STS - 7.6, STS/Modern STS - 7.5, …
      PC:  EL9/Master-RHEL9.6, EL8/PC.7.5, PC.7.6, …

    Matches pages by branch name and RHEL version extracted from the
    goldimage version string.  Falls back to creating a new page under
    the most appropriate parent if no match is found.
    """
    if not space_key:
        space_key = get_space_key(server_key, parent_id)
        Log.info(f"Resolved space key: {space_key}")

    all_pages = _collect_all_pages(server_key, parent_id)
    Log.info(f"Discovered {len(all_pages)} pages under parent {parent_id}")

    branch_ver = _extract_branch_ver(branch)
    rhel_ver = None
    if rows:
        for row in rows:
            rhel_ver = _extract_rhel_major(
                row.get("goldimage_version", row.get("ver", "")))
            if rhel_ver:
                break

    rtype = release_type.upper()

    # Build ordered list of candidate title patterns (exact match first).
    patterns = _build_title_patterns(rtype, branch, branch_ver, rhel_ver)
    Log.info(f"Matching patterns: {patterns}")

    # Exact (case-insensitive) match
    for pattern in patterns:
        for page in all_pages:
            if page.get("title", "").lower() == pattern.lower():
                pid = str(page.get("id", ""))
                Log.info(f"Matched page: '{page['title']}' (id={pid})")
                return pid, page["title"]

    # Minimal regex fallback for PC/master naming drift:
    # Match forms like:
    #   PC-Master-EL9, PC_Master_EL9, PC Master EL9.8, PC Master-RHEL9
    if rtype == "PC" and branch == "master" and rhel_ver:
        pc_master_re = re.compile(
            rf"^pc[-_ ]*master(?:[-_ ]*(?:el|rhel)\s*{re.escape(rhel_ver)}(?:\.\d+)?)?$",
            re.IGNORECASE,
        )
        for page in all_pages:
            title = page.get("title", "").strip()
            if pc_master_re.match(title):
                pid = str(page.get("id", ""))
                Log.info(f"Regex-matched page: '{page['title']}' (id={pid})")
                return pid, page["title"]

    # Fuzzy: branch version appears in title.
    # For PC, keep matching strict to PC-prefixed page names to avoid
    # accidentally routing to AOS-style pages like "Modern STS - x.y".
    if branch_ver:
        for page in all_pages:
            title_lower = page.get("title", "").lower()
            if branch_ver not in title_lower:
                continue
            if rtype == "PC":
                if not title_lower.strip().startswith("pc"):
                    continue
            if branch_ver in title_lower:
                pid = str(page.get("id", ""))
                Log.info(f"Fuzzy-matched page: '{page['title']}' (id={pid})")
                return pid, page["title"]

    # No match — create under the best parent
    new_title = _new_page_title(rtype, branch, branch_ver, rhel_ver)
    create_parent = _best_create_parent(
        rtype, branch, rhel_ver, parent_id, all_pages)
    Log.info(f"No match found, creating '{new_title}' under {create_parent}")
    pid = create_page(server_key, space_key, new_title,
                      f"# {new_title}\n\n*(table pending)*", create_parent)
    return pid, new_title


def _build_title_patterns(rtype, branch, branch_ver, rhel_ver):
    """Return an ordered list of candidate page titles to match against."""
    patterns = []
    if rtype == "AOS":
        if branch == "master":
            if rhel_ver:
                patterns.append(f"Master-el{rhel_ver}")
            patterns.extend(["Master-el9", "Master-el8", "Master"])
        elif branch_ver:
            patterns.append(f"Modern STS - {branch_ver}")
    else:  # PC
        if branch == "master":
            # Keep exact checks intentionally minimal; regex fallback in
            # find_target_page handles common PC master naming variations.
            if rhel_ver:
                patterns.extend([
                    f"PC-Master-EL{rhel_ver}",
                    f"PC Master-RHEL{rhel_ver}",
                    f"PC-Master",
                    "PC Master",
                ])
            else:
                patterns.extend([
                    "PC-Master-EL9",
                    "PC Master-RHEL9",
                    "PC-Master",
                    "PC Master",
                ])
        elif branch_ver:
            patterns.append(f"PC.{branch_ver}")
            patterns.append(f"pc.{branch_ver}")
            # Backward-compatible aliases for minor page naming differences.
            patterns.append(f"PC {branch_ver}")
            patterns.append(f"PC-{branch_ver}")
            patterns.append(f"PC - {branch_ver}")
            patterns.append(f"pc {branch_ver}")
            patterns.append(f"pc-{branch_ver}")
            patterns.append(f"pc - {branch_ver}")
    return patterns


def _new_page_title(rtype, branch, branch_ver, rhel_ver):
    if rtype == "AOS":
        if branch == "master":
            return f"Master-el{rhel_ver or '9'}"
        return f"Modern STS - {branch_ver}" if branch_ver else f"AOS {branch}"
    else:
        if branch == "master":
            return f"PC-Master-EL{rhel_ver or '9'}"
        return f"PC.{branch_ver}" if branch_ver else f"PC {branch}"


def _best_create_parent(rtype, branch, rhel_ver, root_parent, all_pages):
    """Choose the best parent page for a newly created child."""
    if rtype == "AOS":
        if branch == "master":
            for p in all_pages:
                if p.get("title", "").lower() == "master":
                    return str(p["id"])
        else:
            for p in all_pages:
                if p.get("title", "").lower() == "sts":
                    return str(p["id"])
    else:  # PC
        if rhel_ver:
            for p in all_pages:
                if p.get("title", "").lower() == f"el{rhel_ver}":
                    return str(p["id"])
        for p in all_pages:
            if p.get("title", "").lower() in ("el9", "el8"):
                return str(p["id"])
    return root_parent


# ---------------------------------------------------------------------------
# Table Parsing & Building
# ---------------------------------------------------------------------------

TABLE_COLUMNS = [
    "GoldImage Version", "Main Ticket", "Change Log", "RPM List", "Merge Date", "Notes"
]

TABLE_COLUMNS_WITH_TARBALL = [
    "GoldImage Version", "Main Ticket", "Change Log", "RPM List",
    "GI tarball", "Merge Date", "Notes"
]

PC_TARBALL_BRANCHES = {"ganges-7.3", "ganges-7.5"}


def _needs_gi_tarball(branch, release_type):
    """GI tarball column only applies to PC on ganges-7.3 and ganges-7.5."""
    return branch in PC_TARBALL_BRANCHES and release_type.upper() == "PC"


def _normalize_version(ver):
    """Normalize a version string for dedup comparison.

    Strips ``(AOS)``/``(PC)`` suffixes, markdown backticks, extra
    whitespace, and lowercases so that variations like
    ````` main-master-rhel9.7-10.0.0 ````` and
    ``main-master-rhel9.7-10.0.0 (AOS)`` all match.
    """
    if not ver:
        return ""
    ver = re.sub(r"\s*\((AOS|PC)\)\s*$", "", ver, flags=re.IGNORECASE)
    ver = ver.replace("`", "")
    return ver.strip().lower()


def parse_date(date_str):
    if not date_str or date_str in ("N/A", "--", ""):
        return datetime.min
    for fmt in ("%d-%b-%Y", "%Y-%m-%d", "%d-%B-%Y"):
        try:
            return datetime.strptime(date_str.strip(), fmt)
        except ValueError:
            continue
    return datetime.min


def _detect_date_column(rows, sample_size=5):
    """Scan rows to find the column index that contains date values."""
    for col_idx in range(max(len(r) for r in rows[:sample_size])):
        hits = 0
        for row in rows[:sample_size]:
            if col_idx < len(row):
                if parse_date(row[col_idx].strip()) != datetime.min:
                    hits += 1
        if hits >= min(2, len(rows[:sample_size])):
            return col_idx
    return None


_VERSION_PATTERN = re.compile(r"(?:main|sts)-\S+-rhel\d+\.\d+-\S+")


def _extract_version_from_row(row):
    """Extract version string from a row, checking each cell for the pattern."""
    # First try index 0 directly (standard format)
    if row and row[0].strip():
        cell0 = row[0].strip()
        m = _VERSION_PATTERN.search(cell0)
        if m:
            return m.group(0)
        if cell0.startswith(("main-", "sts-")):
            return cell0
    # Scan all cells for the version pattern
    for cell in row:
        m = _VERSION_PATTERN.search(cell)
        if m:
            return m.group(0)
    return None


def _clean_ticket_cell(cell):
    """Extract Jira ticket key from a cell that may contain garbled macro text.

    The MCP gateway renders the Jira Issue macro as rendered text like:
    ``Jiraissuekey,summary,...,resolution<UUID>ENG-923957``
    This extracts just the ticket key.
    """
    cell = cell.strip()
    if re.match(r'^[A-Z]+-\d+$', cell) or cell == "--":
        return cell
    m = re.search(r'([A-Z]+-\d{4,})', cell)
    if m:
        return m.group(1)
    return cell


def _clean_url_cell(cell):
    """Strip markdown link syntax and escaped characters from URL cells.

    MCP returns URLs as ``<https://...>`` or with escaped underscores
    ``PC\\_GoldImages``.
    """
    cell = cell.strip()
    m = re.match(r'^<(.+)>$', cell)
    if m:
        cell = m.group(1)
    cell = cell.replace("\\_", "_")
    return cell


def _clean_cell_backticks(cell):
    """Strip markdown backtick wrapping from a cell value."""
    cell = cell.strip()
    if cell.startswith("`") and cell.endswith("`"):
        cell = cell.strip("`").strip()
    return cell


def _extract_existing_columns(page_content):
    """Extract table header columns from markdown or storage-format content."""
    # Markdown table header
    for line in page_content.split("\n"):
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.split("|")[1:-1]]
        if len(cells) >= 5 and any("goldimage" in c.lower() for c in cells):
            return cells

    # XHTML storage format header
    m = re.search(r"<thead[^>]*>(.*?)</thead>", page_content, re.DOTALL | re.IGNORECASE)
    if m:
        th_values = re.findall(r"<th[^>]*>(.*?)</th>", m.group(1), re.DOTALL | re.IGNORECASE)
        cols = [_strip_html(v).strip() for v in th_values]
        if len(cols) >= 5 and any("goldimage" in c.lower() for c in cols):
            return cols

    return None


def _header_key(name):
    return re.sub(r"[^a-z0-9]+", "", (name or "").lower())


def _extract_fix_version_from_jira_text(text):
    """Extract first fixVersion name from Jira response text."""
    try:
        data = json.loads(text)
        issues = data.get("issues", []) if isinstance(data, dict) else []
        if issues:
            issue = issues[0]
            fields = issue.get("fields", {}) if isinstance(issue, dict) else {}
            fix_versions = (
                fields.get("fixVersions", [])
                or fields.get("fix_versions", [])
                or issue.get("fixVersions", [])
                or issue.get("fix_versions", [])
            )
            for fv in fix_versions:
                if isinstance(fv, dict) and fv.get("name"):
                    return str(fv["name"]).strip()
                if isinstance(fv, str) and fv.strip():
                    return fv.strip()
    except Exception:
        pass

    m = re.search(r'"fixVersions"\s*:\s*\[(.*?)\]', text, re.DOTALL)
    if not m:
        m = re.search(r'"fix_versions"\s*:\s*\[(.*?)\]', text, re.DOTALL)
        if not m:
            return ""
    block = m.group(1)
    n = re.search(r'"name"\s*:\s*"([^"]+)"', block)
    if n:
        return n.group(1).strip()
    # Fallback for list-of-strings format: "fix_versions": ["pc.x.x.x.12","pc.7.5.2"]
    str_vals = re.findall(r'"([^"]+)"', block)
    return str_vals[0].strip() if str_vals else ""


def _extract_branch_version_from_row(row):
    """Extract x.y branch version from notes/version fields."""
    notes = str(row.get("notes", "")).strip()
    m = re.search(r"ganges-([\d.]+)", notes)
    if m:
        return m.group(1)
    ver = str(row.get("goldimage_version", row.get("ver", ""))).strip()
    m = re.search(r"ganges-(?:pc\.)?([\d.]+)-", ver)
    return m.group(1) if m else ""


def _extract_target_release_from_row(row):
    """Extract Target Release value from Sourcegraph/Gerrit commit message."""
    msg = str(row.get("commit_message", "") or "")
    if not msg:
        return ""
    m = re.search(r"Target\s+Release\s*:\s*([^\n\r]+)", msg, re.IGNORECASE)
    if not m:
        return ""
    return m.group(1).strip()


def _extract_jira_branch_equiv_from_row(row):
    """Extract Jira git-tracker 'JIRA Version (branch equiv)' value."""
    return str(row.get("jira_branch_equiv", "") or "").strip()


def _pick_best_fix_version(fix_versions, branch_ver=""):
    """Pick concrete fix version; avoid wildcard placeholders."""
    vals = [str(v).strip() for v in (fix_versions or []) if str(v).strip()]
    if not vals:
        return ""

    def _is_wildcard(v):
        s = (v or "").strip().lower()
        if not s:
            return True
        # Reject wildcard/placeholder style versions like pc.x.x.x.12
        return bool(re.search(r"(^|[.\-_])x($|[.\-_])", s))

    # Prefer versions matching branch (e.g. 7.5) and without wildcard 'x'.
    if branch_ver:
        branch_exact = [v for v in vals if branch_ver in v and not _is_wildcard(v)]
        if branch_exact:
            return branch_exact[0]
        branch_any = [v for v in vals if branch_ver in v and not _is_wildcard(v)]
        if branch_any:
            return branch_any[0]

    non_wild = [v for v in vals if not _is_wildcard(v)]
    if non_wild:
        return non_wild[0]
    # If Jira has only wildcard-style Fix Version/s, keep Jira value.
    return vals[0]


def _fetch_epic_fix_version(server_key, epic_key, cache, branch_ver=""):
    """Fetch EPIC fixVersion from Jira via MCP, with per-run caching."""
    epic_key = _normalize_ticket_key(epic_key)
    if epic_key == "--":
        return ""
    cache_key = f"{epic_key}|{branch_ver or '-'}"
    if cache_key in cache:
        return cache[cache_key]

    read_server_key = "atlassian" if "atlassian" in (server_key or "") else server_key
    read_prefix = _tool_prefix_for_server(read_server_key)
    fix_ver = ""
    try:
        result = _mcp_call_tool(read_server_key, f"{read_prefix}jira_search", {
            "jql": f'key = "{epic_key}"',
            "limit": 1,
            "fields": "fixVersions,fix_versions,status,summary,key",
        })
        text = _extract_text(result)
        # Prefer structured extraction so we can rank candidates.
        candidates = []
        try:
            data = json.loads(text)
            issues = data.get("issues", []) if isinstance(data, dict) else []
            if issues:
                issue = issues[0]
                fields = issue.get("fields", {}) if isinstance(issue, dict) else {}
                candidates = (
                    fields.get("fixVersions", [])
                    or fields.get("fix_versions", [])
                    or issue.get("fixVersions", [])
                    or issue.get("fix_versions", [])
                    or []
                )
                # normalize dict entries to names
                norm = []
                for fv in candidates:
                    if isinstance(fv, dict) and fv.get("name"):
                        norm.append(str(fv["name"]).strip())
                    elif isinstance(fv, str):
                        norm.append(fv.strip())
                candidates = [v for v in norm if v]
        except Exception:
            candidates = []

        if candidates:
            fix_ver = _pick_best_fix_version(candidates, branch_ver=branch_ver)
        else:
            raw = _extract_fix_version_from_jira_text(text)
            fix_ver = _pick_best_fix_version([raw], branch_ver=branch_ver)
    except Exception:
        fix_ver = ""

    cache[cache_key] = fix_ver
    return fix_ver


def _normalize_ticket_key(ticket_val):
    """Return Jira key in canonical form (e.g. ENG-123456) or '--'."""
    if not ticket_val:
        return "--"
    raw = str(ticket_val).strip()
    if raw == "--":
        return "--"
    m = re.search(r"([A-Z]+-\d+)", raw)
    return m.group(1) if m else "--"


def _release_column_kind(header_name):
    """Classify a release-like column as pc/aos/generic/none."""
    key = _header_key(header_name)
    if not key:
        return "none"
    if ("goldimage" in key) or ("ticket" in key) or ("note" in key):
        return "none"

    pc_keys = ("pcreleases", "pcrelease", "pcreleaseversion")
    aos_keys = ("aosreleases", "aosrelease", "aosreleaseversion")
    if key in pc_keys:
        return "pc"
    if key in aos_keys:
        return "aos"
    if key in ("release", "releases", "releaseversion") or key.endswith("releases"):
        return "generic"
    return "none"


def _row_to_cells_by_columns(row, columns, release_values=None):
    """Map row fields into the existing table's column order."""
    ver = row.get("goldimage_version", row.get("ver", ""))
    ticket = _normalize_ticket_key(row.get("main_ticket", row.get("ticket", "--")))
    cl_url = row.get("changelog_url", row.get("cl", ""))
    rpm_url = row.get("rpm_url", row.get("rpm", ""))
    tarball_url = row.get("gi_tarball_url", row.get("gi_tarball", ""))
    merge_date = row.get("merge_date", row.get("date", "N/A"))
    notes = row.get("notes", "")

    cl_cell = cl_url if cl_url and "not found" not in cl_url.lower() else "Data not found"
    rpm_cell = rpm_url if rpm_url and "not found" not in rpm_url.lower() else "Data not found"
    tarball_cell = (tarball_url if tarball_url and "not found" not in tarball_url.lower()
                    else "Data not found")

    release_values = release_values or {}
    pc_release_value = str(release_values.get("pc", "") or "").strip()
    aos_release_value = str(release_values.get("aos", "") or "").strip()
    generic_release_value = str(release_values.get("generic", "") or "").strip()

    cells = []
    for col in columns:
        key = _header_key(col)
        if key in ("goldimageversion", "goldimage", "version"):
            cells.append(ver)
        elif (key in ("mainticket", "ticket", "mainjira", "mainjiraticket", "mainjiraepic")
              or ("main" in key and "ticket" in key)
              or ("main" in key and "jira" in key)):
            cells.append(ticket)
        elif key in ("changelog", "changelogurl", "changeloglink"):
            cells.append(cl_cell)
        elif key in ("rpmlist", "rpm", "rpmurl", "rpmlink"):
            cells.append(rpm_cell)
        elif key in ("gitarball", "tarball", "pcvmtarball", "gitarballurl"):
            cells.append(tarball_cell)
        elif _release_column_kind(col) != "none":
            col_kind = _release_column_kind(col)
            if col_kind == "pc":
                cells.append(pc_release_value or generic_release_value or "")
            elif col_kind == "aos":
                cells.append(aos_release_value or generic_release_value or "")
            else:
                cells.append(generic_release_value or pc_release_value or aos_release_value or "")
        elif key in ("mergedate", "date"):
            cells.append(merge_date)
        elif key in ("status", "state", "mergestatus"):
            status_value = str(row.get("status", row.get("release_status", "Merged"))).strip()
            cells.append(status_value or "Merged")
        elif key in ("notes",):
            cells.append(notes)
        else:
            cells.append("")
    return cells


def _date_col_from_columns(columns):
    for idx, col in enumerate(columns):
        key = _header_key(col)
        if key in ("mergedate", "date"):
            return idx
    return None


def _version_col_from_columns(columns):
    for idx, col in enumerate(columns):
        key = _header_key(col)
        if key in ("goldimageversion", "goldimage", "version"):
            return idx
    return 0


def _url_cols_from_columns(columns):
    url_cols = set()
    for idx, col in enumerate(columns):
        key = _header_key(col)
        if key in ("changelog", "changelogurl", "changeloglink",
                   "rpmlist", "rpm", "rpmurl", "rpmlink",
                   "gitarball", "tarball", "pcvmtarball", "gitarballurl"):
            url_cols.add(idx)
    return url_cols


def _extract_existing_rows_by_columns(page_content, columns):
    """Extract existing table rows preserving all existing cell values."""
    rows = []

    # Markdown table rows
    for line in page_content.split("\n"):
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.split("|")[1:-1]]
        if len(cells) < max(5, len(columns)):
            continue
        # Skip delimiter / header rows
        if all(re.match(r"^[:\-]+$", c or "-") for c in cells):
            continue
        if any("goldimage" in c.lower() for c in cells):
            continue
        rows.append(cells[:len(columns)])

    if rows:
        return rows

    # Storage-format rows
    tr_blocks = re.findall(r"<tr[^>]*>(.*?)</tr>", page_content, re.DOTALL | re.IGNORECASE)
    for tr in tr_blocks:
        if "<th" in tr.lower():
            continue
        td_values = re.findall(r"<td[^>]*>(.*?)</td>", tr, re.DOTALL | re.IGNORECASE)
        if len(td_values) < max(5, len(columns)):
            continue
        cells = [_strip_html(v).strip() for v in td_values[:len(columns)]]
        rows.append(cells)

    return rows


def _ticket_cols_from_columns(columns):
    ticket_cols = set()
    for idx, col in enumerate(columns):
        key = _header_key(col)
        if (key in ("mainticket", "ticket", "mainjira", "mainjiraticket", "mainjiraepic")
                or ("main" in key and "ticket" in key)
                or ("main" in key and "jira" in key)):
            ticket_cols.add(idx)
    return ticket_cols


def _is_blank_release_value(value):
    """Treat empty/placeholder release values as blank."""
    s = str(value or "").strip()
    if s in ("", "--", "N/A", "na", "None"):
        return True
    # Treat wildcard Jira placeholders as replaceable.
    return bool(re.search(r"(^|[.\-_])x($|[.\-_])", s.lower()))


def _is_blank_status_value(value):
    """Treat empty/placeholder status values as blank."""
    s = str(value or "").strip()
    return s in ("", "--", "N/A", "na", "None")


def extract_existing_versions(page_content):
    """Parse table rows from existing page content (markdown or XHTML storage format).

    Returns (set_of_normalized_version_strings, list_of_cell_lists).
    Version strings are normalized for reliable dedup comparison.
    """
    versions = set()
    rows = []

    for line in page_content.split("\n"):
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.split("|")[1:-1]]
        if len(cells) < 5:
            continue
        # Strip backticks that MCP's convert_to_markdown adds around cell text
        cells[0] = _clean_cell_backticks(cells[0])
        # Clean ticket cell: MCP may render Jira macro as garbled text
        # e.g. "Jiraissuekey,...,resolution<UUID>ENG-123456"
        cells[1] = _clean_ticket_cell(cells[1])
        # Clean URL cells: strip markdown link syntax <url> and escapes
        for i in (2, 3, 4):
            if i < len(cells):
                cells[i] = _clean_url_cell(cells[i])
        ver = cells[0].strip()
        if ver and ver != TABLE_COLUMNS[0] and not re.match(r"^[-:]+$", ver):
            versions.add(_normalize_version(ver))
            rows.append(cells)

    if rows:
        return versions, rows

    tr_blocks = re.findall(r"<tr[^>]*>(.*?)</tr>", page_content, re.DOTALL | re.IGNORECASE)
    for tr in tr_blocks:
        if "<th" in tr.lower():
            continue
        td_values = re.findall(r"<td[^>]*>(.*?)</td>", tr, re.DOTALL | re.IGNORECASE)
        if len(td_values) < 5:
            continue
        cells = [_strip_html(v).strip() for v in td_values]
        ver = cells[0]
        if ver and ver != TABLE_COLUMNS[0] and not re.match(r"^[-:]+$", ver):
            versions.add(_normalize_version(ver))
            rows.append(cells)

    return versions, rows


def _strip_html(text):
    """Remove HTML tags and decode common entities, preserving Jira ticket keys."""
    ticket_match = re.search(r'ac:name="key">([A-Z]+-\d+)<', text)
    if ticket_match:
        return ticket_match.group(1)
    href_match = re.search(r'href="([^"]+)"', text)
    if href_match:
        url = href_match.group(1)
        return url.replace("&amp;", "&")
    clean = re.sub(r"<[^>]+>", "", text)
    return clean.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").strip()


def row_to_cells(row, include_tarball=False):
    ver = row.get("goldimage_version", row.get("ver", ""))
    ticket = _normalize_ticket_key(row.get("main_ticket", row.get("ticket", "--")))
    cl_url = row.get("changelog_url", row.get("cl", ""))
    rpm_url = row.get("rpm_url", row.get("rpm", ""))
    merge_date = row.get("merge_date", row.get("date", "N/A"))
    notes = row.get("notes", "")

    cl_cell = cl_url if cl_url and "not found" not in cl_url.lower() else "Data not found"
    rpm_cell = rpm_url if rpm_url and "not found" not in rpm_url.lower() else "Data not found"

    if include_tarball:
        tarball_url = row.get("gi_tarball_url", row.get("gi_tarball", ""))
        tarball_cell = (tarball_url if tarball_url and "not found" not in tarball_url.lower()
                        else "Data not found")
        return [ver, ticket, cl_cell, rpm_cell, tarball_cell, merge_date, notes]

    return [ver, ticket, cl_cell, rpm_cell, merge_date, notes]


JIRA_SERVER_ID = "7a063259-3954-3005-9df8-21c0f279a704"


def _jira_macro(ticket_key):
    """Build Confluence Jira Issue macro in storage format."""
    return (
        f'<ac:structured-macro ac:name="jira">'
        f'<ac:parameter ac:name="serverId">{JIRA_SERVER_ID}</ac:parameter>'
        f'<ac:parameter ac:name="key">{ticket_key}</ac:parameter>'
        f'</ac:structured-macro>'
    )


def _escape_html(text):
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _build_td(content, is_ticket=False, is_url=False):
    """Build a <td> cell. Handles ticket macros and URL hyperlinks."""
    if is_ticket and content and content != "--" and re.match(r"^[A-Z]+-\d+$", content):
        return f"<td>{_jira_macro(content)}</td>"
    if is_url and content and content.startswith("http") and "not found" not in content.lower():
        return f'<td><a href="{_escape_html(content)}">{_escape_html(content)}</a></td>'
    return f"<td>{_escape_html(str(content))}</td>"


def build_table_storage(all_rows_cells, include_tarball=False, columns=None,
                        date_col_idx=None, url_cols=None, ticket_cols=None,
                        sort_rows=True):
    """Build a Confluence storage-format (XHTML) table with Jira Issue macros for tickets."""
    columns = columns or (TABLE_COLUMNS_WITH_TARBALL if include_tarball else TABLE_COLUMNS)
    if date_col_idx is None:
        date_col_idx = 5 if include_tarball else 4
    if url_cols is None:
        url_cols = {2, 3, 4} if include_tarball else {2, 3}
    if ticket_cols is None:
        ticket_cols = {1}

    if sort_rows:
        all_rows_cells.sort(
            key=lambda r: parse_date(r[date_col_idx] if len(r) > date_col_idx else ""),
            reverse=True)

    lines = ['<table>', '<thead>', '<tr>']
    for col in columns:
        lines.append(f"<th>{_escape_html(col)}</th>")
    lines.append("</tr></thead>")
    lines.append("<tbody>")

    for cells in all_rows_cells:
        lines.append("<tr>")
        for i, cell in enumerate(cells):
            is_ticket = (i in ticket_cols)
            is_url = (i in url_cols)
            lines.append(_build_td(cell, is_ticket=is_ticket, is_url=is_url))
        lines.append("</tr>")

    lines.append("</tbody></table>")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Confluence Lookup — Latest Release
# ---------------------------------------------------------------------------

def get_confluence_page_releases(server_key, parent_id, branch, release_type,
                                 space_key=None):
    """Return all existing release rows from the Confluence page for a branch/type.

    Connects to the target page (auto-routed by branch and release type),
    reads the existing table, and returns all rows sorted newest-first.

    Returns:
        {"rows": list of cell-lists, "latest": {"version", "merge_date"},
         "page_id": str, "page_title": str} or None if page is empty/missing.
    """
    release_type = (release_type or "AOS").upper()
    try:
        page_id, page_title = find_target_page(
            server_key, parent_id, branch, release_type,
            rows=None, space_key=space_key)
    except Exception as e:
        Log.error(f"Confluence lookup failed (find_target_page): {e}")
        return None

    if not page_id:
        return None

    try:
        page_content = get_page_content(server_key, page_id)
    except Exception as e:
        Log.error(f"Confluence lookup failed (get_page_content): {e}")
        return None

    existing_versions, existing_rows = extract_existing_versions(page_content)
    if not existing_rows:
        Log.info(f"Confluence page '{page_title}' has no existing rows")
        return None

    # Detect the date column index dynamically by scanning the first few rows
    date_col_idx = _detect_date_column(existing_rows)
    if date_col_idx is None:
        Log.info(f"Confluence page '{page_title}': no rows with valid merge dates")
        return None

    existing_rows.sort(
        key=lambda r: parse_date(r[date_col_idx] if len(r) > date_col_idx else ""),
        reverse=True)

    best_row = existing_rows[0]
    best_date_str = best_row[date_col_idx].strip() if len(best_row) > date_col_idx else "N/A"

    if parse_date(best_date_str) == datetime.min:
        Log.info(f"Confluence page '{page_title}': no rows with valid merge dates")
        return None

    # Detect version: try index 0 first, otherwise scan for version pattern
    version = _extract_version_from_row(best_row)
    if not version:
        Log.info(f"Confluence page '{page_title}': cannot extract version from rows")
        return None

    Log.info(f"Confluence latest for {release_type}/{branch}: "
             f"'{version}' (merged {best_date_str}) on page '{page_title}'")

    return {
        "rows": existing_rows,
        "latest": {"version": version, "merge_date": best_date_str},
        "page_id": page_id,
        "page_title": page_title,
        "_date_col_idx": date_col_idx,
    }


# ---------------------------------------------------------------------------
# Core Upload Logic
# ---------------------------------------------------------------------------

def detect_release_type(rows):
    for row in rows:
        rtype = row.get("type", "").upper()
        if rtype in ("AOS", "PC"):
            return rtype
        ver = row.get("goldimage_version", row.get("ver", ""))
        if "ganges-pc" in ver.lower() or "pc" in row.get("type", "").lower():
            return "PC"
    return "AOS"


def upload_releases(server_key, parent_id, branch, rows, release_type=None,
                    force_rebuild=False, dry_run=False, space_key=None):
    """Upload release rows to Confluence in append mode.

    Workflow:
      1. Find the correct target page by matching branch name and RHEL
         version against the existing page hierarchy under *parent_id*.
      2. Read existing page content and extract table rows.
      3. Deduplicate: skip any new row whose normalized version already exists.
      4. Append new rows to existing rows.
      5. Sort ALL rows by merge date (newest first) and rebuild the table.
      6. Update the page only if new rows were added (or force_rebuild).
    """
    if not rows:
        Log.info("No rows to upload.")
        return {"added": 0, "skipped": 0, "total": 0}

    if not release_type:
        release_type = detect_release_type(rows)
    release_type = release_type.upper()
    Log.info(f"Release type: {release_type}, branch: {branch}, incoming rows: {len(rows)}")

    page_id, page_title = find_target_page(
        server_key, parent_id, branch, release_type,
        rows=rows, space_key=space_key)
    if not page_id:
        raise RuntimeError("Failed to find or create target page")

    page_content = get_page_content(server_key, page_id)
    existing_versions, existing_cells = extract_existing_versions(page_content)
    preserve_existing_layout = branch in PC_TARBALL_BRANCHES
    existing_columns = (_extract_existing_columns(page_content)
                        if preserve_existing_layout else None)
    version_col_idx = 0
    if existing_columns:
        version_col_idx = _version_col_from_columns(existing_columns)
        preserved_rows = _extract_existing_rows_by_columns(page_content, existing_columns)
        if preserved_rows:
            existing_cells = preserved_rows
            existing_versions = set()
            for cells in existing_cells:
                ver_candidates = []
                if version_col_idx < len(cells):
                    ver_candidates.append(cells[version_col_idx].strip())
                inferred = _extract_version_from_row(cells)
                if inferred:
                    ver_candidates.append(inferred)
                for ver in ver_candidates:
                    if ver and not re.match(r"^[-:]+$", ver):
                        existing_versions.add(_normalize_version(ver))
    # Safety guard: never overwrite a populated page when row extraction fails.
    # This avoids replacing historical rows with only the incoming release set.
    has_any_content = bool(page_content and page_content.strip())
    looks_like_existing_table = (
        "GoldImage Version" in page_content
        or "<table" in page_content.lower()
        or re.search(r"^\|.*\|$", page_content, re.MULTILINE) is not None
    )
    is_placeholder_page = "table pending" in page_content.lower()
    if (has_any_content and looks_like_existing_table and not is_placeholder_page
            and not existing_cells and not force_rebuild):
        raise RuntimeError(
            f"Safety stop: unable to parse existing rows on page {page_id}; "
            "aborting update to prevent data loss. Re-run with --force-rebuild "
            "only after validating page parsing."
        )
    Log.info(f"Existing rows on page: {len(existing_cells)} "
         f"({len(existing_versions)} unique versions)")

    include_tarball = _needs_gi_tarball(branch, release_type)
    fix_version_cache = {}
    has_pc_releases_col = False
    has_aos_releases_col = False
    has_generic_releases_col = False
    if existing_columns:
        has_pc_releases_col = any(_release_column_kind(c) == "pc" for c in existing_columns)
        has_aos_releases_col = any(_release_column_kind(c) == "aos" for c in existing_columns)
        has_generic_releases_col = any(
            _release_column_kind(c) == "generic" for c in existing_columns
        )

    new_cells = []
    skipped = 0
    backfilled = 0
    version_to_row_idx = {}
    if existing_columns and existing_cells:
        for idx, row_cells in enumerate(existing_cells):
            ver_candidates = []
            if version_col_idx < len(row_cells):
                ver_candidates.append(row_cells[version_col_idx].strip())
            inferred = _extract_version_from_row(row_cells)
            if inferred:
                ver_candidates.append(inferred)
            for ver in ver_candidates:
                nver = _normalize_version(ver)
                if nver and nver not in version_to_row_idx:
                    version_to_row_idx[nver] = idx

    release_col_indices = []
    status_col_indices = []
    if existing_columns:
        for i, col in enumerate(existing_columns):
            kind = _release_column_kind(col)
            if kind != "none":
                release_col_indices.append((i, kind))
            if _header_key(col) in ("status", "state", "mergestatus"):
                status_col_indices.append(i)

    for row in rows:
        if existing_columns:
            row_type = str(row.get("type", "")).upper()
            pc_release_value = ""
            aos_release_value = str(
                row.get("aos_release", row.get("AOS_release", ""))
            ).strip()

            # Populate PC release value for both explicit PC columns and
            # generic "Release" columns when handling PC rows.
            if row_type == "PC" and (has_pc_releases_col or has_generic_releases_col):
                pc_release_value = str(
                    row.get("pc_release", row.get("PC_release", "")) or ""
                ).strip()
                if not pc_release_value:
                    pc_release_value = _extract_jira_branch_equiv_from_row(row)
                if not pc_release_value:
                    pc_release_value = _extract_target_release_from_row(row)
                if not pc_release_value:
                    branch_ver = _extract_branch_version_from_row(row)
                    pc_release_value = _fetch_epic_fix_version(
                        server_key,
                        row.get("main_ticket", "--"),
                        fix_version_cache,
                        branch_ver=branch_ver,
                    )
            if not aos_release_value and row_type == "AOS":
                # AOS rows can safely default to their own release version.
                aos_release_value = str(
                    row.get("goldimage_version", row.get("ver", ""))
                ).strip()

            generic_release_value = ""
            if has_generic_releases_col:
                if row_type == "PC":
                    generic_release_value = pc_release_value
                elif row_type == "AOS":
                    generic_release_value = aos_release_value

            cells = _row_to_cells_by_columns(
                row,
                existing_columns,
                release_values={
                    "pc": pc_release_value,
                    "aos": aos_release_value,
                    "generic": generic_release_value,
                },
            )
        else:
            cells = row_to_cells(row, include_tarball=include_tarball)
        ver_idx = version_col_idx if existing_columns else 0
        ver_value = cells[ver_idx] if ver_idx < len(cells) else (cells[0] if cells else "")
        ver_normalized = _normalize_version(ver_value)
        if ver_normalized in existing_versions:
            Log.info(f"  SKIP (exists): {ver_value}")
            # Existing version may still have blank release cells; backfill them.
            if existing_columns and release_col_indices:
                existing_idx = version_to_row_idx.get(ver_normalized)
                if existing_idx is not None and existing_idx < len(existing_cells):
                    existing_row = existing_cells[existing_idx]
                    changed = False
                    for col_idx, col_kind in release_col_indices:
                        if col_idx >= len(existing_row) or col_idx >= len(cells):
                            continue
                        if col_kind == "pc" and row.get("type", "").upper() != "PC":
                            continue
                        if col_kind == "aos" and row.get("type", "").upper() != "AOS":
                            continue
                        if _is_blank_release_value(existing_row[col_idx]) and not _is_blank_release_value(cells[col_idx]):
                            existing_row[col_idx] = cells[col_idx]
                            changed = True
                    for col_idx in status_col_indices:
                        if col_idx >= len(existing_row) or col_idx >= len(cells):
                            continue
                        if _is_blank_status_value(existing_row[col_idx]) and not _is_blank_status_value(cells[col_idx]):
                            existing_row[col_idx] = cells[col_idx]
                            changed = True
                    if changed:
                        backfilled += 1
                        Log.info(f"  BACKFILL (release/status): {ver_value}")
            skipped += 1
            continue
        Log.info(f"  ADD (new):     {ver_value}")
        new_cells.append(cells)
        existing_versions.add(ver_normalized)

    if not new_cells and backfilled == 0 and (not force_rebuild or existing_columns):
        Log.info(f"No new rows to add. {skipped} already exist on page.")
        return {"added": 0, "skipped": skipped,
                "total": len(existing_cells), "page_id": page_id}

    # For branch-specific preserved layout pages, never rewrite existing row
    # values. Keep existing rows unchanged and only prepend new rows.
    if existing_columns:
        # Keep only new rows sorted by date, then append existing rows as-is.
        date_col_idx_new = _date_col_from_columns(existing_columns)
        if date_col_idx_new is None:
            date_col_idx_new = _detect_date_column(new_cells) if new_cells else None
        if date_col_idx_new is None:
            date_col_idx_new = 0
        new_cells.sort(
            key=lambda r: parse_date(r[date_col_idx_new] if len(r) > date_col_idx_new else ""),
            reverse=True,
        )
        all_cells = new_cells + existing_cells
    else:
        all_cells = existing_cells + new_cells
    date_col_idx = None
    url_cols = None
    ticket_cols = None
    columns = None
    if existing_columns:
        columns = existing_columns
        date_col_idx = _date_col_from_columns(columns)
        url_cols = _url_cols_from_columns(columns)
        ticket_cols = _ticket_cols_from_columns(columns)
        if date_col_idx is None:
            date_col_idx = _detect_date_column(all_cells)
    table_html = build_table_storage(
        all_cells,
        include_tarball=include_tarball,
        columns=columns,
        date_col_idx=date_col_idx,
        url_cols=url_cols,
        ticket_cols=ticket_cols,
        sort_rows=not bool(existing_columns),
    )
    full_content = f"<h1>{page_title}</h1>\n{table_html}"

    Log.info(f"Table rebuilt: {len(new_cells)} new + {len(existing_cells)} existing "
         f"= {len(all_cells)} total, sorted by date (newest first)")

    if dry_run:
        Log.info("DRY RUN — page not updated")
        print(full_content)
        return {"added": len(new_cells), "skipped": skipped,
                "backfilled": backfilled,
                "total": len(all_cells), "page_id": page_id, "dry_run": True}

    if new_cells and backfilled:
        version_comment = f"Added {len(new_cells)} release(s), backfilled {backfilled} row(s)"
    elif new_cells:
        version_comment = f"Added {len(new_cells)} release(s)"
    elif backfilled:
        version_comment = f"Backfilled release column for {backfilled} row(s)"
    else:
        version_comment = "Table rebuild (re-sorted)"
    update_page_storage(server_key, page_id, page_title,
                        full_content, version_comment)
    Log.info(f"Updated page '{page_title}' (id={page_id}): "
         f"+{len(new_cells)} rows, {backfilled} backfilled, "
         f"{skipped} skipped, {len(all_cells)} total")

    return {
        "added": len(new_cells),
        "skipped": skipped,
        "backfilled": backfilled,
        "total": len(all_cells),
        "page_id": page_id,
        "page_title": page_title,
    }
