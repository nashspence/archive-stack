#!/usr/bin/env bash
set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_compose_env.sh"

setup_test_compose_project
SOURCE_REVISION="$(git -C "${ROOT_DIR}" rev-parse --verify HEAD)" compose build --sbom=true app
