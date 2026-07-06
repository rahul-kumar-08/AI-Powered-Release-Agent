#!/usr/bin/env python3
"""
Agent Runner — receives a mission string, calls Cursor SDK to
decompose it into steps, then dispatches each step to the right tool.

The primary tool is ``release_query.py`` which runs the full pipeline:
  - Extract releases from Sourcegraph/GitHub
  - Fetch CircleCI postmerge status
  - Download RPM artifacts from Artifactory
  - Generate changelogs from template
  - Upload changelog/rpm files to SFTP
  - Update Confluence release tables

Uses the cursor-sdk Python package (pip install cursor-sdk).
Requires Python 3.10+.

Usage:
    python3 agent_runner.py "Extract last 5 releases from master"
    python3 agent_runner.py "Get releases from ganges-7.5 and update confluence"
    python3 agent_runner.py --dry-run "Full pipeline for ganges-7.5 last 3 releases"

Requires:
    CURSOR_API_KEY  — Cursor Dashboard -> Integrations
    GITHUB_TOKEN, SOURCEGRAPH_TOKEN — for release extraction (in tools/.env)
    JIRA_*, CONFLUENCE_* — for Jira/Confluence (in tools/.env)
    SFTP_*, ARTIFACTORY_* — for file upload/download (in tools/.env)
"""

import argparse
import json
import os
import subprocess
import sys
import threading
import time
import re as _re

import pandas as pd

from tools.mcp_client import validate_mcp_tokens
from cursor_sdk import Agent, AgentOptions, CursorAgentError
from tools.mcp_confluence_client import upload_releases, _get_env
from tools.mcp_confluence_client import detect_release_type

MAX_TOOL_RETRIES = 3
RETRY_BACKOFF_BASE = 2

ALL_BRANCHES = ["master", "ganges-7.3", "ganges-7.5", "ganges-7.6"]

