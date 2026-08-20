#!/usr/bin/env bash
set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_compose_env.sh"

setup_test_compose_project
configure_compose_tty
export COMPOSE_PROFILES=development
ensure_compose_image test

archive_store_names="$(compose_env_value RIVERHOG_ARCHIVE_STORES archive)"
archive_store_default="${archive_store_names%%,*}"
archive_store="$(compose_env_value RIVERHOG_ARCHIVE_WRITE_STORE "${archive_store_default}")"
archive_store_suffix="$(printf '%s' "${archive_store}" | tr '[:lower:]-' '[:upper:]_')"
archive_access_key_name="RIVERHOG_ARCHIVE_STORE_${archive_store_suffix}_ACCESS_KEY_ID"
archive_secret_key_name="RIVERHOG_ARCHIVE_STORE_${archive_store_suffix}_SECRET_ACCESS_KEY"
archive_bucket_name="RIVERHOG_ARCHIVE_STORE_${archive_store_suffix}_BUCKET"
archive_access_key_id="$(compose_env_value "${archive_access_key_name}" GK000000000000000000000002)"
archive_secret_access_key="$(
  compose_env_value "${archive_secret_key_name}" 2222222222222222222222222222222222222222222222222222222222222222
)"
archive_bucket="$(compose_env_value "${archive_bucket_name}" riverhog-archive)"
cache_access_key_id="$(compose_env_value RIVERHOG_RETRIEVAL_CACHE_ACCESS_KEY_ID)"
cache_secret_access_key="$(compose_env_value RIVERHOG_RETRIEVAL_CACHE_SECRET_ACCESS_KEY)"
cache_bucket="$(compose_env_value RIVERHOG_RETRIEVAL_CACHE_BUCKET)"
cache_configured=false
if [[ -n "${cache_access_key_id}" || -n "${cache_secret_access_key}" || -n "${cache_bucket}" ]]; then
  cache_configured=true
fi

compose up --detach garage

garage_node=""
for _ in $(seq 1 60); do
  garage_node="$(compose exec -T garage /garage -c /etc/garage.toml node id 2>/dev/null | tail -n 1 || true)"
  if [[ "${garage_node}" == *@* ]] && compose exec -T garage /garage -c /etc/garage.toml -h "${garage_node}" status >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

if [[ "${garage_node}" != *@* ]]; then
  printf 'garage bootstrap failed: could not resolve the running node id\n' >&2
  exit 1
fi

garage_node_id="${garage_node%@*}"
compose exec -T garage /garage -c /etc/garage.toml -h "${garage_node}" layout assign -z local -c 1GB "${garage_node_id}"
compose exec -T garage /garage -c /etc/garage.toml -h "${garage_node}" layout apply --version 1
compose exec -T garage /garage -c /etc/garage.toml -h "${garage_node}" key import --yes -n "${archive_access_key_id}" "${archive_access_key_id}" "${archive_secret_access_key}" >/dev/null
if [[ "${cache_configured}" == true && "${cache_access_key_id}" != "${archive_access_key_id}" ]]; then
  compose exec -T garage /garage -c /etc/garage.toml -h "${garage_node}" key import --yes -n "${cache_access_key_id}" "${cache_access_key_id}" "${cache_secret_access_key}" >/dev/null
fi
compose exec -T garage /garage -c /etc/garage.toml -h "${garage_node}" bucket create "${archive_bucket}"
if [[ "${cache_configured}" == true && "${cache_bucket}" != "${archive_bucket}" ]]; then
  compose exec -T garage /garage -c /etc/garage.toml -h "${garage_node}" bucket create "${cache_bucket}"
fi
compose exec -T garage /garage -c /etc/garage.toml -h "${garage_node}" bucket allow --read --write --owner "${archive_bucket}" --key "${archive_access_key_id}"
if [[ "${cache_configured}" == true && ( "${cache_access_key_id}" != "${archive_access_key_id}" || "${cache_bucket}" != "${archive_bucket}" ) ]]; then
  compose exec -T garage /garage -c /etc/garage.toml -h "${garage_node}" bucket allow --read --write --owner "${cache_bucket}" --key "${cache_access_key_id}"
fi
compose run --rm --entrypoint python "${COMPOSE_RUN_TTY_ARGS[@]}" test tests/harness/configure_garage.py
