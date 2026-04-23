# Codebase layout

The implementation should keep business rules in a shared core library and keep the HTTP API and CLIs thin.

## Current layout

```text
src/
  arc_core/
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
      copies.py
      pins.py
      fetches.py
      contracts.py
    ports/
      catalog.py
      clock.py
      copy_store.py
      crypto.py
      fetch_store.py
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
    archive_artifacts.py
    crypto_age.py
    fs_paths.py
    hashing.py
    proofs.py
    sqlite_db.py
    webhooks.py
  arc_api/
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
      pins.py
    schemas/
      common.py
      collections.py
      fetches.py
      search.py
      plan.py
      images.py
      pins.py
  arc_cli/
    main.py
    client.py
    output.py
  arc_disc/
    main.py
tests/
  acceptance/
  integration/
  unit/
  fixtures/
```

## Guidance

- Keep all business rules in `arc_core`.
- Treat FastAPI and both CLIs as adapters over the same service layer.
- Keep selector parsing and normalization in one shared place.
- Keep planner helpers and donor code adaptations behind ports and services rather than wiring them directly into routers.
- Keep explanation docs aligned to the actual repository layout rather than an aspirational one.
