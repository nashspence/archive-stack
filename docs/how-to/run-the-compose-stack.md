# Run the Compose Stack

The checked-in `compose.yml` is the canonical container packaging surface for the
current server-side stack.

## Choose env values

The default values live in `./.env.compose.example`.

If you want local overrides, create `./.env.compose` first:

```bash
cp .env.compose.example .env.compose
```

The canonical `./test` script prefers `./.env.compose` when it exists and otherwise
falls back to `./.env.compose.example`.

Each `./test ...` invocation also chooses an isolated Compose project name by
default. Export `TEST_COMPOSE_PROJECT_NAME` first if you intentionally want test
runs to reuse one Compose project.

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

## Run the canonical tests

The preferred deterministic test path is still `./test`, which keeps `pytest` inside
the canonical test container while using the same compose surface for the checked-in
`app` service and its sidecars:

```bash
./test
./test prod
./test unit
```

`./test` also performs the deterministic Garage bootstrap that creates the
canonical bucket set, grants the checked-in test credentials, and verifies the
incomplete multipart lifecycle configuration before the prod lane runs.

If `ARC_GLACIER_BUCKET` differs from `ARC_S3_BUCKET`, that bootstrap applies and
verifies the same lifecycle rule on both buckets.

## Tear the stack down

Stop the compose services when you are done:

```bash
docker compose --env-file .env.compose.example down
```
