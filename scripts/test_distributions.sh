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

workspace_wheel_closure() {
  local python="$1"
  local root_wheel="$2"
  "${python}" -I - "${DIST_DIR}" "${root_wheel}" <<'PY'
import re
import sys
import zipfile
from email.parser import BytesParser
from pathlib import Path


def canonical_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


packages = {}
for wheel in sorted(Path(sys.argv[1]).glob("*.whl")):
    with zipfile.ZipFile(wheel) as archive:
        metadata_path = next(
            name for name in archive.namelist() if name.endswith(".dist-info/METADATA")
        )
        metadata = BytesParser().parsebytes(archive.read(metadata_path))
    packages[canonical_name(metadata["Name"])] = (wheel, metadata)

pending = [Path(sys.argv[2])]
selected = {}
while pending:
    wheel = pending.pop()
    with zipfile.ZipFile(wheel) as archive:
        metadata_path = next(
            name for name in archive.namelist() if name.endswith(".dist-info/METADATA")
        )
        metadata = BytesParser().parsebytes(archive.read(metadata_path))
    name = canonical_name(metadata["Name"])
    if name in selected:
        continue
    selected[name] = wheel
    for requirement in metadata.get_all("Requires-Dist", []):
        match = re.match(r"[A-Za-z0-9_.-]+", requirement)
        if match and canonical_name(match.group()) in packages:
            pending.append(packages[canonical_name(match.group())][0])

for wheel in sorted(selected.values()):
    print(wheel)
PY
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

smoke_workspace_distribution() {
  local name="$1"
  local pattern="$2"
  local import_check="$3"
  local executable="${4:-}"
  local wheel
  wheel="$(single_wheel "${pattern}")"
  run_uv venv --python 3.12 "${SCRATCH}/${name}"
  mapfile -t wheels < <(
    workspace_wheel_closure "${SCRATCH}/${name}/bin/python" "${wheel}"
  )
  run_uv pip install \
    --strict \
    --python "${SCRATCH}/${name}/bin/python" \
    --find-links "${DIST_DIR}" \
    "${wheels[@]}"
  (
    cd "${SCRATCH}"
    env -u PYTHONPATH "${SCRATCH}/${name}/bin/python" -I -c "${import_check}"
    if [[ -n "${executable}" ]]; then
      env -u PYTHONPATH "${SCRATCH}/${name}/bin/${executable}" --help >/dev/null
    fi
  )
}

client_wheel="$(single_wheel 'riverhog_client-*.whl')"
recovery_wheel="$(single_wheel 'riverhog_recover-*.whl')"
server_wheel="$(single_wheel 'riverhog_server-*.whl')"

run_uv venv --python 3.12 "${SCRATCH}/client"
mapfile -t client_wheels < <(
  workspace_wheel_closure "${SCRATCH}/client/bin/python" "${client_wheel}"
)
run_uv pip install \
  --strict \
  --python "${SCRATCH}/client/bin/python" \
  --find-links "${DIST_DIR}" \
  "${client_wheels[@]}"
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
mapfile -t recovery_wheels < <(
  workspace_wheel_closure "${SCRATCH}/recovery/bin/python" "${recovery_wheel}"
)
run_uv pip install \
  --strict \
  --python "${SCRATCH}/recovery/bin/python" \
  --find-links "${DIST_DIR}" \
  "${recovery_wheels[@]}"
(
  cd "${SCRATCH}"
  env -u PYTHONPATH "${SCRATCH}/recovery/bin/riverhog-recover" --help >/dev/null
  "${SCRATCH}/recovery/bin/python" -I -c \
    'import importlib.metadata as m; import riverhog_recover; m.version("riverhog-recover")'
)

run_uv venv --python 3.12 "${SCRATCH}/server"
mapfile -t server_wheels < <(
  workspace_wheel_closure "${SCRATCH}/server/bin/python" "${server_wheel}"
)
run_uv pip install \
  --strict \
  --python "${SCRATCH}/server/bin/python" \
  --find-links "${DIST_DIR}" \
  "${server_wheels[@]}"
(
  cd "${SCRATCH}"
  env -u PYTHONPATH "${SCRATCH}/server/bin/python" -I -c \
    'import importlib.metadata as m; from riverhog_api.app import create_app; app = create_app(); assert app.version == m.version("riverhog-server"); assert any(ep.name == "riverhog-api" for ep in m.entry_points(group="console_scripts"))'
)

smoke_workspace_distribution \
  riverhog-adapters \
  'riverhog_adapters-*.whl' \
  'import importlib.metadata as m; import riverhog_adapters.app; m.version("riverhog-adapters")' \
  riverhog-adapters
smoke_workspace_distribution \
  stove0-client \
  'stove0_client-*.whl' \
  'import importlib.metadata as m; import stove0_cli.main; m.version("stove0-client")' \
  stove0
smoke_workspace_distribution \
  stove0-server \
  'stove0_server-*.whl' \
  'import importlib.metadata as m; import stove0_api.app; import stove0_core; m.version("stove0-server")' \
  stove0-server
smoke_workspace_distribution \
  stove0-maintained-extensions \
  'stove0_maintained_extensions-*.whl' \
  'import importlib.metadata as m; import stove0_extensions.app; m.version("stove0-maintained-extensions")' \
  stove0-maintained-extension
smoke_workspace_distribution \
  stove0-observer-support \
  'stove0_observer_support-*.whl' \
  'import importlib.metadata as m; import stove0_observer_support; m.version("stove0-observer-support")' \
  stove0-observer-conformance
smoke_workspace_distribution \
  stove0-target-support \
  'stove0_target_support-*.whl' \
  'import importlib.metadata as m; import stove0_target_support; m.version("stove0-target-support")' \
  stove0-target-conformance
smoke_workspace_distribution \
  gogurt \
  'gogurt-*.whl' \
  'import importlib.metadata as m; import gogurt.cli; m.version("gogurt")' \
  gogurt
smoke_workspace_distribution \
  mango-fish \
  'mango_fish-*.whl' \
  'import importlib.metadata as m; import mango_fish.cli; m.version("mango-fish")' \
  mango-fish

printf 'All application distribution smoke tests passed.\n'
