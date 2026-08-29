SHELL := bash
.DEFAULT_GOAL := help

MISE_BIN ?= mise
FILES ?= .
TESTS ?= companions packages reference riverhog tests/unit utilities
SPEC_TESTS ?= tests/harness/test_spec_harness.py
POSTGRES_TESTS ?= tests/integration/test_catalog_schema_postgres.py tests/integration/test_collection_deletion_concurrency.py tests/integration/test_collection_upload_custody_concurrency.py tests/integration/test_complete_enumeration_concurrency.py tests/integration/test_download_allowance_concurrency.py tests/integration/test_public_selector_plans_postgres.py tests/integration/test_stove0_postgres_concurrency.py
PYTHON_PATHS ?= companions packages reference riverhog scripts tests utilities
RELEASE_VERSION ?= 1.0.0
RELEASE_OUTPUT ?=
RELEASE_SUMMARY ?=
RELEASE_SIGNING_KEY ?=
RELEASE_PUBLIC_KEY ?=
UV_RUN = "$(MISE_BIN)" x -- uv run --locked --all-packages --group dev
BAKE_FILE = docker-bake.hcl
MYPY_FLAGS = --show-error-codes --hide-error-context --no-error-summary --no-color-output
MYPY_SOURCES = \
	companions/stove0/client/src \
	packages/gogurt-core/src \
	packages/gogurt-listener-runtime/src \
	reference/gogurt/mounted-volume/path-support/src \
	reference/gogurt/listener-host/linux/src \
	reference/gogurt/listener-host/macos/src \
	reference/gogurt/listener-host/windows/src \
	reference/gogurt/mounted-volume/linux/src \
	reference/gogurt/mounted-volume/macos/src \
	reference/gogurt/mounted-volume/windows/src \
	reference/stove0/observers/exiftool/src \
	reference/stove0/observers/ffprobe-sampling/src \
	reference/stove0/targets/nvenc-av1-opus/review-sampler/src \
	reference/stove0/targets/nvenc-av1-opus/target/src \
	reference/stove0/targets/opus/review-sampler/src \
	reference/stove0/targets/opus/target/src \
	reference/stove0/targets/review/materialize-target/src \
	reference/stove0/targets/review/rclone-effect-target/src \
	reference/stove0/targets/review/support/src \
	companions/stove0/server/src \
	packages/riverhog-application-access/src \
	packages/config-validation/src \
	packages/file-download/src \
	packages/http-api-contracts/src \
	packages/lifecycle-events/src \
	packages/riverhog-age/src \
	packages/riverhog-archive-contracts/src \
	reference/riverhog/ingress/ftp-api-client/src \
	packages/riverhog-api-client/src \
	packages/riverhog-cli-support/src \
	packages/riverhog-protocol/src \
	packages/riverhog-provenance-contracts/src \
	reference/riverhog/provenance/contracts/linux/src \
	reference/riverhog/provenance/observers/linux/src \
	reference/riverhog/provenance/contracts/macos/src \
	reference/riverhog/provenance/observers/macos/src \
	reference/riverhog/provenance/contracts/windows/src \
	reference/riverhog/provenance/observers/windows/src \
	packages/riverhog-transform-sdk/src \
	packages/riverhog-storage-adapter-asgi-support/src \
	packages/riverhog-storage-adapter-protocol/src \
	reference/riverhog/storage/s3-support/src \
	packages/riverhog-storage-adapter-support/src \
	packages/stove0-api-client/src \
	packages/stove0-observer-client/src \
	packages/stove0-observer-protocol/src \
	packages/stove0-observer-support/src \
	packages/stove0-operator-contracts/src \
	reference/stove0/targets/media-archive/contracts/src \
	reference/stove0/targets/media-archive/support/src \
	reference/stove0/observers/contracts/media-metadata/src \
	reference/stove0/observers/contracts/media-sampling/src \
	packages/stove0-protocol/src \
	packages/stove0-recipe-config/src \
	reference/stove0/targets/review/planning/src \
	reference/stove0/targets/review/contracts/src \
	reference/stove0/targets/review/sampler/client/src \
	reference/stove0/targets/review/sampler/protocol/src \
	reference/stove0/targets/review/sampler/support/src \
	packages/stove0-target-client/src \
	packages/stove0-target-protocol/src \
	packages/stove0-target-support/src \
	packages/riverhog-provenance/src \
	packages/state-schema/src \
	packages/time-formats/src \
	riverhog/client/src \
	reference/riverhog/ingress/ftp/src \
	riverhog/recovery/src \
	riverhog/server/src \
	reference/riverhog/storage/aws/src \
	reference/riverhog/storage/backblaze/src \
	scripts/operation_qualification.py \
	scripts/contract_freeze.py \
	scripts/provider_qualification.py \
	scripts/release.py \
	scripts/release_installation.py \
	scripts/test_mango_fish_image.py \
	scripts/qualify_installation.py \
	utilities/gogurt/src \
	utilities/mango-fish/src
