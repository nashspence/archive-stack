#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MISE_BIN="${MISE_BIN:-mise}"
REVISION="1e3d2860d46e94e777e1b17c7a6f2436387e3ecc"
CHECKSUM="516ce226b3d53c9859fcc973edc8976078dcee5600f72f7c27442857e4a3d16c"
URL="https://github.com/C2SP/CCTV/archive/${REVISION}.tar.gz"
SCRATCH="$(mktemp -d "${TMPDIR:-/tmp}/riverhog-c2sp-vectors.XXXXXX")"
trap 'rm -rf "${SCRATCH}"' EXIT

if ! command -v "${MISE_BIN}" >/dev/null 2>&1; then
  printf '%s\n' 'The C2SP vector target requires mise on PATH.' >&2
  exit 127
fi

curl --fail --location --silent --show-error \
  --output "${SCRATCH}/cctv.tar.gz" \
  "${URL}"
printf '%s  %s\n' "${CHECKSUM}" "${SCRATCH}/cctv.tar.gz" | shasum -a 256 --check
tar -xzf "${SCRATCH}/cctv.tar.gz" -C "${SCRATCH}"

CCTV_AGE_TESTDATA="${SCRATCH}/CCTV-${REVISION}/age/testdata" \
  "${MISE_BIN}" x -- uv run --locked --all-packages --group dev \
  python -m pytest -q \
  "${ROOT_DIR}/packages/riverhog-age/tests/test_riverhog_age_c2sp_vectors.py"
