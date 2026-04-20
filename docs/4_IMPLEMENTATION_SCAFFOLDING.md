# Decisions

1. Collection id is the final path component of the closed staging directory.
   Example: `/srv/archive/staging/photos-2024` -> `photos-2024`

2. Re-closing the same path fails with `conflict`.

3. Repeated `/`, `.` and `..` in targets are rejected with `invalid_target`.

4. Search is case-insensitive substring match over:

   * collection id
   * full logical file path

5. `POST /pin` is exact-target idempotent.
   Calling it twice for the same canonical target produces one pin.

6. `POST /release` removes only the exact canonical target pin.

7. Fetch reuse rule:
   if there is an existing non-`failed`, non-`done` fetch for the same exact target, return that fetch instead of creating a new one.

8. `archived_bytes` means bytes covered by at least one registered copy.

9. After `close`, the whole collection is hot even if no pin exists yet.

10. Hot reconciliation after release may be asynchronous internally, but tests should assert eventual externally visible state through a helper like `wait_until_hot_matches_pins()`.

# Minimal code layout

Use one shared library plus two entrypoints.

```text id="d4zwx1"
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

Keep all business rules in `arc_core`.
Keep FastAPI and CLI very thin.

# Domain types

Use these exact core types first.

```python id="t4yvn3"
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import PurePosixPath
from typing import NewType

CollectionId = NewType("CollectionId", str)
ImageId = NewType("ImageId", str)
CopyId = NewType("CopyId", str)
FetchId = NewType("FetchId", str)
EntryId = NewType("EntryId", str)
TargetStr = NewType("TargetStr", str)
Sha256Hex = NewType("Sha256Hex", str)
```

```python id="6v7mpt"
class FetchState(str, Enum):
    WAITING_MEDIA = "waiting_media"
    UPLOADING = "uploading"
    VERIFYING = "verifying"
    DONE = "done"
    FAILED = "failed"
```

```python id="1cfno1"
class SearchKind(str, Enum):
    COLLECTION = "collection"
    FILE = "file"
```

```python id="szj8kt"
@dataclass(frozen=True)
class Target:
    collection_id: CollectionId
    path: PurePosixPath | None
    is_dir: bool

    @property
    def is_collection(self) -> bool:
        return self.path is None

    @property
    def canonical(self) -> str:
        if self.path is None:
            return str(self.collection_id)
        suffix = str(self.path)
        if self.is_dir and not suffix.endswith("/"):
            suffix += "/"
        return f"{self.collection_id}:{suffix}"
```

```python id="uyehfp"
@dataclass(frozen=True)
class CollectionSummary:
    id: CollectionId
    files: int
    bytes: int
    hot_bytes: int
    archived_bytes: int

    @property
    def pending_bytes(self) -> int:
        return self.bytes - self.archived_bytes
```

```python id="f9hjkw"
@dataclass(frozen=True)
class ImageSummary:
    id: ImageId
    bytes: int
    fill: float
    files: int
    collections: int
    iso_ready: bool
```

```python id="6rdo2n"
@dataclass(frozen=True)
class CopySummary:
    id: CopyId
    image: ImageId
    location: str
    created_at: str
```

```python id="vmbm8u"
@dataclass(frozen=True)
class FetchCopyHint:
    id: CopyId
    location: str
```

```python id="zvh2hu"
@dataclass(frozen=True)
class FetchSummary:
    id: FetchId
    target: TargetStr
    state: FetchState
    files: int
    bytes: int
    copies: list[FetchCopyHint]
```

```python id="7cck6j"
@dataclass(frozen=True)
class PinSummary:
    target: TargetStr
```

# Selector parser

Implement this first. Everything depends on it.

```python id="x3k3ds"
from pathlib import PurePosixPath
import re

_TARGET_COLLECTION_RE = re.compile(r"^[^:/][^:]*$")
_TARGET_WITH_PATH_RE = re.compile(r"^(?P<collection>[^:/][^:]*):(?P<path>/.*)$")

class InvalidTarget(ValueError):
    pass

