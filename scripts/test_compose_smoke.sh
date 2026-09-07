#!/usr/bin/env bash
set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_compose_env.sh"

setup_test_compose_project
configure_compose_tty
export COMPOSE_PROFILES=development
export SOURCE_REVISION="${SOURCE_REVISION:-$(git -C "${ROOT_DIR}" rev-parse HEAD)}"
export RIVERHOG_API_PORT="${RIVERHOG_API_PORT:-0}"
export RIVERHOG_RETRIEVAL_CACHE_STORES="${RIVERHOG_RETRIEVAL_CACHE_STORES:-local,elastic}"
export RIVERHOG_RETRIEVAL_CACHE_LOCAL_ADAPTER_URL="${RIVERHOG_RETRIEVAL_CACHE_LOCAL_ADAPTER_URL:-http://filesystem-cache-adapter:8080}"
export RIVERHOG_RETRIEVAL_CACHE_LOCAL_ADAPTER_TOKEN_FILE="${RIVERHOG_RETRIEVAL_CACHE_LOCAL_ADAPTER_TOKEN_FILE:-/run/secrets/riverhog-storage-adapter.token}"
export RIVERHOG_RETRIEVAL_CACHE_LOCAL_ADAPTER_ALLOW_INSECURE_HTTP="${RIVERHOG_RETRIEVAL_CACHE_LOCAL_ADAPTER_ALLOW_INSECURE_HTTP:-true}"
export RIVERHOG_RETRIEVAL_CACHE_LOCAL_ADMISSION_BUDGET_BYTES="${RIVERHOG_RETRIEVAL_CACHE_LOCAL_ADMISSION_BUDGET_BYTES:-1MiB}"
export RIVERHOG_RETRIEVAL_CACHE_ELASTIC_ADAPTER_URL="${RIVERHOG_RETRIEVAL_CACHE_ELASTIC_ADAPTER_URL:-http://elastic-cache-adapter:8080}"
export RIVERHOG_RETRIEVAL_CACHE_ELASTIC_ADAPTER_TOKEN_FILE="${RIVERHOG_RETRIEVAL_CACHE_ELASTIC_ADAPTER_TOKEN_FILE:-/run/secrets/riverhog-storage-adapter.token}"
export RIVERHOG_RETRIEVAL_CACHE_ELASTIC_ADAPTER_ALLOW_INSECURE_HTTP="${RIVERHOG_RETRIEVAL_CACHE_ELASTIC_ADAPTER_ALLOW_INSECURE_HTTP:-true}"

smoke_root="$(mktemp -d "${TMPDIR:-/tmp}/riverhog-compose-smoke.XXXXXX")"
smoke_file_count="${STOVE0_SMOKE_FILE_COUNT:-16}"
smoke_audio_frames="${STOVE0_SMOKE_AUDIO_FRAMES:-2000}"
if ! [[ "${smoke_file_count}" =~ ^[0-9]+$ ]] ||
  (( smoke_file_count < 1 || smoke_file_count > 1000 )); then
  printf '%s\n' 'STOVE0_SMOKE_FILE_COUNT must be between 1 and 1000.' >&2
  exit 2
fi
if ! [[ "${smoke_audio_frames}" =~ ^[0-9]+$ ]] ||
  (( smoke_audio_frames < 1 || smoke_audio_frames > 10000000 )); then
  printf '%s\n' 'STOVE0_SMOKE_AUDIO_FRAMES must be between 1 and 10000000.' >&2
  exit 2
fi
smoke_claim_file_count=$((smoke_file_count + 1))
smoke_max_bytes=$((smoke_file_count * (smoke_audio_frames * 2 + 4096) + 16384))
# Three independent readers exercise each input in this lifecycle. Account for
# age-unit amplification as well as logical payload so quota policy remains
# enabled without becoming the scale qualification's limiting resource.
smoke_download_quota_bytes=$((8 * (smoke_max_bytes + smoke_claim_file_count * 65552)))
if (( smoke_download_quota_bytes < 16777216 )); then
  smoke_download_quota_bytes=16777216
