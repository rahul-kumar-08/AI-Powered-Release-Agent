"""Stage 3: Version parsing, validation, and release row building."""

import re
import urllib.request
from datetime import datetime

from src.config import (
    _get_env,
    BASE_URL, GITHUB_REPO,
    ENDOR_AOS_RHEL9_MASTER, ENDOR_AOS_STS_BASE, ENDOR_AOS_RHEL8_BASE,
    ENDOR_PC_MASTER, ENDOR_PC_STS_BASE,
)
from src.logger import Log
from src.jira_client import search_jira_epic, validate_version_with_jira

PC_TARBALL_BRANCHES = {"ganges-7.3", "ganges-7.5"}


def _needs_gi_tarball(branch, release_type):
    """GI tarball only applies to PC on ganges-7.3 and ganges-7.5."""
    return branch in PC_TARBALL_BRANCHES and release_type.lower() == "pc"


def _parse_rhel8_version(version_str):
    """Parse rhel8 VERSION_GI into components for Endor URL construction."""
    m = re.match(
        r"^(?:main|sts)-(?:ganges-(?:pc\.)?)([\d.]+)-rhel(\d+)\.(\d+)-([\d.]+)-([\d.]+)$",
        version_str,
    )
    if m:
        return {
            "branch_ver": m.group(1),
            "rhel_major": m.group(2),
            "rhel_minor": m.group(3),
            "kernel": m.group(4),
            "release": m.group(5),
        }
    return None


def build_endor_urls(version_str, release_type, branch):
    """Construct changelog and RPM URLs based on release type, RHEL version, and branch."""
    branch_ver = None
    if branch and branch != "master":
        m = re.match(r"ganges-([\d.]+)", branch)
        if m:
            branch_ver = m.group(1)

    if release_type == "pc":
        if branch_ver:
            pc_sub = f"pc.{branch_ver}"
            if "rhel8" in version_str:
                m = re.search(r"ganges-(pc\.[\d.]+)-rhel", version_str)
                if m:
                    pc_sub = m.group(1)
            base_dir = f"{BASE_URL}{ENDOR_PC_STS_BASE}/{pc_sub}/{version_str}"
        else:
            pc_branch = "master"
            if "rhel8" in version_str:
                m = re.search(r"ganges-(pc\.[\d.]+)-rhel", version_str)
                if m:
                    pc_branch = m.group(1)
            base_dir = f"{BASE_URL}{ENDOR_PC_MASTER}/{pc_branch}/{version_str}"
    elif "rhel8" in version_str:
        info = _parse_rhel8_version(version_str)
        if info:
            dir_name = (
                f"RHEL{info['rhel_major']}{info['rhel_minor']}-SVM-"
                f"{info['rhel_major']}.{info['rhel_minor']}-k{info['kernel']}-"
                f"r{info['release']}.x86_64"
            )
            base_dir = f"{BASE_URL}{ENDOR_AOS_RHEL8_BASE}/{info['branch_ver']}/{dir_name}"
        else:
            base_dir = f"{BASE_URL}{ENDOR_AOS_RHEL8_BASE}/{version_str}"
    elif branch_ver:
        base_dir = f"{BASE_URL}{ENDOR_AOS_STS_BASE}/{branch_ver}/{version_str}"
    else:
        base_dir = f"{BASE_URL}{ENDOR_AOS_RHEL9_MASTER}/{version_str}"

    urls = {
        "changelog": f"{base_dir}/changelog.txt",
        "rpm": f"{base_dir}/rpm.txt",
    }

    if _needs_gi_tarball(branch, release_type):
        urls["gi_tarball"] = f"{base_dir}/pcvm.tar.xz"

    return urls


def validate_url(url):
    """HEAD-check a URL, return True if accessible."""
    req = urllib.request.Request(url, method="HEAD")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status == 200
    except Exception:
        return False


def format_merge_date(date_str):
    """Convert ISO date to DD-Mon-YYYY (e.g. '27-May-2026')."""
    if not date_str or date_str == "N/A":
        return "N/A"
    try:
        dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        return dt.strftime("%d-%b-%Y")
    except (ValueError, AttributeError):
        try:
            dt = datetime.strptime(date_str[:10], "%Y-%m-%d")
            return dt.strftime("%d-%b-%Y")
        except Exception:
            return date_str


