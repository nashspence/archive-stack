#!/usr/bin/env bash
set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_compose_env.sh"

setup_test_compose_project
configure_compose_tty
isolate_test_compose_runtime

cleanup() {
  local status="$?"
  if [[ "${TEST_COMPOSE_PROJECT_ISOLATED:-0}" == "1" ]]; then
    compose down --volumes --remove-orphans >/dev/null 2>&1 || true
  else
    compose down --remove-orphans >/dev/null 2>&1 || true
  fi
  cleanup_test_compose_runtime "${status}"
  return "${status}"
}

trap cleanup EXIT

export RIVERHOG_ENABLE_TEST_CONTROL="${RIVERHOG_ENABLE_TEST_CONTROL:-1}"
export RIVERHOG_TEST_WEBHOOK_CAPTURE_PATH="${RIVERHOG_TEST_WEBHOOK_CAPTURE_PATH:-/app/.compose/webhook-captures.jsonl}"
export UPLOAD_EXPIRY_SWEEP_INTERVAL="${UPLOAD_EXPIRY_SWEEP_INTERVAL:-1s}"
export RIVERHOG_GLACIER_UPLOAD_SWEEP_INTERVAL="${RIVERHOG_GLACIER_UPLOAD_SWEEP_INTERVAL:-1s}"
export RIVERHOG_OPERATOR_WEBHOOK_URL="${RIVERHOG_OPERATOR_WEBHOOK_URL:-http://app:8000/_test/webhooks}"
load_env_defaults "${PROD_HARNESS_ENV_FILE}"

ensure_compose_image app
"$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/bootstrap_garage.sh"
compose up --detach webdav tusd
compose up --detach --wait app
compose run \
  --rm \
  "${COMPOSE_RUN_TTY_ARGS[@]}" \
  -e RIVERHOG_TEST_CANONICAL_ENTRYPOINT=1 \
  test \
  -q \
  -m acceptance \
  tests/harness/test_prod_harness.py \
  "$@"
