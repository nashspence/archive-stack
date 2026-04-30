SHELL := bash
.DEFAULT_GOAL := help

UV_RUN = uv run --python 3.11 --isolated --with-requirements "$(CURDIR)/requirements-test.txt" --with-editable '.[db]'
args ?=

.PHONY: help ruff mypy lint unit spec gated-arc-disc gated-glacier-restore build build-app build-test bootstrap-garage down prod prod-profile test

help:
	@printf '%s\n' \
		'Targets:' \
		'  make ruff              Run ruff in the locked local uv environment.' \
		'  make mypy              Run mypy in the locked local uv environment.' \
		'  make lint              Run ruff, then mypy.' \
		'  make unit              Run the unit test lane locally.' \
		'  make spec              Run the fixture-backed spec harness locally.' \
		'  make gated-arc-disc    Run opt-in real-device arc-disc optical validation.' \
		'  make gated-glacier-restore Run opt-in live AWS Glacier restore validation.' \
		'  make build-app         Build the app image.' \
		'  make build-test        Build the test image.' \
		'  make build             Build both app and test images.' \
		'  make bootstrap-garage  Start Garage and apply the checked-in bucket/key bootstrap.' \
		'  make down              Tear the compose-managed test stack down.' \
		'  make prod              Run the prod-backed acceptance harness.' \
		'  make prod-profile      Run the prod-backed acceptance harness with pytest durations.' \
		'  make test              Run lint, unit, spec, then prod.' \
		'' \
		'Variables:' \
		"  args='...'             Forward arguments to mypy or pytest lanes." \
		'  COMPOSE_ENV_FILE=/abs/path/to/.env.compose' \
		'  TEST_COMPOSE_PROJECT_NAME=archive-stack-shared'

ruff:
	@$(UV_RUN) python -m ruff check .

mypy:
	@$(UV_RUN) python -m mypy src --show-error-codes --hide-error-context --no-error-summary --no-color-output $(args)

lint: ruff mypy

unit:
	@$(UV_RUN) python -m pytest -q tests/unit $(args)

spec:
	@$(UV_RUN) python -m pytest -q tests/harness/test_spec_harness.py $(args)

gated-arc-disc:
	@$(UV_RUN) python -m pytest -q tests/gated/test_arc_disc_real_device.py $(args)

gated-glacier-restore:
	@$(UV_RUN) python -m pytest -q tests/gated/test_glacier_restore.py $(args)

build-app:
	@./scripts/build_app.sh

build-test:
	@./scripts/build_test.sh

build: build-app build-test

bootstrap-garage:
	@./scripts/bootstrap_garage.sh

down:
	@./scripts/compose_down.sh

prod:
	@./scripts/prod.sh $(args)

prod-profile:
	@./scripts/prod_profile.sh $(args)

test: lint unit spec prod
