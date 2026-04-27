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

## Start the API and SeaweedFS

Build and run the active stack:

```bash
docker compose --env-file .env.compose.example up --build app seaweedfs
```

The default example env exposes:

- the API at `http://127.0.0.1:8000`
- the SeaweedFS filer at `http://127.0.0.1:8888`

## Run the canonical tests

The preferred deterministic test path is still `./test`, which keeps `pytest` inside
the canonical test container while using the same compose surface for the checked-in
`app` service and its sidecars:

```bash
./test
./test acceptance
./test unit
```

## Tear the stack down

Stop the compose services when you are done:

```bash
docker compose --env-file .env.compose.example down
```