fi
stove0_project="${COMPOSE_PROJECT_NAME}-stove0"
adapter_project="${COMPOSE_PROJECT_NAME}-ftp-adapter"
stove0_compose_file="${ROOT_DIR}/companions/stove0/compose.yaml"
adapter_compose_file="${ROOT_DIR}/reference/riverhog/ingress/ftp/compose.yaml"
export STOVE0_RECIPES_HOST_PATH="${ROOT_DIR}/qualification/fixtures/stove0/recipes.yaml"

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
compose up --detach --wait archive-adapter filesystem-cache-adapter elastic-cache-adapter
compose run --rm "${COMPOSE_RUN_TTY_ARGS[@]}" \
  --entrypoint riverhog-storage-adapter-conformance \
  test \
  --base-url http://filesystem-cache-adapter:8080 \
  --token-file /run/secrets/riverhog-storage-adapter.token \
  --object-prefix conformance/compose-filesystem \
  --allow-insecure-http
compose run --rm "${COMPOSE_RUN_TTY_ARGS[@]}" \
  --entrypoint python \
  test -m tests.harness.storage_adapter_goodput_probe \
  --base-url http://filesystem-cache-adapter:8080 \
  --token-file /run/secrets/riverhog-storage-adapter.token
continuation_root="${smoke_root}/storage-adapter-continuation"
install -d -m 0700 "${continuation_root}"
compose run --rm "${COMPOSE_RUN_TTY_ARGS[@]}" \
  --volume "${continuation_root}:/continuation" \
  --entrypoint python \
  test -m tests.harness.storage_adapter_restart_probe prepare /continuation/state.json
compose restart archive-adapter
compose up --detach --wait archive-adapter
compose run --rm "${COMPOSE_RUN_TTY_ARGS[@]}" \
  --volume "${continuation_root}:/continuation" \
  --entrypoint python \
  test -m tests.harness.storage_adapter_restart_probe resume /continuation/state.json
filesystem_continuation_root="${smoke_root}/filesystem-adapter-continuation"
install -d -m 0700 "${filesystem_continuation_root}"
compose run --rm "${COMPOSE_RUN_TTY_ARGS[@]}" \
  --env RIVERHOG_STORAGE_ADAPTER_RESTART_PROBE_CACHE_STORE=local \
  --volume "${filesystem_continuation_root}:/continuation" \
  --entrypoint python \
  test -m tests.harness.storage_adapter_restart_probe prepare /continuation/state.json
compose restart filesystem-cache-adapter
compose up --detach --wait filesystem-cache-adapter
compose run --rm "${COMPOSE_RUN_TTY_ARGS[@]}" \
  --env RIVERHOG_STORAGE_ADAPTER_RESTART_PROBE_CACHE_STORE=local \
  --volume "${filesystem_continuation_root}:/continuation" \
  --entrypoint python \
  test -m tests.harness.storage_adapter_restart_probe resume /continuation/state.json
compose run --rm \
  --env RIVERHOG_GARAGE_ARCHIVE_INGRESS_TEST=1 \
  --entrypoint python \
  test -m pytest -q tests/integration/test_garage_encrypted_archive_store.py
ensure_compose_image app
compose up --detach --wait app
compose exec -T app sh -c \
  'test "$(id -u)" = 65532 && test "$(id -g)" = 65532 && test -w /tmp && test ! -w /usr/share/doc/riverhog'
