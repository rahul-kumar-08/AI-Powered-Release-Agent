"""Stage 8: Confluence upload."""

from src.config import _get_env
from src.logger import Log


def upload_to_confluence(rows, branch, filter_type="all", force_rebuild=False):
    """Upload release rows to Confluence using a single parent page."""
    try:
        from tools.mcp_confluence_client import upload_releases
    except ImportError:
        Log.error("Confluence upload skipped: tools.mcp_confluence_client not available")
        return []

    fallback_id = _get_env("CONFLUENCE_PAGE_ID")
    page_id_map = {"AOS": fallback_id, "PC": fallback_id}

    if not any(page_id_map.values()):
        Log.error("Confluence upload skipped: no page ID set in tools/.env "
                  "(need CONFLUENCE_PAGE_ID)")
        return []

    types_in_rows = set(r.get("type", "AOS").upper() for r in rows)
    if filter_type == "aos":
        types_in_rows &= {"AOS"}
    elif filter_type == "pc":
        types_in_rows &= {"PC"}

    results = []
    for rtype in sorted(types_in_rows):
        parent_id = page_id_map.get(rtype)
        if not parent_id:
            Log.error(f"[{rtype}] Confluence upload skipped: no page ID configured")
            continue
        type_rows = [r for r in rows if r.get("type", "AOS").upper() == rtype]
        if not type_rows:
            continue
        try:
            result = upload_releases(
                "atlassian",
                parent_id=parent_id,
                branch=branch,
                rows=type_rows,
                release_type=rtype,
                force_rebuild=force_rebuild,
                dry_run=False,
            )
            result["release_type"] = rtype
            results.append(result)
            Log.info(f"[{rtype}] Confluence: +{result.get('added', 0)} rows, "
                     f"{result.get('skipped', 0)} skipped, "
                     f"{result.get('total', 0)} total on page {result.get('page_id', '?')}")
        except Exception as e:
            Log.error(f"[{rtype}] Confluence upload error: {e}")
            results.append({"release_type": rtype, "added": 0, "error": str(e)})

    return results
