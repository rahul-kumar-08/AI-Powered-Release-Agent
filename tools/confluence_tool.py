#!/usr/bin/env python3
"""
Confluence Tool — Self-contained HTTP client for updating the GoldImage
release table on Confluence.

Reads the release extractor JSON, enriches it via Jira lookups (reusing
jira_tool helpers), and updates/creates the Confluence table.

All public functions raise typed exceptions from tools.exceptions so the
orchestrator can retry transient failures.

Usage:
  python3 tools/confluence_tool.py --input-json <release_json> --branch <branch>
  python3 tools/confluence_tool.py --input-json <release_json> --branch master --dry-run

Environment variables (from tools/.env):
  CONFLUENCE_BASE_URL   — Confluence server URL
  CONFLUENCE_EMAIL      — Confluence user email
  CONFLUENCE_API_TOKEN  — Confluence API token (Bearer)
  CONFLUENCE_PAGE_ID    — Target Confluence page ID
  JIRA_BASE_URL         — Jira server URL
  JIRA_API_TOKEN        — Jira personal access token (Bearer)
"""

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from html import escape as html_escape

try:
    from tools.exceptions import (
        ToolError, AuthError, ConfigError, HttpError, NetworkError,
        RateLimitError, NotFoundError, DataError,
    )
    from tools.jira_tool import (
        find_aos_epic, extract_aos_version as _jira_extract_aos_version,
        get_kernel_version, build_endor_url, format_merge_date,
        check_url_exists, _load_env_file,
    )
except ModuleNotFoundError:
    from exceptions import (
        ToolError, AuthError, ConfigError, HttpError, NetworkError,
        RateLimitError, NotFoundError, DataError,
    )
    from jira_tool import (
        find_aos_epic, extract_aos_version as _jira_extract_aos_version,
        get_kernel_version, build_endor_url, format_merge_date,
        check_url_exists, _load_env_file,
    )



TABLE_COLUMNS = [
    "GoldImage Version",
    "Main Tickets",
    "Change Log",
    "RPM List",
    "Merge Date",
    "Notes",
]


# ---------------------------------------------------------------------------
# Environment helpers
# ---------------------------------------------------------------------------

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
# Version helpers (Confluence uses a slightly broader regex than jira_tool)
# ---------------------------------------------------------------------------

def parse_version_parts(aos_version):
    """
    Parse version string into components.
    e.g. main-master-rhel9.7-9.0.0 -> ("main-master", 9, 7, "9.0.0")
    """
    match = re.match(r"(main-[\w.]+)-rhel(\d+)\.(\d+)-([\d.]+)", aos_version)
    if match:
        return match.group(1), int(match.group(2)), int(match.group(3)), match.group(4)
    return None


def build_goldimage_version(aos_version, kernel_version):
    """Insert kernel version: main-master-rhel9.7-9.0.0 -> main-master-rhel9.7-5.14.0-9.0.0"""
    match = re.match(r"(main-[\w.]+-rhel[\d.]+)-([\d.]+)", aos_version)
    if match:
        return f"{match.group(1)}-{kernel_version}-{match.group(2)}"
    return aos_version


def _extract_aos_version(release_title):
    """Broader AOS version regex that handles non-master branches."""
    match = re.search(
        r"[Rr]elease [Gg]old [Ii]mage (main-[\w.]+-rhel[\d.]+-[\d.]+)",
        release_title,
    )
    return match.group(1) if match else None


# ---------------------------------------------------------------------------
# Release data enrichment
# ---------------------------------------------------------------------------

def _is_pc_row(row):
    """Return True if this row is a PC release.
    
    Uses explicit comp_type from release_query.py JSON when available,
    otherwise falls back to goldimage_version string matching.
    """
    ct = row.get("comp_type", "").upper()
    if ct:
        return ct == "PC"
    gi = row.get("goldimage_version", "").lower()
    if "(pc)" in gi:
        return True
    if "(aos)" in gi:
        return False
    if "pc." in gi or "-pc-" in gi:
        return True
    return False


