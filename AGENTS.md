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
├── release_query.py                 # Main entry point: full pipeline (run from root)
├── agent_runner.py                  # Mission decomposition + tool dispatch (cursor-sdk)
├── .cursor/rules/
│   ├── mcp.json                     # MCP server config (GitHub, Sourcegraph, Atlassian, etc.)
│   ├── confluence-release-update-workflow.mdc  # Always-apply workflow rule
│   ├── release-agent.mdc            # Always-apply agent identity + MCP workflow
│   └── goldimage-table-format.mdc   # Always-apply output format rule
└── tools/                           # MCP client modules (importable package)
    ├── __init__.py
    ├── .env                         # Secrets (DO NOT commit)
    ├── mcp_client.py                # Shared MCP client (fastmcp wrapper)
    ├── mcp_github_client.py         # GitHub MCP + REST API (commit details, CI status)
    ├── mcp_sourcegraph_client.py    # Sourcegraph MCP (commit search)
    ├── mcp_confluence_client.py     # Confluence MCP (table upload, page routing)
    ├── mcp_ticket_validator.py      # Ticket validation via Sourcegraph
    └── release_query.py             # Backward-compatible wrapper → root release_query.py
```

## Key Components

### `release_query.py` (project root)

**Main entry point — full pipeline in a single run.** Runs GitHub + Sourcegraph/Gerrit + Jira lookups concurrently. All public functions are independently callable by MCP tools (`from release_query import fetch_gerrit_releases, parse_releases`).

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
python3 release_query.py --branch master --count 5
python3 release_query.py --branch ganges-7.5 --count 10 --filter pc --with-sg-date --with-github-date
python3 release_query.py --branch master --count 10 --format json --output /tmp/out.json
```

### `tools/mcp_client.py`

Shared MCP client — thin synchronous wrapper around `fastmcp` for use by all tool modules. Provides `call_tool()`, `list_tools()`, `_get_env()`, and `load_mcp_config()`. Reads `.cursor/rules/mcp.json` for MCP server config and `tools/.env` for secrets.

### `tools/mcp_github_client.py`

GitHub MCP client + REST API. Fetches commit details, PR information, and CircleCI postmerge status. The `fetch_postmerge_ci()` function is used by `release_query.py` for CI status enrichment.

Subcommands: `commit`, `ci`, `pr`, `list-commits`, `list-tools`

### `tools/mcp_sourcegraph_client.py`

Sourcegraph MCP client. Searches commits via the `commit_search` MCP tool on the Sourcegraph gateway. Used for Gerrit/GitHub commit history lookups.

### `tools/mcp_confluence_client.py`

Confluence MCP client with **automatic page routing**. Uploads release tables via the Atlassian MCP server. Features auto child page discovery/creation, deduplication, sorted table rebuild, and Jira Issue macros in storage format.

Key flags:
- `--input-json <path>` — path to release JSON
- `--branch` — branch name
- `--type AOS|PC` — release type (auto-detected if omitted)
- `--dry-run` — preview without updating
- `--force-rebuild` — rebuild entire table even if no new rows

### `tools/mcp_ticket_validator.py`

Validates Jira EPIC/ticket IDs against release commits via Sourcegraph MCP. For each release, extracts all referenced ticket IDs from commit messages and validates each by searching Sourcegraph for commits mentioning that ticket.

### `agent_runner.py`

Mission decomposition + tool dispatch script at project root. Uses `cursor-sdk` (Python 3.10+) to decompose natural language missions into ordered tool steps, then dispatches each step with retry logic.

Usage: `python3.14 agent_runner.py "Extract last 5 releases from master and update confluence"`

## Required Environment Variables (in `tools/.env`)

