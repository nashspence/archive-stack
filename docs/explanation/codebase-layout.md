# Codebase layout

The implementation should keep business rules in a shared core library and keep the HTTP API and CLIs thin.

## Current layout

```text
src/
  riverhog_core/
    domain/
      enums.py
      types.py
      errors.py
      selectors.py
      models.py
    services/
      collections.py
      search.py
      planning.py
      discs.py
      fetches.py
      contracts.py
    ports/
      catalog.py
      clock.py
      disc_store.py
      crypto.py
      hot_store.py
      ids.py
      optical_reader.py
      planner.py
      projection.py
    planner/
      layout.py
      manifest.py
      models.py
      packing.py
      split.py
    iso/
      streaming.py
    imports/
      tar_stream.py
    crypto_age.py
    fs_paths.py
    hashing.py
    proofs.py
    catalog_db.py
    webhooks.py
  riverhog_api/
    app.py
    auth.py
    deps.py
    mappers.py
    routers/
      collections.py
      fetches.py
      search.py
      plan.py
      images.py
    schemas/
      common.py
      collections.py
      fetches.py
      search.py
      plan.py
      images.py
  riverhog_cli/
    main.py
    client.py
    output.py
  munchy/
    ingest.py
    local_files.py
    preflight.py
    profiles.py
    runner_client.py
    source_artifacts.py
  munchy_cli/
    main.py
  jeb/
    collector.py
    cli.py
  djdan/
    main.py
services/
  ftpd/
  jeb/
  munchy-runner/
  munchy-av1-nvenc/
tests/
  acceptance/
  harness/
  unit/
  fixtures/
```

## Guidance

- Keep all business rules in `riverhog_core`.
- Treat FastAPI and both CLIs as adapters over the same service layer.
- Keep selector parsing and normalization in one shared place.
- Keep planner helpers and donor code adaptations behind ports and services rather than wiring them directly into routers.
- Keep explanation docs aligned to the actual repository layout rather than an aspirational one.
- Keep Munchy generic: tests and examples should use role-based names, not private devices,
  hostnames, remotes, or deployment overlays.
- Keep Jeb generic: watched-directory source configs can describe roles and target types, but real
  FTP users, hostnames, webhook URLs, and private source mappings belong outside this repository.
- Keep the Jeb/Munchy boundary narrow: Jeb can use Munchy preflight as a go/no-go
  gate, then upload the complete eligible batch and delete that source batch only
  after safe target success. Munchy owns routing, profile selection, metadata
  projection, and leave/cull/archive decisions.
