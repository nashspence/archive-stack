#!/usr/bin/env bash
set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_compose_env.sh"

setup_test_compose_project
configure_compose_tty
export COMPOSE_PROFILES=development
export RIVERHOG_API_PORT="${RIVERHOG_API_PORT:-0}"
export RIVERHOG_ARCHIVE_STORE_ARCHIVE_BACKEND="${RIVERHOG_ARCHIVE_STORE_ARCHIVE_BACKEND:-aws}"
export RIVERHOG_ARCHIVE_STORE_ARCHIVE_READ_MODE="${RIVERHOG_ARCHIVE_STORE_ARCHIVE_READ_MODE:-restore_required}"
export RIVERHOG_RETRIEVAL_CACHE_ENDPOINT_URL="${RIVERHOG_RETRIEVAL_CACHE_ENDPOINT_URL:-http://garage:3900}"
export RIVERHOG_RETRIEVAL_CACHE_REGION="${RIVERHOG_RETRIEVAL_CACHE_REGION:-garage}"
export RIVERHOG_RETRIEVAL_CACHE_BUCKET="${RIVERHOG_RETRIEVAL_CACHE_BUCKET:-riverhog-cache}"
export RIVERHOG_RETRIEVAL_CACHE_ACCESS_KEY_ID="${RIVERHOG_RETRIEVAL_CACHE_ACCESS_KEY_ID:-GK000000000000000000000002}"
export RIVERHOG_RETRIEVAL_CACHE_SECRET_ACCESS_KEY="${RIVERHOG_RETRIEVAL_CACHE_SECRET_ACCESS_KEY:-2222222222222222222222222222222222222222222222222222222222222222}"
export RIVERHOG_RETRIEVAL_CACHE_FORCE_PATH_STYLE="${RIVERHOG_RETRIEVAL_CACHE_FORCE_PATH_STYLE:-true}"

smoke_root="$(mktemp -d "${TMPDIR:-/tmp}/riverhog-compose-smoke.XXXXXX")"
stove0_project="${COMPOSE_PROJECT_NAME}-stove0"
adapter_project="${COMPOSE_PROJECT_NAME}-adapters"
stove0_compose_file="${ROOT_DIR}/companions/stove0/compose.yaml"
adapter_compose_file="${ROOT_DIR}/riverhog/adapters/compose.yaml"

stove0_compose() {
  docker compose --project-name "${stove0_project}" --file "${stove0_compose_file}" "$@"
}

adapter_compose() {
  docker compose --project-name "${adapter_project}" --file "${adapter_compose_file}" "$@"
}

cleanup() {
  local status=$?
  if [[ "${status}" -ne 0 ]]; then
    adapter_compose ps >&2 || true
    adapter_compose logs --no-color --tail 200 >&2 || true
    stove0_compose ps >&2 || true
    stove0_compose logs --no-color --tail 200 >&2 || true
    compose ps >&2 || true
    compose logs --no-color --tail 200 >&2 || true
  fi
  adapter_compose down --volumes --remove-orphans || true
  stove0_compose down --volumes --remove-orphans || true
  compose down --volumes --remove-orphans
  if [[ -d "${smoke_root}" ]]; then
    docker run --rm \
      --volume "${smoke_root}:/cleanup" \
      alpine:3.22@sha256:14358309a308569c32bdc37e2e0e9694be33a9d99e68afb0f5ff33cc1f695dce \
      chown -R "$(id -u):$(id -g)" /cleanup || true
  fi
  rm -rf -- "${smoke_root}"
  return "${status}"
}
trap cleanup EXIT

"${ROOT_DIR}/scripts/bootstrap_garage.sh"
compose run --rm \
  --env RIVERHOG_GARAGE_ARCHIVE_INGRESS_TEST=1 \
  --entrypoint python \
  test -m pytest -q tests/integration/test_garage_encrypted_archive_store.py
ensure_compose_image app
compose up --detach --wait app
compose exec -T postgres createdb --username riverhog --owner riverhog stove0

bootstrap_token="$(compose_env_value RIVERHOG_BOOTSTRAP_TOKEN riverhog-development-bootstrap-token)"
create_code="import json, os, urllib.request
health = json.load(urllib.request.urlopen('http://127.0.0.1:8000/health/ready'))
assert health['status'] == 'ok'
openapi = json.load(urllib.request.urlopen('http://127.0.0.1:8000/openapi.json'))
assert '/v1/apps' in openapi['paths']
request = urllib.request.Request(
    'http://127.0.0.1:8000/v1/apps',
    headers={'Authorization': 'Bearer ' + os.environ['RIVERHOG_SMOKE_TOKEN']},
)
apps = json.load(urllib.request.urlopen(request))
assert apps['apps'] == []
request = urllib.request.Request(
    'http://127.0.0.1:8000/v1/apps/smoke/keys',
    method='POST',
    data=json.dumps({'access': [{'permission': '*', 'resource': '*'}]}).encode(),
    headers={
        'Authorization': 'Bearer ' + os.environ['RIVERHOG_SMOKE_TOKEN'],
        'Content-Type': 'application/json',
    },
)
created = json.load(urllib.request.urlopen(request))
assert created['app'] == 'smoke'
request = urllib.request.Request(
    'http://127.0.0.1:8000/v1/apps/smoke/keys/' + created['id'] + '/download-quota',
    method='PUT',
    data=json.dumps({'monthly_bytes': 16777216}).encode(),
    headers={
        'Authorization': 'Bearer ' + os.environ['RIVERHOG_SMOKE_TOKEN'],
        'Content-Type': 'application/json',
    },
)
quota = json.load(urllib.request.urlopen(request))
assert quota['monthly_bytes'] == 16777216
print(created['token'])"
smoke_token="$(
  compose exec -T --env "RIVERHOG_SMOKE_TOKEN=${bootstrap_token}" app python -c "${create_code}"
)"
test -n "${smoke_token}"

