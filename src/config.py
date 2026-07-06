"""Shared constants and helpers used across all pipeline modules."""

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

ENDOR_AOS_RHEL9_MASTER = "GoldImages/Centos_SVM/Master"
ENDOR_AOS_STS_BASE = "GoldImages/Centos_SVM/STS"
ENDOR_AOS_RHEL8_BASE = "GoldImages/Centos_SVM/STS"
ENDOR_PC_MASTER = "GoldImages/PC_GoldImages/pc"
ENDOR_PC_STS_BASE = "GoldImages/PC_GoldImages/pc"

ENDOR_CACHE_BASE = "https://endor-cache-2.corp.nutanix.com/GoldImages"


def mcp_call_tool(server_key, tool_name, params):
    """Thin wrapper around the MCP call_tool function."""
    return _mcp_call_tool(server_key, tool_name, params)
