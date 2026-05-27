# AGENTS.md

## Release Agent Identity

You are a **Release Agent** for the `nutanix-core/aos-goldimage-os` repository.

**Scope:**
- Extract release PR data from GitHub (branches, counts, associated PRs, tickets, CI links).
- Validate CR merge status via Sourcegraph/Gerrit.
- Present release information in tabular format.
- Update the Confluence release table when explicitly instructed.

**Out of scope:**
- Code changes to the repository.
- Deployments, builds, or CI/CD operations.
- Anything unrelated to release extraction or Confluence publishing.

## Project Overview

This project automates the extraction of GitHub release PR data, validates merge status on Gerrit via Sourcegraph, and publishes release tables to Confluence. It targets the `nutanix-core/aos-goldimage-os` repository.

## Architecture

```
AI-Powered-Release-Agent/
├── AGENTS.md
├── README.md
├── agent_runner.py                  # Mission decomposition + tool dispatch (cursor-sdk)
├── .cursor/rules/
│   ├── mcp.json                     # MCP server config (GitHub, Sourcegraph, Atlassian, etc.)
│   ├── confluence-release-update-workflow.mdc  # Always-apply workflow rule
│   ├── release-agent.mdc            # Always-apply agent identity + MCP workflow
│   └── goldimage-table-format.mdc   # Always-apply output format rule
└── tools/
    ├── .env                         # Secrets (DO NOT commit)
    ├── exceptions.py                # Typed exception hierarchy (ToolError, AuthError, etc.)
    ├── release_query.py             # Fast-path: concurrent GitHub+SG+Jira pipeline (preferred)
    ├── github_tool.py               # GitHub release extraction via REST API
    ├── jira_tool.py                 # Jira Epic ticket search
    ├── confluence_tool.py           # Confluence table updater with auto page routing
    └── sourcegraph_tool.py          # Sourcegraph/Gerrit release merge validation (AOS/PC split)
```

## Key Components

### `tools/release_query.py`

**Fast-path unified pipeline.** Runs GitHub + Sourcegraph/Gerrit + Jira lookups concurrently using `ThreadPoolExecutor`. ~5x faster than sequential MCP calls. Includes a file-based cache (5 min TTL) to skip API calls on repeated queries.

Key capabilities:
- **Revert detection**: Builds a per-version timeline of release/revert events; excludes versions whose latest event is a revert.
- **AOS/PC classification**: Splits combined titles on `/PC:` and detects `ganges-pc.` in version strings to prevent misclassification.
- **Gerrit branch resolution**: For non-master branches, resolves the correct Gerrit branch from the EPIC Jira ticket's fix version using numeric comparison.
- **Git tracker fallback**: For branches where fix-version resolution is insufficient (e.g. `ganges-7.6`), parses `===git tracker===` comments from Jira EPIC tickets to extract Gerrit branch and commit date.

Key flags:
- `--branch` — branch to scan (default: `master`)
- `--count N` — latest N releases (default: 5)
- `--filter all|aos|pc` — filter output rows
- `--with-sg-date` — add Sourcegraph/Gerrit merge date column
- `--with-github-date` — add GitHub PR merge date column
- `--format table|json` — output format
- `--output <path>` — save to file
- `--no-cache` — force fresh API calls

Examples:
```bash
python3 tools/release_query.py --branch master --count 5
python3 tools/release_query.py --branch ganges-7.5 --count 10 --filter pc --with-sg-date --with-github-date
python3 tools/release_query.py --branch master --count 10 --format json --output /tmp/out.json
```

### `tools/github_tool.py`

Python 3 CLI tool that extracts release PRs, associated PRs, and CI status from GitHub using the REST API. Supports retry with exponential backoff.

Key flags:
- `--repo owner/name` — target repository
- `--branch` — branch to scan
- `--count N` — latest N releases
- `--ci-status` — include postmerge CircleCI status
- `--output-json` — save structured JSON output
- `--mode prs|commits|both` — data source
- `--include-comments` — parse PR comments for tickets/commands

### `tools/sourcegraph_tool.py`

