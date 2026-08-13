SHELL := bash
.DEFAULT_GOAL := help

MISE_BIN ?= mise
FILES ?= .
TESTS ?= companions packages riverhog tests/unit utilities
SPEC_TESTS ?= tests/harness/test_spec_harness.py
POSTGRES_TESTS ?= tests/integration/test_catalog_schema_postgres.py tests/integration/test_collection_deletion_concurrency.py tests/integration/test_download_allowance_concurrency.py
PYTHON_PATHS ?= companions packages riverhog scripts tests utilities
TUS_URL ?=
RELEASE_VERSION ?= 1.0.0
RELEASE_OUTPUT ?=
RELEASE_SUMMARY ?=
RELEASE_SIGNING_KEY ?=
RELEASE_PUBLIC_KEY ?=
UV_RUN = "$(MISE_BIN)" x -- uv run --locked --all-packages --group dev
BAKE_FILE = docker-bake.hcl
MYPY_FLAGS = --show-error-codes --hide-error-context --no-error-summary --no-color-output
MYPY_SOURCES = \
	companions/jeb/client/src \
	companions/jeb/server/src \
	companions/munchy/client/src \
	companions/munchy/server/src \
	packages/application-access/src \
	packages/config-validation/src \
	packages/file-download/src \
	packages/http-api-contracts/src \
	packages/jeb-api-client/src \
	packages/jeb-cli-support/src \
	packages/jeb-protocol/src \
	packages/lifecycle-events/src \
	packages/media-preflight/src \
	packages/munchy-api-client/src \
	packages/munchy-config/src \
	packages/munchy-target-support/src \
	packages/munchy-workflows/src \
	packages/riverhog-age/src \
	packages/riverhog-api-client/src \
	packages/riverhog-cli-support/src \
	packages/riverhog-protocol/src \
	packages/riverhog-provenance/src \
	packages/state-schema/src \
	packages/time-formats/src \
	packages/tus-transport/src \
	riverhog/client/src \
	riverhog/recovery/src \
	riverhog/server/src \
	scripts/provider_qualification.py \
	scripts/release.py \
	utilities/gogurt/src \
	utilities/mango-fish/src
args ?=

.PHONY: help license ruff ruff-fix format format-check fix mypy lint compile unit spec dependency-readiness provider-qualification release-check release-plan release-dry-run release-governance-check release-evidence release-verify c2sp-vectors postgres-concurrency compose-smoke tus-throughput transfer-profile stop-spec dist dist-smoke build build-riverhog build-jeb build-mango-fish build-munchy-server build-munchy-av1-nvenc build-test bootstrap-garage down test

define UV_CMD
	@if ! command -v "$(MISE_BIN)" >/dev/null 2>&1; then \
		printf '%s\n' 'Riverhog Makefile targets require mise on PATH, or MISE_BIN=/abs/path/to/mise.' >&2; \
		printf '%s\n' 'Install mise or pass MISE_BIN explicitly, then rerun make.' >&2; \
		exit 127; \
	fi; \
	$(if $(2),$(2) )$(UV_RUN) $(1)
endef

define BAKE_IMAGE
	@revision="$$(git rev-parse --verify HEAD)"; \
	created="$$(git show -s --format=%cI HEAD)"; \
	epoch="$$(git show -s --format=%ct HEAD)"; \
	docker buildx bake --file "$(BAKE_FILE)" --load \
		--set "$(1).args.SOURCE_REVISION=$$revision" \
		--set "$(1).args.BUILD_CREATED=$$created" \
		--set "$(1).args.SOURCE_DATE_EPOCH=$$epoch" \
		--set "$(1).args.RELEASE_VERSION=development" "$(1)"
endef