def parse_target(raw: str) -> Target:
    m = _TARGET_WITH_PATH_RE.match(raw)
    if m:
        collection = CollectionId(m.group("collection"))
        raw_path = m.group("path")
        if raw_path in {"/", ""}:
            raise InvalidTarget("empty path")
        if "//" in raw_path:
            raise InvalidTarget("repeated slash")
        is_dir = raw_path.endswith("/")
        check = raw_path[:-1] if is_dir else raw_path
        path = PurePosixPath(check)
        if str(path) != check:
            raise InvalidTarget("non-canonical path")
        if any(part in {".", ".."} for part in path.parts):
            raise InvalidTarget("dot segments not allowed")
        return Target(collection_id=collection, path=path, is_dir=is_dir)

    if _TARGET_COLLECTION_RE.match(raw):
        return Target(collection_id=CollectionId(raw), path=None, is_dir=False)

    raise InvalidTarget("invalid target syntax")
```

# FastAPI schemas

Keep the API schema layer separate from domain models.

```python id="0v2udr"
from pydantic import BaseModel, Field

class ErrorBody(BaseModel):
    code: str
    message: str

class ErrorResponse(BaseModel):
    error: ErrorBody
```

```python id="7g3s44"
class CloseCollectionRequest(BaseModel):
    path: str

class CollectionSummaryOut(BaseModel):
    id: str
    files: int
    bytes: int
    hot_bytes: int
    archived_bytes: int
    pending_bytes: int

class CloseCollectionResponse(BaseModel):
    collection: CollectionSummaryOut
```

```python id="qd5ud2"
class SearchCopyOut(BaseModel):
    id: str
    location: str

class SearchResultOut(BaseModel):
    kind: str
    target: str
    collection: str
    path: str | None = None
    bytes: int | None = None
    hot: bool | None = None
    files: int | None = None
    hot_bytes: int | None = None
    archived_bytes: int | None = None
    pending_bytes: int | None = None
    copies: list[SearchCopyOut] = Field(default_factory=list)

class SearchResponse(BaseModel):
    query: str
    results: list[SearchResultOut]
```

```python id="h2nozz"
class PlanImageOut(BaseModel):
    id: str
    bytes: int
    fill: float
    collections: int
    files: int
    iso_ready: bool

class PlanResponse(BaseModel):
    ready: bool
    target_bytes: int
    min_fill_bytes: int
    images: list[PlanImageOut]
    unplanned_bytes: int
    note: str | None = None
```

```python id="x5t4q4"
class RegisterCopyRequest(BaseModel):
    id: str
    location: str

class CopyOut(BaseModel):
    id: str
    image: str
    location: str
    created_at: str

class RegisterCopyResponse(BaseModel):
    copy: CopyOut
```

```python id="k7g4eo"
class PinRequest(BaseModel):
    target: str

class HotStatusOut(BaseModel):
    state: str
    present_bytes: int
    missing_bytes: int

class FetchHintCopyOut(BaseModel):
    id: str
    location: str

class FetchHintOut(BaseModel):
    id: str
    state: str
    copies: list[FetchHintCopyOut]

class PinResponse(BaseModel):
    target: str
    pin: bool
    hot: HotStatusOut
    fetch: FetchHintOut | None
```

```python id="mpz0iz"
class ReleaseRequest(BaseModel):
    target: str

class ReleaseResponse(BaseModel):
    target: str
    pin: bool
```

```python id="1z6x3u"
class PinsResponse(BaseModel):
    pins: list[PinSummary]
```

```python id="5wwksr"
class FetchSummaryOut(BaseModel):
    id: str
    target: str
    state: str
    files: int
    bytes: int
    copies: list[FetchHintCopyOut]

class FetchManifestCopyOut(BaseModel):
    copy: str
    location: str
    disc_path: str
    enc: dict

class FetchManifestEntryOut(BaseModel):
    id: str
    path: str
    bytes: int
    sha256: str
    copies: list[FetchManifestCopyOut]

class FetchManifestResponse(BaseModel):
    id: str
    target: str
    entries: list[FetchManifestEntryOut]

class UploadEntryResponse(BaseModel):
    entry: str
    accepted: bool
    bytes: int

