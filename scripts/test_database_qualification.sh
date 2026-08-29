#!/usr/bin/env bash
set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_compose_env.sh"

if [[ -z "${DATABASE_QUALIFICATION_OUTPUT:-}" ]]; then
  printf '%s\n' 'DATABASE_QUALIFICATION_OUTPUT is required.' >&2
  exit 2
fi
if [[ -z "${DATABASE_QUALIFICATION_SOURCE_SHA:-}" ]]; then
  printf '%s\n' 'DATABASE_QUALIFICATION_SOURCE_SHA is required.' >&2
  exit 2
fi

output="$(realpath -m "${DATABASE_QUALIFICATION_OUTPUT}")"
output_dir="$(dirname "${output}")"
output_name="$(basename "${output}")"
mkdir -p "${output_dir}"

source_sha="$(git -C "${ROOT_DIR}" rev-parse --verify HEAD)"
if [[ "${source_sha}" != "${DATABASE_QUALIFICATION_SOURCE_SHA}" ]]; then
  printf 'Qualification source mismatch: %s != %s\n' \
    "${source_sha}" "${DATABASE_QUALIFICATION_SOURCE_SHA}" >&2
  exit 2
fi
if [[ -n "$(git -C "${ROOT_DIR}" status --porcelain)" ]]; then
  printf '%s\n' 'Database qualification requires a clean exact-source checkout.' >&2
  exit 2
fi
export SOURCE_REVISION="${source_sha}"

setup_test_compose_project
configure_compose_tty
ensure_compose_image test
image_revision="$(
  docker image inspect "${TEST_IMAGE_NAME}" \
    --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}'
)"
if [[ "${image_revision}" != "${source_sha}" ]]; then
  printf 'Qualification image source mismatch: %s != %s\n' \
    "${image_revision}" "${source_sha}" >&2
  exit 2
fi

cleanup() {
  compose down --volumes --remove-orphans
}
trap cleanup EXIT

compose up --detach --wait postgres

database_url="postgresql+psycopg://$(compose_env_value POSTGRES_USER riverhog):$(compose_env_value POSTGRES_PASSWORD riverhog)@postgres:5432/$(compose_env_value POSTGRES_DB riverhog)"
compose run --rm --no-deps \
  --entrypoint python \
  --env "RIVERHOG_TEST_POSTGRES_URL=${database_url}" \
  --volume "${output_dir}:/qualification" \
  "${COMPOSE_RUN_TTY_ARGS[@]}" \
  test \
  scripts/database_qualification.py \
  --database-url "${database_url}" \
  --source-sha "${DATABASE_QUALIFICATION_SOURCE_SHA}" \
  --output "/qualification/${output_name}"

test -s "${output}"
