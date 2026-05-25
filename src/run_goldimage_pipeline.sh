#!/usr/bin/env bash
set -euo pipefail

# =============================================================================
# GoldImage Release Pipeline
# =============================================================================
# End-to-end pipeline: GitHub extraction → Jira Epic lookup → Confluence update
#
# Pipeline steps:
#   1. Extract release PRs from GitHub (via GraphQL API)
#   2. Enrich with AOS Epic tickets from Jira
#   3. Build GoldImage version, endor URLs (changelog + RPM)
#   4. Update Confluence page with new table format
#
# Table format on Confluence:
#   GoldImage Version | Main Tickets | Change Log | RPM List | Merge Date | Notes
#
# Usage:
#   src/run_goldimage_pipeline.sh <action> <branch[,branch,...]> <count> [options]
#
# Actions:
#   extract  — Extract from GitHub + enrich with Jira (no Confluence update)
#   update   — Update Confluence from an existing release JSON
#   pipeline — Full pipeline: extract + enrich + update Confluence
#
# Options:
#   --dry-run    Preview changes without updating Confluence
#   --json-path  Path to existing JSON (for 'update' action)
#
# Examples:
#   src/run_goldimage_pipeline.sh extract master 10
#   src/run_goldimage_pipeline.sh pipeline master 10
#   src/run_goldimage_pipeline.sh pipeline "master,ganges-7.6" 5
#   src/run_goldimage_pipeline.sh update master 10 --json-path /tmp/release_graphql_master_10.json
#   src/run_goldimage_pipeline.sh pipeline master 5 --dry-run
#
# Required environment variables (in src/.env):
#   GITHUB_TOKEN          — GitHub PAT with repo read access
#   JIRA_BASE_URL         — Jira server URL (e.g. https://jira.nutanix.com)
#   JIRA_API_TOKEN        — Jira personal access token (Bearer)
#   CONFLUENCE_BASE_URL   — Confluence server URL
#   CONFLUENCE_EMAIL      — Confluence user email
#   CONFLUENCE_API_TOKEN  — Confluence API token (Bearer)
#   CONFLUENCE_PAGE_ID    — Target Confluence page ID
# =============================================================================

ACTION="${1:-}"
BRANCH_ARG="${2:-}"
COUNT="${3:-}"
shift 3 2>/dev/null || true

# Parse optional flags
DRY_RUN_FLAG=""
JSON_PATH=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run)
      DRY_RUN_FLAG="--dry-run"
      shift
      ;;
    --json-path)
      JSON_PATH="${2:-}"
      shift 2
      ;;
    *)
      echo "Unknown option: $1"
      exit 2
      ;;
  esac
done

MAX_ERR_LINES=8

usage() {
  cat <<'EOF'
================================================================================
  GoldImage Release Pipeline
================================================================================

Usage: src/run_goldimage_pipeline.sh <action> <branch[,branch,...]> <count> [options]

Actions:
  extract   — Extract from GitHub + enrich with Jira Epics (no Confluence update)
  update    — Update Confluence from existing release JSON
  pipeline  — Full pipeline: extract → enrich → update Confluence

Options:
  --dry-run    Preview what would be updated without making changes
  --json-path  Path to existing JSON file (required for 'update' action)

Examples:
  src/run_goldimage_pipeline.sh extract master 10
  src/run_goldimage_pipeline.sh pipeline master 10
  src/run_goldimage_pipeline.sh pipeline "master,ganges-7.6,ganges-7.5" 5
  src/run_goldimage_pipeline.sh update master 10 --json-path /tmp/release_graphql_master_10.json
  src/run_goldimage_pipeline.sh pipeline master 5 --dry-run

Required Environment Variables (in src/.env):
  GITHUB_TOKEN, JIRA_BASE_URL, JIRA_API_TOKEN,
  CONFLUENCE_BASE_URL, CONFLUENCE_EMAIL, CONFLUENCE_API_TOKEN, CONFLUENCE_PAGE_ID
================================================================================
EOF
  exit 2
}