def _convert_release_query_rows(data, branch, release_type="AOS"):
    """Convert release_query.py JSON (has 'rows' key) to enriched format.
    
    Filters rows to only include those matching *release_type* (AOS or PC)
    and whose Jira main ticket is Closed.
    """
    from concurrent.futures import ThreadPoolExecutor

    def _url(md):
        m = re.search(r"\((.+?)\)", md or "")
        return m.group(1) if m else None

    filtered_rows = []
    skipped_non_closed = 0
    for r in data["rows"]:
        jira_status = (r.get("jira_status") or "").lower()
        if jira_status and jira_status != "closed":
            skipped_non_closed += 1
            continue
        is_pc = _is_pc_row(r)
        if release_type == "PC" and is_pc:
            filtered_rows.append(r)
        elif release_type == "AOS" and not is_pc:
            filtered_rows.append(r)
    if skipped_non_closed:
        print(f"  Skipped {skipped_non_closed} row(s) with non-Closed Jira ticket")

    results = []
    urls_to_validate = []

    for i, r in enumerate(filtered_rows):
        gi = r.get("goldimage_version", "")
        ticket = r.get("main_ticket", "N/A")
        changelog = r.get("changelog", "")
        rpm = r.get("rpm", "")
        sg_date = r.get("sg_merge_date") or ""
        gh_date = r.get("gh_merge_date") or ""
        best_date = sg_date or gh_date
        notes = r.get("gerrit_ref") or branch

        cl_url = _url(changelog)
        rpm_url = _url(rpm)

        merge_date = ""
        if best_date:
            try:
                dt = datetime.strptime(best_date[:10], "%Y-%m-%d")
                merge_date = dt.strftime("%d-%b-%Y")
            except (ValueError, TypeError):
                merge_date = best_date[:10]

        results.append({
            "goldimage_version": gi,
            "main_tickets": ticket,
            "changelog_url": cl_url,
            "rpm_url": rpm_url,
            "merge_date": merge_date,
            "notes": notes,
            "release_key": gi,
            "merged_at_raw": best_date,
        })

        if cl_url:
            urls_to_validate.append((i, "changelog_url", cl_url))
        if rpm_url:
            urls_to_validate.append((i, "rpm_url", rpm_url))

    if urls_to_validate:
        print(f"  Validating {len(urls_to_validate)} endor URLs...", end=" ", flush=True)
        def _validate(item):
            idx, key, url = item
            return idx, key, check_url_exists(url)
        with ThreadPoolExecutor(max_workers=10) as pool:
            for idx, key, valid in pool.map(_validate, urls_to_validate):
                if not valid:
                    results[idx][key] = None
        print("done.")

    results.sort(key=lambda x: x["merged_at_raw"], reverse=True)
    return results