help:
	@printf '%s\n' \
		'Targets:' \
		'  make license           Verify SPDX/REUSE coverage for every tracked path.' \
		'  make ruff              Run repo-wide ruff in the locked local uv environment.' \
		'  make ruff-fix          Run ruff --fix in the locked local uv environment.' \
		'  make format            Run ruff format in the locked local uv environment.' \
		'  make format-check      Verify ruff formatting without changing files.' \
		'  make fix               Run ruff-fix, then format.' \
		'  make mypy              Run repo-wide mypy in the locked local uv environment.' \
		'  make lint              Run license, format, ruff, and mypy checks.' \
		'  make compile           Byte-compile all repository Python files.' \
		'  make unit              Run the unit test lane locally.' \
		'  make spec              Run the fixture-backed spec harness locally.' \
		'  make dependency-readiness Verify the live uv graph and Dependabot release gate.' \
		'  make provider-qualification Run the operator/provider qualification command.' \
		'  make release-check     Validate the coordinated release-unit contract.' \
		'  make release-plan      Print the exact-SHA v1 release inventory as JSON.' \
		'  make release-dry-run   Version and smoke-test an exact-SHA copy without publishing.' \
		'  make release-governance-check Verify live GitHub controls against release.toml.' \
		'  make release-evidence  Build signed exact-SHA evidence with external release keys.' \
		'  make release-verify    Verify a generated release evidence directory.' \
		'  make c2sp-vectors      Download and run the pinned C2SP age conformance corpus.' \
		'  make postgres-concurrency Run database concurrency tests against disposable Postgres.' \
		'  make compose-smoke     Start and verify a fresh disposable Riverhog stack.' \
		'  make tus-throughput    Measure a TUS endpoint with incomplete, deleted probes.' \
		'  make transfer-profile  Profile a supported transfer command with secret-free JSON.' \
		'  make stop-spec         Stop any in-flight local spec harness process.' \
		'  make dist              Build every Python distribution independently.' \
		'  make dist-smoke        Install and exercise the Riverhog server and client wheels.' \
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
		"  args='...'             Forward arguments to ruff, mypy, or pytest lanes." \
		"  FILES='...'            Narrow ruff and format targets to specific files." \
		"  PYTHON_PATHS='...'      Narrow the Python compile lane." \
		"  TESTS='...'            Narrow the unit test lane to specific tests." \
		"  SPEC_TESTS='...'       Narrow the spec lane to specific tests." \
		"  POSTGRES_TESTS='...'   Select disposable Postgres test files." \
		'  RELEASE_VERSION=1.0.0 Coordinated version for release-plan and release-dry-run.' \
		'  RELEASE_OUTPUT=/path   Output/evidence directory for release-evidence or release-verify.' \
		'  RELEASE_SUMMARY=/path  Write a JSON dry-run or governance summary.' \
		'  RELEASE_SIGNING_KEY=/path Offline minisign secret key for release-evidence.' \
		'  RELEASE_PUBLIC_KEY=/path Minisign public key for release-evidence or release-verify.' \
		'  TUS_URL=https://...    TUS creation URL for make tus-throughput.' \
		'  TUS throughput args require scenario, workload, and same-path raw baseline.' \
		'  TUS_BENCHMARK_USER/PASSWORD Optional benchmark Basic-auth credentials.' \
		'  MISE_BIN=/abs/path/to/mise Use a specific mise binary instead of mise on PATH.' \
		'  COMPOSE_ENV_FILE=/abs/path/to/overrides.env' \
		'  TEST_COMPOSE_PROJECT_NAME=riverhog-shared'

license:
	$(call UV_CMD,python -m reuse lint)

ruff:
	$(call UV_CMD,python -m ruff check $(FILES) $(args))

ruff-fix:
	$(call UV_CMD,python -m ruff check --fix $(FILES) $(args))

format:
	$(call UV_CMD,python -m ruff format $(FILES) $(args))

format-check:
	$(call UV_CMD,python -m ruff format --check $(FILES) $(args))

fix: ruff-fix format

mypy:
	$(call UV_CMD,python -m mypy $(MYPY_SOURCES) $(MYPY_FLAGS) $(args))

lint: license format-check ruff mypy

compile:
	$(call UV_CMD,python -m compileall -q $(PYTHON_PATHS))

unit:
	$(call UV_CMD,python -m pytest -q $(TESTS) $(args))

spec:
	$(call UV_CMD,python -m pytest -q $(SPEC_TESTS) $(args))