# Validate args
if [[ -z "${ACTION}" || -z "${BRANCH_ARG}" || -z "${COUNT}" ]]; then
  usage
fi

if ! [[ "${COUNT}" =~ ^[0-9]+$ ]] || [[ "${COUNT}" -le 0 ]]; then
  echo "Error: <count> must be a positive integer"
  exit 2
fi

ACTION_LOWER="$(echo "${ACTION}" | tr '[:upper:]' '[:lower:]')"
if [[ "${ACTION_LOWER}" != "extract" && "${ACTION_LOWER}" != "update" && "${ACTION_LOWER}" != "pipeline" ]]; then
  echo "Error: unknown action '${ACTION}'. Must be one of: extract, update, pipeline"
  exit 2
fi

# Source .env
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${SCRIPT_DIR}/.env"

if [[ -f "${ENV_FILE}" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "${ENV_FILE}"
  set +a
fi

# Validate required env vars
check_github_env() {
  if [[ -z "${GITHUB_TOKEN:-}" ]]; then
    echo "Error: GITHUB_TOKEN is missing (set in src/.env)"
    exit 2
  fi
}

check_jira_env() {
  for var in JIRA_BASE_URL JIRA_API_TOKEN; do
    if [[ -z "${!var:-}" ]]; then
      echo "Error: ${var} is missing (set in src/.env)"
      exit 2
    fi
  done
}

check_confluence_env() {
  for var in CONFLUENCE_BASE_URL CONFLUENCE_EMAIL CONFLUENCE_API_TOKEN CONFLUENCE_PAGE_ID; do
    if [[ -z "${!var:-}" ]]; then
      echo "Error: ${var} is missing (set in src/.env)"
      exit 2
    fi
  done
}

# Split branches
IFS=',' read -ra BRANCHES <<< "${BRANCH_ARG}"

ALL_JSON_FILES=()
FAILED_BRANCHES=()

json_path_for_branch() {
  local branch="$1"
  echo "/tmp/release_graphql_${branch//\//_}_${COUNT}.json"
}

# =============================================================================
# Step 1: Extract release data from GitHub
# =============================================================================
do_extract() {
  local branch="$1"
  local output_json="$2"

  check_github_env

  echo "  [GitHub] Extracting: branch=${branch} count=${COUNT} -> ${output_json}"

  local err_file
  err_file="$(mktemp)"
  if python3 "${SCRIPT_DIR}/github_release_extractor_graphql.py" \
    --repo nutanix-core/aos-goldimage-os \
    --mode prs \
    --branch "${branch}" \
    --base-branch "${branch}" \
    --pr-title-regex "^Release" \
    --latest-release-pr-count "${COUNT}" \
    --history-pr-limit 2500 \
    --history-commit-limit 10000 \
    --output-json "${output_json}" 2>"${err_file}"; then
    echo "  [GitHub] OK: ${branch}"
    rm -f "${err_file}"
    return 0
  else
    echo "  [GitHub] FAILED: ${branch}"
    head -n "${MAX_ERR_LINES}" "${err_file}" | head -c 500
    echo
    rm -f "${err_file}"
    return 1
  fi
}

# =============================================================================
# Step 2 + 3: Enrich with Jira + Update Confluence
# =============================================================================
do_confluence_update() {
  local branch="$1"
  local input_json="$2"

  check_jira_env
  check_confluence_env

  if [[ ! -f "${input_json}" ]]; then
    echo "  Error: JSON file not found at ${input_json}"
    return 1
  fi

  local cmd_args=(
    "${SCRIPT_DIR}/update_confluence_goldimage_table.py"
    --input-json "${input_json}"
    --branch "${branch}"
    --max-releases "${COUNT}"
  )

  if [[ -n "${DRY_RUN_FLAG}" ]]; then
    cmd_args+=(--dry-run)
  fi

  python3 "${cmd_args[@]}"
}

# =============================================================================
# Step: Print summary table (extract-only mode)
# =============================================================================
print_extract_summary() {
  local branch="$1"
  local input_json="$2"

  check_jira_env

  echo
  echo "  [Jira+Summary] Generating enriched table for ${branch}..."
  python3 "${SCRIPT_DIR}/search_jira_epic.py" \
    --input-json "${input_json}" \
    --branch "${branch}"
}

# =============================================================================
# Dispatch
# =============================================================================

echo
echo "╔══════════════════════════════════════════════════════════════════╗"
echo "║           GoldImage Release Pipeline                           ║"
echo "╠══════════════════════════════════════════════════════════════════╣"
printf "║  Action   : %-50s  ║\n" "${ACTION_LOWER}"
printf "║  Branches : %-50s  ║\n" "${BRANCH_ARG}"
printf "║  Count    : %-50s  ║\n" "${COUNT}"
printf "║  Mode     : %-50s  ║\n" "append (deduplicate)"
printf "║  Dry Run  : %-50s  ║\n" "${DRY_RUN_FLAG:-no}"
echo "╚══════════════════════════════════════════════════════════════════╝"
echo

case "${ACTION_LOWER}" in

  # ---------------------------------------------------------------------------
  # extract: GitHub → Jira enrichment → print summary table
  # ---------------------------------------------------------------------------
  extract)
    for branch in "${BRANCHES[@]}"; do
      ojson="$(json_path_for_branch "${branch}")"
      echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
      echo "  Branch: ${branch}"
      echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
      if do_extract "${branch}" "${ojson}"; then
        ALL_JSON_FILES+=("${ojson}")
        print_extract_summary "${branch}" "${ojson}"
      else
        FAILED_BRANCHES+=("${branch}")
      fi
      echo
    done
    ;;

  # ---------------------------------------------------------------------------
  # update: existing JSON → Jira enrichment → Confluence update
  # ---------------------------------------------------------------------------
  update)
    for branch in "${BRANCHES[@]}"; do
      if [[ -n "${JSON_PATH}" && ${#BRANCHES[@]} -eq 1 ]]; then
        ojson="${JSON_PATH}"
      else
        ojson="$(json_path_for_branch "${branch}")"
      fi
      echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
      echo "  Branch: ${branch}"
      echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
      if do_confluence_update "${branch}" "${ojson}"; then
        ALL_JSON_FILES+=("${ojson}")
      else
        FAILED_BRANCHES+=("${branch}")
      fi
      echo
    done
    ;;

  # ---------------------------------------------------------------------------
  # pipeline: GitHub → Jira enrichment → Confluence update (full flow)
  # ---------------------------------------------------------------------------
  pipeline)
    for branch in "${BRANCHES[@]}"; do
      ojson="$(json_path_for_branch "${branch}")"
      echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
      echo "  Branch: ${branch}"
      echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
      if do_extract "${branch}" "${ojson}"; then
        ALL_JSON_FILES+=("${ojson}")
        echo
        do_confluence_update "${branch}" "${ojson}" || true
      else
        FAILED_BRANCHES+=("${branch}")
      fi
      echo
    done
    ;;
esac

# =============================================================================
# Final status
# =============================================================================
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Pipeline Complete"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
if [[ ${#FAILED_BRANCHES[@]} -gt 0 ]]; then
  echo "  FAILED branches: ${FAILED_BRANCHES[*]}"
fi
echo "  JSON files: ${ALL_JSON_FILES[*]:-none}"
if [[ -n "${CONFLUENCE_PAGE_ID:-}" && "${ACTION_LOWER}" != "extract" ]]; then
  echo "  Confluence: ${CONFLUENCE_BASE_URL}/pages/viewpage.action?pageId=${CONFLUENCE_PAGE_ID}"
fi
echo
