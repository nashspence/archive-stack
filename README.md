# Riverhog

Riverhog is a small, heavily opinionated self-hosted archive service for households, home labs, and small groups that want to move files out of instant-access storage and onto optical containers without losing track of them. It catalogs uploads, automatically packs them into configured container-sized archive sets, and prepares them for ISO creation and inactive storage. Files that have not yet been burned, along with files explicitly kept active locally, stay available on the filesystem under their original uploaded paths, while archived files remain visible in the catalog even when the container holding their data is inactive. It is built for this specific workflow, not as a general-purpose storage platform or polished backup product.

The repo now also includes a separate `ui` service: a deliberately minimal web UI that talks to the Riverhog API over HTTP and keeps the API token on the server side. In the default compose setup it is exposed on `http://localhost:8090`, while the API remains on `http://localhost:8080` and `tusd` remains on `http://localhost:1080`. The compose file also overrides the API container's internal `TUSD_BASE_URL` to `http://tusd:1080/files`, so copying `.env.example` works unchanged for both host-facing URLs and container-to-container traffic. The `tusd` service is configured with image-entrypoint-style arguments, so it really binds port `1080` inside the compose network instead of silently falling back to its default `8080`, and it runs as `root` in the example compose stack so it can write to Docker-created bind mounts under `./data/archive`.

## Testing

Use the Docker-based test path as the default way to run tests. The test image installs all dependencies from `Dockerfile.test`, and `docker-compose.test.yml` bind-mounts the live repo into `/workspace` so targeted runs always use current source files without requiring a rebuild for every code edit.

Run the full suite:

```bash
./scripts/run-tests-in-dind.sh
```

Run a targeted file:

```bash
./scripts/run-tests-in-dind.sh tests/test_ui_smoke.py
```

Run a single test or filtered subset:

```bash
./scripts/run-tests-in-dind.sh tests/test_ui_playwright.py -k collection_uploads_run_in_parallel
```