def enrich_releases(input_json_path, branch, jira_base_url, jira_token, release_type="AOS"):
    """Read release JSON, search Jira for Epics, and return enriched rows."""
    with open(input_json_path) as f:
        data = json.load(f)

    # Support release_query.py format (has 'rows' key)
    if isinstance(data, dict) and "rows" in data:
        print("  Using pre-enriched release_query.py data.")
        return _convert_release_query_rows(data, branch, release_type)

    results = []
    for release_key, release_obj in data.items():
        prs = release_obj.get("prs", [])
        if not prs:
            continue

        pr = prs[0]
        title = pr.get("title", "")
        merged_at = pr.get("merged_at", "")

        if not re.match(r"^Release", title, re.IGNORECASE):
            continue

        aos_version = _extract_aos_version(title)
        if not aos_version:
            continue

        parts = parse_version_parts(aos_version)
        if not parts:
            continue

        branch_prefix, rhel_major, rhel_minor, release_version = parts
        kernel_version = get_kernel_version(rhel_major)

        print(f"  Searching Jira for Epic: {aos_version} ...", end=" ", flush=True)
        epic_key = find_aos_epic(jira_base_url, jira_token, aos_version)
        print(epic_key or "NOT FOUND")

        changelog_url = build_endor_url(rhel_major, rhel_minor, kernel_version, release_version, "changelog.txt", branch, release_type)
        rpm_url = build_endor_url(rhel_major, rhel_minor, kernel_version, release_version, "rpm.txt", branch, release_type)

        print(f"  Validating endor URLs for {release_version} ...", end=" ", flush=True)
        if check_url_exists(changelog_url):
            print("OK")
            merge_date = format_merge_date(merged_at)
        else:
            changelog_url = None
            rpm_url = None
            merge_date = ""
            print("Data not found")

        results.append({
            "goldimage_version": build_goldimage_version(aos_version, kernel_version),
            "main_tickets": epic_key or "N/A",
            "changelog_url": changelog_url,
            "rpm_url": rpm_url,
            "merge_date": merge_date,
            "notes": branch,
            "release_key": release_key,
            "merged_at_raw": merged_at,
        })

    results.sort(key=lambda x: x["merged_at_raw"], reverse=True)
    return results


# ---------------------------------------------------------------------------
# Confluence HTTP helpers
# ---------------------------------------------------------------------------

def confluence_request(base_url, token, method, path, body=None):
    """Low-level Confluence HTTP call. Raises typed exceptions."""
    url = f"{base_url.rstrip('/')}/rest/api{path}"
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
    }
    data = json.dumps(body).encode("utf-8") if body else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
        except Exception:
            pass
        msg = f"Confluence HTTP {exc.code} on {method} {path}: {detail}"
        if exc.code in (401, 403):
            raise AuthError(msg, status_code=exc.code)
        if exc.code == 429:
            raise RateLimitError(msg, retry_after=exc.headers.get("Retry-After"))
        if exc.code == 404:
            raise NotFoundError(msg)
        raise HttpError(msg, status_code=exc.code)
    except urllib.error.URLError as exc:
        raise NetworkError(f"Confluence network error on {method} {path}: {exc}")


def get_page(base_url, token, page_id):
    return confluence_request(
        base_url, token, "GET",
        f"/content/{page_id}?expand=body.storage,version",
    )


def get_child_pages(base_url, token, parent_id):
    """Return list of child pages under *parent_id*."""
    results = []
    start = 0
    while True:
        data = confluence_request(
            base_url, token, "GET",
            f"/content/{parent_id}/child/page?limit=50&start={start}",
        )
        batch = data.get("results", [])
        results.extend(batch)
        if len(batch) < 50:
            break
        start += 50
    return results


def create_page(base_url, token, parent_id, title, space_key, body_html=""):
    """Create a new child page under *parent_id*."""
    payload = {
        "type": "page",
        "title": title,
        "ancestors": [{"id": parent_id}],
        "space": {"key": space_key},
        "body": {"storage": {"value": body_html, "representation": "storage"}},
    }
    return confluence_request(base_url, token, "POST", "/content", body=payload)


def resolve_child_page(base_url, token, parent_id, branch, release_type):
    """Find or create the child page for a given branch + release type.

    Searches children of *parent_id* for a title containing both the branch
    name and the release type (AOS/PC).  If no match is found, creates a new
    child page with a standardised title.

    Returns ``(page_id, page_title, created_bool)``.
    """
    children = get_child_pages(base_url, token, parent_id)
    branch_lower = branch.lower()
    type_lower = release_type.lower()

    for child in children:
        title = child.get("title", "")
        title_lower = title.lower()
        branch_match = branch_lower in title_lower
        type_match = type_lower in title_lower
        if branch_match and type_match:
            return child["id"], title, False

    # Fetch parent to get space key
    parent = confluence_request(
        base_url, token, "GET",
        f"/content/{parent_id}?expand=space",
    )
    space_key = parent.get("space", {}).get("key", "")
    new_title = f"{release_type} Release {branch}"
    new_page = create_page(base_url, token, parent_id, new_title, space_key)
    return new_page["id"], new_title, True


