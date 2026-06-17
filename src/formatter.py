"""Output formatting: table, markdown, JSON."""

import json

from src.config import _log
from src.version import validate_url


def format_table(rows, validate_urls=False, with_github_date=False, with_sg_date=False):
    """Format rows as the standard GoldImage release table."""
    if not rows:
        print("No releases found.")
        return

    if validate_urls:
        _log("Validating URLs...")
        for row in rows:
            row["changelog_valid"] = validate_url(row["changelog_url"])
            row["rpm_valid"] = validate_url(row["rpm_url"])
            if not row["changelog_valid"]:
                row["changelog_url"] = "Data not found"
            if not row["rpm_valid"]:
                row["rpm_url"] = "Data not found"

    hdr = f"| {'GoldImage Version':<45} | {'Main Ticket':<14} | {'Change Log':<90} | {'RPM List':<90} | {'Merge Date':<12} |"
    sep = f"|{'-'*47}|{'-'*16}|{'-'*92}|{'-'*92}|{'-'*14}|"
    if with_github_date:
        hdr += f" {'PR Merge Date':<14} |"
        sep += f"{'-'*16}|"
    if with_sg_date:
        hdr += f" {'CR Merge Date':<14} |"
        sep += f"{'-'*16}|"
    hdr += f" {'Notes':<8} |"
    sep += f"{'-'*10}|"

    print(f"\n{hdr}")
    print(sep)

    for row in rows:
        line = f"| {row['goldimage_version']:<45} | {row['main_ticket']:<14} | {row['changelog_url']:<90} | {row['rpm_url']:<90} | {row['merge_date']:<12} |"
        if with_github_date:
            line += f" {row.get('github_date', 'N/A'):<14} |"
        if with_sg_date:
            line += f" {row.get('sg_date', 'N/A'):<14} |"
        line += f" {row['notes']:<8} |"
        print(line)

    print(f"\nTotal: {len(rows)} release(s)")


def format_markdown(rows, validate_urls=False, with_github_date=False, with_sg_date=False):
    """Format as a cleaner markdown table with linked URLs."""
    if not rows:
        print("No releases found.")
        return

    if validate_urls:
        _log("Validating URLs...")
        for row in rows:
            if not validate_url(row["changelog_url"]):
                row["changelog_url"] = ""
            if not validate_url(row["rpm_url"]):
                row["rpm_url"] = ""

    hdr = "| GoldImage Version | Main Ticket | Change Log | RPM List | Merge Date |"
    sep = "|---|---|---|---|---|"
    if with_github_date:
        hdr += " PR Merge Date |"
        sep += "---|"
    if with_sg_date:
        hdr += " CR Merge Date |"
        sep += "---|"
    hdr += " Notes |"
    sep += "---|"

    print(f"\n{hdr}")
    print(sep)

    for row in rows:
        cl = f"[changelog]({row['changelog_url']})" if row["changelog_url"] else "Data not found"
        rpm = f"[rpm]({row['rpm_url']})" if row["rpm_url"] else "Data not found"
        line = f"| {row['goldimage_version']} | {row['main_ticket']} | {cl} | {rpm} | {row['merge_date']} |"
        if with_github_date:
            line += f" {row.get('github_date', 'N/A')} |"
        if with_sg_date:
            line += f" {row.get('sg_date', 'N/A')} |"
        line += f" {row['notes']} |"
        print(line)

    print(f"\nTotal: {len(rows)} release(s)")


def format_json(rows, output_path=None):
    """Output as JSON — dict keyed by goldimage version."""
    keyed = {row.get("goldimage_version", "unknown"): row for row in rows}
    data = json.dumps(keyed, indent=2)
    if output_path:
        with open(output_path, "w") as f:
            f.write(data)
        _log(f"Saved {len(rows)} releases to: {output_path}")
    else:
        print(data)
