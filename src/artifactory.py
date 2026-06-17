"""Stage 4: RPM download from Artifactory."""

import json
import os
import re
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

from src.config import _log, _get_env, ARTIFACTORY_BASE, ARTIFACTORY_API_STORAGE


def _extract_build_number(ci_url):
    """Extract the CircleCI build number from a target_url."""
    if not ci_url:
        return None
    m = re.search(r'/(\d+)$', ci_url)
    return m.group(1) if m else None


_rpm_url_cache = {}


def _resolve_rpm_url(build_num, rtype, art_token=None):
    """Resolve the rpm.txt download URL by listing the build directory."""
    cache_key = build_num
    if cache_key not in _rpm_url_cache:
        _rpm_url_cache[cache_key] = _list_build_dir(build_num, art_token)

    children = _rpm_url_cache[cache_key]
    base = ARTIFACTORY_BASE.format(build_num=build_num)
    prefix = "pcvm" if rtype.upper() == "PC" else "cvm"

    for uri in children:
        if re.search(r'rpm\.txt$', uri) and uri.startswith(f"/{prefix}-"):
            return f"{base}{uri}"

    for uri in children:
        if uri == "/rpm.txt":
            return f"{base}/rpm.txt"

    return None


def _list_build_dir(build_num, art_token=None):
    """List file URIs inside a build-artifacts directory."""
    dir_url = ARTIFACTORY_API_STORAGE.format(build_num=build_num)
    req = urllib.request.Request(dir_url)
    if art_token:
        req.add_header("Authorization", f"Bearer {art_token}")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
        return [c.get("uri", "") for c in data.get("children", [])]
    except Exception:
        return []


def _download_one_rpm(url, dest_path, art_token=None):
    """Download a single rpm.txt file. Returns (dest_path, size) or None."""
    dest_dir = os.path.dirname(dest_path)
    os.makedirs(dest_dir, exist_ok=True)
    req = urllib.request.Request(url)
    if art_token:
        req.add_header("Authorization", f"Bearer {art_token}")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = resp.read()
        with open(dest_path, "wb") as f:
            f.write(data)
        return dest_path, len(data)
    except (urllib.error.HTTPError, urllib.error.URLError, OSError) as e:
        _log(f"  Download failed for {dest_path}: {e}")
        return None


def download_rpm_artifacts(rows, prev_rows, output_dir="goldimage",
                           filter_type="all"):
    """Download rpm.txt and old_rpm.txt from Artifactory for AOS and/or PC."""
    art_token = (_get_env("ARTIFACTORY_TOKEN")
                 or _get_env("ARTIFACTORY_API_KEY"))

    ci_key_for_type = {"AOS": "ci_cvm", "PC": "ci_pcvm"}
    allowed_types = {"aos": {"AOS"}, "pc": {"PC"}, "all": {"AOS", "PC"}}
    allowed = allowed_types.get(filter_type, {"AOS", "PC"})
    tasks = []

    _log("Resolving Artifactory RPM URLs...")

    for row in rows:
        version = row.get("goldimage_version", "unknown")
        rtype = row.get("type", "AOS").upper()
        if rtype not in allowed:
            continue
        ci_key = ci_key_for_type.get(rtype, "ci_cvm")

        build_num = _extract_build_number(
            row.get(ci_key, {}).get("url", ""))
        if build_num:
            url = _resolve_rpm_url(build_num, rtype, art_token)
            if url:
                dest = os.path.join(output_dir, version, rtype, "rpm.txt")
                tasks.append((rtype, version, "rpm.txt", url, dest))

        prev_row = prev_rows.get(rtype, {}).get(version)
        if prev_row:
            prev_build = _extract_build_number(
                prev_row.get(ci_key, {}).get("url", ""))
            if prev_build:
                prev_url = _resolve_rpm_url(prev_build, rtype, art_token)
                if prev_url:
                    prev_ver = prev_row.get("goldimage_version", "unknown")
                    prev_dest = os.path.join(
                        output_dir, version, rtype, "old_rpm.txt")
                    tasks.append((rtype, version,
                                  f"old_rpm.txt (from {prev_ver})",
                                  prev_url, prev_dest))

    downloaded = []
    if not tasks:
        _log("No builds to download")
        return downloaded

    _log(f"Downloading {len(tasks)} file(s) in parallel...")

    with ThreadPoolExecutor(max_workers=4) as pool:
        future_map = {}
        for rtype, version, label, url, dest in tasks:
            _log(f"[{rtype}] {label} build → {os.path.basename(dest)} "
                 f"(version {version})")
            fut = pool.submit(_download_one_rpm, url, dest, art_token)
            future_map[fut] = (rtype, version, label, dest)

        for fut in as_completed(future_map):
            rtype, version, label, dest = future_map[fut]
            result = fut.result()
            if result:
                path, size = result
                downloaded.append({"rtype": rtype, "version": version,
                                   "file": label, "path": path})
                _log(f"[{rtype}] Saved {os.path.basename(dest)} → "
                     f"{path} ({size} bytes)")

    return downloaded