def update_page(base_url, token, page_id, title, version, new_body):
    payload = {
        "id": page_id,
        "type": "page",
        "title": title,
        "version": {"number": version + 1},
        "body": {"storage": {"value": new_body, "representation": "storage"}},
    }
    return confluence_request(base_url, token, "PUT", f"/content/{page_id}", body=payload)


# ---------------------------------------------------------------------------
# HTML table helpers
# ---------------------------------------------------------------------------

def strip_tags(html):
    return re.sub(r"<[^>]+>", "", html or "").strip()


def extract_existing_goldimage_versions(body_html):
    """Return set of GoldImage Versions already present in the page table."""
    versions = set()
    for row_match in re.finditer(r"<tr[^>]*>(.*?)</tr>", body_html, re.DOTALL | re.IGNORECASE):
        cells = re.findall(r"<td[^>]*>(.*?)</td>", row_match.group(1), re.DOTALL | re.IGNORECASE)
        if cells:
            versions.add(strip_tags(cells[0]).strip())
    return versions


def _parse_merge_date(date_str):
    """Parse 'dd-Mon-YYYY' to datetime for sorting. Blank/missing dates sort newest (top)."""
    if not date_str or not date_str.strip():
        return datetime.max
    try:
        return datetime.strptime(date_str.strip(), "%d-%b-%Y")
    except (ValueError, TypeError):
        return datetime.max


def find_goldimage_table_bounds(body_html):
    """Return (start, end) of the GoldImage table, or (-1, -1) if not found."""
    for table_match in re.finditer(r"<table[^>]*>", body_html, re.IGNORECASE):
        table_start = table_match.start()
        table_end_match = re.search(r"</table>", body_html[table_start:], re.IGNORECASE)
        if not table_end_match:
            continue
        table_end = table_start + table_end_match.end()
        table_html = body_html[table_start:table_end]
        header_cells = re.findall(r"<th[^>]*>(.*?)</th>", table_html, re.DOTALL | re.IGNORECASE)
        header_texts = [strip_tags(c).strip().lower() for c in header_cells]
        if "goldimage version" in header_texts:
            return table_start, table_end
    return -1, -1


def extract_existing_rows(table_html):
    """Extract data rows from table HTML as list of (merge_date_str, row_html)."""
    rows = []
    header_cells = re.findall(r"<th[^>]*>(.*?)</th>", table_html, re.DOTALL | re.IGNORECASE)
    header_texts = [strip_tags(c).strip().lower() for c in header_cells]
    date_idx = next((i for i, h in enumerate(header_texts) if "merge date" in h), -1)

    for row_match in re.finditer(r"<tr[^>]*>(.*?)</tr>", table_html, re.DOTALL | re.IGNORECASE):
        row_inner = row_match.group(1)
        cells = re.findall(r"<td[^>]*>(.*?)</td>", row_inner, re.DOTALL | re.IGNORECASE)
        if not cells:
            continue
        date_text = strip_tags(cells[date_idx]).strip() if 0 <= date_idx < len(cells) else ""
        rows.append((date_text, f"<tr>{row_inner}</tr>"))
    return rows


def format_jira_macro(ticket_key):
    return (
        f'<ac:structured-macro ac:name="jira">'
        f'<ac:parameter ac:name="key">{html_escape(ticket_key)}</ac:parameter>'
        f'</ac:structured-macro>'
    )


def build_table_html(releases, jira_base_url):
    header_cells = "".join(f"<th>{c}</th>" for c in TABLE_COLUMNS)
    header_row = f"<tr>{header_cells}</tr>"
    rows = [build_row_html(r, jira_base_url) for r in releases]
    return (
        '<table class="wrapped"><colgroup>'
        "<col /><col /><col /><col /><col /><col />"
        "</colgroup><tbody>"
        + header_row
        + "\n".join(rows)
        + "</tbody></table>"
    )


