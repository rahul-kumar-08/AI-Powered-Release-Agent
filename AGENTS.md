# AGENTS.md

## Release Agent Identity

You are a **Release Agent** for the `nutanix-core/aos-goldimage-os` repository.

**Scope:** Extract release PR data from GitHub, validate CR merge status via Sourcegraph/Gerrit, present release tables, and update Confluence when instructed.

**Out of scope:** Code changes, deployments, builds, CI/CD operations.

## Architecture

```
AI-Powered-Release-Agent/
├── release_query.py          # Main pipeline entry point (run from root)
├── agent_runner.py           # Mission decomposition + tool dispatch (cursor-sdk)
├── src/                      # Pipeline stage modules
│   ├── config.py             # Shared constants, _log(), MCP wrapper
│   ├── extract.py            # Sourcegraph + GitHub data fetching
│   ├── jira_client.py        # Jira EPIC resolution, status filtering, git-tracker
│   ├── version.py            # Version parsing, validation, row building
│   ├── artifactory.py        # RPM download from Artifactory
│   ├── changelog.py          # Changelog generation from template
│   ├── sftp.py               # SFTP upload to hoth
│   ├── endor.py              # Jenkins publish to endor + URL rewrite
│   ├── confluence.py         # Confluence table upload
│   └── formatter.py          # Output formatting (table/json/markdown)
├── tools/                    # MCP client modules
│   ├── .env                  # Secrets (DO NOT commit)
│   ├── mcp_client.py         # Shared MCP client (fastmcp wrapper)
│   ├── mcp_github_client.py  # GitHub MCP + REST API
│   ├── mcp_sourcegraph_client.py  # Sourcegraph MCP
│   ├── mcp_confluence_client.py   # Confluence MCP (auto page routing)
│   └── mcp_ticket_validator.py    # Ticket validation via Sourcegraph
└── .cursor/rules/
    ├── mcp.json              # MCP server config
    ├── release-agent.mdc     # Agent guardrails + request routing
    ├── goldimage-table-format.mdc  # Output table format spec
    └── confluence-release-update-workflow.mdc  # Confluence workflow
```

## release_query.py — Main Pipeline

Full pipeline in a single run. Stages: Extract → Parse → Jira Filter → CI Status → Ticket Summaries → Build Prev Rows → Download RPM → Generate Changelog → SFTP Upload → Endor Publish → Confluence Upload → Output.

```bash
python3 release_query.py --branch master --count 5
python3 release_query.py --branch ganges-7.5 --count 10 --filter pc --with-sg-date
python3 release_query.py --branch master --count 3 --format json --output /tmp/out.json
```

Key flags: `--branch`, `--count N`, `--filter all|aos|pc`, `--format table|json|markdown`, `--output <path>`, `--with-sg-date`, `--with-github-date`, `--no-ci-status`, `--validate-urls`, `--no-upload-confluence`, `--no-upload-sftp`, `--no-publish-endor`, `--force-publish-endor`, `--force-rebuild-confluence`.

## agent_runner.py — Mission Dispatch

Decomposes natural-language missions into tool steps via Cursor SDK, then dispatches with retry logic. Accepts `--steps-json` for pre-built zero-LLM-cost runs.

```bash
python3 agent_runner.py "Extract last 5 releases from master"
python3 agent_runner.py --steps-json steps/master-5.json "master pipeline"
```

## Required Environment Variables (in `tools/.env`)

`GITHUB_TOKEN`, `SOURCEGRAPH_TOKEN`, `JIRA_BASE_URL`, `JIRA_API_TOKEN`, `CONFLUENCE_BASE_URL`, `CONFLUENCE_API_TOKEN`, `AOS_CONFLUENCE_PAGE_ID`, `PC_CONFLUENCE_PAGE_ID`, `CONFLUENCE_PAGE_ID` (fallback).

## Workflow

**Primary (fast-path):** Run `python3 release_query.py` with appropriate flags. The script handles everything concurrently.

**Fallback 1 (MCP):** If `release_query.py` fails, use GitHub MCP tools (`search_issues`, `get_pull_request`) + Sourcegraph MCP (`commit_search`) against Gerrit repo `nugerrit.ntnxdpro.com/main`.

**Fallback 2 (direct tools):** Run `mcp_github_client.py`, `mcp_sourcegraph_client.py`, `mcp_confluence_client.py` individually.

## Key Conventions

- **GitHub PR merge ≠ CR merge.** Gerrit repo (`nugerrit.ntnxdpro.com/main`) is source of truth. CRs auto-submitted by `svc.jenkins.autosub`.
- Release PRs identified by title matching `^Release`. Combined AOS/PC titles split on `/PC:`.
- Versions containing `ganges-pc.` are PC, not AOS.
- Reverted releases excluded via version-based timeline analysis.
- Only releases with **Closed** Jira EPIC status included in output and Confluence.
- Confluence: `AOS_CONFLUENCE_PAGE_ID` / `PC_CONFLUENCE_PAGE_ID` are separate parent pages; child pages auto-discovered per branch. Deduplicated by GoldImage version, sorted newest-first.
- All timestamps UTC from Gerrit commit dates via Sourcegraph.
- For Gerrit repos, push to `refs/for/<branch>` for review — never push directly.

## Cursor Cloud specific instructions

Environment basics: Python 3 CLI project, no build artifacts, no server/port. Always run from the repo root so the `src.` / `tools.` package imports resolve (e.g. `python3 release_query.py ...`, not from inside `src/`). Standard commands live in the `release_query.py` / `agent_runner.py` sections above and in `README.md`.

- **Dependencies:** `pip install -r requirements.txt` (the update script does this on startup). `pandas` + `tabulate` are required at runtime even though they look like dev-only deps — `src/formatter.py` and `tools/mcp_client.py` render tables via `DataFrame.to_markdown()` (which needs the `tabulate` backend), and `agent_runner.py` summaries use pandas too.
- **No tests, no linter config:** there is no test suite and no committed flake8/ruff/pylint/mypy config. Use `python3 -m compileall release_query.py agent_runner.py src tools` as the syntax/build check. (Source files carry `# noqa` markers, implying flake8 was used historically, but no config ships.)
- **Live runs need secrets that are NOT in the repo:** running `release_query.py` (or `agent_runner.py` without `--dry-run`) first calls `validate_mcp_tokens()`, which reads the gitignored `.cursor/rules/mcp.json` (MCP gateway URLs + auth headers). Without that file it exits 1 with "Server '…' not found in mcp.json". A full end-to-end run also needs `SOURCEGRAPH_TOKEN`, `GITHUB_TOKEN`, and Jira/Confluence tokens (in `tools/.env`). None of these are present in the cloud VM by default.
- **Offline verification without secrets:** `python3 agent_runner.py --dry-run --steps-json steps/<file>.json "msg"` exercises mission dispatch + summary tables with zero network. To verify the signature release-table output stage, feed rows (schema in `src/version.py`: `goldimage_version`, `main_ticket`, `changelog_url`, `rpm_url`, `merge_date`, `notes`) through `src.formatter.format_table` / `format_markdown` / `format_json`.
- **`agent_runner.py` natural-language mode** (no `--steps-json`) needs `CURSOR_API_KEY` to reach the Cursor SDK for mission decomposition; `--steps-json` runs skip the SDK entirely.
