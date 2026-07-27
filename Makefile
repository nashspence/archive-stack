SHELL := bash
.DEFAULT_GOAL := help

MISE_BIN ?= mise
FILES ?= .
TESTS ?= companions packages riverhog tests/unit utilities
SPEC_TESTS ?= tests/harness/test_spec_harness.py
POSTGRES_TESTS ?= tests/integration/test_collection_deletion_concurrency.py tests/integration/test_download_allowance_concurrency.py
TUS_URL ?=
UV_RUN = "$(MISE_BIN)" x -- uv run --locked --all-packages --group dev
MYPY_FLAGS = --show-error-codes --hide-error-context --no-error-summary --no-color-output
MYPY_SOURCES = \
	companions/jeb/client/src \
	companions/jeb/server/src \
	companions/munchy/client/src \
	companions/munchy/server/src \
	packages/application-access/src \
	packages/cli-support/src \
	packages/config-validation/src \
	packages/jeb-api-client/src \
	packages/jeb-cli-support/src \
	packages/jeb-protocol/src \
	packages/lifecycle-events/src \
	packages/munchy-api-client/src \
	packages/munchy-config/src \
	packages/munchy-target-support/src \
	packages/munchy-workflows/src \
	packages/riverhog-age/src \
	packages/riverhog-api-client/src \
	packages/riverhog-protocol/src \
	packages/time-formats/src \
	packages/tus-transport/src \
	riverhog/client/src \
	riverhog/server/src \
	utilities/gogurt/src \
	utilities/mango-fish/src
args ?=

.PHONY: help ruff ruff-fix format fix mypy lint unit spec postgres-concurrency tus-throughput archive-throughput archive-download-smoke stop-spec dist build build-riverhog build-jeb build-mango-fish build-munchy-server build-munchy-av1-nvenc build-test bootstrap-garage down test

define UV_CMD
	@if ! command -v "$(MISE_BIN)" >/dev/null 2>&1; then \
		printf '%s\n' 'Riverhog Makefile targets require mise on PATH, or MISE_BIN=/abs/path/to/mise.' >&2; \
		printf '%s\n' 'Install mise or pass MISE_BIN explicitly, then rerun make.' >&2; \
		exit 127; \
	fi; \
	$(if $(2),$(2) )$(UV_RUN) $(1)
endef

help:
	@printf '%s\n' \
		'Targets:' \
		'  make ruff              Run repo-wide ruff in the locked local uv environment.' \
		'  make ruff-fix          Run ruff --fix in the locked local uv environment.' \
		'  make format            Run ruff format in the locked local uv environment.' \
		'  make fix               Run ruff-fix, then format.' \
		'  make mypy              Run repo-wide mypy in the locked local uv environment.' \
		'  make lint              Run ruff, then mypy.' \
		'  make unit              Run the unit test lane locally.' \
		'  make spec              Run the fixture-backed spec harness locally.' \
		'  make postgres-concurrency Run database concurrency tests against disposable Postgres.' \
		'  make tus-throughput    Measure a TUS endpoint with incomplete, deleted probes.' \
		'  make archive-throughput  Measure, verify, and delete an archive upload probe.' \
		'  make archive-download-smoke  Verify signed CloudFront download and probe cleanup.' \
		'  make stop-spec         Stop any in-flight local spec harness process.' \
		'  make dist              Build every Python distribution independently.' \
		'  make build-riverhog    Build the Riverhog image.' \
		'  make build-jeb         Build the Jeb image.' \
		'  make build-mango-fish  Build the Mango Fish image.' \
		'  make build-munchy-server Build the Munchy server image.' \
		'  make build-munchy-av1-nvenc Build the Munchy AV1 NVENC image.' \
		'  make build-test        Build the test image.' \
		'  make build             Build every application and test image.' \
		'  make bootstrap-garage  Start Garage and apply the checked-in bucket/key bootstrap.' \
		'  make down              Tear the compose-managed test stack down.' \
		'  make test              Run lint, then unit.' \
		'' \
		'Variables:' \
		"  args='...'             Forward arguments to mypy or pytest lanes." \
		"  FILES='...'            Narrow ruff, ruff-fix, or format to specific files." \
		"  TESTS='...'            Narrow the unit test lane to specific tests." \
		"  SPEC_TESTS='...'       Narrow the spec lane to specific tests." \
		"  POSTGRES_TESTS='...'   Select disposable Postgres test files." \
		'  TUS_URL=https://...    TUS creation URL for make tus-throughput.' \
		'  ARCHIVE_SOURCE=/path   Existing file for make archive-throughput.' \
		'  ARCHIVE_STORE=deep     Optional store for make archive-download-smoke.' \
		'  TUS_BENCHMARK_USER/PASSWORD Optional benchmark Basic-auth credentials.' \
		'  MISE_BIN=/abs/path/to/mise Use a specific mise binary instead of mise on PATH.' \
		'  COMPOSE_ENV_FILE=/abs/path/to/.env.compose' \
		'  TEST_COMPOSE_PROJECT_NAME=riverhog-shared'