def build_row_html(release, jira_base_url):
    r = release
    ticket = r["main_tickets"]
    ticket_cell = format_jira_macro(ticket) if ticket != "N/A" else "N/A"

    if r["changelog_url"]:
        cl_url = html_escape(r["changelog_url"])
        changelog_cell = f'<a href="{cl_url}">{cl_url}</a>'
    else:
        changelog_cell = "Data not found"

    if r["rpm_url"]:
        rpm = html_escape(r["rpm_url"])
        rpm_cell = f'<a href="{rpm}">{rpm}</a>'
    else:
        rpm_cell = "Data not found"

    return (
        "<tr>"
        f"<td>{html_escape(r['goldimage_version'])}</td>"
        f"<td>{ticket_cell}</td>"
        f"<td>{changelog_cell}</td>"
        f"<td>{rpm_cell}</td>"
        f"<td>{html_escape(r['merge_date'])}</td>"
        f"<td>{html_escape(r['notes'])}</td>"
        "</tr>"
    )


# ---------------------------------------------------------------------------
# Main CLI
# ---------------------------------------------------------------------------

def _detect_release_type(input_json_path):
    """Infer AOS or PC from the release_query.py JSON data."""
    try:
        with open(input_json_path) as f:
            data = json.load(f)
        if isinstance(data, dict) and "rows" in data:
            for row in data["rows"]:
                gi = row.get("goldimage_version", "").lower()
                if "(pc)" in gi or "pc." in gi:
                    return "PC"
            return "AOS"
    except Exception:
        pass
    return None