class CompleteFetchResponse(BaseModel):
    id: str
    state: str
    hot: HotStatusOut
```

# Router layer

Each route should be a thin adapter:

```python id="9s9n7r"
@router.post("/collections/close", response_model=CloseCollectionResponse)
def close_collection(req: CloseCollectionRequest, svc: CollectionService = Depends(...)):
    summary = svc.close(req.path)
    return CloseCollectionResponse(collection=map_collection(summary))
```

Repeat this pattern for every route.
No route should contain business logic.

# Service interfaces

These are the use cases that should exist before implementation details.

```python id="fwbt3n"
class CollectionService:
    def close(self, staging_path: str) -> CollectionSummary: ...
```

```python id="i49b12"
class SearchService:
    def search(self, query: str, limit: int) -> list[object]: ...
```

```python id="1g01hx"
class PlanningService:
    def get_plan(self) -> object: ...
    def get_image(self, image_id: str) -> ImageSummary: ...
    def get_iso_stream(self, image_id: str): ...
```

```python id="wvl76i"
class CopyService:
    def register(self, image_id: str, copy_id: str, location: str) -> CopySummary: ...
```

```python id="c44uc1"
class PinService:
    def pin(self, raw_target: str) -> object: ...
    def release(self, raw_target: str) -> object: ...
    def list_pins(self) -> list[PinSummary]: ...
```

```python id="6x1te2"
class FetchService:
    def get(self, fetch_id: str) -> FetchSummary: ...
    def manifest(self, fetch_id: str) -> object: ...
    def upload_entry(self, fetch_id: str, entry_id: str, sha256: str, content: bytes) -> object: ...
    def complete(self, fetch_id: str) -> object: ...
```

# Ports

These are the boundaries that keep the core clean.

```python id="sy6q1i"
class CatalogRepo:
    def collection_exists(self, collection_id: CollectionId) -> bool: ...
    def create_collection_from_scan(self, collection_id: CollectionId, staging_path: str) -> CollectionSummary: ...
    def get_collection_summary(self, collection_id: CollectionId) -> CollectionSummary: ...
    def search(self, query: str, limit: int) -> list[object]: ...
    def resolve_target_files(self, target: Target) -> list[object]: ...
    def list_pins(self) -> list[PinSummary]: ...
    def has_exact_pin(self, target: TargetStr) -> bool: ...
    def add_pin(self, target: TargetStr) -> None: ...
    def remove_pin(self, target: TargetStr) -> None: ...
```

```python id="807r5w"
class PlannerPort:
    def include_collection(self, collection_id: CollectionId) -> None: ...
    def get_plan(self) -> object: ...
    def get_image(self, image_id: ImageId) -> ImageSummary: ...
    def open_iso_stream(self, image_id: ImageId): ...
    def image_file_coverage(self, image_id: ImageId) -> list[tuple[CollectionId, str]]: ...
```

```python id="vhqdqh"
class CopyStore:
    def create_copy(self, image_id: ImageId, copy_id: CopyId, location: str) -> CopySummary: ...
    def file_copies(self, collection_id: CollectionId, path: str) -> list[FetchCopyHint]: ...
```

```python id="hf8o3r"
class HotStore:
    def materialize_closed_collection(self, collection_id: CollectionId) -> None: ...
    def has_file(self, collection_id: CollectionId, path: str) -> bool: ...
    def put_file(self, sha256: Sha256Hex, content: bytes) -> None: ...
    def hot_bytes_for_collection(self, collection_id: CollectionId) -> int: ...
```

```python id="rew86w"
class ProjectionStore:
    def reconcile_from_pins(self) -> None: ...
    def ensure_target_visible(self, target: Target) -> None: ...
