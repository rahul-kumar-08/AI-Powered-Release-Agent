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

- `GITHUB_TOKEN` (https://github.com/settings/tokens)
- `SOURCEGRAPH_TOKEN` (https://sourcegraph.ntnxdpro.com/users/<User-Name>/settings/tokens)
- `JIRA_BASE_URL`, `JIRA_API_TOKEN` (or `JIRA_TOKEN`) (https://jira.nutanix.com/secure/ViewProfile.jspa?selectedTab=com.atlassian.pats.pats-plugin:jira-user-personal-access-tokens)
- `CONFLUENCE_BASE_URL`, `CONFLUENCE_API_TOKEN` (or `CONFLUENCE_TOKEN`) (https://confluence.eng.nutanix.com:8443/plugins/personalaccesstokens/usertokens.action)
- `CONFLUENCE_PAGE_ID` (single parent page for both AOS and PC from the link) (https://confluence.eng.nutanix.com:8443/spaces/~<User-Name>/pages/`569444493`/CVM+GoldImage+GI+Releases)
- `JENKINS_BASE`, `JENKINS_USER`, `JENKINS_TOKEN` (https://phx-p10y-jenkins-master-prod-2.p10y.eng.nutanix.com/user/<USer-name>/configure)
- `CURSOR_API_KEY` (required for `agent_runner.py`) (https://cursor.com/dashboard/api?section=user-keys#user-api-keys)

Also set Artifactory and SFTP variables if RPM/changelog upload stages are enabled.

> Never commit `tools/.env`.

## Predefined .env parameters
```
Below data required for SFTP upload of generated report to Hoth server. 
SFTP_HOST=upload.hoth.corp.nutanix.com
SFTP_USERNAME=
SFTP_PASSWORD=
SFTP_PORT=22
BASE_URL=https://hoth.corp.nutanix.com/security/<Specific -Dir>
SFTP_REMOTE_PATH=/mnt/phxitafsprd1/security/security/<Specific -Dir>
#/public_html/GoldImage/


Conflunence page IDs for AOS and PC release notes. These are used by the agent to fetch the latest release notes and include them in the report.
CONFLUENCE_PAGE_ID=

CURSOR API key for fetching relevant code snippets from the codebase. This is used by the agent to provide more context in the report by including relevant code snippets.
CURSOR_API_KEY=

Repository information for fetching code snippets and other relevant data.
DEFAULT_REPO=nugerrit.ntnxdpro.com/main
GITHUB_REPO=github.com/nutanix-core/aos-goldimage-os

Artifactory information for fetching RPMs and other build artifacts.
ARTIFACTORY_BASE=https://artifactory.dyn.ntnxdpro.com:443/artifactory/local-canaveral-generic/nutanix-core/aos-goldimage/os/build-artifacts/{build_num}
ARTIFACTORY_API_STORAGE=https://artifactory.dyn.ntnxdpro.com:443/artifactory/api/storage/local-canaveral-generic/nutanix-core/aos-goldimage/os/build-artifacts/{build_num}
USER=<nutanix_mail_id>
ARTIFACTORY_TOKEN=

Jenkins information for fetching build information and other relevant data.
JENKINS_BASE=https://phx-p10y-jenkins-master-prod-2.p10y.eng.nutanix.com
JENKINS_USER=<Jenkins-User-ID>
JENKINS_TOKEN=

GitHub and Sourcegraph tokens for MCP server validation. These are used by the agent to validate the connectivity and authentication with the MCP servers before starting the pipeline.
GITHUB_TOKEN=
SOURCEGRAPH_TOKEN=
CONFLUENCE_TOKEN=
JIRA_TOKEN=
```

## Usage

### `release_query.py` (main entry point)

```bash
# Full pipeline for latest releases on master
python3 release_query.py --branch master --count 5

# Auto-count mode (count omitted): fetch only releases newer than Confluence latest
python3 release_query.py --branch ganges-7.6 --filter all

# Explicitly force Confluence lookup first even when count is given
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
| `--count` | Number of releases to return; with `--filter all`, count is per type (AOS + PC) |
| `--since-confluence` | Force Confluence lookup first; with explicit `--count`, still honors requested count |
| `--filter all\|aos\|pc` | Release type filter (`all` returns up to N per type) |
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
- It enforces Confluence-first lookup for `release_query` steps.
- It prints stage-wise pipeline status and discrepancies at the end.

#### Interactive mode (recommended for ad-hoc runs)

Start the runner without a mission:

```bash
python3 agent_runner.py
```

Then enter missions one-by-one in the prompt.

Example interactive session:

```text
$ python3 agent_runner.py
Interactive mode started. Type your mission and press Enter.

> Extract last 3 releases from master in table format
# Runner decomposes mission -> executes release_query.py with mapped flags
# Output: release table + stage summary

> Get releases from ganges-7.5 and update confluence
# Runner executes extraction + upload stages (unless disabled by flags/policy)
# Output: Confluence update result + stage summary

> quit
```

Tips:

- Use natural language; `agent_runner.py` maps intent to executable steps.
- For read-only runs, include wording like "view only" or use `release_query.py --no-upload`.
- Use `--dry-run` with a mission to preview planned steps without executing them.

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

- Uses `AOS_CONFLUENCE_PAGE_ID` and `PC_CONFLUENCE_PAGE_ID` when configured
  (falls back to `CONFLUENCE_PAGE_ID`).
- Auto-routes to branch/type-specific child pages under the resolved parent page.
- Deduplicates by GoldImage version and keeps newest-first ordering.
- In lookup mode, Confluence latest entries are used as baseline context before
  release extraction.

Final output Confluence page:

- [CVM GoldImage GI Releases](https://confluence.eng.nutanix.com:8443/spaces/INFRA/pages/86889720/CVM+GoldImage+GI+Releases)

## License

Internal use only (Nutanix proprietary).
