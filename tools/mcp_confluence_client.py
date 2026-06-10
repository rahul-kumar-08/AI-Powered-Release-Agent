#!/usr/bin/env python3
"""
MCP Confluence Client — Uploads release table data to Confluence via
the Atlassian MCP server configured in .cursor/rules/mcp.json.

Features:
  - Auto page routing: finds or creates child pages under CONFLUENCE_PAGE_ID
    by branch + release type (AOS/PC)
  - Deduplication: reads existing table rows, only adds new ones
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
  CONFLUENCE_PAGE_ID  — Parent page ID (child pages auto-resolved per branch/type)
"""

import json
import os
import re
import sys
from datetime import datetime

try:
    from tools.mcp_client import call_tool as _mcp_call_tool, _get_env
except ImportError:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from mcp_client import call_tool as _mcp_call_tool, _get_env

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

TOOL_PREFIX = "atlassian__"


def _log(msg):
    print(f"[confluence] {msg}", file=sys.stderr, flush=True)


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
    _log(f"Created page: {title} (id={page_id})")
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


def find_or_create_child_page(server_key, parent_id, branch, release_type, space_key=None):
    if not space_key:
        space_key = get_space_key(server_key, parent_id)
        _log(f"Resolved space key: {space_key}")

    children = get_child_pages(server_key, parent_id)
    target_title = f"{release_type} Release {branch}"
    _log(f"Looking for child page: '{target_title}' under parent {parent_id}")

    for child in children:
        title = child.get("title", "")
        if branch.lower() in title.lower() and release_type.lower() in title.lower():
            page_id = str(child.get("id", ""))
            _log(f"Found existing page: '{title}' (id={page_id})")
            return page_id, title

    _log(f"No matching child page found, creating: '{target_title}'")
    page_id = create_page(server_key, space_key, target_title, f"# {target_title}\n\n*(table pending)*", parent_id)
    return page_id, target_title


# ---------------------------------------------------------------------------
# Table Parsing & Building
# ---------------------------------------------------------------------------

TABLE_COLUMNS = [
    "GoldImage Version", "Main Ticket", "Change Log", "RPM List", "Merge Date", "Notes"
]


def _normalize_version(ver):
    """Normalize a version string for dedup comparison.

    Strips ``(AOS)``/``(PC)`` suffixes, extra whitespace, and lowercases
    so that ``sts-ganges-7.5-rhel8.10-5.10.237-8.0.0 (AOS)`` matches
    ``sts-ganges-7.5-rhel8.10-5.10.237-8.0.0``.
    """
    if not ver:
        return ""
    ver = re.sub(r"\s*\((AOS|PC)\)\s*$", "", ver, flags=re.IGNORECASE)
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


def row_to_cells(row):
    ver = row.get("goldimage_version", row.get("ver", ""))
    ticket = row.get("main_ticket", row.get("ticket", "--"))
    cl_url = row.get("changelog_url", row.get("cl", ""))
    rpm_url = row.get("rpm_url", row.get("rpm", ""))
    merge_date = row.get("merge_date", row.get("date", "N/A"))
    notes = row.get("notes", "")
    rtype = row.get("type", "")
    if rtype:
        ver = f"{ver} ({rtype})"

    cl_cell = cl_url if cl_url and "not found" not in cl_url.lower() else "Data not found"
    rpm_cell = rpm_url if rpm_url and "not found" not in rpm_url.lower() else "Data not found"

    return [ver, ticket, cl_cell, rpm_cell, merge_date, notes]


def _jira_macro(ticket_key):
    """Build Confluence Jira Issue macro in storage format."""
    return (
        f'<ac:structured-macro ac:name="jira">'
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


def build_table_storage(all_rows_cells):
    """Build a Confluence storage-format (XHTML) table with Jira Issue macros for tickets."""
    all_rows_cells.sort(key=lambda r: parse_date(r[4] if len(r) > 4 else ""), reverse=True)

    lines = ['<table>', '<thead>', '<tr>']
    for col in TABLE_COLUMNS:
        lines.append(f"<th>{_escape_html(col)}</th>")
    lines.append("</tr></thead>")
    lines.append("<tbody>")

    for cells in all_rows_cells:
        lines.append("<tr>")
        for i, cell in enumerate(cells):
            is_ticket = (i == 1)
            is_url = (i in (2, 3))
            lines.append(_build_td(cell, is_ticket=is_ticket, is_url=is_url))
        lines.append("</tr>")

    lines.append("</tbody></table>")
    return "\n".join(lines)


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
      1. Read existing page content and extract table rows.
      2. Deduplicate: skip any new row whose normalized version already exists.
      3. Append new rows to existing rows.
      4. Sort ALL rows by merge date (newest first) and rebuild the table.
      5. Update the page only if new rows were added (or force_rebuild).
    """
    if not rows:
        _log("No rows to upload.")
        return {"added": 0, "skipped": 0, "total": 0}

    if not release_type:
        release_type = detect_release_type(rows)
    release_type = release_type.upper()
    _log(f"Release type: {release_type}, branch: {branch}, incoming rows: {len(rows)}")

    page_id, page_title = find_or_create_child_page(
        server_key, parent_id, branch, release_type, space_key)
    if not page_id:
        raise RuntimeError("Failed to find or create target page")

    page_content = get_page_content(server_key, page_id)
    existing_versions, existing_cells = extract_existing_versions(page_content)
    _log(f"Existing rows on page: {len(existing_cells)} "
         f"({len(existing_versions)} unique versions)")

    new_cells = []
    skipped = 0
    for row in rows:
        cells = row_to_cells(row)
        ver_normalized = _normalize_version(cells[0])
        if ver_normalized in existing_versions:
            _log(f"  SKIP (exists): {cells[0]}")
            skipped += 1
            continue
        _log(f"  ADD (new):     {cells[0]}")
        new_cells.append(cells)
        existing_versions.add(ver_normalized)

    if not new_cells and not force_rebuild:
        _log(f"No new rows to add. {skipped} already exist on page.")
        return {"added": 0, "skipped": skipped,
                "total": len(existing_cells), "page_id": page_id}

    all_cells = existing_cells + new_cells
    table_html = build_table_storage(all_cells)
    full_content = f"<h1>{page_title}</h1>\n{table_html}"

    _log(f"Table rebuilt: {len(new_cells)} new + {len(existing_cells)} existing "
         f"= {len(all_cells)} total, sorted by date (newest first)")

    if dry_run:
        _log("DRY RUN — page not updated")
        print(full_content)
        return {"added": len(new_cells), "skipped": skipped,
                "total": len(all_cells), "page_id": page_id, "dry_run": True}

    version_comment = (f"Added {len(new_cells)} release(s)"
                       if new_cells else "Table rebuild (re-sorted)")
    update_page_storage(server_key, page_id, page_title,
                        full_content, version_comment)
    _log(f"Updated page '{page_title}' (id={page_id}): "
         f"+{len(new_cells)} rows, {skipped} skipped, {len(all_cells)} total")

    return {
        "added": len(new_cells),
        "skipped": skipped,
        "total": len(all_cells),
        "page_id": page_id,
        "page_title": page_title,
    }