def main():
    parser = argparse.ArgumentParser(
        description="Confluence Tool — Update GoldImage release table"
    )
    parser.add_argument("--input-json", required=True,
                        help="Path to release extractor JSON")
    parser.add_argument("--branch", required=True,
                        help="Branch name (Notes column & Jira search)")
    parser.add_argument("--type", dest="release_type", choices=["AOS", "PC"],
                        default=None, help="Release type (auto-detected from JSON if omitted)")
    parser.add_argument("--max-releases", type=int, default=0,
                        help="Max releases to process (0 = all)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview changes without updating Confluence")
    parser.add_argument("--force-rebuild", action="store_true",
                        help="Rebuild entire table (re-sort, re-validate URLs) even if no new rows")
    args = parser.parse_args()

    confluence_base_url = _require_env("CONFLUENCE_BASE_URL")
    confluence_token = _require_env("CONFLUENCE_API_TOKEN")
    parent_page_id = _require_env("CONFLUENCE_PAGE_ID")
    jira_base_url = _require_env("JIRA_BASE_URL")
    jira_token = _require_env("JIRA_API_TOKEN")

    release_type = args.release_type or _detect_release_type(args.input_json) or "AOS"

    print("=" * 50)
    print("  GoldImage Confluence Table Updater")
    print("=" * 50)
    print(f"  Input JSON : {args.input_json}")
    print(f"  Branch     : {args.branch}")
    print(f"  Type       : {release_type}")
    print(f"  Parent ID  : {parent_page_id}")

    print(f"\n[1/4] Resolving Confluence page for {release_type} {args.branch}...")
    page_id, page_title_resolved, created = resolve_child_page(
        confluence_base_url, confluence_token, parent_page_id,
        args.branch, release_type,
    )
    if created:
        print(f"  Created new page: \"{page_title_resolved}\" (ID: {page_id})")
    else:
        print(f"  Found page: \"{page_title_resolved}\" (ID: {page_id})")

    print(f"  Mode       : append (deduplicate)")
    print("=" * 50)

    print("\n[2/4] Enriching releases with Jira Epic tickets...")
    releases = enrich_releases(args.input_json, args.branch, jira_base_url, jira_token, release_type)

    if not releases:
        raise DataError("No releases found in input JSON.")

    if args.max_releases:
        releases = releases[:args.max_releases]

    print(f"\n  Total releases to process: {len(releases)}")

    print("\n[3/4] Fetching current Confluence page...")
    page = get_page(confluence_base_url, confluence_token, page_id)
    page_title = page.get("title", "")
    version = page.get("version", {}).get("number", 0)
    body_html = page.get("body", {}).get("storage", {}).get("value", "")
    print(f"  Page title : {page_title}")
    print(f"  Version    : {version}")

    print("\n[4/4] Updating Confluence page...")

    existing_versions = extract_existing_goldimage_versions(body_html)
    new_releases = [r for r in releases if r["goldimage_version"] not in existing_versions]
    skipped = [r for r in releases if r["goldimage_version"] in existing_versions]

    if not new_releases and not args.force_rebuild:
        print(f"\n  All {len(skipped)} release(s) already exist. Nothing to update.")
        for r in skipped:
            print(f"    - {r['goldimage_version']}")
        return

    table_start, table_end = find_goldimage_table_bounds(body_html)
    table_found = table_start >= 0

    if args.force_rebuild:
        # Rebuild entire table from JSON data — re-sort and re-validate URLs
        print("  Rebuilding entire table (--force-rebuild)...")
        all_row_entries = [
            (r["merge_date"], build_row_html(r, jira_base_url))
            for r in releases
        ]
    else:
        new_row_entries = [
            (r["merge_date"], build_row_html(r, jira_base_url))
            for r in new_releases
        ]
        if table_found:
            existing_row_entries = extract_existing_rows(body_html[table_start:table_end])
        else:
            existing_row_entries = []
        all_row_entries = existing_row_entries + new_row_entries

    all_row_entries.sort(key=lambda x: _parse_merge_date(x[0]), reverse=True)

    sorted_rows_html = "\n".join(html for _, html in all_row_entries)
    header_cells_html = "".join(f"<th>{c}</th>" for c in TABLE_COLUMNS)
    full_table = (
        '<table class="wrapped"><colgroup>'
        "<col /><col /><col /><col /><col /><col />"
        "</colgroup><tbody>"
        f"<tr>{header_cells_html}</tr>\n"
        + sorted_rows_html
        + "\n</tbody></table>"
    )

    if table_found:
        new_body = body_html[:table_start] + full_table + body_html[table_end:]
    else:
        new_body = full_table

    if args.dry_run:
        print(f"\n  DRY RUN — would insert {len(new_releases)} row(s)")
        for r in new_releases:
            print(f"    + {r['goldimage_version']} | {r['main_tickets']} | {r['merge_date']}")
        if skipped:
            print(f"  Skipped {len(skipped)} (already exist)")
        return

    result = update_page(confluence_base_url, confluence_token, page_id, page_title, version, new_body)

    print(f"\n  Confluence page updated successfully!")
    print(f"  Page    : {result.get('title', page_title)}")
    print(f"  Version : {result['version']['number']}")
    print(f"  URL     : {confluence_base_url}/pages/viewpage.action?pageId={page_id}")
    if args.force_rebuild:
        print(f"  Rebuilt : {len(all_row_entries)} row(s), sorted newest-first")
    else:
        print(f"  Added   : {len(new_releases)} row(s)")
    if skipped and not args.force_rebuild:
        print(f"  Skipped : {len(skipped)} (already exist)")

    if new_releases:
        print("\n  Releases added:")
        for r in new_releases:
            print(f"    + {r['goldimage_version']} | {r['main_tickets']} | {r['merge_date']}")


if __name__ == "__main__":
    try:
        main()
    except ToolError as exc:
        print(f"Error [{type(exc).__name__}]: {exc}", file=sys.stderr)
        sys.exit(2 if isinstance(exc, ConfigError) else 1)