```

```python id="lzld6f"
class FetchStore:
    def find_reusable_fetch(self, target: TargetStr) -> FetchSummary | None: ...
    def create_fetch(self, target: TargetStr, entries: list[object], copies: list[FetchCopyHint]) -> FetchSummary: ...
    def get_fetch(self, fetch_id: FetchId) -> FetchSummary: ...
    def get_manifest(self, fetch_id: FetchId) -> object: ...
    def accept_uploaded_entry(self, fetch_id: FetchId, entry_id: EntryId, sha256: str, content: bytes) -> object: ...
    def can_complete(self, fetch_id: FetchId) -> bool: ...
    def mark_verifying(self, fetch_id: FetchId) -> None: ...
    def mark_done(self, fetch_id: FetchId) -> None: ...
    def mark_failed(self, fetch_id: FetchId, reason: str) -> None: ...
```

# Service behavior sketches

These are the only places where behavior should live.

## `CollectionService.close(path)`

```python id="w1s9zh"
def close(self, staging_path: str) -> CollectionSummary:
    collection_id = CollectionId(Path(staging_path).name)
    if self.catalog.collection_exists(collection_id):
        raise Conflict("collection already exists")
    summary = self.catalog.create_collection_from_scan(collection_id, staging_path)
    self.hot.materialize_closed_collection(collection_id)
    self.planner.include_collection(collection_id)
    return self.catalog.get_collection_summary(collection_id)
```

## `PinService.pin(target)`

```python id="z5q87q"
def pin(self, raw_target: str):
    target = parse_target(raw_target)
    canonical = TargetStr(target.canonical)

    if not self.catalog.has_exact_pin(canonical):
        self.catalog.add_pin(canonical)

    files = self.catalog.resolve_target_files(target)
    if not files:
        raise NotFound("target resolves to no files")

    present = sum(f.bytes for f in files if self.hot.has_file(f.collection_id, f.path))
    total = sum(f.bytes for f in files)
    missing = total - present

    if missing == 0:
        self.projection.ensure_target_visible(target)
        return {
            "target": canonical,
            "pin": True,
            "hot": {"state": "ready", "present_bytes": present, "missing_bytes": 0},
            "fetch": None,
        }

    archived_missing = [f for f in files if not self.hot.has_file(f.collection_id, f.path) and f.copies]
    if len(archived_missing) != len([f for f in files if not self.hot.has_file(f.collection_id, f.path)]):
        raise Conflict("some bytes are not archived")

    reusable = self.fetches.find_reusable_fetch(canonical)
    fetch = reusable or self.fetches.create_fetch(
        target=canonical,
        entries=self._entries_for_files(archived_missing),
        copies=self._distinct_copy_hints(archived_missing),
    )

    return {
        "target": canonical,
        "pin": True,
        "hot": {"state": "waiting", "present_bytes": present, "missing_bytes": missing},
        "fetch": fetch,
    }
```

## `PinService.release(target)`

```python id="ltj5fk"
def release(self, raw_target: str):
    target = parse_target(raw_target)
    canonical = TargetStr(target.canonical)
    self.catalog.remove_pin(canonical)
    self.projection.reconcile_from_pins()
    return {"target": canonical, "pin": False}
```

## `FetchService.upload_entry(...)`

```python id="v98a5d"
def upload_entry(self, fetch_id: str, entry_id: str, sha256: str, content: bytes):
    actual = sha256_bytes(content)
    if actual != sha256:
        raise HashMismatch("hash mismatch")
    self.hot.put_file(Sha256Hex(actual), content)
    return self.fetch_store.accept_uploaded_entry(FetchId(fetch_id), EntryId(entry_id), sha256, content)
```

## `FetchService.complete(fetch_id)`

```python id="hwm0al"
def complete(self, fetch_id: str):
    if not self.fetch_store.can_complete(FetchId(fetch_id)):
        raise InvalidState("fetch is incomplete")
    self.fetch_store.mark_verifying(FetchId(fetch_id))
    manifest = self.fetch_store.get_manifest(FetchId(fetch_id))
    for entry in manifest.entries:
        self.projection.ensure_target_visible(parse_target(f"{manifest.target.split(':',1)[0]}:{entry.path}"))
    self.fetch_store.mark_done(FetchId(fetch_id))
    return self.get_completion_response(fetch_id)
