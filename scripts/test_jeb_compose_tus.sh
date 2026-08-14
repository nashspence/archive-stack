#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="${ROOT_DIR}/companions/jeb/server/compose.yaml"
RUNTIME_DIR="$(mktemp -d)"
export COMPOSE_PROJECT_NAME="riverhog-jeb-smoke-${RANDOM}-$$"
export JEB_LANDING_HOST_DIR="${RUNTIME_DIR}/landing"
export JEB_STATE_HOST_DIR="${RUNTIME_DIR}/state"
export JEB_API_TOKEN="jeb-compose-smoke-management-token"
export JEB_MUNCHY_URL="https://munchy.invalid"
export JEB_API_PORT=0
export JEB_TUS_PORT=0

compose() {
  docker compose --file "${COMPOSE_FILE}" "$@"
}

cleanup() {
  compose down --volumes --remove-orphans
  if docker image inspect jeb:dev >/dev/null 2>&1; then
    docker run --rm \
      --volume "${RUNTIME_DIR}:/runtime" \
      --entrypoint chmod \
      jeb:dev -R ugo+rwX /runtime
  fi
  rm -rf -- "${RUNTIME_DIR}"
}
trap cleanup EXIT

mkdir -p "${JEB_LANDING_HOST_DIR}" "${JEB_STATE_HOST_DIR}"
compose build jeb
compose up --detach --no-build --wait jeb-tus

api_port="$(compose port jeb 8081 | awk -F: 'END {print $NF}')"
ingress_port="$(compose port jeb-tus 1081 | awk -F: 'END {print $NF}')"
api_url="http://127.0.0.1:${api_port}"
ingress_url="http://127.0.0.1:${ingress_port}"

"${MISE_BIN:-mise}" x -- uv run --locked --all-packages --group dev \
  python "${ROOT_DIR}/scripts/jeb_compose_tus.py" upload \
  --api-url "${api_url}" \
  --ingress-url "${ingress_url}" \
  --management-token "${JEB_API_TOKEN}" \
  --landing-dir "${JEB_LANDING_HOST_DIR}" \
  --work-dir "${RUNTIME_DIR}/work"

compose restart jeb
for _attempt in $(seq 1 30); do
  if compose exec -T jeb python -c \
    "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8081/health/ready', timeout=2).read()" \
    >/dev/null 2>&1; then
    break
  fi
  sleep 1
done
compose exec -T jeb python -c \
  "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8081/health/ready', timeout=2).read()"
api_port="$(compose port jeb 8081 | awk -F: 'END {print $NF}')"
api_url="http://127.0.0.1:${api_port}"

"${MISE_BIN:-mise}" x -- uv run --locked --all-packages --group dev \
  python "${ROOT_DIR}/scripts/jeb_compose_tus.py" verify \
  --api-url "${api_url}" \
  --ingress-url "${ingress_url}" \
  --management-token "${JEB_API_TOKEN}" \
  --landing-dir "${JEB_LANDING_HOST_DIR}" \
  --work-dir "${RUNTIME_DIR}/work"

IFS= read -r upload_id < "${RUNTIME_DIR}/work/upload-id"
compose exec -T jeb sh -ceu '
  staging=/landing/.ingress/tus
  test ! -e "${staging}/${1}"
  test ! -e "${staging}/${1}.info"
  test ! -e "${staging}/.provenance/${1}"
' sh "${upload_id}"
