# Run Acceptance Tests

The executable acceptance contract lives in the Gherkin feature files under
`tests/acceptance/features`.

## Preferred commands

Run the last full check in separate terminals:

```bash
make lint
make unit
make spec
make prod
```

That keeps the local lanes and the prod-backed lane visible in parallel output
windows without changing the checked-in test surfaces.

Run the same acceptance contract inside the deterministic test container:

```bash
make prod
```

That path keeps the prod-backed `pytest` run in the canonical test container
while `docker compose` manages the checked-in `app` service and its storage
sidecars outside the container.

Do not run the production-backed harness with direct `pytest`. The supported
entrypoints are `make prod`, `make prod-profile`, or `make test`, which prepare
the compose-managed app and sidecars the harness expects.

Run the production-backed harness lane with built-in timing output for scenario and fixture hotspots:

```bash
make prod-profile
```

Run the fixture-backed spec harness lane against the same contract from a local
locked `uv` environment:

```bash
make spec
```

Run the unit lane by itself:

```bash
make unit
```

Forward pytest selectors or other pytest arguments with `args`.

```bash
make prod args='-k server_rejects_incorrect_recovered_bytes'
make spec args='-k server_rejects_incorrect_recovered_bytes'
```

Run the atomic image build targets when you need a fresh local app or test
image before the prod-backed lane:

```bash
make build-app
make build-test
```

Run the Garage bootstrap on its own when you want the checked-in buckets and
keys prepared without running the prod harness yet:

```bash
make bootstrap-garage
```

The non-production lanes resolve against the checked-in `requirements-test.txt`
set plus the editable project, so they do not require Docker or BuildKit. That
lock includes the `db` extra used by the prod-backed harness.

Run the serial aggregate target with `make test` when you want one supported
command to run lint, then unit, spec, and prod in order.

## Gated optical-device validation

`make gated-arc-disc` is an opt-in lane for real `arc-disc` optical I/O. It is
not part of `make test`, `make spec`, or `make prod`, because it can require
operator-provided media, device permissions, and destructive writes to optical
media.

This lane validates the optical service boundary. The full workflow checks stand
up fixture-backed API, storage, session, and restore state inside the test
process; the real ci-unsuitable service is the optical device. The lane fails if
any `ARC_DISC_*_FACTORY` override is configured.

Read-only mounted-media validation requires a mounted disc or mounted ISO
filesystem containing one known recovery payload object:

```bash
export ARC_DISC_GATED_MOUNT_PATH=/media/archive-disc
export ARC_DISC_GATED_PAYLOAD_PATH=disc/000001.bin
export ARC_DISC_GATED_EXPECTED_SHA256=<sha256-of-disc/000001.bin>
make gated-arc-disc
```

Read-only raw-device validation uses `xorriso` to extract the same object from
an inserted optical disc. It can share the payload path and expected digest from
the mounted-media variables, or use raw-device-specific overrides:

```bash
export ARC_DISC_GATED_RAW_DEVICE=/dev/sr0
export ARC_DISC_GATED_RAW_PAYLOAD_PATH=disc/000001.bin
export ARC_DISC_GATED_RAW_EXPECTED_SHA256=<sha256-of-disc/000001.bin>
make gated-arc-disc
```

Destructive burn validation is skipped unless the confirmation variable is set
exactly. Use blank writable media. The test generates a disposable ISO, verifies
it, burns it, then reads the ISO-sized byte range back from the same device and
compares it to the staged ISO.

```bash
export ARC_DISC_GATED_BURN_DEVICE=/dev/sr0
export ARC_DISC_GATED_BURN_COPY_ID=gated-arc-disc-copy
export ARC_DISC_GATED_BURN_CONFIRM=write-optical-media
make gated-arc-disc
```

Full CLI/API workflow validation is also part of `make gated-arc-disc`. These
tests create the API state themselves, generate real ISO bytes from fixture image
roots, run `arc-disc` as a subprocess, burn one real disc, verify it by reading
the device, and then read that disc back through `arc-disc fetch`. The recovery
workflow similarly uses a fixture-backed restored ISO endpoint and burns one real
replacement disc.

The full workflows use `ARC_DISC_GATED_BURN_DEVICE` for both writing and reading
and skip until the destructive confirmation is set. No `ARC_BASE_URL`, fetch id,
session id, staging directory, prompt input, or factory override is needed.

```bash
export ARC_DISC_GATED_BURN_DEVICE=/dev/sr0
export ARC_DISC_GATED_BURN_CONFIRM=write-optical-media
make gated-arc-disc args='-k full'
```

