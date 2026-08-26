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
  local root_wheel="$1"
  "${MISE_BIN}" x -- uv run --locked --all-packages --group dev \
    python -I - "${DIST_DIR}" "${root_wheel}" <<'PY'
import re
import sys
import zipfile
from email.parser import BytesParser
from pathlib import Path

from packaging.markers import default_environment
from packaging.requirements import Requirement


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
    for raw_requirement in metadata.get_all("Requires-Dist", []):
        requirement = Requirement(raw_requirement)
        if requirement.marker is not None and not requirement.marker.evaluate(
            environment=default_environment()
        ):
            continue
        dependency = canonical_name(requirement.name)
        if dependency in packages:
            pending.append(packages[dependency][0])

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
  shift 3
  local wheel
  wheel="$(single_wheel "${pattern}")"
  run_uv venv --python 3.12 "${SCRATCH}/${name}"
  mapfile -t wheels < <(
    workspace_wheel_closure "${wheel}"
  )
  run_uv pip install \
    --strict \
    --python "${SCRATCH}/${name}/bin/python" \
    --find-links "${DIST_DIR}" \
    "${wheels[@]}"
  (
    cd "${SCRATCH}"
    env -u PYTHONPATH "${SCRATCH}/${name}/bin/python" -I -c "${import_check}"
    for executable in "$@"; do
      env -u PYTHONPATH "${SCRATCH}/${name}/bin/${executable}" --help >/dev/null
    done
  )
}

client_wheel="$(single_wheel 'riverhog_client-*.whl')"
recovery_wheel="$(single_wheel 'riverhog_recover-*.whl')"
server_wheel="$(single_wheel 'riverhog_server-*.whl')"

run_uv venv --python 3.12 "${SCRATCH}/client"
mapfile -t client_wheels < <(
  workspace_wheel_closure "${client_wheel}"
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
  "${SCRATCH}/client/bin/python" -I -c \
    'import importlib.metadata as m; names = {d.metadata["Name"].lower() for d in m.distributions()}; native = {"riverhog-provenance-linux-observer", "riverhog-provenance-macos-observer", "riverhog-provenance-windows-observer"}; contracts = {"riverhog-provenance-linux-contracts", "riverhog-provenance-macos-contracts", "riverhog-provenance-windows-contracts"}; assert names.isdisjoint(native | contracts); assert "riverhog-provenance-contracts" in names'
)

linux_observer_wheel="$(single_wheel 'riverhog_provenance_linux_observer-*.whl')"
mapfile -t linux_observer_wheels < <(
  workspace_wheel_closure "${linux_observer_wheel}"
)
run_uv pip install \
  --strict \
  --python "${SCRATCH}/client/bin/python" \
  --find-links "${DIST_DIR}" \
  "${linux_observer_wheels[@]}"
"${SCRATCH}/client/bin/riverhog" local provenance-observer show riverhog-linux --json \
  | "${SCRATCH}/client/bin/python" -I -c \
    'import json, sys; value = json.load(sys.stdin); assert value["observer_id"] == "riverhog-provenance-linux-observer/v1"; assert value["contract_id"] == "riverhog-provenance-linux-observation/v1"; assert len(value["contract_sha256"]) == 64'

run_uv venv --python 3.12 "${SCRATCH}/recovery"
mapfile -t recovery_wheels < <(
  workspace_wheel_closure "${recovery_wheel}"
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
  workspace_wheel_closure "${server_wheel}"
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
  riverhog-ftp-adapter \
  'riverhog_ftp_adapter-*.whl' \
  'import importlib.metadata as m; import riverhog_ftp_adapter.app; m.version("riverhog-ftp-adapter")' \
  riverhog-ftp-adapter
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
  stove0-ffprobe-sampling-observer \
  'stove0_ffprobe_sampling_observer-*.whl' \
  'import importlib.metadata as m; import stove0_ffprobe_sampling_observer.app; m.version("stove0-ffprobe-sampling-observer")' \
  stove0-ffprobe-sampling-observer
smoke_workspace_distribution \
  stove0-exiftool-observer \
  'stove0_exiftool_observer-*.whl' \
  'import importlib.metadata as m; import stove0_exiftool_observer.app; m.version("stove0-exiftool-observer")' \
  stove0-exiftool-observer
smoke_workspace_distribution \
  stove0-nvenc-av1-opus-target \
  'stove0_nvenc_av1_opus_target-*.whl' \
  'import importlib.metadata as m; import stove0_nvenc_av1_opus_target.app; m.version("stove0-nvenc-av1-opus-target")' \
  stove0-nvenc-av1-opus-target
smoke_workspace_distribution \
  stove0-nvenc-av1-opus-review-sampler \
  'stove0_nvenc_av1_opus_review_sampler-*.whl' \
  'import importlib.metadata as m; import stove0_nvenc_av1_opus_review_sampler.app; m.version("stove0-nvenc-av1-opus-review-sampler")' \
  stove0-nvenc-av1-opus-review-sampler
smoke_workspace_distribution \
  stove0-opus-target \
  'stove0_opus_target-*.whl' \
  'import importlib.metadata as m; import stove0_opus_target.app; m.version("stove0-opus-target")' \
  stove0-opus-target
smoke_workspace_distribution \
  stove0-opus-review-sampler \
  'stove0_opus_review_sampler-*.whl' \
  'import importlib.metadata as m; import stove0_opus_review_sampler.app; m.version("stove0-opus-review-sampler")' \
  stove0-opus-review-sampler
smoke_workspace_distribution \
  stove0-review-target \
  'stove0_review_target-*.whl' \
  'import importlib.metadata as m; import stove0_review_target.app; m.version("stove0-review-target")' \
  stove0-review-target
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
  stove0-review-sampler-support \
  'stove0_review_sampler_support-*.whl' \
  'import importlib.metadata as m; import stove0_review_sampler_support; m.version("stove0-review-sampler-support")' \
  stove0-review-sampler-conformance \
  stove0-review-sampler-schemas
smoke_workspace_distribution \
  gogurt-listener-runtime \
  'gogurt_listener_runtime-*.whl' \
  'import importlib.metadata as m; import gogurt_listener_runtime.listener; assert m.version("gogurt-listener-runtime")'
smoke_workspace_distribution \
  gogurt \
  'gogurt-*.whl' \
  'import importlib.metadata as m, sys; import gogurt.cli; import gogurt_listener_runtime.listener; m.version("gogurt"); names = {d.metadata["Name"].lower() for d in m.distributions()}; assert "gogurt-listener-runtime" in names; native = {"gogurt-linux", "gogurt-macos", "gogurt-windows"}; expected = "gogurt-linux" if sys.platform.startswith("linux") else "gogurt-macos" if sys.platform == "darwin" else "gogurt-windows"; assert names & native == {expected}' \
  gogurt
smoke_workspace_distribution \
  mango-fish \
  'mango_fish-*.whl' \
  'import importlib.metadata as m; import mango_fish.cli; m.version("mango-fish")' \
  mango-fish

printf 'All application distribution smoke tests passed.\n'
