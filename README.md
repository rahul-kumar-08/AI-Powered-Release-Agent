# AI-Powered Release Agent

An AI-driven automation tool that extracts GoldImage release PR data from the `nutanix-core/aos-goldimage-os` GitHub repository, validates merge status via Sourcegraph/Gerrit, enriches with Jira Epic metadata, and publishes structured release tables to Confluence.

## Overview

The pipeline runs end-to-end in a single command:

1. **Extract** — Fetch release commits from Sourcegraph (Gerrit) and GitHub, with revert detection and AOS/PC classification.
2. **Parse** — Build release rows from commit headings (source of truth for GoldImage version).
3. **Jira Filter** — Resolve merge dates from git-tracker comments, filter by EPIC status (Closed only).
4. **CI Status** — Fetch postmerge CircleCI status from GitHub.
5. **Download RPM** — Download rpm.txt artifacts from Artifactory.
6. **Generate Changelog** — Produce changelog.txt from template with RPM diff.
7. **SFTP Upload** — Upload changelog and RPM files to hoth.
8. **Endor Publish** — Trigger Jenkins PUBLISH_GOLD_IMAGE to push files to endor-cache-2.
9. **Confluence Upload** — Update release table on Confluence (auto-routed by branch + AOS/PC type).

## Project Structure

```
AI-Powered-Release-Agent/
├── release_query.py              # Main pipeline entry point (run from root)
├── agent_runner.py               # Mission decomposition + tool dispatch (cursor-sdk)
├── src/                          # Pipeline stage modules
│   ├── config.py                 # Shared constants, _log(), MCP wrapper
│   ├── extract.py                # Sourcegraph + GitHub data fetching
│   ├── jira_client.py            # Jira EPIC resolution, status filtering, git-tracker
│   ├── version.py                # Version parsing, validation, row building
│   ├── artifactory.py            # RPM download from Artifactory
│   ├── changelog.py              # Changelog generation from template
│   ├── sftp.py                   # SFTP upload to hoth
│   ├── endor.py                  # Jenkins publish to endor + URL rewrite
│   ├── confluence.py             # Confluence table upload
│   └── formatter.py              # Output formatting (table/json/markdown)
├── tools/                        # MCP client modules
│   ├── .env                      # Secrets (DO NOT commit)
│   ├── mcp_client.py             # Shared MCP client (fastmcp wrapper)
│   ├── mcp_github_client.py      # GitHub MCP + REST API (commit details, CI status)
│   ├── mcp_sourcegraph_client.py # Sourcegraph MCP (commit search)
│   ├── mcp_confluence_client.py  # Confluence MCP (auto page routing)
│   ├── mcp_ticket_validator.py   # Ticket validation via Sourcegraph
│   └── jenkins_tool.py           # Jenkins PUBLISH_GOLD_IMAGE trigger
├── steps/                        # Pre-built step files for zero-LLM-cost runs
│   ├── master-full.json          # Full pipeline: master, 5 releases
│   ├── master-view.json          # View-only: master, no uploads
│   ├── ganges-7.6-full.json      # Full pipeline: ganges-7.6
│   └── ...
├── templates/
│   └── changelog.template        # Changelog template for generation
├── .cursor/rules/
│   ├── mcp.json                  # MCP server configuration
│   ├── release-agent.mdc         # Agent guardrails + request routing
│   ├── goldimage-table-format.mdc    # Output table format spec (on-demand)
│   ├── confluence-release-update-workflow.mdc  # Confluence workflow (on-demand)
│   └── release-version-mismatch-detection.mdc  # Version mismatch detection (on-demand)
├── AGENTS.md                     # Agent identity, architecture, conventions
├── requirements.txt
└── releases/                     # Downloaded RPM/changelog artifacts (gitignored)
```

## Prerequisites