compose exec -T postgres createdb --username riverhog --owner riverhog stove0
compose exec -T postgres psql --username riverhog --dbname stove0 \
  --command 'CREATE EXTENSION pg_trgm WITH SCHEMA public;'

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
    data=json.dumps({'monthly_bytes': int(os.environ['RIVERHOG_SMOKE_DOWNLOAD_QUOTA_BYTES'])}).encode(),
    headers={
        'Authorization': 'Bearer ' + os.environ['RIVERHOG_SMOKE_TOKEN'],
        'Content-Type': 'application/json',
    },
)
quota = json.load(urllib.request.urlopen(request))
assert quota['monthly_bytes'] == int(os.environ['RIVERHOG_SMOKE_DOWNLOAD_QUOTA_BYTES'])
print(created['token'])"
smoke_token="$(
  compose exec -T \
    --env "RIVERHOG_SMOKE_TOKEN=${bootstrap_token}" \
    --env "RIVERHOG_SMOKE_DOWNLOAD_QUOTA_BYTES=${smoke_download_quota_bytes}" \
    app python -c "${create_code}"
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
printf '%s\n' 'stove0-compose-target-callback-signing-key' > "${secret_root}/stove0-target-callback-signing-key"
printf '%s\n' 'stove0-compose-browse-token-signing-key-v1' > "${secret_root}/stove0-browse-token-signing-key"
printf '%s\n' "${smoke_token}" > "${secret_root}/stove0-api-riverhog-token"
printf '%s\n' "${smoke_token}" > "${secret_root}/stove0-controller-riverhog-token"
printf '%s\n' "${smoke_token}" > "${secret_root}/stove0-worker-riverhog-token"
printf '%s\n' 'stove0-compose-ffprobe-observer-token' > "${secret_root}/stove0-ffprobe-sampling-observer-token"
printf '%s\n' 'stove0-compose-exiftool-observer-token' > "${secret_root}/stove0-exiftool-observer-token"
printf '%s\n' 'stove0-compose-nvenc-target-token' > "${secret_root}/stove0-nvenc-av1-opus-target-token"
printf '%s\n' 'stove0-compose-nvenc-sampler-token' > "${secret_root}/stove0-nvenc-av1-opus-review-sampler-token"
printf '%s\n' 'stove0-compose-opus-target-token' > "${secret_root}/stove0-opus-target-token"
printf '%s\n' 'stove0-compose-opus-review-sampler-token' > "${secret_root}/stove0-opus-review-sampler-token"
printf '%s\n' 'stove0-compose-review-materialize-target-token' > "${secret_root}/stove0-review-materialize-target-token"
printf '%s\n' 'stove0-compose-review-rclone-effect-target-token' > "${secret_root}/stove0-review-rclone-effect-target-token"
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
  "      \"max_files\": ${smoke_claim_file_count}," \
  "      \"max_bytes\": ${smoke_max_bytes}," \
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
export STOVE0_OBSERVER_TMPFS_SIZE=256m
export STOVE0_TARGET_TMPFS_SIZE=256m
export STOVE0_REVIEW_TMPFS_SIZE=256m
export STOVE0_DATABASE_URL_FILE="${secret_root}/stove0-database-url"
export STOVE0_API_TOKEN_FILE="${secret_root}/stove0-api-token"
export STOVE0_TARGET_CALLBACK_SIGNING_KEY_FILE="${secret_root}/stove0-target-callback-signing-key"
export STOVE0_BROWSE_TOKEN_SIGNING_KEY_FILE="${secret_root}/stove0-browse-token-signing-key"
export STOVE0_API_RIVERHOG_TOKEN_FILE="${secret_root}/stove0-api-riverhog-token"
export STOVE0_CONTROLLER_RIVERHOG_TOKEN_FILE="${secret_root}/stove0-controller-riverhog-token"
export STOVE0_WORKER_RIVERHOG_TOKEN_FILE="${secret_root}/stove0-worker-riverhog-token"
export STOVE0_FFPROBE_SAMPLING_OBSERVER_TOKEN_FILE="${secret_root}/stove0-ffprobe-sampling-observer-token"
export STOVE0_EXIFTOOL_OBSERVER_TOKEN_FILE="${secret_root}/stove0-exiftool-observer-token"
export STOVE0_NVENC_AV1_OPUS_TARGET_TOKEN_FILE="${secret_root}/stove0-nvenc-av1-opus-target-token"
export STOVE0_NVENC_AV1_OPUS_REVIEW_SAMPLER_TOKEN_FILE="${secret_root}/stove0-nvenc-av1-opus-review-sampler-token"
export STOVE0_OPUS_TARGET_TOKEN_FILE="${secret_root}/stove0-opus-target-token"
export STOVE0_OPUS_REVIEW_SAMPLER_TOKEN_FILE="${secret_root}/stove0-opus-review-sampler-token"
export STOVE0_REVIEW_MATERIALIZE_TARGET_TOKEN_FILE="${secret_root}/stove0-review-materialize-target-token"
export STOVE0_REVIEW_RCLONE_EFFECT_TARGET_TOKEN_FILE="${secret_root}/stove0-review-rclone-effect-target-token"
export STOVE0_FFPROBE_IMAGE_DIGEST="$(printf '1%.0s' {1..64})"
export STOVE0_EXIFTOOL_IMAGE_DIGEST="$(printf '6%.0s' {1..64})"
export STOVE0_NVENC_AV1_OPUS_IMAGE_DIGEST="$(printf '2%.0s' {1..64})"
export STOVE0_OPUS_IMAGE_DIGEST="$(printf '3%.0s' {1..64})"
export STOVE0_REVIEW_MATERIALIZE_IMAGE_DIGEST="$(printf '4%.0s' {1..64})"
export STOVE0_REVIEW_RCLONE_EFFECT_IMAGE_DIGEST="$(printf '7%.0s' {1..64})"
# Compose interpolates the complete model before it selects services.  The
# review targets are created only after this bootstrap value is replaced with
# the running sampler's exact descriptor identity below.
export STOVE0_OPUS_REVIEW_SAMPLER_DESCRIPTOR_SHA256="$(printf '5%.0s' {1..64})"
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