args ?=

.PHONY: help license ruff ruff-fix format format-check fix mypy lint compile unit spec dependency-readiness operation-qualification database-qualification contract-freeze contract-freeze-update provider-qualification installation-qualification release-check release-plan release-dry-run release-governance-check release-evidence release-verify c2sp-vectors postgres-concurrency compose-smoke stove0-scale-qualification mango-fish-smoke transfer-profile stop-spec dist dist-smoke build build-riverhog build-riverhog-ftp-adapter build-riverhog-storage-adapter-aws build-riverhog-storage-adapter-backblaze build-stove0 build-stove0-exiftool-observer build-stove0-ffprobe-sampling-observer build-stove0-nvenc-av1-opus-target build-stove0-opus-target build-stove0-review-materialize-target build-stove0-review-rclone-effect-target build-mango-fish build-test bootstrap-garage down test

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
		'  make operation-qualification Verify or emit the generated operation matrix.' \
		'  make database-qualification Record exact-SHA database scale evidence.' \
		'  make contract-freeze   Verify the checked-in v1 boundary and external contract.' \
		'  make contract-freeze-update Regenerate that contract for semantic review.' \
		'  make provider-qualification Run the operator/provider qualification command.' \
		'  make installation-qualification Stage and qualify independent uv-tool installs.' \
		'  make release-check     Validate the coordinated release-unit contract.' \
		'  make release-plan      Print the exact-SHA v1 release inventory as JSON.' \
		'  make release-dry-run   Version and smoke-test an exact-SHA copy without publishing.' \
		'  make release-governance-check Verify live GitHub controls against release.toml.' \
		'  make release-evidence  Build signed exact-SHA evidence with external release keys.' \
		'  make release-verify    Verify a generated release evidence directory.' \
		'  make c2sp-vectors      Download and run the pinned C2SP age conformance corpus.' \
		'  make postgres-concurrency Run database concurrency tests against disposable Postgres.' \
		'  make compose-smoke     Verify disposable adapter, Riverhog, cache, and stove0 lifecycle.' \
		'  make stove0-scale-qualification Run the final-image lifecycle with a 128-file workload.' \
		'  make mango-fish-smoke  Exercise the already-built final Mango Fish image.' \
		'  make transfer-profile  Profile a supported transfer command with secret-free JSON.' \
		'  make stop-spec         Stop any in-flight local spec harness process.' \
		'  make dist              Build every Python distribution independently.' \
		'  make dist-smoke        Install and exercise the Riverhog server and client wheels.' \
		'  make build-riverhog    Build the Riverhog image.' \
		'  make build-riverhog-ftp-adapter Build the FTP adapter image.' \
		'  make build-riverhog-storage-adapter-aws Build the AWS storage adapter image.' \
		'  make build-riverhog-storage-adapter-backblaze Build the Backblaze storage adapter image.' \
		'  make build-stove0      Build the stove0 service image.' \
		'  make build-stove0-exiftool-observer Build the ExifTool observer image.' \
		'  make build-stove0-ffprobe-sampling-observer Build the FFprobe observer image.' \
		'  make build-stove0-nvenc-av1-opus-target Build the NVENC AV1 + Opus target/sampler image.' \
		'  make build-stove0-opus-target Build the slim Opus target/sampler image.' \
		'  make build-stove0-review-materialize-target Build the review materialization target image.' \
		'  make build-stove0-review-rclone-effect-target Build the rclone review-effect target image.' \
		'  make build-mango-fish  Build the Mango Fish image.' \
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
		'  STOVE0_SCALE_FILES=N  Set the scale-qualification file count (default: 128).' \
		'  STOVE0_SCALE_AUDIO_FRAMES=N Set frames per scale fixture (default: 2000).' \
		'  RELEASE_VERSION=1.0.0 Coordinated version for release-plan and release-dry-run.' \
		'  RELEASE_OUTPUT=/path   Output/evidence directory for release-evidence or release-verify.' \
		'  RELEASE_SUMMARY=/path  Write a JSON dry-run or governance summary.' \
		'  RELEASE_SIGNING_KEY=/path Offline minisign secret key for release-evidence.' \
		'  RELEASE_PUBLIC_KEY=/path Minisign public key for release-evidence or release-verify.' \
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

