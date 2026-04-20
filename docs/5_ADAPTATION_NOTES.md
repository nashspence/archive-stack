# Adapted previous version (see git history for more information) donor code integration notes

## Added modules

### `src/arc_core/planner/`

These files are the planner refactor extracted from the donor planner implementation:

- `models.py` — clean dataclasses for planner-facing collections, files, pieces, and items
- `manifest.py` — manifest, sidecar, README, and manifest-budget helpers
- `split.py` — tree-aware collection splitting helpers
- `packing.py` — MILP item picker, behind an optional `planner` dependency extra
- `layout.py` — preview-only ISO layout accounting and placeholder-root sizing support

These are intentionally pure helpers. They do **not** know about SQLAlchemy, state.json, notifications,
or your final repo layout.

### `src/arc_core/iso/`

- `streaming.py` — adapted xorriso streaming helper
- `__init__.py` — exports

This is the recommended fit for `GET /v1/images/{image_id}/iso`.
It supports both:

- ad hoc mapped file entries via `IsoVolume`
- the cleaner MVP mode: stream an already materialized image root directory via `stream_iso_from_root(...)`

### `src/arc_core/imports/`

- `tar_stream.py` — generic streamed tar extraction helper
- `__init__.py`

This is **not** wired into the public API yet. Keep your canonical MVP fetch upload path as:

- `PUT /v1/fetches/{fetch_id}/files/{entry_id}`
- `POST /v1/fetches/{fetch_id}/complete`

Use `tar_stream.py` later only for an optional bulk upload endpoint such as:

- `PUT /v1/fetches/{fetch_id}/tar`

If you add that endpoint, validate tar members against the exact expected fetch manifest entries.

## Changed files

### `src/arc_api/routers/images.py`

The image ISO route now understands the new `IsoStream` dataclass, preserving:

- `Content-Disposition`
- `Cache-Control`
- async process-backed streaming

It still falls back to a plain `StreamingResponse` for existing implementations.

### `src/arc_core/services/planning.py`

The original stub remains.
A new `ImageRootPlanningService` was added for the clean MVP architecture where the planner emits an image root directory.

Expected image lookup shape:

- `image_id`
- `volume_id`
- `filename`
- `image_root`

## Dependencies

Base dependency added:

- `PyYAML`

Optional planner extra added:

- `numpy`
- `scipy`

Install with:

```bash
pip install -e .[dev,planner]
```

## Recommended next wiring step

Implement a real `PlanningService` backed by your repositories with this flow:

1. planner produces image metadata and a materialized image root directory
2. `GET /v1/images/{image_id}` returns image summary from repo
3. `GET /v1/images/{image_id}/iso` resolves the image root and returns `await stream_iso_from_root(...)`
4. registering a physical burn still stays at `POST /v1/images/{image_id}/copies`

## Deliberate omissions

The donor planner's direct filesystem state machine, SQLAlchemy sealing flow, and container import/finalization side effects were intentionally **not** copied over.
Those belong behind repos and services in this scaffold, not inside the planner helpers.