stove0_compose up --detach --build --wait \
  state api controller worker ffprobe-sampling-observer exiftool-observer opus-target \
  opus-review-sampler
sampler_descriptor_code="import json, urllib.request
request = urllib.request.Request(
    'http://127.0.0.1:8080/v1/sampler',
    headers={'Authorization': 'Bearer stove0-compose-opus-review-sampler-token'},
)
print(json.load(urllib.request.urlopen(request))['descriptor_sha256'])"
export STOVE0_OPUS_REVIEW_SAMPLER_DESCRIPTOR_SHA256="$(
  stove0_compose exec -T opus-review-sampler python -c "${sampler_descriptor_code}"
)"
stove0_compose up --detach --build --wait review-materialize-target review-rclone-effect-target
stove0_compose exec -T review-materialize-target python -c "import json, urllib.request; request = urllib.request.Request('http://127.0.0.1:8080/v1/target', headers={'Authorization': 'Bearer stove0-compose-review-materialize-target-token'}); assert json.load(urllib.request.urlopen(request))['protocol'] == 'stove0-transform-target/v1'"
stove0_compose exec -T review-rclone-effect-target python -c "import json, urllib.request; request = urllib.request.Request('http://127.0.0.1:8080/v1/target', headers={'Authorization': 'Bearer stove0-compose-review-rclone-effect-target-token'}); assert json.load(urllib.request.urlopen(request))['protocol'] == 'stove0-effect-target/v1'"
stove0_compose exec -T review-rclone-effect-target python -c "from pathlib import Path; import subprocess; source = Path('/tmp/rclone-probe'); source.write_bytes(b'riverhog-review-effect-probe'); destination = Path('/var/lib/stove0-review-delivery/qualification/probe'); subprocess.run(['rclone', 'copyto', str(source), str(destination)], check=True); assert destination.read_bytes() == source.read_bytes(); source.unlink(); destination.unlink()"
adapter_compose up --detach --build --wait intake-init ftp-adapter ftp-daemon

adapter_run_code="from ftplib import FTP, all_errors
from io import BytesIO
import json
import os
from pathlib import Path
import time
import wave
from riverhog_ftp_adapter_api_client import RiverhogFtpAdapterClient
payload = BytesIO()
with wave.open(payload, 'wb') as audio:
    audio.setnchannels(1)
    audio.setsampwidth(2)
    audio.setframerate(8000)
    audio.writeframes(b'\\x00\\x00' * int(os.environ['STOVE0_SMOKE_AUDIO_FRAMES']))
expected = payload.getvalue()
sources = [
    Path(f'/intake/ftp/smoke-{index:04}.wav')
    for index in range(int(os.environ['STOVE0_SMOKE_FILE_COUNT']))
]
sidecar = Path('/intake/ftp/smoke-0000.xmp')
sidecar_payload = b'''<x:xmpmeta xmlns:x="adobe:ns:meta/">
 <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
  <rdf:Description xmlns:xmp="http://ns.adobe.com/xap/1.0/"
                   xmp:CreateDate="2026-08-23T00:00:00Z"/>
 </rdf:RDF>
</x:xmpmeta>
'''
uploads = [(source, expected) for source in sources] + [(sidecar, sidecar_payload)]
deadline = time.monotonic() + 30
last_error = None
while time.monotonic() < deadline:
    try:
        with FTP('ftp-daemon', timeout=10) as ftp:
            ftp.login('ftp-intake', 'riverhog-ftp-adapter-compose-smoke-password')
            for source, content in uploads:
                ftp.storbinary('STOR ' + source.name, BytesIO(content))
        break
    except all_errors as error:
        last_error = error
        time.sleep(0.25)
