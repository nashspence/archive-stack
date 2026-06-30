SHELL := bash
.DEFAULT_GOAL := help

UV_BIN ?= uv
UV_RUN = "$(UV_BIN)" run --python 3.11 --isolated --with-requirements "$(CURDIR)/requirements-test.txt" --with-editable '.[db]'
MYPY_FLAGS = --show-error-codes --hide-error-context --no-error-summary --no-color-output
args ?=

.PHONY: help ruff mypy lint unit spec stop-spec build build-app build-test bootstrap-garage down test

define UV_CMD
	@if ! command -v "$(UV_BIN)" >/dev/null 2>&1; then \
		printf '%s\n' 'Riverhog Makefile targets require uv on PATH, or UV_BIN=/abs/path/to/uv.' >&2; \
		printf '%s\n' 'Install uv or pass UV_BIN explicitly, then rerun make.' >&2; \
		exit 127; \
	fi; \
	$(if $(2),$(2) )$(UV_RUN) $(1)
endef

help:
	@printf '%s\n' \
		'Targets:' \
		'  make ruff              Run repo-wide ruff in the locked local uv environment.' \
		'  make mypy              Run repo-wide mypy in the locked local uv environment.' \
		'  make lint              Run ruff, then mypy.' \
		'  make unit              Run the unit test lane locally.' \
		'  make spec              Run the fixture-backed spec harness locally.' \
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
		'  UV_BIN=/abs/path/to/uv Use a specific uv binary instead of uv on PATH.' \
		'  COMPOSE_ENV_FILE=/abs/path/to/.env.compose' \
		'  TEST_COMPOSE_PROJECT_NAME=riverhog-shared'

ruff:
	$(call UV_CMD,python -m ruff check .)

mypy:
	$(call UV_CMD,python -m mypy src $(MYPY_FLAGS) $(args))
	$(call UV_CMD,python -m mypy services/munchy-av1-nvenc/app/main.py $(MYPY_FLAGS) $(args),MYPYPATH="$(CURDIR)/src")
	$(call UV_CMD,python -m mypy services/munchy-runner/app/main.py $(MYPY_FLAGS) $(args),MYPYPATH="$(CURDIR)/src")

lint: ruff mypy

unit:
	$(call UV_CMD,python -m pytest -q tests/unit $(args))

spec:
	$(call UV_CMD,python -m pytest -q tests/harness/test_spec_harness.py $(args))

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
