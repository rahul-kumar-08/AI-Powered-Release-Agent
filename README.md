# AI-Powered Release Agent

An AI-driven automation tool that extracts GoldImage release PR data from the `nutanix-core/aos-goldimage-os` GitHub repository, enriches it with Jira Epic metadata, and publishes a structured release table to Confluence.

## Overview

This project provides an end-to-end pipeline for tracking GoldImage releases:

1. **Extract** — Fetch merged release PRs from GitHub via GraphQL API, capturing version info, associated PRs, Jira tickets, and CI links.
2. **Enrich** — Look up corresponding AOS Epic tickets in Jira and generate Endor URLs for changelogs and RPM lists.
3. **Publish** — Update a Confluence page with a deduplicated release table.

The agent can operate in two modes:

- **MCP mode (primary)** — Uses GitHub, Atlassian, and Sourcegraph MCP servers directly from the Cursor IDE for interactive queries.
- **Script mode (fallback)** — Runs the pipeline via CLI scripts for batch processing or CI use.

## Project Structure

```
AI-Powered-Release-Agent/
├── AGENTS.md                              # Agent identity and guidelines
├── README.md
├── .cursor/rules/
│   ├── mcp.json                           # MCP server configuration
│   ├── release-agent.mdc                  # Agent behavior rules
│   ├── release-report-format.mdc          # Output format rules
│   └── confluence-release-update-workflow.mdc  # Confluence update workflow
└── src/
    ├── github_release_extractor_graphql.py # GitHub GraphQL release extractor
    ├── search_jira_epic.py                # Jira Epic lookup and summary table
    ├── update_confluence_goldimage_table.py # Confluence table updater
    └── run_goldimage_pipeline.sh           # Unified pipeline runner
```

## Prerequisites

- **Python 3.6+** (stdlib only — no external packages required)
- **Bash** (for the pipeline runner script)
- Access tokens for GitHub, Jira, and Confluence (see [Configuration](#configuration))

## Configuration

Create a `src/.env` file with the following variables:

```bash
# GitHub
GITHUB_TOKEN=ghp_...           # PAT with repo read access

# Jira
JIRA_BASE_URL=https://jira.example.com
JIRA_API_TOKEN=...             # Personal access token (Bearer)

# Confluence
CONFLUENCE_BASE_URL=https://confluence.example.com
CONFLUENCE_EMAIL=user@example.com
CONFLUENCE_API_TOKEN=...       # API token or PAT (Bearer)
CONFLUENCE_PAGE_ID=123456      # Target page ID for the release table
```

> **Warning:** Never commit `src/.env`. It is excluded via `.gitignore`.

## Usage

All operations go through the unified pipeline script:

```bash
src/run_goldimage_pipeline.sh <action> <branch[,branch,...]> <count> [options]
```

### Actions

| Action | Description |
|---|---|
| `extract` | Fetch release PRs from GitHub, enrich with Jira Epics, and print a summary table |
| `update` | Update Confluence from an existing release JSON file |
| `pipeline` | Full end-to-end: extract + enrich + update Confluence |

### Options

| Flag | Description |
|---|---|
| `--dry-run` | Preview changes without updating Confluence |
| `--json-path <path>` | Path to existing JSON file (for `update` action) |

### Examples

```bash
# Extract latest release from master and print summary
src/run_goldimage_pipeline.sh extract master 1

# Extract last 10 releases from multiple branches
src/run_goldimage_pipeline.sh extract "master,ganges-7.6,ganges-7.5" 10

# Full pipeline: extract, enrich, and push to Confluence
src/run_goldimage_pipeline.sh pipeline master 10

# Dry-run the full pipeline (no Confluence writes)
src/run_goldimage_pipeline.sh pipeline master 5 --dry-run

# Update Confluence from a previously extracted JSON
src/run_goldimage_pipeline.sh update master 10 --json-path /tmp/release_graphql_master_10.json
```

## Components

### `github_release_extractor_graphql.py`

Extracts release data from GitHub using the GraphQL API. Identifies release PRs by title (default regex: `^Release`), collects associated PRs within each release window, and extracts version numbers, Jira ticket references, and CircleCI links.

Key features:
- Automatic retry with exponential backoff for transient errors (HTTP 429/502/503)
- Configurable PR and commit history limits
- Structured JSON output for downstream processing

### `search_jira_epic.py`

Reads the extractor's JSON output, derives the AOS version from each release title, and searches Jira for the matching Epic ticket. Outputs an enriched summary table with GoldImage version, Epic ticket links, Endor changelog/RPM URLs, and merge dates.

### `update_confluence_goldimage_table.py`

Reads the extractor's JSON output, performs Jira Epic enrichment, and updates a Confluence page with the release table. Deduplicates by GoldImage version — existing rows are preserved, and new releases are inserted at the top.

Confluence table columns:
- GoldImage Version
- Main Tickets
- Change Log
- RPM List
- Merge Date
- Notes

### `run_goldimage_pipeline.sh`

Orchestrator script that sources `src/.env`, dispatches to the appropriate action, and handles multi-branch runs. Provides a formatted status banner and final summary with pass/fail per branch.

## Confluence Table Format

The published table uses the following columns:

| GoldImage Version | Main Tickets | Change Log | RPM List | Merge Date | Notes |
|---|---|---|---|---|---|
| `main-master-rhel9.7-9.0.0` | JIRA-12345 | [link] | [link] | 2026-05-20 | — |

Releases are ordered newest-first. Duplicate versions are automatically skipped.

## MCP Integration

When used inside the Cursor IDE, the agent leverages MCP servers for real-time, interactive queries:

- **GitHub MCP** — Search and inspect PRs, issues, and repositories
- **Atlassian MCP** — Query Jira tickets and update Confluence pages
- **Sourcegraph MCP** — Search code across repositories

MCP server configuration lives in `.cursor/rules/mcp.json`.

## License

Internal use only — Nutanix proprietary.