else:
    raise RuntimeError('FTP listener did not become ready') from last_error
assert all(source.read_bytes() == content for source, content in uploads)
with RiverhogFtpAdapterClient(
    base_url='http://127.0.0.1:8080',
    token='riverhog-ftp-adapter-compose-smoke-token',
    allow_insecure_http=True,
) as client:
    health = client.ftp_adapter_health_ready()
    assert health.service == 'riverhog-ftp-adapter'
    assert health.status == 'ok'
    result = client.flush_ftp_adapter_source('ftp-smoke')
    assert result['completed'] == 1, result
    assert result['failed'] == [], result
    status = client.get_ftp_adapter_status()
    assert status['sources'][0]['claims'] == 0, status
assert all(not source.exists() for source, _content in uploads)
receipts = sorted(Path('/intake/ftp/.riverhog-ftp-adapter/receipts').glob('*.json'))
assert len(receipts) == 1, receipts
receipt = json.loads(receipts[0].read_text(encoding='utf-8'))
print(json.dumps({
    key: receipt[key]
    for key in ('collection_id', 'archive_root_sha256', 'content_identity')
}, sort_keys=True))"
scale_started_ns="$(date +%s%N)"
input_receipt_json="$(adapter_compose exec -T \
  --env RIVERHOG_SMOKE_RECEIPT_OUTPUT=1 \
  --env "STOVE0_SMOKE_FILE_COUNT=${smoke_file_count}" \
  --env "STOVE0_SMOKE_AUDIO_FRAMES=${smoke_audio_frames}" \
  ftp-adapter python -c "${adapter_run_code}")"
test -n "${input_receipt_json}"
input_collection_id="$(printf '%s' "${input_receipt_json}" | jq -r '.collection_id')"

invoke_code="import json, os, urllib.request
from stove0_operator_contracts import WorkCreateIn, WorkflowPreviewIn
from stove0_protocol import CollectionRootRef
receipt = json.loads(os.environ['RIVERHOG_INPUT_RECEIPT'])
root = CollectionRootRef.model_validate(receipt)
def post(path, payload):
    request = urllib.request.Request(
        'http://127.0.0.1:8080' + path,
        data=payload.model_dump_json(exclude_none=True).encode(),
        headers={
            'Authorization': 'Bearer stove0-compose-smoke-token',
            'Content-Type': 'application/json',
        },
        method='POST',
    )
    return json.load(urllib.request.urlopen(request, timeout=30))
preview = post(
    '/v1/workflow-previews',
    WorkflowPreviewIn(recipe_id='stove0.audio-archive/v1', inputs=(root,)),
)
assert preview['state'] == 'ready', preview
work = post(
    '/v1/work',
    WorkCreateIn(
        recipe_id='stove0.audio-archive/v1',
        inputs=(root,),
        preview_sha256=preview['preview_sha256'],
    ),
)
print(work['work_id'])"
stove0_work_id="$(stove0_compose exec -T \
  --env RIVERHOG_SMOKE_INVOCATION_OUTPUT=1 \
  --env "RIVERHOG_INPUT_RECEIPT=${input_receipt_json}" \
  api python -c "${invoke_code}")"
test -n "${stove0_work_id}"

cache_code="import os
from riverhog_api_client import ApiClient
def collect(method, key, **kwargs):
    page_token = None
    rows = []
    while True:
        payload = method(page_size=100, page_token=page_token, **kwargs)
        rows.extend(payload[key])
        page_token = payload.get('next_page_token')
        if page_token is None:
            return rows
with ApiClient() as client:
    input_id = int(os.environ['INPUT_COLLECTION_ID'])
    collection = client.get_collection(input_id)
    assert collection['id'] == input_id, collection
    cached = collect(client.list_retrieval_cache_objects, 'objects', collection_id=input_id)
    assert cached, cached
    assert all(row['state'] == 'ready' for row in cached), cached
    assert all('new_archive' in row['lease_categories'] for row in cached), cached
    assert {row['cache_store'] for row in cached} == {'local'}, cached"
compose run --rm "${COMPOSE_RUN_TTY_ARGS[@]}" "${client_environment[@]}" \
  --env "INPUT_COLLECTION_ID=${input_collection_id}" \
  --entrypoint python test -c "${cache_code}"

