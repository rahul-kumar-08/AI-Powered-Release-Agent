#!/usr/bin/env python3
"""
Jira Tool — Self-contained HTTP client for Jira Epic search.

Reads the release JSON produced by github_tool.py / github_release_extractor_graphql.py,
identifies the AOS version for each release, and queries Jira to find the
corresponding Epic ticket.

All public functions raise typed exceptions from tools.exceptions so the
orchestrator can retry transient failures.

Usage:
  python3 tools/jira_tool.py --input-json <path> --branch <branch>
  python3 tools/jira_tool.py --input-json /tmp/release_graphql_master_10.json --branch master

Environment variables (from tools/.env):
  JIRA_BASE_URL   — Jira server URL (e.g. https://jira.nutanix.com)
  JIRA_API_TOKEN  — Jira personal access token (Bearer token)
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

ENDOR_GI_ROOT = "http://endor.dyn.nutanix.com/GoldImages"


def get_endor_base(branch="master", comp_type="AOS"):
    """Return the endor base URL for a given branch and component type.

    AOS: master -> .../Centos_SVM/Master       ganges-X.Y -> .../Centos_SVM/STS/X.Y
    PC:  master -> .../PC_GoldImages/pc/master  ganges-X.Y -> .../PC_GoldImages/pc/pc.X.Y
    """
    if comp_type == "PC":
        if branch == "master":
            return f"{ENDOR_GI_ROOT}/PC_GoldImages/pc/master"
        m = re.match(r"ganges-(\d+\.\d+)", branch)
        if m:
            return f"{ENDOR_GI_ROOT}/PC_GoldImages/pc/pc.{m.group(1)}"
        return f"{ENDOR_GI_ROOT}/PC_GoldImages/pc/master"
    # AOS
    if branch == "master":
        return f"{ENDOR_GI_ROOT}/Centos_SVM/Master"
    m = re.match(r"ganges-(\d+\.\d+)", branch)
    if m:
        return f"{ENDOR_GI_ROOT}/Centos_SVM/STS/{m.group(1)}"
    return f"{ENDOR_GI_ROOT}/Centos_SVM/Master"

KERNEL_MAP = {
    "9": "5.14.0",
}


# ---------------------------------------------------------------------------
# Environment helpers
# ---------------------------------------------------------------------------

_env_file_loaded = False


def _load_env_file(env_path="tools/.env"):
    """Load .env file once, only as fallback when runtime env lacks a variable."""
    global _env_file_loaded
    if _env_file_loaded:
        return
    _env_file_loaded = True
    if not os.path.isfile(env_path):
        return
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            val = val.strip().strip("\"'")
            os.environ.setdefault(key.strip(), val)


def load_env(env_path="tools/.env"):
    """Compatibility wrapper — triggers lazy .env load."""
    _load_env_file(env_path)


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
# HTTP helpers
# ---------------------------------------------------------------------------

def _jira_request(base_url, token, method, path, body=None, timeout=30):
    """Low-level Jira HTTP call. Raises typed exceptions."""
    url = f"{base_url.rstrip('/')}{path}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    data = json.dumps(body).encode("utf-8") if body else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
        except Exception:
            pass
        msg = f"Jira HTTP {exc.code} on {method} {path}: {detail}"
        if exc.code in (401, 403):
            raise AuthError(msg, status_code=exc.code)
        if exc.code == 429:
            raise RateLimitError(msg, retry_after=exc.headers.get("Retry-After"))
        raise HttpError(msg, status_code=exc.code)
    except urllib.error.URLError as exc:
        raise NetworkError(f"Jira network error on {method} {path}: {exc}")


# ---------------------------------------------------------------------------
# Jira search functions
# ---------------------------------------------------------------------------

def jira_search(base_url, token, jql, fields="key,summary", max_results=10):
    """Execute a JQL search. Returns list of issue dicts."""
    params = urllib.parse.urlencode({
        "jql": jql,
        "fields": fields,
        "maxResults": max_results,
    })
    data = _jira_request(base_url, token, "GET", f"/rest/api/2/search?{params}")
    return data.get("issues", [])


def find_aos_epic(base_url, token, version_string):
    """
    Search Jira for an AOS Epic matching the release version.
    Returns the Epic key (e.g. "AOS-12345") or None.
    """
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
    """Extract AOS version from a release PR title."""
    match = re.search(
        r"[Rr]elease [Gg]old [Ii]mage (main-master-rhel[\d.]+-[\d.]+)",
        release_title,
    )
    return match.group(1) if match else None


def parse_version_parts(aos_version):
    """Parse main-master-rhel9.7-9.0.0 -> (rhel_major, rhel_minor, release_version)."""
    match = re.match(r"main-master-rhel(\d+)\.(\d+)-([\d.]+)", aos_version)
    if match:
        return int(match.group(1)), int(match.group(2)), match.group(3)
    return None


def get_kernel_version(rhel_major):
    return KERNEL_MAP.get(str(rhel_major), "5.14.0")


def build_goldimage_version(aos_version, kernel_version):
    """Insert kernel version: main-master-rhel9.7-9.0.0 -> main-master-rhel9.7-5.14.0-9.0.0"""
    match = re.match(r"(main-master-rhel[\d.]+)-([\d.]+)", aos_version)
    if match:
        return f"{match.group(1)}-{kernel_version}-{match.group(2)}"
    return aos_version


def build_endor_url(rhel_major, rhel_minor, kernel_version, release_version, filename, branch="master", comp_type="AOS", version=None):
    if comp_type == "PC" and branch != "master" and version:
        folder = version
    elif comp_type == "PC":
        rhel_tag = f"RHEL{rhel_major}{rhel_minor}"
        folder = f"{rhel_tag}-PC-{rhel_major}.{rhel_minor}-k{kernel_version}-r{release_version}"
    else:
        rhel_tag = f"RHEL{rhel_major}{rhel_minor}"
        folder = f"{rhel_tag}-SVM-{rhel_major}.{rhel_minor}-k{kernel_version}-r{release_version}.x86_64"
    return f"{get_endor_base(branch, comp_type)}/{folder}/{filename}"


def format_merge_date(iso_date):
    try:
        dt = datetime.strptime(iso_date[:19], "%Y-%m-%dT%H:%M:%S")
        return dt.strftime("%d-%b-%Y")
    except (ValueError, TypeError):
        return iso_date or "N/A"


def check_url_exists(url):
    """HEAD-check a URL. Returns False for 404 or network errors."""
    req = urllib.request.Request(url, method="HEAD")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status < 400
    except urllib.error.HTTPError as e:
        return e.code < 400
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Main CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Jira Tool — AOS Epic search")
    parser.add_argument("--input-json", required=True, help="Path to release extractor JSON")
    parser.add_argument("--branch", required=True, help="Branch name (for Notes column)")
    parser.add_argument("--output-json", help="Save enriched output to JSON")
    parser.add_argument("--format", choices=["table", "json"], default="table",
                        help="Output format (default: table)")
    args = parser.parse_args()

    jira_base_url = _require_env("JIRA_BASE_URL")
    jira_token = _require_env("JIRA_API_TOKEN")

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
        raise DataError("No releases found in the input JSON.")


if __name__ == "__main__":
    try:
        main()
    except ToolError as exc:
        print(f"Error [{type(exc).__name__}]: {exc}", file=sys.stderr)
        sys.exit(2 if isinstance(exc, ConfigError) else 1)