```

# HTTP route map

Implement exactly this set first:

```text id="8a3yit"
POST /v1/collections/close
GET  /v1/search
GET  /v1/collections/{collection_id}
GET  /v1/plan
GET  /v1/images/{image_id}
GET  /v1/images/{image_id}/iso
POST /v1/images/{image_id}/copies
POST /v1/pin
POST /v1/release
GET  /v1/pins
GET  /v1/fetches/{fetch_id}
GET  /v1/fetches/{fetch_id}/manifest
PUT  /v1/fetches/{fetch_id}/files/{entry_id}
POST /v1/fetches/{fetch_id}/complete
```

Do not add browse endpoints.

# CLI scaffolding

Use Typer. Keep `arc` and `arc-disc` thin.

## Shared client

```python id="po6xx9"
class ApiClient:
    def close_collection(self, path: str) -> dict: ...
    def search(self, query: str, limit: int = 25) -> dict: ...
    def get_collection(self, collection_id: str) -> dict: ...
    def get_plan(self) -> dict: ...
    def get_image(self, image_id: str) -> dict: ...
    def download_iso(self, image_id: str) -> bytes: ...
    def register_copy(self, image_id: str, copy_id: str, location: str) -> dict: ...
    def pin(self, target: str) -> dict: ...
    def release(self, target: str) -> dict: ...
    def list_pins(self) -> dict: ...
    def get_fetch(self, fetch_id: str) -> dict: ...
    def get_fetch_manifest(self, fetch_id: str) -> dict: ...
    def upload_fetch_entry(self, fetch_id: str, entry_id: str, sha256: str, content: bytes) -> dict: ...
    def complete_fetch(self, fetch_id: str) -> dict: ...
```

## `arc`

```python id="nhz6jx"
@app.command("close")
def close_cmd(path: str, json_: bool = typer.Option(False, "--json")): ...

@app.command("find")
def find_cmd(query: str, limit: int = 25, json_: bool = typer.Option(False, "--json")): ...

@app.command("show")
def show_cmd(collection: str, json_: bool = typer.Option(False, "--json")): ...

@app.command("plan")
def plan_cmd(json_: bool = typer.Option(False, "--json")): ...

iso_app = typer.Typer()
@iso_app.command("get")
def iso_get_cmd(image_id: str, output: Path | None = None): ...

copy_app = typer.Typer()
@copy_app.command("add")
def copy_add_cmd(image_id: str, copy_id: str, at: str = typer.Option(..., "--at")): ...

@app.command("pin")
def pin_cmd(target: str, json_: bool = typer.Option(False, "--json")): ...

@app.command("release")
def release_cmd(target: str, json_: bool = typer.Option(False, "--json")): ...

@app.command("pins")
def pins_cmd(json_: bool = typer.Option(False, "--json")): ...

@app.command("fetch")
def fetch_cmd(fetch_id: str, json_: bool = typer.Option(False, "--json")): ...
```

## `arc-disc`

```python id="rtp5s8"
@app.command("fetch")
def fetch_cmd(
    fetch_id: str,
    device: str = typer.Option("/dev/sr0", "--device"),
    json_: bool = typer.Option(False, "--json"),
): ...
```

## `arc-disc fetch` algorithm

```python id="jxf8a3"
def run_fetch(fetch_id: str, device: str):
    manifest = client.get_fetch_manifest(fetch_id)

    for entry in manifest["entries"]:
        candidate = choose_copy(entry["copies"])
        prompt_insert(candidate["copy"], candidate["location"], device)
        enc_bytes = optical_reader.read(candidate["disc_path"], device=device)
        plain = crypto.decrypt_entry(enc_bytes, candidate["enc"])
        client.upload_fetch_entry(fetch_id, entry["id"], entry["sha256"], plain)

    return client.complete_fetch(fetch_id)
```

For MVP, `choose_copy(...)` can simply choose the first candidate.

# Acceptance test skeleton

Use pytest. Make acceptance tests hit the HTTP app and CLI binaries, not internals.

```text id="v2zjpj"
tests/
  acceptance/
    test_api_collections.py
    test_api_pins.py
    test_api_copies.py
    test_api_fetches.py
    test_api_selectors.py
    test_cli_arc.py
    test_cli_arc_disc.py
  fixtures/
    make_staging_tree.py
    make_disc_fixture.py
    app_factory.py