DECOMPOSITION_PROMPT_TEMPLATE = """You are a mission planner for a Release Agent. Your ONLY job is to
decompose the user's mission into an ordered list of tool steps.

Available tools and their parameters:

1. release_query — Run the full release pipeline: extract releases from
   Sourcegraph/GitHub, fetch CircleCI status, download RPMs from Artifactory,
   generate changelogs, upload files to SFTP, and update Confluence.
   All stages run by default; use no_* flags to skip individual stages.
   params: branch (string, default "master"),
           count (int, OPTIONAL — when omitted the pipeline auto-determines
                  count by looking up the latest Confluence entry and counting
                  only newer releases since then),
           since_confluence (bool, default false — explicitly trigger
                  Confluence-based auto-count even when count is provided),
           filter (string: "all"|"aos"|"pc", default "all"),
           format (string: "table"|"json"|"markdown", default "table"),
           output (string path, optional — save JSON to file),
           with_github_date (bool, default false — add PR merge date column),
           with_sg_date (bool, default false — add CR merge date column),
           no_ci_status (bool, default false — skip CircleCI fetch),
           no_download_rpm (bool, default false — skip Artifactory RPM download),
           no_generate_changelog (bool, default false — skip changelog generation),
           no_upload (bool, default false — skip SFTP, Jenkins endor, and Confluence),
           force_publish_endor (bool, default false — force republish to endor),
           validate_urls (bool, default false — HEAD-check URLs),
           rpm_dir (string, optional — directory for downloaded RPM files)

2. confluence_update — Re-upload or force-rebuild an existing Confluence page
   from a pre-generated JSON file. Only needed when release_query's built-in
   Confluence upload was skipped (no_upload) or when you need to
   force-rebuild a page.
   params: input_json (string path, required — from release_query JSON output),
           branch (string, required),
           type (string "AOS"|"PC", optional — auto-detected from JSON if omitted),
           force_rebuild (bool, default false),
           dry_run (bool, default false)

Known branches (in order): {all_branches}

Rules:
- Return ONLY a JSON array. No markdown, no explanation, no extra text.
- Each element: {{"step": N, "tool": "<name>", "params": {{...}}}}
- A single release_query step handles the full pipeline including Confluence.
- Do NOT add a separate confluence_update step unless the user explicitly asks
  to force-rebuild or re-upload from existing JSON.
- Default to no_upload=true UNLESS the user explicitly asks to "update",
  "upload", "push to confluence", "publish", or "update confluence".
  Verbs like "get", "extract", "show", "list", "fetch" are read-only
  and MUST include no_upload=true.
- If the user says "force", "forcefully", "force update", or "forcefully update",
  set force_publish_endor=true to re-publish even if the version already exists
  on endor.
- COUNT HANDLING (critical):
  - When the user specifies a number (e.g. "last 5", "3 releases"), set count
    to that number.
  - When the user says "last release", "latest release", or "last 1 release"
    (singular, or the number 1), set count=1.
  - When the user does NOT specify any count or number (e.g. "give me releases
    from master", "releases from ganges-7.6"), do NOT include count in params.
    The pipeline will auto-determine count by looking up Confluence for the
    latest entry and counting only newer releases.
  - NEVER default count to 5. Only include count when the user explicitly
    states a number or says "last"/"latest" (implying 1).
- When the user says "each branch", "all branches", or "every branch", generate
  one release_query step PER branch from the known branches list above.
  Each step must have a different branch. Number steps sequentially.
- When the user says "both AOS and PC" or "AOS and PC", or "each AOS and PC",
  or asks for releases from both types, set filter="all". With filter="all",
  count applies PER TYPE (count=1 gives 1 AOS + 1 PC).
  When the user says only "AOS", set filter="aos". When the user says only
  "PC", set filter="pc".

Examples:

Mission: "Extract last 5 releases from master"
Output:
[{{"step":1,"tool":"release_query","params":{{"branch":"master","count":5,"no_upload":true}}}}]

Mission: "Get releases from ganges-7.5 and update confluence"
Output:
[{{"step":1,"tool":"release_query","params":{{"branch":"ganges-7.5"}}}}]

Mission: "Give me releases from master"
Output:
[{{"step":1,"tool":"release_query","params":{{"branch":"master","no_upload":true}}}}]

Mission: "Releases from ganges-7.6"
Output:
[{{"step":1,"tool":"release_query","params":{{"branch":"ganges-7.6","no_upload":true}}}}]

Mission: "Releases from ganges-7.6 and push to confluence"
Output:
[{{"step":1,"tool":"release_query","params":{{"branch":"ganges-7.6"}}}}]

Mission: "Last 3 PC releases from master"
Output:
[{{"step":1,"tool":"release_query","params":{{"branch":"master","count":3,"filter":"pc","no_upload":true}}}}]

Mission: "Forcefully update last 2 PC releases from ganges-7.6"
Output:
[{{"step":1,"tool":"release_query","params":{{"branch":"ganges-7.6","count":2,"filter":"pc","force_publish_endor":true}}}}]

Mission: "Get last releases from each branch for both PC and AOS"
Output:
[{{"step":1,"tool":"release_query","params":{{"branch":"master","count":1,"filter":"all","no_upload":true}}}},{{"step":2,"tool":"release_query","params":{{"branch":"ganges-7.3","count":1,"filter":"all","no_upload":true}}}},{{"step":3,"tool":"release_query","params":{{"branch":"ganges-7.5","count":1,"filter":"all","no_upload":true}}}},{{"step":4,"tool":"release_query","params":{{"branch":"ganges-7.6","count":1,"filter":"all","no_upload":true}}}}]

Mission: "Latest PC release from every branch"
Output:
[{{"step":1,"tool":"release_query","params":{{"branch":"master","count":1,"filter":"pc","no_upload":true}}}},{{"step":2,"tool":"release_query","params":{{"branch":"ganges-7.3","count":1,"filter":"pc","no_upload":true}}}},{{"step":3,"tool":"release_query","params":{{"branch":"ganges-7.5","count":1,"filter":"pc","no_upload":true}}}},{{"step":4,"tool":"release_query","params":{{"branch":"ganges-7.6","count":1,"filter":"pc","no_upload":true}}}}]

Mission: "Force rebuild confluence page for ganges-7.6 PC releases"
Output:
[{{"step":1,"tool":"release_query","params":{{"branch":"ganges-7.6","count":5,"filter":"pc","format":"json","output":"/tmp/releases_ganges-7.6_5.json","no_upload":true}}}},{{"step":2,"tool":"confluence_update","params":{{"input_json":"/tmp/releases_ganges-7.6_5.json","branch":"ganges-7.6","type":"PC","force_rebuild":true}}}}]

Now decompose this mission:
"""


# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------

def load_env(path="tools/.env"):
    if not os.path.isfile(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            val = val.strip().strip("\"'")
            os.environ.setdefault(key.strip(), val)


# ---------------------------------------------------------------------------
# Cursor SDK decomposition
# ---------------------------------------------------------------------------

def decompose_mission(mission, cursor_key, verbose=False):
    """Use cursor-sdk Agent.prompt() one-shot to decompose mission into steps."""
    prompt = DECOMPOSITION_PROMPT_TEMPLATE.format(
        all_branches=", ".join(ALL_BRANCHES),
    )
    full_prompt = prompt + mission

    if verbose:
        print("[Cursor SDK] Sending mission to Agent.prompt() ...")

    try:
        result = Agent.prompt(
            full_prompt,
            AgentOptions(
                api_key=cursor_key,
                model="claude-sonnet-4",
            ),
        )
        print(f"[Cursor SDK] Agent returned status: {result.status}")
    except CursorAgentError as exc:
        retryable = getattr(exc, "is_retryable", False)
        print(
            f"[Cursor SDK] Agent startup failed: {exc}  retryable={retryable}",
            file=sys.stderr,
        )
        return None

    if verbose:
        print(f"[Cursor SDK] Status: {result.status}")

    if result.status == "error":
        print(f"[Cursor SDK] Run failed: {result.id}", file=sys.stderr)
        return None

    raw = result.result or ""
    if verbose:
        print(f"[Cursor SDK] Raw response ({len(raw)} chars)")

    return parse_steps(raw)


def parse_steps(raw_text):
    """Extract a JSON array from the agent's response text."""
    text = raw_text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        json_lines = []
        inside = False
        for line in lines:
            if line.strip().startswith("```") and not inside:
                inside = True
                continue
            elif line.strip().startswith("```") and inside:
                break
            elif inside:
                json_lines.append(line)
        text = "\n".join(json_lines)

    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end == -1:
        print(f"[Parse] No JSON array found in response:\n{raw_text}", file=sys.stderr)
        return None

    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError as e:
        print(f"[Parse] Invalid JSON: {e}\n{text[start:end + 1]}", file=sys.stderr)
        return None


# ---------------------------------------------------------------------------
# Tool dispatch functions
# ---------------------------------------------------------------------------

def run_release_query(params):
    """Run the full release pipeline via release_query.py."""
    cmd = [
        sys.executable, "release_query.py",
        "--branch", params.get("branch", "master"),
        "--filter", params.get("filter", "all"),
        "--format", params.get("format", "table"),
    ]
    if "count" in params and params["count"] is not None:
        cmd.extend(["--count", str(params["count"])])
    if params.get("since_confluence"):
        cmd.append("--since-confluence")
    output = params.get("output")
    if output:
        cmd.extend(["--output", output])
    rpm_dir = params.get("rpm_dir")
    if rpm_dir:
        cmd.extend(["--rpm-dir", rpm_dir])
    if params.get("with_github_date"):
        cmd.append("--with-github-date")
    if params.get("with_sg_date"):
        cmd.append("--with-sg-date")
    if params.get("no_ci_status"):
        cmd.append("--no-ci-status")
    if params.get("no_download_rpm"):
        cmd.append("--no-download-rpm")
    if params.get("no_generate_changelog"):
        cmd.append("--no-generate-changelog")
    if params.get("no_upload"):
        cmd.append("--no-upload")
    if params.get("force_publish_endor"):
        cmd.append("--force-publish-endor")
    if params.get("validate_urls"):
        cmd.append("--validate-urls")
    return run_subprocess(cmd, stream=True)


def run_confluence_update(params):
    """Re-upload or force-rebuild a Confluence page from pre-generated JSON."""
    input_json = params.get("input_json")
    branch = params.get("branch", "master")
    if not input_json:
        return {"ok": False, "error": "input_json is required"}

    try:
        
        with open(input_json) as f:
            rows = json.load(f)

        release_type = (params.get("type") or "").upper()
        if not release_type:
            
            release_type = detect_release_type(rows)

        aos_page_id = _get_env("AOS_CONFLUENCE_PAGE_ID")
        pc_page_id = _get_env("PC_CONFLUENCE_PAGE_ID")
        fallback_id = _get_env("CONFLUENCE_PAGE_ID")
        page_id_map = {"AOS": aos_page_id or fallback_id,
                       "PC": pc_page_id or fallback_id}
        parent_id = page_id_map.get(release_type, fallback_id)

        if not parent_id:
            return {"ok": False, "error": f"No Confluence page ID set for {release_type} in tools/.env"}

        result = upload_releases(
            "atlassian",
            parent_id=parent_id,
            branch=branch,
            rows=rows,
            release_type=release_type,
            force_rebuild=params.get("force_rebuild", False),
            dry_run=params.get("dry_run", False),
        )
        return {"ok": True, "stdout": json.dumps(result, indent=2)}
    except Exception as e:
        return {"ok": False, "error": str(e), "retryable": False}


def run_subprocess(cmd, stream=False):
    """Execute a subprocess and capture output.

    When *stream* is True, stdout and stderr are printed in real-time
    while also being captured for retry/error checking.
    """
    if stream:
        return _run_streaming(cmd)
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=600,
        )
        retryable = _is_retryable_stderr(result.stderr) if result.returncode != 0 else False
        return {
            "ok": result.returncode == 0,
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "retryable": retryable,
        }
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "Command timed out after 600s", "retryable": True}
    except Exception as e:
        return {"ok": False, "error": str(e), "retryable": False}


