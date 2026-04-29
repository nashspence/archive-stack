#!/usr/bin/env bash
set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_compose_env.sh"

setup_test_compose_project
configure_compose_tty

cleanup() {
  compose down --volumes --remove-orphans >/dev/null 2>&1 || true
}

trap cleanup EXIT

export ARC_ENABLE_TEST_CONTROL="${ARC_ENABLE_TEST_CONTROL:-1}"
export ARC_TEST_WEBHOOK_CAPTURE_PATH="${ARC_TEST_WEBHOOK_CAPTURE_PATH:-/app/.compose/webhook-captures.jsonl}"
export UPLOAD_EXPIRY_SWEEP_INTERVAL="${UPLOAD_EXPIRY_SWEEP_INTERVAL:-1s}"
export ARC_GLACIER_UPLOAD_SWEEP_INTERVAL="${ARC_GLACIER_UPLOAD_SWEEP_INTERVAL:-1s}"
export ARC_GLACIER_FAILURE_WEBHOOK_URL="${ARC_GLACIER_FAILURE_WEBHOOK_URL:-http://app:8000/_test/webhooks}"
load_env_defaults "${PROD_HARNESS_ENV_FILE}"
export ARC_GLACIER_RECOVERY_WEBHOOK_URL="${ARC_GLACIER_RECOVERY_WEBHOOK_URL:-http://app:8000/_test/webhooks}"

ensure_compose_image app
"$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/bootstrap_garage.sh"
compose up --detach webdav tusd
compose up --detach --wait app
compose run \
  --rm \
  "${COMPOSE_RUN_TTY_ARGS[@]}" \
  -e ARC_TEST_CANONICAL_ENTRYPOINT=1 \
  test \
  -q \
  -m acceptance \
  tests/harness/test_prod_harness.py \
  "$@"
