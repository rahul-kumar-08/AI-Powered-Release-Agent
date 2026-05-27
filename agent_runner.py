#!/usr/bin/env python3
"""
Agent Runner — receives a mission string, calls Cursor SDK to
decompose it into steps, then dispatches each step to the right tool.

Uses the cursor-sdk Python package (pip install cursor-sdk).
Requires Python 3.10+.  Run with: python3.14 agent_runner.py ...

Usage:
    python3.14 agent_runner.py "Extract last 5 releases from master"
    python3.14 agent_runner.py "Extract releases from master, find Jira epics, update Confluence"
    python3.14 agent_runner.py --dry-run "Full pipeline for ganges-7.5 last 3 releases"

Requires:
    CURSOR_API_KEY  — Cursor Dashboard -> Integrations
    GITHUB_TOKEN    — for GitHub tool dispatch
    JIRA_* / CONFLUENCE_* — for Jira/Confluence dispatch (in tools/.env)
"""

import argparse
import json
import os
import subprocess
import sys
import time

MAX_TOOL_RETRIES = 3
RETRY_BACKOFF_BASE = 2

DECOMPOSITION_PROMPT = """You are a mission planner for a Release Agent. Your ONLY job is to
decompose the user's mission into an ordered list of tool steps.

Available tools and their parameters:

1. github_releases — Extract release PRs from GitHub.
   params: branch (string, default "master"), count (int, default 1),
           repo (string, default "nutanix-core/aos-goldimage-os"),
           ci_status (bool, default false), output_json (string path)

2. jira_epic_search — Enrich releases with Jira Epic tickets.
   params: input_json (string path, required), branch (string, required)

3. confluence_update — Update Confluence page with release table.
   params: input_json (string path, required), branch (string, required),
           type (string "AOS"|"PC", optional — auto-detected from JSON if omitted),
           force_rebuild (bool, default false — rebuild table even if no new rows),
           dry_run (bool, default false)

4. ci_status — Fetch postmerge CircleCI status for releases.
   params: branch (string, default "master"), count (int, default 1),
           commit_sha (string, optional — for a single commit)

5. shell_pipeline — Run the full end-to-end pipeline script.
   params: action (string: "extract"|"update"|"pipeline"),
           branches (string, comma-separated), count (int, default 1),
           dry_run (bool, default false)

6. sourcegraph_validate — Validate release PR merge status via Sourcegraph.
   params: input_json (string path — from github_releases output),
           pr_titles (string — semicolon-separated titles, alternative to input_json),
           output_json (string path, optional), format (string: "json"|"table")

Rules:
- Return ONLY a JSON array. No markdown, no explanation, no extra text.
- Each element: {"step": N, "tool": "<name>", "params": {...}}
- Infer defaults: branch="master", count=1 when not specified.
- When a step produces JSON output, set output_json="/tmp/releases_<branch>_<count>.json"
  and pass that path as input_json to downstream steps.
- If the user says "pipeline" or "full pipeline", use shell_pipeline with action="pipeline".
- If the user says "update confluence", include confluence_update step.
- If the user mentions "jira" or "epic" or "tickets", include jira_epic_search.
- If the user mentions "ci" or "circleci" or "postmerge", include ci_status.
- If the user mentions "validate", "sourcegraph", or "merge status", include sourcegraph_validate
  after github_releases, passing the output_json as input_json.

Examples:

Mission: "Extract last 5 releases from master"
Output:
[{"step":1,"tool":"github_releases","params":{"branch":"master","count":5,"output_json":"/tmp/releases_master_5.json"}}]

Mission: "Get releases from ganges-7.5, find jira epics, update confluence"
Output:
[{"step":1,"tool":"github_releases","params":{"branch":"ganges-7.5","count":1,"output_json":"/tmp/releases_ganges-7.5_1.json"}},{"step":2,"tool":"jira_epic_search","params":{"input_json":"/tmp/releases_ganges-7.5_1.json","branch":"ganges-7.5"}},{"step":3,"tool":"confluence_update","params":{"input_json":"/tmp/releases_ganges-7.5_1.json","branch":"ganges-7.5"}}]

Mission: "Full pipeline for master last 3"
Output:
[{"step":1,"tool":"shell_pipeline","params":{"action":"pipeline","branches":"master","count":3}}]

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
    from cursor_sdk import Agent, AgentOptions, CursorAgentError

    full_prompt = DECOMPOSITION_PROMPT + mission

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

def run_github_releases(params):
    """Extract releases via github_tool.py."""
    cmd = [
        sys.executable, "tools/github_tool.py",
        "--branch", params.get("branch", "master"),
        "--count", str(params.get("count", 1)),
    ]
    repo = params.get("repo")
    if repo:
        cmd.extend(["--repo", repo])
    if params.get("ci_status"):
        cmd.append("--ci-status")
    output_json = params.get("output_json")
    if output_json:
        cmd.extend(["--output-json", output_json])
    return run_subprocess(cmd)


def run_jira_epic_search(params):
    """Search Jira for Epic tickets via jira_tool.py."""
    input_json = params.get("input_json")
    branch = params.get("branch", "master")
    if not input_json:
        return {"ok": False, "error": "input_json is required"}
    cmd = [
        sys.executable, "tools/jira_tool.py",
        "--input-json", input_json,
        "--branch", branch,
    ]
    return run_subprocess(cmd)


def run_confluence_update(params):
    """Update Confluence via confluence_tool.py."""
    input_json = params.get("input_json")
    branch = params.get("branch", "master")
    if not input_json:
        return {"ok": False, "error": "input_json is required"}
    cmd = [
        sys.executable, "tools/confluence_tool.py",
        "--input-json", input_json,
        "--branch", branch,
    ]
    release_type = params.get("type")
    if release_type:
        cmd.extend(["--type", release_type])
    if params.get("force_rebuild"):
        cmd.append("--force-rebuild")
    if params.get("dry_run"):
        cmd.append("--dry-run")
    return run_subprocess(cmd)


def run_ci_status(params):
    """Fetch CI status via github_tool.py."""
    sha = params.get("commit_sha")
    if sha:
        cmd = [
            sys.executable, "tools/github_tool.py",
            "--commit-status", sha,
        ]
    else:
        cmd = [
            sys.executable, "tools/github_tool.py",
            "--branch", params.get("branch", "master"),
            "--count", str(params.get("count", 1)),
            "--ci-status",
        ]
    return run_subprocess(cmd)


def run_shell_pipeline(params):
    """Run the full pipeline via run_goldimage_pipeline.sh (if available)."""
    script = "tools/run_goldimage_pipeline.sh"
    if not os.path.isfile(script):
        return {"ok": False, "error": f"{script} not found. Use release_query.py + confluence_tool.py instead.", "retryable": False}
    action = params.get("action", "pipeline")
    branches = params.get("branches", "master")
    count = str(params.get("count", 1))
    cmd = ["bash", script, action, branches, count]
    if params.get("dry_run"):
        cmd.append("--dry-run")
    return run_subprocess(cmd)


def run_sourcegraph_validate(params):
    """Validate release merge status via sourcegraph_tool.py."""
    cmd = [sys.executable, "tools/sourcegraph_tool.py"]
    input_json = params.get("input_json")
    pr_titles = params.get("pr_titles")
    if input_json:
        cmd.extend(["--input-json", input_json])
    elif pr_titles:
        cmd.extend(["--pr-titles", pr_titles])
    else:
        return {"ok": False, "error": "input_json or pr_titles is required"}
    output_json = params.get("output_json")
    if output_json:
        cmd.extend(["--output-json", output_json])
    fmt = params.get("format", "json")
    cmd.extend(["--format", fmt])
    return run_subprocess(cmd)


def run_subprocess(cmd):
    """Execute a subprocess and capture output."""
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


def _is_retryable_stderr(stderr):
    """Check if the tool's stderr indicates a retryable exception type."""
    retryable_types = ("RateLimitError", "NetworkError", "HttpError")
    for t in retryable_types:
        if t in (stderr or ""):
            return True
    return False