def _run_streaming(cmd):
    """Run a subprocess with real-time stdout/stderr streaming."""
    try:
        env = os.environ.copy()
        env["_RELEASE_AGENT_SUBPROCESS"] = "1"
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1, env=env,
        )
        stdout_lines = []
        stderr_lines = []

        def _drain_stderr():
            for line in proc.stderr:
                stderr_lines.append(line)
                print(line, end="", file=sys.stderr, flush=True)

        t = threading.Thread(target=_drain_stderr, daemon=True)
        t.start()

        for line in proc.stdout:
            stdout_lines.append(line)
            print(line, end="", flush=True)

        proc.wait(timeout=600)
        t.join(timeout=5)

        stdout = "".join(stdout_lines)
        stderr = "".join(stderr_lines)
        retryable = _is_retryable_stderr(stderr) if proc.returncode != 0 else False
        return {
            "ok": proc.returncode == 0,
            "returncode": proc.returncode,
            "stdout": stdout,
            "stderr": stderr,
            "retryable": retryable,
            "streamed": True,
        }
    except subprocess.TimeoutExpired:
        proc.kill()
        return {"ok": False, "error": "Command timed out after 600s",
                "retryable": True, "streamed": True}
    except Exception as e:
        return {"ok": False, "error": str(e),
                "retryable": False, "streamed": True}


def _is_retryable_stderr(stderr):
    """Check if the tool's stderr indicates a retryable exception type."""
    retryable_types = ("RateLimitError", "NetworkError", "HttpError")
    for t in retryable_types:
        if t in (stderr or ""):
            return True
    return False


TOOL_REGISTRY = {
    "release_query": run_release_query,
    "confluence_update": run_confluence_update,
}

_DISCREPANCY_KEYWORDS = (
    "mismatch", "skipping", "skip", "failed", "error:",
    "missing", "warning", "not found", "unavailable",
)


def _extract_discrepancies(stderr):
    """Extract discrepancy and warning lines from tool stderr output."""
    if not stderr:
        return []
    seen = set()
    lines = []
    for line in stderr.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        lower = stripped.lower()
        if any(kw in lower for kw in _DISCREPANCY_KEYWORDS):
            if stripped not in seen:
                seen.add(stripped)
                lines.append(stripped)
    return lines




