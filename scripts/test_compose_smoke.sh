#!/usr/bin/env bash
set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_compose_env.sh"

setup_test_compose_project
configure_compose_tty
export COMPOSE_PROFILES=development
export RIVERHOG_API_PORT="${RIVERHOG_API_PORT:-0}"

cleanup() {
  compose down --volumes --remove-orphans
}
trap cleanup EXIT

"${ROOT_DIR}/scripts/bootstrap_garage.sh"
compose run --rm \
  --env RIVERHOG_GARAGE_ARCHIVE_INGRESS_TEST=1 \
  --entrypoint python \
  test -m pytest -q tests/integration/test_garage_encrypted_archive_store.py
ensure_compose_image app
compose up --detach --wait app

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
    data=json.dumps({'access': [{'permission': 'catalog:read', 'resource': '*'}]}).encode(),
    headers={
        'Authorization': 'Bearer ' + os.environ['RIVERHOG_SMOKE_TOKEN'],
        'Content-Type': 'application/json',
    },
)
created = json.load(urllib.request.urlopen(request))
assert created['app'] == 'smoke'"
compose exec -T --env "RIVERHOG_SMOKE_TOKEN=${bootstrap_token}" app python -c "${create_code}"

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