overflow_root="${smoke_root}/overflow"
install -d -m 0700 "${overflow_root}"
truncate -s 2MiB "${overflow_root}/larger-than-local-budget.bin"
overflow_result="$(
  compose run --rm "${COMPOSE_RUN_TTY_ARGS[@]}" "${client_environment[@]}" \
    --volume "${overflow_root}:/overflow:ro" \
    --entrypoint riverhog test collection upload start /overflow \
    --omit-provenance 'compose qualification fixture' --json
)"
overflow_collection_id="$(printf '%s' "${overflow_result}" | jq -r '.collection_id')"
overflow_cache_code="import os, time
from riverhog_api_client import ApiClient
def collect(method, key, **kwargs):
    page_token = None
    rows = []
    while True:
        payload = method(page_size=100, page_token=page_token, **kwargs)
        rows.extend(payload[key])
        page_token = payload.get('next_page_token')
        if page_token is None:
            return rows
with ApiClient() as client:
    deadline = time.monotonic() + 60
    while True:
        rows = collect(client.list_retrieval_cache_objects, 'objects')
        overflow = [
            row for row in rows
            if row['collection_id'] == int(os.environ['OVERFLOW_COLLECTION_ID'])
        ]
        stores = {row['cache_store'] for row in rows if row['state'] == 'ready'}
        if overflow and all(row['state'] == 'ready' for row in overflow) and stores == {'local', 'elastic'}:
            break
        if time.monotonic() >= deadline:
            raise AssertionError((stores, overflow))
        time.sleep(0.25)
    assert 'elastic' in {row['cache_store'] for row in overflow}, overflow
    status = client.retrieval_cache_status()
    assert [store['cache_store'] for store in status['stores']] == ['local', 'elastic'], status
    assert status['stores'][0]['admission_budget_bytes'] == 1048576, status"
compose run --rm "${COMPOSE_RUN_TTY_ARGS[@]}" "${client_environment[@]}" \
  --env "OVERFLOW_COLLECTION_ID=${overflow_collection_id}" \
  --entrypoint python test -c "${overflow_cache_code}"

wait_code="import json, time, urllib.request
def diagnostic(row):
    if row is None:
        return None
    target = row.get('target_status') or {}
    return {
        'work_id': row.get('work', {}).get('work_id'),
        'phase': row.get('phase'),
        'revision': row.get('revision'),
        'failure': row.get('failure'),
        'inapplicable': row.get('inapplicable'),
        'abandon_outcome': row.get('abandon_outcome'),
        'target_state': target.get('state'),
        'target_attempt': target.get('attempt'),
        'target_failure': target.get('failure'),
        'target_inapplicable': target.get('inapplicable'),
    }
deadline = time.monotonic() + 180
last = None
while time.monotonic() < deadline:
    request = urllib.request.Request(
        'http://127.0.0.1:8080/v1/work?page_size=100&sort=updated_at&order=asc',
        headers={'Authorization': 'Bearer stove0-compose-smoke-token'},
    )
    payload = json.load(urllib.request.urlopen(request, timeout=5))
    rows = payload['work']
    if rows:
        last = rows[0]
        terminal_failure = next(
            (
                row
                for row in rows
                if row['phase'] in {'failed', 'canceled', 'inapplicable', 'abandon_pending'}
            ),
            None,
        )
        if terminal_failure is not None:
            raise RuntimeError(json.dumps(diagnostic(terminal_failure), sort_keys=True))
        if all(row['phase'] == 'complete' for row in rows):
            assert any((row.get('output') or {}).get('collection_id', 0) > 0 for row in rows)
            break
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
    scheduler_diagnostic = json.load(urllib.request.urlopen(request, timeout=30))
    raise TimeoutError(
        json.dumps(
            {
                'work': [diagnostic(row) for row in rows],
                'scheduler': scheduler_diagnostic,
            },
            sort_keys=True,
        )
    )"
stove0_compose exec -T api python -c "${wait_code}"
scale_elapsed_ns=$(( $(date +%s%N) - scale_started_ns ))

