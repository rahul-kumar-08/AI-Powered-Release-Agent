"""Output formatting: table, markdown, JSON."""

import json
import shutil

import pandas as pd

from src.logger import Log
from src.version import validate_url

FIXED_COLS = ["GoldImage Version", "Main Ticket", "Merge Date", "Notes"]
URL_COLS = ["Change Log", "RPM List", "GI tarball"]


def _terminal_width():
    return shutil.get_terminal_size((120, 24)).columns


def _compute_maxcolwidths(columns):
    """Compute per-column max widths that fit the terminal.

    Fixed-width columns keep their natural size; remaining space is
    split evenly among URL columns (Change Log, RPM List).
    """
    tw = _terminal_width()
    ncols = len(columns)
    separators = (ncols + 1) * 3

    fixed_widths = {
        "GoldImage Version": 45,
        "Main Ticket": 14,
        "Merge Date": 12,
        "PR Merge Date": 14,
        "CR Merge Date": 14,
        "Notes": 10,
    }

    fixed_total = sum(fixed_widths.get(c, 15) for c in columns if c not in URL_COLS)
    url_count = sum(1 for c in columns if c in URL_COLS)
    remaining = tw - fixed_total - separators
    url_width = max(30, remaining // url_count) if url_count else 30

    return [fixed_widths.get(c, url_width) if c not in URL_COLS else url_width
            for c in columns]


def _build_records(rows, validate_urls_flag, with_github_date, with_sg_date, link_style=False):
    """Build a list of dicts suitable for a DataFrame."""
    if validate_urls_flag:
        Log.info("Validating URLs...")
        for row in rows:
            cl_ok = validate_url(row["changelog_url"])
            rpm_ok = validate_url(row["rpm_url"])
            if not cl_ok:
                row["changelog_url"] = "" if link_style else "Data not found"
            if not rpm_ok:
                row["rpm_url"] = "" if link_style else "Data not found"

    types_present = {r.get("type", "AOS").upper() for r in rows}
    # Tag types when mixed OR when version name doesn't clearly indicate the type
    tag_types = len(types_present) > 1 or any(
        r.get("type", "AOS").upper() == "PC" and "-pc" not in r.get("goldimage_version", "").lower()
        for r in rows
    )

    has_tarball = any(row.get("gi_tarball_url") for row in rows)

    records = []
    for row in rows:
        if link_style:
            cl = f"[changelog]({row['changelog_url']})" if row["changelog_url"] else "Data not found"
            rpm = f"[rpm]({row['rpm_url']})" if row["rpm_url"] else "Data not found"
        else:
            cl = row["changelog_url"]
            rpm = row["rpm_url"]
        version_display = row["goldimage_version"]
        if tag_types:
            rtype = row.get("type", "AOS").upper()
            version_display = f"{version_display} ({rtype})"
        rec = {
            "GoldImage Version": version_display,
            "Main Ticket": row["main_ticket"],
            "Change Log": cl,
            "RPM List": rpm,
        }
        if has_tarball:
            tarball_url = row.get("gi_tarball_url", "")
            if link_style:
                rec["GI tarball"] = (f"[pcvm.tar.xz]({tarball_url})"
                                     if tarball_url else "Data not found")
            else:
                rec["GI tarball"] = tarball_url or "Data not found"
        rec["Merge Date"] = row["merge_date"]
        if with_github_date:
            rec["PR Merge Date"] = row.get("github_date", "N/A")
        if with_sg_date:
            rec["CR Merge Date"] = row.get("sg_date", "N/A")
        rec["Notes"] = row["notes"]
        records.append(rec)
    return records


def format_table(rows, validate_urls=False, with_github_date=False, with_sg_date=False):
    """Format rows as the standard GoldImage release table."""
    if not rows:
        print("No releases found.")
        return

    records = _build_records(rows, validate_urls, with_github_date, with_sg_date)
    df = pd.DataFrame(records)
    maxcol = _compute_maxcolwidths(df.columns.tolist())
    print(f"\n{df.to_markdown(index=False, maxcolwidths=maxcol)}")
    print(f"\nTotal: {len(rows)} release(s)")


def format_markdown(rows, validate_urls=False, with_github_date=False, with_sg_date=False):
    """Format as a cleaner markdown table with linked URLs."""
    if not rows:
        print("No releases found.")
        return

    records = _build_records(rows, validate_urls, with_github_date, with_sg_date, link_style=True)
    df = pd.DataFrame(records)
    maxcol = _compute_maxcolwidths(df.columns.tolist())
    print(f"\n{df.to_markdown(index=False, maxcolwidths=maxcol)}")


def format_json(rows, output_path=None):
    """Output as JSON — dict keyed by goldimage version."""
    keyed = {row.get("goldimage_version", "unknown"): row for row in rows}
    data = json.dumps(keyed, indent=2)
    if output_path:
        with open(output_path, "w") as f:
            f.write(data)
        Log.info(f"Saved {len(rows)} releases to: {output_path}")
    else:
        print(data)
