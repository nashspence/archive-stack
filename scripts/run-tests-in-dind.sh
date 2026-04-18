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

if (($# == 0)); then
  set -- pytest
elif [[ "$1" != "pytest" ]]; then
  set -- pytest "$@"
fi

docker compose -f "${COMPOSE_FILE}" build test
docker compose -f "${COMPOSE_FILE}" run --rm test "$@"