def _extract_heading_versions(title_clean):
    """Extract GoldImage versions from commit heading (source of truth)."""
    heading_aos = None
    heading_pc = None

    pc_split = re.split(r"/PC\s*:\s*", title_clean, maxsplit=1)
    if len(pc_split) == 2:
        aos_part = pc_split[0].strip()
        pc_part = pc_split[1].strip()
        heading_aos = re.sub(r"(?i)^release\s+gold\s+image\s+", "", aos_part).strip()
        heading_pc = re.sub(r"(?i)^release\s+gold\s+image\s+", "", pc_part).strip()
        return {"aos": _clean_version(heading_aos), "pc": _clean_version(heading_pc)}

    combined_split = re.split(r"/Release\s+[Gg]old\s+image\s+", title_clean, maxsplit=1)
    if len(combined_split) == 2:
        aos_part = combined_split[0].strip()
        pc_part = combined_split[1].strip()
        heading_aos = re.sub(r"(?i)^release\s+gold\s+image\s+", "", aos_part).strip()
        heading_pc = pc_part.strip()
        return {"aos": _clean_version(heading_aos), "pc": _clean_version(heading_pc)}

    single = re.sub(r"(?i)^release\s+gold\s+image\s+", "", title_clean).strip()
    single = re.sub(r"\s*\(#\d+\)$", "", single).strip()

    if "/" in single:
        parts = single.split("/", 1)
        left = parts[0].strip()
        right = parts[1].strip()
        if re.match(r"^main-", left) and re.match(r"^main-", right):
            return {"aos": _clean_version(left), "pc": _clean_version(right)}

    if single:
        if "ganges-pc" in single.lower() or re.match(r".*-pc\.", single.lower()):
            heading_pc = single
        else:
            heading_aos = single

    return {"aos": _clean_version(heading_aos), "pc": _clean_version(heading_pc)}


def _clean_version(ver):
    """Strip trailing PR number references and whitespace from a version string."""
    if not ver:
        return None
    ver = re.sub(r"\s*\(#\d+\)$", "", ver).strip()
    return ver if ver else None


def _extract_rhel_suffix(v):
    """'main-ganges-7.6-rhel9.7-9.1.0' -> 'rhel9.7-9.1.0'"""
    m = re.search(r'(rhel\d+\.\d+-\d+\.\d+\.\d+)$', v)
    return m.group(1) if m else None


def _extract_num_suffix(v):
    """'main-ganges-7.6-rhel9.7-9.1.0' -> '9.1.0'"""
    m = re.search(r'(\d+\.\d+\.\d+)$', v)
    return m.group(1) if m else None


def _match_gerrit_and_extract(version, gerrit_commits, excluded_titles,
                              github_message, release_type="AOS"):
    """Match the best Gerrit commit for a single version and extract changelog fields."""
    if not version:
        return {
            "release_pr_link": "",
            "gerrit_cr_url": "",
            "gerrit_date": None,
            "tickets_resolved": [],
            "associated_prs": [],
            "commit_message": github_message,
        }

    tickets_match = re.search(
        r'Tickets?\s+Resolved\s*:\s*(.+?)(?:\n|$)', github_message, re.IGNORECASE)
    tickets_resolved = (
        [t.strip() for t in tickets_match.group(1).split(",") if t.strip()]
        if tickets_match else []
    )

    associated_prs = []
    prs_block = re.search(
        r"PR'?s?\s+included\s+in\s+GI\s*[:;](.*?)(?:\n\n|\nChange-Id:|\nEpic|\nTarget|\Z)",
        github_message, re.IGNORECASE | re.DOTALL)
    if prs_block:
        for line in prs_block.group(1).strip().split("\n"):
            line = line.strip().lstrip("-").strip()
            if line and ("github.com" in line or line.startswith("http")):
                associated_prs.append(line)

    pr_num_match = re.search(r'\(#(\d+)\)\s*$', github_message.split("\n")[0])
    release_pr_link = (
        f"https://{GITHUB_REPO}/pull/{pr_num_match.group(1)}"
        if pr_num_match else ""
    )

    _is_pc = release_type.upper() == "PC"

    def _score(gc_title):
        score = 0
        if version in gc_title:
            score += 100
        elif _extract_rhel_suffix(version) and _extract_rhel_suffix(version) in gc_title:
            score += 10
        elif _extract_num_suffix(version) and _extract_num_suffix(version) in gc_title:
            score += 1
        if score == 0:
            return 0
        has_pc_prefix = bool(re.match(r'^PC\s*:', gc_title, re.IGNORECASE))
        if _is_pc and has_pc_prefix:
            score += 50
        elif not _is_pc and not has_pc_prefix:
            score += 50
        return score

    best_gc = None
    best_score = 0
    for gc in gerrit_commits:
        gc_title = gc.get("title", "")
        if gc_title in excluded_titles:
            continue
        s = _score(gc_title)
        if s > best_score:
            best_score = s
            best_gc = gc

    gerrit_cr_url = ""
    gerrit_date = None
    if best_gc:
        gerrit_cr_url = best_gc.get("url", "")
        gerrit_date = best_gc.get("date")
        gc_msg = best_gc.get("message", "")
        tm = re.search(r'Tickets?\s+Resolved\s*:\s*(.+?)(?:\n|$)',
                       gc_msg, re.IGNORECASE)
        if tm:
            tickets_resolved = [
                t.strip() for t in tm.group(1).split(",") if t.strip()]
        pb = re.search(
            r"PR'?s?\s+included\s+in\s+GI\s*[:;](.*?)(?:\n\n|\nChange-Id:|\Z)",
            gc_msg, re.IGNORECASE | re.DOTALL)
        if pb:
            prs_from_gerrit = []
            for line in pb.group(1).strip().split("\n"):
                line = line.strip().lstrip("-").strip()
                if line and ("github.com" in line or line.startswith("http")):
                    prs_from_gerrit.append(line)
            if prs_from_gerrit:
                associated_prs = prs_from_gerrit
        rp = re.search(r'GI\s+Release\s+PR\s*:\s*(https?://\S+)',
                       gc_msg, re.IGNORECASE)
        if rp:
            release_pr_link = rp.group(1)

    return {
        "release_pr_link": release_pr_link,
        "gerrit_cr_url": gerrit_cr_url,
        "gerrit_date": gerrit_date,
        "tickets_resolved": tickets_resolved,
        "associated_prs": associated_prs,
        "commit_message": github_message,
    }


