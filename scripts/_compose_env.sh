#!/usr/bin/env bash

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="${ROOT_DIR}/riverhog/server/compose.yaml"
DEFAULT_ENV_FILE="${ROOT_DIR}/.env.compose.example"
LOCAL_ENV_FILE="${ROOT_DIR}/.env.compose"
APP_IMAGE_NAME="riverhog-app:dev"
TEST_IMAGE_NAME="riverhog-test:dev"

if [[ -f "${LOCAL_ENV_FILE}" ]]; then
  COMPOSE_ENV_FILE="${COMPOSE_ENV_FILE:-${LOCAL_ENV_FILE}}"
else
  COMPOSE_ENV_FILE="${COMPOSE_ENV_FILE:-${DEFAULT_ENV_FILE}}"
fi
export RIVERHOG_COMPOSE_ENV_FILE="${COMPOSE_ENV_FILE}"

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
    return
  fi
  local project_name="${TEST_COMPOSE_PROJECT_NAME:-}"
  if [[ -z "${project_name}" ]]; then
    project_name="$(compose_env_value TEST_COMPOSE_PROJECT_NAME)"
  fi
  if [[ -n "${project_name}" ]]; then
    export COMPOSE_PROJECT_NAME="${project_name}"
    return
  fi
  export COMPOSE_PROJECT_NAME="riverhog-test-$(sanitize_compose_project_component "${USER:-}")-$$"
}

configure_compose_tty() {
  COMPOSE_RUN_TTY_ARGS=()
  if [[ ! -t 0 || ! -t 1 ]]; then
    COMPOSE_RUN_TTY_ARGS=(-T)
  fi
}

ensure_compose_image() {
  local service="$1"
  compose build --sbom=true "${service}"
}
