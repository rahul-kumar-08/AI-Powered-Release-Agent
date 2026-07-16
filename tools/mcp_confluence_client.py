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
    result = _mcp_call_tool(server_key, f"{TOOL_PREFIX}confluence_create_page", {
        "space_key": space_key,
        "title": title,
        "content": content,
        "parent_id": str(parent_id),
        "content_format": "markdown",
    })
    text = _extract_text(result)
    m = re.search(r'"id"\s*:\s*"?(\d+)"?', text)
    page_id = m.group(1) if m else None
    Log.info(f"Created page: {title} (id={page_id})")
    return page_id


def update_page(server_key, page_id, title, content, version_comment=""):
    result = _mcp_call_tool(server_key, f"{TOOL_PREFIX}confluence_update_page", {
        "page_id": str(page_id),
        "title": title,
        "content": content,
        "content_format": "markdown",
        "is_minor_edit": False,
        "version_comment": version_comment or "Release table update",
    })
    return _extract_text(result)


def update_page_storage(server_key, page_id, title, content, version_comment=""):
    result = _mcp_call_tool(server_key, f"{TOOL_PREFIX}confluence_update_page", {
        "page_id": str(page_id),
        "title": title,
        "content": content,
        "content_format": "storage",
        "is_minor_edit": False,
        "version_comment": version_comment or "Release table update",
    })
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

    # Fuzzy: branch version appears anywhere in title
    if branch_ver:
        for page in all_pages:
            title_lower = page.get("title", "").lower()
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
            if rhel_ver:
                # e.g. "Master-RHEL9.6", "Master-RHEL9"
                patterns.append(f"Master-RHEL{rhel_ver}")
                for minor in range(10, -1, -1):
                    patterns.append(f"Master-RHEL{rhel_ver}.{minor}")
            else:
                # No rhel_ver available (auto-count mode) — try EL9 variants first
                for minor in range(10, -1, -1):
                    patterns.append(f"Master-RHEL9.{minor}")
                patterns.append("Master-RHEL9")
            patterns.append("Master - PC")
            patterns.append("Master-PC")
            patterns.append("Master PC")
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
            return f"Master-RHEL{rhel_ver or '9'}"
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
    ticket = row.get("main_ticket", row.get("ticket", "--"))
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


def build_table_storage(all_rows_cells, include_tarball=False):
    """Build a Confluence storage-format (XHTML) table with Jira Issue macros for tickets."""
    columns = TABLE_COLUMNS_WITH_TARBALL if include_tarball else TABLE_COLUMNS
    date_col_idx = 5 if include_tarball else 4
    url_cols = {2, 3, 4} if include_tarball else {2, 3}

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
            is_ticket = (i == 1)
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
    Log.info(f"Existing rows on page: {len(existing_cells)} "
         f"({len(existing_versions)} unique versions)")

    include_tarball = _needs_gi_tarball(branch, release_type)

    new_cells = []
    skipped = 0
    for row in rows:
        cells = row_to_cells(row, include_tarball=include_tarball)
        ver_normalized = _normalize_version(cells[0])
        if ver_normalized in existing_versions:
            Log.info(f"  SKIP (exists): {cells[0]}")
            skipped += 1
            continue
        Log.info(f"  ADD (new):     {cells[0]}")
        new_cells.append(cells)
        existing_versions.add(ver_normalized)

    if not new_cells and not force_rebuild:
        Log.info(f"No new rows to add. {skipped} already exist on page.")
        return {"added": 0, "skipped": skipped,
                "total": len(existing_cells), "page_id": page_id}

    all_cells = existing_cells + new_cells
    table_html = build_table_storage(all_cells, include_tarball=include_tarball)
    full_content = f"<h1>{page_title}</h1>\n{table_html}"

    Log.info(f"Table rebuilt: {len(new_cells)} new + {len(existing_cells)} existing "
         f"= {len(all_cells)} total, sorted by date (newest first)")

    if dry_run:
        Log.info("DRY RUN — page not updated")
        print(full_content)
        return {"added": len(new_cells), "skipped": skipped,
                "total": len(all_cells), "page_id": page_id, "dry_run": True}

    version_comment = (f"Added {len(new_cells)} release(s)"
                       if new_cells else "Table rebuild (re-sorted)")
    update_page_storage(server_key, page_id, page_title,
                        full_content, version_comment)
    Log.info(f"Updated page '{page_title}' (id={page_id}): "
         f"+{len(new_cells)} rows, {skipped} skipped, {len(all_cells)} total")

    return {
        "added": len(new_cells),
        "skipped": skipped,
        "total": len(all_cells),
        "page_id": page_id,
        "page_title": page_title,
    }
