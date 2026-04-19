# Riverhog

Riverhog is a small, opinionated self-hosted archive service for me. I want to safely move files out of instant-access storage and onto optical containers. This api catalogs uploaded collections of files, automatically packs them into configured fixed-sized sets, and prepares on-demand ISOs for them. Collections that have not yet been burned, along with collections explicitly wanted active as well as backed-up, stay available on the filesystem under their original uploaded paths, while archived collections remain visible only in the catalog. The UI now points you at the relevant filesystem paths instead of serving individual collection files or hash-proof bundles directly.

## Runtime Notes

Configure ownership for bind-mounted data with `PREFERRED_UID` and `PREFERRED_GID` in `.env`. The API container bootstraps `/var/lib/archive` and `/var/lib/uploads`, fixes ownership recursively, applies group-shared directory permissions, sets a writable runtime home under `/var/lib/archive/runtime-home`, and then drops privileges to that UID/GID before starting the service.

If you want container-finalization notifications, set `CONTAINER_FINALIZATION_WEBHOOK_URL` in `.env`. Reminder behavior is configured with `CONTAINER_FINALIZATION_REMINDER_INTERVAL_SECONDS`; there is no UI or API endpoint for managing webhook subscriptions anymore.

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
./scripts/run-tests-in-dind.sh tests/test_ui_playwright.py -k collection_seal_and_flush_flow
```