- **Python 3.10+** (3.14 recommended) with `fastmcp`, `cursor-sdk`, `paramiko`
- Access tokens for GitHub, Jira, Confluence, and Sourcegraph (see [Configuration](#configuration))

## Configuration

Create a `tools/.env` file:

```bash
# GitHub
GITHUB_TOKEN=ghp_...

# Sourcegraph
SOURCEGRAPH_TOKEN=...

# Jira
JIRA_BASE_URL=https://jira.example.com
JIRA_API_TOKEN=...

# Confluence
CONFLUENCE_BASE_URL=https://confluence.example.com
CONFLUENCE_API_TOKEN=...
AOS_CONFLUENCE_PAGE_ID=123456     # Parent page for AOS releases
PC_CONFLUENCE_PAGE_ID=789012      # Parent page for PC releases
CONFLUENCE_PAGE_ID=123456         # Fallback parent page

# Artifactory (for RPM downloads)
ARTIFACTORY_BASE=...
ARTIFACTORY_API_STORAGE=...

# SFTP (for changelog/rpm upload)
SFTP_HOST=upload.hoth.corp.nutanix.com
SFTP_USERNAME=...
SFTP_PASSWORD=...
SFTP_REMOTE_PATH=...

# Jenkins (for endor publish)
JENKINS_URL=...
JENKINS_USER=...
JENKINS_TOKEN=...
```

> **Warning:** Never commit `tools/.env`. It is excluded via `.gitignore`.

## Usage

### Full pipeline: `release_query.py`

```bash
# Last 5 releases from master (full pipeline: extract → upload → confluence)
python3 release_query.py --branch master --count 5

# PC-only from ganges-7.6
python3 release_query.py --branch ganges-7.6 --count 5 --filter pc

# View-only (skip uploads)
python3 release_query.py --branch master --count 5 --no-upload-sftp --no-publish-endor --no-upload-confluence

# JSON output
python3 release_query.py --branch master --count 3 --format json --output /tmp/releases.json
```

Key flags: `--branch`, `--count N`, `--filter all|aos|pc`, `--format table|json|markdown`, `--output <path>`, `--with-sg-date`, `--with-github-date`, `--no-ci-status`, `--no-upload-sftp`, `--no-publish-endor`, `--no-upload-confluence`, `--force-publish-endor`, `--force-rebuild-confluence`, `--validate-urls`.

### Agent runner: `agent_runner.py`

```bash
# Natural language mission (uses Cursor SDK for decomposition)
python3 agent_runner.py "Extract last 5 releases from master"

# Pre-built steps (zero LLM token cost)
python3 agent_runner.py --steps-json steps/master-full.json "master pipeline"
python3 agent_runner.py --steps-json steps/ganges-7.6-pc-full.json "ganges-7.6 PC"

# Dry run
python3 agent_runner.py --dry-run "Full pipeline for ganges-7.5"
```

### Pre-built step files (`steps/`)

For routine pipeline runs with zero LLM token cost:

| File | Description |
|---|---|
| `master-full.json` | Full pipeline: master, 5 releases |
| `master-view.json` | View-only: master, no uploads |
| `ganges-7.5-full.json` | Full pipeline: ganges-7.5 |
| `ganges-7.5-view.json` | View-only: ganges-7.5 |
| `ganges-7.6-full.json` | Full pipeline: ganges-7.6 |
| `ganges-7.6-view.json` | View-only: ganges-7.6 |
| `ganges-7.6-pc-full.json` | Full pipeline: ganges-7.6 PC only |
| `ganges-7.6-latest-full.json` | Full pipeline: ganges-7.6, latest 1 |

### Confluence update

The pipeline uploads to Confluence automatically. For standalone updates:

```bash
python3 tools/mcp_confluence_client.py --input-json /tmp/releases.json --branch master
python3 tools/mcp_confluence_client.py --input-json /tmp/releases.json --branch ganges-7.5 --type PC --force-rebuild
```

## Confluence Auto Page Routing

The tool automatically manages Confluence pages:

1. `AOS_CONFLUENCE_PAGE_ID` / `PC_CONFLUENCE_PAGE_ID` point to separate parent pages per release type
2. Child pages are matched by branch name + release type in the title
3. New child pages are created if no match exists (e.g. "PC Release ganges-7.3")
4. Entries are deduplicated by GoldImage version, sorted newest-first
5. Falls back to `CONFLUENCE_PAGE_ID` if type-specific IDs are not set

## Output Format

| GoldImage Version | Main Ticket | Change Log | RPM List | Merge Date | Notes |
|---|---|---|---|---|---|
| `main-master-rhel9.8-9.0.0` | ENG-941559 | [changelog] | [rpm] | 30-May-2026 | master |

- Merge Date is the **Gerrit CR date** (not GitHub PR date)
- Combined AOS/PC releases produce separate rows
- Only releases with Closed Jira EPIC status are included
- Invalid URLs show "Data not found"

## MCP Integration

When used inside the Cursor IDE, the agent leverages MCP servers:

- **GitHub MCP** — Search and inspect PRs, issues, and repositories
- **Atlassian MCP** — Query Jira tickets and update Confluence pages
- **Sourcegraph MCP** — Search code and validate CR merges on Gerrit

MCP server configuration lives in `.cursor/rules/mcp.json`.

## License

Internal use only — Nutanix proprietary.
