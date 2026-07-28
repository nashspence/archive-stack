#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DIST_DIR="${DIST_DIR:-${ROOT_DIR}/dist}"
MISE_BIN="${MISE_BIN:-mise}"
SCRATCH="$(mktemp -d "${TMPDIR:-/tmp}/riverhog-dist-smoke.XXXXXX")"
trap 'rm -rf "${SCRATCH}"' EXIT

run_uv() {
  "${MISE_BIN}" x -- uv --no-config "$@"
}

single_wheel() {
  local pattern="$1"
  local wheels=("${DIST_DIR}"/${pattern})
  if [[ "${#wheels[@]}" -ne 1 || ! -f "${wheels[0]}" ]]; then
    printf 'expected one wheel matching %s; found %s\n' "${pattern}" "${#wheels[@]}" >&2
    return 1
  fi
  printf '%s' "${wheels[0]}"
}

client_wheel="$(single_wheel 'riverhog_client-*.whl')"
recovery_wheel="$(single_wheel 'riverhog_recover-*.whl')"
server_wheel="$(single_wheel 'riverhog_server-*.whl')"

run_uv venv --python 3.12 "${SCRATCH}/client"
run_uv pip install \
  --strict \
  --python "${SCRATCH}/client/bin/python" \
  --find-links "${DIST_DIR}" \
  "${client_wheel}"
(
  cd "${SCRATCH}"
  env -u PYTHONPATH "${SCRATCH}/client/bin/riverhog" --help >/dev/null
  client_version="$(env -u PYTHONPATH "${SCRATCH}/client/bin/riverhog" --version)"
  installed_version="$(
    "${SCRATCH}/client/bin/python" -I -c \
      'import importlib.metadata as m; print(m.version("riverhog-client"))'
  )"
  [[ "${client_version}" == "${installed_version}" ]]
  "${SCRATCH}/client/bin/python" -I -c \
    'import importlib.metadata as m; import riverhog_cli.main; import riverhog_cli_support.output; m.version("riverhog-cli-support")'
)

run_uv venv --python 3.12 "${SCRATCH}/recovery"
run_uv pip install \
  --strict \
  --python "${SCRATCH}/recovery/bin/python" \
  --find-links "${DIST_DIR}" \
  "${recovery_wheel}"
(
  cd "${SCRATCH}"
  env -u PYTHONPATH "${SCRATCH}/recovery/bin/riverhog-recover" --help >/dev/null
  "${SCRATCH}/recovery/bin/python" -I -c \
    'import importlib.metadata as m; import riverhog_recover; m.version("riverhog-recover")'
)

run_uv venv --python 3.12 "${SCRATCH}/server"
run_uv pip install \
  --strict \
  --python "${SCRATCH}/server/bin/python" \
  --find-links "${DIST_DIR}" \
  "${server_wheel}"
(
  cd "${SCRATCH}"
  env -u PYTHONPATH "${SCRATCH}/server/bin/python" -I -c \
    'import importlib.metadata as m; from riverhog_api.app import create_app; app = create_app(); assert app.version == m.version("riverhog-server"); assert any(ep.name == "riverhog-api" for ep in m.entry_points(group="console_scripts"))'
)

printf 'Riverhog server, client, and recovery distribution smoke tests passed.\n'
