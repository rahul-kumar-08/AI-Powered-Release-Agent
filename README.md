# AI-Powered Release Agent

An AI-driven automation tool that extracts GoldImage release PR data from the `nutanix-core/aos-goldimage-os` GitHub repository, validates merge status via Sourcegraph/Gerrit, enriches with Jira Epic metadata, and publishes structured release tables to Confluence.

## Overview

This project provides an end-to-end pipeline for tracking GoldImage releases:

1. **Extract** — Fetch merged release PRs from GitHub, with revert detection and AOS/PC classification.
2. **Validate** — Confirm CR merge status on Gerrit (`nugerrit.ntnxdpro.com/main`) via Sourcegraph. GitHub PR merge does NOT mean the CR is merged.
3. **Enrich** — Look up Epic tickets in Jira, resolve Gerrit branches from fix versions, and generate Endor URLs for changelogs and RPM lists.
4. **Publish** — Update Confluence with a deduplicated, date-sorted release table. Pages are auto-routed by branch and release type (AOS/PC).

The agent operates in three modes:

- **Fast-path (primary)** — `tools/release_query.py` runs GitHub + Sourcegraph + Jira lookups concurrently (~5x faster).
- **MCP mode (fallback)** — Uses GitHub, Atlassian, and Sourcegraph MCP servers from the Cursor IDE.
- **Script mode (last resort)** — CLI scripts for batch processing.

## Project Structure

```
AI-Powered-Release-Agent/
├── AGENTS.md                        # Agent identity and guidelines
├── README.md
├── agent_runner.py                  # Mission decomposition + tool dispatch (cursor-sdk)
├── .cursor/rules/
│   ├── mcp.json                     # MCP server configuration
│   ├── release-agent.mdc            # Agent behavior rules
│   ├── goldimage-table-format.mdc   # Output format rules
│   └── confluence-release-update-workflow.mdc  # Confluence update workflow
└── tools/
    ├── .env                         # Secrets (DO NOT commit)
    ├── exceptions.py                # Typed exception hierarchy
    ├── release_query.py             # Fast-path: concurrent GitHub+SG+Jira pipeline (preferred)
    ├── github_tool.py               # GitHub release extraction via REST API
    ├── sourcegraph_tool.py          # Sourcegraph/Gerrit merge validation
    ├── jira_tool.py                 # Jira Epic ticket search
    └── confluence_tool.py           # Confluence table updater with auto page routing
```

## Prerequisites

