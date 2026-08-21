#!/usr/bin/env bash
set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_compose_env.sh"

setup_test_compose_project
configure_compose_tty
export COMPOSE_PROFILES=development
export RIVERHOG_API_PORT="${RIVERHOG_API_PORT:-0}"

smoke_root="$(mktemp -d "${TMPDIR:-/tmp}/riverhog-compose-smoke.XXXXXX")"
stove0_project="${COMPOSE_PROJECT_NAME}-stove0"
adapter_project="${COMPOSE_PROJECT_NAME}-ftp-adapter"
stove0_compose_file="${ROOT_DIR}/companions/stove0/compose.yaml"
adapter_compose_file="${ROOT_DIR}/riverhog/ftp-adapter/compose.yaml"

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
compose run --rm --entrypoint riverhog-storage-adapter-conformance test \
  http://garage-storage-adapter:8080 \
  --token-file /run/secrets/riverhog-storage-adapter-token \
  --allow-insecure-http \
  --object-prefix compose-conformance >/dev/null
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
printf '%s\n' 'stove0-compose-ffprobe-observer-token' > "${secret_root}/stove0-ffprobe-sampling-observer-token"
printf '%s\n' 'stove0-compose-nvenc-target-token' > "${secret_root}/stove0-nvenc-av1-opus-target-token"
printf '%s\n' 'stove0-compose-nvenc-sampler-token' > "${secret_root}/stove0-nvenc-av1-opus-sampler-token"
printf '%s\n' 'stove0-compose-opus-target-token' > "${secret_root}/stove0-opus-target-token"
printf '%s\n' 'stove0-compose-opus-sampler-token' > "${secret_root}/stove0-opus-sampler-token"
printf '%s\n' 'stove0-compose-review-target-token' > "${secret_root}/stove0-review-target-token"
printf '%s\n' "${smoke_token}" > "${secret_root}/adapter-riverhog-token"
printf '%s\n' 'riverhog-ftp-adapter-compose-smoke-token' > "${secret_root}/ftp-adapter-api-token"
printf '%s\n' 'riverhog-ftp-adapter-compose-smoke-password' > "${secret_root}/ftp-adapter-password"
chmod 0640 "${secret_root}"/*

adapter_config="${smoke_root}/ftp-adapter.json"
printf '%s\n' '{' \
  '  "host_id": "urn:uuid:00000000-0000-4000-8000-000000000522",' \
  '  "riverhog_base_url": "http://app:8000",' \
  '  "allow_insecure_http": true,' \
  '  "poll_seconds": 0.25,' \
  '  "sources": [' \
  '    {' \
  '      "id": "ftp-smoke",' \
  '      "root": "/intake/ftp",' \
  '      "ingest_source": "ftp:compose-smoke",' \
  '      "tags": ["stove0-audio-archive"],' \
  '      "close_mode": "explicit-flush",' \
  '      "stable_seconds": 1,' \
  '      "max_files": 8,' \
  '      "max_bytes": 1048576,' \
  '      "provenance": "omit",' \
  '      "provenance_omission_reason": "The FTP producer cannot observe the source host filesystem."' \
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
export STOVE0_FFPROBE_SAMPLING_OBSERVER_TOKEN_FILE="${secret_root}/stove0-ffprobe-sampling-observer-token"
export STOVE0_NVENC_AV1_OPUS_TARGET_TOKEN_FILE="${secret_root}/stove0-nvenc-av1-opus-target-token"
export STOVE0_NVENC_AV1_OPUS_SAMPLER_TOKEN_FILE="${secret_root}/stove0-nvenc-av1-opus-sampler-token"
export STOVE0_OPUS_TARGET_TOKEN_FILE="${secret_root}/stove0-opus-target-token"
export STOVE0_OPUS_SAMPLER_TOKEN_FILE="${secret_root}/stove0-opus-sampler-token"
export STOVE0_REVIEW_TARGET_TOKEN_FILE="${secret_root}/stove0-review-target-token"
export STOVE0_FFPROBE_IMAGE_DIGEST="$(printf '1%.0s' {1..64})"
export STOVE0_NVENC_AV1_OPUS_IMAGE_DIGEST="$(printf '2%.0s' {1..64})"
export STOVE0_OPUS_IMAGE_DIGEST="$(printf '3%.0s' {1..64})"
export STOVE0_REVIEW_IMAGE_DIGEST="$(printf '4%.0s' {1..64})"
export STOVE0_OPUS_SAMPLER_DESCRIPTOR_SHA256="$(printf '5%.0s' {1..64})"
export RIVERHOG_FTP_ADAPTER_API_PORT=0
export RIVERHOG_FTP_ADAPTER_PORT=0
export RIVERHOG_FTP_ADAPTER_PUBLIC_HOST=ftp-daemon
export RIVERHOG_FTP_ADAPTER_SECRET_FILE_GID="$(id -g)"
export RIVERHOG_FTP_ADAPTER_INTAKE_GID="$(id -g)"
export RIVERHOG_FTP_ADAPTER_INTAKE_HOST_DIR="${intake_root}"
export RIVERHOG_FTP_ADAPTER_CONFIG_HOST_PATH="${adapter_config}"
export RIVERHOG_FTP_ADAPTER_RIVERHOG_TOKEN_FILE="${secret_root}/adapter-riverhog-token"
export RIVERHOG_FTP_ADAPTER_API_TOKEN_FILE="${secret_root}/ftp-adapter-api-token"
export RIVERHOG_FTP_ADAPTER_PASSWORD_FILE="${secret_root}/ftp-adapter-password"

client_environment=(
  --env RIVERHOG_BASE_URL=http://app:8000
  --env RIVERHOG_ALLOW_INSECURE_HTTP=true
  --env "RIVERHOG_TOKEN=${smoke_token}"
)
compose run --rm "${COMPOSE_RUN_TTY_ARGS[@]}" "${client_environment[@]}" \
  --entrypoint riverhog test tag create stove0-audio-archive --json >/dev/null
compose run --rm "${COMPOSE_RUN_TTY_ARGS[@]}" "${client_environment[@]}" \
  --entrypoint riverhog test tag create archive-audio --json >/dev/null

stove0_compose up --detach --build --wait \
  state api controller worker ffprobe-sampling-observer opus-target
adapter_compose up --detach --build --wait intake-init ftp-adapter ftp-daemon

adapter_run_code="from ftplib import FTP, all_errors
from io import BytesIO
from pathlib import Path
import time
import wave
from riverhog_ftp_adapter_api_client import RiverhogFtpAdapterClient
source = Path('/intake/ftp/smoke.wav')
payload = BytesIO()
with wave.open(payload, 'wb') as audio:
    audio.setnchannels(1)
    audio.setsampwidth(2)
    audio.setframerate(8000)
    audio.writeframes(b'\\x00\\x00' * 2000)
expected = payload.getvalue()
deadline = time.monotonic() + 30
last_error = None
while time.monotonic() < deadline:
    try:
        with FTP('ftp-daemon', timeout=10) as ftp:
            ftp.login('ftp-intake', 'riverhog-ftp-adapter-compose-smoke-password')
            ftp.storbinary('STOR smoke.wav', BytesIO(expected))
        break
    except all_errors as error:
        last_error = error
        time.sleep(0.25)
else:
    raise RuntimeError('FTP listener did not become ready') from last_error
assert source.read_bytes() == expected
with RiverhogFtpAdapterClient(
    base_url='http://127.0.0.1:8080',
    token='riverhog-ftp-adapter-compose-smoke-token',
    allow_insecure_http=True,
) as client:
    assert client.ftp_adapter_health_ready() == {'service': 'riverhog-ftp-adapter', 'status': 'ok'}
    result = client.flush_ftp_adapter_source('ftp-smoke')
    assert result['completed'] == 1, result
    assert result['failed'] == [], result
    status = client.get_ftp_adapter_status()
    assert status['sources'][0]['claims'] == 0, status
assert not source.exists()
assert list(Path('/intake/ftp/.riverhog-ftp-adapter/receipts').glob('*.json'))"
adapter_compose exec -T ftp-adapter python -c "${adapter_run_code}"

cache_code="from riverhog_api_client import ApiClient
with ApiClient() as client:
    collections = client.list_collections(tag='stove0-audio-archive', all_items=True)['collections']
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
    request = urllib.request.Request(
        'http://127.0.0.1:8080/v1/admin/scheduler/run',
        data=json.dumps({'role': 'controller', 'work_limit': 25}).encode(),
        headers={
            'Authorization': 'Bearer stove0-compose-smoke-token',
            'Content-Type': 'application/json',
        },
        method='POST',
    )
    diagnostic = json.load(urllib.request.urlopen(request, timeout=30))
    raise TimeoutError(json.dumps({'work': last, 'scheduler': diagnostic}, sort_keys=True))"
stove0_compose exec -T api python -c "${wait_code}"

lineage_code="from riverhog_api_client import ApiClient
with ApiClient() as client:
    inputs = client.list_collections(tag='stove0-audio-archive', all_items=True)['collections']
    outputs = client.list_collections(tag='archive-audio', all_items=True)['collections']
    assert len(inputs) == 1 and len(outputs) == 1, (inputs, outputs)
    derivation = client.get_collection_derivation(outputs[0]['id'])
    assert derivation['derivation']['format'] == 'riverhog-collection-derivation/v1'
    assert [row['collection_id'] for row in derivation['derivation']['inputs']] == [inputs[0]['id']]"
compose run --rm "${COMPOSE_RUN_TTY_ARGS[@]}" "${client_environment[@]}" \
  --entrypoint python test -c "${lineage_code}"

stove0_compose restart api controller worker ffprobe-sampling-observer opus-target
stove0_compose up --detach --wait api controller worker ffprobe-sampling-observer opus-target
stove0_compose exec -T api python -c "${wait_code}"
