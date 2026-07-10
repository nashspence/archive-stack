# Run the Compose Stack

The checked-in `compose.yml` is the canonical local container packaging surface
for the current server-side stack.

## Choose Env Values

The default values live in `./.env.compose.example`.

If you want local overrides, create `./.env.compose` first:

```bash
cp .env.compose.example .env.compose
```

The checked-in scripts prefer `./.env.compose` when it exists and otherwise fall
back to `./.env.compose.example`.

The checked-in recovery payload passphrase is development/test only. Production
Compose deployments should set
`RIVERHOG_RECOVERY_PAYLOAD_REQUIRE_EXPLICIT_PASSPHRASE=true` and provide
`RIVERHOG_RECOVERY_PAYLOAD_PASSPHRASE` from deployment secrets.

## Start the Stack

Build and run the active stack:

```bash
docker compose --env-file .env.compose.example up --build
```

The default example env exposes:

- the API at `http://127.0.0.1:8000`
- the read-only WebDAV surface at `http://127.0.0.1:8080`

The checked-in stack uses Garage for S3-compatible committed storage, Postgres
for authoritative catalog state, `tusd` for resumable staging uploads, and
`rclone serve webdav --read-only` for day-to-day browsing.

The `tusd` and `app` services share the `upload-staging` volume at `/uploads`.
That volume is temporary ingest custody: upload chunks land there first for
maximum local write throughput, then `riverhog-app` streams the verified staged
files into Glacier-compatible archive storage and committed Garage hot storage.

## Run the Checked-In Tests

For the normal full check:

```bash
make lint
make unit
```

`make lint` is the canonical pre-test quality gate. It runs repo-wide
`ruff check .` and then strict `mypy` over `src` and the deployed service app
entrypoints in the same locked local environment.

Run `make build-app`, `make build-test`, or `make build` when you want fresh
local container images.

Run `make bootstrap-garage` when you want the checked-in Garage bootstrap on its
own. Export `TEST_COMPOSE_PROJECT_NAME` first if you also want `make down` to
tear that same standalone stack back down later.

Run `make test` when you want the supported serial aggregate target. It runs
lint, then unit.

Run `make spec` separately when you are working on the fixture-backed
acceptance contract.

If you need to edit code or contract surface while the spec harness is running,
stop it first and restart it after the edit. Do not keep editing during a
canonical run and treat its eventual result as valid.

```bash
make stop-spec
```

The checked-in runtime and service Dockerfiles install hashed dependencies from
`requirements-runtime.txt` and `requirements-service.txt` before copying
application source. The test Dockerfile syncs its environment from `uv.lock`
before copying source. README and documentation edits do not invalidate those
dependency-install layers. When dependency constraints change, update `uv.lock`
first, then regenerate the runtime and service requirements exports from that
lockfile. The unit suite checks that shared exported packages do not drift and
that generated requirements keep hash-pinned entries.

If `RIVERHOG_GLACIER_BUCKET` differs from `RIVERHOG_S3_BUCKET`, the Garage
bootstrap applies and verifies the same lifecycle rule on both buckets.

## Tear the Stack Down

Stop the compose services when you are done:

```bash
make down
```