dependency-readiness:
	$(call UV_CMD,python scripts/check_dependency_readiness.py $(args))

provider-qualification:
	$(call UV_CMD,python scripts/provider_qualification.py $(args))

release-check:
	$(call UV_CMD,python scripts/release.py check)

release-plan:
	$(call UV_CMD,python scripts/release.py plan --version "$(RELEASE_VERSION)" $(args))

release-dry-run:
	$(call UV_CMD,python scripts/release.py dry-run --version "$(RELEASE_VERSION)" $(if $(RELEASE_SUMMARY),--summary "$(RELEASE_SUMMARY)"))

release-governance-check:
	$(call UV_CMD,python scripts/github_governance.py check $(if $(RELEASE_SUMMARY),--summary "$(RELEASE_SUMMARY)"))

release-evidence:
	@if [[ -z "$(RELEASE_OUTPUT)" || -z "$(RELEASE_SIGNING_KEY)" || -z "$(RELEASE_PUBLIC_KEY)" ]]; then \
		printf '%s\n' 'RELEASE_OUTPUT, RELEASE_SIGNING_KEY, and RELEASE_PUBLIC_KEY are required.' >&2; \
		exit 2; \
	fi
	$(call UV_CMD,python scripts/release.py evidence --version "$(RELEASE_VERSION)" --output "$(RELEASE_OUTPUT)" --signing-key "$(RELEASE_SIGNING_KEY)" --public-key "$(RELEASE_PUBLIC_KEY)")

release-verify:
	@if [[ -z "$(RELEASE_OUTPUT)" || -z "$(RELEASE_PUBLIC_KEY)" ]]; then \
		printf '%s\n' 'RELEASE_OUTPUT and RELEASE_PUBLIC_KEY are required.' >&2; \
		exit 2; \
	fi
	$(call UV_CMD,python scripts/release.py verify --directory "$(RELEASE_OUTPUT)" --public-key "$(RELEASE_PUBLIC_KEY)")

c2sp-vectors:
	@MISE_BIN="$(MISE_BIN)" ./scripts/test_c2sp_vectors.sh

postgres-concurrency:
	@POSTGRES_TESTS="$(POSTGRES_TESTS)" ./scripts/test_postgres_concurrency.sh

compose-smoke:
	@./scripts/test_compose_smoke.sh

tus-throughput:
	@if [[ -z "$(TUS_URL)" ]]; then \
		printf '%s\n' 'TUS_URL is required, for example TUS_URL=https://host/files/' >&2; \
		exit 2; \
	fi
	$(call UV_CMD,python scripts/tus_throughput.py "$(TUS_URL)" $(args))

transfer-profile:
	$(call UV_CMD,python scripts/transfer_profile.py $(args))

stop-spec:
	@./scripts/stop_spec.sh

dist:
	@if ! command -v "$(MISE_BIN)" >/dev/null 2>&1; then \
		printf '%s\n' 'Riverhog Makefile targets require mise on PATH, or MISE_BIN=/abs/path/to/mise.' >&2; \
		exit 127; \
	fi
	@"$(MISE_BIN)" x -- uv build --all-packages --clear --no-create-gitignore
	@$(UV_RUN) python scripts/check_distribution_licenses.py dist

dist-smoke: dist
	@MISE_BIN="$(MISE_BIN)" ./scripts/test_distributions.sh

build-riverhog:
	$(call BAKE_IMAGE,riverhog)

build-jeb:
	$(call BAKE_IMAGE,jeb)

build-mango-fish:
	$(call BAKE_IMAGE,mango-fish)

build-munchy-server:
	$(call BAKE_IMAGE,munchy-server)

build-munchy-av1-nvenc:
	$(call BAKE_IMAGE,munchy-av1-nvenc)

build-test:
	$(call BAKE_IMAGE,test)

build: build-riverhog build-jeb build-mango-fish build-munchy-server build-munchy-av1-nvenc build-test

bootstrap-garage:
	@./scripts/bootstrap_garage.sh

down:
	@./scripts/compose_down.sh

test: lint unit
