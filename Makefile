SHELL := bash
.DEFAULT_GOAL := help

UV_RUN = uv run --python 3.11 --isolated --with-requirements "$(CURDIR)/requirements-test.txt" --with-editable '.[db]'
args ?=

.PHONY: help ruff mypy lint unit spec stop-spec build build-app build-test bootstrap-garage down test

help:
	@printf '%s\n' \
		'Targets:' \
		'  make ruff              Run ruff in the locked local uv environment.' \
		'  make mypy              Run mypy in the locked local uv environment.' \
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
		'  COMPOSE_ENV_FILE=/abs/path/to/.env.compose' \
		'  TEST_COMPOSE_PROJECT_NAME=riverhog-shared'

ruff:
	@$(UV_RUN) python -m ruff check .

mypy:
	@$(UV_RUN) python -m mypy src --show-error-codes --hide-error-context --no-error-summary --no-color-output $(args)

lint: ruff mypy

unit:
	@$(UV_RUN) python -m pytest -q tests/unit $(args)

spec:
	@$(UV_RUN) python -m pytest -q tests/harness/test_spec_harness.py $(args)

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