output_code="import json, os, urllib.request
request = urllib.request.Request(
    'http://127.0.0.1:8080/v1/work/' + os.environ['STOVE0_WORK_ID'],
    headers={'Authorization': 'Bearer stove0-compose-smoke-token'},
)
work = json.load(urllib.request.urlopen(request, timeout=30))
assert work['phase'] == 'complete', work
targets = work['preview_acceptance']['target_plans']
assert len(targets) == 1, work
child_request = urllib.request.Request(
    'http://127.0.0.1:8080/v1/work/' + targets[0]['work_id'],
    headers={'Authorization': 'Bearer stove0-compose-smoke-token'},
)
child = json.load(urllib.request.urlopen(child_request, timeout=30))
assert child['phase'] == 'complete' and child['output'] is not None, child
print(child['output']['collection_id'])"
output_collection_id="$(stove0_compose exec -T \
  --env RIVERHOG_SMOKE_SETTLEMENT_OUTPUT=1 \
  --env "STOVE0_WORK_ID=${stove0_work_id}" \
  api python -c "${output_code}")"
test -n "${output_collection_id}"

lineage_code="import json, os
from riverhog_api_client import ApiClient
def collect(method, key, **kwargs):
    page_token = None
    rows = []
    while True:
        payload = method(page_size=100, page_token=page_token, **kwargs)
        rows.extend(payload[key])
        page_token = payload.get('next_page_token')
        if page_token is None:
            return rows
with ApiClient() as client:
    inputs = [client.get_collection(int(os.environ['INPUT_COLLECTION_ID']))]
    outputs = [client.get_collection(int(os.environ['OUTPUT_COLLECTION_ID']))]
    input_files = [row for row in collect(client.search, 'files', collection=inputs[0]['id']) if not row['path'].startswith('riverhog/')]
    output_files = [row for row in collect(client.search, 'files', collection=outputs[0]['id']) if not row['path'].startswith('riverhog/')]
    audio_count = int(os.environ['STOVE0_SMOKE_FILE_COUNT'])
    assert len(input_files) == audio_count + 1
    archive_outputs = [row for row in output_files if row['path'].endswith('.opus')]
    projected_xmp = [row for row in output_files if row['path'].endswith('.opus.xmp')]
    retained_xmp = [
        row for row in output_files
        if row['path'].startswith('audio/~source-artifacts/') and row['path'].endswith('.xmp')
    ]
    assert len(archive_outputs) == audio_count
    assert len(projected_xmp) == audio_count
    assert len(retained_xmp) == 1
    assert len(output_files) == audio_count * 2 + 1
    source_xmp = next(row for row in input_files if row['path'] == 'smoke-0000.xmp')
    assert retained_xmp[0]['bytes'] == source_xmp['bytes']
    assert retained_xmp[0]['sha256'] == source_xmp['sha256']
    elapsed_seconds = int(os.environ['STOVE0_SMOKE_ELAPSED_NS']) / 1_000_000_000
    input_bytes = sum(row['bytes'] for row in input_files)
    derivation = client.get_collection_derivation(outputs[0]['id'])
    assert derivation['derivation']['format'] == 'riverhog-collection-derivation/v1'
    authority = derivation['derivation']['input_set_sha256']
    ordinal = 0
    roots = []
    while True:
        page = client.list_processing_claim_inputs(
            derivation['derivation']['claim']['id'],
            authority_sha256=authority,
            start_ordinal=ordinal,
        )
        assert page.authority.sha256 == authority
        roots.extend(page.inputs)
        if page.next_ordinal is None:
            break
        ordinal = page.next_ordinal
    assert [row.collection_id for row in roots] == [inputs[0]['id']]
    print(json.dumps({
        'format': 'stove0-final-image-scale/v1',
        'elapsed_seconds': elapsed_seconds,
        'input_bytes': input_bytes,
        'input_files': len(input_files),
        'items_per_second': len(input_files) / elapsed_seconds,
        'measurement': 'ftp-ingress-through-opus-derived-publication',
        'mib_per_second': input_bytes / 1048576 / elapsed_seconds,
        'output_bytes': sum(row['bytes'] for row in output_files),
        'output_files': len(output_files),
    }, sort_keys=True))"
compose run --rm "${COMPOSE_RUN_TTY_ARGS[@]}" "${client_environment[@]}" \
  --env "STOVE0_SMOKE_FILE_COUNT=${smoke_file_count}" \
  --env "STOVE0_SMOKE_ELAPSED_NS=${scale_elapsed_ns}" \
  --env "INPUT_COLLECTION_ID=${input_collection_id}" \
  --env "OUTPUT_COLLECTION_ID=${output_collection_id}" \
  --entrypoint python test -c "${lineage_code}"

