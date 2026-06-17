"""Stage 5: Changelog generation from template."""

import os
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed

from src.config import _log, GITHUB_REPO
from src.jira_client import fetch_ticket_summaries, fetch_gerrit_cr_from_jira
from src.artifactory import _extract_build_number

_TEMPLATE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "templates", "changelog.template",
)


def _load_template():
    """Read the changelog template file."""
    with open(_TEMPLATE_PATH) as f:
        return f.read()


def _fill_template(template, row, prev_version, ticket_summaries=None):
    """Populate the changelog template with release row data."""
    ticket_summaries = ticket_summaries or {}
    current_ver = row.get("goldimage_version", "N/A")
    main_ticket = row.get("main_ticket", "--")
    m = re.search(r'([A-Z]+-\d+)', main_ticket)
    main_ticket_key = m.group(1) if m else main_ticket
    release_pr = row.get("release_pr_link", "")
    gerrit_cr = row.get("gerrit_cr_url", "")
    associated_prs = row.get("associated_prs", [])
    tickets = row.get("tickets_resolved", [])

    text = template
    text = text.replace("{Current_Gold_Image_Version}", current_ver)
    text = text.replace("{Previous_Gold_Image_Version}", prev_version)
    text = text.replace("{MAIN_JIRA_EPIC}", main_ticket_key)
    text = text.replace("{RELEASE_PR_LINK}", release_pr)
    text = text.replace("{GERRIT_CR_LINK}", gerrit_cr)

    prs_block = re.search(
        r'\{%\s*for\s+PR\s+in\s+associated_prs\s*%\}(.*?)\{%\s*endfor\s*%\}',
        text, re.DOTALL)
    if prs_block:
        if associated_prs:
            pr_lines = "\n".join(f"- {pr}" for pr in associated_prs)
        else:
            pr_lines = "- N/A"
        text = text[:prs_block.start()] + pr_lines + text[prs_block.end():]

    tkt_block = re.search(
        r'\{%\s*for\s+jira_id\s+in\s+JIRA_TICKETS_FOR_PR\s*%\}(.*?)\{%\s*else\s*%\}',
        text, re.DOTALL)
    if tkt_block:
        if tickets:
            tkt_lines = []
            for t in tickets:
                key = t.split()[0].rstrip(" -:")
                summary = ticket_summaries.get(key, "")
                label = f"{key} - {summary}" if summary else t
                if "Release" in label:
                    continue
                tkt_lines.append(label)
            tkt_text = "\n".join(tkt_lines) if tkt_lines else "N/A"
        else:
            tkt_text = "N/A"
        text = text[:tkt_block.start()] + tkt_text + text[tkt_block.end():]

    return text


def generate_changelog(rows, prev_rows, output_dir, filter_type="all",
                       branch="master"):
    """Generate changelog.txt for each release version."""
    template = _load_template()

    all_ticket_keys = set()
    for row in rows:
        for t in row.get("tickets_resolved", []):
            key = t.split()[0].rstrip(" -:")
            if re.match(r'^[A-Z]+-\d+$', key):
                all_ticket_keys.add(key)
    ticket_summaries = {}
    if all_ticket_keys:
        _log(f"Fetching Jira summaries for {len(all_ticket_keys)} ticket(s)...")
        ticket_summaries = fetch_ticket_summaries(list(all_ticket_keys))

    _log("Fetching Gerrit CR URLs and merged dates from Jira ticket comments...")
    cr_cache = {}

    def _fetch_cr_for_row(row):
        ver = row.get("goldimage_version", "")
        tickets = row.get("tickets_resolved", [])
        keys = [t.split()[0].rstrip(" -:") for t in tickets
                if re.match(r'^[A-Z]+-\d+$', t.split()[0].rstrip(" -:"))]
        main_tkt = row.get("main_ticket", "")
        epic_match = re.search(r'([A-Z]+-\d+)', main_tkt)
        if epic_match:
            epic_key = epic_match.group(1)
            if epic_key not in keys:
                keys.append(epic_key)
        if not keys:
            return ver, {"cr_url": "", "merged_date": None}
        cache_key = tuple(sorted(keys))
        if cache_key in cr_cache:
            return ver, cr_cache[cache_key]
        result = fetch_gerrit_cr_from_jira(keys, branch)
        cr_cache[cache_key] = result
        return ver, result

    cr_data = {}
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(_fetch_cr_for_row, r): r for r in rows}
        for fut in as_completed(futures):
            ver, result = fut.result()
            if result.get("cr_url") or result.get("merged_date"):
                cr_data[ver] = result

    generated = []
    ci_key_map = {"AOS": "ci_cvm", "PC": "ci_pcvm"}

    for row in rows:
        version = row.get("goldimage_version", "unknown")
        rtype = row.get("type", "AOS")
        ci_key = ci_key_map.get(rtype, "ci_cvm")

        if version in cr_data:
            vdata = cr_data[version]
            if vdata.get("cr_url"):
                row["gerrit_cr_url"] = vdata["cr_url"]

        build_num = _extract_build_number(
            row.get(ci_key, {}).get("url", ""))
        if not build_num:
            continue

        dest_dir = os.path.join(output_dir, version, rtype)
        changelog_path = os.path.join(dest_dir, "changelog.txt")
        rpm_path = os.path.join(dest_dir, "rpm.txt")
        old_rpm_path = os.path.join(dest_dir, "old_rpm.txt")

        prev_row = prev_rows.get(rtype, {}).get(version)
        prev_version = (prev_row.get("goldimage_version", "N/A")
                        if prev_row else "N/A")

        content = _fill_template(template, row, prev_version,
                                 ticket_summaries)

        os.makedirs(dest_dir, exist_ok=True)
        with open(changelog_path, "w") as f:
            f.write(content)

        if os.path.isfile(old_rpm_path) and os.path.isfile(rpm_path):
            try:
                result = subprocess.run(
                    ["diff", "-y", old_rpm_path, rpm_path,
                     "--suppress-common-lines"],
                    capture_output=True, text=True, timeout=30,
                )
                diff_output = result.stdout
                if diff_output:
                    with open(changelog_path, "a") as f:
                        f.write(diff_output)
                    _log(f"[{rtype}] changelog.txt: {version} "
                         f"({len(diff_output.splitlines())} diff lines)")
                else:
                    with open(changelog_path, "a") as f:
                        f.write("(no RPM changes)\n")
                    _log(f"[{rtype}] changelog.txt: {version} "
                         f"(no RPM changes)")
            except (subprocess.TimeoutExpired, OSError) as e:
                _log(f"[{rtype}] diff failed for {version}: {e}")
        else:
            _log(f"[{rtype}] changelog.txt: {version} "
                 f"(rpm files missing, skipping diff)")

        generated.append({"rtype": rtype, "version": version,
                          "path": changelog_path})

    return generated
