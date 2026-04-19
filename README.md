# Riverhog

Riverhog is a small, opinionated self-hosted archive service for me. I want to safely move files out of instant-access storage and onto optical containers without losing track of them. This catalogs uploaded collections of files, automatically packs them into configured fixed-sized sets, and prepares them for ISO creation and inactive storage. Collections that have not yet been burned, along with collections explicitly kept active locally, stay available on the filesystem under their original uploaded paths, while archived collections remain visible only in the catalog.

## Testing

Use the Docker-based test path as the default way to run tests. The test image has all necessary dependencies for testing. `docker-compose.test.yml` bind-mounts the live repo into `/workspace` so targeted runs always use current source files without requiring a rebuild for every code edit.

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