compose restart app
compose up --detach --wait app

restart_code="import json, os, urllib.request
health = json.load(urllib.request.urlopen('http://127.0.0.1:8000/health/ready'))
assert health['status'] == 'ok'
request = urllib.request.Request(
    'http://127.0.0.1:8000/v1/apps',
    headers={'Authorization': 'Bearer ' + os.environ['RIVERHOG_SMOKE_TOKEN']},
)
apps = json.load(urllib.request.urlopen(request))
assert [app['name'] for app in apps['apps']] == ['smoke']"
compose exec -T --env "RIVERHOG_SMOKE_TOKEN=${bootstrap_token}" app python -c "${restart_code}"

secret_root="${smoke_root}/secrets"
intake_root="${smoke_root}/intake"
install -d -m 0700 "${secret_root}" "${intake_root}"
umask 077
printf '%s\n' 'postgresql+psycopg://riverhog:riverhog@postgres:5432/stove0' > "${secret_root}/stove0-database-url"
printf '%s\n' 'stove0-compose-smoke-token' > "${secret_root}/stove0-api-token"
printf '%s\n' "${smoke_token}" > "${secret_root}/stove0-api-riverhog-token"
printf '%s\n' "${smoke_token}" > "${secret_root}/stove0-controller-riverhog-token"
printf '%s\n' "${smoke_token}" > "${secret_root}/stove0-worker-riverhog-token"
printf '%s\n' 'stove0-compose-observer-token' > "${secret_root}/stove0-observer-token"
printf '%s\n' 'stove0-compose-local-target-token' > "${secret_root}/stove0-local-target-token"
printf '%s\n' 'stove0-compose-nvenc-target-token' > "${secret_root}/stove0-nvenc-target-token"
printf '%s\n' "${smoke_token}" > "${secret_root}/adapter-riverhog-token"
printf '%s\n' 'riverhog-adapter-compose-smoke-token' > "${secret_root}/adapter-api-token"
printf '%s\n' 'riverhog-tus-compose-smoke-token' > "${secret_root}/tus-password"
chmod 0640 "${secret_root}"/*

adapter_config="${smoke_root}/adapters.json"
printf '%s\n' '{' \
  '  "host_id": "urn:uuid:00000000-0000-4000-8000-000000000522",' \
  '  "riverhog_base_url": "http://app:8000",' \
  '  "allow_insecure_http": true,' \
  '  "poll_seconds": 0.25,' \
  '  "sources": [' \
  '    {' \
  '      "id": "watched-smoke",' \
  '      "adapter": "watched-drop",' \
  '      "root": "/intake/watched",' \
  '      "ingest_source": "watched-drop:compose-smoke",' \
  '      "tags": ["stove0-preserve"],' \
  '      "close_mode": "explicit-flush",' \
  '      "stable_seconds": 1,' \
  '      "max_files": 8,' \
  '      "max_bytes": 1048576,' \
  '      "provenance": "capture"' \
  '    }' \
  '  ]' \
  '}' > "${adapter_config}"
chmod 0640 "${adapter_config}"

export RIVERHOG_CONTROL_NETWORK="${COMPOSE_PROJECT_NAME}_default"
export STOVE0_SECRET_FILE_GID="$(id -g)"
export STOVE0_API_PORT=0
export STOVE0_SCHEDULER_INTERVAL_SECONDS=0.25
export STOVE0_WORKSPACE_TMPFS_SIZE=256m
export STOVE0_DATABASE_URL_FILE="${secret_root}/stove0-database-url"
export STOVE0_API_TOKEN_FILE="${secret_root}/stove0-api-token"
export STOVE0_API_RIVERHOG_TOKEN_FILE="${secret_root}/stove0-api-riverhog-token"
export STOVE0_CONTROLLER_RIVERHOG_TOKEN_FILE="${secret_root}/stove0-controller-riverhog-token"
export STOVE0_WORKER_RIVERHOG_TOKEN_FILE="${secret_root}/stove0-worker-riverhog-token"
export STOVE0_OBSERVER_TOKEN_FILE="${secret_root}/stove0-observer-token"
export STOVE0_LOCAL_TARGET_TOKEN_FILE="${secret_root}/stove0-local-target-token"
export STOVE0_NVENC_TARGET_TOKEN_FILE="${secret_root}/stove0-nvenc-target-token"
export RIVERHOG_ADAPTERS_API_PORT=0
export RIVERHOG_ADAPTERS_SECRET_FILE_GID="$(id -g)"
export RIVERHOG_ADAPTERS_INTAKE_GID="$(id -g)"
export RIVERHOG_ADAPTERS_INTAKE_HOST_DIR="${intake_root}"
export RIVERHOG_ADAPTERS_CONFIG_HOST_PATH="${adapter_config}"
export RIVERHOG_ADAPTER_TOKEN_FILE="${secret_root}/adapter-riverhog-token"
export RIVERHOG_ADAPTERS_API_TOKEN_FILE="${secret_root}/adapter-api-token"
export RIVERHOG_TUS_PASSWORD_FILE="${secret_root}/tus-password"

client_environment=(
  --env RIVERHOG_BASE_URL=http://app:8000
  --env RIVERHOG_ALLOW_INSECURE_HTTP=true
  --env "RIVERHOG_TOKEN=${smoke_token}"
)
compose run --rm "${COMPOSE_RUN_TTY_ARGS[@]}" "${client_environment[@]}" \
  --entrypoint riverhog test tag create stove0-preserve --json >/dev/null
compose run --rm "${COMPOSE_RUN_TTY_ARGS[@]}" "${client_environment[@]}" \
  --entrypoint riverhog test tag create preserved --json >/dev/null

stove0_compose up --detach --build --wait state api controller worker media-sampling local-media
adapter_compose up --detach --build --wait intake-init adapter

adapter_run_code="from pathlib import Path
from riverhog_adapter_api_client import RiverhogAdapterClient
source = Path('/intake/watched/smoke.txt')
source.write_bytes(b'riverhog stove0 compose lifecycle\\n')
with RiverhogAdapterClient(
    base_url='http://127.0.0.1:8080',
    token='riverhog-adapter-compose-smoke-token',
    allow_insecure_http=True,
) as client:
    assert client.adapter_health_ready() == {'service': 'riverhog-adapters', 'status': 'ok'}
    result = client.flush_adapter_source('watched-smoke')
    assert result['completed'] == 1, result
    assert result['failed'] == [], result
    status = client.get_adapter_status()
    assert status['sources'][0]['claims'] == 0, status
assert not source.exists()
assert list(Path('/intake/watched/.riverhog-adapter/receipts').glob('*.json'))"
adapter_compose exec -T adapter python -c "${adapter_run_code}"

cache_code="from riverhog_api_client import ApiClient
with ApiClient() as client:
    collections = client.list_collections(tag='stove0-preserve', all_items=True)['collections']
    assert len(collections) == 1, collections
    input_id = collections[0]['id']
    cached = client.list_retrieval_cache_objects(collection_id=input_id, all_items=True)
    assert cached['objects'], cached
    assert all(row['state'] == 'ready' for row in cached['objects']), cached
    assert all('new_archive' in row['lease_categories'] for row in cached['objects']), cached"
compose run --rm "${COMPOSE_RUN_TTY_ARGS[@]}" "${client_environment[@]}" \
  --entrypoint python test -c "${cache_code}"

wait_code="import json, time, urllib.request
deadline = time.monotonic() + 180
last = None
while time.monotonic() < deadline:
    request = urllib.request.Request(
        'http://127.0.0.1:8080/v1/work?all=true&sort=updated_at&order=asc',
        headers={'Authorization': 'Bearer stove0-compose-smoke-token'},
    )
    payload = json.load(urllib.request.urlopen(request, timeout=5))
    rows = payload['work']
    if rows:
        last = rows[0]
        if last['phase'] == 'complete':
            assert last['output']['collection_id'] > 0
            break
        if last['phase'] in {'failed', 'canceled', 'inapplicable'}:
            raise RuntimeError(json.dumps(last, sort_keys=True))
    time.sleep(0.5)
else:
    raise TimeoutError(json.dumps(last, sort_keys=True) if last else 'no stove0 work appeared')"
stove0_compose exec -T api python -c "${wait_code}"

lineage_code="from riverhog_api_client import ApiClient
with ApiClient() as client:
    inputs = client.list_collections(tag='stove0-preserve', all_items=True)['collections']
    outputs = client.list_collections(tag='preserved', all_items=True)['collections']
    assert len(inputs) == 1 and len(outputs) == 1, (inputs, outputs)
    derivation = client.get_collection_derivation(outputs[0]['id'])
    assert derivation['derivation']['format'] == 'riverhog-collection-derivation/v1'
    assert [row['collection_id'] for row in derivation['derivation']['inputs']] == [inputs[0]['id']]"
compose run --rm "${COMPOSE_RUN_TTY_ARGS[@]}" "${client_environment[@]}" \
  --entrypoint python test -c "${lineage_code}"

stove0_compose restart api controller worker local-media
stove0_compose up --detach --wait api controller worker local-media
stove0_compose exec -T api python -c "${wait_code}"
