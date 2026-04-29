# Run the Compose Stack

The checked-in `compose.yml` is the canonical container packaging surface for the
current server-side stack.

## Choose env values

The default values live in `./.env.compose.example`.

If you want local overrides, create `./.env.compose` first:

```bash
cp .env.compose.example .env.compose
```

The checked-in test scripts prefer `./.env.compose` when it exists and otherwise
fall back to `./.env.compose.example`.

The checked-in example env now keeps product-facing Glacier recovery timing
defaults. The short recovery timing values that keep the prod-backed acceptance
lane fast live only in `tests/harness/prod-harness.env`.

Each prod-backed `make ...` invocation also chooses an isolated Compose
project name by default. Export `TEST_COMPOSE_PROJECT_NAME` first if you
intentionally want prod-backed runs to reuse one Compose project.

## Start the stack

Build and run the active stack:

```bash
docker compose --env-file .env.compose.example up --build
```

The default example env exposes:

- the API at `http://127.0.0.1:8000`
- the read-only WebDAV surface at `http://127.0.0.1:8080`

The checked-in harness uses Garage for S3-compatible committed storage, `tusd`
for resumable staging uploads, and `rclone serve webdav --read-only` for
day-to-day browsing.

## Run the checked-in tests

For the fastest full check, run these in separate terminals:

```bash
make lint
make unit
make spec
make prod
```

`make lint` is the canonical pre-test quality gate. It runs `ruff check .` and
then runs strict `mypy` in that same locked local environment.

`make prod` performs the deterministic Garage bootstrap that creates the
canonical bucket set, grants the checked-in test credentials, and verifies the
incomplete multipart lifecycle configuration before the prod-backed lane runs.

Run `make build-app`, `make build-test`, or `make build` when you want fresh
local container images before the prod-backed lane.

Run `make bootstrap-garage` when you want the checked-in Garage bootstrap on
its own. Export `TEST_COMPOSE_PROJECT_NAME` first if you also want `make down`
to tear that same standalone stack back down later.

Run `make test` when you want the supported serial aggregate target. It runs
lint, then unit, spec, and the prod-backed acceptance phase in order.

When `make prod`, `make prod-profile`, or `make test` starts the prod-backed
lane, it layers the short recovery timing values from
`tests/harness/prod-harness.env` over the shared compose env so local compose
runs stay aligned with product-facing defaults.

If `ARC_GLACIER_BUCKET` differs from `ARC_S3_BUCKET`, that bootstrap applies and
verifies the same lifecycle rule on both buckets.

## Tear the stack down

Stop the compose services when you are done:

```bash
make down
```
