#!/usr/bin/env python3
"""
Search Jira for AOS Epic tickets based on release versions extracted from GitHub.

Reads the release JSON produced by github_release_extractor_graphql.py,
identifies the AOS version for each release, and queries Jira to find the
corresponding Epic ticket.

Outputs a table in the format:
  GoldImage Version | Main Tickets | Change Log | RPM List | Merge Date | Notes

Usage:
  python3 search_jira_epic.py --input-json <path> --branch <branch>
  python3 search_jira_epic.py --input-json /tmp/release_graphql_master_10.json --branch master

Environment variables (from src/.env):
  JIRA_BASE_URL   — Jira server URL (e.g. https://jira.nutanix.com)
  JIRA_API_TOKEN  — Jira personal access token (Bearer token)
"""

import argparse
import json
import os
import re
import sys
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime

ENDOR_BASE_URL = "http://endor.dyn.nutanix.com/GoldImages/Centos_SVM/Master"

KERNEL_MAP = {
    "9": "5.14.0",
}


def get_env_or_exit(name):
    val = os.environ.get(name, "").strip()
    if not val:
        print(f"Error: {name} is not set. Add it to src/.env or export it.", file=sys.stderr)
        sys.exit(2)
    return val


def jira_search(base_url, token, jql, fields="key,summary", max_results=10):
    """Execute a JQL search and return the list of issues."""
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
        print(f"Jira API error: {e.code} {e.reason}", file=sys.stderr)
        return []
    except Exception as e:
        print(f"Jira request failed: {e}", file=sys.stderr)
        return []


def find_aos_epic(base_url, token, version_string):
    """
    Search Jira for an AOS Epic matching the release version.
    version_string example: main-master-rhel9.7-9.0.0
    """
    jql = (
        f'issuetype = Epic AND summary ~ "Release gold image {version_string}"'
    )
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


def extract_aos_version(release_title):
    """
    Extract the AOS version from a release PR title.
    Example: "Release gold image main-master-rhel9.7-9.0.0/PC:..." -> "main-master-rhel9.7-9.0.0"
    """
    match = re.search(
        r"[Rr]elease [Gg]old [Ii]mage (main-master-rhel[\d.]+-[\d.]+)",
        release_title
    )
    if match:
        return match.group(1)
    return None


def parse_version_parts(aos_version):
    """
    Parse main-master-rhel9.7-9.0.0 into (rhel_major, rhel_minor, release_version).
    Returns (9, 7, "9.0.0") or None.
    """
    match = re.match(r"main-master-rhel(\d+)\.(\d+)-([\d.]+)", aos_version)
    if match:
        return int(match.group(1)), int(match.group(2)), match.group(3)
    return None


def get_kernel_version(rhel_major):
    """Get the kernel version for a given RHEL major version."""
    return KERNEL_MAP.get(str(rhel_major), "5.14.0")


def build_goldimage_version(aos_version, kernel_version):
    """
    Build the GoldImage version string.
    main-master-rhel9.7-9.0.0 + kernel 5.14.0 -> main-master-rhel9.7-5.14.0-9.0.0
    """
    match = re.match(r"(main-master-rhel[\d.]+)-([\d.]+)", aos_version)
    if match:
        prefix = match.group(1)
        release = match.group(2)
        return f"{prefix}-{kernel_version}-{release}"
    return aos_version


def build_endor_url(rhel_major, rhel_minor, kernel_version, release_version, filename):
    """
    Build the endor URL for changelog.txt or rpm.txt.
    Pattern: RHEL97-SVM-9.7-k5.14.0-r9.0.0.x86_64/
    """
    rhel_tag = f"RHEL{rhel_major}{rhel_minor}"
    folder = f"{rhel_tag}-SVM-{rhel_major}.{rhel_minor}-k{kernel_version}-r{release_version}.x86_64"
    return f"{ENDOR_BASE_URL}/{folder}/{filename}"


def format_merge_date(iso_date):
    """Convert ISO date to DD-Mon-YYYY format."""
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


def main():
    parser = argparse.ArgumentParser(description="Search Jira for AOS Epic tickets")
    parser.add_argument("--input-json", required=True, help="Path to release extractor JSON")
    parser.add_argument("--branch", required=True, help="Branch name (for Notes column)")
    parser.add_argument("--output-json", help="Optional: save enriched output to JSON")
    parser.add_argument("--format", choices=["table", "json"], default="table",
                        help="Output format (default: table)")
    args = parser.parse_args()

    jira_base_url = get_env_or_exit("JIRA_BASE_URL")
    jira_token = get_env_or_exit("JIRA_API_TOKEN")

    with open(args.input_json) as f:
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

        rhel_major, rhel_minor, release_version = parts
        kernel_version = get_kernel_version(rhel_major)

        epic_key = find_aos_epic(jira_base_url, jira_token, aos_version)

        goldimage_version = build_goldimage_version(aos_version, kernel_version)
        changelog_url = build_endor_url(rhel_major, rhel_minor, kernel_version, release_version, "changelog.txt")
        rpm_url = build_endor_url(rhel_major, rhel_minor, kernel_version, release_version, "rpm.txt")

        if check_url_exists(changelog_url):
            merge_date = format_merge_date(merged_at)
        else:
            changelog_url = None
            rpm_url = None
            merge_date = ""

        results.append({
            "goldimage_version": goldimage_version,
            "main_tickets": epic_key or "N/A",
            "changelog_url": changelog_url,
            "rpm_url": rpm_url,
            "merge_date": merge_date,
            "notes": args.branch,
            "release_key": release_key,
            "merged_at_raw": merged_at,
        })

    results.sort(key=lambda x: x["merged_at_raw"], reverse=True)

    if args.format == "json" or args.output_json:
        output = json.dumps(results, indent=2)
        if args.output_json:
            with open(args.output_json, "w") as f:
                f.write(output)
            print(f"Saved enriched JSON to: {args.output_json}", file=sys.stderr)
        if args.format == "json":
            print(output)

    if args.format == "table":
        print("| GoldImage Version | Main Tickets | Change Log | RPM List | Merge Date | Notes |")
        print("|---|---|---|---|---|---|")
        for r in results:
            changelog_display = r["changelog_url"] or "Data not found"
            rpm_display = r["rpm_url"] or "Data not found"
            print(
                f"| {r['goldimage_version']} "
                f"| {r['main_tickets']} "
                f"| {changelog_display} "
                f"| {rpm_display} "
                f"| {r['merge_date']} "
                f"| {r['notes']} |"
            )

    if not results:
        print("No releases found in the input JSON.", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