def _is_valid_ticket(key):
    """Check if a ticket ID looks complete (not truncated). Valid: ENG-887591, Invalid: ENG-9."""
    if not key or key == "--":
        return False
    m = re.match(r"^ENG-(\d+)$", key)
    return m is not None and len(m.group(1)) >= 5



def _resolve_epic(rtype, gerrit_epics, gh_epics, version, message, is_combined):
    """Resolve EPIC ticket with priority:
    1. Explicit Epic's field (Gerrit or GitHub)
    2. Jira EPIC search (using raw VERSION_GI)
    3. Tickets Resolved first relevant ticket
    """

    if gh_epics:
        if is_combined and len(gh_epics) >= 2:
            candidate = gh_epics[1] if rtype == "pc" else gh_epics[0]
        else:
            candidate = gh_epics[0]
        if _is_valid_ticket(candidate):
            return candidate

    if gerrit_epics:
        if is_combined and len(gerrit_epics) >= 2:
            candidate = gerrit_epics[1] if rtype == "pc" else gerrit_epics[0]
        else:
            candidate = gerrit_epics[0]
        if _is_valid_ticket(candidate):
            return candidate

    epic = search_jira_epic(version, rtype)
    if epic:
        return epic

    tickets_match = re.search(r"Tickets?\s*Resolved\s*:\s*(.+?)(?:\n|$)", message, re.IGNORECASE)
    if tickets_match:
        all_tix = re.findall(r"(ENG-\d+)", tickets_match.group(1))
        if all_tix:
            return all_tix[0]

    return "--"