```

# Recommended acceptance tests

## API tests

```python id="ksac4j"
def test_close_collection_materializes_hot(api, staging_tree): ...
def test_close_duplicate_path_fails(api, staging_tree): ...

def test_pin_collection_already_hot_no_fetch(api, closed_collection): ...
def test_pin_file_already_hot_no_fetch(api, closed_collection): ...
def test_release_missing_pin_is_noop(api, closed_collection): ...
def test_broad_and_narrow_pins_interact_correctly(api, archived_and_hot_fixture): ...
def test_narrow_release_does_not_cancel_broad_pin(api, archived_and_hot_fixture): ...

def test_register_copy_increases_archived_coverage(api, planned_image_fixture): ...
def test_duplicate_copy_id_fails(api, planned_image_fixture): ...

def test_pin_cold_archived_file_creates_fetch(api, cold_archived_fixture): ...
def test_fetch_manifest_is_stable(api, cold_archived_fixture): ...
def test_wrong_hash_upload_fails(api, cold_archived_fixture): ...
def test_complete_fetch_makes_target_hot(api, cold_archived_fixture): ...
def test_pin_remains_after_fetch(api, cold_archived_fixture): ...

def test_invalid_targets_rejected(api): ...
def test_directory_target_selects_descendants_only(api, nested_fixture): ...
def test_file_target_selects_exactly_one_file(api, nested_fixture): ...
```

## CLI tests

```python id="hfhq2r"
def test_arc_pin_json_matches_api(cli_runner, api_server, fixture): ...
def test_arc_release_json_matches_api(cli_runner, api_server, fixture): ...
def test_arc_find_outputs_targets(cli_runner, api_server, fixture): ...
def test_arc_disc_fetch_completes_fetch(cli_runner, api_server, disc_fixture): ...
```

# Disc fixture strategy

Do not begin with real optical hardware in tests.

Make `arc-disc` depend on an `OpticalReader` port.
In acceptance tests, back it with a fake reader over fixture directories.

Fixture shape:

```text id="qgzzh4"
tests/fixtures/disc_01/
  payload/00/1f/7a.enc
  payload/00/1f/8b.enc
tests/fixtures/disc_02/
  payload/...
```

The fetch manifest returned by the test app should point at these paths.
That gives you deterministic recovery tests without hardware.

# Error mapping

Use these exact domain exceptions and map them once.

```python id="3sbykq"
class ArcError(Exception): ...
class BadRequest(ArcError): ...
class InvalidTarget(ArcError): ...
class NotFound(ArcError): ...
class Conflict(ArcError): ...
class InvalidState(ArcError): ...
class HashMismatch(ArcError): ...
```

FastAPI exception mapping:

```python id="vj2ibn"
InvalidTarget -> 400 {"error":{"code":"invalid_target","message":"..."}}
BadRequest    -> 400 {"error":{"code":"bad_request","message":"..."}}
NotFound      -> 404 {"error":{"code":"not_found","message":"..."}}
Conflict      -> 409 {"error":{"code":"conflict","message":"..."}}
InvalidState  -> 409 {"error":{"code":"invalid_state","message":"..."}}
HashMismatch  -> 409 {"error":{"code":"hash_mismatch","message":"..."}}
```

# Implementation order

Build in this order:

1. selector parser
2. domain errors and models
3. collection close
4. search and collection summary
5. plan and image endpoints
6. copy registration
7. pin and release
8. fetch summary and manifest
9. upload and complete
10. `arc`
11. `arc-disc`
12. acceptance tests for fake disc recovery

The first vertical slice to finish is:

* close collection
* search
* pin hot file
* release pin
* `arc close`
* `arc find`
* `arc pin`
* `arc release`

That gets you a working spine fast.

# Definition of done for MVP

The MVP is done when all of these are true:

1. every route in the route map exists
2. every CLI command in the command list exists
3. all selector invariants pass
4. all pin/release invariants pass
5. a cold archived file can be pinned, fetched via `arc-disc`, and become hot
6. no API or CLI requires browsing a collection tree