TOOL_REGISTRY = {
    "github_releases": run_github_releases,
    "jira_epic_search": run_jira_epic_search,
    "confluence_update": run_confluence_update,
    "ci_status": run_ci_status,
    "shell_pipeline": run_shell_pipeline,
    "sourcegraph_validate": run_sourcegraph_validate,
}


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

        if result.get("ok"):
            stdout = result.get("stdout", "")
            preview = stdout[:500] if stdout else "(no output)"
            print("  OK")
            if verbose and stdout:
                print(stdout)
            elif stdout:
                print("  Output: %s%s" % (preview, "..." if len(stdout) > 500 else ""))
        else:
            err = result.get("error") or result.get("stderr", "")
            print("  FAILED: %s" % err[:300])
            if fail_fast:
                print("  --fail-fast: aborting.")
                results.append({"step": step_num, "tool": tool, "ok": False, "error": err})
                break

        results.append({"step": step_num, "tool": tool, "ok": result.get("ok", False)})

    return results


def print_summary(results):
    """Print a final summary table."""
    print("\n%s SUMMARY %s" % ("=" * 30, "=" * 30))
    print("%-6s %-25s %-10s" % ("Step", "Tool", "Status"))
    print("-" * 50)
    for r in results:
        status = "OK" if r.get("ok") else "FAILED"
        if r.get("dry_run"):
            status = "DRY RUN"
        print("%-6s %-25s %-10s" % (r.get("step", "?"), r.get("tool", "?"), status))
    print("-" * 50)
    ok_count = sum(1 for r in results if r.get("ok"))
    print("Total: %d/%d steps succeeded." % (ok_count, len(results)))


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
