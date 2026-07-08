"""Stage 7: Jenkins publish to endor + URL rewrite."""

import re
import urllib.request

from src.config import (
    BASE_URL,
    ENDOR_AOS_RHEL9_MASTER, ENDOR_AOS_STS_BASE, ENDOR_AOS_RHEL8_BASE,
    ENDOR_PC_MASTER, ENDOR_PC_STS_BASE, ENDOR_CACHE_BASE,
)
from src.logger import Log
from src.version import _parse_rhel8_version
from tools.jenkins_tool import (
    trigger_build, resolve_queue_to_build, wait_for_build, DEFAULT_JOB,
)


def _build_endor_base_dir(version_str, release_type, branch):
    """Build the endor-relative directory path using the ENDOR_* constants."""
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
            return f"{ENDOR_PC_STS_BASE}/{pc_sub}/{version_str}".lstrip("/")
        else:
            pc_branch = "master"
            if "rhel8" in version_str:
                m = re.search(r"ganges-(pc\.[\d.]+)-rhel", version_str)
                if m:
                    pc_branch = m.group(1)
            return f"{ENDOR_PC_MASTER}/{pc_branch}/{version_str}".lstrip("/")
    elif "rhel8" in version_str:
        info = _parse_rhel8_version(version_str)
        if info:
            dir_name = (
                f"RHEL{info['rhel_major']}{info['rhel_minor']}-SVM-"
                f"{info['rhel_major']}.{info['rhel_minor']}-k{info['kernel']}-"
                f"r{info['release']}.x86_64"
            )
            return f"{ENDOR_AOS_RHEL8_BASE}/{info['branch_ver']}/{dir_name}".lstrip("/")
        return f"{ENDOR_AOS_RHEL8_BASE}/{version_str}".lstrip("/")
    elif branch_ver:
        return f"{ENDOR_AOS_STS_BASE}/{branch_ver}/{version_str}".lstrip("/")
    else:
        return f"{ENDOR_AOS_RHEL9_MASTER}/{version_str}".lstrip("/")


def _derive_endor_params(row):
    """Derive SOURCE_URL, DESTINATION, and full_path for PUBLISH_GOLD_IMAGE."""
    version = row.get("goldimage_version", "")
    rtype = row.get("type", "AOS").lower()
    branch = row.get("branch") or row.get("notes", "master")
    if not version:
        return None, None, None

    full_path = _build_endor_base_dir(version, rtype, branch)
    destination = full_path.rsplit("/", 1)[0]

    hoth_http_base = BASE_URL.rstrip("/")
    source_url = f"{hoth_http_base}/{full_path}/"

    return source_url, destination, full_path


def _exists_on_endor(destination):
    """Check if a release directory already exists on endor-cache-2."""
    url = f"{ENDOR_CACHE_BASE}/{destination}/"
    req = urllib.request.Request(url, method="HEAD")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status == 200
    except Exception:
        return False


PC_TARBALL_BRANCHES = {"ganges-7.3", "ganges-7.5"}


def rewrite_urls_to_endor(rows, branch):
    """Rewrite changelog_url and rpm_url in each row to point to endor-cache-2."""
    for row in rows:
        version = row.get("goldimage_version", "")
        rtype = row.get("type", "AOS").lower()
        if not version:
            continue
        full_path = _build_endor_base_dir(version, rtype, branch)
        endor_base = f"{ENDOR_CACHE_BASE}/{full_path}"
        row["changelog_url"] = f"{endor_base}/changelog.txt"
        row["rpm_url"] = f"{endor_base}/rpm.txt"
        if branch in PC_TARBALL_BRANCHES and rtype == "pc":
            row["gi_tarball_url"] = f"{endor_base}/pcvm.tar.xz"


def publish_to_endor(rows, filter_type="all", dry_run=False, force=False):
    """Trigger Jenkins PUBLISH_GOLD_IMAGE for each uploaded release version."""
    allowed = {"aos": {"AOS"}, "pc": {"PC"}, "all": {"AOS", "PC"}}
    allowed_types = allowed.get(filter_type, {"AOS", "PC"})

    results = []
    seen_versions = set()

    for row in rows:
        rtype = row.get("type", "AOS").upper()
        if rtype not in allowed_types:
            continue

        version = row.get("goldimage_version", "unknown")
        source_url, destination, full_path = _derive_endor_params(row)
        if not source_url or not destination or not full_path:
            Log.error(f"[{rtype}] Endor publish skipped for {version}: no valid URL")
            continue

        if full_path in seen_versions:
            continue
        seen_versions.add(full_path)

        if _exists_on_endor(full_path) and not force:
            Log.info(f"[{rtype}] Already on endor, skipping: {full_path}")
            results.append({
                "rtype": rtype, "version": version,
                "destination": destination, "skipped": True,
            })
            continue

        force_update = "true" if force else "false"
        params = {
            "SOURCE_URL": source_url,
            "DESTINATION": destination,
            "Force_update": "false",
        }

        if dry_run:
            Log.info(f"[{rtype}] [DRY RUN] Would publish {version} → {destination}")
            results.append({
                "rtype": rtype, "version": version,
                "source_url": source_url, "destination": destination,
                "dry_run": True,
            })
            continue

        Log.info(f"[{rtype}] Publishing {version} to endor: {destination}")
        queue_url, msg = trigger_build(DEFAULT_JOB, params)
        if not queue_url:
            Log.error(f"[{rtype}] FAILED to trigger build: {msg}")
            results.append({
                "rtype": rtype, "version": version,
                "source_url": source_url, "destination": destination,
                "success": False, "error": msg,
            })
            continue

        Log.info(f"[{rtype}] Build queued: {queue_url}")

        Log.info(f"[{rtype}] Waiting for build to start...")
        build_number = resolve_queue_to_build(queue_url)
        if not build_number:
            Log.error(f"[{rtype}] FAILED: build never started (queue timeout)")
            results.append({
                "rtype": rtype, "version": version,
                "source_url": source_url, "destination": destination,
                "success": False, "error": "queue timeout",
            })
            continue

        Log.info(f"[{rtype}] Build #{build_number} started, waiting for completion...")

        build_result = wait_for_build(DEFAULT_JOB, build_number)
        result_str = build_result.get("result", "UNKNOWN")
        duration_s = build_result.get("duration", 0) // 1000

        if build_result["success"]:
            Log.info(f"[{rtype}] Build #{build_number} SUCCESS ({duration_s}s) — {version}")
            results.append({
                "rtype": rtype, "version": version,
                "source_url": source_url, "destination": destination,
                "success": True, "build_number": build_number,
                "duration": duration_s,
            })
        else:
            Log.error(f"[{rtype}] Build #{build_number} FAILED: {result_str} — {version}")
            Log.error(f"[{rtype}] {build_result.get('error', '')}")
            results.append({
                "rtype": rtype, "version": version,
                "source_url": source_url, "destination": destination,
                "success": False, "build_number": build_number,
                "error": result_str,
            })

    return results