- **Python 3.6+** (stdlib only — no external packages required for tools)
- **Python 3.10+** with `cursor-sdk` for `agent_runner.py`
- Access tokens for GitHub, Jira, Confluence, and Sourcegraph (see [Configuration](#configuration))

## Configuration

Create a `tools/.env` file with the following variables:

```bash
# GitHub
GITHUB_TOKEN=ghp_...              # PAT with repo read access

# Jira
JIRA_BASE_URL=https://jira.example.com
JIRA_API_TOKEN=...                # Personal access token (Bearer)

# Confluence
CONFLUENCE_BASE_URL=https://confluence.example.com
CONFLUENCE_API_TOKEN=...          # API token or PAT (Bearer)
CONFLUENCE_PAGE_ID=123456         # Parent page ID — child pages auto-resolved per branch/type

# Sourcegraph
SOURCEGRAPH_TOKEN=...             # Sourcegraph access token for Gerrit merge validation
```

> **Note:** `CONFLUENCE_PAGE_ID` is the **parent page**. The tool automatically discovers or creates child pages for each branch + release type (AOS/PC) combination. No per-branch page IDs needed.

> **Warning:** Never commit `tools/.env`. It is excluded via `.gitignore`.

## Usage

### Fast-path: `release_query.py` (preferred)

```bash
# Last 5 AOS releases from master
python3 tools/release_query.py --branch master --count 5 --filter aos

# Last 10 PC releases from ganges-7.5 with Gerrit merge date
python3 tools/release_query.py --branch ganges-7.5 --count 10 --filter pc --with-sg-date

# Compare GitHub and Gerrit merge dates
python3 tools/release_query.py --branch master --count 5 --with-sg-date --with-github-date

# JSON output for Confluence update
python3 tools/release_query.py --branch master --count 15 --filter aos --format json --output /tmp/releases.json --no-cache
```

### Confluence update: `confluence_tool.py`

```bash
# Auto-detects type (AOS/PC) from JSON, auto-routes to correct child page
python3 tools/confluence_tool.py --input-json /tmp/releases.json --branch master

# Explicit type override
python3 tools/confluence_tool.py --input-json /tmp/releases.json --branch ganges-7.5 --type PC

# Rebuild existing table (re-sort, re-validate URLs)
python3 tools/confluence_tool.py --input-json /tmp/releases.json --branch master --type AOS --force-rebuild

# Preview without writing
python3 tools/confluence_tool.py --input-json /tmp/releases.json --branch master --dry-run
```

### Agent runner: `agent_runner.py`

```bash
# Natural language mission decomposition via Cursor SDK
python3.14 agent_runner.py "Extract last 5 releases from master"
python3.14 agent_runner.py "Extract releases from ganges-7.5, find jira epics, update confluence"
python3.14 agent_runner.py --dry-run "Full pipeline for master last 10"
```

## Components

### `tools/release_query.py`

**Primary tool.** Runs GitHub + Sourcegraph/Gerrit + Jira lookups concurrently using `ThreadPoolExecutor`. Includes file-based cache (5 min TTL). Handles:
- Revert detection (version-based timeline analysis)
- AOS/PC classification (splits on `/PC:` or detects `ganges-pc.` in version)
- Gerrit branch resolution from EPIC fix versions
- Git tracker fallback (parses `===git tracker===` comments from Jira EPICs)
- Endor URL construction for changelogs and RPM lists

### `tools/confluence_tool.py`

Confluence table updater with automatic page routing:
- Uses `CONFLUENCE_PAGE_ID` as parent page, discovers/creates child pages per branch + type
- Validates changelog and RPM URLs (parallel HEAD checks), shows "Data not found" for invalid links
- Rebuilds entire table sorted by merge date (newest first) when adding rows
- `--force-rebuild` rebuilds the table from JSON even when no new rows to add
- `--type AOS|PC` overrides auto-detection from JSON data
- Deduplicates by GoldImage version

### `tools/github_tool.py`

GitHub release extraction via REST API. Fetches release PRs, associated PRs, and CI status with retry and exponential backoff.

### `tools/sourcegraph_tool.py`

Validates release CR merge status by querying the Gerrit repo (`nugerrit.ntnxdpro.com/main`) via Sourcegraph's streaming API. Splits combined AOS/PC titles and validates each independently. Detects reverted releases.

### `tools/jira_tool.py`

Jira Epic ticket search. Reads release JSON, extracts AOS versions, and queries Jira REST API for matching Epics. Generates Endor URLs and enriched summary tables.

### `tools/exceptions.py`

Typed exception hierarchy used by all tools. The orchestrator inspects exception types to decide retry behavior: `ToolError` (base), `AuthError`, `ConfigError`, `RateLimitError`, `HttpError`, `NetworkError`, `NotFoundError`, `DataError`.

### `agent_runner.py`

Mission decomposition and tool dispatch. Uses `cursor-sdk` to decompose natural language missions into ordered tool steps, then executes each with retry logic.

## Confluence Auto Page Routing

The tool automatically manages Confluence pages:

1. `CONFLUENCE_PAGE_ID` in `tools/.env` points to a **parent page** (e.g. "AOS/PC Releases:")
2. When updating, the tool lists child pages and matches by branch name + release type in the title
3. If no matching child exists, a new page is created (e.g. "PC Release ganges-7.3")
4. Release type is auto-detected from JSON data (PC if versions contain "pc.", otherwise AOS)

No per-branch page IDs are needed.

## Confluence Table Format

| GoldImage Version | Main Tickets | Change Log | RPM List | Merge Date | Notes |
|---|---|---|---|---|---|
| `main-master-rhel9.7-5.14.0-9.2.0 (AOS)` | ENG-928113 | [changelog] | [rpm] | 23-May-2026 | — |

- Releases are sorted newest-first
- Invalid changelog/RPM URLs show "Data not found"
- Combined AOS/PC releases produce separate rows
- Merge Date is the Gerrit CR date (not GitHub PR date)
- Notes column shows the Gerrit stable branch for non-master releases

## MCP Integration

When used inside the Cursor IDE, the agent leverages MCP servers for interactive queries:

- **GitHub MCP** — Search and inspect PRs, issues, and repositories
- **Atlassian MCP** — Query Jira tickets and update Confluence pages
- **Sourcegraph MCP** — Search code and validate CR merges on Gerrit

MCP server configuration lives in `.cursor/rules/mcp.json`.

## License

Internal use only — Nutanix proprietary.