state_metrics_code="import json
from collections import Counter
from sqlalchemy import text
from stove0_core import SqlAlchemyStateStore, database_url_from_environment
store = SqlAlchemyStateStore(database_url_from_environment(), initialize=False)
rows = list(store.iter_work(sort='work_id', order='asc'))
assert rows and all(row.phase == 'complete' for row in rows), rows
table_names = (
    'stove0_artifact_selections',
    'stove0_evaluation_records',
    'stove0_event_cursors',
    'stove0_lifecycle_events',
    'stove0_work_records',
)
with store.engine.connect() as connection:
    table_rows = {
        name: int(connection.scalar(text(f'SELECT count(*) FROM {name}')) or 0)
        for name in table_names
    }
    table_bytes = {
        name: int(
            connection.scalar(
                text('SELECT pg_total_relation_size(to_regclass(:table_name))'),
                {'table_name': name},
            )
            or 0
        )
        for name in table_names
    }
print(json.dumps({
    'format': 'stove0-operational-scale/v1',
    'database_bytes': sum(table_bytes.values()),
    'document_bytes': sum(
        len(row.model_dump_json().encode())
        for row in rows
    ),
    'phase_counts': dict(sorted(Counter(row.phase for row in rows).items())),
    'table_bytes': table_bytes,
    'table_rows': table_rows,
    'work_records': len(rows),
}, sort_keys=True))
store.engine.dispose()"
stove0_compose exec -T api python -c "${state_metrics_code}"

target_metrics_code="import json, os
from pathlib import Path
state = Path('/var/lib/stove0-opus-target')
workspace = Path('/run/stove0-opus-target')
peak_path = Path('/sys/fs/cgroup/memory.peak')
peak = peak_path.read_text().strip() if peak_path.is_file() else None
cpu_path = Path('/sys/fs/cgroup/cpu.stat')
cpu = dict(line.split() for line in cpu_path.read_text().splitlines()) if cpu_path.is_file() else {}
filesystem = os.statvfs(workspace)
workspace_bytes = sum(path.stat().st_size for path in workspace.rglob('*') if path.is_file())
assert workspace_bytes == 0
print(json.dumps({
    'format': 'stove0-target-scale/v1',
    'accepted_records': len(tuple(state.glob('*.accepted.json'))),
    'cpu_usage_seconds': int(cpu['usage_usec']) / 1_000_000 if 'usage_usec' in cpu else None,
    'memory_peak_bytes': int(peak) if peak and peak.isdecimal() else None,
    'source_revision': os.environ['STOVE0_OPUS_TARGET_SOURCE_REVISION'],
    'status_records': len(tuple(state.glob('*.status.json'))),
    'workspace_capacity_bytes': filesystem.f_blocks * filesystem.f_frsize,
    'workspace_current_bytes': workspace_bytes,
}, sort_keys=True))"
stove0_compose exec -T opus-target python -c "${target_metrics_code}"

if [[ "${STOVE0_SMOKE_TRANSFER_METRICS:-1}" == "1" ]]; then
  compose logs --no-color app > "${smoke_root}/riverhog-transfer.log"
  PYTHONPATH="${ROOT_DIR}" python3 -c "from dataclasses import asdict
import json
from pathlib import Path
import sys
from scripts.transfer_profile import SCENARIO_OPERATIONS, summarize_transfer_log
summary = summarize_transfer_log(
    Path(sys.argv[1]).read_text(encoding='utf-8'),
    expected_operations=SCENARIO_OPERATIONS['stove0-derived-publication'],
)
print(json.dumps({'format': 'stove0-transfer-phases/v1', **asdict(summary)}, sort_keys=True))" \
    "${smoke_root}/riverhog-transfer.log"
fi

stove0_compose restart \
  api controller worker ffprobe-sampling-observer exiftool-observer \
  opus-target opus-review-sampler review-materialize-target review-rclone-effect-target
stove0_compose up --detach --wait \
  api controller worker ffprobe-sampling-observer exiftool-observer \
  opus-target opus-review-sampler review-materialize-target review-rclone-effect-target
stove0_compose exec -T api python -c "${wait_code}"