| Variable | Purpose |
|----------|---------|
| `GITHUB_TOKEN` | GitHub PAT with repo read access |
| `CONFLUENCE_BASE_URL` | Confluence server URL |
| `CONFLUENCE_API_TOKEN` | Confluence API token or PAT |
| `AOS_CONFLUENCE_PAGE_ID` | Parent page ID for AOS releases (child pages per branch) |
| `PC_CONFLUENCE_PAGE_ID` | Parent page ID for PC releases (child pages per branch) |
| `CONFLUENCE_PAGE_ID` | Fallback parent page ID (used if type-specific IDs not set) |
| `SOURCEGRAPH_TOKEN` | Sourcegraph access token for Gerrit merge validation |
| `JIRA_BASE_URL` | Jira server URL |
| `JIRA_API_TOKEN` | Jira personal access token (Bearer) |

## Workflow

### Primary: fast-path via `release_query.py` (project root)

1. User asks for release data from one or more branches.
2. Agent runs `python3 release_query.py` with appropriate flags.
3. Script concurrently fetches GitHub releases, validates via Sourcegraph/Gerrit, and looks up Jira Epics.
4. Agent displays the table output directly.
5. If user requests Confluence update, agent runs `python3 release_query.py --format json --output <path>`, then `python3 -m tools.mcp_confluence_client --input-json <path> --branch <branch>`.

### Fallback 1: MCP-based extraction

1. If `release_query.py` fails, agent uses **GitHub MCP tools** (`search_issues`, `get_pull_request`, `list_pull_requests`) to fetch release PR metadata.
2. Agent validates CR merge status via **Sourcegraph MCP tools** (`commit_search`) against the **Gerrit repo** (`nugerrit.ntnxdpro.com/main`). A GitHub PR merge does NOT mean the CR is merged.
3. Agent presents results in tabular format.
4. If user requests Confluence update, agent exports JSON and runs `mcp_confluence_client.py`.

### Fallback 2: direct tool invocation

1. If MCP tools are also unavailable, agent runs individual MCP client modules (`mcp_github_client.py`, `mcp_sourcegraph_client.py`) directly.
2. Agent presents results in tabular format.
3. For Confluence update, agent pipes output through `mcp_confluence_client.py`.

## Conventions

- Release PRs are identified by title matching `^Release`.
- **GitHub PR merge ≠ CR merge.** A PR merged in GitHub does not mean the code review (CR) is merged. The Gerrit repo (`nugerrit.ntnxdpro.com/main`) is the source of truth for CR merge status and date. CRs are auto-submitted by `svc.jenkins.autosub` on Gerrit, then synced to GitHub.
- Combined AOS/PC release titles are split on `/PC:` and each component is validated independently via Sourcegraph against the Gerrit repo.
- Versions containing `ganges-pc.` are classified as PC releases, not AOS.
- Sourcegraph `commit_search` shows the original author; the committer on Gerrit is `svc.jenkins.autosub`.
- Reverted release PRs are excluded using version-based timeline analysis.
- Only releases whose Jira Main Ticket (Epic) is in **Closed** status are included in output tables and Confluence updates. Non-Closed tickets are filtered out.
- Confluence tables are deduplicated by GoldImage version; rows are sorted newest-first.
- Invalid changelog/RPM URLs are shown as "Data not found" (validated via HEAD requests).
- `AOS_CONFLUENCE_PAGE_ID` and `PC_CONFLUENCE_PAGE_ID` are separate parent pages for each release type; child pages are auto-discovered/created per branch. Falls back to `CONFLUENCE_PAGE_ID` if type-specific IDs are not set.
- All timestamps are UTC, sourced from Gerrit commit dates via Sourcegraph.

## Agent Guidelines

- Prefer **`release_query.py`** at project root (fast-path) for release extraction; fall back to MCP tools, then direct tool invocation.
- Always source `tools/.env` before running scripts that need tokens.
- Never commit or expose secrets from `.env`.
- For Confluence updates, use `mcp_confluence_client.py` directly — it handles page routing automatically.
- Use `--force-rebuild` when existing pages need re-sorting or URL re-validation.
- For multi-branch queries via MCP, repeat the workflow per branch.
- When updating Confluence, report exact counts of added/skipped entries.
- If auth or config fails, report the exact blocker and suggest next action.
- For Gerrit repositories, push to `refs/for/<branch>` for review — never push directly.
