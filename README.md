# AI-Powered Release Agent

Release automation for `nutanix-core/aos-goldimage-os`: extract GoldImage releases, validate merge state, enrich with Jira metadata, and optionally publish artifacts and release rows to Confluence.

## What the pipeline does

`release_query.py` runs these stages in one flow:

1. Fetch release commits from Gerrit/Sourcegraph and GitHub.
2. Parse release rows from commit headings (source of truth for GoldImage version).
3. Resolve merge dates and filter by Jira EPIC status.
4. Fetch CI status (optional).
5. Download `rpm.txt` from Artifactory.
6. Generate changelog files.
7. Upload changelog/RPM files to SFTP.
8. Publish to endor via Jenkins.
9. Upload deduplicated release rows to Confluence.

Before execution, required MCP/service tokens are validated.

## Prerequisites

- Python `3.10+`
- Dependencies from `requirements.txt`
- Valid tokens/secrets in `tools/.env`

```bash
pip install -r requirements.txt
```

## Configuration (`tools/.env`)

Commonly used variables:

- `GITHUB_TOKEN`
- `SOURCEGRAPH_TOKEN`
- `JIRA_BASE_URL`, `JIRA_API_TOKEN` (or `JIRA_TOKEN`)
- `CONFLUENCE_BASE_URL`, `CONFLUENCE_API_TOKEN` (or `CONFLUENCE_TOKEN`)
- `AOS_CONFLUENCE_PAGE_ID`, `PC_CONFLUENCE_PAGE_ID` (or fallback `CONFLUENCE_PAGE_ID`)
- `JENKINS_BASE`, `JENKINS_USER`, `JENKINS_TOKEN`
- `CURSOR_API_KEY` (required for `agent_runner.py`)

Also set Artifactory and SFTP variables if RPM/changelog upload stages are enabled.

> Never commit `tools/.env`.

## Usage

### `release_query.py` (main entry point)

```bash
# Full pipeline for latest releases on master
python3 release_query.py --branch master --count 5

# Auto-count mode (count omitted): fetch only releases newer than Confluence latest
python3 release_query.py --branch ganges-7.6 --filter all

# Explicitly force Confluence-based auto-count even when count is given
python3 release_query.py --branch master --count 10 --since-confluence

# View-only output (skip SFTP + endor publish + Confluence upload)
python3 release_query.py --branch master --count 5 --no-upload

# JSON export
python3 release_query.py --branch master --count 3 --format json --output /tmp/releases.json

# Include additional date columns
python3 release_query.py --branch master --count 3 --with-github-date --with-sg-date
```

Supported flags:

| Flag | Description |
|---|---|
| `--branch` | Target branch (default `master`) |
| `--count` | Number of releases; when omitted, auto-counts from Confluence baseline |
| `--since-confluence` | Force Confluence-based auto-count logic |
| `--filter all\|aos\|pc` | Release type filter |
| `--format table\|markdown\|json` | Output format |
| `--output PATH` | Save JSON output |
| `--validate-urls` | HEAD-check generated changelog/RPM URLs |
| `--with-github-date` | Add GitHub PR merge date column |
| `--with-sg-date` | Add Sourcegraph/Gerrit merge date column |
| `--no-ci-status` | Skip CI status fetch |
| `--no-upload` | Skip SFTP upload, endor publish, and Confluence upload |
| `--force-publish-endor` | Republish to endor even when already present |
| `--rpm-dir PATH` | Download directory for RPM files |
| `--server` | MCP server key from `.cursor/rules/mcp.json` |

### `agent_runner.py` (natural-language dispatcher)

```bash
# Natural language mission
python3 agent_runner.py "Extract last 5 releases from master"

# Update Confluence through mission intent
python3 agent_runner.py "Get releases from ganges-7.5 and update confluence"

# Use prebuilt step file (no decomposition token cost)
python3 agent_runner.py --steps-json steps/master-full.json "master pipeline"

# Dry run
python3 agent_runner.py --dry-run "Full pipeline for ganges-7.5"
```

Runner behavior:

- If no mission is provided, it starts interactive mode.
- It validates tokens before executing steps.
- It prints stage-wise pipeline status and discrepancies at the end.

### Available prebuilt steps (`steps/`)

- `master-full.json`
- `master-view.json`
- `ganges-7.5-full.json`
- `ganges-7.5-view.json`
- `ganges-7.6-full.json`
- `ganges-7.6-view.json`
- `ganges-7.6-pc-full.json`
- `ganges-7.6-latest-full.json`

## Output

Release output is rendered in table/markdown/json and includes columns such as:

- GoldImage Version
- Main Ticket
- Change Log
- RPM List
- Merge Date
- Notes

Pipeline runs also print a stage summary (extracted rows, CI checks, RPM/changelog generation, upload/publish, Confluence result).

## Confluence behavior

- Uses separate parent pages for AOS/PC when configured.
- Auto-routes to branch-specific child pages.
- Deduplicates by GoldImage version and keeps newest-first ordering.
- Falls back to `CONFLUENCE_PAGE_ID` if type-specific page IDs are not set.

## License

Internal use only (Nutanix proprietary).