def parse_releases(server_key, github_commits, gerrit_commits, github_epics, branch, filter_type):
    """Parse release commits using CR commit heading as the sole source of truth for GoldImage Version."""
    

    reverted_titles = set()
    remerged_titles = set()

    all_commits = sorted(
        github_commits + gerrit_commits,
        key=lambda x: x.get("date", ""),
    )

    for c in all_commits:
        title = c.get("title", "")
        if title.startswith("Revert"):
            m = re.search(r'"(.+?)"', title)
            if m:
                reverted_raw = m.group(1)
                reverted_clean = re.sub(r"\s*\(#\d+\)$", "", reverted_raw).strip()
                reverted_titles.add(reverted_clean)
        elif re.match(r"^(Release|PC\s*:)", title, re.IGNORECASE):
            title_clean = re.sub(r"\s*\(#\d+\)$", "", title).strip()
            if title_clean in reverted_titles:
                remerged_titles.add(title_clean)

    excluded_titles = reverted_titles - remerged_titles

    rows = []
    seen_versions = set()

    sorted_commits = sorted(github_commits, key=lambda x: x.get("date", ""), reverse=True)

    for c in sorted_commits:
        title = c.get("title", "")
        message = c.get("message", "")
        date = c.get("date", "N/A")
        commit_sha = c.get("commit", "")

        if title.startswith("Revert"):
            continue
        title_clean = re.sub(r"\s*\(#\d+\)$", "", title).strip()
        if title_clean in excluded_titles:
            continue
        if not re.match(r"^(Release|PC\s*:)", title, re.IGNORECASE):
            continue

        heading_versions = _extract_heading_versions(title_clean)
        heading_aos = heading_versions.get("aos")
        heading_pc = heading_versions.get("pc")

        aos_validation = validate_version_with_jira(heading_aos, None, "aos") if heading_aos else None
        pc_validation = validate_version_with_jira(heading_pc, None, "pc") if heading_pc else None

        aos_version = aos_validation["confirmed_version"] if aos_validation else None
        pc_version = pc_validation["confirmed_version"] if pc_validation else None

        if not aos_version and not pc_version:
            Log.info(f"Skipping {commit_sha[:8]}: no version in heading")
            continue

        epic_match = re.search(r"Epic'?s?\s*:\s*(.+?)(?:\n|$)", message, re.IGNORECASE)
        gerrit_epics = re.findall(r"(ENG-\d+)", epic_match.group(1)) if epic_match else []

        gh_epics = []
        if title_clean in github_epics:
            gh_epics = github_epics[title_clean]
        else:
            for gh_title, epics in github_epics.items():
                if aos_version and aos_version in gh_title:
                    aos_part = gh_title.split("/PC:")[0] if "/PC:" in gh_title else gh_title
                    if aos_version in aos_part:
                        gh_epics = epics
                        break

        is_combined = bool(aos_version and pc_version)

        _aos_extra = _match_gerrit_and_extract(
            aos_version, gerrit_commits, excluded_titles, message,
            release_type="AOS"
        ) if aos_version else None
        _pc_extra = _match_gerrit_and_extract(
            pc_version, gerrit_commits, excluded_titles, message,
            release_type="PC"
        ) if pc_version else None

        if aos_version and filter_type in ("all", "aos") and (aos_version, "AOS") not in seen_versions:
            seen_versions.add((aos_version, "AOS"))
            aos_epic = _resolve_epic(
                "aos", gerrit_epics, gh_epics, aos_version, message, is_combined=is_combined
            )
            if not _is_valid_ticket(aos_epic):
                aos_epic = (aos_validation.get("epic_key") if aos_validation else None)
                if not aos_epic or not _is_valid_ticket(aos_epic):
                    aos_epic = "--"
            urls = build_endor_urls(aos_version, "aos", branch)
            row_data = {
                "goldimage_version": aos_version,
                "type": "AOS",
                "main_ticket": aos_epic,
                "changelog_url": urls["changelog"],
                "rpm_url": urls["rpm"],
                "merge_date": "N/A",
                "github_date": format_merge_date(date),
                "sg_date": "N/A",
                "gerrit_date": None,
                "notes": branch,
                "commit": commit_sha,
            }
            row_data.update(_aos_extra)
            rows.append(row_data)

        if pc_version and filter_type in ("all", "pc") and (pc_version, "PC") not in seen_versions:
            seen_versions.add((pc_version, "PC"))
            pc_epic = _resolve_epic(
                "pc", gerrit_epics, gh_epics, pc_version, message, is_combined=is_combined
            )
            if not _is_valid_ticket(pc_epic):
                pc_epic = (pc_validation.get("epic_key") if pc_validation else None)
                if not pc_epic or not _is_valid_ticket(pc_epic):
                    pc_epic = "--"
            urls = build_endor_urls(pc_version, "pc", branch)
            row_data = {
                "goldimage_version": pc_version,
                "type": "PC",
                "main_ticket": pc_epic,
                "changelog_url": urls["changelog"],
                "rpm_url": urls["rpm"],
                "merge_date": "N/A",
                "github_date": format_merge_date(date),
                "sg_date": "N/A",
                "gerrit_date": None,
                "notes": branch,
                "commit": commit_sha,
            }
            if urls.get("gi_tarball"):
                row_data["gi_tarball_url"] = urls["gi_tarball"]
            row_data.update(_pc_extra)
            rows.append(row_data)

    return rows, []
