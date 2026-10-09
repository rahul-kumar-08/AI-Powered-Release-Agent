"""Shared constants and helpers used across all pipeline modules."""

import re
import sys

from tools.mcp_client import call_tool as _mcp_call_tool, _get_env
from tools.mcp_sourcegraph_client import TOOL_PREFIX
from tools.mcp_github_client import fetch_postmerge_ci  # noqa: F401 — re-exported
from src.logger import Log  # noqa: F401 — re-exported for convenience

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_SERVER_KEY = "gw-sourcegraph"

DEFAULT_REPO = _get_env("DEFAULT_REPO")
GITHUB_REPO = _get_env("GITHUB_REPO")
BASE_URL = _get_env("BASE_URL")
ARTIFACTORY_BASE = _get_env("ARTIFACTORY_BASE")
ARTIFACTORY_API_STORAGE = _get_env("ARTIFACTORY_API_STORAGE")

ENDOR_AOS_RHEL9_MASTER = "Centos_SVM/Master"
ENDOR_AOS_STS_BASE = "Centos_SVM/STS"
ENDOR_AOS_RHEL8_BASE = "Centos_SVM/STS"
ENDOR_PC_MASTER = "PC_GoldImages/pc"
ENDOR_PC_STS_BASE = "PC_GoldImages/pc"

ENDOR_CACHE_BASE = "https://endor-cache-2.corp.nutanix.com/GoldImages"

PC_TARBALL_BRANCHES = {"ganges-7.3", "ganges-7.5"}

# ganges-7.6.0.x is a GitHub branch. The trailing x is a wildcard, not a
# numeric component. Confluence pages use the capital-X form.
_GANGES_BRANCH_RE = re.compile(r"^ganges-((?:\d+\.)*\d+)(?:\.([xX]))?$")


def normalize_branch_name(branch):
    """Normalize a branch name. ``ganges-7.6.0.X`` → ``ganges-7.6.0.x``."""
    if not branch:
        return branch or ""
    return re.sub(r"\.[xX]$", ".x", str(branch).strip())


def parse_ganges_branch(branch):
    """Split a ganges branch into (page_version, fix_prefix).

    ``ganges-7.6`` → ``("7.6", "7.6")``
    ``ganges-7.6.0.x`` → ``("7.6.0.x", "7.6.0")``
    ``master`` and anything else → ``("", "")``
    """
    normalized = normalize_branch_name(branch)
    m = _GANGES_BRANCH_RE.match(normalized)
    if not m:
        return "", ""
    numeric = m.group(1)
    if m.group(2):
        return f"{numeric}.x", numeric
    return numeric, numeric


def confluence_page_version(branch):
    """Version token used in Confluence page titles.

    Wildcard lines keep a capital X to match titles such as
    ``Modern STS - 7.6.0.X`` and ``PC.7.6.0.X``.
    """
    page, _fix = parse_ganges_branch(branch)
    if page.lower().endswith(".x"):
        return page[:-1] + "X"
    return page


def endor_branch_ver(branch, version_str=""):
    """STS/PC directory segment for Endor.

    Prefer the version embedded in the goldimage string
    (``main-ganges-7.6-rhel…`` → ``7.6``). Wildcard branches such as
    ``ganges-7.6.0.x`` publish under that product line, not under
    ``7.6.0.x``.
    """
    m = re.search(r"ganges-(?:pc\.)?(\d+(?:\.\d+)+)-rhel", version_str or "")
    if m:
        return m.group(1)
    page, fix = parse_ganges_branch(branch)
    if page.lower().endswith(".x"):
        parts = fix.split(".")
        if len(parts) >= 2:
            return ".".join(parts[:2])
    return page


def preserves_confluence_layout(branch):
    """Keep an existing Confluence table layout and fill Release from Jira.

    Used for ganges-7.3, ganges-7.5, and wildcard lines such as
    ``ganges-7.6.0.x``.
    """
    normalized = normalize_branch_name(branch)
    if normalized in PC_TARBALL_BRANCHES:
        return True
    page, _fix = parse_ganges_branch(normalized)
    return page.lower().endswith(".x")


def tracker_matches_branch(branch, gerrit_branch, jira_ver):
    """Match a git-tracker comment to a wildcard branch such as ganges-7.6.0.x.

    ``ganges-7.6.0.x`` matches Gerrit ``ganges-7.6.0.10-stable`` and Jira
    ``7.6.0.10`` / ``pc.7.6.0.10``. It does not match ``ganges-7.6-stable``
    or ``7.6.1.*``.
    """
    branch = normalize_branch_name(branch)
    gerrit_branch = (gerrit_branch or "").strip()
    jira_ver = (jira_ver or "").strip()
    _page, fix = parse_ganges_branch(branch)
    if not fix:
        return False
    gb = gerrit_branch.lower()
    jv = jira_ver.lower()
    pl = fix.lower()
    token = f"ganges-{pl}"
    if jv and (
        jv == pl
        or jv == f"pc.{pl}"
        or jv.startswith(pl + ".")
        or jv.startswith(f"pc.{pl}.")
    ):
        return True
    bl = branch.lower()
    if gb == bl or gb.startswith(bl):
        return True
    if gb == token or gb.startswith(token + "-") or gb.startswith(token + "."):
        return True
    return False


def mcp_call_tool(server_key, tool_name, params):
    """Thin wrapper around the MCP call_tool function."""
    return _mcp_call_tool(server_key, tool_name, params)
