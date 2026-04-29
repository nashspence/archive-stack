#!/usr/bin/env bash

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="${ROOT_DIR}/compose.yml"
DEFAULT_ENV_FILE="${ROOT_DIR}/.env.compose.example"
LOCAL_ENV_FILE="${ROOT_DIR}/.env.compose"
PROD_HARNESS_ENV_FILE="${ROOT_DIR}/tests/harness/prod-harness.env"
APP_IMAGE_NAME="archive-stack-app:dev"
TEST_IMAGE_NAME="archive-stack-test:dev"

if [[ -f "${LOCAL_ENV_FILE}" ]]; then
  COMPOSE_ENV_FILE="${COMPOSE_ENV_FILE:-${LOCAL_ENV_FILE}}"
else
  COMPOSE_ENV_FILE="${COMPOSE_ENV_FILE:-${DEFAULT_ENV_FILE}}"
fi

compose() {
  docker compose \
    --file "${COMPOSE_FILE}" \
    --env-file "${COMPOSE_ENV_FILE}" \
    "$@"
}

compose_env_value() {
  local name="$1"
  local default="${2-}"
  if [[ -n "${!name-}" ]]; then
    printf '%s' "${!name}"
    return
  fi
  local line=""
  line="$(grep -E "^${name}=" "${COMPOSE_ENV_FILE}" | tail -n 1 || true)"
  if [[ -n "${line}" ]]; then
    printf '%s' "${line#*=}"
    return
  fi
  printf '%s' "${default}"
}

load_env_defaults() {
  local env_file="$1"
  local line=""
  local name=""
  local value=""

  if [[ ! -f "${env_file}" ]]; then
    printf 'missing env file: %s\n' "${env_file}" >&2
    exit 1
  fi

  while IFS= read -r line || [[ -n "${line}" ]]; do
    if [[ -z "${line}" || "${line}" == \#* ]]; then
      continue
    fi
    name="${line%%=*}"
    value="${line#*=}"
    if [[ -z "${name}" || "${name}" == "${line}" ]]; then
      printf 'invalid env assignment in %s: %s\n' "${env_file}" "${line}" >&2
      exit 1
    fi
    if [[ -z "${!name-}" ]]; then
      export "${name}=${value}"
    fi
  done < "${env_file}"
}

sanitize_compose_project_component() {
  local value="${1:-}"
  value="$(printf '%s' "${value}" | tr '[:upper:]' '[:lower:]' | tr -cs 'a-z0-9' '-')"
  value="${value#-}"
  value="${value%-}"
  if [[ -z "${value}" ]]; then
    printf 'user'
    return
  fi
  printf '%s' "${value}"
}

setup_test_compose_project() {
  if [[ -n "${COMPOSE_PROJECT_NAME:-}" ]]; then
    export COMPOSE_PROJECT_NAME
    export TEST_COMPOSE_PROJECT_ISOLATED="${TEST_COMPOSE_PROJECT_ISOLATED:-0}"
    return
  fi
  local project_name="${TEST_COMPOSE_PROJECT_NAME:-}"
  if [[ -z "${project_name}" ]]; then
    project_name="$(compose_env_value TEST_COMPOSE_PROJECT_NAME)"
  fi
  if [[ -n "${project_name}" ]]; then
    export COMPOSE_PROJECT_NAME="${project_name}"
    export TEST_COMPOSE_PROJECT_ISOLATED=0
    return
  fi
  export COMPOSE_PROJECT_NAME="archive-stack-test-$(sanitize_compose_project_component "${USER:-}")-$$"
  export TEST_COMPOSE_PROJECT_ISOLATED=1
}

configure_compose_tty() {
  COMPOSE_RUN_TTY_ARGS=()
  if [[ ! -t 0 || ! -t 1 ]]; then
    COMPOSE_RUN_TTY_ARGS=(-T)
  fi
}

isolate_test_compose_runtime() {
  local state_root="/app/.compose/${COMPOSE_PROJECT_NAME}"

  export ARC_TEST_HOST_STATE_ROOT="${ARC_TEST_HOST_STATE_ROOT:-${ROOT_DIR}/.compose/${COMPOSE_PROJECT_NAME}}"
  export ARC_API_PORT=0
  export ARC_WEBDAV_PORT=0
  export ARC_DB_PATH="${state_root}/state.sqlite3"
  export ARC_TEST_EXTERNAL_APP_DB_PATH="${ARC_DB_PATH}"
  export ARC_TEST_WEBHOOK_CAPTURE_PATH="${state_root}/webhook-captures.jsonl"
  export ARC_TEST_ACCEPTANCE_ROOT="${state_root}/acceptance"
}

cleanup_test_compose_runtime() {
  local status="${1:-0}"
  if [[ "${status}" != "0" || "${TEST_COMPOSE_PROJECT_ISOLATED:-0}" != "1" ]]; then
    return
  fi
  if [[ -z "${ARC_TEST_HOST_STATE_ROOT:-}" || "${ARC_TEST_HOST_STATE_ROOT}" == "/" ]]; then
    return
  fi
  docker run \
    --rm \
    --volume "${ROOT_DIR}:/app" \
    --entrypoint rm \
    "${TEST_IMAGE_NAME}" \
    -rf \
    "/app/.compose/${COMPOSE_PROJECT_NAME}"
}

ensure_compose_image() {
  local service="$1"
  compose build "${service}"
}
