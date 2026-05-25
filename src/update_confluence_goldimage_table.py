#!/usr/bin/env python3
"""
Update a Confluence page with the GoldImage release table.

Reads the enriched JSON output from search_jira_epic.py (or generates it
from the release extractor JSON + Jira lookups) and updates a Confluence
page with the new table format:

  GoldImage Version | Main Tickets | Change Log | RPM List | Merge Date | Notes

Deduplicates by GoldImage Version — existing rows are not re-added.

Usage:
  python3 update_confluence_goldimage_table.py --input-json <release_json> --branch <branch>
  python3 update_confluence_goldimage_table.py --input-json /tmp/release_graphql_master_10.json --branch master
  python3 update_confluence_goldimage_table.py --input-json /tmp/release_graphql_master_10.json --branch master --dry-run

Environment variables (from src/.env):
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


ENDOR_BASE_URL = "http://endor.dyn.nutanix.com/GoldImages/Centos_SVM/Master"

KERNEL_MAP = {
    "9": "5.14.0",
}

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

def get_env_or_exit(name):
    val = os.environ.get(name, "").strip()
    if not val:
        print(f"Error: {name} is not set. Add it to src/.env or export it.", file=sys.stderr)
        sys.exit(2)
    return val


# ---------------------------------------------------------------------------
# Jira helpers
# ---------------------------------------------------------------------------

def jira_search(base_url, token, jql, fields="key,summary", max_results=10):
    params = urllib.parse.urlencode({
        "jql": jql,
        "fields": fields,
        "maxResults": max_results,
    })
    url = f"{base_url}/rest/api/2/search?{params}"
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
            return data.get("issues", [])
    except urllib.error.HTTPError as e:
        print(f"  Jira API error: {e.code} {e.reason}", file=sys.stderr)
        return []
    except Exception as e:
        print(f"  Jira request failed: {e}", file=sys.stderr)
        return []


def find_aos_epic(base_url, token, version_string):
    """Search Jira for an AOS Epic matching the release version."""
    jql = f'issuetype = Epic AND summary ~ "Release gold image {version_string}"'
    issues = jira_search(base_url, token, jql, max_results=5)

    for issue in issues:
        summary = issue["fields"]["summary"]
        if "PC" in summary or "pc" in summary.split(":")[0]:
            continue
        if version_string.lower() in summary.lower():
            return issue["key"]

    if issues:
        return issues[0]["key"]
    return None


# ---------------------------------------------------------------------------
# Version / URL helpers
# ---------------------------------------------------------------------------

def extract_aos_version(release_title):
    """Extract AOS version from release PR title."""
    match = re.search(
        r"[Rr]elease [Gg]old [Ii]mage (main-[\w.]+-rhel[\d.]+-[\d.]+)",
        release_title
    )
    if match:
        return match.group(1)
    return None


def parse_version_parts(aos_version):
    """
    Parse version string into components.
    e.g. main-master-rhel9.7-9.0.0 -> ("main-master", 9, 7, "9.0.0")
    """
    match = re.match(r"(main-[\w.]+)-rhel(\d+)\.(\d+)-([\d.]+)", aos_version)
    if match:
        return match.group(1), int(match.group(2)), int(match.group(3)), match.group(4)
    return None


def get_kernel_version(rhel_major):
    return KERNEL_MAP.get(str(rhel_major), "5.14.0")


def build_goldimage_version(aos_version, kernel_version):
    """Insert kernel version: main-master-rhel9.7-9.0.0 -> main-master-rhel9.7-5.14.0-9.0.0"""
    match = re.match(r"(main-[\w.]+-rhel[\d.]+)-([\d.]+)", aos_version)
    if match:
        return f"{match.group(1)}-{kernel_version}-{match.group(2)}"
    return aos_version


def build_endor_url(rhel_major, rhel_minor, kernel_version, release_version, filename):
    rhel_tag = f"RHEL{rhel_major}{rhel_minor}"
    folder = f"{rhel_tag}-SVM-{rhel_major}.{rhel_minor}-k{kernel_version}-r{release_version}.x86_64"
    return f"{ENDOR_BASE_URL}/{folder}/{filename}"


def format_merge_date(iso_date):
    try:
        dt = datetime.strptime(iso_date[:19], "%Y-%m-%dT%H:%M:%S")
        return dt.strftime("%d-%b-%Y")
    except (ValueError, TypeError):
        return iso_date or "N/A"


def check_url_exists(url):
    """Check if a URL is valid (returns 2xx or 3xx). Returns False for 404 or errors."""
    req = urllib.request.Request(url, method="HEAD")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status < 400
    except urllib.error.HTTPError as e:
        return e.code < 400
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Release data enrichment
# ---------------------------------------------------------------------------

def enrich_releases(input_json_path, branch, jira_base_url, jira_token):
    """Read release JSON, search Jira for Epics, and return enriched rows."""
    with open(input_json_path) as f:
        data = json.load(f)

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

        aos_version = extract_aos_version(title)
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

        changelog_url = build_endor_url(rhel_major, rhel_minor, kernel_version, release_version, "changelog.txt")
        rpm_url = build_endor_url(rhel_major, rhel_minor, kernel_version, release_version, "rpm.txt")

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
# Confluence helpers
# ---------------------------------------------------------------------------

def confluence_request(base_url, token, method, path, body=None):
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
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Confluence HTTP {exc.code} on {method} {path}: {detail[:500]}")


def get_page(base_url, token, page_id):
    return confluence_request(
        base_url, token, "GET",
        f"/content/{page_id}?expand=body.storage,version",
    )


def update_page(base_url, token, page_id, title, version, new_body):
    payload = {
        "id": page_id,
        "type": "page",
        "title": title,
        "version": {"number": version + 1},
        "body": {"storage": {"value": new_body, "representation": "storage"}},
    }
    return confluence_request(base_url, token, "PUT", f"/content/{page_id}", body=payload)


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


def find_table_insertion_point(body_html):
    """Find position right after the header row of the GoldImage table."""
    table_iter = re.finditer(r"<table[^>]*>", body_html, re.IGNORECASE)
    for table_match in table_iter:
        table_start = table_match.start()
        table_end_match = re.search(r"</table>", body_html[table_start:], re.IGNORECASE)
        if not table_end_match:
            continue
        table_html = body_html[table_start:table_start + table_end_match.end()]
        header_cells = re.findall(r"<th[^>]*>(.*?)</th>", table_html, re.DOTALL | re.IGNORECASE)
        header_texts = [strip_tags(c).strip().lower() for c in header_cells]
        if "goldimage version" in header_texts:
            first_tr_end = re.search(r"</tr>", body_html[table_start:], re.IGNORECASE)
            if first_tr_end:
                return table_start + first_tr_end.end(), True
    return -1, False


def format_jira_macro(ticket_key):
    """Format a Jira ticket as a Confluence Jira issue macro."""
    return (
        f'<ac:structured-macro ac:name="jira">'
        f'<ac:parameter ac:name="key">{html_escape(ticket_key)}</ac:parameter>'
        f'</ac:structured-macro>'
    )


def build_table_html(releases, jira_base_url):
    """Build a full HTML table from the enriched releases."""
    header_cells = "".join(f"<th>{c}</th>" for c in TABLE_COLUMNS)
    header_row = f"<tr>{header_cells}</tr>"

    rows = []
    for r in releases:
        ticket = r["main_tickets"]
        if ticket != "N/A":
            ticket_cell = format_jira_macro(ticket)
        else:
            ticket_cell = "N/A"

        if r["changelog_url"]:
            changelog_url = html_escape(r["changelog_url"])
            changelog_cell = f'<a href="{changelog_url}">{changelog_url}</a>'
        else:
            changelog_cell = "Data not found"

        if r["rpm_url"]:
            rpm_url = html_escape(r["rpm_url"])
            rpm_cell = f'<a href="{rpm_url}">{rpm_url}</a>'
        else:
            rpm_cell = "Data not found"

        row = (
            "<tr>"
            f"<td>{html_escape(r['goldimage_version'])}</td>"
            f"<td>{ticket_cell}</td>"
            f"<td>{changelog_cell}</td>"
            f"<td>{rpm_cell}</td>"
            f"<td>{html_escape(r['merge_date'])}</td>"
            f"<td>{html_escape(r['notes'])}</td>"
            "</tr>"
        )
        rows.append(row)

    return (
        '<table class="wrapped"><colgroup>'
        "<col /><col /><col /><col /><col /><col />"
        "</colgroup><tbody>"
        + header_row
        + "\n".join(rows)
        + "</tbody></table>"
    )


def build_row_html(release, jira_base_url):
    """Build a single table row."""
    r = release
    ticket = r["main_tickets"]
    if ticket != "N/A":
        ticket_cell = format_jira_macro(ticket)
    else:
        ticket_cell = "N/A"

    if r["changelog_url"]:
        changelog_url = html_escape(r["changelog_url"])
        changelog_cell = f'<a href="{changelog_url}">{changelog_url}</a>'
    else:
        changelog_cell = "Data not found"

    if r["rpm_url"]:
        rpm_url = html_escape(r["rpm_url"])
        rpm_cell = f'<a href="{rpm_url}">{rpm_url}</a>'
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
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Update Confluence GoldImage release table"
    )
    parser.add_argument("--input-json", required=True,
                        help="Path to release extractor JSON (from github_release_extractor_graphql.py)")
    parser.add_argument("--branch", required=True,
                        help="Branch name (used for Notes column and Jira search)")
    parser.add_argument("--max-releases", type=int, default=0,
                        help="Max releases to process (0 = all)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview changes without updating Confluence")
    args = parser.parse_args()

    confluence_base_url = get_env_or_exit("CONFLUENCE_BASE_URL")
    confluence_token = get_env_or_exit("CONFLUENCE_API_TOKEN")
    page_id = get_env_or_exit("CONFLUENCE_PAGE_ID")
    jira_base_url = get_env_or_exit("JIRA_BASE_URL")
    jira_token = get_env_or_exit("JIRA_API_TOKEN")

    print("=" * 50)
    print("  GoldImage Confluence Table Updater")
    print("=" * 50)
    print(f"  Input JSON : {args.input_json}")
    print(f"  Branch     : {args.branch}")
    print(f"  Page ID    : {page_id}")
    print(f"  Mode       : append (deduplicate)")
    print("=" * 50)

    # Step 1: Enrich release data with Jira Epics
    print("\n[1/3] Enriching releases with Jira Epic tickets...")
    releases = enrich_releases(args.input_json, args.branch, jira_base_url, jira_token)

    if not releases:
        print("No releases found in input JSON.", file=sys.stderr)
        sys.exit(1)

    if args.max_releases:
        releases = releases[:args.max_releases]

    print(f"\n  Total releases to process: {len(releases)}")

    # Step 2: Get current Confluence page
    print("\n[2/3] Fetching current Confluence page...")
    page = get_page(confluence_base_url, confluence_token, page_id)
    page_title = page.get("title", "")
    version = page.get("version", {}).get("number", 0)
    body_html = page.get("body", {}).get("storage", {}).get("value", "")
    print(f"  Page title : {page_title}")
    print(f"  Version    : {version}")

    # Step 3: Append new rows (never remove existing entries)
    print("\n[3/3] Updating Confluence page...")

    existing_versions = extract_existing_goldimage_versions(body_html)
    new_releases = [r for r in releases if r["goldimage_version"] not in existing_versions]
    skipped = [r for r in releases if r["goldimage_version"] in existing_versions]

    if not new_releases:
        print(f"\n  All {len(skipped)} release(s) already exist. Nothing to update.")
        for r in skipped:
            print(f"    - {r['goldimage_version']}")
        sys.exit(0)

    insert_pos, table_found = find_table_insertion_point(body_html)
    new_rows_html = "\n".join(build_row_html(r, jira_base_url) for r in new_releases)

    if table_found:
        new_body = body_html[:insert_pos] + "\n" + new_rows_html + "\n" + body_html[insert_pos:]
    else:
        new_body = build_table_html(new_releases, jira_base_url)

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
    print(f"  Added   : {len(new_releases)} row(s)")
    if skipped:
        print(f"  Skipped : {len(skipped)} (already exist)")

    print("\n  Releases added:")
    for r in new_releases:
        print(f"    + {r['goldimage_version']} | {r['main_tickets']} | {r['merge_date']}")


if __name__ == "__main__":
    main()
