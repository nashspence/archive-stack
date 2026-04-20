# Codebase layout

The implementation should keep business rules in a shared core library and keep the HTTP API and CLIs thin.

## Recommended layout

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
    ports/
      catalog.py
      planner.py
      hot_store.py
      projection.py
      fetch_store.py
      copy_store.py
      optical_reader.py
      crypto.py
      clock.py
      ids.py
  arc_api/
    app.py
    deps.py
    routers/
      collections.py
      search.py
      plan.py
      images.py
      pins.py
      fetches.py
    schemas/
      common.py
      collections.py
      search.py
      plan.py
      images.py
      pins.py
      fetches.py
  arc_cli/
    main.py
    client.py
    commands/
      close.py
      find.py
      show.py
      plan.py
      iso.py
      copy_add.py
      pin.py
      release.py
      pins.py
      fetch.py
  arc_disc/
    main.py
    client.py
    fetch.py
tests/
  acceptance/
  unit/
  fixtures/
```

## Guidance

- Keep all business rules in `arc_core`.
- Treat FastAPI and both CLIs as adapters over the same service layer.
- Keep selector parsing and normalization in one shared place.
- Keep planner helpers and donor code adaptations behind ports and services rather than wiring them directly into routers.