_STAGE_PATTERNS = [
    ("Releases Extracted",
     r"\[release-query\].*?Output: (\d+) rows",
     lambda m: "%s release(s)" % m.group(1)),
    ("Gerrit Commits",
     r"\[release-query\].*?Gerrit: (\d+) commits",
     lambda m: "%s commit(s)" % m.group(1)),
    ("GitHub Commits",
     r"\[release-query\].*?GitHub: (\d+) commits",
     lambda m: "%s commit(s)" % m.group(1)),
    ("CI Status",
     r"\[release-query\].*?CI status fetched for (\d+) unique commit",
     lambda m: "%s commit(s) checked" % m.group(1)),
    ("RPM Download",
     r"\[release-query\].*?Downloading (\d+) file",
     lambda m: "%s file(s) queued" % m.group(1)),
    ("Changelog",
     r"\[release-query\].*?Generating changelog",
     lambda m: "generated"),
    ("SFTP Upload",
     r"\[release-query\].*?SFTP upload complete: (\d+) file",
     lambda m: "%s file(s) uploaded" % m.group(1)),
    ("Endor Publish",
     r"\[release-query\].*?Endor publish complete: (\d+) published, (\d+) already exist, (\d+) failed",
     lambda m: "%s published, %s exist, %s failed" % (m.group(1), m.group(2), m.group(3))),
    ("Confluence",
     r"\[release-query\].*?Confluence upload complete: (\d+) added, (\d+) already exist",
     lambda m: "+%s added, %s skipped" % (m.group(1), m.group(2))),
]


def _extract_stage_stats(stderr):
    """Parse pipeline stage results from release_query stderr output."""
    if not stderr:
        return []
    stages = []
    for label, pattern, fmt in _STAGE_PATTERNS:
        m = _re.search(pattern, stderr)
        if m:
            stages.append((label, fmt(m)))
    # Count successful RPM downloads from "Saved" lines
    saved = len(_re.findall(r"\[release-query\].*?\[[A-Z]+\] Saved", stderr))
    if saved:
        for i, (label, _) in enumerate(stages):
            if label == "RPM Download":
                stages[i] = ("RPM Download", "%d file(s) downloaded" % saved)
                break
    # Count changelog files generated
    changelogs = len(_re.findall(r"\[release-query\].*?\[[A-Z]+\] changelog", stderr))
    if changelogs:
        for i, (label, _) in enumerate(stages):
            if label == "Changelog":
                stages[i] = ("Changelog", "%d file(s) generated" % changelogs)
                break
    return stages


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

def dispatch_steps(steps, dry_run=False, fail_fast=False, verbose=False):
    """Execute each step in order, collecting results."""
    results = []
    total = len(steps)

    for step in steps:
        step_num = step.get("step", "?")
        tool = step.get("tool", "unknown")
        params = step.get("params", {})

        print("\n%s Step %s/%s: %s %s" % ("=" * 20, step_num, total, tool, "=" * 20))
        if verbose:
            print("  Params: %s" % json.dumps(params))

        if tool not in TOOL_REGISTRY:
            msg = "Unknown tool: %s" % tool
            print("  ERROR: %s" % msg)
            results.append({"step": step_num, "tool": tool, "ok": False, "error": msg})
            if fail_fast:
                print("  --fail-fast: aborting.")
                break
            continue

        if dry_run:
            print("  [DRY RUN] Would execute: %s(%s)" % (tool, json.dumps(params)))
            results.append({"step": step_num, "tool": tool, "ok": True, "dry_run": True})
            continue

        handler = TOOL_REGISTRY[tool]
        result = None
        for attempt in range(1, MAX_TOOL_RETRIES + 1):
            result = handler(params)
            if result.get("ok"):
                break
            if result.get("retryable") and attempt < MAX_TOOL_RETRIES:
                wait = RETRY_BACKOFF_BASE ** attempt
                print("  Retryable failure (attempt %d/%d), waiting %ds..."
                      % (attempt, MAX_TOOL_RETRIES, wait))
                time.sleep(wait)
                continue
            break

        stderr = result.get("stderr", "")
        discrepancies = _extract_discrepancies(stderr)
        stage_stats = _extract_stage_stats(stderr)

        if result.get("ok"):
            print("  OK")
            stdout = result.get("stdout", "")
            if not result.get("streamed") and stdout:
                print(stdout)
        else:
            err = result.get("error") or stderr
            print("  FAILED: %s" % err[:500])
            if fail_fast:
                print("  --fail-fast: aborting.")
                results.append({"step": step_num, "tool": tool, "ok": False,
                                "error": err, "discrepancies": discrepancies,
                                "stage_stats": stage_stats})
                break

        results.append({"step": step_num, "tool": tool,
                        "ok": result.get("ok", False),
                        "discrepancies": discrepancies,
                        "stage_stats": stage_stats})

    return results


