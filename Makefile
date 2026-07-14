SHELL := bash
.DEFAULT_GOAL := help

MISE_BIN ?= mise
FILES ?= .
TESTS ?= tests/unit
SPEC_TESTS ?= tests/harness/test_spec_harness.py
POSTGRES_TESTS ?= tests/integration/test_collection_deletion_concurrency.py
UV_RUN = "$(MISE_BIN)" x -- uv run --locked --no-default-groups --group dev --extra db
MYPY_FLAGS = --show-error-codes --hide-error-context --no-error-summary --no-color-output
args ?=

.PHONY: help ruff ruff-fix format fix mypy lint unit spec postgres-concurrency stop-spec build build-app build-test bootstrap-garage down test

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
		'  make postgres-concurrency Run collection deletion race tests against disposable Postgres.' \
		'  make stop-spec         Stop any in-flight local spec harness process.' \
		'  make build-app         Build the app image.' \
		'  make build-test        Build the test image.' \
		'  make build             Build both app and test images.' \
		'  make bootstrap-garage  Start Garage and apply the checked-in bucket/key bootstrap.' \
		'  make down              Tear the compose-managed test stack down.' \
		'  make test              Run lint, then unit.' \
		'' \
		'Variables:' \
		"  args='...'             Forward arguments to mypy or pytest lanes." \
		"  FILES='...'            Narrow ruff, ruff-fix, or format to specific files." \
		"  TESTS='...'            Narrow the unit test lane to specific tests." \
		"  SPEC_TESTS='...'       Narrow the spec lane to specific tests." \
		"  POSTGRES_TESTS='...'   Select the disposable Postgres test file." \
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
	$(call UV_CMD,python -m mypy src $(MYPY_FLAGS) $(args))
	$(call UV_CMD,python -m mypy services/munchy-av1-nvenc/app/main.py $(MYPY_FLAGS) $(args),MYPYPATH="$(CURDIR)/src")
	$(call UV_CMD,python -m mypy services/munchy-runner/app/main.py $(MYPY_FLAGS) $(args),MYPYPATH="$(CURDIR)/src")

lint: ruff mypy

unit:
	$(call UV_CMD,python -m pytest -q $(TESTS) $(args))

spec:
	$(call UV_CMD,python -m pytest -q $(SPEC_TESTS) $(args))

postgres-concurrency:
	@POSTGRES_TESTS="$(POSTGRES_TESTS)" ./scripts/test_postgres_collection_deletion_concurrency.sh

stop-spec:
	@./scripts/stop_spec.sh

build-app:
	@./scripts/build_app.sh

build-test:
	@./scripts/build_test.sh

build: build-app build-test

bootstrap-garage:
	@./scripts/bootstrap_garage.sh

down:
	@./scripts/compose_down.sh

test: lint unit
