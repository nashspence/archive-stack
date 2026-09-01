#!/usr/bin/env bash
set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_compose_env.sh"

setup_test_compose_project
configure_compose_tty
export COMPOSE_PROFILES=development
ensure_compose_image test

archive_access_key_id="$(compose_env_value RIVERHOG_GARAGE_ARCHIVE_ACCESS_KEY_ID GK000000000000000000000002)"
archive_secret_access_key="$(
  compose_env_value RIVERHOG_GARAGE_ARCHIVE_SECRET_ACCESS_KEY 2222222222222222222222222222222222222222222222222222222222222222
)"
archive_bucket="$(compose_env_value RIVERHOG_GARAGE_ARCHIVE_BUCKET riverhog-archive)"
cache_access_key_id="$(compose_env_value RIVERHOG_GARAGE_CACHE_ACCESS_KEY_ID GK000000000000000000000002)"
cache_secret_access_key="$(compose_env_value RIVERHOG_GARAGE_CACHE_SECRET_ACCESS_KEY 2222222222222222222222222222222222222222222222222222222222222222)"
cache_bucket="$(compose_env_value RIVERHOG_GARAGE_CACHE_BUCKET riverhog-cache)"

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
if [[ "${cache_access_key_id}" != "${archive_access_key_id}" ]]; then
  compose exec -T garage /garage -c /etc/garage.toml -h "${garage_node}" key import --yes -n "${cache_access_key_id}" "${cache_access_key_id}" "${cache_secret_access_key}" >/dev/null
fi
compose exec -T garage /garage -c /etc/garage.toml -h "${garage_node}" bucket create "${archive_bucket}"
if [[ "${cache_bucket}" != "${archive_bucket}" ]]; then
  compose exec -T garage /garage -c /etc/garage.toml -h "${garage_node}" bucket create "${cache_bucket}"
fi
compose exec -T garage /garage -c /etc/garage.toml -h "${garage_node}" bucket allow --read --write --owner "${archive_bucket}" --key "${archive_access_key_id}"
if [[ "${cache_access_key_id}" != "${archive_access_key_id}" || "${cache_bucket}" != "${archive_bucket}" ]]; then
  compose exec -T garage /garage -c /etc/garage.toml -h "${garage_node}" bucket allow --read --write --owner "${cache_bucket}" --key "${cache_access_key_id}"
fi
