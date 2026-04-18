#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="${ROOT_DIR}/docker-compose.test.yml"
export COMPOSE_PROJECT_NAME="${COMPOSE_PROJECT_NAME:-riverhog_tests}"
export COMPOSE_MENU="${COMPOSE_MENU:-false}"

cleanup() {
  docker compose -f "${COMPOSE_FILE}" down --volumes --remove-orphans >/dev/null 2>&1 || true
}

trap cleanup EXIT
cleanup

docker compose -f "${COMPOSE_FILE}" up --build --abort-on-container-exit --exit-code-from test --menu=false
