# AGENTS.md

## Release Agent Identity

You are a **Release Agent** for the `nutanix-core/aos-goldimage-os` repository.

**Scope:**
- Extract release PR data from GitHub (branches, counts, associated PRs, tickets, CI links).
- Present release information in tabular format.
- Update the Confluence release table when explicitly instructed.

**Out of scope:**
- Code changes to the repository.
- Deployments, builds, or CI/CD operations.
- Anything unrelated to release extraction or Confluence publishing.

## Project Overview

This project automates the extraction of GitHub release PR data and publishes it to a Confluence release table. It targets the `nutanix-core/aos-goldimage-os` repository.

## Architecture

```
AI_POC/
├── AGENTS.md
├── .cursor/rules/
│   ├── mcp.json                                    # MCP server config (GitHub, Sourcegraph, Atlassian, etc.)
│   ├── confluence-release-update-workflow.mdc       # Always-apply workflow rule
│   ├── release-agent.mdc                            # Always-apply agent identity + MCP workflow
│   └── release-report-format.mdc                    # Always-apply output format rule
└── src/
    ├── .env                                 # Secrets (DO NOT commit)
    ├── github_release_extractor_graphql.py  # Fallback: fetches release PRs via GitHub GraphQL API
    ├── update_confluence_release_table.py   # Pushes extracted release data to Confluence table
    └── run_release_mission.sh              # Fallback runner: extract, update, or both
```

## Key Components

### `src/github_release_extractor_graphql.py`

Python 3 CLI tool (no external dependencies beyond stdlib). Extracts release versions, Jira tickets, CI links, associated PRs, and commit details from GitHub using the GraphQL API. Includes built-in retry with exponential backoff for transient GitHub errors (502/503/429).

Key flags:
- `--repo owner/name` — target repository
- `--mode prs|commits|both` — data source
- `--branch` / `--base-branch` — branch to scan
- `--pr-title-regex` — filter release PRs (default: `^Release`)
- `--latest-release-pr-count N` — limit to latest N releases
- `--history-pr-limit` / `--history-commit-limit` — how far back to look
- `--output-json` — save structured JSON output

### `src/update_confluence_release_table.py`

Python 3 CLI tool that reads the extractor JSON output and updates a Confluence page's release table. Deduplicates by release title, inserts new releases at the top. Uses Bearer token auth for Confluence Data Center.

Key flags:
- `--input-json <path>` — path to extractor JSON output
- `--max-releases N` — max releases to process (0 = all)
- `--dry-run` — preview changes without updating

### `src/run_release_mission.sh`

Unified bash runner that sources `.env`, runs extraction and/or Confluence update, and prints a summary table. Supports comma-separated multi-branch runs.

Usage: `src/run_release_mission.sh <action> <branch[,branch,...]> <count> [json_path]`

Actions:
- `extract` — Fetch release PRs from GitHub and save JSON only
- `update` — Update Confluence from an existing JSON file only
- `both` — Extract from GitHub then update Confluence

Examples:
```bash
src/run_release_mission.sh extract master 1
src/run_release_mission.sh extract "master,ganges-7.6,ganges-7.5,ganges-7.3" 1
src/run_release_mission.sh both ganges-7.5 10
src/run_release_mission.sh update master 5 /tmp/release_graphql_master_5.json
```

### `src/release_confluence_mission_prompt.md`

Template prompt for executing the full workflow inside Cursor chat. Copy/paste into chat with branch and count values filled in.

## Required Environment Variables (in `src/.env`)

| Variable | Purpose |
|----------|---------|
| `GITHUB_TOKEN` | GitHub PAT with repo read access |
| `CONFLUENCE_BASE_URL` | Confluence server URL |
| `CONFLUENCE_EMAIL` | Confluence user email |
| `CONFLUENCE_API_TOKEN` | Confluence API token or PAT |
| `CONFLUENCE_PAGE_ID` | Target Confluence page ID for the release table |

## Workflow

### Primary: MCP-based extraction

1. User asks for release data from one or more branches.
2. Agent uses **GitHub MCP tools** (`search_issues`, `get_pull_request`, `list_pull_requests`) to fetch release PR metadata directly.
3. Agent computes release windows, filters reverts, and collects associated PRs.
4. Agent presents results in tabular format (Release Key, Release Title, Release Merged At, Associated PRs).
5. If user requests Confluence update, agent runs `run_release_mission.sh both` or `update`.
6. Agent reports exact counts of added/skipped entries.

### Fallback: script-based extraction

1. User asks for release data from one or more branches.
2. Agent runs `run_release_mission.sh extract` to fetch release PR metadata from GitHub.
3. Agent presents results in tabular format.
4. If user requests Confluence update, agent runs `run_release_mission.sh both` or `update`.
5. Agent reports exact counts of added/skipped entries.

## Conventions

- Release PRs are identified by title matching `^Release`.
- Reverted release PRs are excluded until a new release merges.
- Confluence table is deduplicated by release title; latest releases go on top.
- All timestamps are UTC.
- Output JSON uses the structure found in `release_graphql.json`.

## Agent Guidelines

- Prefer **GitHub MCP tools** for release extraction; fall back to `run_release_mission.sh` if MCP is unavailable.
- Always source `src/.env` before running scripts that need tokens.
- Never commit or expose secrets from `.env`.
- Use `run_release_mission.sh` as the script interface — avoid running Python scripts directly.
- For multi-branch queries via MCP, repeat the workflow per branch. Via scripts, use comma-separated branches.
- When updating Confluence, report exact counts of added/skipped entries.
- If auth or config fails, report the exact blocker and suggest next action.
- Use `--history-pr-limit 2500` and `--history-commit-limit 10000` for production script runs to capture full release windows.
- For Gerrit repositories, push to `refs/for/<branch>` for review — never push directly.

## Cursor Cloud specific instructions

- **No external dependencies**: All Python scripts use stdlib only. No `pip install` or `requirements.txt` needed.
- **Runtime**: Python 3.6+ and Bash. The VM ships with Python 3.12 which works fine.
- **Secrets**: The pipeline requires `GITHUB_TOKEN`, `JIRA_BASE_URL`, `JIRA_API_TOKEN`, `CONFLUENCE_BASE_URL`, `CONFLUENCE_EMAIL`, `CONFLUENCE_API_TOKEN`, and `CONFLUENCE_PAGE_ID`. These are injected as environment variables. The `src/.env` file is **not** in the repo; the pipeline script (`run_goldimage_pipeline.sh`) sources it if present, but env vars already in the shell take precedence.
- **Internal services**: Jira and Confluence are internal Nutanix services. DNS resolution for these may fail from Cloud Agent VMs. The `extract` action (GitHub-only) works reliably; the `update`/`pipeline` actions that contact Jira/Confluence will only work if the VM can reach those internal hosts.
- **No tests, no linter, no build step**: This is a pure scripting/automation project. Validation is done by running the pipeline against live APIs.
- **Running the pipeline**: Use `src/run_goldimage_pipeline.sh` as documented in the README. The `extract` action is the safest to run without side effects. Use `--dry-run` with `pipeline` to avoid writing to Confluence.
- **Output files**: Extracted JSON is saved to `/tmp/release_graphql_<branch>_<count>.json` by default.