ruff:
	$(call UV_CMD,python -m ruff check $(FILES) $(args))

ruff-fix:
	$(call UV_CMD,python -m ruff check --fix $(FILES) $(args))

format:
	$(call UV_CMD,python -m ruff format $(FILES) $(args))

fix: ruff-fix format

mypy:
	$(call UV_CMD,python -m mypy $(MYPY_SOURCES) $(MYPY_FLAGS) $(args))

lint: ruff mypy

unit:
	$(call UV_CMD,python -m pytest -q $(TESTS) $(args))

spec:
	$(call UV_CMD,python -m pytest -q $(SPEC_TESTS) $(args))

postgres-concurrency:
	@POSTGRES_TESTS="$(POSTGRES_TESTS)" ./scripts/test_postgres_concurrency.sh

tus-throughput:
	@if [[ -z "$(TUS_URL)" ]]; then \
		printf '%s\n' 'TUS_URL is required, for example TUS_URL=https://host/files/' >&2; \
		exit 2; \
	fi
	$(call UV_CMD,python scripts/tus_throughput.py "$(TUS_URL)" $(args))

archive-throughput:
	@test -n "$(ARCHIVE_SOURCE)" || { echo "ARCHIVE_SOURCE is required" >&2; exit 2; }
	$(call UV_CMD,python scripts/archive_upload_throughput.py "$(ARCHIVE_SOURCE)" $(args))

archive-download-smoke:
	$(call UV_CMD,python scripts/archive_download_smoke.py $(if $(ARCHIVE_STORE),--store "$(ARCHIVE_STORE)" )$(args))

stop-spec:
	@./scripts/stop_spec.sh

dist:
	@if ! command -v "$(MISE_BIN)" >/dev/null 2>&1; then \
		printf '%s\n' 'Riverhog Makefile targets require mise on PATH, or MISE_BIN=/abs/path/to/mise.' >&2; \
		exit 127; \
	fi
	@"$(MISE_BIN)" x -- uv build --all-packages

build-riverhog:
	@./scripts/build_riverhog.sh

build-jeb:
	@docker compose --file companions/jeb/server/compose.yaml build jeb

build-mango-fish:
	@docker build --file utilities/mango-fish/Dockerfile --tag mango-fish:dev .

build-munchy-server:
	@docker compose --file companions/munchy/server/compose.yaml build munchy-server

build-munchy-av1-nvenc:
	@MUNCHY_AV1_NVENC_IMAGE=munchy-av1-nvenc-target:dev SOURCE_REVISION=development docker compose --file companions/munchy/server/targets/av1-nvenc/compose.yaml build api

build-test:
	@./scripts/build_test.sh

build: build-riverhog build-jeb build-mango-fish build-munchy-server build-munchy-av1-nvenc build-test

bootstrap-garage:
	@./scripts/bootstrap_garage.sh

down:
	@./scripts/compose_down.sh

test: lint unit
