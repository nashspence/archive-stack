#!/usr/bin/env bash
set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_compose_env.sh"

setup_test_compose_project
configure_compose_tty
ensure_compose_image test

access_key_id="$(compose_env_value ARC_S3_ACCESS_KEY_ID GK000000000000000000000001)"
secret_access_key="$(compose_env_value ARC_S3_SECRET_ACCESS_KEY 1111111111111111111111111111111111111111111111111111111111111111)"
bucket="$(compose_env_value ARC_S3_BUCKET riverhog)"
glacier_access_key_id="$(compose_env_value ARC_GLACIER_ACCESS_KEY_ID "${access_key_id}")"
glacier_secret_access_key="$(
  compose_env_value ARC_GLACIER_SECRET_ACCESS_KEY "${secret_access_key}"
)"
glacier_bucket="$(compose_env_value ARC_GLACIER_BUCKET "${bucket}")"

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
compose exec -T garage /garage -c /etc/garage.toml -h "${garage_node}" key import --yes -n "${access_key_id}" "${access_key_id}" "${secret_access_key}"
if [[ "${glacier_access_key_id}" != "${access_key_id}" || "${glacier_secret_access_key}" != "${secret_access_key}" ]]; then
  compose exec -T garage /garage -c /etc/garage.toml -h "${garage_node}" key import --yes -n "${glacier_access_key_id}" "${glacier_access_key_id}" "${glacier_secret_access_key}"
fi
compose exec -T garage /garage -c /etc/garage.toml -h "${garage_node}" bucket create "${bucket}"
if [[ "${glacier_bucket}" != "${bucket}" ]]; then
  compose exec -T garage /garage -c /etc/garage.toml -h "${garage_node}" bucket create "${glacier_bucket}"
fi
compose exec -T garage /garage -c /etc/garage.toml -h "${garage_node}" bucket allow --read --write --owner "${bucket}" --key "${access_key_id}"
if [[ "${glacier_access_key_id}" != "${access_key_id}" || "${glacier_bucket}" != "${bucket}" ]]; then
  compose exec -T garage /garage -c /etc/garage.toml -h "${garage_node}" bucket allow --read --write --owner "${glacier_bucket}" --key "${glacier_access_key_id}"
fi
compose run --rm --entrypoint python "${COMPOSE_RUN_TTY_ARGS[@]}" test tests/harness/configure_garage.py