Self-contained HTTP client for Sourcegraph. Validates release CR merge status by querying the **Gerrit repo** (`nugerrit.ntnxdpro.com/main`) via Sourcegraph's stream API. This is the source of truth for CR merges — CRs are auto-submitted by `svc.jenkins.autosub` on Gerrit before syncing to GitHub. Splits combined AOS/PC release titles (separated by `/PC:`) and validates each component independently. Detects reverted releases. Raises typed exceptions.

Key flags:
- `--input-json <path>` — path to github_tool.py release JSON
- `--pr-titles` — semicolon-separated PR titles to validate directly
- `--output-json` — save validated output to JSON
- `--format table|json` — output format

Required env var: `SOURCEGRAPH_TOKEN`

### `tools/jira_tool.py`

Self-contained HTTP client for Jira Epic search. Reads release JSON, extracts AOS versions, and queries Jira REST API. Raises typed exceptions.

Key flags:
- `--input-json <path>` — path to release extractor JSON
- `--branch` — branch name for Notes column
- `--output-json` — save enriched JSON output
- `--format table|json` — output format

### `tools/confluence_tool.py`

Self-contained HTTP client for Confluence table updates with **automatic page routing**. Uses `CONFLUENCE_PAGE_ID` as the parent page and automatically discovers or creates child pages for each branch + release type (AOS/PC) combination.

Key capabilities:
- **Auto page routing**: Lists child pages under the parent, matches by branch name + type in title, creates new child pages if needed (e.g. "PC Release ganges-7.3").
- **URL validation**: Validates changelog and RPM URLs via parallel HEAD checks; invalid links show "Data not found".
- **Sorted table rebuild**: When adding rows, rebuilds the entire table sorted by merge date (newest first). Existing rows are preserved and re-sorted alongside new ones.
- **Force rebuild**: `--force-rebuild` rebuilds the table from JSON data even when no new rows to add — useful for fixing ordering or URL issues on existing pages.

Key flags:
- `--input-json <path>` — path to extractor JSON output
- `--branch` — branch name
- `--type AOS|PC` — release type (auto-detected from JSON if omitted)
- `--max-releases N` — max releases to process (0 = all)
- `--force-rebuild` — rebuild entire table even if no new rows
- `--dry-run` — preview changes without updating

### `tools/exceptions.py`

Typed exception hierarchy used by all `*_tool.py` files. The orchestrator (`agent_runner.py`) inspects the exception type in subprocess stderr to decide whether to retry.

Exception classes:
- `ToolError` — base (carries `retryable` flag)
- `AuthError` — 401/403, never retryable
- `ConfigError` — missing env var, never retryable
- `RateLimitError` — 429, always retryable
- `HttpError` — other HTTP errors, retryable for 502/503/504
- `NetworkError` — DNS/timeout, always retryable
- `NotFoundError` — 404, never retryable
- `DataError` — parse/validation, never retryable

### `agent_runner.py`

Mission decomposition + tool dispatch script at project root. Uses `cursor-sdk` (Python 3.10+) to decompose natural language missions into ordered tool steps, then dispatches each step with retry logic.

Usage: `python3.14 agent_runner.py "Extract last 5 releases from master and update confluence"`

## Required Environment Variables (in `tools/.env`)

| Variable | Purpose |
|----------|---------|
| `GITHUB_TOKEN` | GitHub PAT with repo read access |
| `CONFLUENCE_BASE_URL` | Confluence server URL |
| `CONFLUENCE_API_TOKEN` | Confluence API token or PAT |
| `CONFLUENCE_PAGE_ID` | **Parent page ID** — child pages auto-resolved per branch/type |
| `SOURCEGRAPH_TOKEN` | Sourcegraph access token for Gerrit merge validation |
| `JIRA_BASE_URL` | Jira server URL |
| `JIRA_API_TOKEN` | Jira personal access token (Bearer) |

## Workflow

### Primary: fast-path via `release_query.py`

1. User asks for release data from one or more branches.
2. Agent runs `python3 tools/release_query.py` with appropriate flags.
3. Script concurrently fetches GitHub releases, validates via Sourcegraph/Gerrit, and looks up Jira Epics.
4. Agent displays the table output directly.
5. If user requests Confluence update, agent runs `release_query.py --format json --output <path>`, then `confluence_tool.py --input-json <path> --branch <branch>`.