When a capability is not configured, the corresponding gated test skips with the
missing env var or device requirement. Once a capability is explicitly
configured, command failures are treated as validation failures so drive,
permission, media, and product regressions stay visible.

## Gated Glacier-restore validation

`make gated-glacier-restore` is an opt-in lane for live AWS S3 Glacier restore
behavior. The lane keeps Riverhog's API, catalog, recovery session, and fixture
state fake-backed; AWS S3 Glacier restore is the only real service. It is not
part of `make test`, `make spec`, or `make prod`, because it can issue real
restore requests, depends on account permissions and object storage class, and
may take hours before a restored object is readable.

Restore validation requires the normal `ARC_GLACIER_*` archive backend
configuration and explicit restore confirmation. The lane creates its own
fixture-backed finalized image, uploads that ISO to the configured AWS archive
bucket/storage class, records the uploaded object's checksum metadata, and uses
that object for restore request, polling, download, and completion checks:

```bash
export ARC_GLACIER_BACKEND=aws
export ARC_GLACIER_GATED_RESTORE_CONFIRM=request-glacier-restore
make gated-glacier-restore
```

If AWS reports the uploaded object is not readable yet, the restore request tests
still pass and the download/completion checks skip with a rerun message. Rerun
the same command after AWS completes the restore; no object path or SHA override
is required.

## Compose-backed sidecars

The checked-in test scripts read `./.env.compose` when present, otherwise they fall
back to `./.env.compose.example`.

For prod-backed lanes, `make prod`, `make prod-profile`, and `make test` also
load the short recovery timing overrides
from `tests/harness/prod-harness.env`. That keeps the checked-in compose env
product-facing while still giving the acceptance harness the smaller timing
window it needs.

Each prod-backed `make ...` invocation uses its own Compose project name by
default so independent prod-backed runs do not tear down each other's one-off
containers, networks, or sidecars. The prod-backed entrypoints also publish the
API and WebDAV services on Docker-assigned ephemeral host ports because the
harness reaches them through the Compose network at `http://app:8000` and
`http://webdav:8080`. That keeps overlapping prod-backed runs independent of
whether host ports `8000` and `8080` are already occupied. Harness SQLite state
files, webhook captures, and acceptance workspaces live under
`/app/.compose/<compose-project>/` so the shared source bind mount does not make
concurrent prod-backed runs share catalog files or fixture trees.
Successful isolated prod-backed runs remove that generated project directory
after Compose teardown. Runs that explicitly set `TEST_COMPOSE_PROJECT_NAME`
preserve the directory because they are intentionally reusing one stack. There
is no supported override for this state root; choose the Compose project name to
control isolation or reuse.

If you need to reuse one Compose project explicitly, export
`TEST_COMPOSE_PROJECT_NAME` before running `make`.
Do that before `make bootstrap-garage` or `make down` as well when you want
those standalone targets to act on the same compose-managed stack.

The prod-backed app image installs hashed runtime dependencies from
`requirements-runtime.txt` before source files are copied. The test image
installs hashed test dependencies from `requirements-test.txt`, then copies
`pyproject.toml`, `src/`, `contracts/`, and `tests/`. README edits,
documentation edits, source edits, and prod-harness state under `.compose/` do
not invalidate the dependency-install layers.

Regenerate the runtime lock with:

```bash
uv pip compile pyproject.toml --extra db --python-version 3.11 --generate-hashes -o requirements-runtime.txt
```

Regenerate the test lock with:

```bash
uv pip compile pyproject.toml --extra dev --extra planner --extra db --python-version 3.11 --generate-hashes -o requirements-test.txt
```


## What lives where

- `tests/acceptance/features/` contains the normative external scenarios.
- `tests/harness/test_prod_harness.py` loads those features against the real production app and CLIs.
- `tests/harness/test_spec_harness.py` loads the same feature files against the fixture-backed spec harness.
- `contracts/disc/` holds the machine-readable ISO layout and YAML schema contracts that the acceptance scenarios verify directly.
- `tests/fixtures/bdd_steps.py` holds the shared step definitions used by both lanes.

## Readiness markers

- `@xfail_contract` means the fixture-backed spec harness executes the scenario, but the prod harness is still behind the contract.
- `@xfail_not_backed` means the Gherkin contract exists before the prod harness fully backs that scenario.
- `@xfail_not_backed` XPASSes are strict and fail the run so incomplete-backing markers get cleaned up promptly when the harness catches up.
- `@xfail_contract` is strict in the prod harness and ignored in the spec harness.
