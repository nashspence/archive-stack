Place deterministic fixture builders here.

The acceptance suite assumes three fixture families:

1. staged trees
   - fixture_empty_archive
   - fixture_staged_photos_2024
   - fixture_docs_with_invoice

2. planner and image fixtures
   - fixture_planned_image_img_2026_04_20_01
   - fixture_registered_copy_br_021_a

3. fetch and optical fixtures
   - fixture_fetch_fx_1_single_file
   - fixture_fake_optical_reader_success
   - fixture_fake_optical_reader_missing_entry
   - fixture_fake_optical_reader_bad_recovered_bytes

Guidelines:

- Every fixture must be deterministic and self-contained.
- Every byte count used by acceptance tests must derive from real fixture files, not hand-entered constants.
- Optical fixtures should model both successful recovery and the two important failure modes:
  missing payload and server-side rejection of incorrect recovered bytes.
- If release reconciliation is asynchronous internally, acceptance helpers should provide an eventual assertion such as
  wait_until_hot_matches_pins().
- CLI acceptance tests should use the same fixture families as the API acceptance tests instead of inventing parallel state.

Spec harness synchronization:

- `tests/fixtures/acceptance.py` runs one live FastAPI server, background reapers, and subprocess-driven CLI commands against shared in-memory `AcceptanceState`.
- Public fixture service methods exposed through `ServiceContainer` must use `_with_state_lock`; `tests/unit/test_acceptance_fixture_sync.py` enforces this for the protocol-backed service surface.
- Direct `AcceptanceSystem` helper access to `AcceptanceState` should hold `state.lock` only around the in-memory read or write. Do not hold it across HTTP requests or CLI subprocess calls.
- Private helper methods may assume their public caller already holds the lock, but reaper-facing entry points such as upload expiry, Glacier upload processing, and recovery-session processing must be explicitly locked.