operation-qualification:
	$(call UV_CMD,python scripts/operation_qualification.py $(args))

database-qualification:
	@DATABASE_QUALIFICATION_OUTPUT="$(DATABASE_QUALIFICATION_OUTPUT)" \
		DATABASE_QUALIFICATION_SOURCE_SHA="$(DATABASE_QUALIFICATION_SOURCE_SHA)" \
		./scripts/test_database_qualification.sh

contract-freeze:
	$(call UV_CMD,python scripts/contract_freeze.py check)

contract-freeze-update:
	$(call UV_CMD,python scripts/contract_freeze.py update)

provider-qualification:
	$(call UV_CMD,python scripts/provider_qualification.py $(args))

installation-qualification:
	$(call UV_CMD,python scripts/qualify_installation.py --version "$(RELEASE_VERSION)" $(args))

release-check:
	$(call UV_CMD,python scripts/release.py check)

release-plan:
	$(call UV_CMD,python scripts/release.py plan --version "$(RELEASE_VERSION)" $(args))

release-dry-run:
	$(call UV_CMD,python scripts/release.py dry-run --version "$(RELEASE_VERSION)" $(if $(RELEASE_SUMMARY),--summary "$(RELEASE_SUMMARY)"))

release-governance-check:
	$(call UV_CMD,python scripts/github_governance.py check $(if $(RELEASE_GOVERNANCE_SCOPE),--scope "$(RELEASE_GOVERNANCE_SCOPE)") $(if $(RELEASE_SUMMARY),--summary "$(RELEASE_SUMMARY)"))

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

stove0-scale-qualification:
	@STOVE0_SMOKE_FILE_COUNT="$${STOVE0_SCALE_FILES:-128}" \
		STOVE0_SMOKE_AUDIO_FRAMES="$${STOVE0_SCALE_AUDIO_FRAMES:-2000}" \
		./scripts/test_compose_smoke.sh

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

build-riverhog-ftp-adapter:
	$(call BAKE_IMAGE,riverhog-ftp-adapter)

build-riverhog-storage-adapter-aws:
	$(call BAKE_IMAGE,riverhog-storage-adapter-aws)

build-riverhog-storage-adapter-backblaze:
	$(call BAKE_IMAGE,riverhog-storage-adapter-backblaze)

build-stove0:
	$(call BAKE_IMAGE,stove0)

build-stove0-exiftool-observer:
	$(call BAKE_IMAGE,stove0-exiftool-observer)

build-stove0-ffprobe-sampling-observer:
	$(call BAKE_IMAGE,stove0-ffprobe-sampling-observer)

build-stove0-nvenc-av1-opus-target:
	$(call BAKE_IMAGE,stove0-nvenc-av1-opus-target)

build-stove0-opus-target:
	$(call BAKE_IMAGE,stove0-opus-target)

build-stove0-review-materialize-target:
	$(call BAKE_IMAGE,stove0-review-materialize-target)

build-stove0-review-rclone-effect-target:
	$(call BAKE_IMAGE,stove0-review-rclone-effect-target)

build-mango-fish:
	$(call BAKE_IMAGE,mango-fish)

mango-fish-smoke:
	@if ! command -v "$(MISE_BIN)" >/dev/null 2>&1; then \
		printf '%s\n' 'Mango Fish image smoke requires mise on PATH, or MISE_BIN=/abs/path/to/mise.' >&2; \
		exit 127; \
	fi
	@"$(MISE_BIN)" x python -- python scripts/test_mango_fish_image.py

build-test:
	$(call BAKE_IMAGE,test)

build: build-riverhog build-riverhog-ftp-adapter build-riverhog-storage-adapter-aws build-riverhog-storage-adapter-backblaze build-stove0 build-stove0-exiftool-observer build-stove0-ffprobe-sampling-observer build-stove0-nvenc-av1-opus-target build-stove0-opus-target build-stove0-review-materialize-target build-stove0-review-rclone-effect-target build-mango-fish build-test

bootstrap-garage:
	@./scripts/bootstrap_garage.sh

down:
	@./scripts/compose_down.sh

test: lint unit