def print_summary(results):
    """Print final summary with stage status, step results, and discrepancies."""
    # Stage status
    all_stages = []
    for r in results:
        all_stages.extend(r.get("stage_stats", []))

    if all_stages:
        df = pd.DataFrame(all_stages, columns=["Stage", "Result"])
        print("\n%s PIPELINE STATUS %s" % ("=" * 26, "=" * 26))
        print(df.to_markdown(index=False, tablefmt="simple"))
        print("-" * 68)

    # Step results
    step_rows = []
    for r in results:
        status = "OK" if r.get("ok") else "FAILED"
        if r.get("dry_run"):
            status = "DRY RUN"
        step_rows.append((r.get("step", "?"), r.get("tool", "?"), status))
    df = pd.DataFrame(step_rows, columns=["Step", "Tool", "Status"])
    print("\n%s STEP RESULTS %s" % ("=" * 27, "=" * 27))
    print(df.to_markdown(index=False, tablefmt="simple"))
    print("-" * 68)
    ok_count = sum(1 for r in results if r.get("ok"))
    print("Total: %d/%d steps succeeded." % (ok_count, len(results)))

    # Discrepancies
    all_discrepancies = []
    for r in results:
        all_discrepancies.extend(r.get("discrepancies", []))

    if all_discrepancies:
        df = pd.DataFrame(all_discrepancies, columns=["Issue"])
        print("\n%s DISCREPANCIES (%d) %s" % (
            "=" * 23, len(all_discrepancies), "=" * 23))
        print(df.to_markdown(index=False, tablefmt="simple"))
        print("-" * 68)
    else:
        print("\nNo discrepancies detected.")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    load_env()

    parser = argparse.ArgumentParser(
        description="Agent Runner: decompose missions via Cursor, dispatch to tools"
    )
    parser.add_argument(
        "mission", help="Natural language mission string"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Show steps without executing them",
    )
    parser.add_argument(
        "--fail-fast", action="store_true",
        help="Stop on first step failure",
    )
    parser.add_argument(
        "--verbose", action="store_true",
        help="Show detailed output",
    )
    parser.add_argument(
        "--steps-json",
        help="Skip Cursor decomposition — use a pre-built JSON step file instead",
    )
    args = parser.parse_args()

    # Validate MCP server tokens before running any steps
    if not args.dry_run:
        try:
            validate_mcp_tokens()
        except SystemExit:
            raise
        except Exception as e:
            print("[Runner] Token validation skipped: %s" % e, file=sys.stderr)

    cursor_key = os.environ.get("CURSOR_API_KEY")

    if args.steps_json:
        print("[Runner] Loading steps from %s" % args.steps_json)
        with open(args.steps_json) as f:
            steps = json.load(f)
    else:
        if not cursor_key:
            print(
                "Error: CURSOR_API_KEY not set.\n"
                "Get one from Cursor Dashboard -> Integrations\n"
                "and set it in environment or tools/.env",
                file=sys.stderr,
            )
            sys.exit(1)

        print("[Runner] Mission: %s" % args.mission)
        print("[Runner] Decomposing via Cursor Cloud Agent...")
        steps = decompose_mission(args.mission, cursor_key, verbose=args.verbose)

    if not steps:
        print("Failed to decompose mission into steps.", file=sys.stderr)
        sys.exit(1)

    print("\n[Runner] Decomposed into %d step(s):" % len(steps))
    for s in steps:
        print("  %d. %s  %s" % (s.get("step", 0), s.get("tool", "?"), json.dumps(s.get("params", {}))))

    results = dispatch_steps(
        steps,
        dry_run=args.dry_run,
        fail_fast=args.fail_fast,
        verbose=args.verbose,
    )
    print_summary(results)

    all_ok = all(r.get("ok") for r in results)
    sys.exit(0 if all_ok else 2)


if __name__ == "__main__":
    main()