### Fallback 1: MCP-based extraction

1. If `release_query.py` fails, agent uses **GitHub MCP tools** (`search_issues`, `get_pull_request`, `list_pull_requests`) to fetch release PR metadata.
2. Agent validates CR merge status via **Sourcegraph MCP tools** (`commit_search`) against the **Gerrit repo** (`nugerrit.ntnxdpro.com/main`). A GitHub PR merge does NOT mean the CR is merged.
3. Agent presents results in tabular format.
4. If user requests Confluence update, agent exports JSON and runs `confluence_tool.py`.

### Fallback 2: direct tool invocation

1. If MCP tools are also unavailable, agent runs individual tools (`github_tool.py`, `sourcegraph_tool.py`, `jira_tool.py`) directly.
2. Agent presents results in tabular format.
3. For Confluence update, agent pipes output through `confluence_tool.py`.

## Conventions

- Release PRs are identified by title matching `^Release`.
- **GitHub PR merge ≠ CR merge.** A PR merged in GitHub does not mean the code review (CR) is merged. The Gerrit repo (`nugerrit.ntnxdpro.com/main`) is the source of truth for CR merge status and date. CRs are auto-submitted by `svc.jenkins.autosub` on Gerrit, then synced to GitHub.
- Combined AOS/PC release titles are split on `/PC:` and each component is validated independently via Sourcegraph against the Gerrit repo.
- Versions containing `ganges-pc.` are classified as PC releases, not AOS.
- Sourcegraph `commit_search` shows the original author; the committer on Gerrit is `svc.jenkins.autosub`.
- Reverted release PRs are excluded using version-based timeline analysis.
- Confluence tables are deduplicated by GoldImage version; rows are sorted newest-first.
- Invalid changelog/RPM URLs are shown as "Data not found" (validated via HEAD requests).
- `CONFLUENCE_PAGE_ID` is a parent page; child pages are auto-discovered/created per branch + type.
- All timestamps are UTC, sourced from Gerrit commit dates via Sourcegraph.

## Agent Guidelines

- Prefer **`tools/release_query.py`** (fast-path) for release extraction; fall back to MCP tools, then direct tool invocation.
- Always source `tools/.env` before running scripts that need tokens.
- Never commit or expose secrets from `.env`.
- For Confluence updates, use `confluence_tool.py` directly — it handles page routing automatically.
- Use `--force-rebuild` when existing pages need re-sorting or URL re-validation.
- For multi-branch queries via MCP, repeat the workflow per branch.
- When updating Confluence, report exact counts of added/skipped entries.
- If auth or config fails, report the exact blocker and suggest next action.
- For Gerrit repositories, push to `refs/for/<branch>` for review — never push directly.

## Cursor Cloud specific instructions

- **No external dependencies**: All Python scripts use stdlib only. No `pip install` or `requirements.txt` needed.
- **Runtime**: Python 3.6+ and Bash. The VM ships with Python 3.12 which works fine.
- **Secrets**: The pipeline requires `GITHUB_TOKEN`, `JIRA_BASE_URL`, `JIRA_API_TOKEN`, `CONFLUENCE_BASE_URL`, `CONFLUENCE_EMAIL`, `CONFLUENCE_API_TOKEN`, and `CONFLUENCE_PAGE_ID`. These are injected as environment variables. The `src/.env` file is **not** in the repo; the pipeline script (`run_goldimage_pipeline.sh`) sources it if present, but env vars already in the shell take precedence.
- **Internal services**: Jira and Confluence are internal Nutanix services. DNS resolution for these may fail from Cloud Agent VMs. The `extract` action (GitHub-only) works reliably; the `update`/`pipeline` actions that contact Jira/Confluence will only work if the VM can reach those internal hosts.
- **No tests, no linter, no build step**: This is a pure scripting/automation project. Validation is done by running the pipeline against live APIs.
- **Running the pipeline**: Use `src/run_goldimage_pipeline.sh` as documented in the README. The `extract` action is the safest to run without side effects. Use `--dry-run` with `pipeline` to avoid writing to Confluence.
- **Output files**: Extracted JSON is saved to `/tmp/release_graphql_<branch>_<count>.json` by default.
