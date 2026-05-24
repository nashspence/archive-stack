#!/usr/bin/env bash
set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_compose_env.sh"

stop_project() {
  local project_name="$1"
  local isolated="${2:-0}"

  export COMPOSE_PROJECT_NAME="${project_name}"
  export TEST_COMPOSE_PROJECT_ISOLATED="${isolated}"
  compose down --volumes --remove-orphans

  if [[ "${isolated}" == "1" ]]; then
    cleanup_test_compose_runtime 0
  fi
}

if [[ -n "${COMPOSE_PROJECT_NAME:-}" ]]; then
  stop_project "${COMPOSE_PROJECT_NAME}" 0
  exit 0
fi

if [[ -n "${TEST_COMPOSE_PROJECT_NAME:-}" ]]; then
  stop_project "${TEST_COMPOSE_PROJECT_NAME}" 0
  exit 0
fi

mapfile -t projects < <(
  docker ps \
    --all \
    --filter label=com.docker.compose.project \
    --format '{{.Label "com.docker.compose.project"}}' \
    | sort -u \
    | grep -E '^riverhog-test-[[:alnum:]-]+-[0-9]+$' \
    || true
)

if [[ "${#projects[@]}" -eq 0 ]]; then
  printf 'No in-flight isolated prod-backed Compose project found.\n'
  exit 0
fi

for project in "${projects[@]}"; do
  stop_project "${project}" 1
done
